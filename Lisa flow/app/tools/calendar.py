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
import uuid
from datetime import datetime, timedelta

from .base import ActionResult

log = logging.getLogger("mary.tools.calendar")

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Fuzzy-fallback relevance floor: how close a windowed candidate must be to the query before
# `find` will surface it, so "no match" returns empty instead of the whole calendar.
_MATCH_THRESHOLD = 0.6

# Fixed English labels for the agenda layout, so a listing reads the same regardless of the
# container's locale (strftime("%A"/"%b") would follow the server locale).
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DESCRIBE = (
    "Create, find, reschedule or cancel events on {owner_name}'s Google Calendar."
)

# Per-task guidance, appended to the system prompt (registry.build_task_prompts). Templated
# with {owner_name}. Co-located with the handler so prompt and behaviour never drift.
GUIDANCE = """Calendar actions — you manage {owner_name}'s Google Calendar: create an event, list the agenda, find an event, reschedule it (update), or cancel it (delete). General rules first, then each verb.

ALWAYS
- The current date is given to you. Resolve every relative time yourself into a full ISO 8601 datetime WITH the offset ("next Friday 3pm" → 2026-08-07T15:00:00-03:00) — never pass a vague phrase to the tool.
- Report what actually happened FROM the tool's result — never announce success before you have seen it come back.
- Do not write the reply header (the system stamps it). No emoji.
- You cannot make recurring/repeating events or all-day / multi-day events. If asked for one, say so briefly and offer a single timed event instead — never pretend you set up a recurrence.

CREATE
- You only need a title and a start; do not interrogate {owner_name} for details he did not give.
- Title is what the event is ABOUT — a short topic ("Budget review", "Apartment viewing") — not who is on it; "Meeting with Ana" names the person, not the subject.
- If he gives no end, it defaults to 45 minutes. Use `virtual: true` for a video call (a Google Meet link is created and the location is dropped — video wins over a place); otherwise set `location`. Add `attendees` emails when he names people; they are emailed an invite by default — set `send_invites: false` only if he does not want that.
- If he is vague about the hour, assume a sensible default (morning ~09:00, lunch ~12:00, afternoon ~14:00, evening ~19:00) and show that assumption in the confirmation so he can fix it.
- Get his go-ahead first — set `confirmed: true` only after he agrees — using EXACTLY this shape (omit the attendees line when there are none):

Ok, confirming before I dispatch:
<title>
<date>
<time>
<attendees>
<location — or "Google Meet (video call)" if virtual>

Should I go ahead, or is anything missing?

- Once it is created, confirm briefly and include the Google Meet link when there is one.

LIST
- Read-only, no confirmation. Resolve the window from his question (default: what is coming up).
- The list tool returns the agenda ALREADY formatted by day. Send it back exactly as returned — do not reformat, re-sort, or add lines. The shape is a "DD/MMM - Weekday" header, then one "HH:MM - Title" per event, with a blank line between days. Say plainly when there is nothing.

FIND
- The resolver. Search by title words (`query`), the person on it (`attendee`), and/or a time window (`time_min`/`time_max`). If he asked "when is X", answer directly; when it is a step toward an edit or cancel it is internal — you do not announce it.
- One clear match → proceed. Several → read them back and ask which one. None → say so. Never invent an id.

UPDATE (reschedule / edit)
- ALWAYS `find` first to get the id — never guess one. A new start keeps the original length unless you also give a new end. You can change the time, title, location, virtual, or attendees.
- Get his go-ahead first (set `confirmed: true` only after he agrees), showing WHAT changes as before → now — EXACTLY this shape:

Ok, confirming this change:
<title> — <current date/time>
<each change as "Field: was → now", e.g. Time: Fri 8 Aug 15:00 → Mon 11 Aug 17:00>

Go ahead?

- Once it is changed, confirm briefly what the event is now.

DELETE (cancel)
- ALWAYS `find` first to get the id. Confirm WHICH event so there is no mistake — set `confirmed: true` only after he agrees — using EXACTLY this shape (include the last sentence only if the event has attendees):

Ok, confirming this cancellation:
<title>
<date>
<time>

Cancel this one? The guests will be notified.

- Once it is cancelled, confirm briefly."""


