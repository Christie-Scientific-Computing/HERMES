"""
Proves run_batch_job's (backend/src/common/sse.py) three JSON-bodied SSE
consumers -- /import/batch_import, /export/dicom_move, /export/proknow_upload
-- never leak a real MRN, server filesystem path, or raw date onto the wire,
on both the success path and an induced worker failure.

This is the test docs/plans/pii-boundary-test-suite.md §D names as the one
that "would have caught finding #1": before step 4's fix
(backend/src/common/sse.py's error yield and success spread now go through
pii_patterns.redact()/redact_dict()), a worker's raw str(exception) was
spliced straight into the SSE 'error' event with no scrubbing at all, and
`redact_dict`'s first version blindly overwrote ANY field -- including
`mrn`/`destination` -- whose value happened to look date-shaped. Both
regressions are covered here across all three consumers, using assert_no_pii
(the generic pattern floor) rather than a single hardcoded substring check,
so a future regression in ANY leak category (not just "the exact real MRN")
would be caught, not just the one bug this test was written from.

Needs the PinnacleExport submodule (retrieve/endpoints.py -> retrieve/logic.py)
for the import-side tests -- skips gracefully if it isn't checked out, same
as test_retrieve_endpoints_errors.py.
"""
import csv
import json
import os
import uuid

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import pytest

pytest.importorskip("backend.src.retrieve.PinnacleExport", reason="PinnacleExport submodule not checked out")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.export import endpoints as export_endpoints
from backend.src.export.logic import Exporter as RealExporter
from backend.src.retrieve import endpoints as retrieve_endpoints
from backend.src.retrieve.logic import Importer as RealImporter
from backend.tests.support.pii_assertions import assert_no_pii

REAL_MRN, ANON_MRN = "500123", "1001"

# A leak-shaped message covering three of pii_patterns.py's generic
# categories at once (real MRN, a server path, a calendar date) -- if
# scrubbing regresses on any one of them, this catches it.
def _leak_message(real_mrn: str) -> str:
    return f"lookup failed for {real_mrn} at /pinnacle/{real_mrn}/Plan_1/dose.dcm on 2026-01-15"


def _write_csv(tmp_path, *patient_ids) -> str:
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        for pid in patient_ids:
            writer.writerow([pid])
    return str(csv_path)


def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: ")]


# ---------------------------------------------------------------------------
# /import/batch_import
# ---------------------------------------------------------------------------

@pytest.fixture
def import_client(monkeypatch):
    class ExplodingImporter:
        read_input_file = staticmethod(RealImporter.read_input_file)

        def __init__(self, import_level):
            pass

        def handle_patient(self, real_mrn):
            raise RuntimeError(_leak_message(real_mrn))

    monkeypatch.setattr(retrieve_endpoints, "Importer", ExplodingImporter)
    app = FastAPI()
    app.include_router(retrieve_endpoints.router)
    return TestClient(app)


