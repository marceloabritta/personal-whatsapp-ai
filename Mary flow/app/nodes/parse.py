"""parse — normalise the Evolution MESSAGES_UPSERT payload.

Returns only the fields it sets (partial update) — never the whole state — so the
`messages` add_messages channel isn't re-appended each node."""
from __future__ import annotations

from ..identity import is_own_message, matched_tag
from ..intent import classify_transcribe
from ..state import MessageState
from ..trace import Trace
from ..whatsapp import extract_text, get_quoted


def parse_node(
    state: MessageState, *, trace: Trace, tags: list[str], owner_name: str, settings
) -> dict:
    raw = state.get("raw") or {}
    data = raw.get("data", raw)
    key = data.get("key") or {}

    from_me = bool(key.get("fromMe"))
    remote_jid = key.get("remoteJid") or ""
    text = extract_text(data.get("message")).strip()
    number = remote_jid.split("@")[0]

    tid = trace.start(number)
    tag = matched_tag(text, tags) if from_me else None

    # Voice-note reply? Capture the quoted audio id, and (when the owner tagged it) decide
    # whether this is a pure transcribe request — the fast lane — with NO model call.
    quoted = get_quoted(data)
    quoted_audio_id = quoted["id"] if (quoted and quoted["has_audio"]) else None
    transcribe_only = False
    if (
        from_me and tag and quoted_audio_id and settings.transcription_enabled
    ):
        transcribe_only = classify_transcribe(
            text, tags, quoted.get("text"),
            threshold=settings.transcribe_fuzzy_threshold,
            on_empty=settings.transcribe_on_empty_reply,
        ) == "transcribe"

    trace.code(
        tid, node="parse", chat=remote_jid, from_me=from_me,
        is_own=is_own_message(text, owner_name), tag=tag, text_preview=text[:80],
        quoted_audio=bool(quoted_audio_id), transcribe_only=transcribe_only,
    )

    return {
        "trace_id": tid,
        "from_me": from_me,
        "remote_jid": remote_jid,
        "msg_id": key.get("id"),
        "text": text,
        "push_name": data.get("pushName"),
        "number": number,
        "ts": int(data.get("messageTimestamp") or 0),
        "is_own": is_own_message(text, owner_name),
        "tag": tag,
        "quoted_audio_id": quoted_audio_id,
        "transcribe_only": transcribe_only,
        "error_category": "none",
    }
