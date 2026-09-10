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

    def list_all(self, limit: int = 100) -> list[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM error_reports ORDER BY created_at DESC LIMIT %s", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
