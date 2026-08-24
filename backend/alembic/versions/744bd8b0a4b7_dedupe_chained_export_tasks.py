"""dedupe chained export tasks

Revision ID: 744bd8b0a4b7
Revises: d9258b18fef3
Create Date: 2026-08-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '744bd8b0a4b7'
down_revision: Union[str, Sequence[str], None] = 'd9258b18fef3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # backend/worker.py's _maybe_chain_export enqueues a follow-up export
    # task after a successful import. Its own docstring explains WHY the
    # enqueue must happen before mark_succeeded (closing a race with the SSE
    # observer) -- but that ordering means _maybe_chain_export can't check
    # mark_succeeded's own claimed_by ownership guard first, so a task
    # reaped from a slow worker and reclaimed by another (TasksDB.
    # reap_stale_claims; the default 1800s staleness threshold is well
    # within reach of a real Pinnacle import) gets its chained export
    # enqueued TWICE -- once by each worker that ran the import to
    # completion -- resulting in a genuine duplicate DICOM C-MOVE / ProKnow
    # upload for one patient, not just duplicate *work* (the at-least-once
    # tradeoff docs/worker-queue-design.md already accepts elsewhere).
    #
    # chained_from_task_id records which import task a chained export came
    # from (NULL for every other task -- plain batch import/export
    # submissions never set it). The partial unique index makes a second
    # chain attempt for the same import a no-op at the database level
    # (TasksDB.enqueue's ON CONFLICT DO NOTHING) rather than a race between
    # a read and a write in application code, which two genuinely-concurrent
    # workers could both pass. Scoped to chained_from_task_id IS NOT NULL
    # so it has zero effect on ordinary (non-chained) export submissions --
    # a duplicate patient_id row in a plain export CSV still enqueues two
    # tasks today, unchanged.
    op.add_column(
        "tasks",
        sa.Column("chained_from_task_id", sa.BigInteger, sa.ForeignKey("tasks.task_id", ondelete="SET NULL"),
                   nullable=True),
    )
    op.create_index(
        "ix_tasks_unique_chained_export", "tasks", ["job_id", "kind", "status_mrn"],
        unique=True, postgresql_where=sa.text("chained_from_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_unique_chained_export", table_name="tasks")
    op.drop_column("tasks", "chained_from_task_id")
