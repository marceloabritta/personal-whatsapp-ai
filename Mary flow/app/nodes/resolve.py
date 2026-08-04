"""resolve_pending — the programmatic happy-path gate, before any model call.

When a skill has proposed a write and is waiting for the owner's go-ahead, it stored the resolved
action in `state["pending_action"]`. This node runs first (right after context) and, using the
skill's own confirm policy, decides whether the incoming message is a clean "yes":

  clean yes  → force `confirmed: true` on the pending action and jump straight to `execute` —
               the model is NEVER called; the success message is composed programmatically later.
  otherwise  → clear the pending and hand the turn to the SAME domain's `reason` (a reply to a
               pending write — "no", "at 5pm instead", "change the title" — is still that domain's
               conversation; the stateless router would misroute a keyword-less follow-up to web).

No pending action → straight through to `route` (the normal domain router)."""
from __future__ import annotations

from ..state import MessageState
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

    if verdict == "yes":
        action = {**pending, "confirmed": True}
        trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
                   pending=task, verdict="yes", route="execute")
        return {"actions": [action], "domain": domain, "pending_action": None,
                "resolve_route": "execute"}

    # A non-yes reply to a pending write stays in that write's domain — the model handles the
    # change/refusal there (calendar schema), instead of being misrouted by the keyword router.
    trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
               pending=task, verdict=verdict, route="reason", domain=domain)
    return {"pending_action": None, "domain": domain, "resolve_route": "reason"}


def route_after_resolve(state: MessageState) -> str:
    return state.get("resolve_route") or "route"
