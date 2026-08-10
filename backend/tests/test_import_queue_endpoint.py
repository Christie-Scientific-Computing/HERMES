"""
Integration test for batch_import_file's HERMES_USE_QUEUE path
(backend/src/retrieve/endpoints.py) -- confirms the endpoint enqueues tasks
rather than streaming SSE when the flag is set. Needs the PinnacleExport
submodule (imports retrieve/endpoints.py -> retrieve/logic.py ->
PinnacleExport) -- skips gracefully if it isn't checked out, matching
test_retrieve_endpoints_errors.py's pattern.

Deliberately doesn't exercise the flag-unset (streaming) path here: that
path calls Importer.handle_patient for real, which needs live
Mosaiq/Pinnacle/ProKnow/Orthanc connectivity this test environment doesn't
have -- it's already covered by test_sse.py (run_batch_job directly, with
a fake worker) and is unchanged by this diff.
"""
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

from backend.src.retrieve import endpoints as retrieve_endpoints
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB

ANON_MRN = "1001"
REAL_MRN = "500123"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(retrieve_endpoints.router)
    return TestClient(app)


def _csv_bytes(mrn: str) -> bytes:
    return f"patient_id\n{mrn}\n".encode()


def test_batch_import_file_enqueues_when_queue_flag_set(client, monkeypatch, active_project):
    project_id, username = active_project
    monkeypatch.setenv("HERMES_USE_QUEUE", "1")
    job_id = f"queue-test-{uuid.uuid4()}"

    resp = client.post(
        "/import/batch_import_file",
        data={"job_id": job_id, "project_id": project_id, "username": username, "import_level": "Planning data"},
        files={"file": ("patients.csv", _csv_bytes(ANON_MRN), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": job_id, "total": 1}

    tasks_db = TasksDB()
    assert tasks_db.count_tasks(job_id) == 1
    task = tasks_db.claim("integration-test-worker")
    assert task is not None
    assert task["kind"] == "import"
    assert task["stage"] == "retrieve"
    assert task["real_id"] == REAL_MRN          # real id, never the submitted anon id
    assert task["display_id"] == ANON_MRN

    # add_patient is called at enqueue time (not deferred to the worker), so
    # the "M" (submitted) half of the "N/M imported" stat is correct
    # immediately, regardless of whether/when a worker claims the task.
    _, submitted_count = StatusDB().count_imported_patients(job_id)
    assert submitted_count == 1
    assert task["status_mrn"] == REAL_MRN
    assert task["params"] == {"import_level": "Planning data", "project_id": project_id, "username": username}


def test_batch_import_file_does_not_enqueue_when_flag_unset(client, monkeypatch, active_project):
    project_id, username = active_project
    monkeypatch.delenv("HERMES_USE_QUEUE", raising=False)
    job_id = f"queue-test-{uuid.uuid4()}"

    # Deliberately not consuming the SSE stream body -- entering the request
    # is enough to prove the queue path wasn't taken; actually running
    # Importer.handle_patient needs live external connectivity this test
    # environment doesn't have (see module docstring).
    with client.stream(
        "POST", "/import/batch_import_file",
        data={"job_id": job_id, "project_id": project_id, "username": username, "import_level": "Planning data"},
        files={"file": ("patients.csv", _csv_bytes(ANON_MRN), "text/csv")},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

    tasks_db = TasksDB()
    assert tasks_db.count_tasks(job_id) == 0  # nothing enqueued on the non-queue path
