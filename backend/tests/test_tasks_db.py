"""
Tests for TasksDB (backend/src/status/tasks_db.py) — the per-item task
queue backing the worker queue (docs/worker-queue-design.md).
"""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.src.common.sse import BatchItem
from backend.src.status.db_client import StatusDB
from backend.src.status.hash_chain import compute_row_hash
from backend.src.status.tasks_db import TasksDB
from backend.src.db import get_conn


@pytest.fixture(autouse=True)
def _clean_tasks_table():
    """
    TasksDB.claim() is deliberately global -- a real worker claims the next
    queued task across every job, not just one. Against this suite's shared,
    persistent test Postgres (tests don't run in a transaction that rolls
    back), leftover 'queued' rows from an earlier run or another test in
    this file would otherwise be claimable by an unrelated test, making
    claim-ordering assertions flaky. Truncate before every test in this file
    so each one starts from an empty tasks table; events.task_id is
    ON DELETE SET NULL (see the tasks migration), so this never fails on FK
    references from events written by other tests.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM tasks")
    yield


@pytest.fixture
def tasks_db():
    return TasksDB()


@pytest.fixture
def status_db():
    return StatusDB()


@pytest.fixture
def job_id(status_db):
    job_id = f"tasks-test-{uuid.uuid4()}"
    status_db.create_job(job_id, description="tasks_db test")
    return job_id


def _items(n: int) -> list[BatchItem]:
    return [
        BatchItem(real_id=f"R{i}", display_id=f"A{i}", status_mrn=f"R{i}")
        for i in range(n)
    ]


def test_enqueue_inserts_one_row_per_item(tasks_db, job_id):
    inserted = tasks_db.enqueue(job_id, _items(3), kind="import", stage="retrieve",
                                 params={"import_level": "Planning data"})
    assert inserted == 3
    assert tasks_db.count_tasks(job_id) == 3


def test_enqueue_empty_list_is_a_no_op(tasks_db, job_id):
    assert tasks_db.enqueue(job_id, [], kind="import", stage="retrieve", params={}) == 0
    assert tasks_db.count_tasks(job_id) == 0


def test_claim_returns_none_when_nothing_queued(tasks_db, job_id):
    assert tasks_db.claim("worker-1") is None


def test_claim_marks_state_and_returns_full_row(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve",
                      params={"import_level": "Planning data"})
    task = tasks_db.claim("worker-1")
    assert task is not None
    assert task["state"] == "claimed"
    assert task["claimed_by"] == "worker-1"
    assert task["claimed_at"] is not None
    assert task["real_id"] == "R0"
    assert task["display_id"] == "A0"
    assert task["params"] == {"import_level": "Planning data"}


def test_each_task_claimed_exactly_once(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(5), kind="import", stage="retrieve", params={})
    claimed_ids = []
    while True:
        task = tasks_db.claim("worker-1")
        if task is None:
            break
        claimed_ids.append(task["task_id"])
    assert len(claimed_ids) == 5
    assert len(set(claimed_ids)) == 5  # every task claimed exactly once


def test_concurrent_claims_get_distinct_task_ids(tasks_db, job_id):
    """
    The core SKIP LOCKED guarantee: two real connections claiming
    concurrently must never both come back with the same task_id.
    """
    tasks_db.enqueue(job_id, _items(20), kind="import", stage="retrieve", params={})

    results: list[dict] = []
    lock = threading.Lock()

    def _claim_loop(worker_id: str):
        db = TasksDB()
        while True:
            task = db.claim(worker_id)
            if task is None:
                return
            with lock:
                results.append(task)

    threads = [threading.Thread(target=_claim_loop, args=(f"worker-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed_ids = [r["task_id"] for r in results]
    assert len(claimed_ids) == 20
    assert len(set(claimed_ids)) == 20


def test_claim_skips_tasks_of_cancelled_job(tasks_db, status_db, job_id):
    tasks_db.enqueue(job_id, _items(2), kind="import", stage="retrieve", params={})
    status_db.cancel_job(job_id)
    assert tasks_db.claim("worker-1") is None


def test_claim_prefers_higher_priority(tasks_db, job_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tasks(job_id, kind, stage, real_id, display_id, status_mrn, priority, created_at) "
            "VALUES (%s, 'import', 'retrieve', 'LOW', 'LOW', 'LOW', 0, %s)",
            (job_id, datetime.now(timezone.utc)),
        )
        cur.execute(
            "INSERT INTO tasks(job_id, kind, stage, real_id, display_id, status_mrn, priority, created_at) "
            "VALUES (%s, 'import', 'retrieve', 'HIGH', 'HIGH', 'HIGH', 10, %s)",
            (job_id, datetime.now(timezone.utc)),
        )
    task = tasks_db.claim("worker-1")
    assert task["real_id"] == "HIGH"


def test_mark_succeeded_sets_state_and_details(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"])
    tasks_db.mark_succeeded(task["task_id"], details={"imported": True})

    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "succeeded"
    assert row["details"] == {"imported": True}
    assert row["finished_at"] is not None


def test_mark_failed_terminal_by_default(tasks_db, job_id):
    """max_attempts defaults to 1: one failure is always terminal, matching
    today's un-retried run_batch_job behaviour."""
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"])

    outcome = tasks_db.mark_failed(task["task_id"], "boom")
    assert outcome == "failed"

    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "failed"
    assert row["attempts"] == 1
    assert row["error_message"] == "boom"
    assert row["finished_at"] is not None


