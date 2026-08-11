"""
Routers for research_projects/: list/create/detail/submit/review/revoke,
membership, and ethics-document upload/download/delete. Port of
research_projects/views.py (Django) -- see that file for the exact behavior
being matched.

Two things go beyond a faithful port, per
docs/frontend-rewrite-implementation-plan.md Phase 2:

1. The document-access-control fix (§4.2 of that plan). Confirmed by direct
   read of the Django app: documents are reached only via `{{ doc.file.url
   }}`, served through Django's raw MEDIA_URL with ZERO access control --
   upload_document is @login_required only, and there is no download view
   at all gating read access. Any logged-in user (or anyone who obtains a
   document's URL by any means) can download any project's ethics-approval
   documents today. Fixed here by adding download_document, gated on
   require_login AND (is_member OR is_staff) -- mirroring project_list's
   existing pattern for the same "who sees this" question, and deliberately
   NOT membership-only (a data-custodian reviewer is structurally not a
   project member, and needs to open a submitted project's documents before
   approving/rejecting it).

2. upload_document itself has the identical gap one level up: the Django
   view is @login_required only, with no is_member check either -- the
   detail template merely HIDES the upload form from non-members
   client-side (`{% if is_member %}`), which is not enforcement. Any
   logged-in user could POST a document onto any project_id. Closed the
   same way as (1): require_login AND (is_member OR is_staff).

delete_document is new (no Django precedent) -- gated the same as download,
plus additionally restricted to the uploader or staff specifically, since a
delete is more consequential than a read.
"""
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session as DBSession

from frontend_fastapi import backend_client
from frontend_fastapi.database import get_db
from frontend_fastapi.deps import get_session, get_template_context, require_data_custodian, require_login
from frontend_fastapi.flash import flash
from frontend_fastapi.forms.research_projects import AddMemberForm, CreateProjectForm, ReviewProjectForm
from frontend_fastapi.models import ProjectDocument, Session, User
from frontend_fastapi.settings import MEDIA_ROOT
from frontend_fastapi.templating import templates

router = APIRouter(prefix="/projects", tags=["research_projects"])

EXPIRING_SOON_WITHIN_DAYS = 30
_DOCUMENTS_SUBDIR = "ethics_documents"
_MAX_DOCUMENT_SIZE_BYTES = 50 * 1024 * 1024  # generous for a PDF ethics certificate, bounds a careless/hostile upload
_COPY_CHUNK_SIZE = 1024 * 1024


class _DocumentTooLargeError(Exception):
    """Raised by _save_document_sync when an upload exceeds _MAX_DOCUMENT_SIZE_BYTES."""


def _is_member(project: dict, username: str) -> bool:
    return any(m["username"] == username for m in project["members"])


def _expiring_soon(active_projects: list[dict], within_days: int = EXPIRING_SOON_WITHIN_DAYS) -> list[dict]:
    """Filters a list of projects down to ones that are currently approved,
    non-revoked, and whose expiry_date falls within the next `within_days`
    days. Explicitly re-checks `status == "approved"` itself rather than
    trusting the caller to have pre-filtered -- list.html's call site
    passes get_template_context's nav_active_projects (already
    approved+non-expired, via backend_client.list_user_active_projects),
    but detail.html's passes a single project of ANY status, which might
    have a future expiry_date left over from a since-revoked approval. An
    open-ended approval (expiry_date is None) never qualifies -- there's
    nothing to warn about. Each returned dict gains a `days_remaining` key."""
    now = datetime.now(timezone.utc)
    soon = []
    for project in active_projects:
        if project.get("status") != "approved":
            continue
        expiry = project.get("expiry_date")
        if not expiry:
            continue
        expiry_dt = datetime.fromisoformat(expiry) if isinstance(expiry, str) else expiry
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        days_remaining = (expiry_dt - now).days
        if 0 <= days_remaining <= within_days:
            soon.append({**project, "days_remaining": days_remaining})
    return soon


async def _get_project_or_flash(session: Session, project_id: str) -> dict | None:
    try:
        return await backend_client.get_project(project_id)
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not load project: {e.detail}")
        return None


@router.get("", name="project_list")
async def project_list(request: Request, status: str = "", user: User = Depends(require_login),
                        ctx: dict = Depends(get_template_context)):
    """Staff see every project regardless of status/membership (no user can
    hide a project from an admin); everyone else sees only their own."""
    status_filter = status or None if user.is_staff else None
    backend_error = None
    try:
        if user.is_staff:
            projects = await backend_client.list_projects(status=status_filter)
        else:
            projects = await backend_client.list_projects(username=user.username)
    except backend_client.BackendError as e:
        backend_error = f"Could not load projects: {e.detail}"
        projects = []
    return templates.TemplateResponse(request, "research_projects/list.html", {
        **ctx, "projects": projects, "status": status_filter, "backend_error": backend_error,
        "expiring_soon": _expiring_soon(ctx["nav_active_projects"]),
    })


