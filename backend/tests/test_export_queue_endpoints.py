"""
Integration tests for the three file-upload export endpoints
(backend/src/export/endpoints.py) -- confirm each enqueues onto the tasks
table (docs/worker-queue-design.md) rather than streaming SSE. Doesn't need
the PinnacleExport submodule: export/endpoints.py never imports
retrieve/logic.py.
"""
import os
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
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB

ANON_MRN = "1001"
REAL_MRN = "500123"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(export_endpoints.router)
    return TestClient(app)


def _csv_bytes(mrn: str) -> bytes:
    return f"patient_id\n{mrn}\n".encode()


def test_dicom_move_file_enqueues(client, active_project):
    project_id, username = active_project
    job_id = f"queue-test-{uuid.uuid4()}"

    resp = client.post(
        "/export/dicom_move_file",
        data={"job_id": job_id, "project_id": project_id, "username": username, "destination": "SOME_AE"},
        files={"file": ("patients.csv", _csv_bytes(ANON_MRN), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": job_id, "total": 1}

    tasks_db = TasksDB()
    task = tasks_db.claim("integration-test-worker")
    assert task is not None
    assert task["kind"] == "dicom_move"
    assert task["stage"] == "export"
    assert task["real_id"] == REAL_MRN
    assert task["display_id"] == ANON_MRN
    assert task["params"] == {"destination": "SOME_AE", "project_id": project_id, "username": username}

    _, submitted_count = StatusDB().count_imported_patients(job_id)
    assert submitted_count == 1


def test_proknow_upload_file_enqueues(client, active_project):
    project_id, username = active_project
    job_id = f"queue-test-{uuid.uuid4()}"

    resp = client.post(
        "/export/proknow_upload_file",
        data={"job_id": job_id, "project_id": project_id, "username": username, "collection": "SomeCollection"},
        files={"file": ("patients.csv", _csv_bytes(ANON_MRN), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": job_id, "total": 1}

    tasks_db = TasksDB()
    task = tasks_db.claim("integration-test-worker")
    assert task["kind"] == "proknow_upload"
    assert task["params"] == {"collection": "SomeCollection", "project_id": project_id, "username": username}


def test_dicom_move_uids_file_enqueues(client, active_project):
    project_id, username = active_project
    job_id = f"queue-test-{uuid.uuid4()}"
    csv_bytes = b"study_instance_uid\n1.2.3.4.5\n"

    resp = client.post(
        "/export/dicom_move_uids_file",
        data={"job_id": job_id, "project_id": project_id, "username": username, "destination": "SOME_AE"},
        files={"file": ("studies.csv", csv_bytes, "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": job_id, "total": 1}

    tasks_db = TasksDB()
    task = tasks_db.claim("integration-test-worker")
    assert task["kind"] == "uid_move"
    assert task["stage"] == "export"
    assert task["extra"] == {"study_uid": "1.2.3.4.5", "series_uid": None}
    assert task["params"] == {"destination": "SOME_AE", "project_id": project_id, "username": username}


def test_cancel_export_cancels_queued_tasks(client, active_project):
    project_id, username = active_project
    job_id = f"queue-test-{uuid.uuid4()}"

    client.post(
        "/export/dicom_move_file",
        data={"job_id": job_id, "project_id": project_id, "username": username, "destination": "SOME_AE"},
        files={"file": ("patients.csv", _csv_bytes(ANON_MRN), "text/csv")},
    )

    resp = client.post(f"/export/cancel/{job_id}")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}

    tasks_db = TasksDB()
    assert tasks_db.claim("integration-test-worker") is None  # cancelled, never claimable
