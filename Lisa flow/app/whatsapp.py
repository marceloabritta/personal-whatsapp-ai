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


def audio_seconds(msg: dict | None) -> float | None:
    """Declared length of a voice note, in seconds, or None. Read from the payload (not the
    provider), so the auto path can skip an over-long clip BEFORE paying to transcribe it."""
    if not msg:
        return None
    node = msg.get("audioMessage") or msg.get("pttMessage") or {}
    try:
        return float(node.get("seconds"))
    except (TypeError, ValueError):
        return None


def chat_key(jid: str | None) -> str:
    """A chat JID normalised to the stable key the roster stores.

    The local part identifies the chat in both shapes: a phone number for a 1:1
    (`5511999@s.whatsapp.net`), the group id for a group (`1203…@g.us`). A device suffix
    (`:12`) is dropped — the same chat can arrive with or without one."""
    local = (jid or "").split("@")[0]
    return local.split(":")[0].strip().lower()


def chat_kind(jid: str | None) -> str:
    """"group" | "contact" — which kind of chat this JID names."""
    return "group" if (jid or "").endswith("@g.us") else "contact"


def _vcard_number(vcard: str) -> str:
    """The WhatsApp id from a vCard's TEL line.

    WhatsApp writes the account id into the TEL parameters, which is the authoritative source:

        TEL;type=CELL;type=VOICE;waid=5511976004417:+55 11 97600-4417

    Prefer `waid`; fall back to the digits of the printed number when a card was exported by a
    client that omits it. Returns "" when nothing usable is present."""
    for line in (vcard or "").splitlines():
        if not line.upper().startswith("TEL"):
            continue
        params, _, value = line.partition(":")
        for part in params.split(";"):
            k, _, v = part.partition("=")
            if k.strip().lower() == "waid" and v.strip():
                return "".join(c for c in v if c.isdigit())
        digits = "".join(c for c in value if c.isdigit())
        if digits:
            return digits
    return ""


def contact_cards(msg: dict | None) -> list[dict]:
    """Every contact card forwarded in this message, as [{name, number}].

    A card carries no text, so `extract_text` returns "" for it — this is the only way the
    graph learns a card was sent. Both shapes are read: `contactMessage` (one card) and
    `contactsArrayMessage` (several forwarded at once). Cards whose vCard yields no number are
    dropped: a card we cannot key on is not a candidate, and guessing is what this whole path
    exists to avoid."""
    if not msg:
        return []
    nodes = []
    if msg.get("contactMessage"):
        nodes.append(msg["contactMessage"])
    arr = msg.get("contactsArrayMessage") or {}
    nodes.extend(arr.get("contacts") or [])

    out: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        number = _vcard_number(node.get("vcard") or "")
        if not number or number in seen:
            continue
        seen.add(number)
        out.append({"name": (node.get("displayName") or "").strip(), "number": number})
    return out


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
    if "documentMessage" in msg or "documentWithCaptionMessage" in msg:
        return "document"
    return "text"


def _doc_node(msg: dict) -> dict:
    """The documentMessage node, unwrapping the documentWithCaptionMessage envelope
    WhatsApp uses when a PDF is sent with a caption."""
    if "documentMessage" in msg:
        return msg["documentMessage"] or {}
    dw = msg.get("documentWithCaptionMessage") or {}
    return (dw.get("message") or {}).get("documentMessage") or dw.get("documentMessage") or {}


def media_info(msg: dict | None) -> dict:
    """Media provenance for a record: {type, mimetype, filename}.

    `type` is `_media_type`; `mimetype`/`filename` are the DECLARED values from the payload
    (used downstream to gate PDFs and title the document block). The actual bytes — and the
    authoritative mimetype — are fetched separately via `get_media_base64`."""
    msg = msg or {}
    mt = _media_type(msg)
    if mt == "document":
        node = _doc_node(msg)
        return {"type": mt, "mimetype": node.get("mimetype"), "filename": node.get("fileName")}
    node = (
        msg.get("imageMessage")
        or msg.get("videoMessage")
        or msg.get("audioMessage")
        or {}
    )
    return {"type": mt, "mimetype": node.get("mimetype"), "filename": None}


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
    # Provenance annotation — tell the model what a line's block/marker refers to, the same
    # way audio is flagged so it weighs a garbled transcript as speech (see the docstring).
    if record.get("is_audio"):
        speaker += " (voice message — transcribed)"
    elif record.get("media_type") == "image":
        speaker += " (image)"
    elif record.get("media_type") == "document":
        name = record.get("media_filename")
        speaker += f" (PDF: {name})" if name else " (PDF)"
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