@router.get("/new", name="project_create")
async def project_create_form(user: User = Depends(require_login), ctx: dict = Depends(get_template_context)):
    return templates.TemplateResponse(ctx["request"], "research_projects/create.html", {**ctx, "form": CreateProjectForm()})


@router.post("/new")
async def project_create_submit(
    request: Request, user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    form = CreateProjectForm(formdata=await request.form())
    if not form.validate():
        return templates.TemplateResponse(request, "research_projects/create.html", {**ctx, "form": form}, status_code=400)

    try:
        project = await backend_client.create_project(
            title=form.title.data, created_by=user.username,
            description=form.description.data or "", ethics_reference=form.ethics_reference.data or "",
        )
    except backend_client.BackendError as e:
        # Inline, not flash(): this re-renders the SAME response rather than
        # redirecting, and ctx's flashes were already popped by
        # get_template_context before this handler body ran -- a flash()
        # call here would only ever be seen on the NEXT page load, not this
        # one. Same reasoning as project_list/review_queue's backend_error.
        backend_error = f"Could not create project: {e.detail}"
        return templates.TemplateResponse(
            request, "research_projects/create.html", {**ctx, "form": form, "backend_error": backend_error}, status_code=400,
        )

    flash(session, "success", "Project created as a draft. Submit it for review when ready.")
    return RedirectResponse(request.url_for("project_detail", project_id=project["project_id"]), status_code=303)


@router.get("/review", name="review_queue")
async def review_queue(request: Request, user: User = Depends(require_data_custodian), ctx: dict = Depends(get_template_context)):
    backend_error = None
    try:
        pending = await backend_client.list_projects(status="submitted")
    except backend_client.BackendError as e:
        backend_error = f"Could not load review queue: {e.detail}"
        pending = []
    return templates.TemplateResponse(request, "research_projects/review_queue.html", {
        **ctx, "projects": pending, "backend_error": backend_error,
    })


@router.get("/{project_id}", name="project_detail")
async def project_detail(
    request: Request, project_id: str, db: DBSession = Depends(get_db),
    user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    project = await _get_project_or_flash(session, project_id)
    if project is None:
        return RedirectResponse(request.url_for("project_list"), status_code=303)

    is_member = _is_member(project, user.username)
    documents = (
        db.query(ProjectDocument).filter_by(project_id=project_id).order_by(ProjectDocument.uploaded_at.desc()).all()
    )
    try:
        jobs = await backend_client.list_project_jobs(project_id)
    except backend_client.BackendError:
        jobs = []

    # Contextual to THIS project specifically (unlike list.html's aggregate
    # banner across every project the viewer belongs to) -- only meaningful
    # for a member of an approved project, not e.g. a staff reviewer who
    # isn't otherwise invested in it.
    days_remaining = None
    if is_member:
        matches = _expiring_soon([project])
        if matches:
            days_remaining = matches[0]["days_remaining"]

    return templates.TemplateResponse(request, "research_projects/detail.html", {
        **ctx,
        "project": project,
        "is_member": is_member,
        "can_manage_documents": is_member or user.is_staff,
        "documents": documents,
        "jobs": jobs,
        "add_member_form": AddMemberForm(),
        "review_form": ReviewProjectForm(),
        "days_remaining": days_remaining,
    })


@router.post("/{project_id}/submit")
async def project_submit(project_id: str, request: Request, user: User = Depends(require_login), session: Session = Depends(get_session)):
    try:
        await backend_client.submit_project(project_id, user.username)
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not submit project: {e.detail}")
    else:
        flash(session, "success", "Project submitted for review.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)


@router.post("/{project_id}/review")
async def project_review(
    project_id: str, request: Request, user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
):
    form = ReviewProjectForm(formdata=await request.form())
    if form.validate():
        approved = form.decision.data == "approve"
        try:
            await backend_client.review_project(
                project_id, reviewer=user.username, approved=approved,
                comment=form.comment.data or "", expiry_date=form.expiry_date.data,
            )
        except backend_client.BackendError as e:
            flash(session, "error", f"Could not review project: {e.detail}")
        else:
            flash(session, "success", f"Project {'approved' if approved else 'rejected'}.")
    else:
        flash(session, "error", "; ".join(err for errs in form.errors.values() for err in errs) or "Invalid review decision.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)


@router.post("/{project_id}/revoke")
async def project_revoke(
    project_id: str, request: Request, user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
):
    formdata = await request.form()
    comment = str(formdata.get("comment", ""))
    try:
        await backend_client.revoke_project(project_id, revoked_by=user.username, comment=comment)
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not revoke project: {e.detail}")
    else:
        flash(session, "success", "Project revoked.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)


@router.post("/{project_id}/members")
async def project_add_member(
    project_id: str, request: Request, user: User = Depends(require_login), session: Session = Depends(get_session),
):
    form = AddMemberForm(formdata=await request.form())
    if form.validate():
        try:
            await backend_client.add_member(project_id, form.username.data, added_by=user.username, role=form.role.data)
        except backend_client.BackendError as e:
            flash(session, "error", f"Could not add member: {e.detail}")
        else:
            flash(session, "success", f"Added {form.username.data} to the project.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)


@router.post("/{project_id}/members/{username}/remove")
async def project_remove_member(
    project_id: str, username: str, request: Request,
    user: User = Depends(require_login), session: Session = Depends(get_session),
):
    try:
        await backend_client.remove_member(project_id, username, removed_by=user.username)
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not remove member: {e.detail}")
    else:
        flash(session, "success", f"Removed {username} from the project.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)


def _save_document_sync(source, project_id: str, original_filename: str) -> str:
    """Blocking, streaming file copy, run off the event loop -- see the
    module docstring's async-threading note. `source` is UploadFile.file (a
    SpooledTemporaryFile), copied in bounded chunks rather than read into
    memory as one bytes object first -- the earlier version of this
    function took `bytes` straight from `await file.read()`, which for a
    large upload means the full file sits in process memory (twice over,
    briefly, alongside this function's own copy) before a single byte
    reaches disk. Chunking bounds that to _COPY_CHUNK_SIZE regardless of
    upload size, and lets the _MAX_DOCUMENT_SIZE_BYTES cap below reject an
    oversized upload partway through instead of only after fully buffering
    it.

    Stored under a random name, never the caller-supplied filename:
    `original_filename` is untrusted and only ever used for display / the
    download Content-Disposition header, so it can't be used to influence
    the path written to disk."""
    ext = Path(original_filename).suffix[:20]  # bounded: an attacker-controlled "extension" shouldn't grow unbounded
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest_dir = MEDIA_ROOT / _DOCUMENTS_SUBDIR / project_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / stored_name
    written = 0
    source.seek(0)
    try:
        with dest_path.open("wb") as dest:
            while chunk := source.read(_COPY_CHUNK_SIZE):
                written += len(chunk)
                if written > _MAX_DOCUMENT_SIZE_BYTES:
                    raise _DocumentTooLargeError(f"Document exceeds the {_MAX_DOCUMENT_SIZE_BYTES}-byte limit")
                dest.write(chunk)
    except _DocumentTooLargeError:
        dest_path.unlink(missing_ok=True)
        raise
    return str(Path(_DOCUMENTS_SUBDIR) / project_id / stored_name)


@router.post("/{project_id}/documents/upload")
async def upload_document(
    project_id: str, request: Request, file: UploadFile = File(...),
    db: DBSession = Depends(get_db), user: User = Depends(require_login), session: Session = Depends(get_session),
):
    project = await _get_project_or_flash(session, project_id)
    if project is None:
        return RedirectResponse(request.url_for("project_list"), status_code=303)
    if not (_is_member(project, user.username) or user.is_staff):
        raise HTTPException(status_code=403, detail="Only a project member or data custodian may upload documents")

    original_filename = (file.filename or "document")[:255]  # matches ProjectDocument.original_filename's column width
    try:
        file_path = await run_in_threadpool(_save_document_sync, file.file, project_id, original_filename)
    except _DocumentTooLargeError:
        flash(session, "error", f"Document too large (max {_MAX_DOCUMENT_SIZE_BYTES // (1024 * 1024)}MB).")
        return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)

    db.add(ProjectDocument(
        project_id=project_id, file_path=file_path, original_filename=original_filename, uploaded_by=user.username,
    ))
    flash(session, "success", "Document uploaded.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)


@router.get("/{project_id}/documents/{doc_id}/download")
async def download_document(
    project_id: str, doc_id: int, db: DBSession = Depends(get_db), user: User = Depends(require_login),
):
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or doc.project_id != project_id:
        raise HTTPException(status_code=404)
    try:
        project = await backend_client.get_project(project_id)
    except backend_client.BackendError:
        raise HTTPException(status_code=404)
    if not (_is_member(project, user.username) or user.is_staff):
        raise HTTPException(status_code=403)
    full_path = MEDIA_ROOT / doc.file_path
    if not full_path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(full_path, filename=doc.original_filename)


@router.post("/{project_id}/documents/{doc_id}/delete")
async def delete_document(
    project_id: str, doc_id: int, request: Request, db: DBSession = Depends(get_db),
    user: User = Depends(require_login), session: Session = Depends(get_session),
):
    doc = db.get(ProjectDocument, doc_id)
    if doc is None or doc.project_id != project_id:
        raise HTTPException(status_code=404)

    project = await _get_project_or_flash(session, project_id)
    if project is None:
        return RedirectResponse(request.url_for("project_list"), status_code=303)
    can_read = _is_member(project, user.username) or user.is_staff
    can_delete = can_read and (doc.uploaded_by == user.username or user.is_staff)
    if not can_delete:
        raise HTTPException(status_code=403)

    full_path = MEDIA_ROOT / doc.file_path
    full_path.unlink(missing_ok=True)
    db.delete(doc)
    flash(session, "success", "Document deleted.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)
