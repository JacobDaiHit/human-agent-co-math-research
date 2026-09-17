"""Durable indexes for the bounded-search controller.

These tables deliberately index mathematical material held in ``Revision``;
they do not duplicate proof text or assert that a memory item is true.
"""

from mathagent.persistence.models import Base, now, uid
from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class SearchSession(Base):
    __tablename__ = "search_sessions"

    root_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    goal_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)
    phase: Mapped[str] = mapped_column(String, default="analysis")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=0)
    selected_route_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class SearchRoute(Base):
    __tablename__ = "search_routes"
    __table_args__ = (UniqueConstraint("root_run_id", "ordinal"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("search_sessions.root_run_id"), index=True)
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    card_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String, default="queued")
    candidate_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    repairs: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)


class SearchWork(Base):
    __tablename__ = "search_work"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("search_sessions.root_run_id"), index=True)
    route_id: Mapped[str | None] = mapped_column(ForeignKey("search_routes.id"), nullable=True, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="queued")
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    budget_pool: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, default=now)


class SearchGap(Base):
    __tablename__ = "search_gaps"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("search_sessions.root_run_id"), index=True)
    route_id: Mapped[str | None] = mapped_column(ForeignKey("search_routes.id"), nullable=True, index=True)
    target_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)
    source_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String, default="open")
    repairs: Mapped[int] = mapped_column(Integer, default=0)


class SearchMemory(Base):
    __tablename__ = "search_memory"
    __table_args__ = (UniqueConstraint("root_run_id", "source_event"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("search_sessions.root_run_id"), index=True)
    route_id: Mapped[str | None] = mapped_column(ForeignKey("search_routes.id"), nullable=True, index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)
    evidence_type: Mapped[str] = mapped_column(String, default="proposed")
    dependency_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    assumptions: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String, default="current")
    source_event: Mapped[str] = mapped_column(String)


class SearchDecision(Base):
    __tablename__ = "search_decisions"
    __table_args__ = (UniqueConstraint("root_run_id", "trigger_key"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("search_sessions.root_run_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    trigger_key: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=now)
