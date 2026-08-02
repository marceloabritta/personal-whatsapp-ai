"""The graph's shared state.

`messages` is persisted per chat by the checkpointer (add_messages reducer);
everything else is per-turn scratch (or the persisted cursor/lang/workflow)."""
from __future__ import annotations

from typing import Annotated, Optional, TypedDict

from langgraph.graph.message import add_messages


class MessageState(TypedDict, total=False):
    # --- persisted memory (checkpointer) ---
    messages: Annotated[list, add_messages]  # model conversation + tool observations
    last_whatsapp_message_id: Optional[str]  # ingestion cursor
    initialized: bool
    session_lang: Optional[str]  # locked at the tag that opened the window
    workflow: Optional[dict]  # in-flight goal + known_inputs + open_questions (or null)

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
    context_message_ids: list

    # reasoning output
    lang: Optional[str]
    next_message: Optional[str]
    loop_state: str  # "keep_listening" | "close_loop"
    actions: list  # [{task, inputs}, ...] this turn
    provider: Optional[str]
    model: Optional[str]
    provider_request_id: Optional[str]
    usage: Optional[dict]
    latency_ms: Optional[int]
    stop_reason: Optional[str]
    tool_calls: list
    error_category: str

    # execute output
    action_results: list  # compact per-action results this run
    action_count: int  # actions run this activation (cap)
    _readback: bool  # route hint after execute

    # act output
    reply: Optional[str]
    sent: bool
