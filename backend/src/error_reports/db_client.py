"""
ErrorReportsDB — user-submitted feedback/error reports (item 06,
docs/plans/feature-round-implementation-plan.md). Same shape as
NotificationsDB/ProjectsDB: a plain class, connections borrowed via
get_conn(), RealDictCursor, no ORM.

`username` is plain TEXT, same accepted limitation as every other
username-bearing table in HermesDB -- there is no user table here;
frontend_fastapi is the sole source of truth for user identity and attaches
it itself (never a browser-supplied value), same as everywhere else.
"""
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import RealDictCursor

from backend.src.db import get_conn


class ErrorReportsDB:
    def create(
        self, username: str, category: str, message: str,
        urgent: bool = False, job_id: Optional[str] = None,
    ) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO error_reports(username, category, urgent, message, job_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (username, category, urgent, message, job_id, datetime.now(timezone.utc)),
            )

    def list_all(self, limit: int = 100, unaddressed_only: bool = False) -> list[dict]:
        query = "SELECT * FROM error_reports"
        if unaddressed_only:
            query += " WHERE resolved_at IS NULL"
        query += " ORDER BY created_at DESC LIMIT %s"
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def mark_addressed(self, report_id: int, username: str) -> bool:
        """Mirrors NotificationsDB.mark_read's idempotency shape (the
        `resolved_at IS NULL` guard makes a second call a no-op returning
        False) -- but scoped to `report_id` alone, not also `username`: any
        staff user may address any report (there's no "owner" of a report
        the way a notification belongs to its recipient)."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE error_reports SET resolved_at = %s, resolved_by = %s WHERE id = %s AND resolved_at IS NULL",
                (datetime.now(timezone.utc), username, report_id),
            )
            return cur.rowcount > 0
