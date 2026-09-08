"""Workspace presentation and human annotations, separate from mathematical revisions."""

from mathagent.persistence.models import Base, now, uid
from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class BranchLayout(Base):
    __tablename__ = "branch_layouts"
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    positions: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String, default=now)


class Annotation(Base):
    __tablename__ = "annotations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    anchor_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str] = mapped_column(String, default="human")
    created_at: Mapped[str] = mapped_column(String, default=now)


class BlockRevision(Base):
    __tablename__ = "manuscript_block_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    block_id: Mapped[str] = mapped_column(ForeignKey("manuscript_blocks.id"), index=True)
    previous_body: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String, default="human")
    created_at: Mapped[str] = mapped_column(String, default=now)


class ConflictResolution(Base):
    __tablename__ = "conflict_resolutions"
    conflict_id: Mapped[str] = mapped_column(ForeignKey("conflicts.id"), primary_key=True)
    selected_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    previous_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    author: Mapped[str] = mapped_column(String, default="human")
    created_at: Mapped[str] = mapped_column(String, default=now)
