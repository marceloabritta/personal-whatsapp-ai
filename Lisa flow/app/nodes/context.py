"""context — assemble the turn (§01/§02).

A fresh @mary tag starts a NEW loop: it wipes the checkpointed conversation memory
and re-seeds the last CONTEXT_WINDOW_MESSAGES from Evolution, so one loop never
leaks into the next on the same chat. A window continuation (untagged follow-up
while the loop is open) fetches only messages after the cursor and keeps the loop's
memory. Assistant-origin messages are filtered (they're already AIMessages in the
checkpoint). The new messages become one labeled user turn; the cursor advances."""
from __future__ import annotations

from langchain_core.messages import RemoveMessage

from ..identity import is_own_message
from ..state import MessageState
from ..trace import Trace
from ..whatsapp import build_labeled_transcript, label_for


def _log_transcript(trace: Trace, tid: str, loop_id: str | None, records: list[dict],
                    owner: str) -> None:
    """Record each chat message into the loop's transcript stream (deduped by id in the
    store). Both sides — owner, contact, and Mary — so the log is the real conversation."""
    if not loop_id:
        return
    for r in records:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        trace.user(tid, label_for(r, owner), text, loop_id=loop_id,
                   wa_id=r.get("id"), ts=r.get("ts"), from_me=bool(r.get("from_me")))


def _after_cursor(records: list[dict], cursor: str | None, window: int) -> list[dict]:
    if cursor:
        for i, r in enumerate(records):
            if r["id"] == cursor:
                return records[i + 1 :]
    # cursor unknown (fell out of the fetched range) — fall back to the window
    return records[-window:]


async def context_node(
    state: MessageState, *, evolution, echoes, settings, trace: Trace
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

    transcript = build_labeled_transcript(new, owner)
    ids = [r["id"] for r in new if r.get("id")]
    newest = ids[-1] if ids else state.get("last_whatsapp_message_id")

    # Durable transcript. On a fresh tag (loop open) log the whole seed — the ~30
    # messages before the tag, both sides — as the loop's opening context. On a window
    # continuation log only the new inbound; Mary's own replies are logged by `act`.
    loop_id = state.get("loop_id")
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
