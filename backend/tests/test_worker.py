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


# --- Export handlers: _run_export, dispatched via _EXPORT_FACTORIES ---
#
# All three export kinds share one _run_export dispatcher that reuses
# export/endpoints.py's own worker factories directly (see worker.py's
# _HANDLERS comment for why, unlike _run_import) -- so what's worth testing
# here is worker.py's own wiring (BatchItem reconstruction, which params go
# where, submitted_by threading), not Exporter/Orthanc/ProKnow behaviour
# itself, which already has its own coverage (test_export_manifest.py,
# test_export_anon_boundary.py). Monkeypatching the factories to fakes
# keeps these tests fast and connection-free.

def test_reconstruct_batch_item_round_trips_extra():
    """The UID-move flow's "identifier" lives in extra, not real_id -- this
    must survive the claim -> task dict -> BatchItem round trip intact."""
    task = {
        "real_id": "1.2.3", "display_id": "1.2.3", "status_mrn": "R1",
        "input_path": "/tmp/x.csv", "extra": {"study_uid": "1.2.3", "series_uid": "1.2.3.4"},
    }
    item = worker._reconstruct_batch_item(task)
    assert item.real_id == "1.2.3"
    assert item.display_id == "1.2.3"
    assert item.status_mrn == "R1"
    assert item.input_path == "/tmp/x.csv"
    assert item.extra == {"study_uid": "1.2.3", "series_uid": "1.2.3.4"}


def test_run_export_dicom_move_calls_factory_with_params(monkeypatch):
    """dicom_move always threads a message_id kwarg through to
    _dicom_move_worker (None when the task's params don't have one) --
    unlike _proknow_worker/_uid_move_worker, which never see it at all
    (see test_run_export_message_id_is_dicom_move_only below)."""
    calls = []

    def _fake_factory(destination, submitted_by=None, message_id=None):
        calls.append({"destination": destination, "submitted_by": submitted_by, "message_id": message_id})
        return lambda item: {"mrn_seen": item.real_id}

    monkeypatch.setattr(worker.export_endpoints, "_dicom_move_worker", _fake_factory)

    task = {"kind": "dicom_move", "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
            "input_path": None, "extra": {},
            "params": {"destination": "SOME_AE", "project_id": "p", "username": "alice"}}
    result = worker._run_export(task)

    assert calls == [{"destination": "SOME_AE", "submitted_by": "alice", "message_id": None}]
    assert result == {"mrn_seen": "R1"}


def test_run_export_dicom_move_threads_message_id_through(monkeypatch):
    """The clinical-trial pseudonymisation-signalling path (docs on
    Exporter.dicom_c_move): a message_id in the task's params must reach
    _dicom_move_worker, which is what actually forwards it to Orthanc as
    MoveOriginatorID."""
    calls = []

    def _fake_factory(destination, submitted_by=None, message_id=None):
        calls.append({"destination": destination, "submitted_by": submitted_by, "message_id": message_id})
        return lambda item: {"mrn_seen": item.real_id}

    monkeypatch.setattr(worker.export_endpoints, "_dicom_move_worker", _fake_factory)

    task = {"kind": "dicom_move", "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
            "input_path": None, "extra": {},
            "params": {"destination": "TRIAL_AE", "message_id": 51966, "project_id": "p", "username": "alice"}}
    result = worker._run_export(task)

    assert calls == [{"destination": "TRIAL_AE", "submitted_by": "alice", "message_id": 51966}]
    assert result == {"mrn_seen": "R1"}


