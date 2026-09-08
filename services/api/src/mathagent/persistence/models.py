"""Relational source of truth; revisions are never overwritten by commands."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return str(uuid4())


def now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(Text)
    original_goal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    policies: Mapped[dict] = mapped_column(JSON, default=dict)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Branch(Base):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    control_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=now)


class ResearchObject(Base):
    __tablename__ = "objects"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    author: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Revision(Base):
    __tablename__ = "revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    object_id: Mapped[str] = mapped_column(ForeignKey("objects.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    author: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class RevisionParent(Base):
    __tablename__ = "revision_parents"
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), primary_key=True)
    parent_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), primary_key=True)


class Head(Base):
    __tablename__ = "branch_heads"
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), primary_key=True)
    object_id: Mapped[str] = mapped_column(ForeignKey("objects.id"), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)


class ProofPlan(Base):
    __tablename__ = "proof_plans"
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), primary_key=True)
    conclusion_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    rule: Mapped[str] = mapped_column(String, default="direct")


class Dependency(Base):
    __tablename__ = "dependencies"
    plan_revision_id: Mapped[str] = mapped_column(
        ForeignKey("proof_plans.revision_id"), primary_key=True
    )
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)
    origin: Mapped[str] = mapped_column(String, default="author_declared")
    scope: Mapped[str] = mapped_column(String, default="plan")


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    target_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    verdict: Mapped[str] = mapped_column(String)
    coverage: Mapped[str] = mapped_column(String, default="partial")
    scope: Mapped[str] = mapped_column(Text)
    findings: Mapped[list] = mapped_column(JSON, default=list)
    dependency_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    author: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Adoption(Base):
    __tablename__ = "adoptions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), index=True)
    state: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String)
    event_seq: Mapped[int] = mapped_column(Integer)


class Relation(Base):
    __tablename__ = "relations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("objects.id"))
    target_id: Mapped[str] = mapped_column(ForeignKey("objects.id"))
    kind: Mapped[str] = mapped_column(String)


class ManuscriptBlock(Base):
    __tablename__ = "manuscript_blocks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text, default="")
    revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)


class Event(Base):
    __tablename__ = "events"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[str] = mapped_column(String, unique=True, default=uid)
    branch_id: Mapped[str | None] = mapped_column(ForeignKey("branches.id"), nullable=True)
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    operation: Mapped[str] = mapped_column(String)
    digest: Mapped[str] = mapped_column(String)
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)


class Conflict(Base):
    __tablename__ = "conflicts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), index=True)
    object_id: Mapped[str] = mapped_column(ForeignKey("objects.id"))
    expected_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    current_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    candidate_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"))
    reason: Mapped[str] = mapped_column(Text)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), index=True)
    goal_object_id: Mapped[str] = mapped_column(ForeignKey("objects.id"))
    state: Mapped[str] = mapped_column(String, default="queued")
    control_epoch: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String, default="fake")
    instruction: Mapped[str] = mapped_column(Text, default="")
    current_attempt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("run_id", "number"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String)
    read_set: Mapped[dict] = mapped_column(JSON)
    control_epoch: Mapped[int] = mapped_column(Integer)
    token: Mapped[str] = mapped_column(String)
    lease_until: Mapped[str] = mapped_column(String)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    output_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("revisions.id"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, default=now)
