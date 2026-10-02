"""Record provider-reported model labels separately from frozen request options."""

import sqlalchemy as sa
from alembic import op

revision = "0010_provider_response_metadata"
down_revision = "0009_continuous_research"
branch_labels = None
depends_on = None


def upgrade():
    # Older migrations create tables from current model metadata on fresh installs.
    if "response_metadata" not in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("provider_calls")}:
        op.add_column("provider_calls", sa.Column("response_metadata", sa.JSON(),
            nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column("provider_calls", "response_metadata")
