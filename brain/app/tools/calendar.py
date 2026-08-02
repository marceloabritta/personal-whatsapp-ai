"""GoogleCalendarService — the local calendar handler (google_api backend).

Direct Google Calendar API v3 over an OAuth2 refresh-token client (ported from the
old lib/google.js). The googleapis client is synchronous, so each call runs in a
thread. Reads GOOGLE_* from settings; targets the owner's GOOGLE_CALENDAR_ID.

Verbs: create · list (also 'find' via query) · update · delete. Every path returns
an ActionResult {ok, summary, data, error?, need?} — never raises into the graph."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from .base import ActionResult

log = logging.getLogger("mary.tools.calendar")

_TZ = "America/Sao_Paulo"
_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Task-specific guidance appended to the system prompt (registry.build_task_prompts).
# Lives with the tool so the instructions and the handler evolve together. `{owner_name}`
# is rendered in when the prompt is assembled.
GUIDANCE = """\
Calendar actions
You can create, find, reschedule and cancel events on {owner_name}'s Google Calendar via \
the calendar.* actions. All times are America/Sao_Paulo: emit start/end as ISO 8601 with \
the -03:00 offset, and resolve relative dates ("tomorrow", "next Tuesday", "in an hour") \
against the current date yourself before acting — never pass a phrase as a time.

Creating (calendar.create). The only things you truly need are a title and a start \
(date + time). Everything else is optional — take what {owner_name} volunteers and don't \
interrogate. Clarify a detail only when acting without it would likely be wrong: how long \
it runs (a sensible default length applies if unsaid), whether it's virtual (set \
virtual:true to attach a video link) or in person (then capture the location), who else \
is invited (collect each attendee's email so invites can go out, and set send_invites \
false if {owner_name} doesn't want them notified). Anyone in the chat may supply a missing \
email or detail — {owner_name} or the guest. Once the scope is clear, restate the plan and \
create only on {owner_name}'s go-ahead.

Changing or cancelling (calendar.update / calendar.delete). Never invent or assume an \
event id. First calendar.list to locate the target (by name, topic, or time window), match \
it, and confirm with {owner_name} that it's the right event before you act. update patches \
only the fields you pass and leaves the rest intact; delete cancels the event and notifies \
its attendees.

