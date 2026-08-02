"""act — post next_message to the chat (if any) and apply loop_state, then emit the
one-row activation record.

Reached either directly from reason (no actions) or from execute after a clean write.
Sending is verbatim; the header is localised to the session language, locked at the
tag that opened the window. Closing the window is the model's decision (loop_state),
independent of whether anything was executed — the 60s TTL is the backstop."""
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
    loop_state = state.get("loop_state") or "keep_listening"
    body = state.get("next_message")

    session_lang = state.get("session_lang")
    if state.get("trigger") == "tag" or not session_lang:
        session_lang = state.get("lang") or session_lang or "en"

    reply = None
    sent = False
    delivery = "silent"
    if body:
        reply = frame(body.rstrip(), settings.owner_name, session_lang)
        sent = await evolution.send_text(number, reply)
        delivery = "ok" if sent else "failed"
        trace.user(tid, "mary", body, status="delivered" if sent else "failed")

    close_reason = None
    if loop_state == "close_loop":
        sessions.close(jid)
        close_reason = "model"
    else:
        sessions.open(jid)  # refresh the window TTL

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
        loop_state=loop_state,
        close_reason=close_reason,
        lang=session_lang,
        actions=state.get("action_results") or [],
        workflow_task=(state.get("workflow") or {}).get("task"),
        response=body,
        delivery_result=delivery,
        error_category=state.get("error_category") or "none",
    )

    out: dict = {"reply": reply, "sent": sent, "session_lang": session_lang}
    if body and sent:  # record exactly what was posted, once
        out["messages"] = [{"role": "assistant", "content": body}]
    return out
