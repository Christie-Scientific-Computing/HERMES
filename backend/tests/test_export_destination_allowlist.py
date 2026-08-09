"""
End-to-end test of the per-user export destination allow-list
(docs/safety-plan.md §A) wired into export/endpoints.py: an export request
403s when the requesting user has a restricted allow-list that doesn't
include the requested destination, and succeeds when it does (or when the
user is unrestricted). Follows test_export_anon_boundary.py's pattern for
building a FastAPI TestClient against export/endpoints.py with a mocked
Exporter -- no real Orthanc/ProKnow needed.
"""
import os
import csv
import uuid

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.src.access.db_client import AccessDB
from backend.src.export import endpoints as export_endpoints
from backend.src.export.logic import Exporter as RealExporter

REAL_MRN_1, ANON_MRN_1 = "500123", "1001"


@pytest.fixture
def access_db():
    return AccessDB()


@pytest.fixture
def client(monkeypatch):
    class FakeExporter:
        read_input_file = staticmethod(RealExporter.read_input_file)

        def __init__(self, destination):
            self.destination = destination

        def dicom_c_move(self, patient_id):
            return {"status": "success"}

        def upload_to_proknow(self, patient_id):
            return {"status": "success"}

    monkeypatch.setattr(export_endpoints, "Exporter", FakeExporter)

    app = FastAPI()
    app.include_router(export_endpoints.router)
    return TestClient(app)


def _csv_with_one_patient(tmp_path, anon_mrn=ANON_MRN_1):
    csv_path = tmp_path / "patients.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["patient_id"])
        writer.writerow([anon_mrn])
    return csv_path


def test_dicom_move_succeeds_when_user_unrestricted(client, tmp_path, active_project):
    """No allow-list rows at all for this user -- today's behavior,
    unchanged: any destination is accepted."""
    project_id, username = active_project
    csv_path = _csv_with_one_patient(tmp_path)

    resp = client.post("/export/dicom_move", json={
        "job_id": f"job-{uuid.uuid4()}", "path_to_csv": str(csv_path), "destination": "ANY_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200


def test_dicom_move_403s_when_destination_not_in_allowlist(client, tmp_path, access_db, active_project):
    project_id, username = active_project
    access_db.add(username, "dicom_modality", "ALLOWED_AE", added_by="admin")
    csv_path = _csv_with_one_patient(tmp_path)

    resp = client.post("/export/dicom_move", json={
        "job_id": f"job-{uuid.uuid4()}", "path_to_csv": str(csv_path), "destination": "OTHER_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 403


def test_dicom_move_succeeds_when_destination_in_allowlist(client, tmp_path, access_db, active_project):
    project_id, username = active_project
    access_db.add(username, "dicom_modality", "ALLOWED_AE", added_by="admin")
    csv_path = _csv_with_one_patient(tmp_path)

    resp = client.post("/export/dicom_move", json={
        "job_id": f"job-{uuid.uuid4()}", "path_to_csv": str(csv_path), "destination": "ALLOWED_AE",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200


def test_proknow_upload_403s_when_collection_not_in_allowlist(client, tmp_path, access_db, active_project):
    project_id, username = active_project
    access_db.add(username, "proknow_collection", "AllowedCollection", added_by="admin")
    csv_path = _csv_with_one_patient(tmp_path)

    resp = client.post("/export/proknow_upload", json={
        "job_id": f"job-{uuid.uuid4()}", "path_to_csv": str(csv_path), "collection": "OtherCollection",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 403


def test_proknow_upload_succeeds_when_collection_in_allowlist(client, tmp_path, access_db, active_project):
    project_id, username = active_project
    access_db.add(username, "proknow_collection", "AllowedCollection", added_by="admin")
    csv_path = _csv_with_one_patient(tmp_path)

    resp = client.post("/export/proknow_upload", json={
        "job_id": f"job-{uuid.uuid4()}", "path_to_csv": str(csv_path), "collection": "AllowedCollection",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 200


def test_proknow_upload_patient_403s_when_collection_not_in_allowlist(client, access_db, active_project):
    project_id, username = active_project
    access_db.add(username, "proknow_collection", "AllowedCollection", added_by="admin")

    resp = client.post("/export/proknow_upload_patient", json={
        "job_id": f"job-{uuid.uuid4()}", "mrn": ANON_MRN_1, "collection": "OtherCollection",
        "project_id": project_id, "username": username,
    })
    assert resp.status_code == 403


def test_dicom_move_destination_check_runs_after_project_membership_check(client, tmp_path, access_db):
    """A user who isn't even a project member should get the 403 for THAT
    reason, not the destination check -- require_project_member runs first."""
    csv_path = _csv_with_one_patient(tmp_path)
    stranger = f"stranger-{uuid.uuid4()}"
    access_db.add(stranger, "dicom_modality", "ALLOWED_AE", added_by="admin")

    resp = client.post("/export/dicom_move", json={
        "job_id": f"job-{uuid.uuid4()}", "path_to_csv": str(csv_path), "destination": "ALLOWED_AE",
        "project_id": str(uuid.uuid4()), "username": stranger,
    })
    assert resp.status_code == 403
