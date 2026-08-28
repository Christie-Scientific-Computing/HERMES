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
    def enqueue(self, job_id: str, items: list[BatchItem], kind: str, stage: str, params: dict,
                chained_from_task_id: Optional[int] = None) -> int:
        """
        Bulk-insert one task row per BatchItem, in a single round trip (not
        N inserts) via execute_values. Returns the number of items submitted
        (not necessarily the number of rows actually inserted -- see
        chained_from_task_id below).

        `params` is denormalised onto every row so a claim needs no join
        back to `jobs` (e.g. import_level, destination/collection,
        project_id, username for the claim-time ethics re-check).

        `chained_from_task_id` marks a task as enqueued by
        backend/worker.py's _maybe_chain_export (the import task_id it was
        chained from) rather than submitted directly by a batch endpoint --
        NULL for every ordinary import/export/uid-move submission. The
        migration adding this column also adds a partial unique index on
        (job_id, kind, status_mrn) WHERE chained_from_task_id IS NOT NULL,
        so ON CONFLICT DO NOTHING here makes a second chain attempt for the
        same import a no-op at the database level -- closing a real race
        where a task reaped from a slow worker and reclaimed by another
        would otherwise get its export chained twice (two independent
        _maybe_chain_export calls, each completing the same import
        successfully). Never triggers for a plain call (chained_from_task_id
        None on every row), so ordinary batch submissions are unaffected.
        """
        if not items:
            return 0
        now = datetime.now(timezone.utc)
        rows = [
            (
                job_id, kind, stage, item.real_id, item.display_id, item.status_mrn,
                item.input_path, Json(item.extra or {}), Json(params or {}), now, chained_from_task_id,
            )
            for item in items
        ]
        with get_conn() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO tasks(job_id, kind, stage, real_id, display_id, status_mrn,
                                   input_path, extra, params, created_at, chained_from_task_id)
                VALUES %s
                ON CONFLICT (job_id, kind, status_mrn) WHERE chained_from_task_id IS NOT NULL DO NOTHING
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

        `reason` is more than a log message: results/endpoints.py's
        _observe_job uses whether error_message is set to tell this
        (attempted-then-denied) cancellation apart from cancel_queued's
        bulk cancellation of never-run queued tasks, reporting the former
        as an "error" event and silently skipping the latter. Always pass
        a `reason` here, or a genuinely-denied task will be silently
        dropped from the observer stream instead of reported.
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

    def job_has_chain_export(self, job_id: str) -> bool:
        """
        Whether any task in this job was submitted with a chain_export block
        (backend/src/retrieve/endpoints.py's batch_import_file, export_kind
        param) -- i.e. whether this is a combined import->export job.

        Checked on `params` (set at enqueue time, before anything runs)
        rather than chained_from_task_id (only set once a chained export
        task actually exists) so this is accurate from the moment a
        combined job is submitted, not only once its first import succeeds
        -- frontend/jobs/views.py's job_watch needs this immediately, to
        pick the two-stage progress component before any progress exists.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM tasks WHERE job_id = %s AND params ? 'chain_export')",
                (job_id,),
            )
            return bool(cur.fetchone()[0])

    def job_is_complete(self, job_id: str) -> bool:
        """
        True iff every task belonging to this job has reached a terminal
        state (succeeded/failed/cancelled) -- i.e. nothing is queued,
        claimed, or running. Backs the job-completion notification hook
        (backend/worker.py, Phase 4): must be re-checked after EVERY
        terminal task write, not decided once, since a combined
        import->export job's task set can still be growing
        (_maybe_chain_export enqueues export tasks one at a time as imports
        succeed) -- "no pending tasks right now" can flip back to "pending"
        the moment the next chained export is enqueued.

        A job with zero tasks at all (e.g. an empty CSV) is trivially
        "complete" -- there's nothing left to wait for.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT NOT EXISTS(SELECT 1 FROM tasks WHERE job_id = %s AND state IN ('queued', 'claimed', 'running'))",
                (job_id,),
            )
            return bool(cur.fetchone()[0])

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

    def job_progress(self, job_id: str) -> list[dict]:
        """
        Every task for this job, current state included -- the observer
        stream's read (backend/src/results/endpoints.py).

        Deliberately not filtered by a task_id watermark: task rows are
        mutated in place as they progress (claimed -> running ->
        succeeded/failed), not appended as a new row per transition, so a
        "only task_id greater than X" filter can only ever report a task
        once, at whatever state it happened to be in on its first
        appearance -- it would silently miss every later transition
        (e.g. running -> succeeded) once that task_id has already been
        returned. (An earlier version of this method had exactly that bug;
        caught before it had any real caller.) The observer instead
        re-reads every task each poll tick and diffs against its own
        in-memory last-seen-state map to decide what to emit -- correct,
        and cheap enough at this domain's scale (a batch job's task count
        is a patient-list CSV, not a large table).
        """
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT task_id, state, real_id, display_id, details, error_message, stage, kind "
                "FROM tasks WHERE job_id = %s ORDER BY task_id",
                (job_id,),
            )
            return [dict(r) for r in cur.fetchall()]