def test_run_export_message_id_is_dicom_move_only(monkeypatch):
    """proknow_upload/uid_move factories don't accept a message_id kwarg at
    all -- _run_export must not pass one, even if a stray "message_id" key
    somehow ended up in their task params (e.g. hand-edited params)."""
    calls = []

    def _fake_proknow(collection, submitted_by=None):
        calls.append({"collection": collection, "submitted_by": submitted_by})
        return lambda item: {"ok": True}

    monkeypatch.setattr(worker.export_endpoints, "_proknow_worker", _fake_proknow)

    task = {"kind": "proknow_upload", "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
            "input_path": None, "extra": {},
            "params": {"collection": "C", "message_id": 51966, "project_id": "p", "username": "bob"}}
    result = worker._run_export(task)  # would TypeError if message_id were passed to _fake_proknow

    assert calls == [{"collection": "C", "submitted_by": "bob"}]
    assert result == {"ok": True}


def test_run_export_proknow_upload_calls_factory_with_params(monkeypatch):
    calls = []

    def _fake_factory(collection, submitted_by=None):
        calls.append({"collection": collection, "submitted_by": submitted_by})
        return lambda item: {"mrn_seen": item.real_id}

    monkeypatch.setattr(worker.export_endpoints, "_proknow_worker", _fake_factory)

    task = {"kind": "proknow_upload", "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
            "input_path": None, "extra": {},
            "params": {"collection": "SomeCollection", "project_id": "p", "username": "bob"}}
    result = worker._run_export(task)

    assert calls == [{"collection": "SomeCollection", "submitted_by": "bob"}]
    assert result == {"mrn_seen": "R1"}


def test_run_export_uid_move_calls_factory_and_preserves_extra(monkeypatch):
    calls = []
    seen_items = []

    def _fake_factory(destination, submitted_by=None):
        calls.append({"destination": destination, "submitted_by": submitted_by})

        def _worker(item):
            seen_items.append(item)
            return {"status": "Success"}
        return _worker

    monkeypatch.setattr(worker.export_endpoints, "_uid_move_worker", _fake_factory)

    task = {
        "kind": "uid_move", "real_id": "1.2.3", "display_id": "1.2.3", "status_mrn": "R1", "input_path": None,
        "extra": {"study_uid": "1.2.3", "series_uid": None},
        "params": {"destination": "SOME_AE", "project_id": "p", "username": "carol"},
    }
    result = worker._run_export(task)

    assert calls == [{"destination": "SOME_AE", "submitted_by": "carol"}]
    assert result == {"status": "Success"}
    assert seen_items[0].extra == {"study_uid": "1.2.3", "series_uid": None}


def test_export_kinds_registered_in_handlers():
    assert set(worker._HANDLERS) == {"import", "dicom_move", "proknow_upload", "uid_move"}
    assert worker._HANDLERS["dicom_move"] is worker._run_export
    assert worker._HANDLERS["proknow_upload"] is worker._run_export
    assert worker._HANDLERS["uid_move"] is worker._run_export


# --- Chained export: _maybe_chain_export + its wiring into _handle_one ---
#
# A combined import->export job (backend/src/retrieve/endpoints.py's
# batch_import_file, export_kind param) puts a "chain_export" block onto an
# import task's params. On success, _maybe_chain_export enqueues a follow-up
# export task on the same job_id -- these tests exercise that mechanism
# directly (_maybe_chain_export in isolation) and end-to-end through
# _handle_one's real "import" _HANDLERS entry (monkeypatched to a fake, since
# a real one needs Mosaiq/Pinnacle/ProKnow/Orthanc).

def _chain_params(project_id, username, kind="dicom_move", **extra):
    chain = {"kind": kind, **extra}
    return {"import_level": "Planning data", "project_id": project_id, "username": username,
            "chain_export": chain}


