"""act — apply the model's JSON decision to the wire and the window, then emit the
one-row activation record (§10/§12).

  keep_listening · message → send, refresh the 60s window
  keep_listening · null    → stay silent, keep the window
  close · message          → send (model writes its own sign-off), drop the window
  close · null             → drop the window, nothing sent
The message goes out verbatim. The reply header is localised to the session language,
which is locked at the tag that opened the window."""
from __future__ import annotations

from ..identity import frame
from ..state import MessageState
from ..trace import Trace


async def act_node(
    state: MessageState, *, evolution, sessions, echoes, settings, trace: Trace
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
    else:
        sessions.open(jid)  # refresh the window TTL

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
    return update
