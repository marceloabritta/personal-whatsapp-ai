"""reason — call the provider-neutral Reasoner over the thread, get enforced JSON.

Skill-scoped: `state["domain"]` (set by `route`) picks the skill, and everything is that one
skill's — the system prompt, the enforced-output schema, and the native server tools attached to
the call (calendar attaches none; web loads web_search/web_fetch). Converts the checkpointed
messages to the neutral [{role, content}] shape and calls the reasoner. On a calendar readback
this is `reason ②`, writing the reply from the tool result (see the respond node).

An "agent" skill (history) is not one reasoner call: `_agent_pass` runs its agent and maps what it
returns onto the same update, so `act` sends it like any reply."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from ..clients.evolution import send_target
from ..identity import frame
from ..intent import detect_language
from ..skills import (
    SKILLS, context_block_for, output_schema_for, reason_runtime_for, server_tools_for,
    skill_enabled, system_prompt_for,
)
from ..skills.history import guess_lang, run_agent
from ..state import MessageState
from ..trace import Trace

log = logging.getLogger("mary.reason")


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
                      directory=None, presets=None, history=None, evolution=None,
                      echoes=None) -> dict:
    tid = state["trace_id"]
    domain = state.get("domain") or settings.default_domain
    locked_lang = state.get("session_lang")  # set once per loop; None right after a fresh tag

    agent_skill = SKILLS.get(domain)
    if agent_skill is not None and agent_skill.kind == "agent":
        if skill_enabled(agent_skill, settings):
            return await _agent_pass(state, skill=agent_skill, settings=settings, trace=trace,
                                     history=history, evolution=evolution, echoes=echoes)
        # Switched off with a loop still on it: the turn is the pre-feature one.
        domain = settings.default_domain

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
                               called_by=stack[-1] if stack else None,
                               called_for=state.get("called_for"))
    schema = output_schema_for(domain, settings=settings)
    server_tools = server_tools_for(domain, settings)
    runtime = reason_runtime_for(domain, settings)  # {model, effort, think}, per skill
    convo = _to_neutral(state.get("messages"))

    t0 = time.monotonic()
    result = await reasoner.respond(
        system=system, messages=convo, output_schema=schema, server_tools=server_tools,
        model=runtime["model"], effort=runtime["effort"], think=runtime["think"],
    )
    skill = SKILLS.get(domain)
    if skill is not None and skill.recheck is not None:
        result = await _rechecked(result, skill, state, settings, reasoner=reasoner, trace=trace,
                                  system=system, convo=convo, schema=schema,
                                  server_tools=server_tools, runtime=runtime, domain=domain)
    latency_ms = int((time.monotonic() - t0) * 1000)

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


_ECHOED = ("reasoning", "state", "message", "lang", "actions", "workflow", "call")


def _add_usage(a: dict | None, b: dict | None) -> dict:
    keys = set(a or {}) | set(b or {})
    return {k: (a or {}).get(k, 0) + (b or {}).get(k, 0) for k in keys}


async def _rechecked(result: dict, skill, state: MessageState, settings, *, reasoner,
                     trace: Trace, system, convo, schema, server_tools, runtime,
                     domain) -> dict:
    """Skill.recheck on this pass, and ONE more model call when it finds the output wrong.

    The second call sees the whole first answer, then the check's note, as the last two messages.
    Its answer replaces the first one, and its tokens are added to the turn's. When the second
    answer fails the check as well, it still stands, and the log says so: the card the system
    composes writes the real weekday beside the date, so a mistake that survives is still on show."""
    tid = state["trace_id"]
    cleaned, note = skill.recheck(result, state, settings)
    if not note:
        return cleaned
    trace.code(tid, node="reason", loop_id=state.get("loop_id"), domain=domain, recheck="sent_back",
               note=note, was_actions=result.get("actions") or [], was_message=result.get("message"),
               was_reasoning=result.get("reasoning"))
    first = {k: result.get(k) for k in _ECHOED if result.get(k) is not None}
    again_convo = [*convo, {"role": "assistant", "content": json.dumps(first, ensure_ascii=False)},
                   {"role": "user", "content": note}]
    second = await reasoner.respond(
        system=system, messages=again_convo, output_schema=schema, server_tools=server_tools,
        model=runtime["model"], effort=runtime["effort"], think=runtime["think"],
    )
    if (second.get("error_category") or "none") != "none" or second.get("stop_reason") == "refusal":
        # The retry itself failed: the first answer goes ahead, and its card shows the real weekday.
        trace.code(tid, node="reason", loop_id=state.get("loop_id"), domain=domain,
                   recheck="retry_failed", stop_reason=second.get("stop_reason"))
        return cleaned
    usage = _add_usage(result.get("usage"), second.get("usage"))
    cleaned2, still = skill.recheck(second, state, settings)
    if still:
        trace.code(tid, node="reason", loop_id=state.get("loop_id"), domain=domain,
                   recheck="still_wrong", note=still)
    return {**cleaned2, "usage": usage}


# --- the agent kind (history search) -----------------------------------------------------

# The note a slow search sends first, the way a secretary would answer before going to look: a new
# search says what she is about to do, a follow-up in the same search is shorter. A few ways of
# saying each, picked per turn, so she does not answer every question with the same words.
_ACK = {
    "new": {
        "pt": ("Ok. Vou procurar no histórico e ver o que acho.",
               "Certo, vou dar uma olhada nas suas conversas e já te conto.",
               "Deixa comigo. Vou procurar aqui e te falo o que encontrar."),
        "es": ("Ok. Voy a buscar en el historial y ver qué encuentro.",
               "Claro, voy a revisar tus conversaciones y te cuento.",
               "Déjamelo a mí. Busco y te digo qué encuentro."),
        "en": ("Ok. I'll look through your history and see what I find.",
               "Sure, let me go through your chats and I'll get back to you.",
               "Leave it with me. I'll search and tell you what I find."),
    },
    "more": {
        "pt": ("Ok, deixa eu ver.", "Certo, vou olhar.", "Tá, vou conferir e já te falo."),
        "es": ("Ok, déjame ver.", "Claro, lo reviso.", "Vale, lo miro y te digo."),
        "en": ("Ok, let me check.", "Sure, give me a moment.", "Let me look into that."),
    },
}


def _ack_text(state: MessageState, lang: str | None) -> str:
    """What the note says: a new search or a follow-up, in his language, one of a few ways — the
    same one for the same turn (seeded by the trace id), so a test can predict it."""
    import random

    kind = "more" if state.get("history_notes") else "new"
    texts = _ACK[kind]
    options = texts.get((lang or "en").lower()[:2]) or texts["en"]
    return random.Random(state.get("trace_id") or "").choice(options)


# What she says herself, in code, around the search. Short on purpose: the agent writes the answers.
_SAY = {
    "self_only": {
        "pt": "Só busco no seu histórico na sua conversa com você mesmo.",
        "es": "Solo busco en tu historial en tu chat contigo mismo.",
        "en": "I only search your history in your chat with yourself.",
    },
    "unavailable": {
        "pt": "A busca no histórico está indisponível agora. Tente de novo mais tarde.",
        "es": "La búsqueda en el historial no está disponible ahora. Inténtalo más tarde.",
        "en": "History search is unavailable right now. Try again later.",
    },
    "failed": {
        "pt": "Não consegui terminar essa busca. Pode pedir de novo?",
        "es": "No pude terminar esa búsqueda. ¿Puedes pedirla de nuevo?",
        "en": "I couldn't finish that search. Could you ask again?",
    },
}
_STATES = ("keep_listening", "awaiting_answer", "close")
# Keys a trace record sets itself; an agent event must not overwrite them.
_OWN_KEYS = {"trace_id", "level", "node", "loop_id", "domain", "tid"}


def _say(what: str, lang: str | None) -> str:
    texts = _SAY[what]
    code = (lang or "en").lower()[:2]
    return texts.get(code) or texts["en"]


def _agent_update(state: MessageState, settings, *, message, llm_state, lang, model=None,
                  usage=None, latency_ms=None, stop_reason=None, request_id=None,
                  tool_calls=None, error_category="none", notes=None, files=None) -> dict:
    """The same update a reason pass produces, plus the search's notes and files. No actions and no
    call, so `route_after_reason` goes straight to `act`."""
    update: dict = {
        "llm_state": llm_state if llm_state in _STATES else "keep_listening",
        "reply_body": message,
        "lang": lang,
        "actions": [],
        "workflow": None,
        "call": None,
        "provider": settings.llm_provider,
        "model": model or getattr(settings, "history_model", None),
        "provider_request_id": request_id,
        "usage": usage or {},
        "latency_ms": latency_ms,
        "stop_reason": stop_reason,
        "tool_calls": tool_calls or [],
        "error_category": error_category or "none",
        "history_notes": notes if isinstance(notes, dict) else (state.get("history_notes") or {}),
        "history_files": list(files or []),
    }
    if not state.get("session_lang") and lang:
        update["session_lang"] = lang
    return update


async def _send_ack(state: MessageState, settings, *, evolution, echoes, trace: Trace,
                    lang: str, after_s: float) -> bool:
    """ONE short framed note, so a slow search is not silence. Recorded as an echo so it is never
    read back as his; never added to the thread, so the agent does not see itself say it."""
    tid, jid = state["trace_id"], state["remote_jid"]
    try:
        sent_id = await evolution.send_text(send_target(jid),
                                            frame(_ack_text(state, lang), settings.owner_name, lang))
    except Exception as exc:  # a note that fails to go must not cost him the answer
        log.warning("history ack failed: %s", exc)
        sent_id = None
    if sent_id and echoes is not None:
        echoes.record(jid, sent_id)
    trace.code(tid, node="history", loop_id=state.get("loop_id"), domain="history", ack=True,
               after_s=after_s, delivered=sent_id is not None, wa_id=sent_id or None)
    return sent_id is not None


async def _agent_pass(state: MessageState, *, skill, settings, trace: Trace, history,
                      evolution, echoes) -> dict:
    """One turn of the history search: guard, run the agent (with a note if it is slow), map."""
    tid = state["trace_id"]
    loop_id = state.get("loop_id")
    domain = skill.name
    locked_lang = state.get("session_lang")
    # Before the agent has said anything, her own words follow his: the locked language, else his —
    # read off the words a search is made of first (detect_language takes any accent for
    # Portuguese, and "quién me mandó" is Spanish), then the general markers.
    text = state.get("text") or ""
    lang = locked_lang or guess_lang(text) or detect_language(text) or "en"

    # Belt and braces on top of routing: his own message, in his own chat with himself. Anyone
    # else is not answered at all — a contact is not told there is anything to search.
    if not (state.get("is_self_chat") and state.get("from_me")):
        trace.code(tid, node="reason", loop_id=loop_id, domain=domain, refused="not_self_chat",
                   from_me=bool(state.get("from_me")), is_self_chat=bool(state.get("is_self_chat")))
        body = _say("self_only", lang) if state.get("from_me") else None
        return _agent_update(state, settings, message=body, llm_state="keep_listening",
                             lang=lang, stop_reason="refused")

    # Switched on, but the store did not open at boot (history/runtime.py set it to None).
    if history is None:
        trace.code(tid, node="reason", loop_id=loop_id, domain=domain, unavailable=True)
        return _agent_update(state, settings, message=_say("unavailable", lang),
                             llm_state="close", lang=lang, stop_reason="unavailable")

    def on_event(event: dict) -> None:
        try:
            fields = {k: v for k, v in (event or {}).items() if k not in _OWN_KEYS}
            trace.code(tid, node="history", loop_id=loop_id, domain=domain, **fields)
        except Exception:  # the log must never break the search
            pass

    ack_after = max(0.0, float(getattr(settings, "history_ack_after_seconds", 8.0) or 0.0))
    t0 = time.monotonic()
    task = asyncio.ensure_future(run_agent(
        convo=_to_neutral(state.get("messages")),
        store=history.store, media=history.media,
        settings=getattr(history, "settings", None) or settings,
        notes=state.get("history_notes"), session_lang=locked_lang, now_ts=int(time.time()),
        on_event=on_event,
    ))
    acked = False
    error = None
    result: dict = {}
    try:
        done, _ = await asyncio.wait({task}, timeout=ack_after)
        if not done and evolution is not None:
            acked = await _send_ack(state, settings, evolution=evolution, echoes=echoes,
                                    trace=trace, lang=lang, after_s=ack_after)
        result = await task or {}
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception as exc:  # the agent's own failure: a short apology, the window kept
        error = exc
        log.exception("history agent failed")
    finally:
        if not task.done():
            task.cancel()
    latency_ms = int((time.monotonic() - t0) * 1000)

    message = result.get("message")
    category = result.get("error_category") or "none"
    if error is not None:
        message, category = _say("failed", lang), "provider"
    elif not message and category != "none":
        # It gave up without a word (a provider error, the budget spent). He asked something and
        # may already have seen "Searching…": silence would leave him waiting.
        message = _say("failed", result.get("lang") or lang)
    out_lang = result.get("lang") or lang
    llm_state = result.get("state") if error is None else "keep_listening"
    files = list(dict.fromkeys(f for f in (result.get("files") or [])
                               if isinstance(f, str) and f))
    usage = result.get("usage") or {}
    model = result.get("model") or getattr(settings, "history_model", None)

    # The same record a reason pass leaves: its decision and metadata. What the tools returned is
    # never here — `tool_calls` names each call and its arguments, and the agent's own events
    # (node="history") say what ran.
    trace.code(
        tid, node="reason", loop_id=loop_id, domain=domain,
        provider=settings.llm_provider, model=model,
        state=llm_state, message=message, lang=out_lang,
        reasoning=result.get("reasoning"), stop_reason=result.get("stop_reason"),
        request_id=result.get("provider_request_id"), usage=usage,
        tools=result.get("tool_calls") or [], actions=[], latency_ms=latency_ms,
        files=files or None, acked=acked or None,
        error=type(error).__name__ if error is not None else None,
    )
    return _agent_update(
        state, settings, message=message, llm_state=llm_state, lang=out_lang, model=model,
        usage=usage, latency_ms=latency_ms,
        stop_reason="error" if error is not None else result.get("stop_reason"),
        request_id=result.get("provider_request_id"), tool_calls=result.get("tool_calls"),
        error_category=category,
        notes=None if error is not None else result.get("notes"),
        files=files,
    )


def route_after_reason(state: MessageState) -> str:
    """Actions to run → confirm (the skill gates them); a call with nothing to run first → handoff;
    otherwise straight to act (send/close)."""
    if state.get("actions"):
        return "confirm"
    return "handoff" if state.get("call") else "act"
