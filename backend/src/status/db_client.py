"""
Status DB client helper for writing job/patient events.
"""
import sqlite3
import json
from datetime import datetime
from typing import Optional


class StatusDB:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # improve concurrency for small-scale usage
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def create_job(self, job_id: str, description: Optional[str] = None, created_by: Optional[str] = None):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO jobs(job_id, created_at, created_by, description) VALUES(?, ?, ?, ?)",
            (job_id, datetime.now(datetime.timezone.utc).isoformat(), created_by, description),
        )
        conn.commit()
        conn.close()

    def add_patient(self, job_id: str, mrn: str, input_path: Optional[str] = None):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO patients(job_id, mrn, input_path, created_at) VALUES(?, ?, ?, ?)",
            (job_id, mrn, input_path, datetime.now(datetime.timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

    def add_event(self, job_id: str, mrn: str, stage: str, event_type: str, error_message: Optional[str] = None, details: Optional[dict] = None, attempt: int = 1):
        conn = self._get_conn()
        cur = conn.cursor()
        details_json = json.dumps(details) if details is not None else None
        cur.execute(
            "INSERT INTO events(job_id, mrn, stage, event_type, ts, attempt, error_message, details) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, mrn, stage, event_type, datetime.now(datetime.timezone.utc).isoformat(), attempt, error_message, details_json),
        )
        conn.commit()
        conn.close()

    def get_patient_history(self, job_id: str, mrn: str):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM events WHERE job_id=? AND mrn=? ORDER BY ts", (job_id, mrn))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def summarize_job(self, job_id: str):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT stage, event_type, COUNT(*) as cnt FROM events WHERE job_id=? GROUP BY stage, event_type", (job_id,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
