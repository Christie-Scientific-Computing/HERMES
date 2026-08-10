"""
Views for import/export/results.

Project selection has no implicit/session concept -- every submission form
carries its own `project_id` field, populated fresh on every request (see
_project_choices_for below) from backend_client.list_user_active_projects.
Django's own ChoiceField validation against those freshly-fetched choices
IS the live re-check that a submitted project_id is one the user currently
has active access to -- there's nothing cached/trusted from earlier in the
request or from session.

The two-phase pattern for batch (file-upload) jobs deliberately separates
the mutating action from the live-progress view: `import_batch`/
`export_dicom`/`export_proknow` (and, now, `import_single`, staging a
one-row CSV the same way) are normal CSRF-protected POSTs that stage the
uploaded file server-side (under MEDIA_ROOT/tmp_uploads) and mint a fresh,
unguessable job_id, stored only in *this browser's own session*
(`pending_job:<job_id>`). `job_stream` is the GET-only relay a browser's
EventSource connects to -- it can only ever act on a job_id that this same
session already staged via its own POST, so a third party can't trigger a
real import/export by getting a victim to load a crafted GET URL (there's
nothing in the session to act on).

`job_stream` is the one async view in this app: it opens the backend's SSE
endpoint via hermes_frontend.backend_client.stream_sse and re-frames every
`data: {...}` line with a matching `event: <type>` line (derived from the
JSON body's own "type" field) before relaying it to the browser, so plain
EventSource.addEventListener('progress', ...) etc. works without any
hand-rolled per-message dispatch.
"""
import json
import uuid
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
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


def _stage_batch_job(request, kind: str, uploaded_file, extra: dict, project_id: str) -> str:
    job_id = str(uuid.uuid4())
    tmp_dir = Path(settings.MEDIA_ROOT) / "tmp_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{job_id}_{uploaded_file.name}"
    with open(tmp_path, "wb") as f:
        for chunk in uploaded_file.chunks():
            f.write(chunk)
    request.session[f"pending_job:{job_id}"] = {
        "kind": kind, "path": str(tmp_path), "project_id": project_id,
        "username": request.user.username, **extra,
    }
    request.session.modified = True
    return job_id


@login_required
def collect_data(request):
    """Single-patient and batch (CSV) import, combined on one page as two
    tabs -- purely a presentation merge, each mode's staging logic is
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
                job_id = _stage_batch_job(
                    request, kind="import_batch",
                    uploaded_file=ContentFile(csv_bytes, name="single_patient.csv"),
                    extra={"import_level": single_form.cleaned_data["import_level"]},
                    project_id=single_form.cleaned_data["project_id"],
                )
                single_form = SingleImportForm()  # fresh form, ready for another entry
                single_form.set_project_choices(projects)
        elif mode == "batch":
            batch_form = BatchImportForm(request.POST, request.FILES)
            batch_form.set_project_choices(projects)
            if batch_form.is_valid():
                job_id = _stage_batch_job(
                    request, kind="import_batch", uploaded_file=batch_form.cleaned_data["file"],
                    extra={"import_level": batch_form.cleaned_data["import_level"]},
                    project_id=batch_form.cleaned_data["project_id"],
                )
                return redirect("jobs:job_watch", job_id=job_id)

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
                job_id = _stage_batch_job(
                    request, kind="export_dicom", uploaded_file=dicom_form.cleaned_data["file"],
                    extra={"destination": dicom_form.cleaned_data["destination"]},
                    project_id=dicom_form.cleaned_data["project_id"],
                )
                return redirect("jobs:job_watch", job_id=job_id)
        elif mode == "proknow":
            proknow_form = ProKnowExportForm(request.POST, request.FILES)
            proknow_form.set_project_choices(projects)
            proknow_form.set_collection_choices(collections)
            if proknow_form.is_valid():
                job_id = _stage_batch_job(
                    request, kind="export_proknow", uploaded_file=proknow_form.cleaned_data["file"],
                    extra={"collection": proknow_form.cleaned_data["collection"]},
                    project_id=proknow_form.cleaned_data["project_id"],
                )
                return redirect("jobs:job_watch", job_id=job_id)

    return render(request, "jobs/retrieve_data.html", {
        "dicom_form": dicom_form, "proknow_form": proknow_form,
        "has_projects": bool(projects), "active_tab": active_tab,
        "modalities_error": modalities_error, "collections_error": collections_error,
    })


@login_required
def job_watch(request, job_id):
    pending = request.session.get(f"pending_job:{job_id}")
    if not pending:
        raise Http404("Unknown job, or its progress stream has already completed")
    return render(request, "jobs/job_watch.html", {"job_id": job_id, "kind": pending["kind"]})


def _build_stream_request(pending: dict, job_id: str) -> tuple[str, dict, dict]:
    path_obj = Path(pending["path"])
    file_bytes = path_obj.read_bytes()
    files = {"file": (path_obj.name, file_bytes, "text/csv")}
    common = {"job_id": job_id, "project_id": pending["project_id"], "username": pending["username"]}

    if pending["kind"] == "import_batch":
        return "/import/batch_import_file", {**common, "import_level": pending["import_level"]}, files
    if pending["kind"] == "export_dicom":
        return "/export/dicom_move_file", {**common, "destination": pending["destination"]}, files
    if pending["kind"] == "export_proknow":
        return "/export/proknow_upload_file", {**common, "collection": pending["collection"]}, files
    raise ValueError(f"unknown pending job kind {pending['kind']!r}")


def _cleanup_pending_job(request, job_id: str) -> None:
    pending = request.session.pop(f"pending_job:{job_id}", None)
    request.session.modified = True
    if pending:
        Path(pending["path"]).unlink(missing_ok=True)


def _load_pending_job(request, job_id: str):
    """
    Django's lazy `request.user`/`request.session` both trigger a
    synchronous DB read on first access -- disallowed directly inside an
    async view (SynchronousOnlyOperation). Do that first access here, in
    one sync_to_async-wrapped call, so job_stream itself never touches
    either before they're already resolved.
    """
    if not request.user.is_authenticated:
        return None
    return request.session.get(f"pending_job:{job_id}")


async def job_stream(request, job_id):
    pending = await sync_to_async(_load_pending_job)(request, job_id)
    if pending is None:
        raise Http404("Unknown or already-completed job")

    # request.user is already resolved (cached on the lazy object) by the
    # call above, so reading .username here doesn't hit the DB again.
    active = await sync_to_async(backend_client.list_user_active_projects)(request.user.username)
    if not any(p["project_id"] == pending["project_id"] for p in active):
        raise Http404("No longer an active member of this project")

    async def relay():
        try:
            path, data, files = await sync_to_async(_build_stream_request)(pending, job_id)
            buffer = b""
            async for chunk in backend_client.stream_sse(path, data=data, files=files):
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
        except backend_client.BackendError as e:
            error_payload = json.dumps({"type": "error", "error": e.detail})
            yield f"event: error\ndata: {error_payload}\n\n".encode()
            yield f"event: done\ndata: {{\"type\": \"done\"}}\n\n".encode()
        finally:
            await sync_to_async(_cleanup_pending_job)(request, job_id)

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
