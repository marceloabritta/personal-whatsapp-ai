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
  owner says no    → the proposal is DROPPED (with its riders, its workflow goal and the
                     confirmation fingerprint) and the turn goes to `reason` so she can answer.
                     A refusal is not a correction — "não precisa" used to land in the fixing
                     loop below, which KEEPS the write, so Lisa went on holding it.

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

"His reply" is every message of his this activation ingested (`owner_replies`), not only the one
that woke it, and a message an earlier activation already read is not read again. A fresh tag
that found his yes under the card in the chat above arrives as `carried_yes` (carry.py) and is
his yes, the same as one inside the window.

No pending action → straight through to `route` (the normal domain router)."""
from __future__ import annotations

from ..state import APPROVED_BY, OWNER_YES, MessageState
from ..trace import Trace


async def resolve_pending_node(
    state: MessageState, *, confirm_policies: dict, trace: Trace,
    tools: dict | None = None, directory=None,
) -> dict:
    tid = state["trace_id"]
    pending = state.get("pending_action")
    if not pending:
        return {"resolve_route": "route"}

    task = (pending or {}).get("task", "")
    domain, _, _ = task.partition(".")
    policy = confirm_policies.get(domain)

    def detect(text: str) -> str:
        return policy.detect(text, pending) if (policy and hasattr(policy, "detect")) else "other"

    replies = state.get("owner_replies")
    carried = state.get("carried_yes")
    if carried:
        # A fresh tag found his yes under this write's card in the chat above (carry.py) — the
        # yes the window would have run had it still been open.
        verdict, is_owner, read = "yes", True, "carried"
    elif replies:
        # What HE said this activation — every message of his it ingested, not just the one that
        # woke it. One verdict for all of them: "sim" then "mas às 16h" is a correction, not a yes.
        verdicts = {detect(t) for t in replies}
        verdict = verdicts.pop() if len(verdicts) == 1 else "other"
        is_owner, read = True, "owner"
    elif replies is not None and state.get("from_me"):
        # His message, already read by an earlier activation that ingested it ahead of its own
        # turn. It has been answered; reading it again could approve a write he never saw — the
        # earlier turn may have replaced the proposal his "sim" was about.
        trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
                   pending=task, verdict="seen", owner=True, route="hold")
        return {"resolve_route": "hold", "domain": domain,
                "reply_body": None, "llm_state": "keep_listening"}
    else:
        # A contact's message (or a caller that does not publish `owner_replies`): the trigger.
        # `from_me` is Evolution's flag for the owner's own account, and the gate has already
        # dropped the assistant's own messages — so a from_me trigger is Marcelo, and nobody else.
        verdict, is_owner, read = detect(state.get("text") or ""), bool(state.get("from_me")), "trigger"

    # A card that takes one answer is only still the question while it is the last thing she said.
    one_answer = bool(getattr(policy, "one_answer", False))
    if verdict == "yes" and is_owner and one_answer and state.get("spoke_after_card"):
        verdict = "other"

    if verdict == "yes" and is_owner:
        action = {**pending, APPROVED_BY: OWNER_YES}
        # The side effects that rode with this proposal fire NOW — on the same yes that approves
        # the write, never at proposal time. A proposal he corrects ("no, her old address") or
        # abandons simply drops them, so a rejected address never reaches the real address book.
        riders = state.get("pending_side_effects") or []
        if riders and directory is not None:
            from .confirm import _run_side_effects

            handler = (tools or {}).get(domain)
            if handler is not None:
                directory.spawn(_run_side_effects(handler, riders, state))
        trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
                   pending=task, verdict="yes", owner=True, route="execute",
                   riders=len(riders) or None, read=read, carried=carried or None)
        return {"actions": [action], "domain": domain, "pending_action": None,
                "pending_side_effects": [], "pending_card": None, "resolve_route": "execute",
                "next_call": state.get("pending_call"), "pending_call": None}

    if verdict == "no" and is_owner:
        # He declined. "other" is the FIXING loop — it keeps the proposal so a correction can
        # replace it — and a refusal routed there left the write standing, with Lisa still holding
        # it and waiting. A decline is not a correction: the proposal is dead.
        #
        # `workflow` goes too. It is the gather memory toward a goal, and the goal was just
        # refused; leaving it would have the model resume the same booking next turn.
        trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
                   pending=task, verdict="no", owner=True, route="reason", dropped=True,
                   read=read)
        return {"domain": domain, "resolve_route": "reason",
                "pending_action": None, "pending_side_effects": [], "pending_card": None,
                "pending_call": None, "workflow": None, "last_confirm_sig": None}

    if verdict in ("yes", "no"):
        # Someone else agreed or declined. Only the owner's calendar is written on the owner's
        # say-so, and only he can call the proposal off. Ignored, silently, proposal standing.
        trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
                   pending=task, verdict=verdict, owner=False, route="hold",
                   reason="not_owner")
        return {"resolve_route": "hold", "domain": domain,
                # This turn produces no message. Both fields are per-turn scratch that only
                # `reason` refreshes, and `reason` does not run on this path — left alone they
                # would carry the PREVIOUS turn's reply and `act` would send it again.
                "reply_body": None, "llm_state": "keep_listening"}

    trace.code(tid, node="resolve_pending", loop_id=state.get("loop_id"),
               pending=task, verdict=verdict, route="reason", domain=domain, read=read)
    if is_owner and one_answer:
        # "não apaga", "cancela por favor", "pode cancelar": no short list of words will ever catch
        # every way of saying no, and the model has no way to withdraw a held write. So it would
        # answer "ok, não vou apagar" with the card still standing, and his next "ok" deleted it.
        return {"domain": domain, "resolve_route": "reason", "pending_action": None,
                "pending_side_effects": [], "pending_call": None, "pending_card": None,
                "last_confirm_sig": None,
                "messages": [{"role": "user", "content":
                              f"[{task} not run] That card is withdrawn: he did not answer it with "
                              f"a clean yes. If what he said corrects it, propose it again with the "
                              f"correction. If he refused or moved on, just answer him."}]}
    # The pending SURVIVES a non-yes turn (that is the fixing loop), but its riders do not. This
    # turn is a correction — "no, her other address" — and the model will re-propose. Carrying the
    # old riders forward would write the address he just rejected the moment he says yes to the
    # corrected proposal. confirm re-attaches riders to whatever it proposes next.
    update = {"domain": domain, "resolve_route": "reason", "pending_side_effects": []}
    if is_owner:
        # Only his correction changes what he is being asked. Someone else's "show, valeu" must not
        # take away the step he is about to approve.
        update["pending_call"] = None
    return update


def route_after_resolve(state: MessageState) -> str:
    return state.get("resolve_route") or "route"
