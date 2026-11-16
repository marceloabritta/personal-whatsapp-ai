"""The history agent's tools: what the model may call, how each call is checked, run, and shown.

Read-only over the HistoryStore (types.py); two tools reach the media seam (a voice note's
transcript, a file's bytes). Nothing here can write, send or contact anyone.

Every result is rendered as compact text on the owner's clock, one line per message:

    3EB0A1 | Tue 2026-10-21 14:03 | Bruno (5511999990001@s.whatsapp.net) | Bruno: [voice 1:24: transcript pending]

and capped per tool, so the 25 calls a turn may make stay well inside the context. Only
open_file returns a content block (a PDF or an image) the model reads directly.

The schemas are strict (grammar-constrained). Anthropic caps a request at 24 optional parameters
and 16 union-typed ones across ALL strict schemas, the answer's included: these tools spend 19
optionals and no unions; the answer schema (prompt.py) spends one union and no optionals. Every
input is still checked here, since a fallback model or a test client may send anything."""
from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional

from .types import KINDS

MAX_TRANSCRIPTIONS = 8        # voice notes transcribed per turn: each costs money and seconds
MAX_OPEN_FILES = 3            # files opened per turn: each stays in the context for every hop
FETCH_MAX_BYTES = 8_000_000   # what open_file downloads at most
IMAGE_MAX_BYTES = 3_750_000   # base64 of this is the API's 5 MB image ceiling
PDF_MAX_PAGES = 30            # ~2k tokens a page, re-read on every later hop
TEXT_FILE_MAX_CHARS = 15_000

# Characters shown per message, by tool, and per result. The caps are what keep 25 hops small.
SNIPPET_CHARS = 280
READ_CHARS = 700
FULL_CHARS = 4_000
TRANSCRIPT_CHARS = 6_000
RESULT_CHARS = 14_000
READ_RESULT_CHARS = 20_000

FILE_KINDS = ("document", "image", "video", "voice")   # what can be sent back to the owner
_READABLE_IMAGES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_TEXT_TYPES = ("text/plain", "text/csv", "text/markdown", "application/json", "text/html")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_DATE_DOC = "YYYY-MM-DD or YYYY-MM-DDTHH:MM on the owner's clock; inclusive"


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": required}


_MSG_ID = {"msg_id": {"type": "string", "description": "A message id, as tool results show it."}}