def test_maybe_chain_export_enqueues_dicom_move_when_imported(tasks_db, status_db, job_id, active_project):
    project_id, username = active_project
    task = {
        "job_id": job_id, "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
        "params": _chain_params(project_id, username, kind="dicom_move", destination="SOME_AE"),
    }
    worker._maybe_chain_export(tasks_db, status_db, task, {"imported": True})

    rows = tasks_db.job_progress(job_id)
    export_rows = [r for r in rows if r["kind"] == "dicom_move"]
    assert len(export_rows) == 1
    assert export_rows[0]["stage"] == "export"
    assert export_rows[0]["state"] == "queued"

    exported = tasks_db.claim("test-worker")
    assert exported["real_id"] == "R1"
    assert exported["display_id"] == "A1"
    assert exported["status_mrn"] == "R1"
    assert exported["params"] == {"destination": "SOME_AE", "project_id": project_id, "username": username}


def test_maybe_chain_export_proknow_upload_when_imported(tasks_db, status_db, job_id, active_project):
    project_id, username = active_project
    task = {
        "job_id": job_id, "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
        "params": _chain_params(project_id, username, kind="proknow_upload", collection="SomeCollection"),
    }
    worker._maybe_chain_export(tasks_db, status_db, task, {"imported": True})

    claimed = tasks_db.claim("test-worker")
    assert claimed["kind"] == "proknow_upload"
    assert claimed["params"] == {"collection": "SomeCollection", "project_id": project_id, "username": username}


def test_maybe_chain_export_includes_message_id_for_dicom_move_only(tasks_db, status_db, job_id, active_project):
    project_id, username = active_project
    task = {
        "job_id": job_id, "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
        "params": _chain_params(project_id, username, kind="dicom_move", destination="TRIAL_AE", message_id=51966),
    }
    worker._maybe_chain_export(tasks_db, status_db, task, {"imported": True})

    claimed = tasks_db.claim("test-worker")
    assert claimed["params"]["message_id"] == 51966


def test_maybe_chain_export_skips_when_not_imported(tasks_db, status_db, job_id, active_project):
    """A patient not found on import (imported falsy/absent) must never get
    a chained export task -- it would have nothing to export."""
    project_id, username = active_project
    task = {
        "job_id": job_id, "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
        "params": _chain_params(project_id, username, destination="SOME_AE"),
    }
    worker._maybe_chain_export(tasks_db, status_db, task, {"imported": False})
    worker._maybe_chain_export(tasks_db, status_db, task, {})  # no "imported" key at all

    assert tasks_db.job_progress(job_id) == []


def test_maybe_chain_export_skips_when_job_cancelled(tasks_db, status_db, job_id, active_project):
    """A cancelled job shouldn't spawn new export work just because an
    in-flight import happened to finish afterward."""
    project_id, username = active_project
    status_db.cancel_job(job_id)
    task = {
        "job_id": job_id, "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
        "params": _chain_params(project_id, username, destination="SOME_AE"),
    }
    worker._maybe_chain_export(tasks_db, status_db, task, {"imported": True})

    assert tasks_db.job_progress(job_id) == []


def test_maybe_chain_export_no_op_without_chain_export_param(tasks_db, status_db, job_id, active_project):
    """A plain import task (no export_kind at submission) must never chain --
    the overwhelming majority of import tasks today."""
    project_id, username = active_project
    task = {
        "job_id": job_id, "real_id": "R1", "display_id": "A1", "status_mrn": "R1",
        "params": {"import_level": "Planning data", "project_id": project_id, "username": username},
    }
    worker._maybe_chain_export(tasks_db, status_db, task, {"imported": True})

    assert tasks_db.job_progress(job_id) == []


