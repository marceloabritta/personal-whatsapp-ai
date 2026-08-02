"""ack — compose the reply. Step 1 is a fixed acknowledgement; Step 2 swaps this
for real reasoning. The body is stamped with Mary's header so her own message is
recognised (and skipped) when Evolution echoes it back."""
from __future__ import annotations

from ..config import Settings
from ..identity import frame
from ..state import MessageState
from ..trace import Trace


def ack_node(state: MessageState, *, settings: Settings, trace: Trace) -> MessageState:
    body = settings.ack_text
    state["reply_body"] = body
    state["reply"] = frame(body)
    trace.code(state["trace_id"], node="ack", reply_body=body)
    return state