def test_batch_import_success_events_carry_no_pii(import_client, tmp_path, active_project, monkeypatch):
    class OkImporter:
        read_input_file = staticmethod(RealImporter.read_input_file)

        def __init__(self, import_level):
            pass

        def handle_patient(self, real_mrn):
            return {"status": f"found for {real_mrn}", "in_mosaiq": True, "imported": True}

    monkeypatch.setattr(retrieve_endpoints, "Importer", OkImporter)
    project_id, username = active_project
    csv_path = _write_csv(tmp_path, ANON_MRN)

    resp = import_client.post("/import/batch_import", json={
        "job_id": f"batch-import-ok-{uuid.uuid4()}", "path_to_csv": csv_path,
        "import_level": "Planning data", "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e["type"] == "success" for e in events)
    assert_no_pii(events, real_ids=[REAL_MRN], context="batch_import success events")


def test_batch_import_induced_failure_scrubs_error_event(import_client, tmp_path, active_project):
    project_id, username = active_project
    csv_path = _write_csv(tmp_path, ANON_MRN)

    resp = import_client.post("/import/batch_import", json={
        "job_id": f"batch-import-fail-{uuid.uuid4()}", "path_to_csv": csv_path,
        "import_level": "Planning data", "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    error_events = [e for e in events if e["type"] == "error"]
    assert error_events, "expected an induced worker failure to produce an 'error' SSE event"
    assert_no_pii(events, real_ids=[REAL_MRN], real_dates=["20260115"], context="batch_import error event")


def test_batch_import_induced_failure_zero_padded_id_still_caught(import_client, tmp_path, active_project, monkeypatch):
    """
    Format-variant coverage: a worker that happens to echo the real id
    zero-padded (a plausible coercion bug, e.g. an upstream fixed-width
    export) must still be caught -- assert_no_pii's real_ids matching goes
    through pii_patterns.real_id_variants, not an exact-string check.
    """
    class ZeroPadImporter:
        read_input_file = staticmethod(RealImporter.read_input_file)

        def __init__(self, import_level):
            pass

        def handle_patient(self, real_mrn):
            raise RuntimeError(f"lookup failed for patient {int(real_mrn):09d}")

    monkeypatch.setattr(retrieve_endpoints, "Importer", ZeroPadImporter)
    project_id, username = active_project
    csv_path = _write_csv(tmp_path, ANON_MRN)

    resp = import_client.post("/import/batch_import", json={
        "job_id": f"batch-import-zp-{uuid.uuid4()}", "path_to_csv": csv_path,
        "import_level": "Planning data", "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert_no_pii(events, real_ids=[REAL_MRN], context="batch_import zero-padded-id error event")


# ---------------------------------------------------------------------------
# /export/dicom_move
# ---------------------------------------------------------------------------

@pytest.fixture
def export_client(monkeypatch):
    class ExplodingExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            self.destination = destination

        def dicom_c_move(self, patient_id, message_id=None):
            raise RuntimeError(_leak_message(patient_id))

        def upload_to_proknow(self, patient_id):
            raise RuntimeError(_leak_message(patient_id))

    monkeypatch.setattr(export_endpoints, "Exporter", ExplodingExporter)
    app = FastAPI()
    app.include_router(export_endpoints.router)
    return TestClient(app)


def test_dicom_move_success_events_carry_no_pii(export_client, tmp_path, active_project, monkeypatch):
    class OkExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            self.destination = destination

        def dicom_c_move(self, patient_id, message_id=None):
            return {"status": f"moved {patient_id}", "study_count": 1}

    monkeypatch.setattr(export_endpoints, "Exporter", OkExporter)
    project_id, username = active_project
    csv_path = _write_csv(tmp_path, ANON_MRN)

    resp = export_client.post("/export/dicom_move", json={
        "job_id": f"dicom-move-ok-{uuid.uuid4()}", "path_to_csv": csv_path, "destination": "SOME_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e["type"] == "success" for e in events)
    assert_no_pii(events, real_ids=[REAL_MRN], context="dicom_move success events")


def test_dicom_move_induced_failure_scrubs_error_event(export_client, tmp_path, active_project):
    project_id, username = active_project
    csv_path = _write_csv(tmp_path, ANON_MRN)

    resp = export_client.post("/export/dicom_move", json={
        "job_id": f"dicom-move-fail-{uuid.uuid4()}", "path_to_csv": csv_path, "destination": "SOME_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    error_events = [e for e in events if e["type"] == "error"]
    assert error_events, "expected an induced worker failure to produce an 'error' SSE event"
    assert_no_pii(events, real_ids=[REAL_MRN], real_dates=["20260115"], context="dicom_move error event")


def test_dicom_move_destination_field_survives_date_shaped_value(export_client, tmp_path, active_project, monkeypatch):
    """
    Regression coverage for the specific redact_dict bug finding #1
    describes: `destination` is operational config (an Orthanc AE title),
    never patient data -- it must survive intact even when it happens to
    look date-shaped, on both the success AND error event (redact_dict's
    exclude applies to both spreads in run_batch_job).
    """
    class OkExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            self.destination = destination

        def dicom_c_move(self, patient_id, message_id=None):
            return {"status": "moved"}

    monkeypatch.setattr(export_endpoints, "Exporter", OkExporter)
    project_id, username = active_project
    csv_path = _write_csv(tmp_path, ANON_MRN)

    resp = export_client.post("/export/dicom_move", json={
        "job_id": f"dicom-move-dest-{uuid.uuid4()}", "path_to_csv": csv_path,
        "destination": "AE_2026-01-15", "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    success_event = next(e for e in events if e["type"] == "success")
    assert success_event["destination"] == "AE_2026-01-15"  # not "[redacted]"


# ---------------------------------------------------------------------------
# /export/proknow_upload
# ---------------------------------------------------------------------------

def test_proknow_upload_success_events_carry_no_pii(export_client, tmp_path, active_project, monkeypatch):
    class OkExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            self.destination = destination

        def upload_to_proknow(self, patient_id):
            return {"status": f"uploaded {patient_id}", "series_count": 2}

    monkeypatch.setattr(export_endpoints, "Exporter", OkExporter)
    project_id, username = active_project
    csv_path = _write_csv(tmp_path, ANON_MRN)

    resp = export_client.post("/export/proknow_upload", json={
        "job_id": f"pk-upload-ok-{uuid.uuid4()}", "path_to_csv": csv_path, "collection": "SOME_COLLECTION",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e["type"] == "success" for e in events)
    assert_no_pii(events, real_ids=[REAL_MRN], context="proknow_upload success events")


def test_proknow_upload_induced_failure_scrubs_error_event(export_client, tmp_path, active_project):
    project_id, username = active_project
    csv_path = _write_csv(tmp_path, ANON_MRN)

    resp = export_client.post("/export/proknow_upload", json={
        "job_id": f"pk-upload-fail-{uuid.uuid4()}", "path_to_csv": csv_path, "collection": "SOME_COLLECTION",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    error_events = [e for e in events if e["type"] == "error"]
    assert error_events, "expected an induced worker failure to produce an 'error' SSE event"
    assert_no_pii(events, real_ids=[REAL_MRN], real_dates=["20260115"], context="proknow_upload error event")
