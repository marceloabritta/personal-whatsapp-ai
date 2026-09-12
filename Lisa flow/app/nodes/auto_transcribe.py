"""auto_transcribe — the ambient fast path (NO model call).

Runs when `gate` matched this voice note against a roster rule: an enrolled chat, and a
direction that covers who recorded it. Download, transcribe, and reply ATTACHED to the audio,
in the same format the reactive path produces.

Ambient output is quiet when it fails. Nobody asked for this transcript in the moment, so a
provider outage stays out of the chat and lives in the trace instead (flip
AUTO_TRANSCRIBE_REPORT_FAILURES to debug it out loud).

Two rails before any money is spent: an over-long clip is skipped on its DECLARED length,
straight from the payload, and a per-chat daily cap stops a pathological day. Both are recorded
rather than silently applied.

One-shot: this mints its own loop for the durable log. If a @lisa listening window happens to be
open on the same chat, the turn continues into `context` afterwards and the normal conversation
runs as well — the transcript and the reply are different jobs."""
from __future__ import annotations

from ..clients.evolution import send_target
from ..state import MessageState
from ..trace import Trace
from ..transcribe_reply import deliver


async def auto_transcribe_node(
    state: MessageState, *, evolution, transcription, echoes, settings, caps, trace: Trace
) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    wa_id = state.get("msg_id")
    rule = state.get("auto_rule") or {}
    key = rule.get("chat_key") or state.get("chat_key") or ""
    loop_id = trace.new_loop_id(state["number"])  # one-shot loop, so the transcript is durable

    def _skip(reason: str, **fields) -> dict:
        trace.code(tid, node="auto_transcribe", loop_id=loop_id, chat_id=jid,
                   wa_audio_id=wa_id, chat_key=key, direction=rule.get("direction"),
                   outcome="skipped", reason=reason, **fields)
        return {"sent": False, "reply": None}

    seconds = state.get("audio_seconds")
    cap_s = settings.auto_transcribe_max_seconds
    if cap_s and seconds and seconds > cap_s:
        return _skip("too_long", duration_sec=seconds, limit=cap_s)

    if caps is not None and not caps.allows(key):
        return _skip("daily_cap", limit=settings.auto_transcribe_daily_cap)

    result = await transcription.get(wa_id)

    quoted = None
    if settings.auto_transcribe_quote_reply and wa_id:
        quoted = {"remoteJid": jid, "fromMe": bool(state.get("from_me")), "id": wa_id}

    sent = await deliver(
        evolution=evolution, result=result, target=send_target(jid),
        owner=settings.owner_name, settings=settings, quoted=quoted,
        report_failures=settings.auto_transcribe_report_failures,
    )

    if sent["sent_id"]:
        echoes.record(jid, sent["sent_id"])  # never re-ingest our own reply
    if sent["delivery"] != "silent" and sent["text"]:
        caps.record(key) if caps is not None else None
        trace.user(tid, "AI Assistant", sent["text"], loop_id=loop_id,
                   wa_id=sent["sent_id"] or None, source="audio", status="delivered")

    trace.code(
        tid, node="auto_transcribe", loop_id=loop_id, chat_id=jid, chat_key=key,
        direction=rule.get("direction"), wa_audio_id=wa_id, outcome=sent["outcome"],
        delivery=sent["delivery"], duration_sec=result.get("duration_sec"),
        lang=sent["lang"], quoted=bool(quoted), prompt_version=settings.prompt_version,
    )
    return {"sent": bool(sent["sent_id"]) or sent["delivery"] == "file",
            "reply": sent["text"] or None}


def route_after_auto(state: MessageState) -> str:
    """A listening window open on this chat → carry on into the normal turn as well."""
    return "run" if state.get("auto_then_run") else "stop"
