"""initial schema: jobs, patients, events

Revision ID: 12852434505f
Revises:
Create Date: 2026-07-30 08:38:45.958136

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '12852434505f'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.Text, primary_key=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("cancelled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        "patients",
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.job_id"), primary_key=True),
        sa.Column("mrn", sa.Text, primary_key=True),
        sa.Column("input_path", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.job_id"), nullable=False),
        sa.Column("mrn", sa.Text, nullable=False),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=True),
    )
    op.create_index("ix_events_job_id_mrn", "events", ["job_id", "mrn"])
    op.create_index("ix_events_mrn", "events", ["mrn"])


def downgrade() -> None:
    op.drop_index("ix_events_mrn", table_name="events")
    op.drop_index("ix_events_job_id_mrn", table_name="events")
    op.drop_table("events")
    op.drop_table("patients")
    op.drop_table("jobs")
