"""The graph's shared state.

`messages` is persisted per chat by the checkpointer (add_messages reducer);
everything else is per-turn scratch that the nodes overwrite each run."""
from __future__ import annotations

from typing import Annotated, Optional, TypedDict

from langgraph.graph.message import add_messages


class MessageState(TypedDict, total=False):
    # --- persisted memory (checkpointer) ---
    messages: Annotated[list, add_messages]  # model conversation history
    last_whatsapp_message_id: Optional[str]  # ingestion cursor
    initialized: bool  # seeded the window yet?
    session_lang: Optional[str]  # language locked at the tag that opened the window

    # --- per-turn scratch ---
    raw: dict
    trace_id: str
    from_me: bool
    remote_jid: str
    msg_id: Optional[str]
    text: str
    push_name: Optional[str]
    number: str
    ts: int
    is_own: bool
    tag: Optional[str]

    decision: str  # gate: "run" | "stop"
    trigger: Optional[str]  # "tag" | "window"

    context_message_ids: list  # WhatsApp ids ingested this run

    # reasoning output + metadata (for the record)
    llm_state: str  # "keep_listening" | "close"
    reply_body: Optional[str]  # the model's message, or None (silence)
    lang: Optional[str]  # language the model wrote in this turn
    provider: Optional[str]
    model: Optional[str]
    provider_request_id: Optional[str]
    usage: Optional[dict]
    latency_ms: Optional[int]
    stop_reason: Optional[str]
    tool_calls: list
    error_category: str

    # act output
    reply: Optional[str]  # framed text actually sent
    sent: bool
    close_reason: Optional[str]  # "model" | "timeout" | None
