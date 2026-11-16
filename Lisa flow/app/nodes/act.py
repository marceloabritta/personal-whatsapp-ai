"""act — apply the model's JSON decision to the wire and the window, then emit the
one-row activation record (§10/§12).

  keep_listening · message  → send, refresh the 60s window
  awaiting_answer · message → send, hold the window for AWAIT_WINDOW_SECONDS (5 min)
  any · null                → stay silent, keep the window — never shorter than it was
  nothing new to read       → the same: silent, window kept (context found no new message)
  close · message           → send (model writes its own sign-off), drop the window
  close · null              → drop the window, nothing sent
The message goes out verbatim. The reply header is localised to the session language,
which is locked at the tag that opened the window.

After the message, the files a history search asked for go out, into his chat with himself only
(`_send_history_files`)."""
from __future__ import annotations

import logging
import time

from ..identity import frame
from ..state import MessageState
from ..trace import Trace

AWAITING = "awaiting_answer"
log = logging.getLogger("mary.act")


def _waiting(state: MessageState) -> bool:
    """Is Lisa waiting on an answer? A held write (a confirmation card is out) and a gather goal
    with open questions are read off the state — no model judgment involved. The model's own
    "awaiting_answer" covers the question that is neither, like "shall I book just the next one?"."""
    workflow = state.get("workflow") or {}
    return bool(state.get("llm_state") == AWAITING or state.get("pending_action")
                or workflow.get("open_questions"))


def window_ttl(state: MessageState, settings) -> int:
    """How long the window stays open after this turn. A setup loop holds a longer window:
    finding a contact and forwarding their card means leaving this chat. So does a question:
    looking up an email or checking a date takes minutes, not the conversational seconds.

    A history search holds no longer window of its own. It holds its loop, and his chat with
    himself is also his notepad: every untagged line in an open window goes to the search agent,
    the costliest call she makes. When the agent asks him something or offers a next step it says
    "awaiting_answer", and that holds the five minutes like any question; otherwise it is the
    conversational window, and a fresh tag reopens the search."""
    ttl = settings.loop_ttl_seconds
    if state.get("loop_domain") == "setup":
        ttl = max(ttl, settings.setup_window_seconds)
    if _waiting(state):
        ttl = max(ttl, settings.await_window_seconds)
    return ttl


async def _send_history_files(state: MessageState, asked: list, *, history, echoes, settings,
                              trace: Trace) -> int:
    """Send the files a history search asked for, after its message. Returns how many went.

    Only into his chat with himself — HistoryMedia.send_to_owner has no other destination, and this
    refuses anywhere else as well. Capped at settings.history_max_files. Each sent file's id is
    recorded as an echo: a file Lisa sends comes back through the webhook as his own fromMe
    message, and a document has no text, so no header can mark it as hers — the id is the only
    thing that stops it waking her as if he had sent it. A file that cannot be sent (expired at
    WhatsApp) is logged; the agent's message already said what it could and could not find."""
    tid, jid, loop_id = state["trace_id"], state["remote_jid"], state.get("loop_id")
    if not state.get("is_self_chat") or history is None or getattr(history, "media", None) is None:
        trace.code(tid, node="act", loop_id=loop_id, history_files_dropped=len(asked),
                   reason="not_self_chat" if not state.get("is_self_chat") else "unavailable")
        return 0
    ids = list(dict.fromkeys(m for m in asked if isinstance(m, str) and m))
    cap = max(0, int(getattr(settings, "history_max_files", 3) or 0))
    todo, over = ids[:cap], ids[cap:]
    if over:
        trace.code(tid, node="act", loop_id=loop_id, history_files_dropped=len(over), reason="cap")
    # The id may come back under his phone JID or the chat's own; record it under both.
    echo_jids = list(dict.fromkeys(j for j in (jid, getattr(settings, "owner_jid", "")) if j))
    sent = 0
    for msg_id in todo:
        try:
            res = await history.media.send_to_owner(msg_id) or {}
        except Exception as exc:
            log.warning("history file send failed: %s", exc)
            res = {"ok": False, "error": "failed"}
        sent_id = res.get("sent_id")
        if sent_id:
            for j in echo_jids:
                echoes.record(j, sent_id)
        ok = bool(res.get("ok"))
        sent += ok
        # What went and how, never what it holds: no bytes, no file name.
        trace.code(tid, node="act", loop_id=loop_id, history_file=msg_id, ok=ok,
                   error=None if ok else (res.get("error") or "failed"),
                   mimetype=res.get("mimetype"), wa_id=sent_id or None)
    return sent


