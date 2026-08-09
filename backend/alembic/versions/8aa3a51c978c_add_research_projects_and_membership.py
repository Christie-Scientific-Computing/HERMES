"""add research_projects, project_memberships, project_audit_log; jobs.project_id

Revision ID: 8aa3a51c978c
Revises: 12852434505f
Create Date: 2026-07-31 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '8aa3a51c978c'
down_revision: Union[str, Sequence[str], None] = '12852434505f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_projects",
        sa.Column("project_id", sa.Text, primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("ethics_reference", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("reviewed_by", sa.Text, nullable=True),
        sa.Column("review_comment", sa.Text, nullable=True),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expiry_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    op.create_table(
        "project_memberships",
        sa.Column("project_id", sa.Text, sa.ForeignKey("research_projects.project_id"), primary_key=True),
        sa.Column("username", sa.Text, primary_key=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("added_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    # Composite PK optimizes project_id-first lookups; "which active projects
    # is this user a member of" (checked on every login/project-switch/job
    # start) filters on username alone, so it needs its own index.
    op.create_index("ix_project_memberships_username", "project_memberships", ["username"])

    op.create_table(
        "project_audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("research_projects.project_id"), nullable=False),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("details", postgresql.JSONB, nullable=True),
    )
    op.create_index("ix_project_audit_log_project_id", "project_audit_log", ["project_id"])

    op.add_column(
        "jobs",
        sa.Column("project_id", sa.Text, sa.ForeignKey("research_projects.project_id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "project_id")

    op.drop_index("ix_project_audit_log_project_id", table_name="project_audit_log")
    op.drop_table("project_audit_log")

    op.drop_index("ix_project_memberships_username", table_name="project_memberships")
    op.drop_table("project_memberships")

    op.drop_table("research_projects")
