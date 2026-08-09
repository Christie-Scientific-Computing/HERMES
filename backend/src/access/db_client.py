"""
AccessDB client — per-user export destination allow-list
(docs/safety-plan.md §A). Backed by PostgreSQL (see backend/src/db.py for
the shared pool), same HermesDB as ProjectsDB/StatusDB.

This is independent of project membership: `require_project_member`
(backend/src/projects/enforcement.py) governs *whether* a user may export
at all; this governs *where* -- an admin can additionally restrict a
specific user to a subset of Orthanc modalities / ProKnow collections.

Usernames are plain TEXT here, not a foreign key to a user table -- same
reasoning as `project_memberships.username` (backend/src/projects/db_client.py):
Django is the sole source of truth for user identity.
"""
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from backend.src.db import get_conn


class AccessDB:
    def list_for_user(self, username: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM user_export_destinations WHERE username = %s ORDER BY added_at",
                (username,),
            )
            return [dict(r) for r in cur.fetchall()]

    def add(self, username: str, destination_type: str, destination: str, added_by: str) -> None:
        now = datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_export_destinations
                    (username, destination_type, destination, added_by, added_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (username, destination_type, destination) DO NOTHING
                """,
                (username, destination_type, destination, added_by, now),
            )

    def remove(self, username: str, id: int) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_export_destinations WHERE username = %s AND id = %s",
                (username, id),
            )

    def is_allowed(self, username: str, destination_type: str, destination: str) -> bool:
        """
        Opt-in allow-list, not fail-closed by default: a user with zero rows
        has no restriction configured (today's behavior, unchanged) and may
        export anywhere. Once a user has >=1 row, only the destinations
        explicitly listed are allowed. This is a deliberate design choice
        from docs/safety-plan.md §A, matching this codebase's existing idiom
        for optional hardening (ANON_DB_HOST unset -> passthrough,
        HERMES_INTERNAL_KEY unset -> no-op) rather than introducing a new,
        fail-closed-by-default pattern here.

        Known limitation, stated explicitly (per the plan): this means
        "not yet restricted" and "deliberately left open" are
        indistinguishable at the database level. Not solved here.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM user_export_destinations WHERE username = %s LIMIT 1",
                (username,),
            )
            has_any_restriction = cur.fetchone() is not None
            if not has_any_restriction:
                return True
            cur.execute(
                """
                SELECT 1 FROM user_export_destinations
                WHERE username = %s AND destination_type = %s AND destination = %s
                """,
                (username, destination_type, destination),
            )
            return cur.fetchone() is not None