async def act_node(
    state: MessageState, *, evolution, sessions, echoes, settings, trace: Trace, reaper=None,
    history=None,
) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    number = state["number"]
    llm_state = state.get("llm_state") or "keep_listening"
    body = state.get("reply_body")

    # Session language is locked in `reason` on the first pass after the tag and carried on the
    # state; the header just reads it (fall back only if a turn never produced one).
    session_lang = state.get("session_lang") or state.get("lang") or "en"

    reply = None
    sent = False
    sent_id = None
    delivery = "silent"

    if body:
        # Send the model's message exactly as it decided — no programmatic trailer.
        text = body.rstrip()
        reply = frame(text, settings.owner_name, session_lang)
        sent_id = await evolution.send_text(number, reply)
        sent = sent_id is not None  # None == failure; "" == sent, id unknown
        delivery = "ok" if sent else "failed"
        if sent_id:
            echoes.record(jid, sent_id)  # so we never re-ingest our own reply
        trace.user(tid, "AI Assistant", text, loop_id=state.get("loop_id"),
                   wa_id=sent_id or None, status="delivered" if sent else "failed")

    asked_files = state.get("history_files") or []
    files_sent = 0
    if asked_files:
        files_sent = await _send_history_files(state, asked_files, history=history,
                                               echoes=echoes, settings=settings, trace=trace)

    close_reason = None
    if llm_state == "close":
        sessions.close(jid)
        close_reason = "model"
        # Fast lane for the reviewer: this session just ended, so it can be graded now rather
        # than waiting for the sweeper to notice it went quiet. Non-blocking, and a full queue
        # simply drops it — the sweeper is the guarantee, this is only the latency win.
        if reaper is not None:
            reaper.offer(state.get("loop_id"))
    else:
        ttl = window_ttl(state, settings)
        if not sent:
            # A silent turn asked nothing and answered nothing, so whatever Lisa is waiting on
            # still stands. Someone else's chatter while she waits must not cut a 5-minute window
            # back to 60s; only her next message decides what she is waiting for.
            ttl = max(ttl, sessions.ttl_left(jid))
        sessions.open(jid, ttl)  # refresh the window TTL

    # activation record (prompt_version stitched here so _record stays dependency-free)
    usage = state.get("usage") or {}
    trace.code(
        tid, node="record",
        loop_id=state.get("loop_id"),
        trigger=state.get("trigger"),
        loop_started_ts=state.get("loop_started_ts"),
        activation_message_id=state.get("msg_id"),
        chat_id=jid,
        context_message_ids=state.get("context_message_ids") or [],
        provider=state.get("provider"),
        model=state.get("model"),
        prompt_version=settings.prompt_version,
        provider_request_id=state.get("provider_request_id"),
        input_tokens=usage.get("input"),
        output_tokens=usage.get("output"),
        latency_ms=state.get("latency_ms"),
        stop_reason=state.get("stop_reason"),
        state=llm_state,
        close_reason=close_reason,
        lang=session_lang,
        response=body,
        actions=state.get("action_results") or [],
        delivery_result=delivery,
        error_category=state.get("error_category") or "none",
        # No model ran: an earlier activation had read this message. Marked so the review does
        # not grade it as a silence she chose (review/store.py).
        nothing_new=state.get("nothing_new") or None,
        # Only on a turn a history search asked to send files on, so every other record is as it was.
        **({"files_asked": len(asked_files), "files_sent": files_sent} if asked_files else {}),
    )

    update = {
        "reply": reply,
        "sent": sent,
        "close_reason": close_reason,
        "session_lang": session_lang,
    }
    # History is appended here — the single place a *sent* message enters the thread. The raw
    # model text (not the framed header) is what the model should see it said.
    if body and sent:
        update["messages"] = [{"role": "assistant", "content": body.rstrip()}]
    if sent:
        # Is her card still the last thing she said? A yes answers her last message, so once she
        # has said anything else, the card is no longer what he is saying yes to (resolve.py).
        update["spoke_after_card"] = not state.get("card_out")
    if asked_files:
        update["history_files"] = []  # sent once; a second `act` in this activation sends none
    if sent and state.get("card_out"):
        # Which message the card was, so a fresh tag can find it in the chat above and read the
        # owner's answer under it. The framed text is the fallback when Evolution gave no id.
        update["pending_card"] = {"id": sent_id or None, "ts": int(time.time()),
                                  "text": reply, "sig": state["card_out"]}
    return update


def route_after_act(state: MessageState) -> str:
    """Almost every turn ends here. The exception is a write that has more waiting behind it: this
    message is its result, and now the next step, or the skill that called this one, carries on."""
    return "handoff" if state.get("next_call") or state.get("resume_caller") else "end"
