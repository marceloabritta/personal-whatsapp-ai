"""Read fields out of an Evolution `message` object. Step 1 needs only the text;
the media/quote helpers from the old lib/whatsapp.js come back in later steps."""
from __future__ import annotations


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
