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
    loop_id: Optional[str]  # id of the current listening loop (grouping key for the log)
    loop_started_ts: Optional[int]  # unix ts the loop opened (tag on a closed window)
    workflow: Optional[dict]  # persistent gather memory toward a goal; cleared on tag-reset
    seen_event_ids: list  # calendar ids surfaced by find/list this loop; gates update/delete

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
    loop_opened: bool  # this activation opened a NEW loop (tag on a closed window)

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

    # tool loop (execute node)
    actions: list  # calendar actions the model wants run this turn; [] if none
    action_results: list  # ActionResults from this loop's executions (for the record)
    needs_readback: bool  # execute -> reason again (a read happened, or something failed/blocked)
    tool_hops: int  # execute passes taken this loop; bounds the read-back loop

    # act output
    reply: Optional[str]  # framed text actually sent
    sent: bool
    close_reason: Optional[str]  # "model" | "timeout" | None
