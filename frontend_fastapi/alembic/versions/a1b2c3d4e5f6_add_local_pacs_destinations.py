"""add local_pacs_destinations

Revision ID: a1b2c3d4e5f6
Revises: 2d3c255c556a
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '2d3c255c556a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'local_pacs_destinations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ae_title', sa.String(length=16), nullable=False),
        sa.Column('display_name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=False),
        sa.Column('created_by', sa.String(length=150), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_local_pacs_destinations_ae_title'), 'local_pacs_destinations', ['ae_title'], unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_local_pacs_destinations_ae_title'), table_name='local_pacs_destinations')
    op.drop_table('local_pacs_destinations')
