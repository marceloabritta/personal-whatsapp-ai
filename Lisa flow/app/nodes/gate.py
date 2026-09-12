"""gate — open or continue the listening window.

  - never for Mary's own echoed messages;
  - fresh trigger: the OWNER (fromMe) sends a message carrying @mary → opens the window;
  - continuation: ANY message (owner or contact) while the window is open — option (a),
    she listens to everything and decides per-message whether to speak (§05).

Everything else stops here with a one-line "ignored" trace.

Loop identity: a fresh @mary tag opens a NEW loop (mints a loop_id — the grouping key
for the durable log, matching the checkpointer's reset-on-tag). A window continuation
carries the open loop's id forward. Only loop-scoped activations reach the log."""
from __future__ import annotations

import time

from ..state import MessageState
from ..trace import Trace


def gate_node(state: MessageState, *, sessions, roster=None, settings=None,
              trace: Trace) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]

    if state["is_own"]:
        trace.code(tid, node="gate", decision="stop", reason="own_message")
        return {"decision": "stop", "trigger": None}

    # Automatic transcription. Checked BEFORE the tag branch — a trigger tag is text, so it can
    # never collide with a voice note — and after the own-message check above, so Lisa can never
    # react to her own output. The roster read is a pure dict lookup: no awaits on this path.
    if state.get("is_audio") and roster is not None and settings is not None \
            and settings.auto_transcribe_enabled and settings.transcription_enabled:
        rule = roster.should_transcribe(
            [state.get("chat_key"), state.get("alt_key")], bool(state["from_me"])
        )
        if rule:
            # A listening window open on this chat means Lisa is mid-conversation here: post the
            # transcript AND let the normal turn run. They are different jobs.
            also_run = sessions.is_open(jid)
            trace.code(tid, node="gate", decision="auto", trigger="roster",
                       chat_key=rule["chat_key"], direction=rule["direction"],
                       also_run=also_run)
            return {"decision": "auto", "trigger": "roster", "auto_rule": rule,
                    "auto_then_run": also_run}

    if state["from_me"] and state["tag"]:
        # Fast lane: a pure transcribe request (reply to a voice note + tag, nothing else)
        # short-circuits the model. One-shot — no listening window, no loop opened; the
        # transcribe node does the work and mints its own loop for the durable log.
        if state.get("transcribe_only") and state.get("quoted_audio_id"):
            trace.code(tid, node="gate", decision="transcribe", trigger="transcribe")
            return {"decision": "transcribe", "trigger": "transcribe"}
        sessions.open(jid)  # open the window (act refreshes/closes it)
        loop_id = trace.new_loop_id(state["number"])  # every tag = a new loop
        started = state.get("ts") or int(time.time())
        trace.code(tid, node="gate", loop_id=loop_id, decision="run",
                   trigger="tag", window="opened")
        return {"decision": "run", "trigger": "tag", "loop_id": loop_id,
                "loop_opened": True, "loop_started_ts": started}

    if sessions.is_open(jid):
        loop_id = state.get("loop_id")
        trace.code(tid, node="gate", loop_id=loop_id, decision="run", trigger="window")
        return {"decision": "run", "trigger": "window", "loop_id": loop_id,
                "loop_opened": False}

    # A message arriving after the window lapsed is dropped here. Carry the STALE loop_id the
    # checkpointer still holds so the drop reaches the durable log at all — Trace only persists
    # loop-scoped records, so without this the message vanishes with no trace anywhere and a
    # follow-up nobody ever answered stays invisible. Behaviour is unchanged; this only decides
    # whether the event is written down.
    stale_loop = state.get("loop_id")
    trace.code(
        tid, node="gate", decision="stop", reason="no_trigger", loop_id=stale_loop,
        window="expired" if stale_loop else None,
        from_me=state["from_me"], tag=state["tag"],
    )
    return {"decision": "stop", "trigger": None}


def route_after_gate(state: MessageState) -> str:
    return state["decision"]
