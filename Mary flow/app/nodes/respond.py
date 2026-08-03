"""respond — the skill-owned reply assembly.

After `execute` runs a skill's action, how the result becomes the WhatsApp message is the routed
skill's `render` policy, not a graph default:

  LLMReadback  → route the result back into a second reason call (reason ②), which writes the
                 reply from what actually happened. This is calendar's behaviour today.
  Programmatic → build the reply string here from the results (no second model call), then act.

Bounded by `max_tool_actions` so the readback loop can never spin."""
from __future__ import annotations

from ..state import MessageState
from ..trace import Trace


async def respond_node(
    state: MessageState, *, render_policies: dict, settings, trace: Trace
) -> dict:
    tid = state["trace_id"]
    domain = state.get("domain") or ""
    policy = render_policies.get(domain)
    ran = int(state.get("last_ran") or 0)
    hops = int(state.get("tool_hops") or 0)

    update: dict = {}
    if policy is None:
        route = "act"
    elif getattr(policy, "mode", "llm") == "code":
        reply = await policy.assemble(results=state.get("last_results") or [], state=state)
        update["reply_body"] = reply
        route = "act"
    else:  # LLMReadback — the model reads the result and writes the reply truthfully
        route = "reason" if (ran and hops < settings.max_tool_actions) else "act"

    trace.code(
        tid, node="respond", loop_id=state.get("loop_id"), domain=domain,
        render=type(policy).__name__ if policy else None, ran=ran, route=route,
    )

    update["respond_route"] = route
    return update


def route_after_respond(state: MessageState) -> str:
    return state.get("respond_route") or "act"
