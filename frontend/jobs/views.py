"""
Views for import/export/results.

Project selection has no implicit/session concept -- every submission form
carries its own `project_id` field, populated fresh on every request (see
_project_choices_for below) from backend_client.list_user_active_projects.
Django's own ChoiceField validation against those freshly-fetched choices
IS the live re-check that a submitted project_id is one the user currently
has active access to -- there's nothing cached/trusted from earlier in the
request or from session.

Every batch job (single/batch import, DICOM export, ProKnow export) is
enqueued directly onto the backend's task queue (docs/worker-queue-design.md):
the submitting POST view (collect_data/retrieve_data) calls straight through
to the matching backend_client.*_file function, which returns a job_id the
moment the backend has finished enqueueing every row -- nothing is staged to
local disk or session first. job_watch/job_stream both re-check live
visibility on every request (_user_can_watch_job, mirroring job_detail's
_job_is_visible_to) rather than trusting anything cached from submission
time, and job_stream relays the backend's observer stream (GET
/results/job/{job_id}/stream). Closing the tab, or never opening job_stream
at all, has no effect on whether the job actually runs -- a worker process
executes it independently either way.

(This project used to stage batch uploads under MEDIA_ROOT/tmp_uploads and a
`pending_job:<job_id>` session entry, POSTing the file only once job_stream's
EventSource connected -- a two-phase design that made a GET request safe by
construction, since there was nothing to act on until this same browser's own
earlier POST had staged it. The queue makes that unnecessary: the job is
already fully submitted and running independently by the time job_watch/
job_stream are ever reached, so there's nothing left for a GET to trigger --
the live visibility re-check on every request is what does that job now.)

`job_stream` is the one async view in this app: it opens the backend's SSE
endpoint via hermes_frontend.backend_client.stream_sse and re-frames every
`data: {...}` line with a matching `event: <type>` line (derived from the
JSON body's own "type" field) before relaying it to the browser, so plain
EventSource.addEventListener('progress', ...) etc. works without any
hand-rolled per-message dispatch.
"""
import json
import uuid

from asgiref.sync import sync_to_async
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import redirect, render

from hermes_frontend import backend_client
from jobs.forms import (
    BatchImportForm, DicomExportForm, JobLookupForm, PatientLookupForm,
    ProKnowExportForm, SingleImportForm,
)


def _project_choices_for(user) -> list[dict]:
    """Projects to offer on a submission form's project_id field, fetched
    live. Superusers get their auto-provisioned bypass project ensured
    first, so it's always among their choices."""
    try:
        if user.is_superuser:
            backend_client.ensure_superuser_bypass_project(user.username)
        return backend_client.list_user_active_projects(user.username)
    except backend_client.BackendError:
        return []


def _users_projects(user) -> list[dict]:
    """Every project (any status) `user` belongs to -- used to scope
    results visibility, which is about viewing your own history, not about
    being allowed to start new jobs (so, deliberately, no status filter)."""
    try:
        return backend_client.list_projects(username=user.username)
    except backend_client.BackendError:
        return []


@login_required
def dashboard(request):
    projects = _project_choices_for(request.user)
    jobs = []
    for p in projects:
        try:
            jobs.extend(backend_client.list_project_jobs(p["project_id"]))
        except backend_client.BackendError:
            pass
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return render(request, "jobs/dashboard.html", {"projects": projects, "recent_jobs": jobs[:10]})


def _enqueue_batch_job(post_fn, content: bytes, filename: str, project_id: str, username: str, **extra_fields) -> str:
    """
    Mint a job_id and hand it, along with the upload's raw bytes, straight
    to one of backend_client's *_file functions (batch_import_file/
    dicom_move_file/proknow_upload_file) -- shared by collect_data and
    retrieve_data below, since all three enqueue calls have the same shape
    and differ only in which backend endpoint and which kind-specific field
    (import_level/destination/collection) they carry. Nothing is written to
    local disk or session: the backend already has everything it needs to
    run this job once this call returns, so job_watch/job_stream only need
    the job_id to watch, not how to submit it.
    """
    job_id = str(uuid.uuid4())
    post_fn(job_id=job_id, filename=filename, content=content, project_id=project_id, username=username, **extra_fields)
    return job_id


