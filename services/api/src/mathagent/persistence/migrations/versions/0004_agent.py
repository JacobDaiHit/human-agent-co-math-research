"""Persist autonomous research and immutable provider observations."""

from alembic import op
from mathagent.persistence.agent_models import AgentRun, AgentStep, BranchRuntime, ProviderCall

revision = "0004_agent"
down_revision = "0003_runtime"
branch_labels = None
depends_on = None


def upgrade():
    for table in (AgentRun.__table__, BranchRuntime.__table__, AgentStep.__table__, ProviderCall.__table__):
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore an explicit backup instead.")
