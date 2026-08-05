"""
Status DB client helper for writing job/patient events.

Backed by PostgreSQL (see backend/src/db.py for the shared connection pool).
"""
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import RealDictCursor, Json

from backend.src.db import get_conn


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

    def add_event(self, job_id: str, mrn: str, stage: str, event_type: str, error_message: Optional[str] = None, details: Optional[dict] = None, attempt: int = 1):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events(job_id, mrn, stage, event_type, ts, attempt, error_message, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (job_id, mrn, stage, event_type, datetime.now(timezone.utc), attempt, error_message,
                 Json(details) if details is not None else None),
            )

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

    def get_latest_event_per_patient(self, job_id: str) -> dict[str, dict]:
        """
        Most recent event per patient in a job, whatever its stage or type.

        Deliberately broader than get_latest_retrieve_details, which only looks
        at successful retrieve events and therefore cannot see a patient that
        only ever failed -- exactly the patient someone debugging is looking
        for. Use this for a patient's outcome and latest error; use the other
        for source-system presence.

        `id DESC` breaks ties: two events written in the same transaction can
        share a `ts`, and without it the "latest" would be arbitrary.
        """
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
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
