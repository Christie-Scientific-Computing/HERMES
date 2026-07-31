"""
The ONLY module in this project that talks to the HERMES FastAPI backend.

Every call attaches the internal shared-secret header (HERMES_INTERNAL_KEY)
and, where relevant, the current Django user's username -- never a value
supplied by the browser. This is what makes "Django is the backend's sole,
authenticated caller" (CLAUDE.md's frontend architecture section) hold in
code, not just in intent: nothing here ever forwards a client-supplied
project_id/username verbatim without it having come from `request.user`
or a value this module itself resolved server-side.

Two call shapes:
  - `_get`/`_post`/`_delete`: ordinary JSON request/response, used by
    research_projects/ and the non-streaming parts of jobs/.
  - `stream_sse`: an async generator that opens the backend's SSE endpoint
    and yields raw `data: {...}` lines as they arrive, for jobs/views.py's
    relay view to re-frame with named `event:` lines and forward to the
    browser. No read timeout -- batch jobs can run long (see
    webui/core/backend_client.py, which had to special-case this too).
"""
from typing import AsyncIterator, Optional

import httpx
from django.conf import settings


def _headers() -> dict:
    headers = {}
    if settings.HERMES_INTERNAL_KEY:
        headers["X-Hermes-Internal-Key"] = settings.HERMES_INTERNAL_KEY
    return headers


class BackendError(Exception):
    """Wraps a non-2xx response from the backend with its status code and detail."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise BackendError(resp.status_code, detail)


def _get(path: str, params: Optional[dict] = None) -> dict:
    resp = httpx.get(f"{settings.BACKEND_URL}{path}", params=params, headers=_headers(), timeout=30)
    _raise_for_status(resp)
    return resp.json()


def _post(path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    resp = httpx.post(f"{settings.BACKEND_URL}{path}", json=json, params=params, headers=_headers(), timeout=30)
    _raise_for_status(resp)
    return resp.json()


def _delete(path: str, params: Optional[dict] = None) -> dict:
    resp = httpx.delete(f"{settings.BACKEND_URL}{path}", params=params, headers=_headers(), timeout=30)
    _raise_for_status(resp)
    return resp.json()


# ---- Projects (research_projects/ app) ----

def create_project(title: str, created_by: str, description: str = "", ethics_reference: str = "") -> dict:
    return _post("/projects", json={
        "title": title, "created_by": created_by,
        "description": description or None, "ethics_reference": ethics_reference or None,
    })


def submit_project(project_id: str, username: str) -> dict:
    return _post(f"/projects/{project_id}/submit", json={"username": username})


def review_project(project_id: str, reviewer: str, approved: bool, comment: str = "", expiry_date=None) -> dict:
    return _post(f"/projects/{project_id}/review", json={
        "reviewer": reviewer, "approved": approved, "comment": comment or None,
        "expiry_date": expiry_date.isoformat() if expiry_date else None,
    })


def revoke_project(project_id: str, revoked_by: str, comment: str = "") -> dict:
    return _post(f"/projects/{project_id}/revoke", json={"revoked_by": revoked_by, "comment": comment or None})


def list_projects(username: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    params = {}
    if username:
        params["username"] = username
    if status:
        params["status"] = status
    return _get("/projects", params=params)["projects"]


def get_project(project_id: str) -> dict:
    return _get(f"/projects/{project_id}")


def add_member(project_id: str, username: str, added_by: str, role: str = "member") -> dict:
    return _post(f"/projects/{project_id}/members", json={"username": username, "role": role, "added_by": added_by})


def remove_member(project_id: str, username: str, removed_by: str) -> dict:
    return _delete(f"/projects/{project_id}/members/{username}", params={"removed_by": removed_by})


def list_project_jobs(project_id: str) -> list[dict]:
    return _get(f"/projects/{project_id}/jobs")["jobs"]


def list_user_active_projects(username: str) -> list[dict]:
    """Active (approved, non-expired) projects `username` belongs to -- used to
    populate the project switcher and to re-validate the session's current
    project on every job-starting request."""
    return [p for p in list_projects(username=username, status="approved")]


# ---- Import (jobs/ app) ----

def single_import(job_id: str, mrn: str, import_level: str, project_id: str, username: str) -> dict:
    return _post("/import/single_import", json={
        "job_id": job_id, "mrn": mrn, "import_level": import_level,
        "project_id": project_id, "username": username,
    })


def find_patient(mrn: str, username: str, import_level: Optional[str] = None) -> dict:
    params = {"mrn": mrn, "username": username}
    if import_level:
        params["import_level"] = import_level
    return _get("/import/find_patient", params=params)


# ---- Export reference data (jobs/ app) ----

def get_orthanc_modalities(username: str) -> list[str]:
    return _get("/export/get_orthanc_modalities", params={"username": username})


def get_proknow_collections(username: str) -> list[str]:
    return _get("/export/get_proknow_collections", params={"username": username})


def proknow_upload_patient(job_id: str, mrn: str, collection: str, project_id: str, username: str) -> dict:
    return _post("/export/proknow_upload_patient", json={
        "job_id": job_id, "mrn": mrn, "collection": collection,
        "project_id": project_id, "username": username,
    })


# ---- Results (jobs/ app) ----

def job_summary(job_id: str) -> dict:
    return _get(f"/results/job/{job_id}")


def job_patients(job_id: str) -> dict:
    return _get(f"/results/job/{job_id}/patients")


def patient_timeline(job_id: str, mrn: str) -> dict:
    return _get(f"/results/patient/{job_id}/{mrn}")


def patient_timeline_all(mrn: str) -> dict:
    return _get(f"/results/patient/timeline/{mrn}/all")


# ---- Cancellation ----

def cancel_import(job_id: str) -> dict:
    return _post(f"/import/cancel/{job_id}")


def cancel_export(job_id: str) -> dict:
    return _post(f"/export/cancel/{job_id}")


# ---- SSE batch jobs (jobs/ app) ----

async def stream_sse(path: str, *, data: Optional[dict] = None, files: Optional[dict] = None) -> AsyncIterator[bytes]:
    """
    Open a POST to a backend SSE endpoint and yield raw response bytes as
    they arrive. `data`/`files` become a multipart request when `files` is
    given (the batch_*_file/dicom_move_file/etc. endpoints), otherwise a
    plain form/JSON POST is used to match what each backend route expects.

    No read timeout: large batch imports run for a long time and must not
    be killed mid-stream by a default httpx timeout (see
    webui/core/backend_client.py, which already had to special-case this).
    """
    timeout = httpx.Timeout(10.0, read=None)
    async with httpx.AsyncClient(base_url=settings.BACKEND_URL, timeout=timeout) as client:
        async with client.stream("POST", path, data=data, files=files, headers=_headers()) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                try:
                    detail = httpx.Response(resp.status_code, content=body).json().get("detail")
                except Exception:
                    detail = body.decode(errors="replace")
                raise BackendError(resp.status_code, detail or "backend request failed")
            async for chunk in resp.aiter_bytes():
                yield chunk
