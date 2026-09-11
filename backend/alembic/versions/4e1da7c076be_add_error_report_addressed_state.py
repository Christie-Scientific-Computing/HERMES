"""add error report addressed state

Revision ID: 4e1da7c076be
Revises: db9f73c021e3
Create Date: 2026-09-10 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e1da7c076be'
down_revision: Union[str, Sequence[str], None] = 'db9f73c021e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Persisted "addressed" state for error_reports (F003, .plans/ui-polish/plan.md),
    # mirroring notifications.read_at's nullable-timestamp shape. resolved_by
    # is added (unlike read_at, which is always "by the reading user
    # themselves") since the admin page shows who addressed a report, not
    # just when.
    op.add_column("error_reports", sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("error_reports", sa.Column("resolved_by", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("error_reports", "resolved_by")
    op.drop_column("error_reports", "resolved_at")
