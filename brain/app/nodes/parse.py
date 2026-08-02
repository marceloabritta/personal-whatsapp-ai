"""parse — normalise the Evolution MESSAGES_UPSERT payload.

Returns only the fields it sets (partial update) — never the whole state — so the
`messages` add_messages channel isn't re-appended each node."""
from __future__ import annotations

from ..identity import is_own_message, matched_tag
from ..state import MessageState
from ..trace import Trace
from ..whatsapp import extract_text


def parse_node(
    state: MessageState, *, trace: Trace, tags: list[str], owner_name: str
) -> dict:
    raw = state.get("raw") or {}
    data = raw.get("data", raw)
    key = data.get("key") or {}

    from_me = bool(key.get("fromMe"))
    remote_jid = key.get("remoteJid") or ""
    text = extract_text(data.get("message")).strip()
    number = remote_jid.split("@")[0]

    # main._run mints the trace id and puts it on the contextvar before ainvoke; honour
    # it so the whole run shares one id. Fallback (direct-invoke tests): mint here.
    tid = state.get("trace_id") or trace.start(number)
    tag = matched_tag(text, tags) if from_me else None

    trace.code(
        tid, node="parse", chat=remote_jid, from_me=from_me,
        is_own=is_own_message(text, owner_name), tag=tag, text_preview=text[:80],
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
        "error_category": "none",
    }
