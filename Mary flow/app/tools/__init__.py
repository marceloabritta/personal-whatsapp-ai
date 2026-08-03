"""Lisa's tool framework.

One declarative registry (`registry.TOOLS`) is fanned out into every seam the runtime
needs — the enforced-JSON output schema, the system-prompt tool list, the per-tool task
prompts, the MCP connectors, and the local handler instances. Add a tool by adding one
entry; nothing else in the graph changes.

Two tool types:
  - "local"        — a hardcoded API handler run inside the graph's execute node
                     (Google Calendar). Contributes its verbs to the output schema.
  - "anthropic_mcp" — a URL connector Claude calls inline; no local handler.
"""
from __future__ import annotations

from .base import ActionResult, ToolHandler

__all__ = ["ActionResult", "ToolHandler"]
