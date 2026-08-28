"""
Admin compliance dashboard (Phase 4,
docs/plans/frontend-rewrite-implementation-plan.md §6). Staff-only --
require_data_custodian is the ENTIRE access-control story here: the backend
endpoint this calls (backend/src/admin/endpoints.py) has no role-checking of
its own (HermesDB has no user/role table), so a route here that forgot this
dependency would be the actual gap, not a defense-in-depth nicety. See
CLAUDE.md's Phase 4 note and that backend router's own module docstring.

Project-status counts (draft/submitted/approved/etc.) aren't a field the
backend's /admin/overview returns -- there's no need for a dedicated backend
aggregate for a tally this trivial, so this view computes it itself from
the same unfiltered backend_client.list_projects() call staff already make
on /projects (research_projects.py's project_list, when user.is_staff).
"""
from collections import Counter

from fastapi import APIRouter, Depends, Request

from frontend_fastapi import backend_client
from frontend_fastapi.deps import get_template_context, require_data_custodian
from frontend_fastapi.models import User
from frontend_fastapi.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", name="admin_overview")
async def admin_overview(
    request: Request, user: User = Depends(require_data_custodian), ctx: dict = Depends(get_template_context),
):
    backend_error = None
    overview = {"expiring_projects": [], "recent_jobs": [], "audit_chain_check": None}
    project_status_counts: Counter = Counter()
    try:
        overview = await backend_client.admin_overview()
    except backend_client.BackendError as e:
        backend_error = f"Could not load admin overview: {e.detail}"
    try:
        project_status_counts = Counter(p["status"] for p in await backend_client.list_projects())
    except backend_client.BackendError as e:
        if backend_error is None:
            backend_error = f"Could not load project counts: {e.detail}"

    return templates.TemplateResponse(request, "admin/overview.html", {
        **ctx, **overview, "project_status_counts": project_status_counts, "backend_error": backend_error,
    })
