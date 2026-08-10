"""
TasksDB — per-item task queue backing the worker queue
(docs/worker-queue-design.md, backend/worker.py).

Mirrors StatusDB's shape: a plain class, connections borrowed via
get_conn() (backend/src/db.py's shared ThreadedConnectionPool),
RealDictCursor for multi-column reads, no ORM.

`tasks` is mutable current state (queued -> claimed -> running ->
succeeded/failed/cancelled) -- the same mutable-state/immutable-log split
already used elsewhere in this codebase (research_projects vs
project_audit_log). `events` stays the immutable audit log; TasksDB never
writes to it. Callers (backend/worker.py) write events via StatusDB
alongside task state transitions, passing the task_id back so the two rows
can be joined later.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg2.extras import RealDictCursor, Json, execute_values

from backend.src.db import get_conn
from backend.src.common.sse import BatchItem


class TasksDB:
    def enqueue(self, job_id: str, items: list[BatchItem], kind: str, stage: str, params: dict) -> int:
        """
        Bulk-insert one task row per BatchItem, in a single round trip (not
        N inserts) via execute_values. Returns the number of rows inserted.

        `params` is denormalised onto every row so a claim needs no join
        back to `jobs` (e.g. import_level, destination/collection,
        project_id, username for the claim-time ethics re-check).
        """
        if not items:
            return 0
        now = datetime.now(timezone.utc)
        rows = [
            (
                job_id, kind, stage, item.real_id, item.display_id, item.status_mrn,
                item.input_path, Json(item.extra or {}), Json(params or {}), now,
            )
            for item in items
        ]
        with get_conn() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO tasks(job_id, kind, stage, real_id, display_id, status_mrn,
                                   input_path, extra, params, created_at)
                VALUES %s
                """,
                rows,
            )
        return len(items)

    def claim(self, worker_id: str) -> Optional[dict]:
        """
        Atomically claim the highest-priority, oldest queued task whose job
        isn't cancelled. FOR UPDATE SKIP LOCKED means concurrent callers
        (other worker processes) never block on or duplicate-claim the same
        row -- each gets a distinct task, or None if nothing is queued.
        """
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE tasks SET state='claimed', claimed_by=%s, claimed_at=%s
                WHERE task_id = (
                    SELECT task_id FROM tasks
                    WHERE state='queued'
                      AND job_id NOT IN (SELECT job_id FROM jobs WHERE cancelled)
                    ORDER BY priority DESC, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING *
                """,
                (worker_id, datetime.now(timezone.utc)),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def mark_running(self, task_id: int, worker_id: str) -> bool:
        """
        Guarded by `WHERE state='claimed' AND claimed_by=%s`: a task can
        only move claimed -> running once, and only by the worker that
        currently owns the claim -- see the ownership note below. Also
        refreshes `claimed_at` to "now" -- without this, reap_stale_claims
        (below) would judge a long-*running* task's staleness against the
        original claim timestamp rather than when it actually started
        running, and wrongly reap (and double-claim) a task that's simply
        taking a while, not one whose worker died.

        Returns True if the transition was applied, False if the task
        wasn't in 'claimed' state and owned by worker_id (already running,
        terminal, or reaped and possibly reclaimed by someone else) --
        callers should treat False as "don't log a start event for this,"
        since nothing changed.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET state='running', started_at=%s, claimed_at=%s "
                "WHERE task_id=%s AND state='claimed' AND claimed_by=%s",
                (datetime.now(timezone.utc), datetime.now(timezone.utc), task_id, worker_id),
            )
            return cur.rowcount > 0

    def mark_succeeded(self, task_id: int, worker_id: str, details: Optional[dict] = None) -> bool:
        """
        Guarded by `WHERE state IN ('claimed','running') AND claimed_by=%s`.
        The state check alone stops a late/duplicate call from overwriting
        an already-terminal row; the claimed_by check additionally stops a
        *reaped* worker's late write from clobbering a *different* worker's
        result -- reap_stale_claims can requeue (and a second worker can
        reclaim and finish) a task whose original worker is simply slower
        than `stale_seconds`, not dead, so its eventual mark_succeeded/
        mark_failed call must not win against the task's current owner.
        Note this stops the corrupted *write*, not the duplicate *work*:
        the reaped worker's handler(task) call may still complete and
        perform real external I/O a second time -- the existing
        at-least-once tradeoff docs/worker-queue-design.md already accepts
        for crash recovery applies here too. Returns True if applied, False
        otherwise.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET state='succeeded', finished_at=%s, details=%s "
                "WHERE task_id=%s AND state IN ('claimed', 'running') AND claimed_by=%s",
                (datetime.now(timezone.utc), Json(details) if details is not None else None, task_id, worker_id),
            )
            return cur.rowcount > 0

    def mark_failed(self, task_id: int, worker_id: str, error_message: str) -> str:
        """
        Increments `attempts`. While the new attempt count is still under
        `max_attempts`, returns the task to 'queued' (clearing
        claimed_by/claimed_at so it's eligible for another SKIP LOCKED
        claim); otherwise marks it terminally 'failed'. `max_attempts`
        defaults to 1, so by default one failure is always terminal --
        behaviour unchanged from today's un-retried run_batch_job until
        retries are deliberately enabled per job/task kind.

        Guarded by `WHERE state IN ('claimed','running') AND claimed_by=%s`
        -- the same terminal-state AND ownership protection as
        mark_succeeded (see its docstring for why both are needed).

        Returns "requeued" or "failed" if the transition was applied, or
        "unchanged" if the guard blocked it (task was already terminal, or
        no longer owned by this worker) -- callers (backend/worker.py)
        should not log an event in that case.
        """
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE tasks
                SET attempts = attempts + 1,
                    error_message = %(error_message)s,
                    state = CASE WHEN attempts + 1 < max_attempts THEN 'queued' ELSE 'failed' END,
                    claimed_by = CASE WHEN attempts + 1 < max_attempts THEN NULL ELSE claimed_by END,
                    claimed_at = CASE WHEN attempts + 1 < max_attempts THEN NULL ELSE claimed_at END,
                    finished_at = CASE WHEN attempts + 1 < max_attempts THEN NULL ELSE %(now)s END
                WHERE task_id = %(task_id)s AND state IN ('claimed', 'running') AND claimed_by = %(worker_id)s
                RETURNING state
                """,
                {"error_message": error_message, "now": datetime.now(timezone.utc),
                 "task_id": task_id, "worker_id": worker_id},
            )
            row = cur.fetchone()
            if row is None:
                return "unchanged"
            return "requeued" if row["state"] == "queued" else "failed"

    def cancel_task(self, task_id: int, reason: Optional[str] = None) -> bool:
        """
        Single-task cancellation -- e.g. the claim-time ethics-revocation
        path (a project is revoked/expires between enqueue and execution).
        Distinct from cancel_queued below, which is job-level. Guarded by
        `WHERE state IN ('queued','claimed','running')` so a task that
        already finished (succeeded/failed) can't be silently overwritten
        to 'cancelled', discarding the record that it actually completed.
        No claimed_by check here (unlike mark_running/mark_succeeded/
        mark_failed above): this is called immediately after claim(), by
        the same worker that owns the claim, before any handler(task) work
        has started, so there's no other owner it could be racing.
        Returns True if applied, False otherwise.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET state='cancelled', finished_at=%s, error_message=%s "
                "WHERE task_id=%s AND state IN ('queued', 'claimed', 'running')",
                (datetime.now(timezone.utc), reason, task_id),
            )
            return cur.rowcount > 0

    def cancel_queued(self, job_id: str) -> int:
        """
        Job-level cancellation: only rows still 'queued' are cancelled --
        in-flight (claimed/running) tasks are left to finish, matching the
        cancel dialog's existing promise ("in-flight items finish").
        Returns the number of rows cancelled.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET state='cancelled', finished_at=%s WHERE job_id=%s AND state='queued'",
                (datetime.now(timezone.utc), job_id),
            )
            return cur.rowcount

    def reap_stale_claims(self, stale_seconds: int) -> int:
        """
        Recovers tasks from workers that died holding a claim: any
        claimed/running task whose claimed_at predates the threshold goes
        back to 'queued' for another worker to pick up. Returns the number
        of rows reaped.

        claimed_at is refreshed by mark_running (see above) when a task
        moves from 'claimed' to 'running', so this judges staleness from
        "last confirmed alive" rather than the original claim time -- a
        task that's simply taking a while to run isn't wrongly reaped and
        double-claimed just because it started running long ago.
        """
        threshold = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tasks
                SET state='queued', claimed_by=NULL, claimed_at=NULL
                WHERE state IN ('claimed', 'running') AND claimed_at < %s
                """,
                (threshold,),
            )
            return cur.rowcount

    def get_task(self, task_id: int) -> Optional[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM tasks WHERE task_id=%s", (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def count_tasks(self, job_id: str) -> int:
        """Total task count for a job -- the observer stream's initial `start` event's `total`."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tasks WHERE job_id=%s", (job_id,))
            return cur.fetchone()[0]

    def job_progress(self, job_id: str, after_task_id: int = 0) -> list[dict]:
        """
        Tasks for this job with task_id > after_task_id, ordered by
        task_id -- the observer stream's state-transition read. Each poll
        tick is then a small indexed range scan against ix_tasks_job_id,
        not a rescan of the whole job.
        """
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT task_id, state, display_id, details, error_message
                FROM tasks
                WHERE job_id = %s AND task_id > %s
                ORDER BY task_id
                """,
                (job_id, after_task_id),
            )
            return [dict(r) for r in cur.fetchall()]

    def job_has_pending(self, job_id: str) -> bool:
        """Backs the observer's 'done' event: true while any task for this job hasn't reached a terminal state."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tasks WHERE job_id=%s AND state IN ('queued','claimed','running') LIMIT 1",
                (job_id,),
            )
            return cur.fetchone() is not None
