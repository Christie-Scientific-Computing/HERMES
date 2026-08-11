"""
Tests for backend/worker.py -- the queue-driven task runner
(docs/worker-queue-design.md). Exercises the claim/execute/terminal-write
loop via a fake "kind" registered in worker._HANDLERS, so these tests
never touch Importer/Exporter or any real Mosaiq/Pinnacle/ProKnow/Orthanc
connection -- that's Importer's/Exporter's own responsibility, already
covered by their own tests. This file only tests worker.py's orchestration:
claim, the ethics re-check, terminal-state writes, and the events it logs.

Needs the PinnacleExport submodule: backend/worker.py imports
retrieve/endpoints.py -> retrieve/logic.py -> PinnacleExport at module
level, same as test_retrieve_endpoints_errors.py -- skips gracefully if
it isn't checked out.
"""
import uuid

import pytest

pytest.importorskip("backend.src.retrieve.PinnacleExport", reason="PinnacleExport submodule not checked out")

import backend.worker as worker
from backend.src.common.sse import BatchItem
from backend.src.projects.db_client import ProjectsDB
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB


@pytest.fixture
def tasks_db():
    return TasksDB()


@pytest.fixture
def status_db():
    return StatusDB()


@pytest.fixture
def job_id(status_db):
    job_id = f"worker-test-{uuid.uuid4()}"
    status_db.create_job(job_id, description="worker test")
    return job_id


@pytest.fixture
def _restore_handlers():
    """_HANDLERS is module-level state; tests register a fake kind onto it
    and must restore it afterward so other tests (and the real 'import'
    entry) aren't affected."""
    original = dict(worker._HANDLERS)
    yield
    worker._HANDLERS.clear()
    worker._HANDLERS.update(original)


def _enqueue_one(tasks_db, job_id, kind, params=None):
    item = BatchItem(real_id="R1", display_id="A1", status_mrn="R1")
    tasks_db.enqueue(job_id, [item], kind=kind, stage="retrieve", params=params or {})
    return tasks_db.claim("test-worker")


def test_handle_one_success_path(tasks_db, status_db, job_id, active_project, _restore_handlers):
    project_id, username = active_project
    worker._HANDLERS["fake_success"] = lambda task: {"ok": True, "real_id": task["real_id"]}

    task = _enqueue_one(tasks_db, job_id, "fake_success",
                         params={"project_id": project_id, "username": username})
    worker._handle_one(tasks_db, status_db, task)

    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "succeeded"
    assert row["details"] == {"ok": True, "real_id": "R1"}

    history = status_db.get_patient_history(job_id, "R1")
    assert [e["event_type"] for e in history] == ["start", "success"]
    assert history[1]["details"] == {"ok": True, "real_id": "R1"}
    assert history[1]["attempt"] == 1
    assert history[1]["task_id"] == task["task_id"]


def test_handle_one_failure_below_max_attempts_requeues(tasks_db, status_db, job_id, active_project, _restore_handlers):
    project_id, username = active_project

    def _always_fail(task):
        raise ValueError("transient")

    worker._HANDLERS["fake_fail"] = _always_fail

    task = _enqueue_one(tasks_db, job_id, "fake_fail",
                         params={"project_id": project_id, "username": username})
    from backend.src.db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE tasks SET max_attempts = 3 WHERE task_id = %s", (task["task_id"],))

    worker._handle_one(tasks_db, status_db, task)

    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "queued"  # requeued, not terminal
    assert row["attempts"] == 1

    history = status_db.get_patient_history(job_id, "R1")
    assert [e["event_type"] for e in history] == ["start", "failure"]
    assert history[1]["attempt"] == 1


def test_handle_one_failure_at_max_attempts_is_terminal(tasks_db, status_db, job_id, active_project, _restore_handlers):
    project_id, username = active_project

    def _always_fail(task):
        raise ValueError("permanent")

    worker._HANDLERS["fake_fail"] = _always_fail

    task = _enqueue_one(tasks_db, job_id, "fake_fail",
                         params={"project_id": project_id, "username": username})
    worker._handle_one(tasks_db, status_db, task)  # max_attempts defaults to 1

    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "failed"
    assert row["attempts"] == 1

    history = status_db.get_patient_history(job_id, "R1")
    assert [e["event_type"] for e in history] == ["start", "failure"]
    assert "permanent" in history[1]["error_message"]


def test_handle_one_job_cancelled_mid_flight_still_finishes(tasks_db, status_db, job_id, active_project, _restore_handlers):
    """
    Cancellation only prevents future claims of queued tasks
    (TasksDB.claim excludes cancelled jobs; test_tasks_db.py covers that
    directly). A task already claimed and running is not interrupted --
    "in-flight items finish" is the same promise run_batch_job's
    cancellation already makes elsewhere in this codebase.
    """
    project_id, username = active_project
    worker._HANDLERS["fake_success"] = lambda task: {"ok": True}

    task = _enqueue_one(tasks_db, job_id, "fake_success",
                         params={"project_id": project_id, "username": username})
    status_db.cancel_job(job_id)  # job cancelled after the task was already claimed

    worker._handle_one(tasks_db, status_db, task)

    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "succeeded"  # completed despite the cancellation


def test_handle_one_project_revoked_after_enqueue_cancels_not_retries(tasks_db, status_db, job_id, active_project, _restore_handlers):
    project_id, username = active_project
    calls = []
    worker._HANDLERS["fake_success"] = lambda task: calls.append(task) or {"ok": True}

    task = _enqueue_one(tasks_db, job_id, "fake_success",
                         params={"project_id": project_id, "username": username})

    ProjectsDB().revoke_project(project_id, revoked_by="admin", comment="test revocation")

    worker._handle_one(tasks_db, status_db, task)

    assert calls == []  # the handler must never run for a denied task
    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "cancelled"

    history = status_db.get_patient_history(job_id, "R1")
    assert [e["event_type"] for e in history] == ["failure"]


def test_handle_one_denial_with_unknown_project_also_cancels(tasks_db, status_db, job_id, _restore_handlers):
    """No active_project at all -- require_project_member should still deny
    (fail closed) rather than the worker raising an unhandled exception."""
    calls = []
    worker._HANDLERS["fake_success"] = lambda task: calls.append(task) or {"ok": True}

    task = _enqueue_one(tasks_db, job_id, "fake_success",
                         params={"project_id": "no-such-project", "username": "nobody"})

    worker._handle_one(tasks_db, status_db, task)

    assert calls == []
    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "cancelled"
