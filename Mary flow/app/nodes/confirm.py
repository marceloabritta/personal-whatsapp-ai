"""confirm — the skill-owned confirmation gate.

The routed skill's confirm policy decides whether each pending mutating action may run. This node
only delegates: for every action whose verb the policy `needs`, it awaits the policy; approved
actions stay in `state["actions"]` and flow to `execute`, blocked ones are dropped and recorded as
an observation the model reads back so it asks the owner. Reads (verbs the policy does not gate)
and skills with no confirm policy pass straight through. Bounded by `max_tool_actions` so a
blocked-only turn can't loop between confirm and reason.

The old structural confirm gate that lived in `execute` is gone — the rule now lives in the
skill. The resolved-id gate stays in `execute` (that is tool safety, not user confirmation)."""
from __future__ import annotations

from ..state import MessageState
from ..trace import Trace


async def confirm_node(
    state: MessageState, *, confirm_policies: dict, settings, reasoner, trace: Trace
) -> dict:
    tid = state["trace_id"]
    domain = state.get("domain") or ""
    policy = confirm_policies.get(domain)
    actions = state.get("actions") or []
    hops = int(state.get("tool_hops") or 0)
    ctx = {"settings": settings, "reasoner": reasoner}

    approved: list = []
    observations: list = []

    for action in actions:
        task = (action or {}).get("task", "")
        _, _, verb = task.partition(".")
        needs = getattr(policy, "needs", set()) if policy else set()
        if policy is None or verb not in needs:
            approved.append(action)  # reads / ungated verbs / no policy -> straight through
            continue
        decision = await policy.confirm(action=action, state=state, deps=ctx)
        if decision.get("ok"):
            approved.append(action)
        else:
            msg = decision.get("message") or (
                f"Not executed — {task} needs {settings.owner_name}'s go-ahead first. "
                f"Restate the plan and wait for confirmation."
            )
            observations.append({"role": "user", "content": f"[{task} result] {msg}"})

    # Routing: any approved action -> execute (blocked observations, if any, read back after).
    # Only-blocked -> back to reason so the model asks (bounded); nothing at all -> act.
    if approved:
        route = "execute"
    elif observations and hops < settings.max_tool_actions:
        route = "reason"
        hops += 1  # count the ask so a stubborn unconfirmed action can't loop forever
    else:
        route = "act"

    trace.code(
        tid, node="confirm", loop_id=state.get("loop_id"), domain=domain,
        approved=len(approved), blocked=len(observations), route=route,
    )

    return {
        "actions": approved,
        "messages": observations,
        "tool_hops": hops,
        "confirm_route": route,
    }


def route_after_confirm(state: MessageState) -> str:
    return state.get("confirm_route") or "act"
