"""widen users.department to 200 chars

Revision ID: 2d3c255c556a
Revises: 7d914f970eaf
Create Date: 2026-08-11 09:57:23.062083

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2d3c255c556a'
down_revision: Union[str, Sequence[str], None] = '7d914f970eaf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch_alter_table (not a plain op.alter_column): SQLite has no
    # ALTER COLUMN ... TYPE support at all -- batch mode is Alembic's
    # portable way to express this, recreating the table under the hood on
    # SQLite while emitting a plain ALTER on Postgres.
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column(
            'department', existing_type=sa.VARCHAR(length=150),
            type_=sa.String(length=200), existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column(
            'department', existing_type=sa.String(length=200),
            type_=sa.VARCHAR(length=150), existing_nullable=False,
        )
