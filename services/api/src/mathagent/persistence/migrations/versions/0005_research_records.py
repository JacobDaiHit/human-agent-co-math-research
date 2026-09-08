"""Structured research record references and persistent branch presentation."""

import sqlalchemy as sa
from alembic import op

revision = "0005_research_records"
down_revision = "0004_agent"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "branch_presentations",
        sa.Column("branch_id", sa.String(), sa.ForeignKey("branches.id"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("hidden_object_ids", sa.JSON(), nullable=False),
        sa.Column("collapsed_object_ids", sa.JSON(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "research_record_references",
        sa.Column(
            "record_revision_id", sa.String(), sa.ForeignKey("revisions.id"), primary_key=True
        ),
        sa.Column(
            "target_revision_id", sa.String(), sa.ForeignKey("revisions.id"), primary_key=True
        ),
        sa.Column("role", sa.String(), primary_key=True),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore an explicit backup instead.")
