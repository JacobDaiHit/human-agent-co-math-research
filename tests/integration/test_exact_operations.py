"""Exact tools run locally on bounded arithmetic data and persist partial evidence."""

import ast
from uuid import uuid4

import pytest
from mathagent.application.errors import DomainError
from mathagent.application.state import StateService
from mathagent.persistence.database import Database
from mathagent.persistence.models import Adoption, ResearchObject, Review, Revision
from mathagent.tools.exact import LIMITS, TOOL_VERSION, execute_calculation
from sqlalchemy import select


@pytest.fixture
def workspace(tmp_path):
    database = Database(tmp_path / "exact.sqlite3")
    database.migrate()
    state = StateService(database)
    _, project = state.execute(
        "project.create",
        str(uuid4()),
        {"title": "Exact tool", "body": "检查给定计算。"},
        state.create_project,
    )
    yield state, project
    database.close()


def calculate(workspace, tool, inputs, *, target=None, key=None):
    state, project = workspace
    payload = {"tool": tool, "inputs": inputs}
    if target:
        payload["target_revision_id"] = target

    def handler(session, values):
        branch = state.require_branch(session, project["branch_id"])
        return 201, execute_calculation(state, session, branch, values, author="human", run_id=None)

    return state.execute("calculation.execute", key or str(uuid4()), payload, handler)[1]


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("1/3 + 2/5", "11/15"),
        ("-(7/9)/(14/3)", "-1/6"),
        ("(3/2)**-2", "4/9"),
        ("0", "0"),
        ("7-7", "0"),
        ("(+5)/(-10)", "-1/2"),
        ("2**16", "65536"),
    ],
)
def test_exact_rational_results_and_latex_artifacts(workspace, expression, expected):
    result = calculate(workspace, "rational_arithmetic", {"expression": expression})
    assert result["status"] == "ok"
    assert result["exact_result"]["value"] == expected
    assert result["tool_version"] == TOOL_VERSION
    assert result["execution"] == "actual_local_exact_calculation"
    assert result["runtime"]["network"] is False
    assert result["runtime"]["subprocess"] is False
    assert result["error"] is None and result["stdout"]
    assert result["input"] == {"expression": expression}
    state, _ = workspace
    with state.db.sessions() as session:
        artifact = session.get(Revision, result["revision_id"])
        assert "$" in artifact.body
        if "/" in expression:
            assert r"\frac" in artifact.body
        assert artifact.payload["candidate"] is True
        assert artifact.payload["simulated"] is False
        assert session.scalars(select(Adoption)).all() == []


@pytest.mark.parametrize(
    "left,right,identity",
    [
        ("(x+y)**2", "x**2+2*x*y+y**2", True),
        ("(x+y)**2", "x**2+y**2", False),
        ("x/2+x/3", "5*x/6", True),
        ("(x-y)*(x+y)", "x**2-y**2", True),
        ("x-x", "0", True),
    ],
)
def test_polynomial_coefficient_identity_is_exact_and_partial(workspace, left, right, identity):
    state, project = workspace
    result = calculate(
        workspace,
        "polynomial_identity",
        {"left": left, "right": right, "variables": ["x", "y"]},
        target=project["revision_id"],
    )
    assert result["status"] == "ok"
    assert result["exact_result"]["identity"] is identity
    assert bool(result["exact_result"]["difference"]) is not identity
    with state.db.sessions() as session:
        review = session.get(Review, result["review_id"])
        assert review.kind == "exact_computation" and review.coverage == "partial"
        assert review.verdict == ("passed" if identity else "issues")
        assert review.author == "exact_tool:polynomial_identity"
        assert review.dependency_snapshot[result["object_id"]] == result["revision_id"]
        assert review.target_revision_id == project["revision_id"]
        assert all("$" in finding for finding in review.findings)
        assert session.scalars(select(Adoption)).all() == []


