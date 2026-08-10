"""
The ONLY module in this project that talks to the HERMES FastAPI backend.
Port of frontend/hermes_frontend/backend_client.py (Django) -- same shape,
same call sites, minus the django.conf.settings import. Every call attaches
the internal shared-secret header (HERMES_INTERNAL_KEY) and, where relevant,
the current user's username -- never a value supplied by the browser.

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


def _get(path: str, params: Optional[dict] = None) -> dict:
    resp = httpx.get(f"{BACKEND_URL}{path}", params=params, headers=_headers(), timeout=30)
    _raise_for_status(resp)
    return resp.json()


# ---- Projects (research_projects, ported in Phase 2) ----

def list_projects(username: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    params = {}
    if username:
        params["username"] = username
    if status:
        params["status"] = status
    return _get("/projects", params=params)["projects"]


def list_user_active_projects(username: str) -> list[dict]:
    """Active (approved, non-expired) projects `username` belongs to -- used
    by deps.get_template_context to populate the nav's active-projects
    banner, live, on every request (never cached)."""
    return list_projects(username=username, status="approved")
