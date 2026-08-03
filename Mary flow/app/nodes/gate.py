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


def gate_node(state: MessageState, *, sessions, trace: Trace) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]

    if state["is_own"]:
        trace.code(tid, node="gate", decision="stop", reason="own_message")
        return {"decision": "stop", "trigger": None}

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

    trace.code(
        tid, node="gate", decision="stop", reason="no_trigger",
        from_me=state["from_me"], tag=state["tag"],
    )
    return {"decision": "stop", "trigger": None}


def route_after_gate(state: MessageState) -> str:
    return state["decision"]
