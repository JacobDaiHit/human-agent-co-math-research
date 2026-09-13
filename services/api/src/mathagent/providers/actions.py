"""Validated research proposals; isolated code requires opt-in, browsing is absent."""

import keyword
import re
from typing import Annotated, Literal

from mathagent.api.schemas import Body, Command, Id, ProofCreate, RevisionCreate
from pydantic import Field, RootModel, ValidationError, field_validator


class ReadObject(Command):
    object_id: Id
    revision_id: Id | None = None
    branch_id: Id | None = None
    section: Literal["body", "payload", "record"] = "body"
    context_attempt_id: Id | None = None
    offset: int = Field(default=0, ge=0, le=1000000000)
    max_chars: int = Field(default=12000, ge=1000, le=20000)


class SearchProject(Command):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=8, ge=1, le=20)


class WriteDraft(Command):
    kind: Literal["problem", "context", "claim", "artifact", "activity"] = "claim"
    body: Body
    payload: dict = Field(default_factory=dict)


class ReviseObject(RevisionCreate):
    branch_id: Id | None = None
    object_id: Id


class ProposeProof(ProofCreate):
    branch_id: Id | None = None


class CreateBranch(Command):
    name: str = Field(min_length=1, max_length=120)


class SpawnTask(Command):
    goal_object_id: Id
    branch_id: Id | None = None
    instruction: Body
    request_budget: int = Field(default=3, ge=1, le=1000)


class RequestReview(Command):
    target_revision_id: Id
    instruction: str = Field(default="独立检查论证的前提、各步及可能的缺口。", max_length=20000)


class Discuss(Command):
    body: Body
    recipient_run_id: Id | None = None


CALCULATION_SYNTAX = (
    "Use integer literals and exact rational fractions such as 1/3. "
    "Use explicit * for multiplication and ** for powers; never ^ or implicit multiplication. "
    "Only +, -, *, /, ** and parentheses are allowed; no functions, decimals, "
    "LaTeX commands, assignments, imports or executable code. "
    "Use the exact named input fields; lhs/rhs are not aliases."
)


class RationalInputs(Command):
    expression: str = Field(
        min_length=1,
        max_length=4096,
        pattern=r"^[0-9+*/()\s-]+$",
        description=CALCULATION_SYNTAX
        + " No variables. Integer powers must be between -16 and 16.",
        examples=["1/3+2/5", "(3/2)**-2"],
    )

    @field_validator("expression")
    @classmethod
    def explicit_arithmetic(cls, value):
        if not value.strip() or re.search(r"[0-9)]\s*\(|\)\s*[0-9]", value):
            raise ValueError("Only explicit arithmetic operations are accepted.")
        return value


class PolynomialInputs(Command):
    left: str = Field(
        min_length=1,
        max_length=4096,
        pattern=r"^[A-Za-z0-9_+*/()\s-]+$",
        description=CALCULATION_SYNTAX + " Left polynomial with rational coefficients.",
        examples=["(x+y)**2"],
    )
    right: str = Field(
        min_length=1,
        max_length=4096,
        pattern=r"^[A-Za-z0-9_+*/()\s-]+$",
        description=CALCULATION_SYNTAX
        + " Right polynomial; divide only by nonzero rational constants. "
        "Integer powers must be from 0 through 16 and total degree at most 16.",
        examples=["x**2+2*x*y+y**2"],
    )
    variables: list[Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,15}$")]] = Field(
        min_length=1,
        max_length=4,
        description="Required list of all variable names used on either side: 1 to 4 distinct "
        "ASCII identifiers, each at most 16 characters; Python keywords are forbidden.",
        examples=[["x", "y"]],
    )

    @field_validator("left", "right")
    @classmethod
    def no_calls_or_implicit_products(cls, value):
        if not value.strip() or re.search(r"[A-Za-z0-9_)]\s*\(|\)\s*[A-Za-z0-9_]", value):
            raise ValueError("Only explicit arithmetic operations are accepted.")
        return value

    @field_validator("variables")
    @classmethod
    def distinct_variables(cls, values):
        if len(values) != len(set(values)) or any(keyword.iskeyword(value) for value in values):
            raise ValueError("Variable identifiers must be distinct and non-keywords.")
        return values


class HermitianInputs(Command):
    """Empty object only; the matrix family is fixed and takes no parameters."""


class RationalCalculation(Command):
    tool: Literal["rational_arithmetic"]
    inputs: RationalInputs
    target_revision_id: Id | None = None


class PolynomialCalculation(Command):
    tool: Literal["polynomial_identity"]
    inputs: PolynomialInputs
    target_revision_id: Id | None = None


class HermitianCalculation(Command):
    tool: Literal["hermitian_crossing"]
    inputs: HermitianInputs
    target_revision_id: Id | None = None


class RunCode(Command):
    code: str = Field(min_length=1, max_length=20000)
    timeout_seconds: int = Field(default=5, ge=1, le=10)
    target_revision_id: Id | None = None


class Calculate(
    RootModel[
        Annotated[
            RationalCalculation | PolynomialCalculation | HermitianCalculation,
            Field(discriminator="tool"),
        ]
    ]
):
    """Choose one fixed calculator and supply exactly that tool's required inputs."""


def calculation_validation_feedback(error: ValidationError, tool):
    """Expose schema locations, never rejected values, messages or exception context."""
    expected = {
        "rational_arithmetic": ["inputs.expression"],
        "polynomial_identity": ["inputs.left", "inputs.right", "inputs.variables"],
        "hermitian_crossing": ["inputs (empty object)"],
    }
    safe_names = {
        "tool",
        "inputs",
        "target_revision_id",
        "expression",
        "left",
        "right",
        "variables",
        "lhs",
        "rhs",
    }
    errors = []
    selected_tool = tool if isinstance(tool, str) else None
    for item in error.errors(include_url=False, include_context=False, include_input=False)[:12]:
        path = []
        for part in item["loc"]:
            if part in expected:
                continue  # Pydantic's discriminated-union branch is not a JSON field.
            path.append(part if type(part) is int or part in safe_names else "<extra_field>")
        errors.append({"path": path, "code": item["type"]})
    return {
        "validation_errors": errors,
        "expected_inputs": expected.get(selected_tool, ["tool", "inputs"]),
        "syntax_hint": CALCULATION_SYNTAX,
    }


OPERATION_MODELS = {
    "read_object": ReadObject,
    "search_project": SearchProject,
    "write_draft": WriteDraft,
    "revise_object": ReviseObject,
    "propose_proof": ProposeProof,
    "create_branch": CreateBranch,
    "spawn_task": SpawnTask,
    "request_review": RequestReview,
    "discuss": Discuss,
    "calculate": Calculate,
    "run_code": RunCode,
}


def operation_schemas():
    from mathagent.api.research_routes import FailureCreate, SourceCreate

    models = {**OPERATION_MODELS, "record_failure": FailureCreate, "record_source": SourceCreate}
    return {name: model.model_json_schema() for name, model in models.items()}
