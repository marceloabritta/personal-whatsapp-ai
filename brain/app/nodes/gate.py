"""gate — the whole point of Step 1.

A run proceeds to a reply on exactly these conditions:
  - never for Mary's own echoed messages;
  - fresh trigger: the OWNER (fromMe) sends a message carrying @mary  -> opens a session;
  - continuation: the OWNER sends into a chat whose session is already open.

Everything else stops here. The gate writes a one-line "ignored" trace for stops so
the trace store shows the decision without carrying a full transcript for chatter."""
from __future__ import annotations

from ..state import MessageState
from ..trace import Trace


def gate_node(state: MessageState, *, sessions, trace: Trace) -> MessageState:
    tid = state["trace_id"]
    jid = state["remote_jid"]

    if state["is_own"]:
        state["decision"], state["trigger"] = "stop", None
        trace.code(tid, node="gate", decision="stop", reason="own_message")
        return state

    if state["from_me"] and state["tag"]:
        sessions.open(jid)
        state["decision"], state["trigger"] = "ack", "tag"
        trace.user(tid, "you", state["text"])
        trace.code(
            tid, node="gate", decision="ack", trigger="tag",
            owner=True, tag=state["tag"], session="opened",
        )
        return state

    if state["from_me"] and sessions.is_open(jid):
        state["decision"], state["trigger"] = "ack", "session"
        trace.user(tid, "you", state["text"])
        trace.code(tid, node="gate", decision="ack", trigger="session")
        return state

    state["decision"], state["trigger"] = "stop", None
    trace.code(
        tid, node="gate", decision="stop", reason="no_trigger",
        from_me=state["from_me"], tag=state["tag"],
    )
    return state


def route_after_gate(state: MessageState) -> str:
    """Conditional edge: 'ack' continues to the reply, 'stop' ends the run."""
    return state["decision"]
