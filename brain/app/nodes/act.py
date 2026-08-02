"""act — apply the model's JSON decision to the wire and the window, then emit the
one-row activation record (§10/§12).

  keep_listening · message → send, refresh the 60s window
  keep_listening · null    → stay silent, keep the window
  close · message          → send + deterministic sign-off, drop the window
  close · null             → drop the window, nothing sent
The sign-off is appended HERE (never by the model) so it's always verbatim."""
from __future__ import annotations

from ..identity import frame
from ..prompt import signoff_for
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

    reply = None
    sent = False
    delivery = "silent"

    if body:
        text = body.rstrip()
        if llm_state == "close":
            text = f"{text}\n\n{signoff_for(settings.primary_tag)}"
        reply = frame(text, settings.owner_name)
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
        response=body,
        delivery_result=delivery,
        error_category=state.get("error_category") or "none",
    )

    return {"reply": reply, "sent": sent, "close_reason": close_reason}