def test_mark_failed_requeues_while_attempts_remain(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE tasks SET max_attempts = 3 WHERE task_id = %s", (task["task_id"],))
    tasks_db.mark_running(task["task_id"])

    outcome = tasks_db.mark_failed(task["task_id"], "transient error")
    assert outcome == "requeued"
    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "queued"
    assert row["attempts"] == 1
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None
    assert row["finished_at"] is None

    # requeued task is claimable again
    reclaimed = tasks_db.claim("worker-2")
    assert reclaimed["task_id"] == task["task_id"]
    tasks_db.mark_running(reclaimed["task_id"])

    # second failure: attempts becomes 2, still < max_attempts (3) -> requeued again
    outcome2 = tasks_db.mark_failed(reclaimed["task_id"], "still failing")
    assert outcome2 == "requeued"
    reclaimed2 = tasks_db.claim("worker-3")
    tasks_db.mark_running(reclaimed2["task_id"])

    # third failure: attempts becomes 3, no longer < max_attempts (3) -> terminal
    outcome3 = tasks_db.mark_failed(reclaimed2["task_id"], "final failure")
    assert outcome3 == "failed"
    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "failed"
    assert row["attempts"] == 3


def test_cancel_task_is_terminal(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    applied = tasks_db.cancel_task(task["task_id"], reason="project membership revoked")
    assert applied is True

    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "cancelled"
    assert row["error_message"] == "project membership revoked"


def test_mark_running_guards_against_non_claimed_state(tasks_db, job_id):
    """A task can only move claimed -> running once; a second call (e.g. a
    duplicate/late worker invocation) must be a no-op, not silently reset
    started_at/claimed_at on an already-running task."""
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")

    assert tasks_db.mark_running(task["task_id"]) is True
    first_started_at = tasks_db.get_task(task["task_id"])["started_at"]

    assert tasks_db.mark_running(task["task_id"]) is False
    assert tasks_db.get_task(task["task_id"])["started_at"] == first_started_at


def test_mark_running_refreshes_claimed_at_so_reap_does_not_double_claim(tasks_db, job_id):
    """
    Regression test for the bug where reap_stale_claims judged a *running*
    task's staleness against its original claim time. Backdate claimed_at
    to simulate a task that was claimed a long time ago, then transition it
    to running -- mark_running must refresh claimed_at, so a reap sweep
    with a threshold that would have caught the old claim time leaves this
    legitimately-running task alone.
    """
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    with get_conn() as conn, conn.cursor() as cur:
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=7200)
        cur.execute("UPDATE tasks SET claimed_at = %s WHERE task_id = %s", (old_ts, task["task_id"]))

    tasks_db.mark_running(task["task_id"])

    reaped = tasks_db.reap_stale_claims(stale_seconds=1800)
    assert reaped == 0
    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "running"
    assert row["claimed_by"] == "worker-1"  # not reset by a reap


def test_mark_succeeded_guards_against_terminal_state(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"])
    assert tasks_db.mark_succeeded(task["task_id"], details={"imported": True}) is True

    # a duplicate/late call after the task already succeeded must not
    # silently overwrite it (e.g. re-run with different details)
    applied_again = tasks_db.mark_succeeded(task["task_id"], details={"imported": False})
    assert applied_again is False
    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "succeeded"
    assert row["details"] == {"imported": True}  # unchanged


def test_mark_failed_guards_against_terminal_state(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"])
    tasks_db.mark_succeeded(task["task_id"], details={})

    outcome = tasks_db.mark_failed(task["task_id"], "late failure report")
    assert outcome == "unchanged"
    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "succeeded"  # not resurrected into failed/queued
    assert row["attempts"] == 0  # not incremented either


def test_cancel_task_guards_against_terminal_state(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"])
    tasks_db.mark_succeeded(task["task_id"], details={"imported": True})

    applied = tasks_db.cancel_task(task["task_id"], reason="revoked after completion")
    assert applied is False
    row = tasks_db.get_task(task["task_id"])
    assert row["state"] == "succeeded"  # not overwritten to cancelled


def test_cancel_queued_leaves_running_rows_alone(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(3), kind="import", stage="retrieve", params={})
    running_task = tasks_db.claim("worker-1")
    tasks_db.mark_running(running_task["task_id"])

    cancelled_count = tasks_db.cancel_queued(job_id)
    assert cancelled_count == 2  # the other two, still queued

    row = tasks_db.get_task(running_task["task_id"])
    assert row["state"] == "running"  # untouched


def test_reap_stale_claims_requeues_old_claim_and_leaves_recent_alone(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(2), kind="import", stage="retrieve", params={})
    stale = tasks_db.claim("worker-dead")
    fresh = tasks_db.claim("worker-alive")

    # backdate only the "stale" claim
    with get_conn() as conn, conn.cursor() as cur:
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=7200)
        cur.execute("UPDATE tasks SET claimed_at = %s WHERE task_id = %s", (old_ts, stale["task_id"]))

    reaped = tasks_db.reap_stale_claims(stale_seconds=1800)
    assert reaped == 1

    stale_row = tasks_db.get_task(stale["task_id"])
    assert stale_row["state"] == "queued"
    assert stale_row["claimed_by"] is None
    assert stale_row["claimed_at"] is None

    fresh_row = tasks_db.get_task(fresh["task_id"])
    assert fresh_row["state"] == "claimed"  # untouched, claimed recently
    assert fresh_row["claimed_by"] == "worker-alive"


def test_job_progress_only_returns_rows_after_watermark(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(3), kind="import", stage="retrieve", params={})
    all_rows = tasks_db.job_progress(job_id)
    assert len(all_rows) == 3

    watermark = all_rows[1]["task_id"]
    remaining = tasks_db.job_progress(job_id, after_task_id=watermark)
    assert len(remaining) == 1
    assert remaining[0]["task_id"] == all_rows[2]["task_id"]


def test_job_has_pending_true_until_all_terminal(tasks_db, job_id):
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    assert tasks_db.job_has_pending(job_id) is True

    task = tasks_db.claim("worker-1")
    tasks_db.mark_running(task["task_id"])
    assert tasks_db.job_has_pending(job_id) is True

    tasks_db.mark_succeeded(task["task_id"], details={})
    assert tasks_db.job_has_pending(job_id) is False


def _event_row(job_id: str) -> dict:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, mrn, stage, event_type, ts, attempt, error_message, details, "
            "prev_hash, row_hash, task_id FROM events WHERE job_id = %s",
            (job_id,),
        )
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))


def test_add_event_task_id_does_not_perturb_the_hash_chain(tasks_db, status_db, job_id):
    """
    events.task_id (added alongside the tasks table) is outside
    hash_chain.py's canonical_event_json field set. Passing it to
    add_event must not change the resulting row_hash -- verified here by
    recomputing row_hash from the stored fields exactly as
    verify_audit_chain.py does, with a task_id-bearing event.
    """
    tasks_db.enqueue(job_id, _items(1), kind="import", stage="retrieve", params={})
    task = tasks_db.claim("worker-1")

    status_db.add_event(job_id, mrn="R0", stage="retrieve", event_type="success",
                         details={"imported": True}, task_id=task["task_id"])

    row = _event_row(job_id)
    assert row["task_id"] == task["task_id"]

    recomputed = compute_row_hash(
        row["prev_hash"], row["job_id"], row["mrn"], row["stage"],
        row["event_type"], row["ts"], row["attempt"], row["error_message"], row["details"],
    )
    assert recomputed == row["row_hash"]
