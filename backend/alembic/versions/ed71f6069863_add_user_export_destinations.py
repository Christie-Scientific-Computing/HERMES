"""add user_export_destinations (per-user export destination allow-list)

Revision ID: ed71f6069863
Revises: 8aa3a51c978c
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ed71f6069863'
down_revision: Union[str, Sequence[str], None] = '8aa3a51c978c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_export_destinations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("destination_type", sa.Text(), nullable=False),
        sa.Column("destination", sa.Text(), nullable=False),
        sa.Column("added_by", sa.Text(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "destination_type IN ('dicom_modality', 'proknow_collection')",
            name="ck_user_export_destinations_type",
        ),
        sa.UniqueConstraint("username", "destination_type", "destination"),
    )
    op.create_index(
        "ix_user_export_destinations_username", "user_export_destinations", ["username"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_export_destinations_username", table_name="user_export_destinations")
    op.drop_table("user_export_destinations")