TOOLS: list[dict] = [
    {
        "name": "search_messages",
        "strict": True,
        "description": (
            "Find messages by words, across every chat or within one. Matching is case- and "
            "accent-insensitive and a term matches inside words (\"acelera\" finds "
            "\"aceleradora\"; \"eua\" finds \"EUA\"). Searches message text, captions, file names "
            "and voice-note transcripts. A voice note not transcribed yet cannot match: look at the "
            "voice notes around the hits in a candidate chat. Lisa's own messages are left out "
            "unless include_lisa. Returns the total, the best matches, and the chats with the most "
            "matches."),
        "input_schema": _obj({
            "terms": {"type": "array", "items": {"type": "string"},
                      "description": "Words or short phrases (1-12)."},
            "match": {"type": "string", "enum": ["all", "any"],
                      "description": "all: every term in the same message. any: at least one."},
            "regex": {"type": "boolean",
                      "description": "Treat each term as a POSIX regular expression."},
            "chat": {"type": "string", "description": "Only this chat: its key."},
            "from_me": {"type": "string", "enum": ["me", "others"],
                        "description": "Only messages the owner sent (me) or received (others)."},
            "kinds": {"type": "array", "items": {"type": "string", "enum": list(KINDS)},
                      "description": "Only these kinds of message, e.g. [\"document\"]."},
            "after": {"type": "string", "description": f"From this date: {_DATE_DOC}."},
            "before": {"type": "string", "description": f"Up to this date: {_DATE_DOC}."},
            "include_lisa": {"type": "boolean",
                             "description": "Also search what Lisa wrote on the owner's behalf."},
            "limit": {"type": "integer", "description": "Matches to show: default 30, at most 50."},
        }, ["terms", "match"]),
    },
    {
        "name": "topics_together",
        "strict": True,
        "description": (
            "Find chats where several topics all come up within a few days of each other: the way "
            "to intersect clues (a project name AND a place AND an event). Each topic is a list of "
            "alternatives, any one of which counts, matched like search_messages. Returns the chats, "
            "best first (tightest window), with the dates where the topics meet and an example of "
            "each."),
        "input_schema": _obj({
            "topics": {"type": "array",
                       "items": {"type": "array", "items": {"type": "string"}},
                       "description": "2-5 topics, each a list of alternative words, e.g. "
                                      "[[\"eua\", \"estados unidos\", \"usa\"], [\"demo day\"]]."},
            "within_days": {"type": "integer",
                            "description": "How close in time the topics must come up: default "
                                           "14, at most 120."},
            "after": {"type": "string", "description": f"From this date: {_DATE_DOC}."},
            "before": {"type": "string", "description": f"Up to this date: {_DATE_DOC}."},
        }, ["topics"]),
    },
    {
        "name": "list_chats",
        "strict": True,
        "description": (
            "List chats, most recently active first: to find a person's or group's chat key from "
            "a name (fuzzy, accent-insensitive), or to see who was active in a period. Shows each "
            "chat's key, kind, message count and active dates."),
        "input_schema": _obj({
            "name": {"type": "string", "description": "Part of a person's or group's name."},
            "kind": {"type": "string", "enum": ["contact", "group", "self"]},
            "active_after": {"type": "string",
                             "description": f"Active on or after this date: {_DATE_DOC}."},
            "active_before": {"type": "string",
                              "description": f"Active on or before this date: {_DATE_DOC}."},
        }, []),
    },
    {
        "name": "read_chat",
        "strict": True,
        "description": (
            "Read a stretch of one chat in order: around a message (to see what was said before "
            "and after a hit), or between dates. Without around_msg_id, after or before: the "
            "latest messages."),
        "input_schema": _obj({
            "chat": {"type": "string", "description": "The chat's key."},
            "around_msg_id": {"type": "string",
                              "description": "Center the window on this message."},
            "after": {"type": "string", "description": f"From this date: {_DATE_DOC}."},
            "before": {"type": "string", "description": f"Up to this date: {_DATE_DOC}."},
            "limit": {"type": "integer",
                      "description": "Messages to show: default 40, at most 80."},
        }, ["chat"]),
    },
    {
        "name": "get_message",
        "strict": True,
        "description": "One message in full: its whole text or transcript, file name and type, "
                       "and the message it replies to.",
        "input_schema": _obj(dict(_MSG_ID), ["msg_id"]),
    },
    {
        "name": "transcribe_voice_note",
        "strict": True,
        "description": (
            f"Transcribe a voice note whose transcript is pending. Slow (seconds to a minute) and "
            f"paid: at most {MAX_TRANSCRIPTIONS} per turn, so pick the ones that matter (the "
            f"candidate chat, around the dates of the hits). Run several at once."),
        "input_schema": _obj(dict(_MSG_ID), ["msg_id"]),
    },
    {
        "name": "open_file",
        "strict": True,
        "description": (
            f"Open a file sent in a chat so you can read it: a PDF, an image, or a text file. At "
            f"most {MAX_OPEN_FILES} per turn. WhatsApp keeps files downloadable for only about 2-3 "
            f"weeks, so an older one is usually gone. Opening is not sending: to send a file to the "
            f"owner, put its id in the answer's files."),
        "input_schema": _obj(dict(_MSG_ID), ["msg_id"]),
    },
]

TOOL_NAMES = tuple(t["name"] for t in TOOLS)


# --- time -------------------------------------------------------------------------------------

