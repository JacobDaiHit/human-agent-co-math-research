"""Durable run configuration and conservative external-request accounting."""

from mathagent.persistence.models import Base, now, uid
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class RuntimeSettings(Base):
    __tablename__ = "runtime_settings"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    request_budget: Mapped[int] = mapped_column(Integer, default=1000)
    allow_real_api: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_providers: Mapped[list] = mapped_column(JSON, default=lambda: ["fake"])


class RunOptions(Base):
    __tablename__ = "run_options"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String, default="research")
    request_budget: Mapped[int] = mapped_column(Integer, default=5)


class ProviderRequest(Base):
    __tablename__ = "provider_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"), index=True)
    provider: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="reserved")
    provider_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    output_token_reservation: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, default=now)
