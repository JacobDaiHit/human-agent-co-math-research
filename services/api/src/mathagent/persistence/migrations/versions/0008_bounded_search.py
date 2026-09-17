"""Add durable bounded-search controller and memory indexes."""

import sqlalchemy as sa
from alembic import op

revision = "0008_bounded_search"
down_revision = "0007_output_token_budget"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("search_sessions",
        sa.Column("root_run_id", sa.String(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("goal_revision_id", sa.String(), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("phase", sa.String(), nullable=False), sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("selected_route_id", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False))
    op.create_index("ix_search_sessions_project_id", "search_sessions", ["project_id"])
    op.create_index("ix_search_sessions_goal_revision_id", "search_sessions", ["goal_revision_id"])
    op.create_table("search_routes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("root_run_id", sa.String(), sa.ForeignKey("search_sessions.root_run_id"), nullable=False),
        sa.Column("branch_id", sa.String(), sa.ForeignKey("branches.id")),
        sa.Column("card_revision_id", sa.String(), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False), sa.Column("state", sa.String(), nullable=False),
        sa.Column("candidate_revision_id", sa.String(), sa.ForeignKey("revisions.id")),
        sa.Column("progress", sa.JSON(), nullable=False), sa.Column("repairs", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("root_run_id", "ordinal"))
    for name, columns in (("ix_search_routes_root_run_id", ["root_run_id"]), ("ix_search_routes_branch_id", ["branch_id"]), ("ix_search_routes_card_revision_id", ["card_revision_id"])):
        op.create_index(name, "search_routes", columns)
    op.create_table("search_work",
        sa.Column("id", sa.String(), primary_key=True), sa.Column("root_run_id", sa.String(), sa.ForeignKey("search_sessions.root_run_id"), nullable=False),
        sa.Column("route_id", sa.String(), sa.ForeignKey("search_routes.id")), sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False), sa.Column("state", sa.String(), nullable=False), sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("budget_pool", sa.String(), nullable=False), sa.Column("created_at", sa.String(), nullable=False))
    for name, columns in (("ix_search_work_root_run_id", ["root_run_id"]), ("ix_search_work_route_id", ["route_id"]), ("ix_search_work_run_id", ["run_id"])):
        op.create_index(name, "search_work", columns)
    op.create_table("search_gaps",
        sa.Column("id", sa.String(), primary_key=True), sa.Column("root_run_id", sa.String(), sa.ForeignKey("search_sessions.root_run_id"), nullable=False),
        sa.Column("route_id", sa.String(), sa.ForeignKey("search_routes.id")), sa.Column("target_revision_id", sa.String(), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("source_revision_id", sa.String(), sa.ForeignKey("revisions.id")), sa.Column("kind", sa.String(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False), sa.Column("state", sa.String(), nullable=False), sa.Column("repairs", sa.Integer(), nullable=False))
    for name, columns in (("ix_search_gaps_root_run_id", ["root_run_id"]), ("ix_search_gaps_route_id", ["route_id"]), ("ix_search_gaps_target_revision_id", ["target_revision_id"])):
        op.create_index(name, "search_gaps", columns)
    op.create_table("search_memory",
        sa.Column("id", sa.String(), primary_key=True), sa.Column("root_run_id", sa.String(), sa.ForeignKey("search_sessions.root_run_id"), nullable=False),
        sa.Column("route_id", sa.String(), sa.ForeignKey("search_routes.id")), sa.Column("revision_id", sa.String(), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("branch_id", sa.String(), sa.ForeignKey("branches.id")), sa.Column("kind", sa.String(), nullable=False), sa.Column("evidence_type", sa.String(), nullable=False),
        sa.Column("dependency_snapshot", sa.JSON(), nullable=False), sa.Column("assumptions", sa.JSON(), nullable=False), sa.Column("state", sa.String(), nullable=False), sa.Column("source_event", sa.String(), nullable=False),
        sa.UniqueConstraint("root_run_id", "source_event"))
    for name, columns in (("ix_search_memory_root_run_id", ["root_run_id"]), ("ix_search_memory_route_id", ["route_id"]), ("ix_search_memory_revision_id", ["revision_id"]), ("ix_search_memory_branch_id", ["branch_id"])):
        op.create_index(name, "search_memory", columns)
    op.create_table("search_decisions",
        sa.Column("id", sa.String(), primary_key=True), sa.Column("root_run_id", sa.String(), sa.ForeignKey("search_sessions.root_run_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("trigger_key", sa.String(), nullable=False), sa.Column("action", sa.String(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False), sa.Column("created_at", sa.String(), nullable=False), sa.UniqueConstraint("root_run_id", "trigger_key"))
    op.create_index("ix_search_decisions_root_run_id", "search_decisions", ["root_run_id"])


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore an explicit backup instead.")
