"""Semantic regression cases for the M1 evidence and version support policy."""

from copy import deepcopy

import pytest
from mathagent.domain.support import analyze_support


def node(kind="claim", adoption="adopted", current=True, **payload):
    return {
        "object_id": "object",
        "kind": kind,
        "adoption_state": adoption,
        "is_current": current,
        "evidence": [],
        "payload": payload,
    }


def review(
    kind="human_review",
    verdict="passed",
    coverage="whole_plan",
    scope="All steps and assumptions checked",
):
    return {"kind": kind, "verdict": verdict, "scope": scope, "coverage": coverage}


def add_plan(
    nodes, plans, plan_id, conclusion, premises=(), contexts=(), assumptions=(), **changes
):
    nodes[plan_id] = node("argument")
    nodes[plan_id]["evidence"] = [review()]
    plan = {
        "revision_id": plan_id,
        "conclusion_revision_id": conclusion,
        "premise_revision_ids": list(premises),
        "context_revision_ids": list(contexts),
        "assumption_revision_ids": list(assumptions),
        "body": "The recorded direct derivation covers the precise statement and its declared assumptions.",
        "gaps": [],
        "rule": "direct",
    }
    plan.update(changes)
    plans.append(plan)
    return plan


def graph():
    return {key: node() for key in ("A", "B", "C", "D", "E")}, []


