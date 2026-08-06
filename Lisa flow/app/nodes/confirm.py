"""confirm — the skill-owned confirmation gate + composer.

For each action the routed skill's confirm policy `needs`, this node checks whether it may run.
Reads and already-confirmed writes pass through to `execute`. An unconfirmed write is held: the
skill's policy **composes the confirmation prompt** (no model writes it), the node stores the
action as `state["pending_action"]`, sets that prompt as `reply_body`, and routes to `act` to send
it. Next turn `resolve_pending` runs the write on a clean "yes".

If the skill can't compose a prompt (returns None) the turn falls back to the model (a readback so
it asks). Skills with no confirm policy pass straight through. The resolved-id gate stays in
`execute` (tool safety, not user confirmation)."""
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
    pending: dict | None = None
    ask_message: str | None = None

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
            continue
        # A write awaiting go-ahead: compose the confirmation in code and hold it as pending.
        composed = policy.compose(action, state) if pending is None else None
        if composed:
            pending, ask_message = action, composed
        else:
            msg = decision.get("message") or (
                f"Not executed — {task} needs {settings.owner_name}'s go-ahead first. "
                f"Restate the plan and wait for confirmation."
            )
            observations.append({"role": "user", "content": f"[{task} result] {msg}"})

    # Routing: approved actions run; else a composed confirmation is sent (holding the pending);
    # else a bare block reads back so the model asks (bounded); else nothing to do.
    if approved:
        route = "execute"
    elif ask_message:
        route = "act"
    elif observations and hops < settings.max_tool_actions:
        route = "reason"
        hops += 1
    else:
        route = "act"

    update: dict = {"actions": approved, "tool_hops": hops, "confirm_route": route}
    if observations:
        update["messages"] = observations
    if ask_message and route == "act":
        update["pending_action"] = pending
        update["reply_body"] = ask_message

    trace.code(
        tid, node="confirm", loop_id=state.get("loop_id"), domain=domain,
        approved=len(approved), blocked=len(observations), pending=bool(update.get("pending_action")),
        route=route,
    )
    return update


def route_after_confirm(state: MessageState) -> str:
    return state.get("confirm_route") or "act"
