"""Tool primitives shared by every domain.

A LOCAL tool is a handler we run ourselves (dispatched by the execute node). An
ANTHROPIC_MCP tool has no handler — its registry entry is adapted onto the model's
MCP connector and Claude calls it inline. Both are declared in one registry (registry.py)."""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict


class ActionResult(TypedDict, total=False):
    ok: bool
    summary: str  # human-readable, appended to the thread for the read-back
    data: Optional[dict]  # e.g. {event_id, html_link, meet_link, items}
    error: Optional[str]  # "validation" | "auth" | "not_found" | "<msg>"
    need: Optional[str]  # a field the model must still supply


class LocalTool(Protocol):
    async def run(self, verb: str, inputs: dict) -> ActionResult:
        """Run one verb of this domain and return a compact result."""
        ...
