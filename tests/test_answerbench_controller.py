"""Offline contract checks for the bounded solver controller configuration."""

import sys
import types

import pytest
from mathagent.evaluation.answerbench import Limits, instruction_for


def install_search_contract(monkeypatch):
    class SearchConfig:
        @classmethod
        def model_validate(cls, value):
            if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
                raise ValueError("invalid")
            return cls()

        def model_dump(self, mode="json"):
            return {"strategy": "bounded", "max_routes": 2}

    module = types.ModuleType("mathagent.providers.search_contract")
    module.SearchConfig = SearchConfig
    monkeypatch.setitem(sys.modules, module.__name__, module)


def test_legacy_controller_keeps_existing_defaults():
    limits = Limits()
    limits.validate()
    assert limits.solver_controller == "legacy"
    assert limits.search_config == {}


def test_bounded_instructions_delegate_reviews_and_limit_analysis_scope():
    text = instruction_for(Limits(solver_controller="bounded_search_v1"))
    assert "Analysis proposes route cards" in text
    assert "server dispatches independent reviews" in text
    assert "first save the" not in text
    assert "then request_review" not in text


def test_bounded_controller_requires_agent_output_cap_and_normalizes_config(monkeypatch):
    install_search_contract(monkeypatch)
    limits = Limits(solver_controller="bounded_search_v1", cumulative_output_token_budget=256,
                    search_config={"max_routes": 2})
    limits.validate()
    assert limits.search_config == {"strategy": "bounded", "max_routes": 2}

    with pytest.raises(ValueError, match="cumulative output"):
        Limits(solver_controller="bounded_search_v1").validate()
    with pytest.raises(ValueError, match="requires bounded_search_v1"):
        Limits(search_config={"x": 1}).validate()
