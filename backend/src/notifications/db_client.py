"""
NotificationsDB — persisted per-user notifications (Phase 4,
docs/plans/frontend-rewrite-implementation-plan.md §6). Same shape as
ProjectsDB/StatusDB: a plain class, connections borrowed via get_conn(),
RealDictCursor, no ORM.

`username` is plain TEXT, same accepted limitation as ProjectsDB's own
membership/audit rows -- there is no user table in HermesDB, and
Django/frontend_fastapi (whichever is the caller) is the sole source of
truth for user identity. Two population sources today: backend/worker.py
(job completion, via the race-safe StatusDB.mark_job_notified marker) and
backend/src/projects/endpoints.py's review_project (an approval/rejection
decision, notifying every current member).
"""
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import RealDictCursor

from backend.src.db import get_conn


class NotificationsDB:
    def create(
        self, username: str, kind: str, message: str,
        job_id: Optional[str] = None, project_id: Optional[str] = None,
    ) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notifications(username, kind, message, job_id, project_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (username, kind, message, job_id, project_id, datetime.now(timezone.utc)),
            )

    def list_for_user(self, username: str, unread_only: bool = False, limit: int = 20) -> list[dict]:
        query = "SELECT * FROM notifications WHERE username = %s"
        params: list = [username]
        if unread_only:
            query += " AND read_at IS NULL"
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def mark_read(self, notification_id: int, username: str) -> bool:
        """Scoped to `username` in the WHERE clause -- not just the id --
        so one user can never mark another user's notification read (or
        even discover, via a differing response, whether a given id belongs
        to someone else). Returns True iff a row was actually updated."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE notifications SET read_at = %s WHERE id = %s AND username = %s AND read_at IS NULL",
                (datetime.now(timezone.utc), notification_id, username),
            )
            return cur.rowcount > 0
