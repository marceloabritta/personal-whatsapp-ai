"""Provider-neutral reasoning. The graph imports only `Reasoner` / `build_reasoner`;
concrete providers (Anthropic today) live behind the seam and are chosen by env."""
from __future__ import annotations

from .base import Reasoner, ReasonResult


def build_reasoner(settings) -> Reasoner:
    provider = (settings.llm_provider or "anthropic").lower()
    if provider == "anthropic":
        from .anthropic import AnthropicReasoner

        return AnthropicReasoner(settings)
    raise ValueError(f"unknown LLM_PROVIDER: {provider!r}")


__all__ = ["Reasoner", "ReasonResult", "build_reasoner"]
