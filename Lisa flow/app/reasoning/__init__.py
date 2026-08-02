"""Provider-neutral reasoning. The graph imports only `Reasoner` / `build_reasoner`;
concrete providers (Anthropic today) live behind the seam and are chosen by env."""
from __future__ import annotations

from .base import Reasoner, ReasonResult


def build_reasoner(settings, output_schema: dict | None = None,
                   mcp_servers: list[dict] | None = None) -> Reasoner:
    """Build the provider's reasoner. The enforced-JSON `output_schema` and MCP connectors
    are built from the tool registry (in deps) and injected here; when omitted, the provider
    builds the schema itself from the registry."""
    provider = (settings.llm_provider or "anthropic").lower()
    if provider == "anthropic":
        from .anthropic import AnthropicReasoner

        return AnthropicReasoner(settings, output_schema=output_schema, mcp_servers=mcp_servers)
    raise ValueError(f"unknown LLM_PROVIDER: {provider!r}")


__all__ = ["Reasoner", "ReasonResult", "build_reasoner"]
