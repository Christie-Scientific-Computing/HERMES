"""
Error report / suggestion form (item 06,
docs/plans/feature-round-implementation-plan.md). require_login is the
whole gate -- any authenticated user can report an issue, same as
notifications.py's own access-control story. `username` is always taken
from the session (never a form field), same as every other write in this
project.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from frontend_fastapi import backend_client
from frontend_fastapi.deps import get_session, get_template_context, require_login
from frontend_fastapi.flash import flash
from frontend_fastapi.forms.error_reports import ErrorReportForm
from frontend_fastapi.models import Session, User
from frontend_fastapi.templating import templates

router = APIRouter(prefix="/report", tags=["error_reports"])


@router.get("", name="error_report_form")
async def error_report_form(user: User = Depends(require_login), ctx: dict = Depends(get_template_context)):
    return templates.TemplateResponse(ctx["request"], "error_reports/report.html", {**ctx, "form": ErrorReportForm()})


@router.post("")
async def error_report_submit(
    request: Request, user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    form = ErrorReportForm(formdata=await request.form())
    if not form.validate():
        return templates.TemplateResponse(request, "error_reports/report.html", {**ctx, "form": form}, status_code=400)

    try:
        await backend_client.create_error_report(
            username=user.username, category=form.category.data, message=form.message.data,
            urgent=form.urgent.data, job_id=form.job_id.data or None,
        )
    except backend_client.BackendError as e:
        backend_error = f"Could not submit report: {e.detail}"
        return templates.TemplateResponse(
            request, "error_reports/report.html", {**ctx, "form": form, "backend_error": backend_error}, status_code=400,
        )

    flash(session, "success", "Thanks -- your report has been submitted.")
    return RedirectResponse(request.url_for("dashboard"), status_code=303)
