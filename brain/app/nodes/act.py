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
    state: MessageState, *, evolution, sessions, settings, trace: Trace
) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    number = state["number"]
    llm_state = state.get("llm_state") or "keep_listening"
    body = state.get("reply_body")

    # Session language: lock it at the tag that opened the window; keep it thereafter.
    session_lang = state.get("session_lang")
    if state.get("trigger") == "tag" or not session_lang:
        session_lang = state.get("lang") or session_lang or "en"

    reply = None
    sent = False
    delivery = "silent"

    if body:
        # Send the model's message exactly as it decided — no programmatic trailer.
        text = body.rstrip()
        reply = frame(text, settings.owner_name, session_lang)
        sent = await evolution.send_text(number, reply)
        delivery = "ok" if sent else "failed"
        trace.user(tid, "mary", text, status="delivered" if sent else "failed")

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
        delivery_result=delivery,
        error_category=state.get("error_category") or "none",
    )

    return {
        "reply": reply,
        "sent": sent,
        "close_reason": close_reason,
        "session_lang": session_lang,
    }
