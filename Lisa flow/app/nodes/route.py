"""route — the programmatic domain orchestrator.

Decides which skill serves this turn in CODE (skills.router.route_domain): each skill's cheap
matcher runs first; an LLM classifier resolves only the ambiguous band; web is the safe default.
Sets `state["domain"]`, which `reason` reads to pick the skill's prompt, schema, and tools."""
from __future__ import annotations

from ..intent import detect_language
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
    update = {"domain": domain, "loop_domain": domain}

    # Setup's language follows the OWNER's own words, decided in code, not the model's reading of
    # the surrounding history. In the self-chat that history is mostly Portuguese, so a
    # conversation opened with "@lisa setup" came back in Portuguese — and the programmatic
    # renderers, which read session_lang, then disagreed with the parts written in English.
    # Start in English; move to Portuguese the moment he writes it; hold the last known language
    # through messages that carry no signal ("1", "ok").
    if domain == "setup":
        detected = detect_language(state.get("text") or "")
        update["session_lang"] = detected or state.get("session_lang") or "en"
        trace.code(tid, node="route", loop_id=state.get("loop_id"),
                   setup_lang=update["session_lang"], detected=detected)

    # Remember the loop's domain so a later continuation sticks to it (see route_domain step 2).
    return update
