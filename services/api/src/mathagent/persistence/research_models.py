"""Version-bound research records and presentation independent of mathematics."""

from mathagent.persistence.models import Base, now
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column


class BranchPresentation(Base):
    __tablename__ = "branch_presentations"
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    hidden_object_ids: Mapped[list] = mapped_column(JSON, default=list)
    collapsed_object_ids: Mapped[list] = mapped_column(JSON, default=list)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[str] = mapped_column(String, default=now)


class ResearchRecordReference(Base):
    __tablename__ = "research_record_references"
    record_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), primary_key=True)
    target_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)
