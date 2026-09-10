"""merge project-destinations and error-reports heads

Revision ID: db9f73c021e3
Revises: a7c9e2f4b1d3, ddccca9b1fca
Create Date: 2026-09-10 15:04:47.986293

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'db9f73c021e3'
down_revision: Union[str, Sequence[str], None] = ('a7c9e2f4b1d3', 'ddccca9b1fca')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
