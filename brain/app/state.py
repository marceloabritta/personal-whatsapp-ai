"""The graph's shared state. One dict flows parse -> gate -> ack -> send."""
from __future__ import annotations

from typing import Optional, TypedDict


class MessageState(TypedDict, total=False):
    # Input
    raw: dict  # the full webhook body

    # Parsed (parse node)
    trace_id: str
    from_me: bool
    remote_jid: str
    msg_id: Optional[str]
    text: str
    push_name: Optional[str]
    number: str  # reply target = remote_jid without the @domain
    ts: int
    is_own: bool
    tag: Optional[str]

    # Decision (gate node)
    decision: str  # "ack" | "stop"
    trigger: Optional[str]  # "tag" | "session" | None

    # Output (ack + send nodes)
    reply_body: str  # the human-readable body (for the transcript)
    reply: str  # the framed message actually sent (header + body)
    sent: bool