@login_required
def collect_data(request):
    """Single-patient and batch (CSV) import, combined on one page as two
    tabs -- purely a presentation merge, each mode's submission logic is
    unchanged from when they were separate pages."""
    projects = _project_choices_for(request.user)
    single_form = SingleImportForm()
    batch_form = BatchImportForm()
    single_form.set_project_choices(projects)
    batch_form.set_project_choices(projects)
    job_id = None
    active_tab = request.POST.get("mode", "single")

    if request.method == "POST":
        mode = request.POST.get("mode")
        if mode == "single":
            single_form = SingleImportForm(request.POST)
            single_form.set_project_choices(projects)
            if single_form.is_valid():
                csv_bytes = f"patient_id\n{single_form.cleaned_data['mrn']}\n".encode()
                try:
                    job_id = _enqueue_batch_job(
                        backend_client.batch_import_file, csv_bytes, "single_patient.csv",
                        single_form.cleaned_data["project_id"], request.user.username,
                        import_level=single_form.cleaned_data["import_level"],
                    )
                    single_form = SingleImportForm()  # fresh form, ready for another entry
                    single_form.set_project_choices(projects)
                except backend_client.BackendError as e:
                    messages.error(request, f"Could not start import: {e.detail}")
        elif mode == "batch":
            batch_form = BatchImportForm(request.POST, request.FILES)
            batch_form.set_project_choices(projects)
            if batch_form.is_valid():
                uploaded = batch_form.cleaned_data["file"]
                try:
                    job_id = _enqueue_batch_job(
                        backend_client.batch_import_file, uploaded.read(), uploaded.name,
                        batch_form.cleaned_data["project_id"], request.user.username,
                        import_level=batch_form.cleaned_data["import_level"],
                    )
                    return redirect("jobs:job_watch", job_id=job_id)
                except backend_client.BackendError as e:
                    messages.error(request, f"Could not start import: {e.detail}")

    return render(request, "jobs/collect_data.html", {
        "single_form": single_form, "batch_form": batch_form, "job_id": job_id,
        "has_projects": bool(projects), "active_tab": active_tab,
    })


@login_required
def retrieve_data(request):
    """DICOM (C-MOVE) and ProKnow export, combined on one page as two tabs --
    same presentation-merge approach as collect_data. Destination/collection
    are real dropdowns, populated live from the backend (empty + an inline
    warning if Orthanc/ProKnow can't be reached, rather than blocking the
    whole page)."""
    projects = _project_choices_for(request.user)

    modalities, modalities_error = [], None
    collections, collections_error = [], None
    if projects:
        try:
            modalities = backend_client.get_orthanc_modalities(request.user.username)
        except backend_client.BackendError as e:
            modalities_error = e.detail
        try:
            collections = backend_client.get_proknow_collections(request.user.username)
        except backend_client.BackendError as e:
            collections_error = e.detail

    dicom_form = DicomExportForm()
    proknow_form = ProKnowExportForm()
    dicom_form.set_project_choices(projects)
    dicom_form.set_destination_choices(modalities)
    proknow_form.set_project_choices(projects)
    proknow_form.set_collection_choices(collections)
    active_tab = request.POST.get("mode", "dicom")

    if request.method == "POST":
        mode = request.POST.get("mode")
        if mode == "dicom":
            dicom_form = DicomExportForm(request.POST, request.FILES)
            dicom_form.set_project_choices(projects)
            dicom_form.set_destination_choices(modalities)
            if dicom_form.is_valid():
                uploaded = dicom_form.cleaned_data["file"]
                try:
                    job_id = _enqueue_batch_job(
                        backend_client.dicom_move_file, uploaded.read(), uploaded.name,
                        dicom_form.cleaned_data["project_id"], request.user.username,
                        destination=dicom_form.cleaned_data["destination"],
                    )
                    return redirect("jobs:job_watch", job_id=job_id)
                except backend_client.BackendError as e:
                    messages.error(request, f"Could not start export: {e.detail}")
        elif mode == "proknow":
            proknow_form = ProKnowExportForm(request.POST, request.FILES)
            proknow_form.set_project_choices(projects)
            proknow_form.set_collection_choices(collections)
            if proknow_form.is_valid():
                uploaded = proknow_form.cleaned_data["file"]
                try:
                    job_id = _enqueue_batch_job(
                        backend_client.proknow_upload_file, uploaded.read(), uploaded.name,
                        proknow_form.cleaned_data["project_id"], request.user.username,
                        collection=proknow_form.cleaned_data["collection"],
                    )
                    return redirect("jobs:job_watch", job_id=job_id)
                except backend_client.BackendError as e:
                    messages.error(request, f"Could not start export: {e.detail}")

    return render(request, "jobs/retrieve_data.html", {
        "dicom_form": dicom_form, "proknow_form": proknow_form,
        "has_projects": bool(projects), "active_tab": active_tab,
        "modalities_error": modalities_error, "collections_error": collections_error,
    })


