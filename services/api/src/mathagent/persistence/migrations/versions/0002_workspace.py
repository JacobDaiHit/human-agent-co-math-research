"""Persist human workspace state without changing immutable mathematical revisions."""

import sqlalchemy as sa
from alembic import op

revision = "0002_workspace"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "branch_layouts",
        sa.Column("branch_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("positions", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["branch_id"], ["branches.id"]),
        sa.PrimaryKeyConstraint("branch_id"),
    )
    op.create_table(
        "annotations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("branch_id", sa.String(), nullable=False),
        sa.Column("revision_id", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("anchor_quote", sa.Text(), nullable=True),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["branch_id"], ["branches.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_annotations_branch_id", "annotations", ["branch_id"])
    op.create_index("ix_annotations_revision_id", "annotations", ["revision_id"])
    op.create_table(
        "manuscript_block_revisions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("block_id", sa.String(), nullable=False),
        sa.Column("previous_body", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["block_id"], ["manuscript_blocks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_manuscript_block_revisions_block_id", "manuscript_block_revisions", ["block_id"]
    )
    op.create_table(
        "conflict_resolutions",
        sa.Column("conflict_id", sa.String(), nullable=False),
        sa.Column("selected_revision_id", sa.String(), nullable=False),
        sa.Column("previous_revision_id", sa.String(), nullable=False),
        sa.Column("revision_id", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["conflict_id"], ["conflicts.id"]),
        sa.ForeignKeyConstraint(["selected_revision_id"], ["revisions.id"]),
        sa.ForeignKeyConstraint(["previous_revision_id"], ["revisions.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["revisions.id"]),
        sa.PrimaryKeyConstraint("conflict_id"),
    )


def downgrade():
    op.drop_table("conflict_resolutions")
    op.drop_index("ix_manuscript_block_revisions_block_id", "manuscript_block_revisions")
    op.drop_table("manuscript_block_revisions")
    op.drop_index("ix_annotations_revision_id", "annotations")
    op.drop_index("ix_annotations_branch_id", "annotations")
    op.drop_table("annotations")
    op.drop_table("branch_layouts")