class GoogleCalendarService:
    """Local tool handler for the `calendar` domain. Instantiated once in deps with settings."""

    def __init__(self, settings) -> None:
        self.s = settings
        self._svc = None  # lazy Calendar service (or a fake, injected in tests)

    # ---- service / helpers -------------------------------------------------------------

    def _service(self):
        if self._svc is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(
                token=None,
                refresh_token=self.s.google_refresh_token,
                client_id=self.s.google_client_id,
                client_secret=self.s.google_client_secret,
                token_uri="https://oauth2.googleapis.com/token",
                scopes=_SCOPES,
            )
            self._svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._svc

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
        return {
            "event_id": e.get("id"),
            "title": e.get("summary") or "(no title)",
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "attendees": [a.get("email") for a in e.get("attendees") or [] if a.get("email")],
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
    def _agenda(views: list[dict]) -> str:
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
            all_day = "T" not in iso
            if not days or days[-1][0] != dt.date():
                header = f"{dt.day:02d}/{_MONTHS[dt.month - 1]} - {_WEEKDAYS[dt.weekday()]}"
                days.append((dt.date(), [header]))
            time_str = "All day" if all_day else dt.strftime("%H:%M")
            days[-1][1].append(f"{time_str} - {v.get('title') or '(no title)'}")
        return "\n\n".join("\n".join(lines) for _, lines in days)

    def _body_from(self, inp: dict, base: dict | None = None) -> tuple[dict, bool]:
        """Build (event body, want_meet). Only fields present in `inp` are set, so the same
        builder serves create (full) and update (partial patch)."""
        body = dict(base or {})
        tz = self.s.calendar_timezone
        if inp.get("title") is not None:
            body["summary"] = inp["title"]
        if inp.get("start"):
            body["start"] = {"dateTime": inp["start"], "timeZone": tz}
            end = inp.get("end")
            if not end:
                dur = inp.get("duration_min") or self.s.default_meeting_minutes
                end = self._plus_minutes(inp["start"], dur)
            body["end"] = {"dateTime": end, "timeZone": tz}
        elif inp.get("end"):
            body["end"] = {"dateTime": inp["end"], "timeZone": tz}
        want_meet = False
        if inp.get("virtual"):
            body["location"] = None  # video wins over a place
            want_meet = True
            body["conferenceData"] = {"createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }}
        elif inp.get("location") is not None:
            body["location"] = inp["location"]
        if inp.get("attendees") is not None:
            body["attendees"] = [{"email": e} for e in inp["attendees"]]
        return body, want_meet

    @staticmethod
    def _send_updates(inp: dict) -> str:
        return "none" if inp.get("send_invites") is False else "all"

    # ---- verbs (synchronous; run in a thread) ------------------------------------------

    def _create(self, inp: dict) -> ActionResult:
        if not inp.get("title") or not inp.get("start"):
            return {"ok": False, "error": "validation",
                    "summary": "create needs a title and a start time."}
        body, want_meet = self._body_from(inp)
        kw = dict(calendarId=self._cal(), body=body, sendUpdates=self._send_updates(inp))
        if want_meet:
            kw["conferenceDataVersion"] = 1
        ev = self._service().events().insert(**kw).execute()
        view = self._event_view(ev)
        meet = self._meet_link(ev)
        parts = [f"Created '{view['title']}' {self._fmt(view['start'])}"]
        n = len(inp.get("attendees") or [])
        if n:
            parts.append(f"invited {n}")
        if meet:
            parts.append(meet)
        return {"ok": True, "summary": " · ".join(parts),
                "data": {"event_id": view["event_id"], "html_link": view["html_link"],
                         "meet_link": meet}}

    def _list(self, inp: dict) -> ActionResult:
        params = dict(calendarId=self._cal(), singleEvents=True, orderBy="startTime",
                      maxResults=10, timeMin=inp.get("time_min") or self._now_iso())
        if inp.get("time_max"):
            params["timeMax"] = inp["time_max"]
        items = self._service().events().list(**params).execute().get("items", [])
        views = [self._event_view(e) for e in items]
        return {"ok": True, "summary": self._agenda(views) or "No upcoming events.",
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
        existing = self._service().events().get(calendarId=self._cal(), eventId=eid).execute()
        # Rescheduling with only a new start? Preserve the original duration instead of
        # silently collapsing to the default meeting length.
        if inp.get("start") and not inp.get("end") and not inp.get("duration_min"):
            try:
                os_ = existing["start"]["dateTime"]
                oe = existing["end"]["dateTime"]
                mins = int((datetime.fromisoformat(oe) - datetime.fromisoformat(os_))
                           .total_seconds() // 60)
                inp = {**inp, "duration_min": mins}
            except (KeyError, ValueError):
                pass
        body, want_meet = self._body_from(inp)
        kw = dict(calendarId=self._cal(), eventId=eid, body=body,
                  sendUpdates=self._send_updates(inp))
        if want_meet:
            kw["conferenceDataVersion"] = 1
        ev = self._service().events().patch(**kw).execute()
        view = self._event_view(ev)
        return {"ok": True, "summary": f"Updated '{view['title']}' → {self._fmt(view['start'])}",
                "data": {"event_id": view["event_id"]}}

    def _delete(self, inp: dict) -> ActionResult:
        eid = inp.get("event_id")
        if not eid:
            return {"ok": False, "error": "validation", "summary": "delete needs an event_id."}
        title = None
        try:
            title = self._service().events().get(
                calendarId=self._cal(), eventId=eid).execute().get("summary")
        except Exception:  # best-effort label; the delete is what matters
            pass
        self._service().events().delete(
            calendarId=self._cal(), eventId=eid, sendUpdates="all").execute()
        label = f" '{title}'" if title else ""
        return {"ok": True, "summary": f"Cancelled{label}.", "data": {"event_id": eid}}

    # ---- dispatch ----------------------------------------------------------------------

    _VERBS = {"create": "_create", "list": "_list", "find": "_find",
              "update": "_update", "delete": "_delete"}

    async def run(self, verb: str, inputs: dict) -> ActionResult:
        method = self._VERBS.get(verb)
        if not method:
            return {"ok": False, "error": "unknown_verb",
                    "summary": f"no calendar verb {verb!r}"}
        try:
            return await asyncio.to_thread(getattr(self, method), inputs)
        except Exception as exc:  # never raise into the graph
            return self._on_error(exc, verb)

    def _on_error(self, exc: Exception, verb: str) -> ActionResult:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (401, 403):
            err = "auth"
        elif status == 404:
            err = "not_found"
        else:
            err = str(exc)
        log.exception("calendar.%s failed: %s", verb, err)
        return {"ok": False, "error": err, "summary": f"calendar.{verb} failed: {err}"}
