"""Durable autonomous research options, steps, calls, and branch controls."""

from mathagent.persistence.models import Base, now, uid
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class AgentRun(Base):
    __tablename__ = "agent_runs"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    root_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    target_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    autonomous: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[dict] = mapped_column(JSON, default=dict)


class BranchRuntime(Base):
    __tablename__ = "branch_runtime"
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String, default="active")
    request_budget: Mapped[int] = mapped_column(Integer, default=1000)
    instruction: Mapped[str] = mapped_column(Text, default="")


class AgentStep(Base):
    __tablename__ = "agent_steps"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), index=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("provider_requests.id"), unique=True)
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    output_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    request_id: Mapped[str] = mapped_column(ForeignKey("provider_requests.id"), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), index=True)
    call_config: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    raw_text_truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    raw_sha256: Mapped[str] = mapped_column(String, default="")
    finish_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    provider_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
