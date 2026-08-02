"""The reasoning seam. The graph depends only on this — never on a provider SDK.

The enforced-JSON schema is built from the tool registry and injected at construction
(so adding a tool reshapes the model's contract without touching the reasoner)."""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict


class ReasonResult(TypedDict, total=False):
    lang: Optional[str]
    next_message: Optional[str]  # what to post to the chat, or None
    loop_state: str  # "keep_listening" | "close_loop"
    actions: list  # [{task, inputs}, ...] — 0..N, may span domains
    workflow: Optional[dict]  # {task, known_inputs, open_questions} or None
    usage: Optional[dict]
    provider_request_id: Optional[str]
    stop_reason: Optional[str]
    tool_calls: list  # names of server/MCP tools used this turn
    error_category: str


class Reasoner(Protocol):
    async def respond(self, *, system: str, messages: list) -> ReasonResult:
        """Given the system prompt and a neutral [{role, content}] history, return the
        enforced-JSON decision (message + loop_state + actions + workflow) plus metadata."""
        ...
