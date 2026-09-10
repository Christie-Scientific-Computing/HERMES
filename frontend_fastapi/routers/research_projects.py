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
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session as DBSession
from starlette.datastructures import UploadFile as _RawUploadFile
# ^ fastapi.UploadFile (imported above, used by upload_document's declared
# File(...) parameter) is a DIFFERENT class from what a raw
# `await request.form()` parse yields for a file field (used below, for the
# same reason jobs.py's submit_job imports it this way too) -- an
# isinstance check against the wrong one always fails.

from frontend_fastapi import backend_client
from frontend_fastapi.database import get_db
from frontend_fastapi.deps import (
    expiring_soon,
    get_session,
    get_template_context,
    require_data_custodian,
    require_login,
)
from frontend_fastapi.flash import flash
from frontend_fastapi.forms.research_projects import (
    AddMemberForm,
    AmendmentDecisionForm,
    AmendProjectForm,
    CreateProjectForm,
    ReviewProjectForm,
)
from frontend_fastapi.models import ProjectDocument, Session, User
from frontend_fastapi.settings import MEDIA_ROOT
from frontend_fastapi.templating import templates

router = APIRouter(prefix="/projects", tags=["research_projects"])

_DOCUMENTS_SUBDIR = "ethics_documents"
_MAX_DOCUMENT_SIZE_BYTES = 50 * 1024 * 1024  # generous for a PDF ethics certificate, bounds a careless/hostile upload
_COPY_CHUNK_SIZE = 1024 * 1024
# The requested-patients list is parsed straight into project_requested_patients
# rows (backend-owned) -- unlike ethics documents, nothing here downloads or
# audits the raw file later, so it's read and discarded rather than saved
# under MEDIA_ROOT via _save_document_sync's pattern. Bounds mirror that
# function's own reasoning (a size cap enforced mid-stream, not only after
# fully buffering); _MAX_REQUESTED_PATIENTS_ROWS additionally caps how many
# MRN rows a single upload can add, a basic guard against a pathological
# (or hostile) file with millions of tiny "lines".
_MAX_REQUESTED_PATIENTS_FILE_BYTES = 5 * 1024 * 1024
_MAX_REQUESTED_PATIENTS_ROWS = 20_000


class _DocumentTooLargeError(Exception):
    """Raised by _save_document_sync when an upload exceeds _MAX_DOCUMENT_SIZE_BYTES."""


def _is_member(project: dict, username: str) -> bool:
    return any(m["username"] == username for m in project["members"])


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
        "expiring_soon": expiring_soon(ctx["nav_active_projects"]),
    })


async def _fetch_destination_choices(username: str) -> tuple[list[str], list[str], Optional[str]]:
    """Live Orthanc modalities + ProKnow collections for the destination
    picker -- same two backend_client calls jobs.py's submit_job already
    uses. Both require the caller to already have SOME active approved
    project (backend/src/projects/enforcement.py's require_any_active_project)
    -- a user's very first-ever project has none yet, so this degrades to
    an error banner for them specifically, exactly like submit_job.html
    already does when Orthanc/ProKnow itself is unreachable. Not a new gap;
    inherited from reusing the same calls."""
    try:
        modalities = await backend_client.get_orthanc_modalities(username)
    except backend_client.BackendError as e:
        return [], [], f"Could not load destination choices: {e.detail}"
    try:
        collections = await backend_client.get_proknow_collections(username)
    except backend_client.BackendError as e:
        return modalities, [], f"Could not load destination choices: {e.detail}"
    return modalities, collections, None


@router.get("/new", name="project_create")
async def project_create_form(user: User = Depends(require_login), ctx: dict = Depends(get_template_context)):
    modalities, collections, destination_error = await _fetch_destination_choices(user.username)
    form = CreateProjectForm()
    form.set_destination_choices(modalities, collections)
    return templates.TemplateResponse(ctx["request"], "research_projects/create.html", {
        **ctx, "form": form, "destination_error": destination_error,
    })


