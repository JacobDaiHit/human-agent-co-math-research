"""Shared, explicit runtime limits for provider requests."""

from typing import Literal

# Provider limit, not an estimate of how much mathematics a problem needs.
MAX_OUTPUT_TOKENS = 393_216
MAX_REQUEST_TIMEOUT_SECONDS = 3600
MAX_RESEARCH_SECONDS = 604800
ThinkingMode = Literal["provider_default", "enabled", "disabled"]
ReasoningEffort = Literal["provider_default", "low", "high", "max"]
