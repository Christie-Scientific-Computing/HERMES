"""add message_id to research_projects

Revision ID: f1a2b3c4d5e6
Revises: 3cea980979d2
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = '3cea980979d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Just the one column item 01 ("Job settings: MU tolerance & message
    # ID", docs/plans/feature-round-implementation-plan.md) needs to read --
    # the rest of item 05's schema (project_destinations,
    # project_requested_patients, the amendment workflow) is a separate,
    # not-yet-built PR. Nullable: there's no UI to set this yet either (that
    # is also item 05's job), so every project's message_id stays NULL
    # until then.
    op.add_column("research_projects", sa.Column("message_id", sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column("research_projects", "message_id")
