"""
End-to-end test of the export SSE batch flow: CSV upload (anon ids) ->
anon resolution -> BatchItem -> run_batch_job -> worker (Exporter mocked,
no real Orthanc needed) -> SSE events. Confirms the anon id (not the real
id) appears in every outbound event, while StatusDB records the real id.
"""
import os
import csv
import json
import uuid

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.export import endpoints as export_endpoints
from backend.src.export.logic import Exporter as RealExporter
from backend.src.identity import anon
from backend.tests.support.pii_assertions import assert_no_pii

REAL_MRN_1, ANON_MRN_1 = "500123", "1001"
REAL_MRN_2, ANON_MRN_2 = "500456", "1002"


@pytest.fixture
def client(monkeypatch):
    class FakeExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            self.destination = destination

        def dicom_c_move(self, patient_id, message_id=None):
            assert patient_id in (REAL_MRN_1, REAL_MRN_2), f"worker got non-real id: {patient_id}"
            return {"status": "success"}

    monkeypatch.setattr(export_endpoints, "Exporter", FakeExporter)

    app = FastAPI()
    app.include_router(export_endpoints.router)
    return TestClient(app)


def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: ")]


def test_dicom_move_events_use_anon_id_not_real(client, tmp_path, active_project):
    project_id, username = active_project
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        writer.writerow([ANON_MRN_1])
        writer.writerow([ANON_MRN_2])

    job_id = f"export-anon-{uuid.uuid4()}"
    resp = client.post("/export/dicom_move", json={
        "job_id": job_id, "path_to_csv": str(csv_path), "destination": "SOME_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    assert [e["type"] for e in events] == ["start", "progress", "success", "progress", "success", "done"]
    success_mrns = [e["mrn"] for e in events if e["type"] == "success"]
    assert set(success_mrns) == {ANON_MRN_1, ANON_MRN_2}
    assert_no_pii(events, real_ids=[REAL_MRN_1, REAL_MRN_2], context="dicom_move success events")

    # StatusDB, backend-internal, is allowed to (and does) contain the real ids
    history = export_endpoints.status_db.get_patient_history(job_id, REAL_MRN_1)
    assert [e["event_type"] for e in history] == ["start", "success"]


def test_dicom_move_worker_echoing_float_cast_id_still_caught(client, tmp_path, active_project, monkeypatch):
    """
    Format-variant coverage: a worker whose result dict echoes the real id
    float-cast (e.g. round-tripped through a pandas/polars numeric column
    upstream, the specific coercion bug pii_patterns.real_id_variants was
    written to catch) must still be caught by assert_no_pii, not just an
    exact-string match.
    """
    class FloatCastExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            self.destination = destination

        def dicom_c_move(self, patient_id, message_id=None):
            return {"status": f"moved {float(patient_id)}"}

    monkeypatch.setattr(export_endpoints, "Exporter", FloatCastExporter)
    project_id, username = active_project
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        writer.writerow([ANON_MRN_1])

    resp = client.post("/export/dicom_move", json={
        "job_id": f"export-floatcast-{uuid.uuid4()}", "path_to_csv": str(csv_path), "destination": "SOME_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200
    assert_no_pii(_parse_sse(resp.text), real_ids=[REAL_MRN_1], context="dicom_move float-cast-id success event")


def test_dicom_move_unknown_anon_id_in_csv_returns_422(client, tmp_path, active_project):
    project_id, username = active_project
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        writer.writerow(["999999999"])

    resp = client.post("/export/dicom_move", json={
        "job_id": f"export-anon-{uuid.uuid4()}", "path_to_csv": str(csv_path), "destination": "SOME_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 422


def test_dicom_move_missing_csv_returns_400_not_500(client, active_project):
    project_id, username = active_project
    resp = client.post("/export/dicom_move", json={
        "job_id": f"export-anon-{uuid.uuid4()}",
        "path_to_csv": "/nonexistent/path/patients.csv",
        "destination": "SOME_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 400
    assert resp.json()["detail"]


def test_dicom_move_anon_db_unreachable_returns_503(client, tmp_path, monkeypatch, active_project):
    project_id, username = active_project
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        writer.writerow([ANON_MRN_1])

    monkeypatch.setattr(anon, "ANON_DB_PORT", 1)
    monkeypatch.setattr(anon, "_pool", None)
    try:
        resp = client.post("/export/dicom_move", json={
            "job_id": f"export-anon-{uuid.uuid4()}", "path_to_csv": str(csv_path), "destination": "SOME_AE",
            "project_id": project_id, "username": username,
        })
        assert resp.status_code == 503
        assert resp.json()["detail"]
    finally:
        monkeypatch.setattr(anon, "_pool", None)


def test_get_orthanc_modalities_failure_returns_502_not_bare_500(client, monkeypatch, active_project):
    _, username = active_project

    def boom(*args, **kwargs):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(export_endpoints, "Orthanc", boom)
    resp = client.get("/export/get_orthanc_modalities", params={"username": username})
    assert resp.status_code == 502
    assert resp.json()["detail"]


def test_get_proknow_collections_failure_returns_502_not_bare_500(client, monkeypatch, active_project):
    _, username = active_project

    def boom(*args, **kwargs):
        raise FileNotFoundError("credentials.json")

    monkeypatch.setattr(export_endpoints, "ProKnow", boom)
    resp = client.get("/export/get_proknow_collections", params={"username": username})
    assert resp.status_code == 502
    assert resp.json()["detail"]


def test_cancel_mid_batch_stops_remaining_items(client, tmp_path, monkeypatch, active_project):
    project_id, username = active_project
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        writer.writerow([ANON_MRN_1])
        writer.writerow([ANON_MRN_2])

    job_id = f"export-cancel-{uuid.uuid4()}"
    call_count = 0

    class CancellingExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            pass

        def dicom_c_move(self, patient_id, message_id=None):
            nonlocal call_count
            call_count += 1
            export_endpoints.status_db.cancel_job(job_id)
            return {"status": "success"}

    monkeypatch.setattr(export_endpoints, "Exporter", CancellingExporter)

    resp = client.post("/export/dicom_move", json={
        "job_id": job_id, "path_to_csv": str(csv_path), "destination": "SOME_AE",
        "project_id": project_id, "username": username,
    })
    events = _parse_sse(resp.text)
    assert call_count == 1
    assert any(e["type"] == "cancelled" for e in events)
    assert events[-1]["type"] == "done"
