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
from typing import Optional

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
