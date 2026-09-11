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
from fastapi.responses import RedirectResponse

from frontend_fastapi import backend_client
from frontend_fastapi.deps import get_session, get_template_context, require_data_custodian
from frontend_fastapi.flash import flash
from frontend_fastapi.models import Session, User
from frontend_fastapi.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", name="admin_overview")
async def admin_overview(
    request: Request, show: str = "unaddressed",
    user: User = Depends(require_data_custodian), ctx: dict = Depends(get_template_context),
):
    backend_error = None
    overview = {"expiring_projects": [], "recent_jobs": [], "audit_chain_check": None}
    project_status_counts: Counter = Counter()
    all_error_reports: list = []
    try:
        overview = await backend_client.admin_overview()
    except backend_client.BackendError as e:
        backend_error = f"Could not load admin overview: {e.detail}"
    try:
        project_status_counts = Counter(p["status"] for p in await backend_client.list_projects())
    except backend_client.BackendError as e:
        if backend_error is None:
            backend_error = f"Could not load project counts: {e.detail}"
    try:
        # Fetched unfiltered so both the show/hide-addressed toggle's pill
        # counts and the amber/red indicator (which is always about
        # *unaddressed* reports specifically, regardless of which list is
        # currently displayed) come from one backend call -- same "compute
        # client-side from an unfiltered call" reasoning as
        # project_status_counts above.
        all_error_reports = await backend_client.list_error_reports()
    except backend_client.BackendError as e:
        if backend_error is None:
            backend_error = f"Could not load error reports: {e.detail}"

    unaddressed_reports = [r for r in all_error_reports if not r.get("resolved_at")]
    if any(r.get("urgent") for r in unaddressed_reports):
        report_indicator = "red"
    elif unaddressed_reports:
        report_indicator = "amber"
    else:
        report_indicator = None
    show = show if show == "all" else "unaddressed"
    error_reports = unaddressed_reports if show == "unaddressed" else all_error_reports
    report_pills = [
        {"key": "unaddressed", "label": "Unaddressed", "count": len(unaddressed_reports), "active": show == "unaddressed"},
        {"key": "all", "label": "All", "count": len(all_error_reports), "active": show == "all"},
    ]

    return templates.TemplateResponse(request, "admin/overview.html", {
        **ctx, **overview, "project_status_counts": project_status_counts,
        "error_reports": error_reports, "report_indicator": report_indicator, "report_pills": report_pills,
        "show": show, "backend_error": backend_error,
    })


@router.post("/error_reports/{report_id}/resolve")
async def resolve_error_report(
    report_id: int, request: Request,
    user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
):
    try:
        await backend_client.mark_error_report_addressed(report_id, user.username)
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not mark report addressed: {e.detail}")
    return RedirectResponse(request.url_for("admin_overview"), status_code=303)
