import json

import pytest
from mathagent.api.agent_routes import PolicyUpdate, RunUpdate
from mathagent.api.schemas import RunCreate
from mathagent.providers.actions import OPERATION_MODELS, operation_schemas
from mathagent.providers.protocol import PROMPT_VERSION, messages_for, validate_result
from mathagent.providers.search_contract import SearchConfig
from pydantic import ValidationError


def test_search_config_defaults_and_cross_field_limits():
    config = SearchConfig()
    assert config.max_routes == 3
    assert config.active_routes == 2
    assert config.final_output_tokens == 4096
    assert config.final_seconds == 90
    with pytest.raises(ValidationError, match="active_routes"):
        SearchConfig(max_routes=1, active_routes=2)
    with pytest.raises(ValidationError, match="final_seconds"):
        SearchConfig(deadline_seconds=90, final_seconds=90)


def test_run_models_accept_controller_and_sparse_updates():
    created = RunCreate(branch_id="branch", goal_object_id="goal", solver_controller="bounded_search_v1")
    assert created.search_config == SearchConfig()
    update = RunUpdate(search_config={"max_routes": 2, "active_routes": 2})
    assert update.model_dump(exclude_unset=True) == {
        "search_config": {"max_routes": 2, "active_routes": 2}
    }
    assert RunUpdate(cumulative_output_token_budget=None).model_fields_set == {
        "cumulative_output_token_budget"
    }
    assert PolicyUpdate(allowed_operations=list(OPERATION_MODELS)).allowed_operations


def test_search_actions_are_published_with_their_contracts():
    schemas = operation_schemas()
    assert {"propose_routes", "report_progress", "report_gap", "propose_check", "request_memory", "share_memory"} <= set(schemas)
    routes = OPERATION_MODELS["propose_routes"].model_validate({"routes": [{
        "title": "Factor", "core_reduction": "Reduce $n$.", "key_lemmas": ["$L$"],
        "assumptions": [], "subgoal": "Prove $L$.", "cheap_check": "Check $n=1$."
    }]})
    assert routes.routes[0].title == "Factor"
    with pytest.raises(ValidationError):
        OPERATION_MODELS["propose_check"].model_validate({
            "target_revision_id": "r", "statement": "x", "scope": "s", "tool": "network",
        })


def test_protocol_exposes_search_material_only_for_controlled_tasks():
    base = {"mode": "research", "goal_object_id": "goal", "instruction": "Research", "inputs": [],
            "autonomous": True, "operation_schemas": {"propose_routes": {"type": "object"}}}
    legacy = messages_for(base)
    assert "bounded_search_v1 控制器" not in legacy[0]["content"]
    assert "search_context" not in json.loads(legacy[1]["content"])

    controlled = messages_for({**base, "search_context": {"route_id": "route"},
                               "memory_packet": {"entries": []}})
    payload = json.loads(controlled[1]["content"])
    assert PROMPT_VERSION == "research-operations-v12"
    assert payload["search_context"] == {"route_id": "route"}
    assert payload["memory_packet"] == {"entries": []}
    assert "bounded_search_v1 控制器" in controlled[0]["content"]


def test_only_reviews_may_attach_structured_gaps():
    review = validate_result({
        "mode": "review", "body": "There is a gap.", "findings": ["A step is missing."],
        "scope": "One local implication.", "verdict": "issues",
        "structured_gaps": [{"kind": "missing_argument", "anchor": "step-2",
                             "detail": "The implication needs a proof."}],
    }, mode="review", read_set={})
    assert review.structured_gaps[0].kind == "missing_argument"
    with pytest.raises(ValidationError, match="Only reviews"):
        validate_result({
            "mode": "research", "body": "Candidate.", "findings": ["Need work."],
            "structured_gaps": [{"kind": "missing_argument", "anchor": "step-2", "detail": "Missing."}],
        }, mode="research", read_set={})