def _user_can_watch_job(request, job_id: str) -> bool:
    """
    Live visibility check backing both job_watch and job_stream: mirrors
    job_detail's own _job_is_visible_to, re-checked on every request rather
    than trusting anything cached from submission time -- the job may have
    been enqueued minutes ago by this browser, or be someone else's job_id
    typed/guessed into the URL.

    Plain sync function, deliberately -- job_stream (async) calls this via
    sync_to_async: request.user/request.session both trigger a synchronous
    DB read on first touch, which is disallowed directly inside an async
    def view. job_watch (a normal sync view) calls it directly.
    """
    if not request.user.is_authenticated:
        return False
    try:
        job_info = backend_client.job_summary(job_id)
    except backend_client.BackendError:
        return False
    user_project_ids = [p["project_id"] for p in _users_projects(request.user)]
    return _job_is_visible_to(request, job_info, user_project_ids)


@login_required
def job_watch(request, job_id):
    if not _user_can_watch_job(request, job_id):
        raise Http404("Unknown job, or you don't have access to it")
    return render(request, "jobs/job_watch.html", {"job_id": job_id})


async def job_stream(request, job_id):
    """
    Relays the backend's observer stream (GET /results/job/{job_id}/stream,
    docs/worker-queue-design.md) to the browser, re-framing every raw
    `data: {...}` line with a matching `event: <type>` line (derived from
    the payload's own "type" field) so the browser's plain
    EventSource.addEventListener('progress', ...) etc. works without any
    hand-rolled per-message dispatch.
    """
    if not await sync_to_async(_user_can_watch_job)(request, job_id):
        raise Http404("Unknown job, or you don't have access to it")

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

    return StreamingHttpResponse(relay(), content_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@login_required
def cancel_job(request, job_id):
    if request.method == "POST":
        try:
            backend_client.cancel_import(job_id)  # same jobs.cancelled column regardless of import/export
        except backend_client.BackendError as e:
            messages.error(request, f"Could not cancel: {e.detail}")
    return redirect("jobs:job_watch", job_id=job_id)


def _job_is_visible_to(request, job_info: dict, user_project_ids: list[str]) -> bool:
    return request.user.is_staff or job_info.get("project_id") in user_project_ids


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


@login_required
def job_detail(request, job_id):
    try:
        job_info = backend_client.job_summary(job_id)
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load job: {e.detail}")
        return redirect("jobs:dashboard")

    user_project_ids = [p["project_id"] for p in _users_projects(request.user)]
    if not _job_is_visible_to(request, job_info, user_project_ids):
        messages.error(request, "You don't have access to that job.")
        return redirect("jobs:dashboard")

    try:
        patients = backend_client.job_patients(job_id)["patients"]
        patient_summary = {p["mrn"]: p for p in backend_client.job_patients_summary(job_id)["patients"]}
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load job: {e.detail}")
        return redirect("jobs:dashboard")

    rows = _patient_rows(patients, patient_summary)
    visible, pills = _filter_patient_rows(rows, request.GET.get("filter", ""))

    return render(request, "jobs/job_detail.html", {
        "job_id": job_id, "summary": job_info["summary"],
        "imported_count": job_info.get("imported_count"),
        "submitted_count": job_info.get("submitted_count"),
        "rows": visible, "pills": pills, "total": len(rows),
    })


@login_required
def patient_detail(request, job_id, mrn):
    """
    One patient, reached through a job so the job's own visibility check
    applies. The timeline is job-scoped; the plans are not -- PinnacleExport's
    plans table has no job_id, so it shows everything recorded for this
    patient, whichever job touched them.
    """
    try:
        job_info = backend_client.job_summary(job_id)
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load job: {e.detail}")
        return redirect("jobs:dashboard")

    user_project_ids = [p["project_id"] for p in _users_projects(request.user)]
    if not _job_is_visible_to(request, job_info, user_project_ids):
        messages.error(request, "You don't have access to that job.")
        return redirect("jobs:dashboard")

    # Deliberately separate try blocks: a plans failure must not blank the
    # timeline, and vice versa -- either half is useful on its own.
    events, events_error = None, None
    try:
        events = backend_client.patient_timeline(job_id, mrn)["events"]
    except backend_client.BackendError as e:
        events_error = e.detail

    plans, plans_available, plans_error = [], False, None
    try:
        payload = backend_client.patient_plans(mrn)
        plans, plans_available = payload["plans"], payload["available"]
    except backend_client.BackendError as e:
        plans_error = e.detail

    active_status = request.GET.get("status", "")
    status_pills = _plan_status_pills(plans, active_status)
    if active_status:
        plans = [p for p in plans if (p.get("status") or "") == active_status]

    # Source badges for the header. Best-effort: the page is still worth
    # rendering without them.
    summary = {}
    try:
        summary = next(
            (p for p in backend_client.job_patients_summary(job_id)["patients"] if p["mrn"] == mrn),
            {},
        )
    except backend_client.BackendError:
        pass

    return render(request, "jobs/patient_detail.html", {
        "job_id": job_id, "mrn": mrn, "summary": summary,
        "events": events, "events_error": events_error,
        "plans": plans, "plans_available": plans_available, "plans_error": plans_error,
        "status_pills": status_pills, "active_status": active_status,
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


@login_required
def results_lookup(request):
    users_projects = _users_projects(request.user)
    user_project_ids = [p["project_id"] for p in users_projects]

    project_jobs = []
    for p in users_projects:
        try:
            for j in backend_client.list_project_jobs(p["project_id"]):
                project_jobs.append({**j, "project_title": p["title"]})
        except backend_client.BackendError:
            pass
    project_jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)

    lookup = request.GET.get("lookup", "job")
    job_form = JobLookupForm(request.GET if lookup == "job" else None)
    patient_form = PatientLookupForm(request.GET if lookup == "patient" else None)
    summary = None
    rows = None
    pills = None
    total = 0
    looked_up_job_id = None
    events = None
    error = None

    if lookup == "job" and job_form.is_valid():
        job_id = job_form.cleaned_data["job_id"]
        try:
            job_info = backend_client.job_summary(job_id)
            if not _job_is_visible_to(request, job_info, user_project_ids):
                error = "You don't have access to that job."
            else:
                summary = job_info["summary"]
                patients = backend_client.job_patients(job_id)["patients"]
                patient_summary = {p["mrn"]: p for p in backend_client.job_patients_summary(job_id)["patients"]}
                all_rows = _patient_rows(patients, patient_summary)
                total = len(all_rows)
                rows, pills = _filter_patient_rows(all_rows, request.GET.get("filter", ""))
                looked_up_job_id = job_id
        except backend_client.BackendError as e:
            error = e.detail

    if lookup == "patient" and patient_form.is_valid():
        mrn = patient_form.cleaned_data["mrn"]
        job_id = patient_form.cleaned_data["job_id"]
        if not job_id and not request.user.is_staff:
            error = "You must specify a job ID to look up a patient."
        else:
            try:
                if job_id:
                    job_info = backend_client.job_summary(job_id)
                    if not _job_is_visible_to(request, job_info, user_project_ids):
                        error = "You don't have access to that job."
                    else:
                        events = backend_client.patient_timeline(job_id, mrn)["events"]
                else:
                    events = backend_client.patient_timeline_all(mrn)["events"]
            except backend_client.BackendError as e:
                error = e.detail

    return render(request, "jobs/results_lookup.html", {
        "project_jobs": project_jobs,
        "lookup": lookup, "job_form": job_form, "patient_form": patient_form,
        "summary": summary, "rows": rows, "pills": pills, "total": total,
        "looked_up_job_id": looked_up_job_id,
        "events": events, "error": error,
    })
