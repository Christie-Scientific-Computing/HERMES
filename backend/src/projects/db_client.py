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
        destinations: Optional[list[dict]] = None,
        message_id: Optional[int] = None,
    ) -> None:
        """
        Create a project in `draft` status and add its creator as owner.

        `destinations` (each `{"destination_type": "dicom"|"proknow", "destination_value": str}`)
        and `message_id` are the creator's REQUESTED values -- written
        straight in as `status='active'` rows / `research_projects.message_id`
        rather than through the propose/approve amendment dance below,
        because nothing reads either one until the project itself reaches
        `approved` (backend/src/retrieve/endpoints.py's batch_import_file
        only looks up message_id for an approved, active member's project;
        an export destination is meaningless before then either). The
        amendment flow exists specifically for changing an ALREADY-approved
        project without interrupting its current settings mid-review.
        """
        now = datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_projects
                    (project_id, title, description, ethics_reference, status, created_by, created_at, message_id)
                VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s)
                ON CONFLICT (project_id) DO NOTHING
                """,
                (project_id, title, description, ethics_reference, created_by, now, message_id),
            )
            cur.execute(
                """
                INSERT INTO project_memberships (project_id, username, role, added_at)
                VALUES (%s, %s, 'owner', %s)
                ON CONFLICT (project_id, username) DO NOTHING
                """,
                (project_id, created_by, now),
            )
            self._insert_destinations(project_id, destinations or [], status="active", cur=cur)
        self.add_audit_entry(project_id, created_by, "created", {"title": title, "message_id": message_id})

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

    # ---- Destinations & amendments ----
    #
    # An approved project stays live on its current (status='active')
    # destinations/message_id until an amendment is approved -- an
    # amendment proposes a whole NEW desired set as status='proposed' rows
    # (project_destinations) / research_projects.pending_message_id
    # alongside the still-live ones; approving flips old active rows out and
    # promotes the proposed ones, rejecting just drops the proposed ones.
    # See create_project's own docstring for why the FIRST-ever set (at
    # creation) skips this dance entirely.

    def _insert_destinations(self, project_id: str, destinations: list[dict], status: str, cur=None) -> None:
        """Takes an optional already-open `cur` so create_project below can
        insert the project row and its initial destinations in ONE
        transaction -- a destinations-insert failure must not leave an
        orphan project row with no destinations and no way to add them
        (the amendment flow only accepts destinations for an already-
        approved project, see propose_amendment)."""
        if not destinations:
            return
        now = datetime.now(timezone.utc)
        rows = [(project_id, d["destination_type"], d["destination_value"], status, now) for d in destinations]
        sql = """
            INSERT INTO project_destinations (project_id, destination_type, destination_value, status, added_at)
            VALUES (%s, %s, %s, %s, %s)
        """
        if cur is not None:
            cur.executemany(sql, rows)
            return
        with get_conn() as conn, conn.cursor() as cur:
            cur.executemany(sql, rows)

    def list_destinations(self, project_id: str, status: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM project_destinations WHERE project_id = %s"
        params: list = [project_id]
        if status is not None:
            query += " AND status = %s"
            params.append(status)
        query += " ORDER BY added_at"
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def propose_amendment(
        self,
        project_id: str,
        proposed_by: str,
        destinations: Optional[list[dict]] = None,
        message_id: Optional[int] = None,
    ) -> None:
        """Only meaningful for an already-approved project -- same
        WHERE-status-guard-then-check-rowcount pattern submit_project/
        review_project use to enforce their own state transitions. Without
        this, a draft/submitted project could pick up 'proposed' rows that
        list_pending_amendments (status='approved' only) would never
        surface, permanently blocking has_pending_amendment's 409 guard on
        a project nobody can ever get in front of a reviewer -- and once
        the project is later approved normally, those stale rows would
        resurface as an amendment nobody actually filed against the
        approved version.

        The guard and both writes share ONE connection/transaction (not
        three separate get_conn() calls) -- otherwise a revoke_project
        landing in the gap between them could reproduce the exact stuck
        state this guard exists to prevent, or leave a half-applied
        amendment (destinations written, message_id update failed, or
        vice versa)."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM research_projects WHERE project_id = %s AND status = 'approved'", (project_id,))
            if cur.fetchone() is None:
                raise ProjectNotFoundError(f"No approved project {project_id!r} to amend")
            self._insert_destinations(project_id, destinations or [], status="proposed", cur=cur)
            if message_id is not None:
                cur.execute(
                    "UPDATE research_projects SET pending_message_id = %s WHERE project_id = %s",
                    (message_id, project_id),
                )
        self.add_audit_entry(
            project_id, proposed_by, "amendment_proposed",
            {"destinations": destinations or [], "message_id": message_id},
        )

    def has_pending_amendment(self, project_id: str) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM research_projects WHERE project_id = %s AND pending_message_id IS NOT NULL
                UNION ALL
                SELECT 1 FROM project_destinations WHERE project_id = %s AND status = 'proposed'
                LIMIT 1
                """,
                (project_id, project_id),
            )
            return cur.fetchone() is not None

    def list_pending_amendments(self) -> list[dict]:
        """Approved projects with an amendment awaiting review -- a distinct
        review-queue entry from a brand-new submitted project (both need a
        data custodian's decision, but this one changes an already-live
        project rather than approving one for the first time)."""
        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT p.* FROM research_projects p
                WHERE p.status = 'approved' AND (
                    p.pending_message_id IS NOT NULL
                    OR EXISTS (
                        SELECT 1 FROM project_destinations d
                        WHERE d.project_id = p.project_id AND d.status = 'proposed'
                    )
                )
                ORDER BY p.created_at DESC
                """
            )
            return [dict(r) for r in cur.fetchall()]

    def approve_amendment(self, project_id: str, reviewed_by: str, comment: Optional[str] = None) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            # Only swap destinations out if this amendment actually proposed
            # some -- an amendment that only touched message_id (destinations
            # left as None in propose_amendment) leaves no 'proposed' rows at
            # all, and deleting 'active' ones unconditionally would wipe out
            # the project's current destinations for no reason.
            cur.execute(
                "SELECT 1 FROM project_destinations WHERE project_id = %s AND status = 'proposed' LIMIT 1",
                (project_id,),
            )
            if cur.fetchone() is not None:
                cur.execute("DELETE FROM project_destinations WHERE project_id = %s AND status = 'active'", (project_id,))
                cur.execute(
                    "UPDATE project_destinations SET status = 'active' WHERE project_id = %s AND status = 'proposed'",
                    (project_id,),
                )
            cur.execute(
                """
                UPDATE research_projects
                SET message_id = COALESCE(pending_message_id, message_id), pending_message_id = NULL
                WHERE project_id = %s
                """,
                (project_id,),
            )
        self.add_audit_entry(project_id, reviewed_by, "amendment_approved", {"comment": comment})

    def reject_amendment(self, project_id: str, reviewed_by: str, comment: Optional[str] = None) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM project_destinations WHERE project_id = %s AND status = 'proposed'", (project_id,))
            cur.execute(
                "UPDATE research_projects SET pending_message_id = NULL WHERE project_id = %s", (project_id,)
            )
        self.add_audit_entry(project_id, reviewed_by, "amendment_rejected", {"comment": comment})

    # ---- Requested patients (optional list uploaded at creation) ----

    def add_requested_patients(self, project_id: str, mrns: list[str]) -> int:
        """Bulk-record the patient IDs a project's creator says they intend
        to work with -- purely for the "N requested" stat (item 04); nothing
        elsewhere reads or validates against this list."""
        if not mrns:
            return 0
        now = datetime.now(timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO project_requested_patients (project_id, mrn, added_at) VALUES (%s, %s, %s)",
                [(project_id, mrn, now) for mrn in mrns],
            )
        return len(mrns)

    def count_requested_patients(self, project_id: str) -> int:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM project_requested_patients WHERE project_id = %s", (project_id,))
            return cur.fetchone()[0]

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