def test_chain_export_enqueued_before_import_task_marked_succeeded(
    tasks_db, status_db, job_id, active_project, _restore_handlers,
):
    """
    THE race-condition regression test. results/endpoints.py's _observe_job
    decides a job is `done` once nothing is left queued/claimed/running --
    if the chained export task were enqueued AFTER the import task's own row
    committed to 'succeeded', a poll landing in that gap would see zero
    pending tasks and end the stream early. _handle_one must call
    _maybe_chain_export before mark_succeeded, not after.

    This wraps tasks_db.mark_succeeded so that, at the exact moment it's
    called, the test can assert the chained export task already exists in
    'queued' state -- proving the ordering directly rather than just
    checking final state (a test that only checked final state would pass
    even with the buggy post-mark_succeeded ordering, since both rows exist
    by the time _handle_one returns either way).
    """
    project_id, username = active_project
    worker._HANDLERS["import"] = lambda task: {"imported": True}

    item = BatchItem(real_id="R1", display_id="A1", status_mrn="R1")
    params = _chain_params(project_id, username, destination="SOME_AE")
    tasks_db.enqueue(job_id, [item], kind="import", stage="retrieve", params=params)
    task = tasks_db.claim("test-worker")

    real_mark_succeeded = tasks_db.mark_succeeded
    observed_export_states_at_call_time = []

    def _spying_mark_succeeded(task_id, worker_id, details=None):
        rows = tasks_db.job_progress(job_id)
        export_rows = [r for r in rows if r["kind"] == "dicom_move"]
        observed_export_states_at_call_time.append([r["state"] for r in export_rows])
        return real_mark_succeeded(task_id, worker_id, details)

    tasks_db.mark_succeeded = _spying_mark_succeeded
    try:
        worker._handle_one(tasks_db, status_db, task)
    finally:
        tasks_db.mark_succeeded = real_mark_succeeded

    # The export task must already exist (queued) at the moment
    # mark_succeeded was called for the import task.
    assert observed_export_states_at_call_time == [["queued"]]

    import_row = tasks_db.get_task(task["task_id"])
    assert import_row["state"] == "succeeded"


def test_handle_one_chain_export_failure_does_not_block_import_success(
    tasks_db, status_db, job_id, active_project, monkeypatch, _restore_handlers,
):
    """A chain-enqueue failure (e.g. a transient DB error) must not crash the
    worker or fail the import task, which genuinely succeeded -- it should
    be recorded as its own export-stage failure event instead."""
    project_id, username = active_project
    worker._HANDLERS["import"] = lambda task: {"imported": True}

    def _boom(*args, **kwargs):
        raise RuntimeError("enqueue exploded")

    monkeypatch.setattr(worker, "_maybe_chain_export", lambda *a, **k: _boom())

    item = BatchItem(real_id="R1", display_id="A1", status_mrn="R1")
    params = _chain_params(project_id, username, destination="SOME_AE")
    tasks_db.enqueue(job_id, [item], kind="import", stage="retrieve", params=params)
    task = tasks_db.claim("test-worker")

    worker._handle_one(tasks_db, status_db, task)  # must not raise

    import_row = tasks_db.get_task(task["task_id"])
    assert import_row["state"] == "succeeded"

    history = status_db.get_patient_history(job_id, "R1")
    export_failures = [e for e in history if e["stage"] == "export" and e["event_type"] == "failure"]
    assert len(export_failures) == 1


def test_handle_one_dicom_move_end_to_end(tasks_db, status_db, job_id, active_project, monkeypatch, _restore_handlers):
    """A full claim/execute/terminal-write pass through the real 'dicom_move'
    _HANDLERS entry (not a fake kind), with only the underlying Exporter
    call faked out."""
    project_id, username = active_project

    def _fake_factory(destination, submitted_by=None, message_id=None):
        return lambda item: {"destination": destination, "submitted_by": submitted_by, "status": "Success"}

    monkeypatch.setattr(worker.export_endpoints, "_dicom_move_worker", _fake_factory)

    item = BatchItem(real_id="R1", display_id="A1", status_mrn="R1")
    tasks_db.enqueue(job_id, [item], kind="dicom_move", stage="export",
                      params={"destination": "SOME_AE", "project_id": project_id, "username": username})
    task = tasks_db.claim("test-worker")

    worker._handle_one(tasks_db, status_db, task)

    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "succeeded"
    assert row["details"] == {"destination": "SOME_AE", "submitted_by": username, "status": "Success"}
