"""act — apply the model's JSON decision to the wire and the window, then emit the
one-row activation record (§10/§12).

  keep_listening · message  → send, refresh the 60s window
  awaiting_answer · message → send, hold the window for AWAIT_WINDOW_SECONDS (5 min)
  any · null                → stay silent, keep the window — never shorter than it was
  nothing new to read       → the same: silent, window kept (context found no new message)
  close · message           → send (model writes its own sign-off), drop the window
  close · null              → drop the window, nothing sent
The message goes out verbatim. The reply header is localised to the session language,
which is locked at the tag that opened the window."""
from __future__ import annotations

import time

from ..identity import frame
from ..state import MessageState
from ..trace import Trace

AWAITING = "awaiting_answer"


def _waiting(state: MessageState) -> bool:
    """Is Lisa waiting on an answer? A held write (a confirmation card is out) and a gather goal
    with open questions are read off the state — no model judgment involved. The model's own
    "awaiting_answer" covers the question that is neither, like "shall I book just the next one?"."""
    workflow = state.get("workflow") or {}
    return bool(state.get("llm_state") == AWAITING or state.get("pending_action")
                or workflow.get("open_questions"))


def window_ttl(state: MessageState, settings) -> int:
    """How long the window stays open after this turn. A setup loop holds a longer window:
    finding a contact and forwarding their card means leaving this chat. So does a question:
    looking up an email or checking a date takes minutes, not the conversational seconds."""
    ttl = settings.loop_ttl_seconds
    if state.get("loop_domain") == "setup":
        ttl = max(ttl, settings.setup_window_seconds)
    if _waiting(state):
        ttl = max(ttl, settings.await_window_seconds)
    return ttl


async def act_node(
    state: MessageState, *, evolution, sessions, echoes, settings, trace: Trace, reaper=None
) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    number = state["number"]
    llm_state = state.get("llm_state") or "keep_listening"
    body = state.get("reply_body")

    # Session language is locked in `reason` on the first pass after the tag and carried on the
    # state; the header just reads it (fall back only if a turn never produced one).
    session_lang = state.get("session_lang") or state.get("lang") or "en"

    reply = None
    sent = False
    sent_id = None
    delivery = "silent"

    if body:
        # Send the model's message exactly as it decided — no programmatic trailer.
        text = body.rstrip()
        reply = frame(text, settings.owner_name, session_lang)
        sent_id = await evolution.send_text(number, reply)
        sent = sent_id is not None  # None == failure; "" == sent, id unknown
        delivery = "ok" if sent else "failed"
        if sent_id:
            echoes.record(jid, sent_id)  # so we never re-ingest our own reply
        trace.user(tid, "AI Assistant", text, loop_id=state.get("loop_id"),
                   wa_id=sent_id or None, status="delivered" if sent else "failed")

    close_reason = None
    if llm_state == "close":
        sessions.close(jid)
        close_reason = "model"
        # Fast lane for the reviewer: this session just ended, so it can be graded now rather
        # than waiting for the sweeper to notice it went quiet. Non-blocking, and a full queue
        # simply drops it — the sweeper is the guarantee, this is only the latency win.
        if reaper is not None:
            reaper.offer(state.get("loop_id"))
    else:
        ttl = window_ttl(state, settings)
        if not sent:
            # A silent turn asked nothing and answered nothing, so whatever Lisa is waiting on
            # still stands. Someone else's chatter while she waits must not cut a 5-minute window
            # back to 60s; only her next message decides what she is waiting for.
            ttl = max(ttl, sessions.ttl_left(jid))
        sessions.open(jid, ttl)  # refresh the window TTL

    # activation record (prompt_version stitched here so _record stays dependency-free)
    usage = state.get("usage") or {}
    trace.code(
        tid, node="record",
        loop_id=state.get("loop_id"),
        trigger=state.get("trigger"),
        loop_started_ts=state.get("loop_started_ts"),
        activation_message_id=state.get("msg_id"),
        chat_id=jid,
        context_message_ids=state.get("context_message_ids") or [],
        provider=state.get("provider"),
        model=state.get("model"),
        prompt_version=settings.prompt_version,
        provider_request_id=state.get("provider_request_id"),
        input_tokens=usage.get("input"),
        output_tokens=usage.get("output"),
        latency_ms=state.get("latency_ms"),
        stop_reason=state.get("stop_reason"),
        state=llm_state,
        close_reason=close_reason,
        lang=session_lang,
        response=body,
        actions=state.get("action_results") or [],
        delivery_result=delivery,
        error_category=state.get("error_category") or "none",
        # No model ran: an earlier activation had read this message. Marked so the review does
        # not grade it as a silence she chose (review/store.py).
        nothing_new=state.get("nothing_new") or None,
    )

    update = {
        "reply": reply,
        "sent": sent,
        "close_reason": close_reason,
        "session_lang": session_lang,
    }
    # History is appended here — the single place a *sent* message enters the thread. The raw
    # model text (not the framed header) is what the model should see it said.
    if body and sent:
        update["messages"] = [{"role": "assistant", "content": body.rstrip()}]
    if sent:
        # Is her card still the last thing she said? A yes answers her last message, so once she
        # has said anything else, the card is no longer what he is saying yes to (resolve.py).
        update["spoke_after_card"] = not state.get("card_out")
    if sent and state.get("card_out"):
        # Which message the card was, so a fresh tag can find it in the chat above and read the
        # owner's answer under it. The framed text is the fallback when Evolution gave no id.
        update["pending_card"] = {"id": sent_id or None, "ts": int(time.time()),
                                  "text": reply, "sig": state["card_out"]}
    return update


def route_after_act(state: MessageState) -> str:
    """Almost every turn ends here. The exception is a write that has more waiting behind it: this
    message is its result, and now the next step, or the skill that called this one, carries on."""
    return "handoff" if state.get("next_call") or state.get("resume_caller") else "end"
