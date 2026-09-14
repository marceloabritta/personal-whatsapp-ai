"""Google Calendar tool — the owner's own calendar over the Calendar API v3.

Auth: an OAuth2 refresh-token client on the owner's account (google.oauth2.credentials) —
no service account, no key file. The googleapis client is synchronous, so every call runs in
a thread via asyncio.to_thread; imports of the google libs are lazy so this module loads
without them present (tests inject a fake service).

Times: written as {dateTime, timeZone} in settings.calendar_timezone. timeMin/timeMax are
always sent tz-aware (RFC 3339 with offset) — a tz-naive bound is a Google 400. The model
resolves relative dates itself; Python does no natural-language date parsing.

`run` never raises into the graph: every failure comes back as an ActionResult the reasoner
reads back and reports honestly."""
from __future__ import annotations

import asyncio
import difflib
import logging
import socket
import threading
import uuid
from datetime import date, datetime, timedelta

from .base import ActionResult

log = logging.getLogger("mary.tools.calendar")

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Transport failures where the request never reached Google, so replaying it is safe. A broken
# pipe is the one that mattered: the client used to cache its httplib2 connection for the life of
# the container, Google closed the idle socket, and the next write died on it — 10 of 26 creates.
# Deliberately NOT here: any 4xx (Google answered), which a replay would only repeat.
_TRANSIENT_EXC = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                  socket.timeout, TimeoutError)
_TRANSIENT_STATUS = {500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.25  # 0.25s, then 0.5s

# Fuzzy-fallback relevance floor: how close a windowed candidate must be to the query before
# `find` will surface it, so "no match" returns empty instead of the whole calendar.
_MATCH_THRESHOLD = 0.6

# --- one presence rule, one change-set -----------------------------------------------------
#
# Optional fields are omitted from the schema's `required` rather than wrapped as anyOf:[T,null]
# (the union cap), so the handler reads them with .get(). That made `false`, `""` and `[]` look
# exactly like "not sent" to any truthiness test — and they are the opposite: they are the
# instructions "turn this off", "clear this", "remove them all".
#
# The damage came from having TWO implementations of "what is changing" — the body builder here
# and the confirmation composer in skills/calendar_format.py — each with its own presence rule.
# They disagreed in both directions: `virtual: false` built an EMPTY patch that silently did
# nothing, while `attendees: []` built a patch that silently removed every guest from an event
# after a confirmation that mentioned no change at all.
#
# So presence is decided in exactly one place, and the change-set is computed once and consumed
# by both layers.

CHANGE_FIELDS = ("title", "start", "end", "all_day", "virtual", "location", "attendees")


def provided(inp: dict, key: str) -> bool:
    """Did the model actually send this field? false / "" / [] / 0 all count as SENT."""
    return isinstance(inp, dict) and key in inp and inp[key] is not None


# --- one kind, decided once ----------------------------------------------------------------
#
# Google models the two kinds with different wire fields: a timed event carries
# {dateTime, timeZone}; an all-day event carries {date: "YYYY-MM-DD"} — and its end.date is
# EXCLUSIVE, the day AFTER the last day. Both facts stay sealed in this module. Above the
# handler an event is `all_day` plus an INCLUSIVE last day, which is what a person means when
# they say "I'm away the 14th to the 16th".


def is_date_only(iso: str | None) -> bool:
    """Is this the all-day SPELLING? A bare YYYY-MM-DD, no time part."""
    return bool(iso) and "T" not in iso


def resolve_kind(inp: dict, prev: dict | None = None) -> bool:
    """Is this write all-day? THE decision, made in exactly one place.

    The explicit flag wins when the model sent it — including `all_day: false`, which is the
    instruction "give this a time", not an absence (see `provided`). With no flag the spelling
    of the new start decides; with neither, the event being patched keeps its kind. Callers must
    COERCE the values to this answer rather than re-deriving it, so the flag and the value can
    never disagree — two implementations of one fact is the bug this module already paid for."""
    if provided(inp, "all_day"):
        return bool(inp["all_day"])
    ref = inp.get("start") or inp.get("end")
    if ref:
        return is_date_only(ref)
    return bool((prev or {}).get("all_day"))


def as_day(iso: str) -> str:
    """Coerce any ISO value to its date part. This is what makes the flag authoritative:
    `all_day: true` carrying a full datetime becomes the right whole day, not a midnight
    meeting."""
    return iso[:10]


def _shift(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def to_wire_end(last_day: str) -> str:
    """Inclusive last day -> Google's exclusive end.date. The ONLY place +1 appears."""
    return _shift(last_day, 1)


def from_wire_end(end_date: str) -> str:
    """Google's exclusive end.date -> the inclusive last day. The ONLY place -1 appears."""
    return _shift(end_date, -1)


def span_days(first_day: str, last_day: str) -> int:
    """How many days an all-day event covers, inclusive. 1 for a single day."""
    return (date.fromisoformat(last_day) - date.fromisoformat(first_day)).days + 1


def _same_dt(a: str | None, b: str | None) -> bool:
    """Compare two ISO instants by value, not by spelling — Google echoes its own offset format,
    so a string compare reports a time change that isn't one."""
    if a == b:
        return True
    if not a or not b:
        return False
    # A naive date and a naive midnight datetime compare EQUAL, which would let a
    # timed<->all-day conversion vanish from the change-set and report "nothing to change".
    if is_date_only(a) != is_date_only(b):
        return False
    try:
        return datetime.fromisoformat(a) == datetime.fromisoformat(b)
    except ValueError:
        return False


def changes(action: dict, before: dict | None = None) -> list[dict]:
    """Every field this action actually changes: [{field, kind, new, old}].

    `kind` is "set" for a new value, "clear" for removing one ("", [], virtual:false). When
    `before` (a cached event view) is given, fields whose new value equals the current one are
    dropped, so a confirmation never lists a change that isn't one. This is THE definition of
    the change-set — the body builder and the confirmation composer both read it."""
    before = before or {}
    out: list[dict] = []
    for f in CHANGE_FIELDS:
        if not provided(action, f):
            continue
        new = action[f]
        old = before.get(f)
        if f == "virtual":
            # There is no "virtual" on a stored event; a Meet link is the observable form.
            has_meet = bool(before.get("meet_link")) if "meet_link" in before else None
            if has_meet is not None and bool(new) == has_meet:
                continue
            out.append({"field": f, "kind": "set" if new else "clear", "new": bool(new), "old": has_meet})
            continue
        if f == "all_day":
            # A conversion is a change the owner must see named. Compare against the cached
            # view's own kind; equal means nothing is converting.
            if old is not None and bool(new) == bool(old):
                continue
            out.append({"field": f, "kind": "set" if new else "clear",
                        "new": bool(new), "old": old})
            continue
        if f in ("start", "end"):
            if _same_dt(new, old):
                continue
        elif f == "attendees":
            if set(new or []) == set(old or []):
                continue
        elif new == old:
            continue
        out.append({"field": f, "kind": "clear" if new in ("", []) else "set",
                    "new": new, "old": old})
    return out

# Fixed English labels for the agenda layout, so a listing reads the same regardless of the
# container's locale (strftime("%A"/"%b") would follow the server locale).
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DESCRIBE = (
    "Create, find, reschedule or cancel events on {owner_name}'s Google Calendar."
)

# Per-task guidance, appended to the system prompt by the calendar skill (skills/calendar.py).
# Templated with {owner_name}. Co-located with the handler so prompt and behaviour never drift.
GUIDANCE = """Calendar — you manage {owner_name}'s Google Calendar: create an event, list the agenda, find an event, reschedule it (update), or cancel it (delete).

HOW YOU WORK HERE — you do NOT write the confirmation, the result, or the agenda. You emit the structured action; the SYSTEM composes and sends every calendar message. So on a calendar turn set "message" to null, UNLESS you genuinely need to ask a clarifying question or explain something the tools cannot do (e.g. a recurring event). Never write a "Scheduled:"/"Confirming:" text yourself.

ALWAYS
- The current date is given to you. Resolve every relative time yourself into a full ISO 8601 datetime WITH the offset ("next Friday 3pm" -> 2026-08-07T15:00:00-03:00) — never pass a vague phrase.
- To create, change or cancel anything, just emit the action and leave "message" null. The system shows {owner_name} a confirmation and runs it for you when he agrees — you never mark anything approved, and you never re-send an action you already sent. Only `list` and `find` run without asking.
- Two kinds of event. Set "all_day": true for whole-day things — birthdays, holidays, travel and trips, time off, a deadline with no hour, "block Thursday". Leave it out for anything with an hour.
- On an all-day event write "start" as a bare date, "2026-09-14". On a timed one write a full ISO datetime with offset, "2026-09-14T15:00:00-03:00".
- "end" on an all-day event is the LAST DAY, INCLUSIVE. Away the 14th through the 16th is start "2026-09-14", end "2026-09-16". A single day needs no "end" at all. Never add a day to the end yourself — the system handles how Google stores it.
- To convert an event, send "all_day" with the new value and nothing else. The system rewrites both ends of the event for you.
- "attendees" is ALWAYS THE COMPLETE FINAL GUEST LIST, never just the people being added. Google replaces the whole list, so anyone you leave out is uninvited and emailed a cancellation. To add Carla to an event that already has Ana and Rafael, send all three. To remove someone, send everyone except them. If you do not know the current list, `find` the event first and read it.
- Guests work the same on an all-day event as on a timed one, and they are invited by default. "send_invites": false adds or removes them without sending any email — use it on create, update AND delete when {owner_name} asks you to do something quietly. An all-day event has no timezone, so every guest sees it on the same calendar day wherever they are.
- You still cannot make recurring/repeating events. If asked for one, say so briefly in "message" and offer a single event instead.

CREATE — you only need a title and a start; do not interrogate {owner_name} for details he did not give.
- Title is what the event is ABOUT — a short topic ("Budget review"). If you can't resolve the topic, use the format Name & Name for the people, starting with {owner_name}.
- No end -> defaults to 45 minutes. Use "virtual": true for a video call (a Meet link is created and the location dropped); otherwise set "location". Add "attendees" emails when he names people (invited by default; "send_invites": false to suppress).
- Vague about the hour? Assume a sensible default (morning ~09:00, lunch ~12:00, afternoon ~14:00, evening ~19:00) — the confirmation shows it so he can fix it.
- Emit: {{"task": "calendar.create", "title": ..., "start": ...}} with "message": null.

LIST — read-only, no confirmation. Resolve the window from his question (default: what is coming up). Emit `calendar.list` with time_min/time_max; the system formats and sends the agenda.

FIND — the resolver. Search by title words ("query"), the person ("attendee"), and/or a time window. Emit `calendar.find`.
- To answer "when is X", just emit the find; the system replies.
- To CANCEL an event, emit the find AND set "workflow" to {{"task": "calendar.delete"}} — when the search hits a single event the system asks to cancel it directly. If several match, the system lists them and you pick one next turn.

UPDATE (reschedule / edit) — `find` first to resolve the event; once you see the match, emit `calendar.update` with its "event_id" and ONLY the fields that change (any of: title, start, end, all_day, location, virtual, attendees) and "message": null — but remember "attendees", when you send it at all, is the complete final list. A new start keeps the original length unless you also give an end. (The system shows {owner_name} exactly what changes and asks before applying — you don't write that.)

DELETE (cancel) — `find` first to resolve the event, then emit `calendar.delete` with the "event_id" and "message": null.

ADDRESS BOOK — you keep {owner_name}'s contacts. The people this conversation mentions are listed above under "Address book", with exactly what is on file for each.
- NEVER invent an email address. If it is not listed above and not written in this chat, ask for it.
- ONE address on file -> use it, silently. Do not ask; the confirmation card names the person and the address, and that is {owner_name}'s chance to correct it.
- TWO OR MORE on file -> do NOT choose. Put the question in "message", listing them numbered, emit no action that turn, and use his answer next turn.
- A line marked "identity not confirmed" means the person was matched by name alone. Confirm it is the right person before you use their address.
- NONE on file and an address appears in this chat -> use it for the invite AND emit calendar.remember for it, in the same turn.
- He gives an address that differs from the one on file -> his wins, always. Use it for the invite; do NOT emit calendar.remember for it (he is correcting you, not adding a second address).
- calendar.remember is silent bookkeeping. It needs no confirmation, produces no reply, and you never mention it — do not tell him you saved anything, and never read the address book out loud.
- Emit: {{"task": "calendar.remember", "name": "Ana Silva", "email": "ana@acme.com"}}"""


class GoogleCalendarService:
    """Local tool handler for the `calendar` domain. Instantiated once in deps with settings."""

    def __init__(self, settings) -> None:
        self.s = settings
        # Attached in deps, the way the setup handler gets its roster. None = contacts off.
        self.directory = None
        self._svc = None  # test seam ONLY: a fake service injected by the selftests
        self._creds = None
        self._creds_lock = threading.Lock()

    # ---- service / helpers -------------------------------------------------------------

    def _credentials(self):
        """Built once and shared. Credentials caches the access token, so rebuilding the client
        per call costs no extra OAuth round-trip — but its refresh is not thread-safe and we are
        called from arbitrary to_thread workers, so the construction is guarded."""
        with self._creds_lock:
            if self._creds is None:
                from google.oauth2.credentials import Credentials

                self._creds = Credentials(
                    token=None,
                    refresh_token=self.s.google_refresh_token,
                    client_id=self.s.google_client_id,
                    client_secret=self.s.google_client_secret,
                    token_uri="https://oauth2.googleapis.com/token",
                    scopes=_SCOPES,
                )
            return self._creds

    def _service(self):
        """A FRESH client — and so a fresh httplib2 connection — for every call.

        This used to memoise the built client on `self._svc` for the life of the container
        (five weeks in production). googleapiclient rides httplib2, which holds one keep-alive
        socket per host; Google drops it long before Lisa's next calendar call, and the following
        write landed on a dead socket: `[Errno 32] Broken pipe` on 10 of 26 creates. That one
        object was also shared across `asyncio.to_thread` workers, and httplib2 is not
        thread-safe. Building per call kills both.

        The cost is nothing: `calendar.v3` ships inside googleapiclient, so with static discovery
        `build()` never touches the network — ~1.8ms, against a p50 turn of 7.3 seconds.

        `self._svc` survives as a test seam: the selftests inject a fake and it wins."""
        if self._svc is not None:
            return self._svc
        from googleapiclient.discovery import build

        return build("calendar", "v3", credentials=self._credentials(),
                     cache_discovery=False, static_discovery=True)

    def _cal(self) -> str:
        return self.s.google_calendar_id or "primary"

    def _now_iso(self) -> str:
        # tz-aware RFC 3339 — never send a naive timeMin (Google 400).
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self.s.calendar_timezone)).isoformat()
        except Exception:
            return datetime.now().astimezone().isoformat()

    @staticmethod
    def _plus_minutes(iso: str, minutes: int) -> str:
        return (datetime.fromisoformat(iso) + timedelta(minutes=minutes)).isoformat()

    @staticmethod
    def _fmt(iso: str | None) -> str:
        if not iso:
            return "?"
        try:
            return datetime.fromisoformat(iso).strftime("%a %d %b %H:%M")
        except ValueError:
            return iso

    def _event_view(self, e: dict) -> dict:
        start = e.get("start") or {}
        end = e.get("end") or {}
        all_day = bool(start.get("date"))
        s_iso = start.get("dateTime") or start.get("date")
        e_iso = end.get("dateTime") or end.get("date")
        if all_day and e_iso:
            # Google's exclusive boundary stops here and goes no further. Above the handler an
            # all-day event ends on its LAST DAY, which is what a person means by "through the 16th".
            e_iso = from_wire_end(e_iso)
        return {
            "event_id": e.get("id"),
            "title": e.get("summary") or "(no title)",
            "start": s_iso,
            "end": e_iso,
            "all_day": all_day,
            "days": span_days(s_iso, e_iso) if (all_day and s_iso and e_iso) else None,
            "attendees": [a.get("email") for a in e.get("attendees") or [] if a.get("email")],
            # Who has actually accepted. Carried now so a future card can show it without
            # needing another round-trip; nothing renders it yet.
            "attendee_status": {a["email"]: a.get("responseStatus")
                                for a in e.get("attendees") or [] if a.get("email")},
            "location": e.get("location"),
            "html_link": e.get("htmlLink"),
        }

    @staticmethod
    def _meet_link(e: dict) -> str | None:
        if e.get("hangoutLink"):
            return e["hangoutLink"]
        for ep in (e.get("conferenceData") or {}).get("entryPoints") or []:
            if ep.get("entryPointType") == "video" and ep.get("uri"):
                return ep["uri"]
        return None

    @staticmethod
    def _lines(views: list[dict], fmt) -> str:
        """Candidate lines for `find` — numbered, id-bearing, so the model can pick one to act on."""
        return "\n".join(
            f"{i + 1}. {v['title']} — {fmt(v['start'])}"
            + (f" @ {v['location']}" if v.get("location") else "")
            + (f" (with {', '.join(v['attendees'])})" if v.get("attendees") else "")
            + f" [id={v['event_id']}]"
            for i, v in enumerate(views)
        )

    @staticmethod
    def _agenda(views: list[dict], window_start: date | None = None) -> str:
        """The owner's agenda layout for `list` — start-sorted events grouped by local day:
            DD/MMM - Weekday
            HH:MM - Title
        one blank line between days; an all-day event shows 'All day - Title'."""
        days: list[tuple] = []  # (date, [lines])
        for v in views:
            iso = v.get("start")
            if not iso:
                continue
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                continue
            all_day = v.get("all_day", is_date_only(iso))
            # An in-progress MULTI-DAY ALL-DAY event starts before the window; group it under
            # the window's first day so "what's on this week" never opens with last Thursday.
            # Narrow on purpose: a single-day or timed event keeps its own date, always.
            day = dt.date()
            if (window_start and all_day and (v.get("days") or 1) > 1
                    and day < window_start):
                day = window_start
            if not days or days[-1][0] != day:
                header = f"{day.day:02d}/{_MONTHS[day.month - 1]} - {_WEEKDAYS[day.weekday()]}"
                days.append((day, [header]))
            if all_day:
                n = v.get("days") or 1
                if n <= 1:
                    time_str = "All day"
                elif day == dt.date():
                    time_str = f"All day ({n} days, through {v.get('end')})"
                else:
                    time_str = f"All day (day {(day - dt.date()).days + 1} of {n})"
            else:
                time_str = dt.strftime("%H:%M")
            days[-1][1].append(f"{time_str} - {v.get('title') or '(no title)'}")
        return "\n\n".join("\n".join(lines) for _, lines in days)

    @staticmethod
    def _default_last_day(first: str, prev: dict) -> str:
        """No end given on an all-day write: keep the event's current span (a move), else one
        day (a create). Moving a 3-day trip must not silently shrink it to a single day."""
        if prev.get("all_day") and prev.get("start") and prev.get("end"):
            return _shift(first, span_days(prev["start"], prev["end"]) - 1)
        return first

    def _body_from(self, inp: dict, base: dict | None = None,
                   existing: dict | None = None) -> tuple[dict, str | None]:
        """Build (event body, conference intent). Only fields present in `inp` are set, so the
        same builder serves create (full) and update (partial patch).

        The KIND is resolved once by `resolve_kind` and the values are then COERCED to match it
        — an `all_day: true` carrying a full datetime becomes the right whole day rather than a
        midnight meeting. Both sides are always written together, because Google rejects a mixed
        body ("start and end must both be date or both be dateTime"), so a patch that flips the
        kind must carry both even when only one of them changed."""
        body = dict(base or {})
        tz = self.s.calendar_timezone
        if provided(inp, "title"):
            body["summary"] = inp["title"]

        prev = existing or {}
        new_start = inp["start"] if provided(inp, "start") else None
        new_end = inp["end"] if provided(inp, "end") else None
        all_day = resolve_kind(inp, prev)
        # Write the dates when either side moved, or when the kind itself is flipping.
        flips = provided(inp, "all_day") and all_day != bool(prev.get("all_day"))
        if new_start or new_end or flips:
            # events.patch MERGES nested objects: writing {dateTime} over a stored {date}
            # leaves BOTH set and Google answers 400 "Invalid start time". So a kind flip has
            # to null the field it is replacing. Only on a patch — an insert has nothing to
            # merge with, and nulls there are noise.
            clear = {"dateTime": None, "timeZone": None} if existing is not None else {}
            clear_date = {"date": None} if existing is not None else {}
            if all_day:
                first = as_day(new_start or prev.get("start") or "")
                if first:   # nothing to anchor on (a flip against an event we never read) —
                    last = as_day(new_end) if new_end else self._default_last_day(first, prev)
                    if last < first:
                        last = first    # a backwards span is a typo, not a zero-day event
                    body["start"] = {"date": first, **clear}       # no timeZone — an all-day
                    body["end"] = {"date": to_wire_end(last), **clear}  # has no zone at all
            else:
                start = new_start or prev.get("start") or ""
                if is_date_only(start):   # all-day -> timed, and no hour was given
                    start = f"{start}T{self.s.default_start_hour}"
                if new_end and not is_date_only(new_end):
                    end = new_end
                else:
                    dur = inp.get("duration_min") or self.s.default_meeting_minutes
                    end = self._plus_minutes(start, dur)
                body["start"] = {"dateTime": start, "timeZone": tz, **clear_date}
                body["end"] = {"dateTime": end, "timeZone": tz, **clear_date}

        # Conference INTENT, not a "want a meet" boolean. Removing a Meet is a real instruction
        # with its own wire form (conferenceData: null), and BOTH forms are ignored by Google
        # unless conferenceDataVersion=1 rides with the request — which used to be attached only
        # when creating one, so a removal was dropped on the floor even once it was built.
        conference: str | None = None
        if provided(inp, "virtual") and inp["virtual"]:
            body["location"] = None  # video wins over a place
            conference = "create"
            body["conferenceData"] = {"createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }}
        else:
            if provided(inp, "virtual"):   # explicitly false → drop the video call
                body["conferenceData"] = None
                conference = "remove"
            if provided(inp, "location"):
                body["location"] = inp["location"]
        if provided(inp, "attendees"):
            body["attendees"] = [{"email": e} for e in inp["attendees"]]
        return body, conference

    @staticmethod
    def _send_updates(inp: dict) -> str:
        return "none" if inp.get("send_invites") is False else "all"

    # ---- verbs (synchronous; run in a thread) ------------------------------------------

    def _create(self, inp: dict) -> ActionResult:
        if not inp.get("title") or not inp.get("start"):
            return {"ok": False, "error": "validation",
                    "summary": "create needs a title and a start time."}
        if resolve_kind(inp) and inp.get("end") and as_day(inp["end"]) < as_day(inp["start"]):
            return {"ok": False, "error": "validation",
                    "summary": "the last day of an all-day event cannot be before its first."}
        body, conference = self._body_from(inp)
        want_meet = conference == "create"
        kw = dict(calendarId=self._cal(), body=body, sendUpdates=self._send_updates(inp))
        if conference:
            kw["conferenceDataVersion"] = 1
        # Idempotency across the retry in `run`. The key is minted ONCE PER run() call, so a
        # replayed insert reuses it and Google rejects the duplicate (409) instead of
        # double-booking — while a genuinely new request to book the same slot again gets a fresh
        # key and succeeds. Deriving the key from the event's content instead would wrongly
        # collapse that second, deliberate booking into the first.
        key = inp.get("_idempotency_key")
        if key:
            body["id"] = key
        try:
            ev = self._service().events().insert(**kw).execute()
        except Exception as exc:
            if key and self._is_duplicate(exc):
                # The insert DID land before the transport died; the retry is the duplicate.
                # Return the event we already created rather than reporting a failure.
                ev = self._service().events().get(calendarId=self._cal(), eventId=key).execute()
            else:
                raise
        view = self._event_view(ev)
        meet = self._meet_link(ev)
        # The read-back summary carries what the model needs to render the "Scheduled:" message:
        # the ISO start (it formats date/time), where (Video call vs a place — NOT the Meet URL,
        # per the message spec), guest count, and the EVENT link.
        # Describe what Google RETURNED, never what we asked for. Conference creation is async
        # and can fail, and this used to read `want_meet` — so a create whose Meet never
        # materialised still reported "Video call (Google Meet)".
        if meet:
            where = "Video call (Google Meet)"
        elif want_meet:
            where = "video call REQUESTED BUT NOT CREATED"
        else:
            where = view["location"] or "no location"
        parts = [f"Created '{view['title']}'", f"start {view['start']}", where]
        if view.get("all_day"):
            parts.append(f"all day ({view.get('days') or 1} day(s), through {view['end']})")
        # Count the guests GOOGLE returned, not the ones we asked for — same rule the Meet
        # read-back already follows — and say honestly whether they were emailed.
        notified = self._send_updates(inp) != "none"
        n = len(view.get("attendees") or [])
        if n:
            parts.append(f"{n} guest(s) {'invited' if notified else 'added without notice'}")
        if view["html_link"]:
            parts.append(f"event link: {view['html_link']}")
        # Full view in data so the skill can render the "Scheduled" card programmatically.
        return {"ok": True, "summary": " · ".join(parts),
                "data": {**view, "meet_link": meet, "notified": notified}}

    def _list(self, inp: dict) -> ActionResult:
        params = dict(calendarId=self._cal(), singleEvents=True, orderBy="startTime",
                      maxResults=10, timeMin=inp.get("time_min") or self._now_iso())
        if inp.get("time_max"):
            params["timeMax"] = inp["time_max"]
        items = self._service().events().list(**params).execute().get("items", [])
        views = [self._event_view(e) for e in items]
        try:
            window_start = datetime.fromisoformat(params["timeMin"]).date()
        except (KeyError, ValueError):
            window_start = None
        return {"ok": True, "summary": self._agenda(views, window_start) or "No upcoming events.",
                "data": {"items": views}}

    def _find(self, inp: dict) -> ActionResult:
        """Robust prose->event resolver. Full-text pass first (Google `q` matches summary,
        description, location and attendees); if that finds nothing, scan the window and rank
        client-side by fuzzy similarity to the query/title/attendee, so a slightly-off phrasing
        still surfaces the event. Returns ranked candidates each carrying its id."""
        svc = self._service()
        query = inp.get("query")
        attendee = inp.get("attendee")
        title_contains = inp.get("title_contains")
        params = dict(calendarId=self._cal(), singleEvents=True, orderBy="startTime",
                      maxResults=25, timeMin=inp.get("time_min") or self._now_iso())
        if inp.get("time_max"):
            params["timeMax"] = inp["time_max"]

        terms = " ".join(t for t in (query, attendee, title_contains) if t)
        signal = any((query, attendee, title_contains))
        items: list[dict] = []
        used_fuzzy = False
        if terms:
            items = svc.events().list(q=terms, **params).execute().get("items", [])
        if not items:  # nothing via full-text — pull the window and rank ourselves
            used_fuzzy = True
            items = svc.events().list(**params).execute().get("items", [])

        views = [self._event_view(e) for e in items]
        ranked = self._rank(views, query=query, attendee=attendee, title_contains=title_contains)
        # The full-text pass already matched, so keep those. The fuzzy fallback pulled the whole
        # window, so drop candidates that don't plausibly match the query (else "no match" would
        # return everything). No text signal (time-only find) → keep the window chronologically.
        if used_fuzzy and signal:
            ranked = [v for v in ranked
                      if self._relevance(v, query, attendee, title_contains) >= _MATCH_THRESHOLD]
        if not ranked:
            return {"ok": True, "summary": "No matching events found.", "data": {"items": []}}
        head = "Best matches (use the id to act):\n" if len(ranked) > 1 else "Found:\n"
        return {"ok": True, "summary": head + self._lines(ranked, self._fmt),
                "data": {"items": ranked}}

    @staticmethod
    def _score(view: dict, needle: str | None) -> float:
        if not needle:
            return 0.0
        hay = " ".join([view.get("title") or "", view.get("location") or "",
                        " ".join(view.get("attendees") or [])]).lower()
        n = needle.lower()
        if n in hay:
            return 1.0
        best = 0.0
        for word in hay.split():
            best = max(best, difflib.SequenceMatcher(None, n, word).ratio())
        return best

    def _relevance(self, view: dict, query, attendee, title_contains) -> float:
        return (max(self._score(view, query), self._score(view, title_contains))
                + 0.5 * self._score(view, attendee))

    def _rank(self, views: list[dict], *, query, attendee, title_contains) -> list[dict]:
        if not any((query, attendee, title_contains)):
            return views  # no signal — keep chronological order
        return sorted(
            views, key=lambda v: self._relevance(v, query, attendee, title_contains),
            reverse=True,
        )

    def _update(self, inp: dict) -> ActionResult:
        eid = inp.get("event_id")
        if not eid:
            return {"ok": False, "error": "validation", "summary": "update needs an event_id."}
        prev = self._event_view(
            self._service().events().get(calendarId=self._cal(), eventId=eid).execute())
        if (resolve_kind(inp, prev) and provided(inp, "start") and provided(inp, "end")
                and as_day(inp["end"]) < as_day(inp["start"])):
            return {"ok": False, "error": "validation",
                    "summary": "the last day of an all-day event cannot be before its first."}
        # Rescheduling with only a new start keeps the event's length. For a timed event that
        # is its duration in minutes; for an all-day event it is its span in DAYS — which the
        # old code could not express, so it fell through to the default meeting length and
        # collapsed a week-long trip into a 45-minute block at midnight.
        if provided(inp, "start") and not provided(inp, "end") and not inp.get("duration_min"):
            if not resolve_kind(inp, prev):
                try:
                    mins = int((datetime.fromisoformat(prev["end"])
                                - datetime.fromisoformat(prev["start"])).total_seconds() // 60)
                    inp = {**inp, "duration_min": mins}
                except (TypeError, ValueError):
                    pass
            # all-day spans ride on _default_last_day, off `prev` — no guess needed here.
        body, conference = self._body_from(inp, existing=prev)
        if not body:
            # An empty patch is a guaranteed lie: Google returns 200 and the unchanged event, and
            # we would report "Updated". This is exactly how `virtual: false` used to behave.
            return {"ok": False, "error": "no_change",
                    "summary": "update had nothing to change — say what should be different."}

        kw = dict(calendarId=self._cal(), eventId=eid, body=body,
                  sendUpdates=self._send_updates(inp))
        if conference:
            kw["conferenceDataVersion"] = 1
        ev = self._service().events().patch(**kw).execute()
        view = self._event_view(ev)
        meet = self._meet_link(ev)

        # Verify against the RESPONSE. A 200 only says Google accepted the request, not that it
        # did what was asked — the removal of a Meet came back 200 with the Meet still attached.
        missed = self._unapplied(inp, view, meet)
        if missed:
            return {"ok": False, "error": "not_applied",
                    "summary": f"Google accepted the change but did not apply: {', '.join(missed)}.",
                    "data": {**view, "meet_link": meet}}

        return {"ok": True, "summary": f"Updated '{view['title']}' → {self._fmt(view['start'])}",
                "data": {**view, "meet_link": meet}}

    @staticmethod
    def _unapplied(inp: dict, view: dict, meet: str | None) -> list[str]:
        """Which requested changes are NOT visible in the event Google returned."""
        missed: list[str] = []
        if provided(inp, "virtual"):
            if inp["virtual"] and not meet:
                missed.append("video call not created")
            elif not inp["virtual"] and meet:
                missed.append("video call still attached")
        if provided(inp, "title") and (view.get("title") or "") != inp["title"]:
            missed.append("title")
        if provided(inp, "location") and not inp.get("virtual"):
            if (view.get("location") or "") != inp["location"]:
                missed.append("location")
        if provided(inp, "attendees") and set(view.get("attendees") or []) != set(inp["attendees"]):
            missed.append("guests")
        if provided(inp, "all_day") and bool(view.get("all_day")) != bool(inp["all_day"]):
            missed.append("all-day setting")
        if provided(inp, "start"):
            want = as_day(inp["start"]) if view.get("all_day") else inp["start"]
            if not _same_dt(view.get("start"), want):
                missed.append("start time")
        # An all-day span is only half-verified by the start; a wrong end is a wrong event.
        if view.get("all_day") and provided(inp, "end") and view.get("end") != as_day(inp["end"]):
            missed.append("last day")
        return missed

    def _delete(self, inp: dict) -> ActionResult:
        eid = inp.get("event_id")
        if not eid:
            return {"ok": False, "error": "validation", "summary": "delete needs an event_id."}
        # Fetch the event first, so the cancellation card can show its title/time and whether it
        # had guests (the delete itself is what matters — best-effort).
        view: dict = {}
        try:
            ev = self._service().events().get(calendarId=self._cal(), eventId=eid).execute()
            view = self._event_view(ev)
        except Exception:
            pass
        notified = self._send_updates(inp) != "none"
        self._service().events().delete(
            calendarId=self._cal(), eventId=eid,
            sendUpdates=self._send_updates(inp)).execute()
        label = f" '{view['title']}'" if view.get("title") else ""
        return {"ok": True, "summary": f"Cancelled{label}.",
                "data": {"event_id": eid, "title": view.get("title"), "start": view.get("start"),
                         "end": view.get("end"), "all_day": view.get("all_day"),
                         "days": view.get("days"), "attendees": view.get("attendees"),
                         "had_attendees": bool(view.get("attendees")), "notified": notified}}

    # ---- dispatch ----------------------------------------------------------------------

    _VERBS = {"create": "_create", "list": "_list", "find": "_find",
              "update": "_update", "delete": "_delete"}

    async def _remember(self, inputs: dict) -> ActionResult:
        """The address-book write. Runs OUT OF BAND — stripped at the confirm node, dispatched
        detached, never rendered. It touches Google only through the Directory's durable outbox,
        so nothing in the reply path can be delayed or failed by it."""
        if self.directory is None:
            return {"ok": True, "summary": "contacts off", "data": {}}
        name = (inputs.get("name") or "").strip()
        email = (inputs.get("email") or "").strip()
        if not email:
            return {"ok": False, "error": "validation", "summary": "remember needs an email."}
        await self.directory.remember(name, email, phone=inputs.get("_phone"))
        return {"ok": True, "summary": f"noted {name}".strip(), "data": {"email": email}}

    def _attempts_for(self, verb: str, inputs: dict) -> int:
        """How many times this verb may be replayed.

        `create` is protected by its idempotency key: a replayed insert collides with 409 and we
        re-fetch, so guests are invited exactly once. `update` and `delete` have no such shield —
        Calendar v3 exposes no idempotency parameter on patch or delete, so there is no token to
        mint — and they default to notifying every guest. A broken pipe is genuinely ambiguous
        about whether Google received the write, so replaying one can email everybody a second
        time about a single change. Replay them only when no notification can go out."""
        if verb in ("update", "delete") and self._send_updates(inputs) != "none":
            return 1
        return _MAX_ATTEMPTS

    @staticmethod
    def _is_duplicate(exc: Exception) -> bool:
        """Google's "that identifier already exists" — our own retry landing twice."""
        return getattr(getattr(exc, "resp", None), "status", None) == 409

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Did the request fail before Google could answer? Only then is a replay safe."""
        if isinstance(exc, _TRANSIENT_EXC):
            return True
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status in _TRANSIENT_STATUS

    async def run(self, verb: str, inputs: dict) -> ActionResult:
        """Run one verb, retrying only what is safe to replay.

        A fresh connection per call (see `_service`) removes the cause of the broken pipes; this
        removes the class. Both are wanted — the next transient error will not be this one."""
        if verb == "remember":
            # Async and local: no Google call, no thread, no retry ladder.
            try:
                return await self._remember(inputs)
            except Exception as exc:  # never raise into the graph
                log.warning("calendar.remember failed: %s", exc)
                return {"ok": False, "error": str(exc), "summary": "remember failed"}

        method = self._VERBS.get(verb)
        if not method:
            return {"ok": False, "error": "unknown_verb",
                    "summary": f"no calendar verb {verb!r}"}

        # One key for this whole run, retries included. Only `create` reads it; a custom event
        # id must be base32hex (0-9, a-v), which uuid4().hex satisfies.
        if verb == "create":
            inputs = {**inputs, "_idempotency_key": uuid.uuid4().hex}

        attempts = self._attempts_for(verb, inputs)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return await asyncio.to_thread(getattr(self, method), inputs)
            except Exception as exc:  # never raise into the graph
                last = exc
                if not self._is_transient(exc) or attempt == attempts - 1:
                    break
                log.warning("calendar.%s transient (%s); retry %d/%d",
                            verb, exc, attempt + 1, attempts - 1)
                await asyncio.sleep(_BACKOFF_BASE * (2 ** attempt))
        return self._on_error(last, verb)

    def _on_error(self, exc: Exception, verb: str) -> ActionResult:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (401, 403):
            err = "auth"
        elif status == 404:
            err = "not_found"
        elif self._is_transient(exc):
            # Named, so the confirm/respond path can tell "the network hiccuped" from "Google
            # said no" and report it honestly instead of silently asking the same question again.
            err = "transient"
        else:
            err = str(exc)
        log.exception("calendar.%s failed: %s", verb, err)
        return {"ok": False, "error": err, "summary": f"calendar.{verb} failed: {err}"}