@router.post("/new")
async def project_create_submit(
    request: Request, user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    formdata = await request.form()
    upload = formdata.get("requested_patients_file")
    has_file = isinstance(upload, _RawUploadFile) and bool(upload.filename)

    modalities, collections, destination_error = await _fetch_destination_choices(user.username)
    form = CreateProjectForm(formdata=formdata)
    form.set_destination_choices(modalities, collections)
    if not form.validate():
        return templates.TemplateResponse(request, "research_projects/create.html", {
            **ctx, "form": form, "destination_error": destination_error,
        }, status_code=400)

    try:
        project = await backend_client.create_project(
            title=form.title.data, created_by=user.username,
            description=form.description.data or "", ethics_reference=form.ethics_reference.data or "",
            destinations=form.destinations(), message_id=form.message_id.data,
        )
    except backend_client.BackendError as e:
        # Inline, not flash(): this re-renders the SAME response rather than
        # redirecting, and ctx's flashes were already popped by
        # get_template_context before this handler body ran -- a flash()
        # call here would only ever be seen on the NEXT page load, not this
        # one. Same reasoning as project_list/review_queue's backend_error.
        backend_error = f"Could not create project: {e.detail}"
        return templates.TemplateResponse(
            request, "research_projects/create.html",
            {**ctx, "form": form, "backend_error": backend_error, "destination_error": destination_error},
            status_code=400,
        )
    project_id = project["project_id"]

    if has_file:
        try:
            mrns = await run_in_threadpool(_parse_requested_patients_sync, upload.file)
        except _DocumentTooLargeError:
            flash(session, "error", f"Patient list too large (max {_MAX_REQUESTED_PATIENTS_FILE_BYTES // (1024 * 1024)}MB) -- project created without it.")
        else:
            if mrns:
                try:
                    await backend_client.add_requested_patients(project_id, mrns, added_by=user.username)
                except backend_client.BackendError as e:
                    # The project itself was already created successfully --
                    # a failure here must not 500 the whole request or hide
                    # that the project exists; just say the list didn't land.
                    flash(session, "error", f"Project created, but the patient list could not be saved: {e.detail}")
    else:
        # FEATURES.md item 5: non-blocking -- a nudge, not a requirement.
        flash(session, "warning", "No patient-ID list uploaded. Consider adding one so this project's usage can be tracked.")

    flash(session, "success", "Project created as a draft. Submit it for review when ready.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)


@router.get("/review", name="review_queue")
async def review_queue(request: Request, user: User = Depends(require_data_custodian), ctx: dict = Depends(get_template_context)):
    backend_error = None
    try:
        pending = await backend_client.list_projects(status="submitted")
        amendments = await backend_client.list_pending_amendments()
    except backend_client.BackendError as e:
        backend_error = f"Could not load review queue: {e.detail}"
        pending, amendments = [], []
    return templates.TemplateResponse(request, "research_projects/review_queue.html", {
        **ctx, "projects": pending, "amendments": amendments, "backend_error": backend_error,
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

    try:
        stats = await backend_client.get_project_stats(project_id)
    except backend_client.BackendError:
        stats = {"requested_count": 0, "restored_count": 0, "sent_by_destination": []}

    # Contextual to THIS project specifically (unlike list.html's aggregate
    # banner across every project the viewer belongs to) -- only meaningful
    # for a member of an approved project, not e.g. a staff reviewer who
    # isn't otherwise invested in it.
    days_remaining = None
    if is_member:
        matches = expiring_soon([project])
        if matches:
            days_remaining = matches[0]["days_remaining"]

    # Unlike the banner above, the "Project overview" card's expiry
    # colour-coding is for ANY viewer (e.g. a staff reviewer deciding
    # whether to act on it), not gated on project membership.
    overview_matches = expiring_soon([project])
    overview_days_remaining = overview_matches[0]["days_remaining"] if overview_matches else None

    destinations = project["destinations"]
    active_destinations = [d for d in destinations if d["status"] == "active"]
    proposed_destinations = [d for d in destinations if d["status"] == "proposed"]
    pending_amendment = bool(proposed_destinations) or project["pending_message_id"] is not None

    return templates.TemplateResponse(request, "research_projects/detail.html", {
        **ctx,
        "project": project,
        "is_member": is_member,
        "can_manage_documents": is_member or user.is_staff,
        "documents": documents,
        "jobs": jobs,
        "stats": stats,
        "add_member_form": AddMemberForm(),
        "review_form": ReviewProjectForm(),
        "amendment_decision_form": AmendmentDecisionForm(),
        "days_remaining": days_remaining,
        "overview_days_remaining": overview_days_remaining,
        "active_destinations": active_destinations,
        "proposed_destinations": proposed_destinations,
        "pending_amendment": pending_amendment,
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


@router.get("/{project_id}/amend", name="project_amend")
async def project_amend_form(
    request: Request, project_id: str, user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    project = await _get_project_or_flash(session, project_id)
    if project is None:
        return RedirectResponse(request.url_for("project_list"), status_code=303)
    if not (_is_member(project, user.username) or user.is_staff):
        raise HTTPException(status_code=403, detail="Only a project member may propose an amendment")

    modalities, collections, destination_error = await _fetch_destination_choices(user.username)
    form = AmendProjectForm()
    form.set_destination_choices(modalities, collections)
    # Prefill with the project's CURRENT active selections, not a blank
    # form -- an amendment usually tweaks one thing, not re-picks everything.
    active = [d for d in project["destinations"] if d["status"] == "active"]
    form.use_dicom.data = any(d["destination_type"] == "dicom" for d in active)
    form.dicom_destinations.data = [d["destination_value"] for d in active if d["destination_type"] == "dicom"]
    form.use_proknow.data = any(d["destination_type"] == "proknow" for d in active)
    form.proknow_destinations.data = [d["destination_value"] for d in active if d["destination_type"] == "proknow"]
    form.message_id.data = project["message_id"]

    return templates.TemplateResponse(request, "research_projects/amend.html", {
        **ctx, "project": project, "form": form, "destination_error": destination_error,
    })


@router.post("/{project_id}/amend")
async def project_amend_submit(
    request: Request, project_id: str, user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    project = await _get_project_or_flash(session, project_id)
    if project is None:
        return RedirectResponse(request.url_for("project_list"), status_code=303)
    if not (_is_member(project, user.username) or user.is_staff):
        raise HTTPException(status_code=403, detail="Only a project member may propose an amendment")

    modalities, collections, destination_error = await _fetch_destination_choices(user.username)
    form = AmendProjectForm(formdata=await request.form())
    form.set_destination_choices(modalities, collections)
    if not form.validate():
        return templates.TemplateResponse(request, "research_projects/amend.html", {
            **ctx, "project": project, "form": form, "destination_error": destination_error,
        }, status_code=400)

    try:
        await backend_client.propose_amendment(
            project_id, proposed_by=user.username, destinations=form.destinations(), message_id=form.message_id.data,
        )
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not propose amendment: {e.detail}")
    else:
        flash(session, "success", "Amendment proposed. It stays pending until a data custodian reviews it.")
    return RedirectResponse(request.url_for("project_detail", project_id=project_id), status_code=303)


@router.post("/{project_id}/amendment/decide")
async def project_amendment_decide(
    project_id: str, request: Request, user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
):
    """Mirrors project_review's own single-endpoint decision pattern (one
    decision RadioField, branched on here) rather than a separate route per
    outcome."""
    form = AmendmentDecisionForm(formdata=await request.form())
    if form.validate():
        approve = form.decision.data == "approve"
        try:
            if approve:
                await backend_client.approve_amendment(project_id, reviewed_by=user.username, comment=form.comment.data or "")
            else:
                await backend_client.reject_amendment(project_id, reviewed_by=user.username, comment=form.comment.data or "")
        except backend_client.BackendError as e:
            flash(session, "error", f"Could not record amendment decision: {e.detail}")
        else:
            flash(session, "success", f"Amendment {'approved' if approve else 'rejected'}.")
    else:
        flash(session, "error", "Invalid amendment decision.")
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


def _parse_requested_patients_sync(source) -> list[str]:
    """Blocking, streaming read of an uploaded patient-ID list -- bounded
    the same way _save_document_sync bounds a document upload (a size cap
    enforced mid-stream), but writes nothing to disk: see this module's
    _MAX_REQUESTED_PATIENTS_FILE_BYTES comment for why.

    "Basic CSV/text content check" (per the plan): a header line
    ('patient_id' or 'mrn', case-insensitively, alone on its line) is
    skipped; every other non-blank line is treated as one MRN token,
    stripped of surrounding whitespace/commas, deduplicated, and capped at
    _MAX_REQUESTED_PATIENTS_ROWS. No MRN-shape validation -- anon/real ids
    vary in format across deployments, and this list is only ever counted
    (item 04's "requested" stat), never looked up against."""
    source.seek(0)
    read = 0
    chunks = []
    while chunk := source.read(_COPY_CHUNK_SIZE):
        read += len(chunk)
        if read > _MAX_REQUESTED_PATIENTS_FILE_BYTES:
            raise _DocumentTooLargeError(f"Patient list exceeds the {_MAX_REQUESTED_PATIENTS_FILE_BYTES}-byte limit")
        chunks.append(chunk)
    text = b"".join(chunks).decode("utf-8", errors="ignore")

    mrns: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        token = line.strip().strip(",")
        if not token or token.lower() in ("patient_id", "mrn"):
            continue
        if token in seen:
            continue
        seen.add(token)
        mrns.append(token)
        if len(mrns) >= _MAX_REQUESTED_PATIENTS_ROWS:
            break
    return mrns


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
