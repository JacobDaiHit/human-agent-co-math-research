"""Durable request accounting and project permissions."""

import sqlalchemy as sa
from alembic import op

revision = "0003_runtime"
down_revision = "0002_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_settings",
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("request_budget", sa.Integer(), nullable=False),
        sa.Column("allow_real_api", sa.Boolean(), nullable=False),
        sa.Column("allowed_providers", sa.JSON(), nullable=False),
    )
    op.create_table(
        "run_options",
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("request_budget", sa.Integer(), nullable=False),
    )
    op.create_table(
        "provider_requests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("attempt_id", sa.String(), sa.ForeignKey("attempts.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("provider_request_id", sa.String(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    for column in ("project_id", "run_id", "attempt_id"):
        op.create_index(f"ix_provider_requests_{column}", "provider_requests", [column])


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore an explicit backup instead.")
