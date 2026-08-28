"""
ProjectsDB client — ethics/research project lifecycle, membership, and audit
trail. Backed by PostgreSQL (see backend/src/db.py for the shared pool),
same HermesDB as StatusDB (backend/src/status/db_client.py).

This is the coarse-gate enforcement data source: a user may run an
import/export only while they are an active member of a project whose
status is "approved" and whose expiry_date (if any) hasn't passed. See
backend/src/projects/enforcement.py for the actual gate.

Usernames are plain TEXT here, not a foreign key to a user table -- there
is no user table in HermesDB. Django (the sole caller) is the source of
truth for user identity; renaming a Django username would silently orphan
memberships/audit rows referencing the old name. Accepted limitation for
v1 -- don't build username-edit UI on the frontend without revisiting this.
"""
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import RealDictCursor, Json

from backend.src.db import get_conn


class ProjectNotFoundError(Exception):
    """Raised when a project_id has no matching row."""


class ProjectsDB:
    # ---- Project lifecycle ----

    def create_project(
        self,
        project_id: str,
        title: str,
        created_by: str,
        description: Optional[str] = None,
        ethics_reference: Optional[str] = None,
    ) -> None:
        """Create a project in `draft` status and add its creator as owner."""
        now = datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_projects
                    (project_id, title, description, ethics_reference, status, created_by, created_at)
                VALUES (%s, %s, %s, %s, 'draft', %s, %s)
                ON CONFLICT (project_id) DO NOTHING
                """,
                (project_id, title, description, ethics_reference, created_by, now),
            )
            cur.execute(
                """
                INSERT INTO project_memberships (project_id, username, role, added_at)
                VALUES (%s, %s, 'owner', %s)
                ON CONFLICT (project_id, username) DO NOTHING
                """,
                (project_id, created_by, now),
            )
        self.add_audit_entry(project_id, created_by, "created", {"title": title})

    def submit_project(self, project_id: str, username: str) -> None:
        """Transition draft -> submitted, ready for admin review."""
        now = datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE research_projects
                SET status = 'submitted', submitted_at = %s
                WHERE project_id = %s AND status = 'draft'
                """,
                (now, project_id),
            )
            if cur.rowcount == 0:
                raise ProjectNotFoundError(f"No draft project {project_id!r} to submit")
        self.add_audit_entry(project_id, username, "submitted")

    def review_project(
        self,
        project_id: str,
        approved: bool,
        reviewer: str,
        comment: Optional[str] = None,
        expiry_date: Optional[datetime] = None,
    ) -> None:
        """Approve or reject a submitted project. `expiry_date` only applies on approval."""
        now = datetime.now(timezone.utc)
        new_status = "approved" if approved else "rejected"
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE research_projects
                SET status = %s, reviewed_by = %s, review_comment = %s,
                    approved_at = CASE WHEN %s THEN %s ELSE approved_at END,
                    expiry_date = CASE WHEN %s THEN %s ELSE expiry_date END
                WHERE project_id = %s AND status = 'submitted'
                """,
                (new_status, reviewer, comment, approved, now, approved, expiry_date, project_id),
            )
            if cur.rowcount == 0:
                raise ProjectNotFoundError(f"No submitted project {project_id!r} to review")
        self.add_audit_entry(
            project_id, reviewer, "approved" if approved else "rejected",
            {"comment": comment, "expiry_date": expiry_date.isoformat() if expiry_date else None},
        )

    def revoke_project(self, project_id: str, revoked_by: str, comment: Optional[str] = None) -> None:
        """Admin early-revocation of a previously-approved project."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE research_projects SET status = 'revoked' WHERE project_id = %s AND status = 'approved'",
                (project_id,),
            )
            if cur.rowcount == 0:
                raise ProjectNotFoundError(f"No approved project {project_id!r} to revoke")
        self.add_audit_entry(project_id, revoked_by, "revoked", {"comment": comment})

    def get_project(self, project_id: str) -> dict:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM research_projects WHERE project_id = %s", (project_id,))
            row = cur.fetchone()
            if row is None:
                raise ProjectNotFoundError(f"No project {project_id!r}")
            return dict(row)

    def list_projects(self, username: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        """List projects, optionally filtered to ones `username` is a member of and/or a status."""
        query = "SELECT DISTINCT p.* FROM research_projects p"
        params: list = []
        conditions = []
        if username is not None:
            query += " JOIN project_memberships m ON m.project_id = p.project_id"
            conditions.append("m.username = %s")
            params.append(username)
        if status is not None:
            conditions.append("p.status = %s")
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY p.created_at DESC"
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    # ---- Membership ----

    def add_member(self, project_id: str, username: str, role: str = "member", added_by: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO project_memberships (project_id, username, role, added_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (project_id, username) DO NOTHING
                """,
                (project_id, username, role, now),
            )
        self.add_audit_entry(project_id, added_by or username, "member_added", {"username": username, "role": role})

    def remove_member(self, project_id: str, username: str, removed_by: str) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM project_memberships WHERE project_id = %s AND username = %s",
                (project_id, username),
            )
        self.add_audit_entry(project_id, removed_by, "member_removed", {"username": username})

    def list_members(self, project_id: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM project_memberships WHERE project_id = %s ORDER BY added_at", (project_id,)
            )
            return [dict(r) for r in cur.fetchall()]

    def is_member(self, project_id: str, username: str) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM project_memberships WHERE project_id = %s AND username = %s",
                (project_id, username),
            )
            return cur.fetchone() is not None

    # ---- Enforcement-facing queries ----

    def is_project_active(self, project_id: str) -> bool:
        """True iff the project is approved and not expired."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM research_projects
                WHERE project_id = %s AND status = 'approved'
                  AND (expiry_date IS NULL OR expiry_date > now())
                """,
                (project_id,),
            )
            return cur.fetchone() is not None

    def is_active_member(self, project_id: str, username: str) -> bool:
        """True iff `username` belongs to `project_id` and that project is currently active."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM project_memberships m
                JOIN research_projects p ON p.project_id = m.project_id
                WHERE m.project_id = %s AND m.username = %s AND p.status = 'approved'
                  AND (p.expiry_date IS NULL OR p.expiry_date > now())
                """,
                (project_id, username),
            )
            return cur.fetchone() is not None

    def has_any_active_project(self, username: str) -> bool:
        """True iff `username` is a member of at least one active (approved, non-expired) project."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM project_memberships m
                JOIN research_projects p ON p.project_id = m.project_id
                WHERE m.username = %s AND p.status = 'approved'
                  AND (p.expiry_date IS NULL OR p.expiry_date > now())
                LIMIT 1
                """,
                (username,),
            )
            return cur.fetchone() is not None

    def list_user_active_projects(self, username: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT p.* FROM research_projects p
                JOIN project_memberships m ON m.project_id = p.project_id
                WHERE m.username = %s AND p.status = 'approved'
                  AND (p.expiry_date IS NULL OR p.expiry_date > now())
                ORDER BY p.created_at DESC
                """,
                (username,),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_expiring_projects(self, within_days: int = 30) -> list[dict]:
        """
        Every approved project (across ALL members, not scoped to one user)
        whose expiry_date falls within the next `within_days` days --
        backs the admin dashboard's project-wide expiring-soon list
        (Phase 4). Deliberately a different query from
        list_user_active_projects above: that one is scoped to a single
        user's own memberships (the nav banner / per-user notification use
        case), this one is project-wide (the administrative overview).
        A project with no expiry_date (open-ended approval) never
        qualifies -- there's nothing to warn about.
        """
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM research_projects
                WHERE status = 'approved'
                  AND expiry_date IS NOT NULL
                  AND expiry_date BETWEEN now() AND now() + make_interval(days => %s)
                ORDER BY expiry_date
                """,
                (within_days,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ---- Jobs (traceability) ----

    def list_project_jobs(self, project_id: str) -> list[dict]:
        """Jobs created under this project (jobs.project_id, wired via run_batch_job/single_import)."""
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM jobs WHERE project_id = %s ORDER BY created_at DESC", (project_id,)
            )
            return [dict(r) for r in cur.fetchall()]

    # ---- Audit trail ----

    def add_audit_entry(self, project_id: str, username: str, action: str, details: Optional[dict] = None) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO project_audit_log (project_id, username, action, ts, details)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (project_id, username, action, datetime.now(timezone.utc), Json(details) if details is not None else None),
            )

    def list_audit_log(self, project_id: str) -> list[dict]:
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM project_audit_log WHERE project_id = %s ORDER BY ts", (project_id,)
            )
            return [dict(r) for r in cur.fetchall()]
