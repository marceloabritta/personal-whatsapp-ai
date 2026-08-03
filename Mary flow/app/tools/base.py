"""The tool seam. The graph depends only on these shapes — never on a concrete API SDK.

A local tool is any object with an async `run(verb, inputs) -> ActionResult`. It must never
raise into the graph: every failure comes back as an ActionResult with `ok=False` and a
classified `error`, so the reasoner can read it back and speak truthfully."""
from __future__ import annotations

from typing import Any, Optional, Protocol, TypedDict


class ActionResult(TypedDict, total=False):
    ok: bool                 # did the action succeed?
    summary: str             # one line the model reads back (appended to the thread)
    data: Optional[dict]     # structured payload, e.g. {"items": [...]}, {"event_id": ...}
    error: Optional[str]     # classification when not ok: "auth" | "not_found" | "validation" | str


class ToolHandler(Protocol):
    async def run(self, verb: str, inputs: dict[str, Any]) -> ActionResult:
        """Execute one verb of this tool's domain. Never raises — returns an ActionResult."""
        ...
