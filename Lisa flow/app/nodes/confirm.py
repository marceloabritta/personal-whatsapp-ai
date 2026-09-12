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

import hashlib
import logging

from ..state import MessageState
from ..trace import Trace

log = logging.getLogger("mary.confirm")


def _signature(action: dict, text: str) -> str:
    """Fingerprint of a confirmation actually sent — the task plus the exact words."""
    task = (action or {}).get("task", "")
    return hashlib.sha1(f"{task}|{text}".encode("utf-8")).hexdigest()[:16]


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
    repeated = False
    if ask_message and route == "act":
        # Backstop against the defect this whole path was built around: asking the SAME question
        # twice in one session. The composer is stateless, so a retry that reaches here again
        # produces byte-identical text and, to the person in the chat, an assistant that ignored
        # their answer. If that happens, stay silent and keep the proposal standing — the owner's
        # yes still resolves it — and log loudly, because arriving here means something upstream
        # did not report its failure.
        sig = _signature(pending, ask_message)
        if sig == state.get("last_confirm_sig"):
            repeated = True
            route = "act"
            update["confirm_route"] = "act"
            update["pending_action"] = pending   # keep it live; just do not re-ask
            update["reply_body"] = None
            update["llm_state"] = "keep_listening"
            log.warning(
                '{"confirm":"suppressed_repeat","task":"%s","loop_id":"%s"}',
                (pending or {}).get("task"), state.get("loop_id"),
            )
        else:
            update["pending_action"] = pending
            update["reply_body"] = ask_message
            update["last_confirm_sig"] = sig

    trace.code(
        tid, node="confirm", loop_id=state.get("loop_id"), domain=domain,
        approved=len(approved), blocked=len(observations), pending=bool(update.get("pending_action")),
        route=route, repeated=repeated,
    )
    return update


def route_after_confirm(state: MessageState) -> str:
    return state.get("confirm_route") or "act"
