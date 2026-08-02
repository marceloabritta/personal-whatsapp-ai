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


def label_for(record: dict, owner_name: str) -> str:
    """AI Assistant (own header) | owner | contact — the transcript's three speakers."""
    text = record.get("text") or ""
    if is_own_message(text, owner_name):
        return "AI Assistant"
    if record.get("from_me"):
        return owner_name
    return record.get("push_name") or "Contact"


def build_labeled_transcript(records: list[dict], owner_name: str) -> str:
    """Chronological "Speaker: text" lines. Empty-text records are skipped."""
    lines = []
    for r in records:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{label_for(r, owner_name)}: {text}")
    return "\n".join(lines)
