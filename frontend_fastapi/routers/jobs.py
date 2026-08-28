"""
Routers for import/export/results. Port of jobs/views.py (Django) -- see
that file for the exact behavior being matched
(docs/plans/frontend-rewrite-implementation-plan.md Phase 3a).

Project selection has no implicit/session concept -- every submission form
carries its own `project_id` field, populated fresh on every request (see
_project_choices_for below) from backend_client.list_user_active_projects.
The SelectField's own validation against those freshly-fetched choices IS
the live re-check that a submitted project_id is one the user currently has
active access to -- there's nothing cached/trusted from earlier in the
request or from session.

Every batch job (single/batch import, DICOM export, ProKnow export) is
enqueued directly onto the backend's task queue
(docs/plans/worker-queue-design.md): submit_job calls straight through to
the matching backend_client.*_file function, which returns a job_id the
moment the backend has finished enqueueing every row -- nothing is staged
to local disk or session first. job_watch/job_stream both re-check live
visibility on every request (_check_job_visibility, mirroring job_detail's
_job_is_visible_to) rather than trusting anything cached from submission
time, and job_stream relays the backend's observer stream (GET
/results/job/{job_id}/stream). Closing the tab, or never opening job_stream
at all, has no effect on whether the job actually runs -- a worker process
executes it independently either way.

job_stream re-frames every `data: {...}` line from the backend's SSE stream
with a matching `event: <type>` line (derived from the JSON body's own
"type" field) before relaying it to the browser, so plain
EventSource.addEventListener('progress', ...) etc. works without any
hand-rolled per-message dispatch. Unlike Django's version, this needs no
sync_to_async wrapper -- this whole stack is natively async.
"""
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.datastructures import UploadFile

from frontend_fastapi import backend_client
from frontend_fastapi.deps import get_current_user, get_session, get_template_context, require_login
from frontend_fastapi.flash import flash
from frontend_fastapi.forms.jobs import JobLookupForm, JobSubmissionForm, PatientLookupForm
from frontend_fastapi.models import Session, User
from frontend_fastapi.templating import templates

router = APIRouter(tags=["jobs"])


async def _project_choices_for(user: User) -> list[dict]:
    """Projects to offer on a submission form's project_id field, fetched
    live. Superusers get their auto-provisioned bypass project ensured
    first, so it's always among their choices."""
    try:
        if user.is_superuser:
            await backend_client.ensure_superuser_bypass_project(user.username)
        return await backend_client.list_user_active_projects(user.username)
    except backend_client.BackendError:
        return []


async def _users_projects(user: User) -> list[dict]:
    """Every project (any status) `user` belongs to -- used to scope
    results visibility, which is about viewing your own history, not about
    being allowed to start new jobs (so, deliberately, no status filter)."""
    try:
        return await backend_client.list_projects(username=user.username)
    except backend_client.BackendError:
        return []


@router.get("/", name="dashboard")
async def dashboard(user: User = Depends(require_login), ctx: dict = Depends(get_template_context)):
    projects = await _project_choices_for(user)
    jobs = []
    for p in projects:
        try:
            jobs.extend(await backend_client.list_project_jobs(p["project_id"]))
        except backend_client.BackendError:
            pass
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return templates.TemplateResponse(ctx["request"], "jobs/dashboard.html", {
        **ctx, "projects": projects, "recent_jobs": jobs[:10],
    })


async def _enqueue_batch_job(post_fn, content: bytes, filename: str, project_id: str, username: str, **extra_fields) -> str:
    """
    Mint a job_id and hand it, along with the upload's raw bytes, straight
    to one of backend_client's *_file functions (batch_import_file/
    dicom_move_file/proknow_upload_file/combined_import_export_file) --
    shared by every branch of submit_job below, since all these enqueue
    calls have the same shape and differ only in which backend endpoint and
    which kind-specific field (import_level/destination/collection) they
    carry. Nothing is written to local disk or session: the backend already
    has everything it needs to run this job once this call returns, so
    job_watch/job_stream only need the job_id to watch, not how to submit it.
    """
    job_id = str(uuid.uuid4())
    await post_fn(job_id=job_id, filename=filename, content=content, project_id=project_id, username=username, **extra_fields)
    return job_id


