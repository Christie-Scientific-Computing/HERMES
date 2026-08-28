"""
The ONLY module in this project that talks to the HERMES FastAPI backend.
Port of frontend/hermes_frontend/backend_client.py (Django) -- same shape,
same call sites, minus the django.conf.settings import. Every call attaches
the internal shared-secret header (HERMES_INTERNAL_KEY) and, where relevant,
the current user's username -- never a value supplied by the browser.

Async and backed by one shared, connection-pooled httpx.AsyncClient (closed
in main.py's lifespan) rather than one-off sync httpx.get() calls: this is
called from deps.get_template_context on every authenticated page render,
so a sync call would tie up a FastAPI threadpool worker (bounded, shared
with every other sync dependency in the app) for as long as the backend
takes to answer -- a slow-but-not-down backend would starve unrelated
requests, not just the ones actually waiting on it.

Grows phase by phase alongside the rest of this rewrite (see
docs/frontend-rewrite-implementation-plan.md): only what Phase 0 needs
(the active-projects nav banner) lives here so far.
"""
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import httpx

from frontend_fastapi.settings import BACKEND_URL, HERMES_INTERNAL_KEY


class BackendError(Exception):
    """Wraps a non-2xx response from the backend with its status code and detail."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


def _headers() -> dict:
    headers = {}
    if HERMES_INTERNAL_KEY:
        headers["X-Hermes-Internal-Key"] = HERMES_INTERNAL_KEY
    return headers


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise BackendError(resp.status_code, detail)


# Module-level and reused across requests -- httpx.AsyncClient pools
# connections internally, so this avoids paying a fresh TCP+TLS handshake
# to the backend on every call. Closed in main.py's lifespan on shutdown.
client = httpx.AsyncClient(base_url=BACKEND_URL, timeout=30, headers=_headers())


async def _get(path: str, params: Optional[dict] = None) -> dict:
    resp = await client.get(path, params=params)
    _raise_for_status(resp)
    return resp.json()


async def _post(path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    resp = await client.post(path, json=json, params=params)
    _raise_for_status(resp)
    return resp.json()


async def _delete(path: str, params: Optional[dict] = None) -> dict:
    resp = await client.delete(path, params=params)
    _raise_for_status(resp)
    return resp.json()


# ---- Projects (research_projects, ported in Phase 2) ----

async def create_project(title: str, created_by: str, description: str = "", ethics_reference: str = "") -> dict:
    return await _post("/projects", json={
        "title": title, "created_by": created_by,
        "description": description or None, "ethics_reference": ethics_reference or None,
    })


async def submit_project(project_id: str, username: str) -> dict:
    return await _post(f"/projects/{project_id}/submit", json={"username": username})


async def review_project(project_id: str, reviewer: str, approved: bool, comment: str = "", expiry_date=None) -> dict:
    # expiry_date may arrive as a datetime.date (WTForms' DateField),
    # datetime.datetime, an already-isoformatted str, or None -- httpx's
    # JSON encoder only knows how to serialize plain str/None, so normalize
    # anything date-like (both date and datetime expose .isoformat()) here
    # rather than pushing that distinction onto every caller.
    return await _post(f"/projects/{project_id}/review", json={
        "reviewer": reviewer, "approved": approved, "comment": comment or None,
        "expiry_date": expiry_date.isoformat() if hasattr(expiry_date, "isoformat") else expiry_date,
    })


async def revoke_project(project_id: str, revoked_by: str, comment: str = "") -> dict:
    return await _post(f"/projects/{project_id}/revoke", json={"revoked_by": revoked_by, "comment": comment or None})


async def list_projects(username: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    params = {}
    if username:
        params["username"] = username
    if status:
        params["status"] = status
    return (await _get("/projects", params=params))["projects"]


async def get_project(project_id: str) -> dict:
    return await _get(f"/projects/{project_id}")


async def add_member(project_id: str, username: str, added_by: str, role: str = "member") -> dict:
    return await _post(f"/projects/{project_id}/members", json={"username": username, "role": role, "added_by": added_by})


async def remove_member(project_id: str, username: str, removed_by: str) -> dict:
    return await _delete(f"/projects/{project_id}/members/{username}", params={"removed_by": removed_by})


async def list_project_jobs(project_id: str) -> list[dict]:
    return (await _get(f"/projects/{project_id}/jobs"))["jobs"]


async def list_user_active_projects(username: str) -> list[dict]:
    """Active (approved, non-expired) projects `username` belongs to -- used
    by deps.get_template_context to populate the nav's active-projects
    banner, live, on every request (never cached)."""
    return await list_projects(username=username, status="approved")


# ---- Superuser bypass project (jobs/, ported from hermes_frontend) ----
#
# Superusers shouldn't need ethics approval to use the tool -- Django
# auto-provisions one genuine, permanently-approved project and makes the
# superuser a real member of it rather than threading a new trusted
# `is_superuser` field through every gated backend endpoint. The backend's
# enforcement (backend/src/projects/enforcement.py) is completely untouched
# -- a superuser is just an ordinary active project member as far as
# require_project_member is concerned.

_SUPERUSER_BYPASS_MARKER = "__SUPERUSER_BYPASS__"
_SUPERUSER_BYPASS_SYSTEM_USER = "system"
_FAR_FUTURE_EXPIRY = datetime(9999, 12, 31, tzinfo=timezone.utc)
# Plain module-level cache, not a proper cache abstraction (this project has
# none yet) -- deliberately a pure perf optimization, mirroring Django's own
# LocMemCache-backed version: correctness never depends on it, each process
# falls back to the live lookup if its own copy is empty/unset, which
# matters once this runs under multiple uvicorn workers (each has its own).
_bypass_project_id_cache: Optional[str] = None


async def _find_or_create_superuser_bypass_project() -> str:
    # Approving requires a non-null expiry_date (POST /projects/{id}/review
    # rejects approved=True with none) -- there's no "never expires" option
    # through this API, so a far-future sentinel stands in for one.
    existing = [
        p for p in await list_projects(status="approved")
        if p.get("ethics_reference") == _SUPERUSER_BYPASS_MARKER
    ]
    if existing:
        return existing[0]["project_id"]

    project = await create_project(
        title="Administrative Access (superuser bypass)",
        created_by=_SUPERUSER_BYPASS_SYSTEM_USER,
        description="Auto-provisioned project granting superusers import/export access without ethics review.",
        ethics_reference=_SUPERUSER_BYPASS_MARKER,
    )
    project_id = project["project_id"]
    await submit_project(project_id, _SUPERUSER_BYPASS_SYSTEM_USER)
    await review_project(
        project_id, reviewer=_SUPERUSER_BYPASS_SYSTEM_USER, approved=True,
        comment="Auto-approved: administrative bypass project", expiry_date=_FAR_FUTURE_EXPIRY,
    )
    # Accepted, harmless race: two processes racing on first-ever use could
    # each create a duplicate bypass project (no unique constraint exists or
    # is needed for this) -- membership is what gates authorization, and
    # either project works identically for whoever ends up in it.
    return project_id


async def ensure_superuser_bypass_project(username: str) -> str:
    """Ensure `username` is an active member of the bypass project, creating
    it on first-ever use, and return its project_id."""
    global _bypass_project_id_cache
    if _bypass_project_id_cache is None:
        _bypass_project_id_cache = await _find_or_create_superuser_bypass_project()

    # added_by must always be the fixed system identity, never the target
    # user's own username: the add-member endpoint requires added_by to
    # already be a project member, and "system" (auto-added as owner at
    # creation) always qualifies, whereas a brand-new superuser wouldn't yet.
    await add_member(_bypass_project_id_cache, username, added_by=_SUPERUSER_BYPASS_SYSTEM_USER)
    return _bypass_project_id_cache


# ---- Import/export (jobs/, ported from hermes_frontend) ----

async def _post_batch_file(path: str, job_id: str, filename: str, content: bytes,
                            project_id: str, username: str, **extra_fields) -> dict:
    """
    Shared by batch_import_file/dicom_move_file/proknow_upload_file/
    combined_import_export_file below: a multipart POST to one of the
    backend's queue-enqueue endpoints (docs/worker-queue-design.md),
    returning the {"job_id", "total"} receipt once the backend has enqueued
    every row. Takes raw bytes rather than a file path -- nothing here
    stages the upload to local disk (see routers/jobs.py's module docstring).
    """
    resp = await client.post(
        path,
        data={"job_id": job_id, "project_id": project_id, "username": username, **extra_fields},
        files={"file": (filename, content, "text/csv")},
    )
    _raise_for_status(resp)
    return resp.json()


async def batch_import_file(job_id: str, filename: str, content: bytes, project_id: str,
                             username: str, import_level: str) -> dict:
    return await _post_batch_file("/import/batch_import_file", job_id, filename, content,
                                   project_id, username, import_level=import_level)


async def combined_import_export_file(job_id: str, filename: str, content: bytes, project_id: str,
                                       username: str, import_level: str, export_kind: str,
                                       destination_or_collection: str, message_id: Optional[int] = None) -> dict:
    """
    Import, then chain a matching export for each patient once its import
    succeeds (backend/worker.py's _maybe_chain_export). Hits the same
    /import/batch_import_file endpoint as batch_import_file, just with the
    extra export_kind/destination-or-collection/message_id fields that opt a
    job into chaining -- see that endpoint's docstring for why this isn't a
    separate backend endpoint.
    """
    extra = {"import_level": import_level, "export_kind": export_kind}
    if export_kind == "dicom_move":
        extra["destination"] = destination_or_collection
        if message_id is not None:
            extra["message_id"] = message_id
    elif export_kind == "proknow_upload":
        extra["collection"] = destination_or_collection
    else:
        raise ValueError(f"Unknown export_kind: {export_kind}")
    return await _post_batch_file("/import/batch_import_file", job_id, filename, content,
                                   project_id, username, **extra)


async def get_orthanc_modalities(username: str) -> list[str]:
    return await _get("/export/get_orthanc_modalities", params={"username": username})


async def get_proknow_collections(username: str) -> list[str]:
    return await _get("/export/get_proknow_collections", params={"username": username})


async def dicom_move_file(job_id: str, filename: str, content: bytes, project_id: str,
                           username: str, destination: str, message_id: Optional[int] = None) -> dict:
    # message_id is genuinely optional (an ordinary export has none) -- omit
    # the key entirely rather than sending it as None, which httpx would
    # otherwise stringify into the literal multipart value "None".
    extra = {"message_id": message_id} if message_id is not None else {}
    return await _post_batch_file("/export/dicom_move_file", job_id, filename, content,
                                   project_id, username, destination=destination, **extra)


async def proknow_upload_file(job_id: str, filename: str, content: bytes, project_id: str,
                               username: str, collection: str) -> dict:
    return await _post_batch_file("/export/proknow_upload_file", job_id, filename, content,
                                   project_id, username, collection=collection)


# ---- Results (jobs/, ported from hermes_frontend) ----

async def job_summary(job_id: str) -> dict:
    return await _get(f"/results/job/{job_id}")


async def job_patients(job_id: str) -> dict:
    return await _get(f"/results/job/{job_id}/patients")


async def job_patients_summary(job_id: str) -> dict:
    """Per-patient source-system presence (in_mosaiq/in_pinnacle/in_proknow)
    for an import job -- null for each key on export-only jobs/patients."""
    return await _get(f"/results/job/{job_id}/patients/summary")


async def patient_timeline(job_id: str, mrn: str) -> dict:
    return await _get(f"/results/patient/{job_id}/{mrn}")


async def patient_timeline_all(mrn: str) -> dict:
    return await _get(f"/results/patient/timeline/{mrn}/all")


async def patient_plans(mrn: str) -> dict:
    """Every plan PinnacleExport recorded for this patient, across all jobs --
    the plans table has no job_id. `available` is False when PinnacleExport's
    schema isn't present at all, which is not the same as "no plans"."""
    return await _get(f"/results/patient/{mrn}/plans")


# ---- Cancellation ----

async def cancel_import(job_id: str) -> dict:
    return await _post(f"/import/cancel/{job_id}")


# ---- SSE (jobs/, ported from hermes_frontend) ----

async def stream_sse(path: str) -> AsyncIterator[bytes]:
    """
    Open a GET request to a backend SSE endpoint -- the queue's observer
    stream, GET /results/job/{job_id}/stream (docs/worker-queue-design.md)
    -- and yield raw response bytes as they arrive. routers/jobs.py's
    job_stream is the sole caller, re-framing each `data: {...}` line with a
    matching `event: <type>` line before relaying it to the browser.

    Deliberately opens its own dedicated httpx.AsyncClient rather than
    reusing the shared pooled `client` above: a batch job can run for a long
    time, and the connection must not be killed mid-stream by that client's
    fixed 30-second timeout.
    """
    timeout = httpx.Timeout(10.0, read=None)
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=timeout, headers=_headers()) as stream_client:
        async with stream_client.stream("GET", path) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                try:
                    detail = httpx.Response(resp.status_code, content=body).json().get("detail")
                except Exception:
                    detail = body.decode(errors="replace")
                raise BackendError(resp.status_code, detail or "backend request failed")
            async for chunk in resp.aiter_bytes():
                yield chunk


# ---- Admin dashboard + notifications (Phase 4) ----

async def admin_overview(within_days: int = 30, limit: int = 50) -> dict:
    return await _get("/admin/overview", params={"within_days": within_days, "limit": limit})


async def list_notifications(username: str, unread_only: bool = False, limit: int = 20) -> list[dict]:
    return (await _get("/notifications", params={
        "username": username, "unread_only": unread_only, "limit": limit,
    }))["notifications"]


async def mark_notification_read(notification_id: int, username: str) -> dict:
    return await _post(f"/notifications/{notification_id}/read", params={"username": username})
