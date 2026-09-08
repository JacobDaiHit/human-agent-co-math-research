"""Shared, explicit runtime limits for provider requests."""

from typing import Literal

# A project budget cap, below DeepSeek's documented 384K maximum output.
MAX_OUTPUT_TOKENS = 65_536
ThinkingMode = Literal["provider_default", "enabled", "disabled"]
ReasoningEffort = Literal["provider_default", "low", "high", "max"]
