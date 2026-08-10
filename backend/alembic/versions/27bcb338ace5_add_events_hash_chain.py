"""add events hash chain (prev_hash/row_hash + event_chain_state)

Revision ID: 27bcb338ace5
Revises: 8aa3a51c978c
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '27bcb338ace5'
down_revision: Union[str, Sequence[str], None] = '8aa3a51c978c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both nullable=True: existing rows predate the chain and stay NULL --
    # the chain simply starts fresh from the first post-migration event.
    # See docs/safety-plan.md §D1.
    op.add_column("events", sa.Column("prev_hash", sa.Text(), nullable=True))
    op.add_column("events", sa.Column("row_hash", sa.Text(), nullable=True))

    op.create_table(
        "event_chain_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("last_hash", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_event_chain_state_singleton"),
    )
    op.execute(
        "INSERT INTO event_chain_state (id, last_hash) VALUES (1, encode(sha256(''::bytea), 'hex'))"
    )


def downgrade() -> None:
    op.drop_table("event_chain_state")
    op.drop_column("events", "row_hash")
    op.drop_column("events", "prev_hash")
