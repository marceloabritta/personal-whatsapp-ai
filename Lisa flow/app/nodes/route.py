"""route — the programmatic domain orchestrator.

Decides which skill serves this turn in CODE (skills.router.route_domain): each skill's cheap
matcher runs first; an LLM classifier resolves only the ambiguous band; web is the safe default.
Sets `state["domain"]`, which `reason` reads to pick the skill's prompt, schema, and tools."""
from __future__ import annotations

from ..skills.router import route_domain
from ..state import MessageState
from ..trace import Trace


async def route_node(state: MessageState, *, settings, reasoner, trace: Trace) -> dict:
    tid = state["trace_id"]
    domain, how = await route_domain(state, settings, reasoner=reasoner)
    trace.code(
        tid, node="route", loop_id=state.get("loop_id"),
        domain=domain, how=how, text_preview=(state.get("text") or "")[:80],
    )
    # Remember the loop's domain so a later continuation sticks to it (see route_domain step 2).
    return {"domain": domain, "loop_domain": domain}
