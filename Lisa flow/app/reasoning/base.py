"""The reasoning seam. The graph depends only on this — never on a provider SDK.

The enforced-JSON contract is no longer a frozen constant here: it is built from the tool
registry (tools.registry.build_output_schema) and injected into the reasoner, so registering
a tool extends the schema automatically. `reasoning` is FIRST so the model writes its
rationale before committing to a decision — it is the readable "why" for the durable
reasoning log (this model's thinking blocks come back redacted, so the rationale lives in the
output, not in a thinking block)."""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict


class ReasonResult(TypedDict, total=False):
    state: str  # "keep_listening" | "close"
    message: Optional[str]  # prose to send, or None (silence)
    lang: Optional[str]  # ISO 639-1 code the reply is written in
    reasoning: Optional[str]  # the model's stated rationale this turn, for the log
    actions: list  # calendar (etc.) actions the model wants run this turn; [] if none
    workflow: Optional[dict]  # persistent gather memory toward a goal, or None
    usage: Optional[dict]  # {"input": int, "output": int}
    provider_request_id: Optional[str]
    stop_reason: Optional[str]
    tool_calls: list  # names of server tools used this turn
    error_category: str  # "none" | "provider"


class Reasoner(Protocol):
    async def respond(self, *, system: str, messages: list) -> ReasonResult:
        """Given the system prompt and a neutral [{role, content}] history, return the
        enforced-JSON decision plus metadata. Tool use / thinking / the pause_turn loop
        are the provider's private concern."""
        ...
