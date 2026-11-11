"""reason — call the provider-neutral Reasoner over the thread, get enforced JSON.

Skill-scoped: `state["domain"]` (set by `route`) picks the skill, and everything is that one
skill's — the system prompt, the enforced-output schema, and the native server tools attached to
the call (calendar attaches none; web loads web_search/web_fetch). Converts the checkpointed
messages to the neutral [{role, content}] shape and calls the reasoner. On a calendar readback
this is `reason ②`, writing the reply from the tool result (see the respond node)."""
from __future__ import annotations

import time

from ..skills import (
    SKILLS, context_block_for, output_schema_for, reason_runtime_for, server_tools_for,
    system_prompt_for,
)
from ..state import MessageState
from ..trace import Trace


def _to_neutral(messages: list) -> list[dict]:
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        else:  # a langchain BaseMessage (restored from the checkpoint)
            role = "assistant" if getattr(m, "type", "") == "ai" else "user"
            out.append({"role": role, "content": getattr(m, "content", "")})
    return out


async def reason_node(state: MessageState, *, reasoner, settings, trace: Trace,
                      directory=None, presets=None) -> dict:
    tid = state["trace_id"]
    domain = state.get("domain") or settings.default_domain
    locked_lang = state.get("session_lang")  # set once per loop; None right after a fresh tag

    # The address book, injected as a prompt block — no tool hop, no await, ~0.3 ms.
    #
    # Injected on EVERY pass, including the read-backs. The tempting optimisation — first pass only,
    # since reason ② "just composes prose from a tool result" — is wrong for the flow that matters
    # most: an update runs `find` first, so the write (and its complete attendee roster) is composed
    # in reason ②, with hops already ≥ 1. Dropping the block there left the model told to never
    # invent an address while holding no addresses, so it either asked for one the book already had
    # or silently dropped a guest from the roster it was rebuilding.
    # The owner's meeting presets ride in the same block, on the same every-pass rule: an update
    # composed in reason ② that moves a meeting "to my house" needs the saved address there too.
    # Asked for even with no address book and no presets: the calendar's block also says when a
    # chat is one-to-one, which its "who the meeting is with" rule reads, and that needs neither.
    context_block, patch = context_block_for(
        domain, state, {"directory": directory, "settings": settings, "presets": presets})
    seen_contacts = dict(patch.get("seen_contacts") or {})
    seen_people = dict(patch.get("seen_people") or {})

    stack = state.get("skill_stack") or []
    system = system_prompt_for(domain, settings, session_lang=locked_lang,
                               context_block=context_block,
                               called_by=stack[-1] if stack else None)
    schema = output_schema_for(domain, settings=settings)
    server_tools = server_tools_for(domain, settings)
    runtime = reason_runtime_for(domain, settings)  # {model, effort, think}, per skill
    convo = _to_neutral(state.get("messages"))

    t0 = time.monotonic()
    result = await reasoner.respond(
        system=system, messages=convo, output_schema=schema, server_tools=server_tools,
        model=runtime["model"], effort=runtime["effort"], think=runtime["think"],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    skill = SKILLS.get(domain)
    if skill is not None and skill.revise is not None:
        # This pass's address block counts as seen: it is what the model just read.
        seen_now = {**(state.get("seen_contacts") or {}), **seen_contacts}
        revised = skill.revise(result, {**state, "seen_contacts": seen_now}, settings)
        if revised is not result:
            trace.code(tid, node="reason", loop_id=state.get("loop_id"), domain=domain,
                       revised=True, was_actions=result.get("actions") or [],
                       call=revised.get("call"))
            result = revised

    llm_state = result.get("state") or "keep_listening"
    message = result.get("message")
    usage = result.get("usage") or {}

    actions = result.get("actions") or []
    workflow = result.get("workflow")
    call = result.get("call") or None

    # Reasoning stream: the model's own turn — its stated rationale, the enforced-JSON decision,
    # and the metadata for the record.
    trace.code(
        tid, node="reason", loop_id=state.get("loop_id"), domain=domain,
        provider=settings.llm_provider, model=runtime["model"],
        state=llm_state, message=message, lang=result.get("lang"),
        reasoning=result.get("reasoning"), stop_reason=result.get("stop_reason"),
        request_id=result.get("provider_request_id"), usage=usage,
        tools=result.get("tool_calls") or [], actions=actions, latency_ms=latency_ms,
        contacts_shown=len(seen_contacts) or len(seen_people) or None, call=call,
    )

    # NB: history is appended in `act` (the single place a *sent* message enters the thread),
    # so a read-back reason pass never leaves a phantom, undelivered assistant turn behind.
    update: dict = {
        "llm_state": llm_state,
        "reply_body": message,
        "lang": result.get("lang"),
        "actions": actions,
        "workflow": workflow,
        "call": call,
        "provider": settings.llm_provider,
        "model": runtime["model"],
        "provider_request_id": result.get("provider_request_id"),
        "usage": usage,
        "latency_ms": latency_ms,
        "stop_reason": result.get("stop_reason"),
        "tool_calls": result.get("tool_calls") or [],
        "error_category": result.get("error_category") or "none",
    }
    if seen_contacts:
        # MERGE, never replace — the confirmation composer runs two nodes later and must still be
        # able to name someone the first pass surfaced (this is the seen_events rule).
        update["seen_contacts"] = {**(state.get("seen_contacts") or {}), **seen_contacts}
    if seen_people:
        # What is already here wins: a `find` read the whole card, and this block only re-surfaces
        # the snapshot's fields of the same person. The other way round, the card about to overwrite
        # his notes could no longer show him what they said.
        update["seen_people"] = {**seen_people, **(state.get("seen_people") or {})}
    # Lock the session language on the FIRST pass after a tag (when it isn't set yet), so every
    # later read-back pass and window continuation is told to keep writing in it.
    if not locked_lang and result.get("lang"):
        update["session_lang"] = result["lang"]
    return update


def route_after_reason(state: MessageState) -> str:
    """Actions to run → confirm (the skill gates them); a call with nothing to run first → handoff;
    otherwise straight to act (send/close)."""
    if state.get("actions"):
        return "confirm"
    return "handoff" if state.get("call") else "act"