Reading (calendar.list). Use it freely — to answer "what's on my calendar?" and as the \
lookup step before every edit or cancellation. It changes nothing, so it needs no \
confirmation."""


class GoogleCalendarService:
    def __init__(self, settings) -> None:
        self.s = settings
        self._svc = None  # built lazily so importing this module needs no google libs

    # -- service (lazy) ------------------------------------------------------
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

    @property
    def _cal_id(self) -> str:
        return self.s.google_calendar_id or "primary"

    # -- dispatch ------------------------------------------------------------
    async def run(self, verb: str, inputs: dict) -> ActionResult:
        inputs = inputs or {}
        try:
            if verb == "create":
                return await asyncio.to_thread(self._create, inputs)
            if verb == "list":
                return await asyncio.to_thread(self._list, inputs)
            if verb == "update":
                return await asyncio.to_thread(self._update, inputs)
            if verb == "delete":
                return await asyncio.to_thread(self._delete, inputs)
            return ActionResult(ok=False, summary=f"unknown verb {verb!r}", error="validation")
        except Exception as exc:  # HttpError et al — classify, never raise into the graph
            return self._on_error(exc)

    # -- helpers -------------------------------------------------------------
    def _on_error(self, exc: Exception) -> ActionResult:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (401, 403):
            log.error("calendar auth error: %s", exc)
            return ActionResult(ok=False, summary="calendar auth failed", error="auth")
        if status == 404:
            return ActionResult(ok=False, summary="event not found", error="not_found")
        log.exception("calendar call failed: %s", exc)
        return ActionResult(ok=False, summary=f"calendar error: {exc}", error=str(exc))

    @staticmethod
    def _fmt(iso: str) -> str:
        try:
            return datetime.fromisoformat(iso).strftime("%a %d %b %H:%M")
        except (ValueError, TypeError):
            return iso or "?"

    def _event_view(self, ev: dict) -> dict:
        start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        end = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
        return {
            "event_id": ev.get("id"),
            "title": ev.get("summary") or "(no title)",
            "start": start,
            "end": end,
            "attendees": [a.get("email") for a in ev.get("attendees", []) if a.get("email")],
            "location": ev.get("location"),
            "html_link": ev.get("htmlLink"),
        }

    def _body_from(self, inp: dict, base: dict | None = None) -> tuple[dict, bool]:
        body = dict(base or {})
        if inp.get("title") is not None:
            body["summary"] = inp["title"]
        if inp.get("start") is not None:
            start = inp["start"]
            body["start"] = {"dateTime": start, "timeZone": _TZ}
            end = inp.get("end")
            if not end:
                mins = inp.get("duration_min") or self.s.default_meeting_minutes
                end = (datetime.fromisoformat(start) + timedelta(minutes=mins)).isoformat()
            body["end"] = {"dateTime": end, "timeZone": _TZ}
        elif inp.get("end") is not None:
            body["end"] = {"dateTime": inp["end"], "timeZone": _TZ}
        # location XOR virtual — video wins
        want_meet = False
        if inp.get("virtual") is True:
            want_meet = True
            body["location"] = None
        elif inp.get("location") is not None:
            body["location"] = inp["location"]
        if inp.get("attendees") is not None:
            body["attendees"] = [{"email": e} for e in inp["attendees"] if e]
        return body, want_meet

    # -- verbs ---------------------------------------------------------------
    def _create(self, inp: dict) -> ActionResult:
        if not inp.get("title") or not inp.get("start"):
            return ActionResult(ok=False, summary="need title and start", error="validation",
                                need="start" if inp.get("title") else "title")
        body, want_meet = self._body_from(inp)
        kwargs = {"calendarId": self._cal_id, "body": body}
        kwargs["sendUpdates"] = "none" if inp.get("send_invites") is False else "all"
        if want_meet:
            body["conferenceData"] = {"createRequest": {"requestId": inp["start"] + inp["title"][:8]}}
            kwargs["conferenceDataVersion"] = 1
        ev = self._service().events().insert(**kwargs).execute()
        v = self._event_view(ev)
        meet = ev.get("hangoutLink")
        where = f" · {meet}" if meet else (f" · {v['location']}" if v.get("location") else "")
        n = len(v["attendees"])
        return ActionResult(
            ok=True,
            summary=f"Created '{v['title']}' {self._fmt(v['start'])}, invited {n}{where}",
            data={"event_id": v["event_id"], "html_link": v["html_link"], "meet_link": meet},
        )

    def _list(self, inp: dict) -> ActionResult:
        svc = self._service()
        params = {
            "calendarId": self._cal_id,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 10,
        }
        if inp.get("time_min"):
            params["timeMin"] = inp["time_min"]
        else:
            params["timeMin"] = datetime.now().astimezone().isoformat()
        if inp.get("time_max"):
            params["timeMax"] = inp["time_max"]
        if inp.get("query"):
            params["q"] = inp["query"]
        events = svc.events().list(**params).execute().get("items", [])
        items = [self._event_view(e) for e in events]
        if not items:
            return ActionResult(ok=True, summary="No matching events.", data={"items": []})
        lines = [f"{i+1}. {it['title']} — {self._fmt(it['start'])} [id={it['event_id']}]"
                 for i, it in enumerate(items)]
        return ActionResult(ok=True, summary=f"{len(items)} event(s):\n" + "\n".join(lines),
                            data={"items": items})

    def _update(self, inp: dict) -> ActionResult:
        eid = inp.get("event_id")
        if not eid:
            return ActionResult(ok=False, summary="need event_id", error="validation", need="event_id")
        svc = self._service()
        current = svc.events().get(calendarId=self._cal_id, eventId=eid).execute()
        body, want_meet = self._body_from(inp, base={})
        kwargs = {"calendarId": self._cal_id, "eventId": eid, "body": body, "sendUpdates": "all"}
        if want_meet:
            kwargs["conferenceDataVersion"] = 1
        ev = svc.events().patch(**kwargs).execute()
        v = self._event_view(ev)
        return ActionResult(ok=True, summary=f"Updated '{v['title']}' — now {self._fmt(v['start'])}",
                            data={"event_id": v["event_id"], "html_link": v["html_link"]})

    def _delete(self, inp: dict) -> ActionResult:
        eid = inp.get("event_id")
        if not eid:
            return ActionResult(ok=False, summary="need event_id", error="validation", need="event_id")
        svc = self._service()
        title = "(event)"
        try:
            title = svc.events().get(calendarId=self._cal_id, eventId=eid).execute().get("summary", title)
        except Exception:
            pass
        svc.events().delete(calendarId=self._cal_id, eventId=eid, sendUpdates="all").execute()
        return ActionResult(ok=True, summary=f"Cancelled '{title}'", data={"event_id": eid})
