"""add admin dashboard and notifications

Revision ID: 3cea980979d2
Revises: 744bd8b0a4b7
Create Date: 2026-08-28 12:16:02.401796

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3cea980979d2'
down_revision: Union[str, Sequence[str], None] = '744bd8b0a4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Persists the outcome of each periodic hash-chain verification run
    # (backend/worker.py's new daily timer, backend/src/status/audit_chain_db.py)
    # -- so the admin dashboard can show "last verified: ..., OK" (or the
    # tampered reason) instead of re-running the check on every page load.
    # bad_event_id is nullable and ON DELETE SET NULL: a genuinely tampered
    # events row is exactly the kind of thing that might later be
    # corrected/investigated and the row altered again or removed -- this
    # history record shouldn't disappear or block that.
    op.create_table(
        "audit_chain_checks",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("checked_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("bad_event_id", sa.BigInteger, sa.ForeignKey("events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
    )
    op.create_index("ix_audit_chain_checks_checked_at", "audit_chain_checks", ["checked_at"])

    # Per-user notifications (backend/src/notifications/db_client.py).
    # job_id/project_id are both nullable and ON DELETE SET NULL, same
    # reasoning as bad_event_id above -- a notification about a job/project
    # should outlive that row if it's ever removed, not be silently deleted
    # or block the delete.
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("research_projects.project_id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_notifications_username_created_at", "notifications", ["username", "created_at"])

    # Race-safe "has a job-completion notification already been created for
    # this job" marker (StatusDB.mark_job_notified) -- backend/worker.py
    # checks job completion after every terminal task write, and this
    # guarded-UPDATE column is what makes "exactly one notification per job"
    # hold even when multiple such writes race to notice completion first.
    op.add_column("jobs", sa.Column("completed_notified_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "completed_notified_at")
    op.drop_index("ix_notifications_username_created_at", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_audit_chain_checks_checked_at", table_name="audit_chain_checks")
    op.drop_table("audit_chain_checks")