@pytest.mark.parametrize(
    "expression,error",
    [
        ("1/0", "division_by_zero"),
        ("0**0", "indeterminate_zero_power"),
        ("2**17", "exponent_limit_exceeded"),
        ("2**(2**3)", "literal_integer_exponent_required"),
        ("9" * 101, "integer_literal_limit_exceeded"),
        ("(" * 25 + "1" + ")" * 25, "expression_depth_exceeded"),
        ("9" * 100 + "**16", "intermediate_integer_limit_exceeded"),
        ("1.5+2", "integer_literals_only"),
        ("1e100+2", "integer_literals_only"),
        ("__import__('os').system('never-run')", "undeclared_variable"),
        ("[1, 2][0]", "unsupported_operator"),
        ("(lambda: 1)()", "undeclared_variable"),
    ],
)
def test_invalid_calculations_are_visible_error_artifacts_not_passing_evidence(
    workspace, expression, error
):
    state, project = workspace
    result = calculate(
        workspace, "rational_arithmetic", {"expression": expression}, target=project["revision_id"]
    )
    assert result["status"] == "error" and result["error"] == error
    assert result["exact_result"] is None and result["stdout"] == ""
    assert result["runtime"]["operation_count"] <= LIMITS["operation_count"]
    with state.db.sessions() as session:
        review = session.get(Review, result["review_id"])
        assert review.verdict == "inconclusive" and review.coverage == "partial"
        assert session.scalars(select(Adoption)).all() == []


@pytest.mark.parametrize(
    "left,variables,error",
    [
        ("x**-1", ["x"], "polynomial_negative_power"),
        ("x/(x+1)", ["x"], "nonconstant_polynomial_denominator"),
        ("(x**10)*(x**10)", ["x"], "polynomial_degree_limit_exceeded"),
        ("(x+y+z+w)**8", ["x", "y", "z", "w"], "polynomial_term_limit_exceeded"),
        ("z+x", ["x"], "undeclared_variable"),
    ],
)
def test_polynomial_growth_and_domain_limits(workspace, left, variables, error):
    result = calculate(
        workspace, "polynomial_identity", {"left": left, "right": "0", "variables": variables}
    )
    assert result["status"] == "error" and result["error"] == error
    assert result["runtime"]["operation_count"] <= LIMITS["operation_count"]


def test_large_literal_and_deep_parentheses_are_rejected_before_ast_parse(workspace, monkeypatch):
    def deny_parse(*_args, **_kwargs):
        pytest.fail("Oversized source must be bounded before AST construction")

    monkeypatch.setattr(ast, "parse", deny_parse)
    for expression in ["9" * 101, "(" * 25 + "1" + ")" * 25]:
        result = calculate(workspace, "rational_arithmetic", {"expression": expression})
        assert result["status"] == "error"
        assert result["runtime"]["operation_count"] == 0


def test_ast_node_count_is_limited_before_interpretation(workspace):
    expressions = ["1"] * 64
    while len(expressions) > 1:
        expressions = [
            "(" + expressions[i] + "+" + expressions[i + 1] + ")"
            for i in range(0, len(expressions), 2)
        ]
    result = calculate(workspace, "rational_arithmetic", {"expression": expressions[0]})
    assert result["error"] == "ast_node_limit_exceeded"
    assert result["runtime"]["operation_count"] == 0


@pytest.mark.parametrize(
    "tool,inputs",
    [
        ("python", {"expression": "1"}),
        ("rational_arithmetic", {"expression": "1", "execute": True}),
        ("rational_arithmetic", {"expression": "1" * 4097}),
        ("polynomial_identity", {"left": "x", "right": "x", "variables": ["x"] * 2}),
        (
            "polynomial_identity",
            {"left": "x", "right": "x", "variables": ["x", "y", "z", "w", "v"]},
        ),
        ("hermitian_crossing", {"matrix": [[1, 0], [0, 1]]}),
    ],
)
def test_invalid_envelope_is_rejected_before_creating_artifact(workspace, tool, inputs):
    state, _ = workspace
    with state.db.sessions() as session:
        before = len(session.scalars(select(ResearchObject)).all())
    with pytest.raises(DomainError) as caught:
        calculate(workspace, tool, inputs)
    assert caught.value.status == 422
    with state.db.sessions() as session:
        assert len(session.scalars(select(ResearchObject)).all()) == before


