"""gate — open or continue the listening window.

  - never for Mary's own echoed messages;
  - fresh trigger: the OWNER (fromMe) sends a message carrying @mary → opens the window;
  - continuation: ANY message (owner or contact) while the window is open — option (a),
    she listens to everything and decides per-message whether to speak (§05).

Everything else stops here with a one-line "ignored" trace."""
from __future__ import annotations

from ..state import MessageState
from ..trace import Trace


def gate_node(state: MessageState, *, sessions, trace: Trace) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]

    if state["is_own"]:
        trace.code(tid, node="gate", decision="stop", reason="own_message")
        return {"decision": "stop", "trigger": None}

    if state["from_me"] and state["tag"]:
        sessions.open(jid)  # open the window (act refreshes/closes it)
        trace.code(tid, node="gate", decision="run", trigger="tag", window="opened")
        return {"decision": "run", "trigger": "tag"}

    if sessions.is_open(jid):
        trace.code(tid, node="gate", decision="run", trigger="window")
        return {"decision": "run", "trigger": "window"}

    trace.code(
        tid, node="gate", decision="stop", reason="no_trigger",
        from_me=state["from_me"], tag=state["tag"],
    )
    return {"decision": "stop", "trigger": None}


def route_after_gate(state: MessageState) -> str:
    return state["decision"]
