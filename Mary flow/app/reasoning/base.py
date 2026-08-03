"""The reasoning seam. The graph depends only on this — never on a provider SDK.

The enforced-JSON contract is no longer a frozen constant here: it is built per domain by the
skills registry (skills.output_schema_for) and passed to the reasoner each call, so each skill
gets its own schema. `reasoning` is FIRST so the model writes its
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
    async def respond(
        self, *, system: str, messages: list,
        output_schema: dict | None = None, server_tools: list | None = None,
        model: str | None = None, effort: str | None = None, think_budget: int = 0,
    ) -> ReasonResult:
        """Given the system prompt and a neutral [{role, content}] history, return the
        enforced-JSON decision plus metadata. `output_schema` is the per-call enforced-JSON
        contract (the routed skill's) and `server_tools` the native tools to attach this call
        (e.g. the web skill's) — both fall back to the reasoner's defaults when None. `model`,
        `effort`, `think_budget` are the routed skill's per-call runtime (settings defaults when
        None/0; think_budget > 0 enables thinking). Tool use / the pause_turn loop are the
        provider's private concern."""
        ...

    async def classify(
        self, *, system: str, text: str, schema: dict,
        max_tokens: int = 32, effort: str = "low",
    ) -> dict:
        """A single lightweight enforced-JSON call (no tools) returning the raw parsed object —
        used by the router's domain classifier. Raises on any transport/parse failure so the
        caller can fall back to a default."""
        ...