def test_target_cannot_reference_another_project(workspace):
    state, _ = workspace
    _, other = state.execute(
        "project.create",
        str(uuid4()),
        {"title": "Other", "body": "Another target"},
        state.create_project,
    )
    with pytest.raises(DomainError) as caught:
        calculate(
            workspace, "rational_arithmetic", {"expression": "1+1"}, target=other["revision_id"]
        )
    assert caught.value.status == 422
    with state.db.sessions() as session:
        assert session.scalars(select(Review)).all() == []


def test_exact_target_and_proof_dependencies_remain_pinned_after_a_revision(workspace):
    state, project = workspace

    def command(name, payload, handler):
        return state.execute(name, str(uuid4()), payload, handler)[1]

    claim = command(
        "object.create",
        {"branch_id": project["branch_id"], "kind": "claim", "body": "$x=x$", "payload": {}},
        state.create_object,
    )
    premise = command(
        "object.create",
        {
            "branch_id": project["branch_id"],
            "kind": "context",
            "body": "旧定义。",
            "payload": {"role": "definition"},
        },
        state.create_object,
    )
    proof = command(
        "proof.create",
        {
            "branch_id": project["branch_id"],
            "conclusion_revision_id": claim["revision_id"],
            "body": "检查定义。",
            "context_revision_ids": [premise["revision_id"]],
            "premise_revision_ids": [],
            "assumption_revision_ids": [],
            "gaps": [],
            "rule": "direct",
        },
        state.create_proof,
    )
    command(
        "object.revise",
        {
            "object_id": premise["object_id"],
            "branch_id": project["branch_id"],
            "expected_revision_id": premise["revision_id"],
            "body": "新定义。",
            "read_set": {},
        },
        state.revise_object,
    )
    result = calculate(
        workspace, "rational_arithmetic", {"expression": "1/2+1/2"}, target=proof["revision_id"]
    )
    with state.db.sessions() as session:
        review = session.get(Review, result["review_id"])
        assert review.dependency_snapshot[premise["object_id"]] == premise["revision_id"]
        assert review.dependency_snapshot[claim["object_id"]] == claim["revision_id"]
        assert review.target_revision_id == proof["revision_id"]


def test_hermitian_family_stays_fixed_and_exact(workspace):
    result = calculate(workspace, "hermitian_crossing", {})
    assert result["status"] == "ok"
    assert result["exact_result"]["sorted_eigenvalues"] == ["-|t|", "|t|"]
    assert result["exact_result"]["one_sided_derivatives_at_zero"] == {
        "left": ["1", "-1"],
        "right": ["-1", "1"],
    }
    assert r"$H(t)=\begin{pmatrix}" in result["scope"]


def test_command_retry_preserves_one_actual_artifact_and_one_review(workspace):
    state, project = workspace
    key = str(uuid4())
    first = calculate(
        workspace,
        "rational_arithmetic",
        {"expression": "1/3+2/3"},
        target=project["revision_id"],
        key=key,
    )
    second = calculate(
        workspace,
        "rational_arithmetic",
        {"expression": "1/3+2/3"},
        target=project["revision_id"],
        key=key,
    )
    assert first == second
    with state.db.sessions() as session:
        assert len(session.scalars(select(Review)).all()) == 1
        assert (
            len(
                session.scalars(
                    select(ResearchObject).where(ResearchObject.kind == "artifact")
                ).all()
            )
            == 1
        )
