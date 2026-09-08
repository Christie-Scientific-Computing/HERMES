"""
Tests for backend/scripts/check_worker_health.py and the StatusDB method it
relies on (list_completed_jobs_missing_notification) -- Phase 5 cutover's
"confirm, don't just merge" check that worker.py's periodic hooks are
actually running post-deploy.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.scripts.check_worker_health import check_audit_chain, check_notifications
from backend.src.common.sse import BatchItem
from backend.src.db import get_conn
from backend.src.status.audit_chain_db import AuditChainDB
from backend.src.status.db_client import StatusDB
from backend.src.status.tasks_db import TasksDB


@pytest.fixture
def status_db():
    return StatusDB()


@pytest.fixture
def tasks_db():
    return TasksDB()


def _backdate_job(job_id: str, created_at: datetime) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE jobs SET created_at = %s WHERE job_id = %s", (created_at, job_id))


def _backdate_task_finish(task_id: int, finished_at: datetime) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE tasks SET finished_at = %s WHERE task_id = %s", (finished_at, task_id))


def _make_finished_job(status_db: StatusDB, tasks_db: TasksDB, *, age_minutes: int) -> str:
    """A job with one task, taken all the way to 'succeeded', whose task
    actually FINISHED `age_minutes` ago -- the shape
    list_completed_jobs_missing_notification is meant to find
    (completed_notified_at is never set here). Backdates the job's
    created_at too, matching a real job of that age, but it's the task's
    finished_at backdating that the query itself keys on -- see that
    method's own docstring for why created_at alone isn't the right signal."""
    job_id = f"health-check-test-{uuid.uuid4()}"
    status_db.create_job(job_id, description="health check test")
    finished_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    _backdate_job(job_id, finished_at)

    item = BatchItem(real_id="MRN1", display_id="MRN1", status_mrn="MRN1", input_path=None)
    tasks_db.enqueue(job_id, [item], kind="import", stage="retrieve", params={})
    task = tasks_db.claim(worker_id="test-worker")
    tasks_db.mark_running(task["task_id"], worker_id="test-worker")
    tasks_db.mark_succeeded(task["task_id"], worker_id="test-worker")
    _backdate_task_finish(task["task_id"], finished_at)
    return job_id


class TestListCompletedJobsMissingNotification:
    def test_finds_a_finished_job_past_the_grace_window_with_no_notification(self, status_db, tasks_db):
        job_id = _make_finished_job(status_db, tasks_db, age_minutes=60)

        stuck = status_db.list_completed_jobs_missing_notification(older_than_minutes=30)

        assert job_id in [row["job_id"] for row in stuck]

    def test_does_not_flag_a_job_still_inside_the_grace_window(self, status_db, tasks_db):
        job_id = _make_finished_job(status_db, tasks_db, age_minutes=5)

        stuck = status_db.list_completed_jobs_missing_notification(older_than_minutes=30)

        assert job_id not in [row["job_id"] for row in stuck]

    def test_does_not_flag_a_long_running_job_that_just_finished(self, status_db, tasks_db):
        """A job CREATED well outside the grace window but whose task only
        just FINISHED (a large batch that took a while) must not be flagged
        -- the query keys on when the last task actually finished, not on
        how old the job itself is. This is the exact scenario a
        created_at-based check would wrongly flag: a legitimately
        long-running job, penalized the instant it completes, even though
        the notification hook hasn't had a chance to run yet."""
        job_id = f"health-check-test-{uuid.uuid4()}"
        status_db.create_job(job_id, description="long-running job")
        _backdate_job(job_id, datetime.now(timezone.utc) - timedelta(hours=2))
        item = BatchItem(real_id="MRN1", display_id="MRN1", status_mrn="MRN1", input_path=None)
        tasks_db.enqueue(job_id, [item], kind="import", stage="retrieve", params={})
        task = tasks_db.claim(worker_id="test-worker")
        tasks_db.mark_running(task["task_id"], worker_id="test-worker")
        tasks_db.mark_succeeded(task["task_id"], worker_id="test-worker")  # finished_at = now, NOT backdated

        stuck = status_db.list_completed_jobs_missing_notification(older_than_minutes=30)

        assert job_id not in [row["job_id"] for row in stuck]

    def test_does_not_flag_a_job_that_was_actually_notified(self, status_db, tasks_db):
        job_id = _make_finished_job(status_db, tasks_db, age_minutes=60)
        assert status_db.mark_job_notified(job_id) is True

        stuck = status_db.list_completed_jobs_missing_notification(older_than_minutes=30)

        assert job_id not in [row["job_id"] for row in stuck]

    def test_does_not_flag_a_job_still_in_progress(self, status_db, tasks_db):
        job_id = f"health-check-test-{uuid.uuid4()}"
        status_db.create_job(job_id, description="still running")
        _backdate_job(job_id, datetime.now(timezone.utc) - timedelta(minutes=60))
        item = BatchItem(real_id="MRN1", display_id="MRN1", status_mrn="MRN1", input_path=None)
        tasks_db.enqueue(job_id, [item], kind="import", stage="retrieve", params={})
        # Left in 'queued' -- not yet terminal.

        stuck = status_db.list_completed_jobs_missing_notification(older_than_minutes=30)

        assert job_id not in [row["job_id"] for row in stuck]

    def test_does_not_flag_a_job_with_no_tasks_at_all(self, status_db):
        job_id = f"health-check-test-{uuid.uuid4()}"
        status_db.create_job(job_id, description="no tasks enqueued yet")
        _backdate_job(job_id, datetime.now(timezone.utc) - timedelta(minutes=60))

        stuck = status_db.list_completed_jobs_missing_notification(older_than_minutes=30)

        assert job_id not in [row["job_id"] for row in stuck]


class TestCheckAuditChain:
    def test_healthy_recent_check_passes(self):
        db = AuditChainDB()
        db.record_check(ok=True)

        ok, msg = check_audit_chain(db, max_staleness_seconds=3600)

        assert ok is True
        assert "OK" in msg

    def test_stale_check_fails(self):
        db = AuditChainDB()
        db.record_check(ok=True)

        ok, msg = check_audit_chain(db, max_staleness_seconds=-1)

        assert ok is False
        assert "last checked" in msg

    def test_tampered_check_fails_even_if_recent(self):
        db = AuditChainDB()
        db.record_check(ok=False, reason="row_hash mismatch")

        ok, msg = check_audit_chain(db, max_staleness_seconds=3600)

        assert ok is False
        assert "TAMPERED" in msg

    def test_no_check_yet_is_reported_but_not_a_failure(self):
        class _EmptyAuditChainDB:
            def latest_check(self):
                return None

        ok, msg = check_audit_chain(_EmptyAuditChainDB(), max_staleness_seconds=3600)

        assert ok is True
        assert "never checked" in msg


class TestCheckNotifications:
    def test_passes_when_nothing_is_stuck(self, status_db, tasks_db):
        job_id = _make_finished_job(status_db, tasks_db, age_minutes=5)
        assert status_db.mark_job_notified(job_id) is True

        ok, msg = check_notifications(status_db, older_than_minutes=30)

        assert ok is True
        assert job_id not in msg

    def test_fails_and_names_the_stuck_job(self, status_db, tasks_db):
        job_id = _make_finished_job(status_db, tasks_db, age_minutes=60)

        ok, msg = check_notifications(status_db, older_than_minutes=30)

        assert ok is False
        assert job_id in msg
