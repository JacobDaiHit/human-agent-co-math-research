"""Persistent research members, local work and topic-addressed messages."""

import sqlalchemy as sa
from alembic import op

revision = "0009_continuous_research"
down_revision = "0008_bounded_search"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("research_sessions",
        sa.Column("root_run_id", sa.String(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("goal_revision_id", sa.String(), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("shared_note_id", sa.String(), sa.ForeignKey("objects.id")),
        sa.Column("solution_revision_id", sa.String(), sa.ForeignKey("revisions.id")),
        sa.Column("answer", sa.Text()), sa.Column("outcome", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False))
    op.create_table("research_members",
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("root_run_id", sa.String(), sa.ForeignKey("research_sessions.root_run_id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("personal_note_id", sa.String(), sa.ForeignKey("objects.id")))
    op.create_index("ix_research_members_root_run_id", "research_members", ["root_run_id"])
    op.create_table("research_work_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("root_run_id", sa.String(), sa.ForeignKey("research_sessions.root_run_id"), nullable=False),
        sa.Column("member_run_id", sa.String(), sa.ForeignKey("research_members.run_id"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False), sa.Column("state", sa.String(), nullable=False),
        sa.Column("independent", sa.Boolean(), nullable=False),
        sa.Column("materials", sa.JSON(), nullable=False),
        sa.Column("output_revision_id", sa.String(), sa.ForeignKey("revisions.id")),
        sa.Column("created_at", sa.String(), nullable=False))
    for key in ("root_run_id", "member_run_id"):
        op.create_index("ix_research_work_items_" + key, "research_work_items", [key])
    op.create_table("research_messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("root_run_id", sa.String(), sa.ForeignKey("research_sessions.root_run_id"), nullable=False),
        sa.Column("sender_run_id", sa.String(), sa.ForeignKey("research_members.run_id"), nullable=False),
        sa.Column("recipient_run_id", sa.String(), sa.ForeignKey("research_members.run_id"), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.String(), sa.ForeignKey("revisions.id"), nullable=False),
        sa.Column("delivered_work_id", sa.String(), sa.ForeignKey("research_work_items.id")),
        sa.Column("created_at", sa.String(), nullable=False))
    for key in ("root_run_id", "recipient_run_id"):
        op.create_index("ix_research_messages_" + key, "research_messages", [key])


def downgrade():
    raise RuntimeError("Restore an explicit backup instead of deleting research records.")
