from alembic import context
from mathagent.persistence import (  # noqa: F401
    agent_models,
    research_models,
    runtime_models,
    workspace_models,
)
from mathagent.persistence.models import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