def zone(name: Optional[str]) -> tzinfo:
    """The owner's zone, or UTC when the name is unknown (a wrong clock beats no answer)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or "UTC")
    except Exception:
        return timezone.utc


def fmt_ts(ts: Optional[int], tz: tzinfo) -> str:
    if not ts:
        return "?"
    dt = datetime.fromtimestamp(int(ts), tz)
    return f"{_WEEKDAYS[dt.weekday()]} {dt:%Y-%m-%d %H:%M}"


def fmt_day(ts: Optional[int], tz: tzinfo) -> str:
    if not ts:
        return "?"
    dt = datetime.fromtimestamp(int(ts), tz)
    return f"{_WEEKDAYS[dt.weekday()]} {dt:%Y-%m-%d}"


_WHEN = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2})?)?")


def parse_when(value: Any, tz: tzinfo, *, end: bool, field: str = "date") -> int:
    """An ISO date on the owner's clock as unix seconds. `end` makes it the END of that day (or
    minute), so a range of two dates holds both days whole. The end is the first second AFTER it:
    exclusive for a store that compares with <, and one second over for one that uses <=."""
    s = str(value or "").strip()
    m = _WHEN.fullmatch(s)
    if not m:
        raise ToolError("invalid_input",
                        f'{field}: "{s}" is not a date. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM.')
    y, mo, d, hh, mm = m.groups()
    try:
        if hh is None:
            dt = datetime(int(y), int(mo), int(d), tzinfo=tz)
            if end:
                dt += timedelta(days=1)
        else:
            dt = datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=tz)
            if end:
                dt += timedelta(minutes=1)
    except ValueError as exc:
        raise ToolError("invalid_input", f'{field}: "{s}" is not a real date ({exc}).') from None
    return int(dt.timestamp())


def _range(args: dict, tz: tzinfo, lo_key: str = "after", hi_key: str = "before"):
    lo = parse_when(args[lo_key], tz, end=False, field=lo_key) if args.get(lo_key) else None
    hi = parse_when(args[hi_key], tz, end=True, field=hi_key) if args.get(hi_key) else None
    if lo is not None and hi is not None and lo >= hi:
        raise ToolError("invalid_input", f"{lo_key} must be before {hi_key}.")
    return lo, hi


# --- text -------------------------------------------------------------------------------------

def fold(s: str) -> str:
    """Lowercase, accents off, one character for one character (so positions line up)."""
    out = []
    for ch in s:
        base = next((c for c in unicodedata.normalize("NFKD", ch)
                     if not unicodedata.combining(c)), ch)
        low = base.lower()
        out.append(low[0] if low else base)
    return "".join(out)


def snippet(text: str, terms=(), cap: int = SNIPPET_CHARS, regex: bool = False) -> str:
    """The text on one line, cut to `cap` around the first term found in it."""
    text = " ".join((text or "").split())
    if len(text) <= cap:
        return text
    pos = -1
    folded = fold(text)
    for t in terms or ():
        if not t:
            continue
        if regex:
            try:
                m = re.search(t, text, re.IGNORECASE)
            except re.error:
                m = None
            i = m.start() if m else -1
        else:
            i = folded.find(fold(t))
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    start = max(0, pos - cap // 3) if pos >= 0 else 0
    end = min(len(text), start + cap)
    start = max(0, end - cap)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _cap_text(text: str, cap: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else text[:cap] + f"… [cut at {cap} of {len(text)} characters]"


def _duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    return f" {s // 60}:{s % 60:02d}" if s > 0 else ""


def kind_marker(h: dict) -> str:
    kind = h.get("kind") or "text"
    if kind == "text":
        return ""
    if kind == "voice":
        dur = _duration(h.get("seconds"))
        state = h.get("transcript")
        if state == "done":
            return f"[voice{dur}, transcribed]"
        if state == "expired":
            return f"[voice{dur}: audio expired, cannot be transcribed]"
        if state == "failed":
            return f"[voice{dur}: transcription failed]"
        return f"[voice{dur}: transcript pending]"
    if kind == "document":
        return f'[document "{h.get("file_name") or "unnamed"}"]'
    if kind == "video":
        return f"[video{_duration(h.get('seconds'))}]"
    if kind == "contact":
        return "[contact card]"
    return f"[{kind}]"


@dataclass
class Outcome:
    """One tool call's result: what the model reads, how many things it found (for the log), and
    an error code when it failed (the content then says why, for the model)."""
    content: Any
    n: int = 0
    error: Optional[str] = None


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- the toolbox ------------------------------------------------------------------------------

_CHAT_SUFFIXES = ("@s.whatsapp.net", "@g.us", "@lid")
_PHONE = re.compile(r"\+?[\d\s().-]{8,}")


class ToolBox:
    """The tools of ONE turn: the store and media seam behind them, the owner's clock, and the
    per-turn limits (voice notes transcribed, files opened). `seen` keeps every message shown to
    the model this turn, so the answer's files can be checked against what it actually saw."""

    def __init__(self, store: Any, media: Any, *, tz: tzinfo, owner_name: str,
                 max_transcriptions: int = MAX_TRANSCRIPTIONS,
                 max_open_files: int = MAX_OPEN_FILES) -> None:
        self.store = store
        self.media = media
        self.tz = tz
        self.owner = owner_name
        self.max_transcriptions = max_transcriptions
        self.max_open_files = max_open_files
        self.transcribed = 0
        self.opened = 0
        self.seen: dict[str, dict] = {}
        self.names: dict[str, str] = {}
        self._handlers = {
            "search_messages": self.search_messages,
            "topics_together": self.topics_together,
            "list_chats": self.list_chats,
            "read_chat": self.read_chat,
            "get_message": self.get_message,
            "transcribe_voice_note": self.transcribe_voice_note,
            "open_file": self.open_file,
        }

    async def run(self, name: str, args: Any) -> Outcome:
        handler = self._handlers.get(name)
        if handler is None:
            return Outcome(f"There is no tool named {name!r}. Tools: {', '.join(TOOL_NAMES)}.",
                           0, "unknown_tool")
        if not isinstance(args, dict):
            return Outcome("The input must be an object.", 0, "invalid_input")
        try:
            _no_extra(name, args)
            return await handler(args)
        except ToolError as e:
            return Outcome(e.message, 0, e.code)

    # --- rendering ----------------------------------------------------------------------------

    def chat_label(self, chat: Optional[str], name: Optional[str] = None) -> str:
        if name and chat:
            self.names[chat] = name
        name = name or self.names.get(chat or "")
        return f"{name} ({chat})" if name else (chat or "?")

    def _remember_chat(self, c: Optional[dict]) -> None:
        if c and c.get("chat") and c.get("name"):
            self.names[c["chat"]] = c["name"]

    def sender(self, h: dict) -> str:
        if h.get("by_lisa"):
            return f"Lisa (for {self.owner})"
        if h.get("from_me"):
            return h.get("sender") or self.owner
        return h.get("sender") or "?"

    def line(self, h: dict, *, chat: bool = True, terms=(), regex: bool = False,
             cap: int = SNIPPET_CHARS) -> str:
        """One message on one line: id | when | chat | sender: [kind] text (reply to id)."""
        mid = h.get("msg_id") or "?"
        if mid != "?":
            self.seen[mid] = h
        parts = [mid, fmt_ts(h.get("ts"), self.tz)]
        if chat:
            parts.append(self.chat_label(h.get("chat"), h.get("chat_name")))
        body = " ".join(p for p in (kind_marker(h),
                                    snippet(h.get("text") or "", terms, cap, regex)) if p)
        if h.get("quoted_msg_id"):
            body += f" (reply to {h['quoted_msg_id']})"
        parts.append(f"{self.sender(h)}: {body}" if body else self.sender(h))
        return " | ".join(parts)

    def chat_summary(self, c: dict) -> str:
        self._remember_chat(c)
        bits = [c.get("kind") or "chat"]
        if c.get("n_messages") is not None:
            bits.append(f"{c['n_messages']} messages")
        if c.get("first_ts") or c.get("last_ts"):
            bits.append(f"{fmt_day(c.get('first_ts'), self.tz)} to {fmt_day(c.get('last_ts'), self.tz)}")
        return f"{self.chat_label(c.get('chat'), c.get('name'))} — {', '.join(bits)}"

    # --- argument checks ----------------------------------------------------------------------

    async def chat_key(self, raw: Any) -> str:
        """A chat key as the store wants it. A bare phone number becomes its key; a name is
        refused with the chats it could mean, so the model can retry with the key."""
        s = str(raw or "").strip()
        if not s:
            raise ToolError("invalid_input", "chat is empty: give a chat key.")
        low = s.lower()
        if low.endswith(_CHAT_SUFFIXES) and " " not in low:
            return low
        if _PHONE.fullmatch(s):
            digits = re.sub(r"\D", "", s)
            if 8 <= len(digits) <= 15:
                return f"{digits}@s.whatsapp.net"
        try:
            options = await self.store.list_chats(name=s, limit=5)
        except Exception:
            options = []
        if options:
            listed = "; ".join(self.chat_label(c.get("chat"), c.get("name")) for c in options)
            raise ToolError("unknown_chat",
                            f'"{s}" is not a chat key. Chats whose name matches: {listed}. '
                            f"Use the key in parentheses.")
        raise ToolError("unknown_chat",
                        f'"{s}" is not a chat key and no chat name matches it. Keys look like '
                        f"5511999990000@s.whatsapp.net or 120363000000000000@g.us; find one with "
                        f"list_chats.")

    # --- tools --------------------------------------------------------------------------------

    async def search_messages(self, args: dict) -> Outcome:
        terms = _terms(args.get("terms"), "terms")
        match = _enum(args, "match", ("all", "any"), "all")
        regex = _bool(args, "regex", False)
        if regex:
            for t in terms:
                try:
                    re.compile(t)
                except re.error as exc:
                    raise ToolError("invalid_input", f'"{t}" is not a valid regex: {exc}.') from None
        chat = await self.chat_key(args["chat"]) if args.get("chat") else None
        from_me = {"me": True, "others": False, None: None}.get(args.get("from_me"), "bad")
        if from_me == "bad":
            raise ToolError("invalid_input", 'from_me must be "me" or "others".')
        kinds = _kinds(args.get("kinds"))
        after, before = _range(args, self.tz)
        limit = _int(args, "limit", 30, 1, 50)
        res = await self.store.search(
            terms=terms, match=match, regex=regex, chat=chat, from_me=from_me, kinds=kinds,
            after=after, before=before, include_lisa=_bool(args, "include_lisa", False),
            limit=limit)
        total = int(res.get("total") or 0)
        hits = list(res.get("hits") or [])[:limit]
        by_chat = list(res.get("by_chat") or [])
        for c in by_chat:
            self._remember_chat(c)
        if not hits:
            hint = (" Try fewer terms, match \"any\", word stems, other spellings or the other "
                    "language." if match == "all" or len(terms) == 1 else
                    " Try word stems, other spellings or the other language.")
            return Outcome("No matches." + hint, 0)
        head = f"{total} match{'es' if total != 1 else ''}"
        if total > len(hits):
            head += f", showing the best {len(hits)}"
        if by_chat and not chat:
            top = "; ".join(f"{self.chat_label(c.get('chat'), c.get('name'))} ×{c.get('n_hits', '?')}"
                            for c in by_chat[:10])
            more = f" (+{len(by_chat) - 10} more chats)" if len(by_chat) > 10 else ""
            head += f". By chat: {top}{more}"
        lines = [self.line(h, terms=terms, regex=regex) for h in hits]
        return Outcome(_fit(head + ".", lines), total)

    async def topics_together(self, args: dict) -> Outcome:
        raw = args.get("topics")
        if not isinstance(raw, list) or not raw:
            raise ToolError("invalid_input", "topics must be a list of topics (lists of words).")
        topics = [_terms(t, f"topics[{i}]") for i, t in enumerate(raw)]
        if len(topics) < 2:
            raise ToolError("invalid_input", "Give at least 2 topics; for one, use search_messages.")
        if len(topics) > 5:
            raise ToolError("invalid_input", "Give at most 5 topics.")
        within = _int(args, "within_days", 14, 1, 120)
        after, before = _range(args, self.tz)
        res = list(await self.store.topics_together(topics=topics, within_days=within,
                                                    after=after, before=before, limit=10) or [])
        if not res:
            return Outcome(f"No chat has all {len(topics)} topics within {within} days of each "
                           f"other. Try more alternatives per topic, fewer topics, or a wider "
                           f"within_days.", 0)
        flat = [t for topic in topics for t in topic]
        blocks = [f"{len(res)} chat{'s' if len(res) != 1 else ''} where all {len(topics)} topics "
                  f"come up within {within} days:"]
        for i, tc in enumerate(res, 1):
            c = tc.get("chat") or {}
            self._remember_chat(c)
            span = max(0, int(tc.get("last_ts") or 0) - int(tc.get("first_ts") or 0))
            days = span / 86400
            blocks.append(
                f"{i}. {self.chat_label(c.get('chat'), c.get('name'))} — the topics meet "
                f"{fmt_ts(tc.get('first_ts'), self.tz)} to {fmt_ts(tc.get('last_ts'), self.tz)} "
                f"({days:.1f} days)")
            for h in list(tc.get("examples") or [])[:5]:
                blocks.append("   " + self.line(h, chat=False, terms=flat, cap=200))
        return Outcome(_fit(blocks[0], blocks[1:]), len(res))

    async def list_chats(self, args: dict) -> Outcome:
        name = _str(args, "name", 80)
        kind = args.get("kind")
        if kind is not None and kind not in ("contact", "group", "self"):
            raise ToolError("invalid_input", 'kind must be "contact", "group" or "self".')
        lo, hi = _range(args, self.tz, "active_after", "active_before")
        res = list(await self.store.list_chats(name=name or None, kind=kind, active_after=lo,
                                               active_before=hi, limit=20) or [])
        if not res:
            return Outcome("No chat matches." + (" Try part of the name, or another spelling."
                                                 if name else ""), 0)
        return Outcome(_fit(f"{len(res)} chat{'s' if len(res) != 1 else ''}, most recent first:",
                            [self.chat_summary(c) for c in res]), len(res))

    async def read_chat(self, args: dict) -> Outcome:
        chat = await self.chat_key(args.get("chat"))
        around = _str(args, "around_msg_id", 100)
        after, before = _range(args, self.tz)
        limit = _int(args, "limit", 40, 5, 80)
        res = await self.store.read_chat(chat=chat, around_msg_id=around or None, after=after,
                                         before=before, limit=limit)
        res = res or {}
        c = res.get("chat") or {}
        msgs = list(res.get("messages") or [])
        if not msgs and not c.get("n_messages"):
            if around:
                raise ToolError("not_found",
                                f"Nothing to read: no message {around} in {chat}, or no such chat.")
            raise ToolError("unknown_chat",
                            f"No chat with the key {chat}. Find the key with list_chats.")
        head = self.chat_summary({**c, "chat": c.get("chat") or chat})
        if not msgs and around:
            raise ToolError("not_found", f"No message {around} in {self.chat_label(chat)}.")
        if not msgs:
            return Outcome(head + "\nNo messages in that window.", 0)
        lines = []
        if res.get("more_before"):
            lines.append(f"(earlier messages exist: read with before={_iso(msgs[0].get('ts'), self.tz)})")
        # Long transcripts share the room: each message gets an equal part of it.
        per = max(200, min(READ_CHARS, READ_RESULT_CHARS // len(msgs)))
        lines += [self.line(h, chat=False, cap=per) for h in msgs]
        if res.get("more_after"):
            lines.append(f"(later messages exist: read with after={_iso(msgs[-1].get('ts'), self.tz)})")
        return Outcome(_fit(head, lines, cap=READ_RESULT_CHARS), len(msgs))

    async def get_message(self, args: dict) -> Outcome:
        mid = _required_id(args)
        h = await self.store.get_message(mid)
        if not h:
            raise ToolError("not_found", f"No message with id {mid}.")
        lines = [self.line(h, cap=FULL_CHARS)]
        if h.get("kind") in ("document", "image", "video", "sticker"):
            lines.append(f"file: {h.get('file_name') or 'unnamed'} ({h.get('mimetype') or '?'})")
        return Outcome("\n".join(lines), 1)

    async def transcribe_voice_note(self, args: dict) -> Outcome:
        mid = _required_id(args)
        h = await self.store.get_message(mid)
        if not h:
            raise ToolError("not_found", f"No message with id {mid}.")
        self.seen[mid] = h
        if h.get("kind") != "voice":
            raise ToolError("not_voice", f"{mid} is not a voice note (it is {h.get('kind')}).")
        head = self.line({**h, "text": ""}, cap=0)
        if h.get("transcript") == "done" and (h.get("text") or "").strip():
            return Outcome(f"{head}\nAlready transcribed:\n{_cap_text(h['text'], TRANSCRIPT_CHARS)}", 1)
        if h.get("transcript") == "expired":
            raise ToolError("expired", f"{mid}: WhatsApp no longer serves this audio, so it "
                                       f"cannot be transcribed.")
        if self.transcribed >= self.max_transcriptions:
            raise ToolError("limit", f"The limit of {self.max_transcriptions} voice notes "
                                     f"transcribed per turn is reached. Tell the owner which ones "
                                     f"are left; they can be transcribed in the next turn.")
        self.transcribed += 1
        res = await self.media.transcribe(mid) or {}
        text = (res.get("text") or "").strip()
        if res.get("ok") and text:
            return Outcome(f"{head}\nTranscript:\n{_cap_text(text, TRANSCRIPT_CHARS)}", 1)
        err = res.get("error") or "failed"
        why = {"expired": "WhatsApp no longer serves this audio (it is older than ~2-3 weeks)",
               "empty": "the recording has no speech in it"}.get(err, "the transcription failed")
        raise ToolError("expired" if err == "expired" else ("empty" if err == "empty" else "failed"),
                        f"{mid}: {why}.")

    async def open_file(self, args: dict) -> Outcome:
        mid = _required_id(args)
        h = await self.store.get_message(mid)
        if not h:
            raise ToolError("not_found", f"No message with id {mid}.")
        self.seen[mid] = h
        kind = h.get("kind")
        if kind == "voice":
            raise ToolError("not_file", f"{mid} is a voice note: use transcribe_voice_note.")
        if kind not in ("document", "image", "sticker"):
            what = "a video, which cannot be read here (it can still be sent)" \
                if kind == "video" else f"a {kind} message, not a file"
            raise ToolError("not_file", f"{mid} is {what}.")
        if self.opened >= self.max_open_files:
            raise ToolError("limit", f"The limit of {self.max_open_files} files opened per turn "
                                     f"is reached.")
        self.opened += 1
        res = await self.media.fetch(mid, max_bytes=FETCH_MAX_BYTES) or {}
        if not res.get("ok") or not res.get("base64"):
            err = res.get("error") or "failed"
            why = {"expired": "WhatsApp no longer serves this file (older than ~2-3 weeks); it "
                              "can be neither opened nor sent",
                   "too_large": f"it is larger than {FETCH_MAX_BYTES // 1_000_000} MB, too big to "
                                f"read here (it may still be sendable)",
                   "not_media": "it holds no file"}.get(err, "the download failed")
            raise ToolError(err if err in ("expired", "too_large") else "failed", f"{mid}: {why}.")
        b64 = res["base64"]
        mimetype = (res.get("mimetype") or h.get("mimetype") or "").split(";")[0].strip().lower()
        name = res.get("file_name") or h.get("file_name") or "unnamed"
        size = len(b64) * 3 // 4
        pages = pdf_pages(_b64decode(b64)) if mimetype == "application/pdf" else None
        extent = f", {pages} page{'s' if pages != 1 else ''}" if pages else ""
        head = (f"{self.line({**h, 'text': ''}, cap=0)}\n"
                f"file: {name} ({mimetype or '?'}, {max(1, size // 1024)} KB{extent})")
        caption = (h.get("text") or "").strip()
        if caption:
            head += f"\ncaption: {_cap_text(caption, 500)}"
        if mimetype == "application/pdf":
            if pages and pages > PDF_MAX_PAGES:
                raise ToolError("too_large", f"{mid}: the PDF has {pages} pages, too long to read "
                                             f"here (at most {PDF_MAX_PAGES}). It can still be sent.")
            block = {"type": "document", "title": name[:200],
                     "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
            return Outcome([{"type": "text", "text": head}, block], 1)
        if mimetype in _READABLE_IMAGES:
            if size > IMAGE_MAX_BYTES:
                raise ToolError("too_large", f"{mid}: the image is {size // 1_000_000} MB, too "
                                             f"large to look at here. It can still be sent.")
            return Outcome([{"type": "text", "text": head},
                            {"type": "image",
                             "source": {"type": "base64", "media_type": mimetype, "data": b64}}], 1)
        if mimetype in _TEXT_TYPES:
            text = _b64decode(b64).decode("utf-8", errors="replace")
            return Outcome(f"{head}\ncontent:\n{_cap_text(text, TEXT_FILE_MAX_CHARS)}", 1)
        raise ToolError("unsupported", f"{mid}: {name} is {mimetype or 'of an unknown type'}, which "
                                       f"cannot be read here. It can still be sent to the owner.")


# --- helpers ----------------------------------------------------------------------------------

_ALLOWED = {t["name"]: set(t["input_schema"]["properties"]) for t in TOOLS}


def _no_extra(name: str, args: dict) -> None:
    extra = sorted(set(args) - _ALLOWED.get(name, set()))
    if extra:
        raise ToolError("invalid_input", f"Unknown parameter(s) for {name}: {', '.join(extra)}.")


def _str(args: dict, key: str, maxlen: int) -> str:
    v = args.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ToolError("invalid_input", f"{key} must be a string.")
    v = v.strip()
    if len(v) > maxlen:
        raise ToolError("invalid_input", f"{key} is too long (at most {maxlen} characters).")
    return v


def _required_id(args: dict) -> str:
    mid = _str(args, "msg_id", 100)
    if not mid:
        raise ToolError("invalid_input", "msg_id is required.")
    return mid


def _bool(args: dict, key: str, default: bool) -> bool:
    v = args.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ToolError("invalid_input", f"{key} must be true or false.")
    return v


def _int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = args.get(key)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ToolError("invalid_input", f"{key} must be a number.")
    return max(lo, min(hi, int(v)))


def _enum(args: dict, key: str, allowed: tuple, default: str) -> str:
    v = args.get(key, default)
    if v is None:
        return default
    if v not in allowed:
        raise ToolError("invalid_input", f"{key} must be one of: {', '.join(allowed)}.")
    return v


def _terms(raw: Any, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise ToolError("invalid_input", f"{field} must be a list of words.")
    out = []
    for t in raw:
        if not isinstance(t, str):
            raise ToolError("invalid_input", f"{field} must hold only strings.")
        t = " ".join(t.split())
        if not t:
            continue
        if len(t) > 100:
            raise ToolError("invalid_input", f"{field}: a term is too long (at most 100 characters).")
        if t not in out:
            out.append(t)
    if not out:
        raise ToolError("invalid_input", f"{field} has no words.")
    if len(out) > 12:
        raise ToolError("invalid_input", f"{field}: at most 12 terms.")
    return out


def _kinds(raw: Any) -> Optional[list[str]]:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ToolError("invalid_input", "kinds must be a list.")
    bad = [k for k in raw if k not in KINDS]
    if bad:
        raise ToolError("invalid_input", f"Unknown kind(s): {bad}. Kinds: {', '.join(KINDS)}.")
    return list(dict.fromkeys(raw)) or None


def _iso(ts: Any, tz: tzinfo) -> str:
    return datetime.fromtimestamp(int(ts or 0), tz).strftime("%Y-%m-%dT%H:%M")


def _fit(head: str, lines: list[str], *, cap: int = RESULT_CHARS) -> str:
    """Head and lines within `cap` characters, saying how many lines were left out."""
    out = head
    used = len(head)
    kept = []
    for ln in lines:
        if used + len(ln) + 1 > cap:
            break
        kept.append(ln)
        used += len(ln) + 1
    if len(kept) < len(lines):
        kept.append(f"(… {len(lines) - len(kept)} more lines cut to keep this short; narrow the "
                    f"query or read a smaller window)")
    return "\n".join([out, *kept]) if kept else out


def _b64decode(b64: str) -> bytes:
    try:
        return base64.b64decode(b64, validate=False)
    except (ValueError, TypeError):
        return b""


_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def pdf_pages(raw: bytes) -> Optional[int]:
    """Pages in a PDF, by counting its page objects. None when they are hidden in compressed
    object streams (the byte cap bounds those)."""
    n = len(_PDF_PAGE.findall(raw or b""))
    return n or None


def safe_args(args: Any) -> dict:
    """A tool call's input for the log: the model's own query words, with long strings and lists
    cut. Never a result."""
    if not isinstance(args, dict):
        return {}

    def cut(v: Any, depth: int = 0) -> Any:
        if isinstance(v, str):
            return v if len(v) <= 120 else v[:120] + "…"
        if isinstance(v, list):
            return [cut(x, depth + 1) for x in v[:12]] if depth < 3 else []
        if isinstance(v, (int, float, bool)) or v is None:
            return v
        return str(v)[:120]

    return {k: cut(v) for k, v in list(args.items())[:12]}
