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
    seen_events: dict  # {event_id: view} surfaced by find/list this loop; feeds programmatic messages
    pending_action: Optional[dict]  # a write awaiting the owner's yes; run by resolve_pending on a clean confirmation

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

    # Voice-note transcription. `quoted_audio_id` is the audio this message replies to (fed to
    # the fast-path transcribe node, or injected into context on the slow lane).
    # `transcribe_only` is the matcher verdict — gate routes it to the fast lane.
    quoted_audio_id: Optional[str]
    transcribe_only: bool

    decision: str  # gate: "run" | "stop"
    trigger: Optional[str]  # "tag" | "window"
    loop_opened: bool  # this activation opened a NEW loop (tag on a closed window)

    context_message_ids: list  # WhatsApp ids ingested this run

    # routing (route node) — which skill serves this turn; set programmatically
    domain: Optional[str]  # "calendar" | "web" | ...

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

    # tool loop (confirm -> execute -> respond nodes)
    actions: list  # actions the model wants run this turn; [] if none
    action_results: list  # ActionResults from this loop's executions (for the record)
    tool_hops: int  # execute passes taken this loop; bounds the read-back loop
    last_ran: int  # actions executed in the latest execute pass (respond reads this)
    last_results: list  # results of the latest execute pass (for a Programmatic render)
    confirm_route: str  # confirm node's routing verdict: "execute" | "reason" | "act"
    respond_route: str  # respond node's routing verdict: "reason" | "confirm" | "act"
    resolve_route: str  # resolve_pending node's verdict: "execute" (clean yes) | "route"

    # act output
    reply: Optional[str]  # framed text actually sent
    sent: bool
    close_reason: Optional[str]  # "model" | "timeout" | None
