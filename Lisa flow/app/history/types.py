"""The contracts. Every history module codes against these, so they can be built apart.

Identity. A WhatsApp 1:1 chat is stored under two ids: an opaque `<n>@lid` and the phone jid
`<phone>@s.whatsapp.net` (Evolution reports the twin as key.remoteJidAlt). The index merges them:
`chat` below is ONE canonical key per conversation — the phone jid when it is known, else the @lid;
groups are `<id>@g.us`. Every tool takes and returns canonical keys only.

Times are unix seconds (int). Message ids are WhatsApp's key.id."""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict

# What a message is. "voice" is a voice note (audioMessage); "lisa" marks a message Lisa sent
# (framed with "[<owner>'s AI Assistant]:"), which searches leave out unless asked for.
KINDS = ("text", "voice", "image", "video", "document", "sticker", "contact", "location",
         "poll", "reaction", "other")


class Hit(TypedDict, total=False):
    msg_id: str
    chat: str                    # canonical chat key
    chat_name: Optional[str]
    ts: int
    from_me: bool
    by_lisa: bool                # sent by Lisa on the owner's behalf
    sender: Optional[str]        # display name of who wrote it; the owner's name when from_me
    kind: str                    # one of KINDS
    text: str                    # the words: text, caption, or the voice note's transcript
    file_name: Optional[str]
    mimetype: Optional[str]
    seconds: Optional[int]       # voice/video length
    # Voice notes only: "done" (text holds the transcript), "pending" (not tried yet),
    # "expired" (WhatsApp no longer serves the audio), "failed".
    transcript: Optional[str]
    quoted_msg_id: Optional[str]  # the message this one replies to, if any


class ChatSummary(TypedDict, total=False):
    chat: str
    name: Optional[str]          # best name we have: group subject, contact card, WhatsApp name
    kind: str                    # "contact" | "group" | "self"
    phone: Optional[str]         # digits only, when known
    n_messages: int
    first_ts: int
    last_ts: int
    n_hits: int                  # only in search results: hits in this chat


class SearchResult(TypedDict):
    total: int                   # every match, not just the ones returned
    hits: list                   # [Hit], best first
    by_chat: list                # [ChatSummary] with n_hits, most hits first


class ReadResult(TypedDict):
    chat: ChatSummary
    messages: list               # [Hit], oldest first
    more_before: bool
    more_after: bool


class TopicChat(TypedDict):
    chat: ChatSummary
    first_ts: int                # where the topics meet: the tightest window holding all of them
    last_ts: int
    examples: list               # [Hit], at least one per topic


class HistoryStore(Protocol):
    """Read-only queries over the index. Implemented by store.PgHistoryStore; tests use a fake.

    Matching rules for `terms`: case- and accent-insensitive substring ("eua" finds "EUA",
    "acelerad" finds "aceleradora"), over the message text, caption, file name and voice-note
    transcript. `regex=True` treats each term as a POSIX regex instead. Never raises on a bad
    query: returns an empty result and logs."""

    async def search(self, *, terms: list[str], match: str = "all", regex: bool = False,
                     chat: Optional[str] = None, from_me: Optional[bool] = None,
                     kinds: Optional[list[str]] = None, after: Optional[int] = None,
                     before: Optional[int] = None, include_lisa: bool = False,
                     limit: int = 30) -> SearchResult:
        """match="all": every term in the same message; "any": at least one."""
        ...

    async def topics_together(self, *, topics: list[list[str]], within_days: int = 14,
                              after: Optional[int] = None, before: Optional[int] = None,
                              limit: int = 15) -> list[TopicChat]:
        """Chats where every topic comes up within `within_days` of the others. A topic is a list
        of alternatives (["eua", "estados unidos", "us", "usa"]), any one of which counts. Best
        first: tightest window, then most matches."""
        ...

    async def list_chats(self, *, name: Optional[str] = None, kind: Optional[str] = None,
                         active_after: Optional[int] = None, active_before: Optional[int] = None,
                         limit: int = 20) -> list[ChatSummary]:
        """Fuzzy, accent-insensitive name match when `name` is given; most recent first."""
        ...

    async def read_chat(self, *, chat: str, around_msg_id: Optional[str] = None,
                        after: Optional[int] = None, before: Optional[int] = None,
                        limit: int = 40) -> ReadResult:
        """A window of one chat in order. With around_msg_id: about limit/2 on each side."""
        ...

    async def get_message(self, msg_id: str) -> Optional[Hit]:
        ...


class FileResult(TypedDict, total=False):
    ok: bool
    error: Optional[str]         # "expired" | "too_large" | "not_media" | "failed"
    mimetype: Optional[str]
    file_name: Optional[str]
    base64: Optional[str]


class HistoryMedia(Protocol):
    """The on-demand media seam: Evolution re-downloads a file from WhatsApp by message id, which
    works for roughly 2-4 weeks after it was sent. Implemented by media.EvolutionHistoryMedia."""

    async def transcribe(self, msg_id: str) -> dict:
        """{"ok", "text", "error"} — cached in lisa_log.transcripts like every other transcript."""
        ...

    async def fetch(self, msg_id: str, *, max_bytes: int = 15_000_000) -> FileResult:
        ...

    async def send_to_owner(self, msg_id: str, *, caption: str = "") -> FileResult:
        """Re-send a file into the owner's chat with himself. The ONLY destination there is."""
        ...


class AgentResult(TypedDict, total=False):
    state: str                   # "keep_listening" | "awaiting_answer" | "close"
    message: Optional[str]
    lang: Optional[str]
    reasoning: Optional[str]
    files: list                  # [msg_id] to send back to the owner after the message
    notes: dict                  # carried to the next turn (state["history_notes"])
    usage: dict                  # {"input","output","cache_read","cache_write"}
    tool_calls: list             # [{"tool","args","n","ms"}] — what ran, never the results
    stop_reason: Optional[str]   # "end_turn" | "budget" | "error" | ...
    error_category: str          # "none" | "provider" | "budget"
    model: Optional[str]
    provider_request_id: Optional[str]