@router.api_route("/submit", methods=["GET", "POST"], name="submit_job")
async def submit_job(
    request: Request, user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    """
    The one job-submission page: single patient or batch (CSV), import
    and/or export (DICOM or ProKnow). JobSubmissionForm.validate() enforces
    which fields are actually required given what was chosen; this view
    only needs to branch on (do_import, do_export) to pick the matching
    backend_client call, since all four already exist unchanged.

    Single-scope submissions re-render this same page inline with job_id
    set and a fresh form (does NOT redirect -- this is deliberate rapid-entry
    UX, ported as-is); batch-scope submissions redirect to job_watch.
    """
    projects = await _project_choices_for(user)

    modalities, modalities_error = [], None
    collections, collections_error = [], None
    if projects:
        try:
            modalities = await backend_client.get_orthanc_modalities(user.username)
        except backend_client.BackendError as e:
            modalities_error = e.detail
        try:
            collections = await backend_client.get_proknow_collections(user.username)
        except backend_client.BackendError as e:
            collections_error = e.detail

    def _fresh_form(formdata=None) -> JobSubmissionForm:
        form = JobSubmissionForm(formdata=formdata)
        form.set_project_choices(projects)
        form.set_destination_choices(modalities)
        form.set_collection_choices(collections)
        return form

    job_id = None
    is_combined = False

    if request.method == "POST":
        formdata = await request.form()
        upload = formdata.get("file")
        has_file = isinstance(upload, UploadFile) and bool(upload.filename)
        form = _fresh_form(formdata)
        form.file_provided = has_file

        if form.validate():
            do_import, do_export = form.do_import.data, form.do_export.data
            is_combined = do_import and do_export

            if form.scope.data == "single":
                content, filename = f"patient_id\n{form.mrn.data}\n".encode(), "single_patient.csv"
            else:
                content, filename = await upload.read(), upload.filename

            try:
                if is_combined:
                    job_id = await _enqueue_batch_job(
                        backend_client.combined_import_export_file, content, filename,
                        form.project_id.data, user.username,
                        import_level=form.import_level.data, export_kind=form.export_kind.data,
                        destination_or_collection=(
                            form.destination.data if form.export_kind.data == "dicom_move" else form.collection.data
                        ),
                        message_id=form.message_id.data if form.export_kind.data == "dicom_move" else None,
                    )
                elif do_import:
                    job_id = await _enqueue_batch_job(
                        backend_client.batch_import_file, content, filename,
                        form.project_id.data, user.username, import_level=form.import_level.data,
                    )
                elif form.export_kind.data == "dicom_move":
                    job_id = await _enqueue_batch_job(
                        backend_client.dicom_move_file, content, filename,
                        form.project_id.data, user.username,
                        destination=form.destination.data, message_id=form.message_id.data,
                    )
                else:
                    job_id = await _enqueue_batch_job(
                        backend_client.proknow_upload_file, content, filename,
                        form.project_id.data, user.username, collection=form.collection.data,
                    )
            except backend_client.BackendError as e:
                flash(session, "error", f"Could not start job: {e.detail}")
                job_id = None

            if job_id:
                if form.scope.data == "batch":
                    return RedirectResponse(request.url_for("job_watch", job_id=job_id), status_code=303)
                form = _fresh_form()  # ready for another entry
    else:
        form = _fresh_form()

    return templates.TemplateResponse(request, "jobs/submit_job.html", {
        **ctx, "form": form, "job_id": job_id, "is_combined": is_combined,
        "has_projects": bool(projects),
        "modalities_error": modalities_error, "collections_error": collections_error,
    })


def _job_is_visible_to(user: User, job_info: dict, user_project_ids: list[str]) -> bool:
    return user.is_staff or job_info.get("project_id") in user_project_ids


async def _check_job_visibility(user: Optional[User], job_id: str) -> tuple[bool, Optional[dict]]:
    """
    Live visibility check backing both job_watch and job_stream: mirrors
    job_detail's own _job_is_visible_to, re-checked on every request rather
    than trusting anything cached from submission time -- the job may have
    been enqueued minutes ago by this browser, or be someone else's job_id
    typed/guessed into the URL. Returns (visible, job_info) -- job_info is
    the one job_summary call this makes, handed back so a caller (job_watch)
    doesn't need a second round trip just to read another field off the
    same response.
    """
    if user is None:
        return False, None
    try:
        job_info = await backend_client.job_summary(job_id)
    except backend_client.BackendError:
        return False, None
    user_project_ids = [p["project_id"] for p in await _users_projects(user)]
    return _job_is_visible_to(user, job_info, user_project_ids), job_info


@router.get("/jobs/{job_id}/watch", name="job_watch")
async def job_watch(job_id: str, user: User = Depends(require_login), ctx: dict = Depends(get_template_context)):
    visible, job_info = await _check_job_visibility(user, job_id)
    if not visible:
        raise HTTPException(status_code=404, detail="Unknown job, or you don't have access to it")
    # is_combined (backend's TasksDB.job_has_chain_export, checked on
    # submission-time params) picks the two-stage progress component for a
    # combined import->export job -- accurate from the moment the job is
    # submitted, not only once its first import has actually succeeded and
    # chained an export task.
    is_combined = (job_info or {}).get("is_combined", False)
    return templates.TemplateResponse(ctx["request"], "jobs/job_watch.html", {
        **ctx, "job_id": job_id, "is_combined": is_combined,
    })


@router.get("/jobs/{job_id}/stream")
async def job_stream(job_id: str, user: Optional[User] = Depends(get_current_user)):
    """
    Relays the backend's observer stream (GET /results/job/{job_id}/stream,
    docs/plans/worker-queue-design.md) to the browser, re-framing every raw
    `data: {...}` line with a matching `event: <type>` line (derived from
    the payload's own "type" field) so the browser's plain
    EventSource.addEventListener('progress', ...) etc. works without any
    hand-rolled per-message dispatch. No require_login dependency here --
    auth is via the visibility check itself, same as Django's version (an
    anonymous request is simply never visible).
    """
    visible, _ = await _check_job_visibility(user, job_id)
    if not visible:
        raise HTTPException(status_code=404, detail="Unknown job, or you don't have access to it")

    async def relay():
        buffer = b""
        async for chunk in backend_client.stream_sse(f"/results/job/{job_id}/stream"):
            buffer += chunk
            while b"\n\n" in buffer:
                raw_event, buffer = buffer.split(b"\n\n", 1)
                line = raw_event.decode(errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: "):]
                try:
                    event_type = json.loads(payload).get("type", "message")
                except Exception:
                    event_type = "message"
                yield f"event: {event_type}\ndata: {payload}\n\n".encode()

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str, request: Request, user: User = Depends(require_login), session: Session = Depends(get_session),
):
    try:
        await backend_client.cancel_import(job_id)  # same jobs.cancelled column regardless of import/export
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not cancel: {e.detail}")
    return RedirectResponse(request.url_for("job_watch", job_id=job_id), status_code=303)


# `in_*` is tri-state: True/False are answers, None means "we never checked"
# (an export-only job, or a patient with no successful retrieve event). Every
# predicate below therefore tests `is False`, never falsiness -- treating None
# as "missing" would invent failures that were never observed.
PATIENT_FILTERS = [
    ("", "All", lambda r: True),
    ("failed", "Failed", lambda r: r.get("outcome") == "failure"),
    ("not_found", "Found nowhere", lambda r: all(
        r.get(k) is False for k in ("in_mosaiq", "in_pinnacle", "in_proknow")
    )),
    ("missing_mosaiq", "No Mosaiq", lambda r: r.get("in_mosaiq") is False),
    ("missing_pinnacle", "No Pinnacle", lambda r: r.get("in_pinnacle") is False),
    ("missing_proknow", "No ProKnow", lambda r: r.get("in_proknow") is False),
]


def _patient_rows(patients: list[str], patient_summary: dict) -> list[dict]:
    """Flatten the two backend calls into one row per patient, so templates
    don't need dict-lookup-by-variable gymnastics."""
    return [{"mrn": mrn, **(patient_summary.get(mrn) or {})} for mrn in patients]


def _filter_patient_rows(rows: list[dict], active: str) -> tuple[list[dict], list[dict]]:
    """
    Returns (visible rows, filter pills).

    Counts come from the *unfiltered* rows so the pills keep showing how much
    is behind each option rather than collapsing to the current selection.
    """
    pills = [
        {"key": key, "label": label, "count": sum(1 for r in rows if pred(r)), "active": key == active}
        for key, label, pred in PATIENT_FILTERS
    ]
    predicate = next((p for k, _, p in PATIENT_FILTERS if k == active), None)
    visible = [r for r in rows if predicate(r)] if predicate else rows
    return visible, pills


@router.get("/jobs/{job_id}", name="job_detail")
async def job_detail(
    job_id: str, request: Request, filter: str = Query("", alias="filter"),
    user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    try:
        job_info = await backend_client.job_summary(job_id)
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not load job: {e.detail}")
        return RedirectResponse(request.url_for("dashboard"), status_code=303)

    user_project_ids = [p["project_id"] for p in await _users_projects(user)]
    if not _job_is_visible_to(user, job_info, user_project_ids):
        flash(session, "error", "You don't have access to that job.")
        return RedirectResponse(request.url_for("dashboard"), status_code=303)

    try:
        patients = (await backend_client.job_patients(job_id))["patients"]
        patient_summary = {p["mrn"]: p for p in (await backend_client.job_patients_summary(job_id))["patients"]}
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not load job: {e.detail}")
        return RedirectResponse(request.url_for("dashboard"), status_code=303)

    rows = _patient_rows(patients, patient_summary)
    visible, pills = _filter_patient_rows(rows, filter)

    return templates.TemplateResponse(request, "jobs/job_detail.html", {
        **ctx, "job_id": job_id, "summary": job_info["summary"],
        "imported_count": job_info.get("imported_count"),
        "submitted_count": job_info.get("submitted_count"),
        "exported_count": job_info.get("exported_count"),
        "export_attempted_count": job_info.get("export_attempted_count"),
        "rows": visible, "pills": pills, "total": len(rows),
    })


def _plan_status_pills(plans: list[dict], active: str) -> list[dict]:
    """
    Filter pills built from the statuses actually present, not a hardcoded
    list -- the real vocabulary lives in PinnacleExport, not here. Counts come
    from the unfiltered plans so they don't collapse as you filter.
    """
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan.get("status") or ""] = counts.get(plan.get("status") or "", 0) + 1
    pills = [{"key": "", "label": "All", "count": len(plans), "active": not active}]
    pills += [
        {"key": status, "label": status or "(none)", "count": count, "active": status == active}
        for status, count in sorted(counts.items())
    ]
    return pills


@router.get("/jobs/{job_id}/patients/{mrn}", name="patient_detail")
async def patient_detail(
    job_id: str, mrn: str, request: Request, status: str = "",
    user: User = Depends(require_login), session: Session = Depends(get_session),
    ctx: dict = Depends(get_template_context),
):
    """
    One patient, reached through a job so the job's own visibility check
    applies. The timeline is job-scoped; the plans are not -- PinnacleExport's
    plans table has no job_id, so it shows everything recorded for this
    patient, whichever job touched them.
    """
    try:
        job_info = await backend_client.job_summary(job_id)
    except backend_client.BackendError as e:
        flash(session, "error", f"Could not load job: {e.detail}")
        return RedirectResponse(request.url_for("dashboard"), status_code=303)

    user_project_ids = [p["project_id"] for p in await _users_projects(user)]
    if not _job_is_visible_to(user, job_info, user_project_ids):
        flash(session, "error", "You don't have access to that job.")
        return RedirectResponse(request.url_for("dashboard"), status_code=303)

    # Deliberately separate try blocks: a plans failure must not blank the
    # timeline, and vice versa -- either half is useful on its own.
    events, events_error = None, None
    try:
        events = (await backend_client.patient_timeline(job_id, mrn))["events"]
    except backend_client.BackendError as e:
        events_error = e.detail

    plans, plans_available, plans_error = [], False, None
    try:
        payload = await backend_client.patient_plans(mrn)
        plans, plans_available = payload["plans"], payload["available"]
    except backend_client.BackendError as e:
        plans_error = e.detail

    status_pills = _plan_status_pills(plans, status)
    if status:
        plans = [p for p in plans if (p.get("status") or "") == status]

    # Source badges for the header. Best-effort: the page is still worth
    # rendering without them.
    summary = {}
    try:
        summary = next(
            (p for p in (await backend_client.job_patients_summary(job_id))["patients"] if p["mrn"] == mrn), {},
        )
    except backend_client.BackendError:
        pass

    return templates.TemplateResponse(request, "jobs/patient_detail.html", {
        **ctx, "job_id": job_id, "mrn": mrn, "summary": summary,
        "events": events, "events_error": events_error,
        "plans": plans, "plans_available": plans_available, "plans_error": plans_error,
        "status_pills": status_pills, "active_status": status,
    })


@router.get("/results", name="results_lookup")
async def results_lookup(
    request: Request, lookup: str = "job", filter: str = Query("", alias="filter"),
    user: User = Depends(require_login), ctx: dict = Depends(get_template_context),
):
    users_projects = await _users_projects(user)
    user_project_ids = [p["project_id"] for p in users_projects]

    project_jobs = []
    for p in users_projects:
        try:
            for j in await backend_client.list_project_jobs(p["project_id"]):
                project_jobs.append({**j, "project_title": p["title"]})
        except backend_client.BackendError:
            pass
    project_jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)

    query_params = request.query_params
    job_form = JobLookupForm(formdata=query_params if lookup == "job" else None)
    patient_form = PatientLookupForm(formdata=query_params if lookup == "patient" else None)
    summary = None
    rows = None
    pills = None
    total = 0
    looked_up_job_id = None
    events = None
    error = None

    if lookup == "job" and job_form.validate():
        job_id = job_form.job_id.data
        try:
            job_info = await backend_client.job_summary(job_id)
            if not _job_is_visible_to(user, job_info, user_project_ids):
                error = "You don't have access to that job."
            else:
                summary = job_info["summary"]
                patients = (await backend_client.job_patients(job_id))["patients"]
                patient_summary = {p["mrn"]: p for p in (await backend_client.job_patients_summary(job_id))["patients"]}
                all_rows = _patient_rows(patients, patient_summary)
                total = len(all_rows)
                rows, pills = _filter_patient_rows(all_rows, filter)
                looked_up_job_id = job_id
        except backend_client.BackendError as e:
            error = e.detail

    if lookup == "patient" and patient_form.validate():
        mrn = patient_form.mrn.data
        patient_job_id = patient_form.job_id.data
        if not patient_job_id and not user.is_staff:
            error = "You must specify a job ID to look up a patient."
        else:
            try:
                if patient_job_id:
                    job_info = await backend_client.job_summary(patient_job_id)
                    if not _job_is_visible_to(user, job_info, user_project_ids):
                        error = "You don't have access to that job."
                    else:
                        events = (await backend_client.patient_timeline(patient_job_id, mrn))["events"]
                else:
                    events = (await backend_client.patient_timeline_all(mrn))["events"]
            except backend_client.BackendError as e:
                error = e.detail

    return templates.TemplateResponse(request, "jobs/results_lookup.html", {
        **ctx, "project_jobs": project_jobs,
        "lookup": lookup, "job_form": job_form, "patient_form": patient_form,
        "summary": summary, "rows": rows, "pills": pills, "total": total,
        "looked_up_job_id": looked_up_job_id, "events": events, "error": error,
    })
