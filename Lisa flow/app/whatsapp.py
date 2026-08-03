"""Read fields out of Evolution `message` objects, and render a labeled transcript.

Ported from secretary/1. Orchestrator/lib/whatsapp.js (extractText + the labeled
transcript idea): the same account sends everything, so the speaker label comes from
the reply header, not the message direction."""
from __future__ import annotations

from .identity import is_own_message


def extract_text(msg: dict | None) -> str:
    """Text from an Evolution `message` object (several possible shapes)."""
    if not msg:
        return ""
    return (
        msg.get("conversation")
        or (msg.get("extendedTextMessage") or {}).get("text")
        or (msg.get("imageMessage") or {}).get("caption")
        or (msg.get("videoMessage") or {}).get("caption")
        or ""
    )


def is_audio_message(msg: dict | None) -> bool:
    """True for a WhatsApp voice note. `audioMessage` covers both a recorded voice note
    (ptt) and a sent audio file; `pttMessage` is the older explicit push-to-talk shape."""
    if not msg:
        return False
    return "audioMessage" in msg or "pttMessage" in msg


def get_quoted(data: dict | None) -> dict | None:
    """The message this one replies to, or None. Pass the whole webhook `data` object.

    Evolution delivers the reply context in one of two places depending on shape:
      - data.contextInfo            — a plain-text ("conversation") reply: contextInfo is a
                                      SIBLING of `message`, not inside it.
      - message.<type>.contextInfo  — some payloads nest it under the message.
    Sibling first, then the nested shapes. Returns {id, has_audio, media_type, text}."""
    if not data:
        return None
    msg = data.get("message") or {}
    ctx = (
        data.get("contextInfo")
        or (msg.get("extendedTextMessage") or {}).get("contextInfo")
        or (msg.get("imageMessage") or {}).get("contextInfo")
        or (msg.get("videoMessage") or {}).get("contextInfo")
        or (msg.get("audioMessage") or {}).get("contextInfo")
        or None
    )
    if not ctx:
        return None
    qid = ctx.get("stanzaId") or ctx.get("quotedMessageId")
    if not qid:
        return None
    quoted = ctx.get("quotedMessage") or {}
    return {
        "id": qid,
        "has_audio": is_audio_message(quoted),
        "media_type": _media_type(quoted),
        "text": extract_text(quoted).strip(),
    }


def _media_type(msg: dict) -> str:
    if "audioMessage" in msg or "pttMessage" in msg:
        return "audio"
    if "imageMessage" in msg:
        return "image"
    if "videoMessage" in msg:
        return "video"
    if "documentMessage" in msg:
        return "document"
    return "text"


def label_for(record: dict, owner_name: str) -> str:
    """AI Assistant (own header) | owner | contact — the transcript's three speakers.

    Provenance: a record from a voice note carries `is_audio`, so its line is marked
    "(voice message — transcribed)". The model must know a line was SPOKEN — a transcript
    can miss punctuation, mis-hear names/numbers, and carry recognition slips — so it can
    weigh it as speech and ask instead of guessing when one is garbled."""
    text = record.get("text") or ""
    if is_own_message(text, owner_name):
        speaker = "AI Assistant"
    elif record.get("from_me"):
        speaker = owner_name
    else:
        speaker = record.get("push_name") or "Contact"
    if record.get("is_audio"):
        speaker += " (voice message — transcribed)"
    return speaker


def build_labeled_transcript(records: list[dict], owner_name: str) -> str:
    """Chronological "Speaker: text" lines. Empty-text records are skipped."""
    lines = []
    for r in records:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{label_for(r, owner_name)}: {text}")
    return "\n".join(lines)
