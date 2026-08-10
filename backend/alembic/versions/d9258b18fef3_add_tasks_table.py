"""add tasks table

Revision ID: d9258b18fef3
Revises: 27bcb338ace5
Create Date: 2026-08-10 12:14:10.706758

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd9258b18fef3'
down_revision: Union[str, Sequence[str], None] = '27bcb338ace5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-item unit of work for the worker queue (docs/worker-queue-design.md).
    # job_id stays the group -- jobs already carries every group-level
    # attribute (project_id, created_by, cancelled) and cancel_job is already
    # a cross-process UPDATE. task_id is a surrogate PK (sa.Identity, same
    # style as events.id/project_audit_log.id) since, unlike jobs.job_id,
    # there's no natural key here -- the UID export flow legitimately repeats
    # status_mrn, so no UNIQUE(job_id, mrn, stage) either; idempotency comes
    # from the terminal-state guard in TasksDB.mark_failed, not a constraint.
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.job_id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="queued"),
        sa.Column("real_id", sa.Text, nullable=False),
        sa.Column("display_id", sa.Text, nullable=False),
        sa.Column("status_mrn", sa.Text, nullable=False),
        sa.Column("input_path", sa.Text, nullable=True),
        sa.Column("extra", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("claimed_by", sa.Text, nullable=True),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=True),
        sa.CheckConstraint(
            "state IN ('queued','claimed','running','succeeded','failed','cancelled')",
            name="ck_tasks_state",
        ),
    )
    op.create_index("ix_tasks_job_id", "tasks", ["job_id"])
    # Backs the SKIP LOCKED claim query's WHERE state='queued' ORDER BY
    # priority DESC, created_at.
    op.create_index("ix_tasks_claim", "tasks", ["state", "priority", "created_at"])

    # The claim query's `job_id NOT IN (SELECT job_id FROM jobs WHERE cancelled)`
    # runs on every single claim (the hottest path once workers poll
    # continuously). Cancelled jobs are rare, so a partial index keyed on
    # exactly that predicate keeps the subquery an index-only scan over a
    # tiny slice of `jobs`, regardless of how large `jobs` grows overall.
    op.create_index("ix_jobs_cancelled", "jobs", ["job_id"], postgresql_where=sa.text("cancelled"))

    # Nullable, populated only once a worker actually runs a task -- see
    # backend/src/status/hash_chain.py's canonical_event_json, which hashes
    # exactly (job_id, mrn, stage, event_type, ts, attempt, error_message,
    # details) and never sees this column, so adding it here cannot perturb
    # any previously-computed row_hash (same reasoning 27bcb338ace5 relied on
    # when it added prev_hash/row_hash as plain nullable columns).
    # ondelete="SET NULL": events is the immutable audit log and must never
    # be blocked from existing by a since-pruned task row -- a historical
    # event simply loses its task_id link rather than the delete failing.
    op.add_column(
        "events",
        sa.Column("task_id", sa.BigInteger, sa.ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("events", "task_id")

    op.drop_index("ix_jobs_cancelled", table_name="jobs")
    op.drop_index("ix_tasks_claim", table_name="tasks")
    op.drop_index("ix_tasks_job_id", table_name="tasks")
    op.drop_table("tasks")
