"""Persist conservative per-request output-token reservations."""

import sqlalchemy as sa
from alembic import op

revision = "0007_output_token_budget"
down_revision = "0006_call_retention"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "provider_requests",
        sa.Column("output_token_reservation", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore an explicit backup instead.")