def test_deleted_evidence_cannot_support_a_claim_even_with_old_adoption():
    nodes, plans = graph()
    nodes["Context"] = node("context", role="definition", deleted=True)
    add_plan(nodes, plans, "PA", "A", contexts=["Context"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] != "supported"
    assert any("deleted" in reason for reason in result["plans"]["PA"]["reasons"])


def test_adoption_and_bare_claim_reviews_are_not_proof_roots():
    nodes, plans = graph()
    nodes["A"]["evidence"] = [review(), review("exact_computation")]
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "no_current_support"
    assert result["claims"]["A"]["plan_revision_ids"] == []


@pytest.mark.parametrize(
    "kind", ["human_review", "llm_review", "exact_computation", "formal_check"]
)
def test_actual_reviewed_direct_plan_can_be_a_root(kind):
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A")
    nodes["PA"]["evidence"] = [review(kind)]
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "supported"
    assert result["claims"]["A"]["plan_revision_ids"] == ["PA"]
    assert "not a mathematical truth" in result["claims"]["A"]["reasons"][0]


@pytest.mark.parametrize(
    "evidence",
    [
        review("numerical_experiment"),
        review("candidate_proof"),
        review(coverage="partial"),
        review(scope="  "),
        review(verdict="inconclusive"),
        {"kind": "human_review", "verdict": "passed", "scope": "Whole plan"},
    ],
)
def test_insufficient_evidence_never_establishes_support(evidence):
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A")
    nodes["PA"]["evidence"] = [evidence]
    assert analyze_support(nodes, plans)["claims"]["A"]["status"] == "needs_recheck"


def test_plan_premises_are_and_alternatives_are_or():
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A")
    add_plan(nodes, plans, "PD", "D")
    add_plan(nodes, plans, "P1", "C", ["A", "B"])
    add_plan(nodes, plans, "P2", "C", ["D"])
    result = analyze_support(nodes, plans)
    assert result["plans"]["P1"]["status"] == "no_current_support"
    assert result["claims"]["C"]["status"] == "supported"
    assert result["claims"]["C"]["plan_revision_ids"] == ["P2"]
    nodes["P2"]["adoption_state"] = "withdrawn"
    result = analyze_support(nodes, plans)
    assert result["plans"]["P2"]["status"] == "withdrawn"
    assert result["claims"]["C"]["status"] == "no_current_support"


def test_alternative_support_stops_transitive_invalidation():
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A")
    add_plan(nodes, plans, "PD", "D")
    add_plan(nodes, plans, "P1", "C", ["A"])
    add_plan(nodes, plans, "P2", "C", ["D"])
    add_plan(nodes, plans, "P3", "E", ["C"])
    nodes["A"]["is_current"] = False
    nodes["A_v2"] = node()
    result = analyze_support(nodes, plans)
    assert result["plans"]["P1"]["status"] == "needs_recheck"
    assert result["claims"]["A_v2"]["status"] == "no_current_support"
    assert result["claims"]["C"]["status"] == "supported"
    assert result["claims"]["E"]["status"] == "supported"
    nodes["P2"]["adoption_state"] = "withdrawn"
    result = analyze_support(nodes, plans)
    assert result["claims"]["C"]["status"] == "needs_recheck"
    assert result["claims"]["E"]["status"] == "needs_recheck"


def test_withdrawal_propagates_absence_of_support_without_claiming_falsehood():
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A")
    add_plan(nodes, plans, "PC", "C", ["A"])
    add_plan(nodes, plans, "PE", "E", ["C"])
    nodes["A"]["adoption_state"] = "withdrawn"
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "withdrawn"
    assert result["claims"]["C"]["status"] == "no_current_support"
    assert result["claims"]["E"]["status"] == "no_current_support"


def test_draft_claims_and_draft_arguments_are_not_upgraded():
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A")
    add_plan(nodes, plans, "PB", "B")
    nodes["A"]["adoption_state"] = "draft"
    nodes["PB"]["adoption_state"] = "draft"
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "draft"
    assert result["plans"]["PB"]["status"] == "draft"
    assert result["claims"]["B"]["status"] == "no_current_support"


@pytest.mark.parametrize("adoption", ["draft", "pending_review", "withdrawn"])
@pytest.mark.parametrize("problem", ["stale", "missing", "invalid_kind"])
def test_invalid_dependencies_require_recheck_even_for_drafts_without_changing_adoption(
    adoption, problem
):
    nodes, plans = graph()
    add_plan(nodes, plans, "PB", "B", premises=["A"])
    nodes["PB"]["adoption_state"] = adoption
    nodes["PB"]["evidence"] = []
    if problem == "stale":
        nodes["A"]["is_current"] = False
    elif problem == "missing":
        del nodes["A"]
    else:
        nodes["A"]["kind"] = "activity"
    before = deepcopy(nodes)
    result = analyze_support(nodes, plans)
    assert result["plans"]["PB"]["status"] == (
        "withdrawn" if adoption == "withdrawn" else "needs_recheck"
    )
    assert any("A:" in reason for reason in result["plans"]["PB"]["reasons"])
    assert nodes == before
    assert nodes["PB"]["adoption_state"] == adoption


@pytest.mark.parametrize("target", ["A", "PA"])
def test_disputed_adoption_and_conflicting_reviews_block_support(target):
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A")
    nodes[target]["adoption_state"] = "disputed"
    assert analyze_support(nodes, plans)["claims"]["A"]["status"] == "needs_recheck"
    nodes[target]["adoption_state"] = "adopted"
    nodes[target]["evidence"].append(review(verdict="issues", coverage="partial"))
    assert analyze_support(nodes, plans)["claims"]["A"]["status"] == "needs_recheck"


def test_explicit_draft_assumption_remains_conditional_transitively():
    nodes, plans = graph()
    nodes["Q"] = node(adoption="draft")
    add_plan(nodes, plans, "PA", "A", premises=["Q"], assumptions=["Q"])
    add_plan(nodes, plans, "PC", "C", ["A"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["Q"]["status"] == "draft"
    for claim in ("A", "C"):
        assert result["claims"][claim]["status"] == "conditional"
        assert result["claims"][claim]["conditions"] == ["Q"]


def test_default_assumption_context_and_definition_context_have_distinct_roots():
    nodes, plans = graph()
    nodes["Q"] = node("context")
    nodes["Def"] = node("context", role="definition")
    add_plan(nodes, plans, "PA", "A", contexts=["Q", "Def"])
    add_plan(nodes, plans, "PB", "B", contexts=["Def"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["conditions"] == ["Q"]
    assert result["claims"]["A"]["status"] == "conditional"
    assert result["claims"]["B"]["status"] == "supported"
    nodes["Def"]["adoption_state"] = "draft"
    assert analyze_support(nodes, plans)["claims"]["B"]["status"] == "no_current_support"


@pytest.mark.parametrize(
    "payload", [{"role": []}, {"role": {}}, {"role": None}, {"role": "axiom"}, None]
)
def test_malformed_context_roles_require_recheck_without_crashing(payload):
    nodes, plans = graph()
    nodes["Context"] = node("context")
    nodes["Context"]["payload"] = payload
    add_plan(nodes, plans, "PA", "A", contexts=["Context"])
    add_plan(nodes, plans, "PB", "B", assumptions=["Context"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "needs_recheck"
    assert result["claims"]["B"]["status"] == "needs_recheck"


def test_or_assumptions_are_not_unioned_and_shared_routes_can_be_selected():
    nodes, plans = graph()
    nodes["Q"] = node("context")
    nodes["R"] = node("context")
    add_plan(nodes, plans, "PA_Q", "A", assumptions=["Q"])
    add_plan(nodes, plans, "PA_R", "A", assumptions=["R"])
    add_plan(nodes, plans, "PB_R", "B", assumptions=["R"])
    add_plan(nodes, plans, "PC", "C", ["A", "B"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["conditions"] == ["Q"]
    assert result["claims"]["A"]["plan_revision_ids"] == ["PA_Q"]
    assert result["claims"]["C"]["conditions"] == ["R"]
    add_plan(nodes, plans, "PA_unconditional", "A")
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "supported"
    assert result["claims"]["A"]["conditions"] == []


@pytest.mark.parametrize(
    "change",
    [{"is_current": False}, {"adoption_state": "withdrawn"}, {"adoption_state": "disputed"}],
)
def test_unusable_explicit_assumptions_require_recheck(change):
    nodes, plans = graph()
    nodes["Q"] = node(adoption="draft")
    nodes["Q"].update(change)
    add_plan(nodes, plans, "PA", "A", assumptions=["Q"])
    assert analyze_support(nodes, plans)["claims"]["A"]["status"] == "needs_recheck"


def test_cycles_do_not_bootstrap_and_independent_anchors_remain_usable():
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A", ["B"])
    add_plan(nodes, plans, "PB", "B", ["A"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "no_current_support"
    assert result["claims"]["B"]["status"] == "no_current_support"
    add_plan(nodes, plans, "PA_root", "A")
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["plan_revision_ids"] == ["PA_root"]
    assert result["claims"]["B"]["status"] == "supported"
    assert result == analyze_support(nodes, list(reversed(plans)))


def test_conditional_anchor_cannot_be_laundered_through_cycle():
    nodes, plans = graph()
    nodes["Q"] = node("context")
    add_plan(nodes, plans, "PA", "A", ["B"])
    add_plan(nodes, plans, "PB", "B", ["A"])
    add_plan(nodes, plans, "PA_root", "A", assumptions=["Q"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["conditions"] == ["Q"]
    assert result["claims"]["B"]["conditions"] == ["Q"]
    assert result["claims"]["A"]["status"] == "conditional"


def test_shorter_route_is_retained_when_consumer_already_assumes_extra_condition():
    nodes, plans = graph()
    nodes["Q"] = node("context")
    nodes["R"] = node("context")
    add_plan(nodes, plans, "PB_short", "B", assumptions=["Q", "R"])
    add_plan(nodes, plans, "PA", "A", ["B"], assumptions=["R"])
    add_plan(nodes, plans, "PC", "C", assumptions=["Q"])
    add_plan(nodes, plans, "PD", "D", ["C"])
    add_plan(nodes, plans, "PB_long", "B", ["D"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["B"]["conditions"] == ["Q"]
    assert result["claims"]["A"]["conditions"] == ["Q", "R"]
    assert result["claims"]["A"]["plan_revision_ids"] == ["PA"]
    assert result == analyze_support(nodes, list(reversed(plans)))


@pytest.mark.parametrize(
    "change",
    [
        {"rule": "induction"},
        {"gaps": ["limit exchange"]},
        {"body": "  "},
        {"premise_revision_ids": ["missing"]},
    ],
)
def test_incomplete_or_unsupported_plans_are_saved_but_need_recheck(change):
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A", **change)
    assert analyze_support(nodes, plans)["claims"]["A"]["status"] == "needs_recheck"


def test_nonmathematical_artifacts_cannot_be_premises_or_assumptions():
    nodes, plans = graph()
    nodes["artifact"] = node("artifact")
    add_plan(nodes, plans, "PA", "A", premises=["artifact"])
    add_plan(nodes, plans, "PB", "B", assumptions=["artifact"])
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "needs_recheck"
    assert result["claims"]["B"]["status"] == "needs_recheck"


def test_review_does_not_migrate_to_new_argument_revision():
    nodes, plans = graph()
    add_plan(nodes, plans, "PA_v1", "A")
    add_plan(nodes, plans, "PA_v2", "A")
    nodes["PA_v1"]["is_current"] = False
    nodes["PA_v2"]["evidence"] = []
    result = analyze_support(nodes, plans)
    assert result["claims"]["A"]["status"] == "needs_recheck"


def test_analysis_is_pure_and_duplicate_plan_ids_fail_explicitly():
    nodes, plans = graph()
    add_plan(nodes, plans, "PA", "A")
    before = deepcopy((nodes, plans))
    analyze_support(nodes, plans)
    assert (nodes, plans) == before
    with pytest.raises(ValueError, match="unique"):
        analyze_support(nodes, plans + plans)
