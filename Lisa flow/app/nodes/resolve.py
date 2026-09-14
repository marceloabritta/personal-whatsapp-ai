"""resolve_pending — the programmatic happy-path gate, before any model call.

When a skill has proposed a write and is waiting for the owner's go-ahead, it stored the resolved
action in `state["pending_action"]`. This node runs first (right after context) and, using the
skill's own confirm policy, decides what the incoming message means:

  owner says yes   → stamp the action APPROVED and jump straight to `execute` — the model is
                     never called; the success message is composed programmatically later.
  someone else
  says yes         → HOLD. The pending stays exactly as it is, nothing is sent, the turn ends.
                     Only the owner's calendar may be written on the owner's say-so, and a
                     stranger's "sim" must not be answered either — announcing that we are
                     waiting would turn every bystander's agreement into chatter in the chat.
  anything else    → the SAME domain's `reason`, pending KEPT. This is the fixing loop: "my
                     email is wrong", "the location is X", "add a conference room" are all
                     corrections to the proposal, and the model answers them by proposing a
                     corrected action, which replaces the pending at the `confirm` node.

Two rails worth spelling out:

  * The pending SURVIVES a non-yes turn. It used to be cleared, so any intervening remark —
    even chit-chat from a third party — dropped the proposal on the floor, and the owner's later
    "yes" then had nothing to resolve, so the model simply re-proposed and the same confirmation
    went out a second time.
  * The reply stays in the pending write's DOMAIN. A keyword-less follow-up ("at 5pm instead")
    would be misrouted to web by the stateless router.

No pending action → straight through to `route` (the normal domain router)."""
from __future__ import annotations

from ..state import APPROVED_BY, OWNER_YES, MessageState
from ..trace import Trace


async def resolve_pending_node(
    state: MessageState, *, confirm_policies: dict, trace: Trace
) -> dict:
    tid = state["trace_id"]
    pending = state.get("pending_action")
    if not pending:
        return {"resolve_route": "route"}

    task = (pending or {}).get("task", "")
    domain, _, _ = task.partition(".")
    policy = confirm_policies.get(domain)
    text = state.get("text") or ""
    verdict = policy.detect(text) if (policy and hasattr(policy, "detect")) else "other"
    # `from_me` is Evolution's flag for the owner's own account, and the gate has already dropped
    # the assistant's own messages — so this is Marcelo, and nobody else.
    is_owner = bool(state.get("from_me"))

    if verdict == "yes" and is_owner:
        action = {**pending, APPROVED_BY: OWNER_YES}
        trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
                   pending=task, verdict="yes", owner=True, route="execute")
        return {"actions": [action], "domain": domain, "pending_action": None,
                "resolve_route": "execute"}

    if verdict == "yes":
        # Someone else agreed. Ignored, silently, and the proposal keeps standing.
        trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
                   pending=task, verdict="yes", owner=False, route="hold",
                   reason="not_owner")
        return {"resolve_route": "hold", "domain": domain,
                # This turn produces no message. Both fields are per-turn scratch that only
                # `reason` refreshes, and `reason` does not run on this path — left alone they
                # would carry the PREVIOUS turn's reply and `act` would send it again.
                "reply_body": None, "llm_state": "keep_listening"}

    trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
               pending=task, verdict=verdict, route="reason", domain=domain)
    return {"domain": domain, "resolve_route": "reason"}


def route_after_resolve(state: MessageState) -> str:
    return state.get("resolve_route") or "route"
