"""Retain whether visible raw output was shortened by storage bounds."""

from alembic import op
from sqlalchemy import Boolean, Column, inspect

revision = "0006_call_retention"
down_revision = "0005_research_records"
branch_labels = None
depends_on = None


def upgrade():
    if "raw_text_truncated" not in {c["name"] for c in inspect(op.get_bind()).get_columns("provider_calls")}:
        op.add_column("provider_calls", Column("raw_text_truncated", Boolean(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("provider_calls", "raw_text_truncated")
