"""
Thin HTTP client for the HERMES backend. Talks directly to BACKEND_URL
(same convention as the existing Streamlit pages) -- no anon-awareness
here since this is a local testing tool, not the DMZ-facing deployment;
when ANON_DB_HOST is unset on the backend, everything passes through
unchanged anyway.
"""
import json
import uuid

import requests
from django.conf import settings


def new_job_id() -> str:
    return str(uuid.uuid4())


def _parse_sse_line(line: str) -> dict:
    return json.loads(line[len("data: "):])


def _consume_sse(response: requests.Response) -> list[dict]:
    """Read a whole SSE response to completion and return every event."""
    events = []
    for line in response.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            events.append(_parse_sse_line(line))
    return events


def _url(path: str) -> str:
    return f"{settings.BACKEND_URL}{path}"


# ── Import ─────────────────────────────────────────────────────────────────

def import_single(mrn: str, import_level: str) -> dict:
    resp = requests.post(_url("/import/single_import"), json={
        "job_id": new_job_id(), "mrn": mrn, "import_level": import_level,
    }, timeout=(10, None))
    resp.raise_for_status()
    return _parse_sse_line(resp.text)


def import_batch_file(file_name: str, file_bytes: bytes, import_level: str) -> list[dict]:
    job_id = new_job_id()
    resp = requests.post(
        _url("/import/batch_import_file"),
        files={"file": (file_name, file_bytes, "text/csv")},
        data={"job_id": job_id, "import_level": import_level},
        stream=True, timeout=(10, None),
    )
    resp.raise_for_status()
    return _consume_sse(resp)


# ── Export ─────────────────────────────────────────────────────────────────

def get_orthanc_modalities() -> list[str]:
    resp = requests.get(_url("/export/get_orthanc_modalities"), timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_proknow_collections() -> list[str]:
    resp = requests.get(_url("/export/get_proknow_collections"), timeout=10)
    resp.raise_for_status()
    return resp.json()


def export_dicom_move_file(file_name: str, file_bytes: bytes, destination: str) -> list[dict]:
    job_id = new_job_id()
    resp = requests.post(
        _url("/export/dicom_move_file"),
        files={"file": (file_name, file_bytes, "text/csv")},
        data={"job_id": job_id, "destination": destination},
        stream=True, timeout=(10, None),
    )
    resp.raise_for_status()
    return _consume_sse(resp)


def export_proknow_upload_file(file_name: str, file_bytes: bytes, collection: str) -> list[dict]:
    job_id = new_job_id()
    resp = requests.post(
        _url("/export/proknow_upload_file"),
        files={"file": (file_name, file_bytes, "text/csv")},
        data={"job_id": job_id, "collection": collection},
        stream=True, timeout=(10, None),
    )
    resp.raise_for_status()
    return _consume_sse(resp)


# ── Results ────────────────────────────────────────────────────────────────

def job_summary(job_id: str) -> dict:
    resp = requests.get(_url(f"/results/job/{job_id}"), timeout=10)
    resp.raise_for_status()
    return resp.json()


def job_patients(job_id: str) -> list[str]:
    resp = requests.get(_url(f"/results/job/{job_id}/patients"), timeout=10)
    resp.raise_for_status()
    return resp.json()["patients"]


def patient_timeline(job_id: str, mrn: str) -> list[dict]:
    resp = requests.get(_url(f"/results/patient/{job_id}/{mrn}"), timeout=10)
    resp.raise_for_status()
    return resp.json()["events"]


def patient_timeline_all(mrn: str) -> list[dict]:
    resp = requests.get(_url(f"/results/patient/timeline/{mrn}/all"), timeout=10)
    resp.raise_for_status()
    return resp.json()["events"]
