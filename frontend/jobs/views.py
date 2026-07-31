"""
Views for import/export/results.

The two-phase pattern for batch (file-upload) jobs deliberately separates
the mutating action from the live-progress view, per the architecture
review: `import_batch`/`export_dicom`/`export_proknow` are normal
CSRF-protected POSTs that stage the uploaded file server-side (under
MEDIA_ROOT/tmp_uploads) and mint a fresh, unguessable job_id, stored only
in *this browser's own session* (`pending_job:<job_id>`). `job_stream` is
the GET-only relay a browser's EventSource connects to -- it can only ever
act on a job_id that this same session already staged via its own POST, so
a third party can't trigger a real import/export by getting a victim to
load a crafted GET URL (there's nothing in the session to act on).

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
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import redirect, render

from hermes_frontend import backend_client
from jobs.forms import (
    BatchImportForm, DicomExportForm, JobLookupForm, PatientLookupForm,
    ProKnowExportForm, SingleImportForm,
)


def _require_active_project(request):
    """
    Re-validate the session's current project against live backend state.
    Never trust the session alone -- a project can be revoked or expire
    between page loads. Returns the project_id, or None if the user has no
    active approved project (or none selected).
    """
    project_id = request.session.get("current_project_id")
    if not project_id:
        return None
    try:
        active = backend_client.list_user_active_projects(request.user.username)
    except backend_client.BackendError:
        return None
    if not any(p["project_id"] == project_id for p in active):
        return None
    return project_id


@login_required
def dashboard(request):
    project_id = _require_active_project(request)
    jobs = []
    if project_id:
        try:
            jobs = backend_client.list_project_jobs(project_id)[:10]
        except backend_client.BackendError:
            jobs = []
    return render(request, "jobs/dashboard.html", {"project_id": project_id, "recent_jobs": jobs})


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
def import_single(request):
    project_id = _require_active_project(request)
    result = None
    if request.method == "POST":
        if not project_id:
            messages.error(request, "You need an active approved project to import data.")
            return redirect("research_projects:list")
        form = SingleImportForm(request.POST)
        if form.is_valid():
            job_id = str(uuid.uuid4())
            try:
                result = backend_client.single_import(
                    job_id, form.cleaned_data["mrn"], form.cleaned_data["import_level"],
                    project_id, request.user.username,
                )
            except backend_client.BackendError as e:
                messages.error(request, f"Import failed: {e.detail}")
            else:
                if result.get("type") == "error":
                    messages.error(request, f"Import failed: {result.get('error')}")
                else:
                    messages.success(request, f"Imported {result.get('mrn')}.")
    else:
        form = SingleImportForm()
    return render(request, "jobs/import_single.html", {"form": form, "project_id": project_id, "result": result})


@login_required
def import_batch(request):
    project_id = _require_active_project(request)
    if request.method == "POST":
        if not project_id:
            messages.error(request, "You need an active approved project to import data.")
            return redirect("research_projects:list")
        form = BatchImportForm(request.POST, request.FILES)
        if form.is_valid():
            job_id = _stage_batch_job(
                request, kind="import_batch", uploaded_file=form.cleaned_data["file"],
                extra={"import_level": form.cleaned_data["import_level"]}, project_id=project_id,
            )
            return redirect("jobs:job_watch", job_id=job_id)
    else:
        form = BatchImportForm()
    return render(request, "jobs/import_batch.html", {"form": form, "project_id": project_id})


@login_required
def export_dicom(request):
    project_id = _require_active_project(request)
    if request.method == "POST":
        if not project_id:
            messages.error(request, "You need an active approved project to export data.")
            return redirect("research_projects:list")
        form = DicomExportForm(request.POST, request.FILES)
        if form.is_valid():
            job_id = _stage_batch_job(
                request, kind="export_dicom", uploaded_file=form.cleaned_data["file"],
                extra={"destination": form.cleaned_data["destination"]}, project_id=project_id,
            )
            return redirect("jobs:job_watch", job_id=job_id)
    else:
        form = DicomExportForm()
    return render(request, "jobs/export_dicom.html", {"form": form, "project_id": project_id})


@login_required
def export_proknow(request):
    project_id = _require_active_project(request)
    if request.method == "POST":
        if not project_id:
            messages.error(request, "You need an active approved project to export data.")
            return redirect("research_projects:list")
        form = ProKnowExportForm(request.POST, request.FILES)
        if form.is_valid():
            job_id = _stage_batch_job(
                request, kind="export_proknow", uploaded_file=form.cleaned_data["file"],
                extra={"collection": form.cleaned_data["collection"]}, project_id=project_id,
            )
            return redirect("jobs:job_watch", job_id=job_id)
    else:
        form = ProKnowExportForm()
    return render(request, "jobs/export_proknow.html", {"form": form, "project_id": project_id})


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


@login_required
def job_detail(request, job_id):
    try:
        summary = backend_client.job_summary(job_id)
        patients = backend_client.job_patients(job_id)
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load job: {e.detail}")
        return redirect("jobs:dashboard")
    return render(request, "jobs/job_detail.html", {
        "job_id": job_id, "summary": summary["summary"], "patients": patients["patients"],
    })


@login_required
def results_lookup(request):
    lookup = request.GET.get("lookup", "job")
    job_form = JobLookupForm(request.GET if lookup == "job" else None)
    patient_form = PatientLookupForm(request.GET if lookup == "patient" else None)
    summary = None
    patients = None
    events = None
    error = None

    if lookup == "job" and job_form.is_valid():
        job_id = job_form.cleaned_data["job_id"]
        try:
            summary = backend_client.job_summary(job_id)["summary"]
            patients = backend_client.job_patients(job_id)["patients"]
        except backend_client.BackendError as e:
            error = e.detail

    if lookup == "patient" and patient_form.is_valid():
        mrn = patient_form.cleaned_data["mrn"]
        job_id = patient_form.cleaned_data["job_id"]
        try:
            if job_id:
                events = backend_client.patient_timeline(job_id, mrn)["events"]
            else:
                events = backend_client.patient_timeline_all(mrn)["events"]
        except backend_client.BackendError as e:
            error = e.detail

    return render(request, "jobs/results_lookup.html", {
        "lookup": lookup, "job_form": job_form, "patient_form": patient_form,
        "summary": summary, "patients": patients, "events": events, "error": error,
    })
