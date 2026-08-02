"""send — deliver the reply through the one Evolution client, and close the trace
with both the code-level API record and the user-level transcript line."""
from __future__ import annotations

from ..state import MessageState
from ..trace import Trace


async def send_node(state: MessageState, *, evolution, trace: Trace) -> MessageState:
    tid = state["trace_id"]
    ok = await evolution.send_text(state["number"], state["reply"])
    state["sent"] = ok

    trace.code(
        tid,
        node="send",
        http=f"POST /message/sendText/{evolution.instance}",
        body={"number": state["number"], "text": state["reply"]},
        ok=ok,
    )
    trace.user(tid, "mary", state["reply_body"], status="delivered" if ok else "failed")
    return state
