"""handoff — one skill calling another inside the same loop, and coming back.

A calendar turn that needs Felipe's email calls the contacts skill, contacts finds it, and calendar
carries on with the booking, all on one thread. Nothing is new to the called skill: it runs through
`reason` like any other, with its own prompt and schema, its own card and its own gate. This node only
moves the conversation between them and remembers who is waiting (`skill_stack`).

It is a call on the same thread rather than a side agent running beside it, because the called skill
may have to stop and ask. A write needs the owner's yes, which comes from a later message, and a side
agent would have nowhere to wait for it or any way to put a card in the chat. Here the stack is
checkpointed like everything else, so a call can span as many turns as he takes to answer.

Three ways in, one way out each:

  call          the model asked for another skill, with nothing to run first. If that skill is the
                one that called this one, this is the answer and the stack pops. Otherwise it pushes.
  next_call     a call sent WITH a write: the step after it. The owner's yes on the card approved
                both, and it fires once the write's result is out. A next step, not a call to come
                back from, so it pushes nothing.
  resume_caller a called skill's write rendered in code, so no model pass is left in it that could
                hand back. The code does.

What it does NOT do is approve anything. A handoff carries words, never authority: the called skill's
writes still go through their own card, and its reads through their own gate. Nor does a request count
as something the conversation said (see HANDOFF_MARK).
"""
from __future__ import annotations

import logging

from ..skills import SKILLS, callable_skills
from ..state import HANDOFF_MARK, MessageState
from ..trace import Trace

log = logging.getLogger("mary.handoff")

# Calling back is a pop, so two skills stay at depth 1. This is here for a third skill in the chain.
_MAX_DEPTH = 2

# Moving skills withdraws any card still standing. The next thing he reads is the other skill's
# question, so his "sim" to "shall I move it?" must not land on the delete card from two messages ago,
# and his answer must reach the skill that asked. The skill that comes back re-proposes if it still
# wants to, and that card has to be able to go out again, hence the signature.
_NO_STANDING_WRITE = {"pending_action": None, "pending_side_effects": [], "pending_card": None,
                      "pending_call": None, "last_confirm_sig": None}


def _note(text: str) -> dict:
    return {"role": "user", "content": f"{HANDOFF_MARK}{text}"}


def _refusal(*, domain: str, target: str, request: str, depth: int, from_owner: bool,
             settings) -> str | None:
    if not target or not request:
        return "A call needs both a skill and a request."
    if target not in callable_skills(domain, settings):
        return f"The {domain} skill cannot call {target!r}."
    if SKILLS[target].owner_only and not from_owner:
        return f"Only {settings.owner_name} can bring in the {target} skill."
    if depth >= _MAX_DEPTH:
        return "Too many skills are already waiting on each other. Answer with what you have."
    return None


async def handoff_node(state: MessageState, *, settings, trace: Trace) -> dict:
    tid = state["trace_id"]
    domain = state.get("domain") or ""
    stack = list(state.get("skill_stack") or [])
    used = int(state.get("skill_calls") or 0) + 1
    tail = state.get("next_call")
    resume = bool(state.get("resume_caller")) and bool(stack)
    # A message already went out on these two, so there is nothing further to send if this stops.
    after_act = bool(tail or state.get("resume_caller"))
    update: dict = {"call": None, "next_call": None, "resume_caller": False, "actions": [],
                    "skill_calls": used}

    def stop(reason: str) -> dict:
        log.warning('{"handoff":"stopped","reason":"%s","loop_id":"%s"}',
                    reason, state.get("loop_id"))
        trace.code(tid, node="handoff", loop_id=state.get("loop_id"), domain=domain,
                   mode="stopped", reason=reason)
        return {**update, "handoff_route": "end" if after_act else "act"}

    # Two skills that keep calling each other would otherwise run until the chat lock gave out.
    # `act` still sends whatever the model last said, so a stop is not a silent one if it said anything.
    if used > settings.max_skill_calls:
        return stop("too_many_calls")

    if resume:
        frame = stack.pop()
        results = state.get("last_results") or [{}]
        target, mode = frame["domain"], "resume"
        note = (f"The {domain} skill finished what you asked for ({frame.get('goal')}): "
                f"{results[0].get('summary') or 'done'} You are back in the {target} skill. Carry on "
                f"with what {settings.owner_name} asked. He already has the result above, so do not "
                f"repeat it.")
        update["workflow"] = frame.get("workflow")
    else:
        call = tail or state.get("call") or {}
        target = (call.get("skill") or "").strip()
        request = (call.get("request") or "").strip()
        answering = bool(stack) and stack[-1]["domain"] == target
        if answering:
            frame = stack.pop()
            mode = "answer"
            note = (f"The {domain} skill answered: {request} You are back in the {target} skill. "
                    f"Carry on with what {settings.owner_name} asked.")
            update["workflow"] = frame.get("workflow")
        else:
            refusal = _refusal(
                domain=domain, target=target, request=request, depth=len(stack),
                from_owner=bool(tail or state.get("from_me") or state.get("owner_replies")),
                settings=settings)
            if refusal:
                if after_act:
                    return stop(refusal)
                trace.code(tid, node="handoff", loop_id=state.get("loop_id"), domain=domain,
                           mode="refused", target=target, reason=refusal)
                return {**update, "handoff_route": "reason", "reply_body": None,
                        "messages": [_note(f"That call did not happen. {refusal}")]}
            if tail:
                mode = "next"
                note = f"Next step, already approved by {settings.owner_name}: {request}"
            else:
                mode = "call"
                stack.append({"domain": domain, "goal": request, "workflow": state.get("workflow")})
                note = f"The {domain} skill is asking you: {request}"
            update["workflow"] = None

    trace.code(tid, node="handoff", loop_id=state.get("loop_id"), mode=mode, source=domain,
               target=target, depth=len(stack), calls=used)
    return {**update, **_NO_STANDING_WRITE, "domain": target, "loop_domain": target,
            "skill_stack": stack, "reply_body": None, "messages": [_note(note)], "handoff_route": "reason"}


def route_after_handoff(state: MessageState) -> str:
    return state.get("handoff_route") or "end"
