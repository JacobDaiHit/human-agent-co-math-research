"""Stable request contract for the bounded-search solver controller."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SearchConfig(BaseModel):
    """Frozen per-run limits for ``bounded_search_v1``.

    Admission against the root request/token budget belongs to the controller;
    this model only validates relations internal to the search configuration.
    """

    model_config = ConfigDict(extra="forbid")

    max_routes: int = Field(default=3, ge=1, le=3)
    active_routes: int = Field(default=2, ge=1, le=2)
    max_route_steps: int = Field(default=3, ge=1, le=8)
    max_repairs: int = Field(default=1, ge=0, le=2)
    final_output_tokens: int = Field(default=4096, ge=256)
    final_requests: int = Field(default=1, ge=1, le=3)
    check_requests: int = Field(default=3, ge=0, le=12)
    check_output_tokens: int = Field(default=12288, ge=0)
    # ``fixed`` preserves the original per-pool allocation.  Adaptive runs
    # account against the root total and retain only the delivery/check floors.
    budget_policy: Literal["fixed", "adaptive"] = "adaptive"
    min_work_output_tokens: int = Field(default=4096, ge=256)
    min_check_output_tokens: int = Field(default=8192, ge=256)
    analysis_output_tokens: int = Field(default=8192, ge=256)
    deadline_seconds: int = Field(default=1800, ge=1)
    final_seconds: int = Field(default=90, ge=1)
    enable_repairs: bool = True
    enable_tools: bool = True
    enable_memory: bool = True
    enable_multi_route: bool = True
    route_schedule: Literal["first_pass", "evidence_first"] = "first_pass"
    require_passed_check_for_final: bool = True

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.active_routes > self.max_routes:
            raise ValueError("active_routes cannot exceed max_routes")
        if self.final_seconds >= self.deadline_seconds:
            raise ValueError("final_seconds must leave time before deadline_seconds")
        return self
