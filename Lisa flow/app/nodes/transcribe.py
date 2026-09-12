"""transcribe — the deterministic fast path (NO model call).

Runs only when `parse` recognised a pure transcribe request: the owner replied to a voice note
with the tag and nothing but a transcribe verb. Download the audio, transcribe it, and send
exactly one message — inline for a short clip, a .txt document for a long one. One-shot: no
listening window is opened, mirroring the legacy skill's "exactly one message".

The transcript is what the owner asked for, so it is delivered verbatim under the framed header,
and failures are reported honestly — never a fabricated transcript. The delivery itself lives in
`transcribe_reply.deliver`, shared with the automatic path so the two can never drift."""
from __future__ import annotations

from ..clients.evolution import send_target
from ..state import MessageState
from ..trace import Trace
from ..transcribe_reply import deliver


async def transcribe_node(
    state: MessageState, *, evolution, transcription, echoes, settings, trace: Trace
) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    wa_id = state.get("quoted_audio_id")
    loop_id = trace.new_loop_id(state["number"])  # one-shot loop so the transcript is durable

    result = await transcription.get(wa_id)
    sent = await deliver(
        evolution=evolution, result=result, target=send_target(jid),
        owner=settings.owner_name, settings=settings,
        # The owner asked for this one and is waiting on it, so a failure is answered.
        report_failures=True,
    )

    if sent["sent_id"]:
        echoes.record(jid, sent["sent_id"])  # never re-ingest our own reply
    if sent["text"]:
        # Durable: log the transcript we delivered, marked as audio-sourced.
        trace.user(tid, "AI Assistant", sent["text"], loop_id=loop_id,
                   wa_id=sent["sent_id"] or None, source="audio", status="delivered")

    trace.code(
        tid, node="transcribe", loop_id=loop_id, chat_id=jid,
        wa_audio_id=wa_id, outcome=sent["outcome"], delivery=sent["delivery"],
        duration_sec=result.get("duration_sec"), lang=sent["lang"],
        prompt_version=settings.prompt_version,
    )
    return {"sent": bool(sent["sent_id"]) or sent["delivery"] == "file",
            "reply": sent["text"] or None}
