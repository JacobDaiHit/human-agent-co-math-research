"""HTTP contract source. OpenAPI is generated from these models."""

from enum import StrEnum
from typing import Annotated, Literal

from mathagent.providers.options import (
    MAX_OUTPUT_TOKENS,
    MAX_REQUEST_TIMEOUT_SECONDS,
    MAX_RESEARCH_SECONDS,
    ReasoningEffort,
    ThinkingMode,
)
from mathagent.providers.search_contract import SearchConfig
from pydantic import BaseModel, ConfigDict, Field

Id = Annotated[str, Field(min_length=1, max_length=100)]
Body = Annotated[str, Field(min_length=1, max_length=200_000)]


class ObjectKind(StrEnum):
    problem = "problem"
    context = "context"
    claim = "claim"
    argument = "argument"
    artifact = "artifact"
    activity = "activity"


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Command):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    body: Body


class ObjectCreate(Command):
    branch_id: Id
    kind: ObjectKind
    body: Body
    payload: dict = Field(default_factory=dict)


class RevisionCreate(Command):
    branch_id: Id
    expected_revision_id: Id
    body: Body
    payload: dict | None = None
    read_set: dict[str, str] = Field(default_factory=dict)


class BranchCreate(Command):
    source_branch_id: Id
    name: Annotated[str, Field(min_length=1, max_length=120)]


class ProofCreate(Command):
    branch_id: Id
    conclusion_revision_id: Id
    body: Body
    premise_revision_ids: list[Id] = Field(default_factory=list, max_length=500)
    context_revision_ids: list[Id] = Field(default_factory=list, max_length=500)
    assumption_revision_ids: list[Id] = Field(default_factory=list, max_length=500)
    gaps: list[str] = Field(default_factory=list)
    rule: Annotated[str, Field(min_length=1, max_length=100)] = "direct"


class ReviewCreate(Command):
    branch_id: Id
    target_revision_id: Id
    kind: Literal[
        "candidate_proof",
        "llm_review",
        "human_review",
        "numerical_experiment",
        "exact_computation",
        "formal_check",
    ]
    verdict: Literal["passed", "issues", "inconclusive"]
    coverage: Literal["whole_plan", "partial"] = "partial"
    scope: Body
    findings: list[str] = Field(min_length=1)


class AdoptionCreate(Command):
    branch_id: Id
    revision_id: Id
    state: Literal["draft", "pending_review", "adopted", "disputed", "withdrawn"]
    reason: Body


class RelationCreate(Command):
    branch_id: Id
    source_id: Id
    target_id: Id
    kind: Literal["inspires", "attempts", "refutes", "rewrites", "similar"]


class BlockCreate(Command):
    branch_id: Id
    kind: Literal["text", "reference"]
    body: str = ""
    revision_id: str | None = None


class RunCreate(Command):
    branch_id: Id
    goal_object_id: Id
    instruction: str = ""
    provider: Literal["fake", "deepseek", "glm"] = "fake"
    mode: Literal["research", "review"] = "research"
    request_budget: int = Field(default=1000, ge=1, le=1000)
    autonomous: bool = False
    max_steps: int = Field(default=8, ge=1, le=40)
    max_review_rounds: int = Field(default=2, ge=0, le=2)
    max_children: int = Field(default=4, ge=0, le=12)
    max_depth: int = Field(default=2, ge=0, le=4)
    max_output_tokens: int = Field(default=131072, ge=1, le=MAX_OUTPUT_TOKENS)
    # This constrains completion/output tokens across the complete root run tree.
    # Prompt tokens are reported when supplied by the provider, but are not used as
    # a tokenizer-dependent admission bound.
    cumulative_output_token_budget: int | None = Field(default=None, ge=1, le=10_000_000)
    request_timeout_seconds: int = Field(default=3600, ge=1, le=MAX_REQUEST_TIMEOUT_SECONDS)
    thinking_mode: ThinkingMode = "provider_default"
    reasoning_effort: ReasoningEffort = "provider_default"
    completion_policy: Literal["draft", "reviewed_answer"] = "draft"
    length_recovery: Literal["none", "high"] = "none"
    answer_submission_recovery: bool = False
    answer_requires_exhaustiveness: bool = False
    unknown_recovery: Literal["stop", "once"] = "stop"
    solver_controller: Literal["legacy", "bounded_search_v1", "continuous_research"] = "continuous_research"
    discussion: bool = True
    research_deadline_seconds: int = Field(default=86400, ge=1, le=MAX_RESEARCH_SECONDS)
    max_researchers: int = Field(default=4, ge=1, le=16)
    literature: bool = True
    search_config: SearchConfig = Field(default_factory=SearchConfig)


class CompleteAttempt(Command):
    token: Id
    body: Body
    result: dict | None = None


class InterventionCreate(Command):
    action: Literal["pause", "cancel", "steer"]
    instruction: str = ""
