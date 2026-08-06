"""respond — skill-owned, per-verb reply assembly (+ the single-match auto-resolve).

After `execute`, the routed skill's render policy for the executed VERB decides how the result
becomes a message:

  Programmatic → build the reply from the result in code, on success, in a supported language → act.
  LLMReadback  → route the result back into reason ② (the model writes it) — for `find`, any
                 failure, or an unsupported language.

Single-match auto-resolve: when a `find` was a step toward an update/delete (the model recorded
the intent in `workflow`) and it resolved to exactly one event, this node assembles the write in
code and routes to `confirm` — skipping reason ②. Best-effort; anything it can't build falls to
reason ②."""
from __future__ import annotations

from ..skills.calendar_format import LANGS
from ..state import MessageState
from ..trace import Trace

def _build_write_from_find(state: MessageState) -> dict | None:
    """A single-match find + a recorded DELETE intent → the resolved delete action, or None.

    Only `delete` auto-resolves: it needs nothing but the id, so a single match goes straight to
    the cancel confirmation with no reason ②. `update` is intentionally NOT auto-resolved here — its
    edits (a new title, added guests, a video call) are structured fields that don't survive being
    carried as workflow string values; those go through the model's structured proposal (reason ②)."""
    wf = state.get("workflow") or {}
    if wf.get("task") != "calendar.delete":
        return None
    results = state.get("last_results") or []
    items = ((results[0].get("data") or {}).get("items")) if results else None
    if not items or len(items) != 1:
        return None
    eid = items[0].get("event_id")
    if not eid:
        return None
    return {"task": "calendar.delete", "event_id": eid, "confirmed": False}


async def respond_node(
    state: MessageState, *, render_policies: dict, settings, trace: Trace
) -> dict:
    tid = state["trace_id"]
    domain = state.get("domain") or ""
    results = state.get("last_results") or []
    ran = int(state.get("last_ran") or 0)
    hops = int(state.get("tool_hops") or 0)
    lang = (state.get("session_lang") or state.get("lang") or "")[:2].lower()
    verb = ""
    if results:
        _, _, verb = (results[0].get("task") or "").partition(".")

    # Single-match auto-resolve: a find done toward a write, one match → propose the write in code.
    if verb == "find" and ran:
        action = _build_write_from_find(state)
        if action is not None:
            trace.code(tid, node="respond", loop_id=state.get("loop_id"), domain=domain,
                       verb=verb, auto="single-match", route="confirm")
            return {"actions": [action], "workflow": None, "respond_route": "confirm"}

    render_map = render_policies.get(domain)
    policy = render_map.get(verb) if isinstance(render_map, dict) else render_map
    all_ok = bool(results) and all(r.get("ok") for r in results)

    update: dict = {}
    if policy is None:
        route = "act"
    elif getattr(policy, "mode", "llm") == "code" and all_ok and lang in LANGS:
        update["reply_body"] = await policy.assemble(results=results, state=state)
        route = "act"
    else:  # LLMReadback, a failure, or an unsupported language → the model reads back
        route = "reason" if (ran and hops < settings.max_tool_actions) else "act"

    trace.code(
        tid, node="respond", loop_id=state.get("loop_id"), domain=domain, verb=verb,
        render=type(policy).__name__ if policy else None, ran=ran, route=route,
    )
    update["respond_route"] = route
    return update


def route_after_respond(state: MessageState) -> str:
    return state.get("respond_route") or "act"
