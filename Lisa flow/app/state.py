"""The graph's shared state.

`messages` is persisted per chat by the checkpointer (add_messages reducer);
everything else is per-turn scratch that the nodes overwrite each run."""
from __future__ import annotations

from typing import Annotated, Optional, TypedDict

from langgraph.graph.message import add_messages


# The approval stamp. Written ONLY by resolve_pending, on the owner's own message, and present in
# no schema the model sees — which is the whole point: a model-writable approval flag is advisory,
# and under a repeated tool failure the model once set one itself and ran an unapproved calendar
# write. The calendar schema no longer carries `confirmed` at all (tools/schemas.py); the setup
# schema still does, harmlessly, since nothing reads it. Lives here rather than in either layer so
# nodes and skills can both read it without one importing the other.
APPROVED_BY = "_approved_by"
OWNER_YES = "owner_yes"


class MessageState(TypedDict, total=False):
    # --- persisted memory (checkpointer) ---
    messages: Annotated[list, add_messages]  # model conversation history
    last_whatsapp_message_id: Optional[str]  # ingestion cursor
    initialized: bool  # seeded the window yet?
    session_lang: Optional[str]  # language locked at the tag that opened the window
    loop_id: Optional[str]  # id of the current listening loop (grouping key for the log)
    loop_started_ts: Optional[int]  # unix ts the loop opened (tag on a closed window)
    workflow: Optional[dict]  # persistent gather memory toward a goal; cleared on tag-reset
    loop_domain: Optional[str]  # the domain this loop is operating in; a continuation sticks to it
    seen_event_ids: list  # calendar ids surfaced by find/list this loop; gates update/delete
    seen_chat_keys: list  # chat keys surfaced by a card/resolve/list this loop; gates setup writes
    seen_chats: dict  # {chat_key: view} surfaced this loop; supplies jid/label/kind to a write
    listed_chats: dict  # {"1": chat_key, ...} from the last setup.list; makes "edit 5" resolvable
    seen_events: dict  # {event_id: view} surfaced by find/list this loop; feeds programmatic messages
    pending_action: Optional[dict]  # a write awaiting the owner's yes; run by resolve_pending on a clean confirmation
    last_confirm_sig: Optional[str]  # fingerprint of the confirmation already sent this loop; blocks an identical re-ask
    # Contact memory. `seen_contacts` is MERGED across the loop (like seen_events) so the
    # confirmation composer can still name a person the first pass surfaced. `pending_side_effects`
    # rides with a proposal and is dispatched only on the owner's yes — a corrected proposal must
    # never leave a rejected address behind in the real address book. Both are checkpointed, so
    # both are cleared on tag-reset.
    seen_contacts: dict  # {email: {name, resource_name, n_emails}} surfaced this loop
    side_effects: list  # side-effect actions stripped this turn (feeds the guest-name renderer)
    pending_side_effects: list  # side effects waiting on the same yes as pending_action

    # --- per-turn scratch ---
    raw: dict
    trace_id: str
    from_me: bool
    remote_jid: str
    msg_id: Optional[str]
    text: str
    push_name: Optional[str]
    number: str
    # The chat's phone number, or None. NOT `number` — that is a raw JID local part, which is the
    # group id in a group and an opaque @lid under LID addressing, and whose tail looks enough like
    # a real number to bind a stranger confidently. Resolved once, in parse.
    phone: Optional[str]
    ts: int
    is_own: bool
    tag: Optional[str]

    # Voice-note transcription. `quoted_audio_id` is the audio this message replies to (fed to
    # the fast-path transcribe node, or injected into context on the slow lane).
    # `transcribe_only` is the matcher verdict — gate routes it to the fast lane.
    quoted_audio_id: Optional[str]
    transcribe_only: bool

    # This message's OWN audio (the automatic path). `auto_rule` is the roster rule the gate
    # matched; `auto_then_run` means a listening window was also open, so the turn continues
    # into the normal conversation after the transcript goes out.
    is_audio: bool
    audio_seconds: Optional[float]
    auto_rule: Optional[dict]
    auto_then_run: bool

    # Chat identity, normalised once in `parse`. `alt_key` is the @lid/phone twin Evolution
    # reports as remoteJidAlt — a 1:1 persists inbound under one and outbound under the other.
    chat_key: str
    alt_key: Optional[str]
    chat_kind: str  # "contact" | "group"
    is_self_chat: bool  # the owner's chat with himself — the only place setup runs
    contact_cards: list  # [{name, number}] forwarded this message; the ONLY way a contact enrolls

    decision: str  # gate: "run" | "stop"
    trigger: Optional[str]  # "tag" | "window"
    loop_opened: bool  # this activation opened a NEW loop (tag on a closed window)

    context_message_ids: list  # WhatsApp ids ingested this run
    turn_text: str  # the labeled transcript this turn — what the address-book scan reads

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
