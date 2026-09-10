"""add error_reports table

Revision ID: ddccca9b1fca
Revises: 3cea980979d2
Create Date: 2026-09-10 09:44:30.486980

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ddccca9b1fca'
down_revision: Union[str, Sequence[str], None] = '3cea980979d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # User-submitted feedback/error reports (item 06,
    # backend/src/error_reports/db_client.py). job_id is nullable and ON
    # DELETE SET NULL, same reasoning as notifications.job_id -- a report
    # about a job should outlive that row if it's ever removed, not be
    # silently deleted or block the delete.
    op.create_table(
        "error_reports",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("urgent", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.job_id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_error_reports_created_at", "error_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_error_reports_created_at", table_name="error_reports")
    op.drop_table("error_reports")
