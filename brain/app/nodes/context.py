"""context — assemble the turn (§01/§02).

First activation (not initialized): seed the last CONTEXT_WINDOW_MESSAGES from Evolution.
Later: fetch only messages after the cursor. Assistant-origin messages are filtered
(they're already AIMessages in the checkpoint). The new messages become one labeled
user turn; the cursor advances."""
from __future__ import annotations

from ..identity import is_own_message
from ..state import MessageState
from ..trace import Trace
from ..whatsapp import build_labeled_transcript


def _after_cursor(records: list[dict], cursor: str | None, window: int) -> list[dict]:
    if cursor:
        for i, r in enumerate(records):
            if r["id"] == cursor:
                return records[i + 1 :]
    # cursor unknown (fell out of the fetched range) — fall back to the window
    return records[-window:]


async def context_node(state: MessageState, *, evolution, settings, trace: Trace) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    owner = settings.owner_name
    window = settings.context_window_messages

    records = await evolution.fetch_history(jid)  # oldest → newest
    if not state.get("initialized"):
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

    # Filter assistant-origin — already AIMessages; never re-ingest.
    new = [r for r in new if not is_own_message(r.get("text") or "", owner)]

    transcript = build_labeled_transcript(new, owner)
    ids = [r["id"] for r in new if r.get("id")]
    newest = ids[-1] if ids else state.get("last_whatsapp_message_id")

    trace.user(tid, "you", state.get("text") or "")
    trace.code(
        tid, node="context", initialized=bool(state.get("initialized")),
        ingested=len(new), context_message_ids=ids,
    )

    update: dict = {
        "initialized": True,
        "last_whatsapp_message_id": newest,
        "context_message_ids": ids,
    }
    if transcript.strip():
        update["messages"] = [{"role": "user", "content": transcript}]
    return update
