"""context — assemble the turn (§01/§02).

A fresh @mary tag starts a NEW loop: it wipes the checkpointed conversation memory
and re-seeds the last CONTEXT_WINDOW_MESSAGES from Evolution, so one loop never
leaks into the next on the same chat. A window continuation (untagged follow-up
while the loop is open) fetches only messages after the cursor and keeps the loop's
memory. Assistant-origin messages are filtered (they're already AIMessages in the
checkpoint). The new messages become one labeled user turn; the cursor advances."""
from __future__ import annotations

import asyncio

from langchain_core.messages import RemoveMessage

from ..identity import is_own_message
from ..state import MessageState
from ..trace import Trace
from ..whatsapp import build_labeled_transcript, label_for


def _log_transcript(trace: Trace, tid: str, loop_id: str | None, records: list[dict],
                    owner: str) -> None:
    """Record each chat message into the loop's transcript stream (deduped by id in the
    store). Both sides — owner, contact, and Mary — so the log is the real conversation.
    Provenance: an audio record is logged with source="audio" so the durable stream records
    which turns were spoken, not just what was said."""
    if not loop_id:
        return
    for r in records:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        trace.user(tid, label_for(r, owner), text, loop_id=loop_id,
                   wa_id=r.get("id"), ts=r.get("ts"), from_me=bool(r.get("from_me")),
                   source="audio" if r.get("is_audio") else "text")


async def _transcribe_audio(records: list[dict], transcription, settings, trace: Trace,
                            tid: str, loop_id: str | None) -> None:
    """Transcribe the voice notes in `records` IN PLACE — an is_audio record with no text
    gets its transcript (or an honest bracketed placeholder). Cached by wa id; capped at
    settings.max_context_transcriptions with the overflow logged (no silent truncation);
    concurrent under a bounded semaphore so a multi-clip window doesn't serialize."""
    audio = [r for r in records if r.get("is_audio") and not (r.get("text") or "").strip()]
    if not audio:
        return
    if not (transcription and settings.transcription_enabled):
        for r in audio:  # mark presence — better than a silently dropped, empty voice note
            r["text"] = "[voice message]"
        return

    cap = settings.max_context_transcriptions
    todo, overflow = audio[:cap], audio[cap:]
    for r in overflow:
        r["text"] = "[voice message — not transcribed]"
    if overflow and loop_id:
        trace.code(tid, node="context", loop_id=loop_id, transcription_dropped=len(overflow))

    sem = asyncio.Semaphore(settings.transcription_concurrency)

    async def fill(r: dict) -> None:
        async with sem:
            res = await transcription.get(r["id"])
            txt = (res.get("text") or "").strip()
            if txt:
                r["text"] = txt
            elif res.get("error") == "empty":
                r["text"] = "[voice message — no speech]"
            else:
                r["text"] = "[voice message — could not transcribe]"

    await asyncio.gather(*(fill(r) for r in todo))


def _after_cursor(records: list[dict], cursor: str | None, window: int) -> list[dict]:
    if cursor:
        for i, r in enumerate(records):
            if r["id"] == cursor:
                return records[i + 1 :]
    # cursor unknown (fell out of the fetched range) — fall back to the window
    return records[-window:]


async def context_node(
    state: MessageState, *, evolution, echoes, settings, trace: Trace, transcription=None
) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    owner = settings.owner_name
    window = settings.context_window_messages

    # A fresh @mary tag opens a new loop → start from a clean context window.
    reset = state.get("trigger") == "tag"

    records = await evolution.fetch_history(jid)  # oldest → newest
    if reset or not state.get("initialized"):
        new = records[-window:]
    else:
        new = _after_cursor(records, state.get("last_whatsapp_message_id"), window)

    # Race guard: the triggering message may not be in Evolution's DB yet.
    cur_id = state.get("msg_id")
    if cur_id and not any(r.get("id") == cur_id for r in new):
        new = new + [{
            "id": cur_id, "from_me": state.get("from_me"),
            "text": state.get("text") or "", "push_name": state.get("push_name"),
            "ts": state.get("ts", 0),
        }]

    raw = list(new)  # both sides, before the assistant-origin filter — for the log

    # Filter assistant-origin — already AIMessages; never re-ingest. Primary filter is
    # the message id we recorded when we sent it; the header stamp is the fallback.
    new = [
        r for r in new
        if not echoes.is_ours(jid, r.get("id"))
        and not is_own_message(r.get("text") or "", owner)
    ]

    loop_id = state.get("loop_id")

    # Hear voice notes: transcribe the window's audio IN PLACE before building the transcript,
    # so label_for annotates them "(voice message — transcribed)" and _log_transcript marks
    # the source. Mutates the shared record dicts, so both `raw` (reset log) and `new` (model)
    # see the transcripts. Language follows the locked session language when set, else auto.
    lang = state.get("session_lang")
    await _transcribe_audio(new, transcription, settings, trace, tid, loop_id)

    transcript = build_labeled_transcript(new, owner)
    ids = [r["id"] for r in new if r.get("id")]
    newest = ids[-1] if ids else state.get("last_whatsapp_message_id")

    # Compositional reply-to-audio: the owner referenced a voice note that isn't in the window.
    # Transcribe it and inject one explicit line — no speaker-label guessing about who spoke it.
    qid = state.get("quoted_audio_id")
    if qid and qid not in ids and transcription and settings.transcription_enabled:
        qres = await transcription.get(qid, language=lang)
        qtext = (qres.get("text") or "").strip()
        if qtext:
            line = f'[{owner} replied to a voice message — transcribed: "{qtext}"]'
            transcript = f"{transcript}\n{line}" if transcript.strip() else line
            if loop_id:
                trace.user(tid, f"{owner} (replied-to voice message)", qtext,
                           loop_id=loop_id, wa_id=qid, source="audio")

    # Durable transcript. On a fresh tag (loop open) log the whole seed — the ~30
    # messages before the tag, both sides — as the loop's opening context. On a window
    # continuation log only the new inbound; Mary's own replies are logged by `act`.
    _log_transcript(trace, tid, loop_id, raw if reset else new, owner)
    trace.code(
        tid, node="context", loop_id=loop_id,
        initialized=bool(state.get("initialized")),
        reset=reset, ingested=len(new), context_message_ids=ids,
    )

    update: dict = {
        "initialized": True,
        "last_whatsapp_message_id": newest,
        "context_message_ids": ids,
        # Per-activation tool-loop scratch — always fresh so a bound/log never carries over.
        "tool_hops": 0,
        "action_results": [],
        "needs_readback": False,
    }
    if reset:
        # New loop → drop tool memory too, so a stale goal or a resolved id from the previous
        # loop can never bleed into this one (the anti-delirium invariant).
        update["workflow"] = None
        update["seen_event_ids"] = []

    # add_messages appends, so to truly start fresh we must first REMOVE every
    # message the checkpoint restored, then add this loop's seed turn.
    msgs: list = []
    if reset:
        for m in state.get("messages") or []:
            mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
            if mid:
                msgs.append(RemoveMessage(id=mid))
        update["session_lang"] = None  # re-lock the language on this tag's turn
    if transcript.strip():
        msgs.append({"role": "user", "content": transcript})
    if msgs:
        update["messages"] = msgs
    return update
