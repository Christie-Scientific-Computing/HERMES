"""
Status DB client helper for writing job/patient events.

Backed by PostgreSQL (see backend/src/db.py for the shared connection pool).
"""
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import RealDictCursor, Json

from backend.src.db import get_conn
from backend.src.status.hash_chain import compute_row_hash


class StatusDB:
    def create_job(
        self,
        job_id: str,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        project_id: Optional[str] = None,
    ):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs(job_id, created_at, created_by, description, project_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                """,
                (job_id, datetime.now(timezone.utc), created_by, description, project_id),
            )

    def add_patient(self, job_id: str, mrn: str, input_path: Optional[str] = None):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO patients(job_id, mrn, input_path, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (job_id, mrn) DO NOTHING
                """,
                (job_id, mrn, input_path, datetime.now(timezone.utc)),
            )

    def add_event(self, job_id: str, mrn: str, stage: str, event_type: str, error_message: Optional[str] = None, details: Optional[dict] = None, attempt: int = 1, task_id: Optional[int] = None):
        """
        Insert one event and extend the hash chain (docs/safety-plan.md §D1)
        in the same transaction:

        1. `SELECT ... FOR UPDATE` the singleton `event_chain_state` row --
           the row lock serializes concurrent writers (multiple uvicorn
           workers, or multiple batch jobs running at once) so the chain
           always has one, and only one, valid next link.
        2. Compute row_hash = sha256(prev_hash || canonical_json(...)) via
           backend/src/status/hash_chain.py, shared with
           backend/scripts/verify_audit_chain.py so the two can never
           compute the hash two different ways.
        3. Insert the event with both prev_hash and row_hash set.
        4. Advance event_chain_state.last_hash to this row's row_hash.

        `get_conn()` wraps all of this in one commit/rollback unit, so the
        lock is held for the whole read-compute-insert-update sequence and
        released atomically.

        `task_id` (optional, added for the worker queue -- see
        backend/src/status/tasks_db.py) links this event back to the task
        row that produced it. It plays no part in the hash chain --
        hash_chain.py's canonical_event_json hashes a fixed field set that
        never included it, by design, so passing it here cannot change any
        previously- or subsequently-computed row_hash.
        """
        ts = datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT last_hash FROM event_chain_state WHERE id = 1 FOR UPDATE")
            row = cur.fetchone()
            prev_hash = row[0]

            row_hash = compute_row_hash(prev_hash, job_id, mrn, stage, event_type, ts, attempt, error_message, details)

            cur.execute(
                """
                INSERT INTO events(job_id, mrn, stage, event_type, ts, attempt, error_message, details, prev_hash, row_hash, task_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (job_id, mrn, stage, event_type, ts, attempt, error_message,
                 Json(details) if details is not None else None, prev_hash, row_hash, task_id),
            )
            cur.execute("UPDATE event_chain_state SET last_hash = %s WHERE id = 1", (row_hash,))

    def get_patient_history(self, job_id: str, mrn: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM events WHERE job_id=%s AND mrn=%s ORDER BY ts", (job_id, mrn))
            return [dict(r) for r in cur.fetchall()]

    def get_patient_history_all_jobs(self, mrn: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM events WHERE mrn=%s ORDER BY ts", (mrn,))
            return [dict(r) for r in cur.fetchall()]

    def list_job_patients(self, job_id: str) -> list[str]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT mrn FROM events WHERE job_id=%s ORDER BY mrn", (job_id,))
            return [r[0] for r in cur.fetchall()]

    def summarize_job(self, job_id: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT stage, event_type, COUNT(*) as cnt FROM events WHERE job_id=%s GROUP BY stage, event_type",
                (job_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def cancel_job(self, job_id: str):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET cancelled = TRUE, cancelled_at = %s WHERE job_id = %s",
                (datetime.now(timezone.utc), job_id),
            )

    def is_cancelled(self, job_id: str) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT cancelled FROM jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return bool(row[0]) if row else False

    def get_job(self, job_id: str) -> Optional[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_latest_retrieve_details(self, job_id: str) -> dict[str, dict]:
        """
        Most recent retrieve-stage success `details` per patient in a job --
        e.g. {"in_mosaiq": bool, "in_pinnacle": bool, "in_proknow": bool, "status": ...}
        as returned by Importer.handle_patient. Only patients with at least
        one successful retrieve event appear; a patient with only failures
        (or an export-only job) simply isn't in the returned dict.
        """
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (mrn) mrn, details
                FROM events
                WHERE job_id = %s AND stage = 'retrieve' AND event_type = 'success'
                ORDER BY mrn, ts DESC
                """,
                (job_id,),
            )
            return {row["mrn"]: (row["details"] or {}) for row in cur.fetchall()}

    def get_latest_event_per_patient(self, job_id: str, stage: Optional[str] = None) -> dict[str, dict]:
        """
        Most recent event per patient in a job, whatever its type -- or, when
        `stage` is given, the most recent event of that stage specifically.

        Deliberately broader than get_latest_retrieve_details, which only looks
        at successful retrieve events and therefore cannot see a patient that
        only ever failed -- exactly the patient someone debugging is looking
        for. Use this for a patient's outcome and latest error; use the other
        for source-system presence.

        The optional `stage` filter backs a combined import->export job's
        per-stage outcome (job_patients_summary's import_outcome/export_outcome):
        without it, a patient whose export ran after a successful import would
        only ever show the export's outcome, silently losing the import result
        the same query would otherwise report for an import-only job.

        `id DESC` breaks ties: two events written in the same transaction can
        share a `ts`, and without it the "latest" would be arbitrary.
        """
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            if stage is not None:
                cur.execute(
                    """
                    SELECT DISTINCT ON (mrn) mrn, stage, event_type, ts, error_message
                    FROM events
                    WHERE job_id = %s AND stage = %s
                    ORDER BY mrn, ts DESC, id DESC
                    """,
                    (job_id, stage),
                )
            else:
                cur.execute(
                    """
                    SELECT DISTINCT ON (mrn) mrn, stage, event_type, ts, error_message
                    FROM events
                    WHERE job_id = %s
                    ORDER BY mrn, ts DESC, id DESC
                    """,
                    (job_id,),
                )
            return {row["mrn"]: dict(row) for row in cur.fetchall()}

    def count_imported_patients(self, job_id: str) -> tuple[int, int]:
        """
        The "N/M imported" headline figure for a job: (imported_count,
        submitted_count).

        `imported_count` is the count of distinct patients with a
        retrieve-stage success event whose `details->>'imported'` is the
        string 'true' -- Importer.verify_on_orthanc's own ground-truth check
        (backend/src/retrieve/logic.py, §D3), not a guess from `event_type`
        alone: a patient found nowhere still gets `event_type = 'success'`
        (the operation ran without raising), so counting those would
        overstate how many patients actually got data.

        `submitted_count` is simply every distinct patient submitted as part
        of this job (COUNT(DISTINCT mrn) from `patients`), regardless of
        outcome -- the "M" half of "N/M imported".
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT mrn) FROM events
                WHERE job_id = %s AND stage = 'retrieve' AND event_type = 'success'
                  AND details ->> 'imported' = 'true'
                """,
                (job_id,),
            )
            imported_count = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(DISTINCT mrn) FROM patients WHERE job_id = %s",
                (job_id,),
            )
            submitted_count = cur.fetchone()[0]

            return imported_count, submitted_count

    def count_exported_patients(self, job_id: str) -> tuple[int, int]:
        """
        The export-side counterpart to count_imported_patients: (exported_count,
        export_attempted_count).

        Unlike submitted_count above (fixed at job submission, from the
        `patients` table), export_attempted_count counts distinct patients
        with ANY export-stage event -- for a combined import->export job,
        export tasks are chained in one at a time as imports succeed
        (backend/worker.py's _maybe_chain_export), so this denominator grows
        over the job's lifetime rather than being knowable upfront. For a
        plain export-only job it simply converges to the same number
        submitted_count would report once every task has run at least once.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT mrn) FROM events "
                "WHERE job_id = %s AND stage = 'export' AND event_type = 'success'",
                (job_id,),
            )
            exported_count = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(DISTINCT mrn) FROM events WHERE job_id = %s AND stage = 'export'",
                (job_id,),
            )
            export_attempted_count = cur.fetchone()[0]

            return exported_count, export_attempted_count
