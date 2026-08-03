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


def _media_marker(r: dict, status: str = "ok") -> str:
    """Transcript marker for an image/PDF row: "[image]" / "[PDF: report.pdf]", or a status
    variant ("— unavailable" / "— too large" / "— omitted") when no block was attached."""
    kind = "PDF" if r.get("media_type") == "document" else "image"
    name = r.get("media_filename") if kind == "PDF" else None
    label = f"{kind}: {name}" if name else kind
    return f"[{label}]" if status == "ok" else f"[{label} — {status}]"


def _mark(r: dict, status: str) -> None:
    """Write the marker into the row's text so the labeled transcript emits a line — but never
    clobber a real caption (an image/PDF sent with text keeps its words)."""
    if not (r.get("text") or "").strip():
        r["text"] = _media_marker(r, status)


def _in_media_scope(r: dict) -> bool:
    """Images always; documents only when the declared mimetype is application/pdf."""
    mt = r.get("media_type")
    if mt == "image":
        return True
    if mt == "document":
        return (r.get("media_mimetype") or "").lower() == "application/pdf"
    return False


def _media_block(r: dict, res: dict, b64: str) -> dict:
    if r.get("media_type") == "document":
        blk = {"type": "document",
               "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
        if r.get("media_filename"):
            blk["title"] = r["media_filename"]
        return blk
    mimetype = res.get("mimetype") or r.get("media_mimetype") or "image/jpeg"
    return {"type": "image",
            "source": {"type": "base64", "media_type": mimetype, "data": b64}}


async def _attach_media(records: list[dict], evolution, settings, trace: Trace,
                        tid: str, loop_id: str | None) -> list[dict]:
    """Download the window's images/PDFs and return Anthropic content blocks — the twin of
    `_transcribe_audio`, but media can't fold into text so it travels as blocks. Each media row
    is marked IN PLACE so `build_labeled_transcript` emits a line even for a caption-less file.
    Bounded-concurrency gather; capped at settings.max_context_media; a failed/oversized download
    degrades to a marker with no block, and a per-turn byte budget stays under Anthropic's 32MB
    request ceiling — so a pathological file never breaks the turn."""
    if not settings.media_enabled:
        return []
    media = [r for r in records if _in_media_scope(r)]
    if not media:
        return []

    cap = settings.max_context_media
    todo, overflow = media[:cap], media[cap:]
    for r in overflow:
        _mark(r, "omitted")
    if overflow and loop_id:
        trace.code(tid, node="context", loop_id=loop_id, media_dropped=len(overflow))

    sem = asyncio.Semaphore(settings.transcription_concurrency)

    async def download(r: dict) -> tuple[dict, dict | None]:
        async with sem:
            return r, await evolution.get_media_base64(r["id"])

    fetched = await asyncio.gather(*(download(r) for r in todo))

    # Build blocks sequentially so the per-turn byte budget is honoured deterministically
    # (drop the rows that would overflow to markers), preserving chronological order.
    blocks: list[dict] = []
    used = 0
    budget = settings.media_request_budget_bytes
    for r, res in fetched:
        b64 = (res or {}).get("base64")
        if not b64:
            _mark(r, "unavailable")
            continue
        approx = (len(b64) * 3) // 4  # decoded bytes ≈ 3/4 of the base64 length
        if approx > settings.media_max_item_bytes or used + approx > budget:
            _mark(r, "too large")
            continue
        used += approx
        blocks.append(_media_block(r, res, b64))
        _mark(r, "ok")
    return blocks


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

    # Filter assistant-origin messages. On a window CONTINUATION her past replies already live
    # in the checkpoint as AIMessages, so re-ingesting them from history would duplicate them —
    # drop anything recognised by recorded id OR header stamp. On a RESET the checkpoint was just
    # wiped, so there is nothing to duplicate: KEEP her own replies as context, so she can see
    # what she already did and not redo an already-answered order. But keep only the ones we can
    # attribute — a header-stamped reply is labeled "AI Assistant" by build_labeled_transcript;
    # a reply recognised ONLY by recorded id (header missing/mangled) has no header for label_for
    # to key on and would be misattributed to the owner, so drop that one rather than mislabel it.
    if reset:
        new = [
            r for r in new
            if not (echoes.is_ours(jid, r.get("id"))
                    and not is_own_message(r.get("text") or "", owner))
        ]
    else:
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
    # See images & PDFs: download the window's media and build inline base64 blocks. Runs after
    # transcription so both passes have marked their rows before the transcript string is built.
    media_blocks = await _attach_media(new, evolution, settings, trace, tid, loop_id)

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
    # The turn is a plain string unless there are media blocks; then it's a content list with
    # the labeled transcript first, followed by the inline image/document blocks (chronological).
    content: object | None = None
    if transcript.strip() and media_blocks:
        content = [{"type": "text", "text": transcript}, *media_blocks]
    elif transcript.strip():
        content = transcript
    elif media_blocks:
        content = media_blocks
    if content is not None:
        msgs.append({"role": "user", "content": content})
    if msgs:
        update["messages"] = msgs
    return update
