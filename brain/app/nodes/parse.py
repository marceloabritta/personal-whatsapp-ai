"""parse — normalise the Evolution MESSAGES_UPSERT payload into MessageState.

The envelope: body.data.key = {fromMe, remoteJid, id}, body.data.message, plus
messageTimestamp and pushName. Owner-vs-tag decisions happen in the gate; here we
only extract and stamp the run's trace id."""
from __future__ import annotations

from ..identity import is_own_message, matched_tag
from ..state import MessageState
from ..trace import Trace
from ..whatsapp import extract_text


def parse_node(
    state: MessageState, *, trace: Trace, tags: list[str], owner_name: str
) -> MessageState:
    raw = state.get("raw") or {}
    data = raw.get("data", raw)
    key = data.get("key") or {}

    from_me = bool(key.get("fromMe"))
    remote_jid = key.get("remoteJid") or ""
    text = extract_text(data.get("message")).strip()
    number = remote_jid.split("@")[0]

    tid = trace.start(number)
    # Only the owner's account (fromMe) can trigger via tag.
    tag = matched_tag(text, tags) if from_me else None

    state.update(
        {
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
        }
    )

    trace.code(
        tid,
        node="parse",
        chat=remote_jid,
        from_me=from_me,
        is_own=state["is_own"],
        tag=tag,
        text_preview=text[:80],
    )
    return state
