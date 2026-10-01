"""Small indexes for persistent researchers, work items and topic messages.

Mathematics still lives in ordinary immutable workbench revisions. These rows
describe execution and delivery; none represents a certified mathematical fact.
"""

from mathagent.persistence.models import Base, now, uid
from sqlalchemy import JSON, Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class ResearchSession(Base):
    __tablename__ = "research_sessions"
    root_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    goal_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    state: Mapped[str] = mapped_column(String, default="researching")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    shared_note_id: Mapped[str | None] = mapped_column(ForeignKey("objects.id"), nullable=True)
    solution_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ResearchMember(Base):
    __tablename__ = "research_members"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("research_sessions.root_run_id"), index=True)
    name: Mapped[str] = mapped_column(String)
    personal_note_id: Mapped[str | None] = mapped_column(ForeignKey("objects.id"), nullable=True)


class ResearchWork(Base):
    __tablename__ = "research_work_items"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("research_sessions.root_run_id"), index=True)
    member_run_id: Mapped[str] = mapped_column(ForeignKey("research_members.run_id"), index=True)
    goal: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="queued")
    independent: Mapped[bool] = mapped_column(Boolean, default=False)
    materials: Mapped[list] = mapped_column(JSON, default=list)
    output_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ResearchMessage(Base):
    __tablename__ = "research_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("research_sessions.root_run_id"), index=True)
    sender_run_id: Mapped[str] = mapped_column(ForeignKey("research_members.run_id"))
    recipient_run_id: Mapped[str] = mapped_column(ForeignKey("research_members.run_id"), index=True)
    topic: Mapped[str] = mapped_column(Text)
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    delivered_work_id: Mapped[str | None] = mapped_column(ForeignKey("research_work_items.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
