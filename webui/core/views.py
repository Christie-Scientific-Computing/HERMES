import requests
from django.shortcuts import render

from . import backend_client
from .forms import BatchImportForm, ExportForm, JobLookupForm, PatientLookupForm, SingleImportForm


def _summarize_sse_events(events: list[dict]) -> list[dict]:
    """Drop the start/done bookkeeping events; keep one row per patient with
    everything besides type/mrn folded into a single `detail` dict for display."""
    rows = []
    for e in events:
        if e.get("type") in ("start", "done", "cancelled", "progress"):
            continue
        rows.append({
            "type": e.get("type"),
            "mrn": e.get("mrn", ""),
            "detail": {k: v for k, v in e.items() if k not in ("type", "mrn")},
        })
    return rows


def home(request):
    return render(request, "core/home.html")


def import_view(request):
    single_form = SingleImportForm()
    batch_form = BatchImportForm()
    single_result = None
    batch_results = None
    error = None

    if request.method == "POST" and request.POST.get("form") == "single":
        single_form = SingleImportForm(request.POST)
        if single_form.is_valid():
            try:
                single_result = backend_client.import_single(
                    single_form.cleaned_data["mrn"], single_form.cleaned_data["import_level"],
                )
            except requests.RequestException as exc:
                error = f"Backend request failed: {exc}"

    elif request.method == "POST" and request.POST.get("form") == "batch":
        batch_form = BatchImportForm(request.POST, request.FILES)
        if batch_form.is_valid():
            f = batch_form.cleaned_data["file"]
            try:
                events = backend_client.import_batch_file(
                    f.name, f.read(), batch_form.cleaned_data["import_level"],
                )
                batch_results = _summarize_sse_events(events)
            except requests.RequestException as exc:
                error = f"Backend request failed: {exc}"

    return render(request, "core/import.html", {
        "single_form": single_form,
        "batch_form": batch_form,
        "single_result": single_result,
        "batch_results": batch_results,
        "error": error,
    })


def export_view(request):
    form = ExportForm()
    results = None
    error = None

    if request.method == "POST":
        form = ExportForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["file"]
            file_bytes = f.read()
            try:
                if form.cleaned_data["mode"] == "dicom":
                    events = backend_client.export_dicom_move_file(
                        f.name, file_bytes, form.cleaned_data["destination"],
                    )
                else:
                    events = backend_client.export_proknow_upload_file(
                        f.name, file_bytes, form.cleaned_data["collection"],
                    )
                results = _summarize_sse_events(events)
            except requests.RequestException as exc:
                error = f"Backend request failed: {exc}"

    return render(request, "core/export.html", {
        "form": form,
        "results": results,
        "error": error,
    })


def results_view(request):
    lookup = request.GET.get("lookup")
    job_form = JobLookupForm(request.GET if lookup == "job" else None)
    patient_form = PatientLookupForm(request.GET if lookup == "patient" else None)
    job_data = None
    patient_data = None
    error = None

    if lookup == "job" and job_form.is_valid():
        job_id = job_form.cleaned_data["job_id"]
        try:
            job_data = {
                "job_id": job_id,
                "summary": backend_client.job_summary(job_id)["summary"],
                "patients": backend_client.job_patients(job_id),
            }
        except requests.RequestException as exc:
            error = f"Backend request failed: {exc}"

    if lookup == "patient" and patient_form.is_valid():
        mrn = patient_form.cleaned_data["mrn"]
        job_id = patient_form.cleaned_data.get("job_id")
        try:
            if job_id:
                events = backend_client.patient_timeline(job_id, mrn)
            else:
                events = backend_client.patient_timeline_all(mrn)
            patient_data = {"mrn": mrn, "job_id": job_id, "events": events}
        except requests.RequestException as exc:
            error = f"Backend request failed: {exc}"

    return render(request, "core/results.html", {
        "job_form": job_form,
        "patient_form": patient_form,
        "job_data": job_data,
        "patient_data": patient_data,
        "error": error,
    })
