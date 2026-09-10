"""add project_destinations, project_requested_patients, pending_message_id

Revision ID: a7c9e2f4b1d3
Revises: f1a2b3c4d5e6
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c9e2f4b1d3'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tracks a proposed message_id until an amendment is approved -- the
    # live value stays in research_projects.message_id (added by
    # f1a2b3c4d5e6) the whole time, same "stays live until approved" rule
    # project_destinations.status implements below.
    op.add_column("research_projects", sa.Column("pending_message_id", sa.Integer, nullable=True))

    op.create_table(
        "project_destinations",
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("research_projects.project_id"), nullable=False),
        sa.Column("destination_type", sa.Text, nullable=False),
        sa.Column("destination_value", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("added_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("destination_type IN ('dicom', 'proknow')", name="ck_project_destinations_type"),
        sa.CheckConstraint("status IN ('active', 'proposed')", name="ck_project_destinations_status"),
    )
    op.create_index("ix_project_destinations_project_id", "project_destinations", ["project_id"])

    op.create_table(
        "project_requested_patients",
        # Not in the plan doc's own SQL sketch, but every other table in this
        # schema has a surrogate PK for row identity -- a PK-less table would
        # be the only exception and buys nothing (no upload-time dedup
        # constraint was asked for either).
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("research_projects.project_id"), nullable=False),
        sa.Column("mrn", sa.Text, nullable=False),
        sa.Column("added_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_project_requested_patients_project_id", "project_requested_patients", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_project_requested_patients_project_id", table_name="project_requested_patients")
    op.drop_table("project_requested_patients")
    op.drop_index("ix_project_destinations_project_id", table_name="project_destinations")
    op.drop_table("project_destinations")
    op.drop_column("research_projects", "pending_message_id")
