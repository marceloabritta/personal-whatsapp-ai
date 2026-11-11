"""End-to-end verification for the busy check: she says he is not free, and nothing else.

Drives the REAL compiled graph with an in-memory checkpointer, a fake Evolution, the real calendar
handler over a fake Google service, and a stub reasoner. No network, no Postgres, no Anthropic key.

    cd "Lisa flow" && .venv/bin/python tests/run_busy.py
Exits non-zero if any check fails.

The fake Google answers with whole events, title and place included, whatever `fields` asked for.
So "the title never leaves the handler" is proven against a Google that hands it over, not against
one that politely withholds it.

Covers: what counts as taking his time, the one definition of when a write ends, the free times
left in a day, the note the model is given, the yes-anyway rule, the line said in code when the
model says nothing, BUSY_CHECK_ENABLED=false as the pre-feature build, and the flows through the
graph: held back, settled with the other person, booked anyway, a failed read, an update."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.presets import MEETING_MINUTES, Presets  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import (  # noqa: E402
    confirm_policies, count_optionals, count_unions, output_schema_for, render_policies,
    resolve_gates, system_prompt_for,
)
from app.skills.calendar import (  # noqa: E402
    _say_he_is_busy, _told_and_answered, busy_note, calendar_busy_check,
)
from app.skills.calendar_format import busy_line  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.tools.calendar import (  # noqa: E402
    GoogleCalendarService, free_windows, keep_length, timed_span,
)
from app.trace import build_trace  # noqa: E402
from run_step3 import FakeEvolution, StubReasoner  # noqa: E402

CHAT = "5512996600002@s.whatsapp.net"     # a one-to-one chat with a partner
# Far enough ahead that "what is left of today" never trims the day under test.
DAY = "2031-03-12"
SECRET = "Jantar com o conselho da Canary"
PLACE = "Rua Secreta 99"
LINE_PT = "Marcelo, você já tem outro compromisso nesse horário."

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    tail = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{tail}")
    _checks["pass" if cond else "fail"] += 1


def at(h: int, m: int = 0, day: str = DAY) -> str:
    return f"{day}T{h:02d}:{m:02d}:00-03:00"


def ev(eid: str, start: str, end: str, **extra) -> dict:
    """An event as Google stores it — with everything she must never repeat."""
    return {"id": eid, "summary": SECRET, "location": PLACE, "status": "confirmed",
            "start": {"dateTime": start}, "end": {"dateTime": end},
            "attendees": [{"email": "secreto@canary.com", "responseStatus": "accepted"}],
            **extra}


# ============================ fakes =======================================================

class _Err(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.resp = type("R", (), {"status": status})()


class _Req:
    def __init__(self, result=None, raises=None) -> None:
        self._r, self._e = result, raises

    def execute(self):
        if self._e:
            raise self._e
        return self._r


class _Events:
    def __init__(self, items, raise_on) -> None:
        self.items = list(items or [])
        self.raise_on = raise_on or {}
        self.calls: list = []

    def list(self, **kw):
        self.calls.append(("list", kw))
        if "list" in self.raise_on:
            return _Req(raises=self.raise_on["list"])
        items = list(self.items)
        if kw.get("q"):
            items = [e for e in items if kw["q"].lower() in (e.get("summary") or "").lower()]
        return _Req({"items": items})   # the whole calendar, whatever window or fields were asked

    def insert(self, **kw):
        self.calls.append(("insert", kw))
        e = dict(kw["body"])
        e["id"] = e.get("id") or "NEW1"
        e["htmlLink"] = "http://cal/NEW1"
        self.items.append(e)
        return _Req(e)

    def get(self, **kw):
        self.calls.append(("get", kw))
        for e in self.items:
            if e.get("id") == kw["eventId"]:
                return _Req(e)
        return _Req(raises=_Err(404))

    def patch(self, **kw):
        self.calls.append(("patch", kw))
        for e in self.items:
            if e.get("id") == kw["eventId"]:
                e.update(kw["body"])
                return _Req(e)
        return _Req(raises=_Err(404))


class FakeGoogle:
    def __init__(self, items=None, raise_on=None) -> None:
        self._events = _Events(items, raise_on)

    def events(self):
        return self._events

    def n(self, verb: str) -> int:
        return sum(1 for v, _ in self._events.calls if v == verb)

    def busy_reads(self) -> list:
        """The list calls the busy check made: the only ones that ask for a partial response."""
        return [kw for v, kw in self._events.calls if v == "list" and kw.get("fields")]


def handler(items=None, *, raise_on=None, presets=None, **over) -> GoogleCalendarService:
    h = GoogleCalendarService(Settings(evolution_apikey="x", **over))
    h._svc = FakeGoogle(items, raise_on)
    h.presets = presets
    return h


async def busy(h, write: dict, verb: str = "create", prev: dict | None = None) -> dict:
    return await h.run("busy", {"verb": verb, "write": write, "prev": prev})


def build(items=None, *, raise_on=None, presets=None, on: bool = True, **over):
    s = Settings(evolution_apikey="x", busy_check_enabled=on, **over)
    cal = GoogleCalendarService(s)
    cal._svc = FakeGoogle(items, raise_on)
    cal.presets = presets
    evo, stub = FakeEvolution(), StubReasoner()
    deps = Deps(settings=s, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=stub, redis=None,
                tools={"calendar": cal}, confirm_policies=confirm_policies(),
                render_policies=render_policies(), resolve_gates=resolve_gates(),
                presets=presets)
    return cal._svc, evo, stub, build_graph(deps, MemorySaver())


_mid = {"n": 0}


def said(text: str, *, mine: bool = True) -> dict:
    _mid["n"] += 1
    return {"data": {"key": {"remoteJid": CHAT, "fromMe": mine, "id": f"m{_mid['n']}"},
                     "message": {"conversation": text}, "messageTimestamp": 1930000000,
                     "pushName": "Marcelo" if mine else "Marcos Arruda"}}


async def turn(graph, text: str, *, mine: bool = True) -> dict:
    config = {"configurable": {"thread_id": make_thread_id("secretaria", CHAT)}}
    return await graph.ainvoke({"raw": said(text, mine=mine)}, config=config)


def create(h: int, m: int = 0, *, end: str | None = None, **extra) -> dict:
    a = {"task": "calendar.create", "title": "Marcelo & Marcos Arruda", "start": at(h, m), **extra}
    if end:
        a["end"] = end
    return a


def last(evo) -> str:
    return evo.sent[-1][1] if evo.sent else ""


def seen_by_model(stub, call: int = -1) -> str:
    """Everything in the thread the model was handed on that call, as one string."""
    out = []
    for m in stub.calls[call]["messages"]:
        c = m["content"] if isinstance(m, dict) else getattr(m, "content", "")
        out.append(c if isinstance(c, str) else str(c))
    return "\n".join(out)


# ============================ P1 — what takes his time ====================================

async def p1_read():
    print("P1 — the read: times only, and what counts as busy")

    h = handler([ev("B1", at(11), at(12))])
    r = await busy(h, {"title": "X", "start": at(14), "end": at(15)})
    check("a free hour is free", r["ok"] and r["data"] == {
        "checked": True, "conflict": False, "start": at(14), "end": at(15), "minutes": 60},
        detail=str(r["data"]))
    r = await busy(h, {"title": "X", "start": at(11), "end": at(12, 30)})
    check("an overlapping hour is a conflict", r["ok"] and r["data"]["conflict"] is True)
    check("the answer carries no title, place or guest of the other event",
          SECRET not in repr(r) and PLACE not in repr(r) and "canary" not in repr(r).lower())
    check("the answer is times and nothing else",
          set(r["data"]) == {"checked", "conflict", "start", "end", "minutes", "free"}
          and all(set(w) == {"start", "end"} for w in r["data"]["free"]))
    fields = h._svc.busy_reads()[-1]["fields"]
    check("Google is not even asked for the title, the place or who is invited",
          "summary" not in fields and "location" not in fields and "email" not in fields
          and "description" not in fields, detail=fields)
    kw = h._svc.busy_reads()[-1]
    check("the read is tz-aware and expands recurring events",
          datetime.fromisoformat(kw["timeMin"]).tzinfo is not None and kw["singleEvents"] is True)

    r = await busy(handler([ev("B1", at(10), at(11))]), {"title": "X", "start": at(11), "end": at(12)})
    check("ending at 11:00 leaves 11:00 free (touching is not overlapping)",
          r["data"]["conflict"] is False)
    r = await busy(handler([ev("B1", at(12), at(13))]), {"title": "X", "start": at(11), "end": at(12)})
    check("starting at 12:00 leaves 11:00-12:00 free", r["data"]["conflict"] is False)

    allday = {"id": "A1", "summary": SECRET, "start": {"date": DAY}, "end": {"date": "2031-03-13"}}
    quiet = [
        ("an all-day event (a birthday, a trip) does not block the day", allday),
        ("an event marked free does not block", ev("T", at(11), at(12), transparency="transparent")),
        ("a cancelled event does not block", ev("C", at(11), at(12), status="cancelled")),
        ("an invitation he declined does not block", ev("D", at(11), at(12), attendees=[
            {"email": "me@x.com", "self": True, "responseStatus": "declined"}])),
        ("where he works from that day does not block",
         ev("W", at(11), at(12), eventType="workingLocation")),
    ]
    for name, item in quiet:
        r = await busy(handler([item]), {"title": "X", "start": at(11), "end": at(12)})
        check(name, r["ok"] and r["data"]["conflict"] is False)
    blocks = [
        ("someone ELSE declining does not free him", ev("O", at(11), at(12), attendees=[
            {"email": "me@x.com", "self": True, "responseStatus": "accepted"},
            {"email": "o@x.com", "responseStatus": "declined"}])),
        ("an invitation he has not answered blocks", ev("N", at(11), at(12), attendees=[
            {"email": "me@x.com", "self": True, "responseStatus": "needsAction"}])),
        ("focus time and out-of-office block", ev("F", at(11), at(12), eventType="outOfOffice")),
        ("an event stored in UTC is compared as an instant",
         ev("Z", f"{DAY}T14:00:00Z", f"{DAY}T15:00:00Z")),          # 11:00-12:00 in Sao Paulo
    ]
    for name, item in blocks:
        r = await busy(handler([item]), {"title": "X", "start": at(11), "end": at(12)})
        check(name, r["ok"] and r["data"]["conflict"] is True)

    h = handler([allday])
    r = await busy(h, {"title": "Folga", "start": DAY, "all_day": True})
    check("an all-day booking is not checked, and nothing is read",
          r["data"] == {"checked": False} and h._svc.n("list") == 0)
    r = await busy(handler([ev("B1", at(11), at(12))]), {"title": "X", "start": "amanhã às 11"})
    check("a time that cannot be parsed is not checked", r["ok"] and r["data"] == {"checked": False})
    r = await busy(handler([ev("B1", at(11), at(12))]),
                   {"title": "X", "start": at(12), "end": at(11)})
    check("an end before the start is not checked", r["data"] == {"checked": False})
    r = await busy(handler([ev("B1", at(11), at(12))]),
                   {"title": "X", "start": f"{DAY}T11:30:00", "end": f"{DAY}T12:00:00"})
    check("a time with no offset is read on his clock",
          r["data"]["conflict"] is True and r["data"]["start"] == at(11, 30))

    r = await busy(handler(raise_on={"list": _Err(403)}), {"title": "X", "start": at(11)})
    check("a failed read says so (ok false), it does not say free", r["ok"] is False)


# ============================ P2 — when a write ends, decided once ========================

async def p2_span():
    print("\nP2 — when a write ends: one definition, read by the event and by the check")

    check("timed_span: an explicit end stands",
          timed_span({"start": at(11), "end": at(12, 30)}, None, 45, "09:00:00") == (at(11), at(12, 30)))
    check("timed_span: no end -> the default length",
          timed_span({"start": at(11)}, None, 45, "09:00:00") == (at(11), at(11, 45)))
    check("timed_span: duration_min wins over the default",
          timed_span({"start": at(11), "duration_min": 90}, None, 45, "09:00:00")[1] == at(12, 30))
    prev = {"start": at(15), "end": at(16), "all_day": False}
    check("keep_length: a move keeps the event's 60 minutes",
          keep_length({"start": at(11)}, prev).get("duration_min") == 60)
    check("keep_length: a new end is not overridden",
          "duration_min" not in keep_length({"start": at(11), "end": at(13)}, prev))
    check("keep_length: nothing to keep without the event", keep_length({"start": at(11)}, None)
          == {"start": at(11)})

    # No end: 45 minutes. 11:00-11:45 misses an 11:45 meeting and hits an 11:30 one.
    r = await busy(handler([ev("B1", at(11, 45), at(12, 30))]), {"title": "X", "start": at(11)})
    check("default length: 11:00 for 45 min clears an 11:45 meeting",
          r["data"]["conflict"] is False and r["data"]["end"] == at(11, 45))
    r = await busy(handler([ev("B1", at(11, 30), at(12, 30))]), {"title": "X", "start": at(11)})
    check("default length: and runs into an 11:30 one", r["data"]["conflict"] is True)

    # His preset length is the one checked, and the one booked.
    p = Presets(default_minutes=45)
    await p.set_many({MEETING_MINUTES: 90})
    h = handler([ev("B1", at(12), at(13))], presets=p)
    r = await busy(h, {"title": "X", "start": at(11)})
    check("his standard length (90 min) is what is checked",
          r["data"]["conflict"] is True and r["data"]["minutes"] == 90)
    made = await h.run("create", {"title": "X", "start": at(11)})
    check("and the event is booked to the same minute the check looked at",
          made["data"]["end"] == r["data"]["end"], detail=f"{made['data']['end']} / {r['data']['end']}")

    # Updates.
    cal = [ev("E1", at(15), at(16)), ev("E2", at(11), at(12))]
    h = handler(cal)
    r = await busy(h, {"event_id": "E1", "title": "Novo nome"}, "update")
    check("an update that moves nothing is not checked",
          r["data"] == {"checked": False} and h._svc.n("list") == 0)
    r = await busy(handler(cal), {"event_id": "E1", "start": at(15, 30)}, "update")
    check("an event moved within its own time does not clash with itself",
          r["data"]["conflict"] is False and r["data"]["end"] == at(16, 30))
    h = handler(cal)
    r = await busy(h, {"event_id": "E1", "start": at(10, 30)}, "update")
    check("moved onto another event: a conflict, with its own 60 minutes",
          r["data"]["conflict"] is True and r["data"]["end"] == at(11, 30))
    check("the event's current time is fetched when the loop has not seen it", h._svc.n("get") == 1)
    h = handler(cal)
    r = await busy(h, {"event_id": "E1", "start": at(10, 30)}, "update",
                   prev={"event_id": "E1", "start": at(15), "end": at(15, 30), "all_day": False})
    check("with the event already seen, nothing is fetched and its length is the seen one",
          h._svc.n("get") == 0 and r["data"]["end"] == at(11) and r["data"]["conflict"] is False)
    r = await busy(handler(cal), {"event_id": "E1", "end": at(18)}, "update")
    check("stretching an event checks the new stretch", r["data"] == {
        "checked": True, "conflict": False, "start": at(15), "end": at(18), "minutes": 180})


# ============================ P3 — the free times =========================================

async def p3_free():
    print("\nP3 — the free times left in the day")

    def t(h, m=0):
        return datetime.fromisoformat(at(h, m))

    need = timedelta(minutes=60)
    check("free_windows: the gaps, in order",
          free_windows([(t(11), t(12)), (t(9), t(10))], t(8), t(20), need)
          == [(t(8), t(9)), (t(10), t(11)), (t(12), t(20))])
    check("free_windows: a gap shorter than the meeting is not offered",
          free_windows([(t(9), t(10)), (t(10, 30), t(12))], t(8), t(20), need)
          == [(t(8), t(9)), (t(12), t(20))])
    check("free_windows: overlapping busy stretches are one stretch",
          free_windows([(t(9), t(11)), (t(10), t(12)), (t(10, 30), t(11))], t(8), t(13), need)
          == [(t(8), t(9)), (t(12), t(13))])
    check("free_windows: a stretch that began before the day is honoured",
          free_windows([(t(6), t(9, 30))], t(8), t(12), need) == [(t(9, 30), t(12))])
    check("free_windows: a full day has none", free_windows([(t(7), t(21))], t(8), t(20), need) == [])

    day = [ev("a", at(9), at(10)), ev("b", at(11), at(12)), ev("c", at(15), at(16))]
    r = await busy(handler(day), {"title": "X", "start": at(11), "end": at(12, 30)})
    free = [(w["start"], w["end"]) for w in r["data"]["free"]]
    check("the day's free times for a 90-minute meeting: 12:00-15:00 and 16:00-20:00",
          free == [(at(12), at(15)), (at(16), at(20))], detail=str(free))
    r = await busy(handler(day, free_day_from_hour=7, free_day_until_hour=18),
                   {"title": "X", "start": at(11), "end": at(12)})
    free = [(w["start"], w["end"]) for w in r["data"]["free"]]
    check("the day's edges are settings",
          free == [(at(7), at(9)), (at(10), at(11)), (at(12), at(15)), (at(16), at(18))],
          detail=str(free))
    r = await busy(handler([ev("late", at(21), at(22))]), {"title": "Jantar", "start": at(20, 30),
                                                           "end": at(22)})
    check("a meeting asked for after hours widens the day to hold it",
          r["data"]["conflict"] is True and r["data"]["free"][0] == {"start": at(8), "end": at(21)})

    h = handler(day)
    h._now = lambda: datetime.fromisoformat(at(13, 7))
    r = await busy(h, {"title": "X", "start": at(15), "end": at(16)})
    free = [(w["start"], w["end"]) for w in r["data"]["free"]]
    check("today, the free times start from now, on the next quarter hour",
          free == [(at(13, 15), at(15)), (at(16), at(20))], detail=str(free))
    h._now = lambda: datetime.fromisoformat(at(19, 30))
    r = await busy(h, {"title": "X", "start": at(15), "end": at(16)})
    check("and when too little of the day is left, there are none", r["data"]["free"] == [])


# ============================ P4 — the note, the yes-anyway rule, the floor ===============

def _note_state(*msgs, from_me=True, owner_replies=None) -> dict:
    return {"messages": list(msgs), "from_me": from_me, "owner_replies": owner_replies or []}


async def p4_note():
    print("\nP4 — what the model is told, and what the code says for it")
    data = {"checked": True, "conflict": True, "start": at(11), "end": at(12, 30), "minutes": 90,
            "free": [{"start": at(12, 30), "end": at(15)}, {"start": at(16), "end": at(20)}]}
    note = busy_note("Marcelo", data)
    check("the note says he is not free, and when",
          "Marcelo is not free then" in note and "Wednesday, 2031-03-12, 11:00 to 12:30" in note)
    check("the note lists the free times, for her eyes only",
          "12:30 to 15:00, 16:00 to 20:00" in note and "never to read out" in note)
    check("the note says no card was shown and that she is not told what it is",
          "No card was shown" in note and "not told what the other thing is" in note)
    none = busy_note("Marcelo", {**data, "free": []})
    check("with no free time that fits, the note says so and lists nothing",
          "no other free time that day" in none and " to 15:00" not in none)

    slot = f"{at(11)}/{at(12, 30)}"
    n = {"role": "user", "content": f"[calendar.create not proposed] {note}"}
    hers = {"role": "assistant", "content": LINE_PT}
    his = {"role": "user", "content": "Marcelo: marca assim mesmo"}
    check("anyway: told, and he is speaking now -> the card may go out",
          _told_and_answered(_note_state(n, hers, his), slot))
    check("anyway: the note alone is not him being told (the model is only insisting)",
          not _told_and_answered(_note_state(n), slot))
    check("anyway: someone else speaking is not his decision",
          not _told_and_answered(_note_state(n, hers, his, from_me=False), slot))
    check("anyway: his message ingested with someone else's counts (owner_replies)",
          _told_and_answered(_note_state(n, hers, his, from_me=False, owner_replies=["marca"]), slot))
    check("anyway: a different time is a different question",
          not _told_and_answered(_note_state(n, hers, his), f"{at(14)}/{at(15, 30)}"))

    s_on = Settings(evolution_apikey="x", busy_check_enabled=True)
    silent = {"message": None, "actions": [], "call": None, "state": "keep_listening", "lang": "pt"}
    out = _say_he_is_busy(silent, {"messages": [n], "session_lang": "pt"}, s_on)
    check("the model says nothing after the note -> the line is said for it, and she waits",
          out["message"] == LINE_PT and out["state"] == "awaiting_answer")
    check("she already answered the note -> her silence later is hers",
          _say_he_is_busy(silent, {"messages": [n, hers, his]}, s_on) is silent)
    spoke = {**silent, "message": "Marcelo, você já tem outro compromisso. 14h funciona?"}
    check("the model's own words stand", _say_he_is_busy(spoke, {"messages": [n]}, s_on) is spoke)
    acts = {**silent, "actions": [create(14)]}
    check("a new time proposed in place of the old one is not silence",
          _say_he_is_busy(acts, {"messages": [n]}, s_on) is acts)
    check("with the check off the floor does nothing",
          _say_he_is_busy(silent, {"messages": [n]}, Settings(evolution_apikey="x")) is silent)
    check("the line, in his three languages and a fallback",
          busy_line("Marcelo", "pt") == LINE_PT
          and busy_line("Marcelo", "en") == "Marcelo, there is another event conflicting with that time slot."
          and busy_line("Marcelo", "es-MX").startswith("Marcelo, ya tienes")
          and busy_line("Marcelo", "fr") == busy_line("Marcelo", "en"))

    # The check itself, outside the graph.
    h = handler([ev("B1", at(11), at(12))])
    ctx = {"settings": s_on, "handler": h}
    found = await calendar_busy_check(create(11), {"session_lang": "pt"}, ctx)
    check("check: a taken hour is held back, with a note and the line",
          found["hold"] is True and found["say"] == LINE_PT and SECRET not in found["note"])
    check("check: the trace carries times and a count, never a title",
          found["trace"] == {"read": "ok", "start": at(11), "end": at(11, 45), "conflict": True,
                             "free": 2}, detail=str(found["trace"]))
    found = await calendar_busy_check(create(14), {}, ctx)
    check("check: a free hour is not held", found["hold"] is False and found["trace"]["conflict"] is False)
    off = await calendar_busy_check(create(11), {}, {"settings": Settings(evolution_apikey="x"),
                                                     "handler": h})
    check("check: switched off, it does not look", off is None)
    found = await calendar_busy_check({"task": "calendar.delete", "event_id": "B1"}, {}, ctx)
    check("check: a cancellation is never looked at", found is None)
    found = await calendar_busy_check(
        create(11), {}, {"settings": s_on, "handler": handler(raise_on={"list": _Err(500)})})
    check("check: a failed read holds nothing back",
          found["hold"] is False and found["trace"]["read"] == "failed")


# ============================ P5 — off is the pre-feature build ===========================

def p5_gating():
    print("\nP5 — BUSY_CHECK_ENABLED=false is the build before it")
    off, on = Settings(evolution_apikey="x"), Settings(evolution_apikey="x", busy_check_enabled=True)
    p_off, p_on = system_prompt_for("calendar", off), system_prompt_for("calendar", on)
    check("off: the prompt says nothing about a busy check", "WHEN HE IS NOT FREE" not in p_off)
    check("on: the prompt carries the rules, owner named, no stray braces",
          "WHEN HE IS NOT FREE" in p_on and "Marcelo, você já tem outro compromisso" in p_on
          and "{owner_name}" not in p_on)
    check("on: the rules sit in the part of the prompt that repeats on every call",
          p_on.index("WHEN HE IS NOT FREE") < p_on.stable_ends[0])
    check("on: the rest of the prompt is the same text", p_on.replace(
        p_on[p_on.index("\nWHEN HE IS NOT FREE"):p_on.stable_ends[0]], "")[:p_off.stable_ends[0]]
        == p_off[:p_off.stable_ends[0]])
    s_off, s_on = output_schema_for("calendar", settings=off), output_schema_for("calendar", settings=on)
    check("the model's schema is the same either way (the read is not a verb of hers)",
          s_off == s_on and "calendar.busy" not in repr(s_on))
    check("so the schema caps are untouched",
          count_unions(s_on) <= 16 and count_optionals(s_on) <= 24,
          detail=f"{count_unions(s_on)}/16 unions, {count_optionals(s_on)}/24 optionals")

    from app.review.prompt import build_judge_prompt
    j_off, j_on = build_judge_prompt("Marcelo", "@lisa"), build_judge_prompt("Marcelo", "@lisa",
                                                                           busy_check=True)
    check("the judge is told it is deliberate only when it is on",
          "take her word that he is busy" in j_on and "take her word" not in j_off)


# ============================ P6 — through the graph ======================================

SUGGEST = f"{LINE_PT}\n12:30 funciona para vocês?"
ASK = {"lang": "pt", "message": None}


async def p6_graph():
    print("\nP6 — through the graph")
    taken = [ev("B1", at(11), at(12))]

    # --- off: the card goes out for a taken hour, exactly as it did ----------------------
    g, evo, stub, graph = build(taken, on=False)
    stub.script = [{**ASK, "actions": [create(11, end=at(12, 30))]}]
    st = await turn(graph, "@mary agendar aqui em casa, 11 as 12:30")
    check("[off] the card goes out, one model call, his calendar is not read",
          "Confirmando" in last(evo) and len(stub.calls) == 1 and g.n("list") == 0
          and (st.get("pending_action") or {}).get("start") == at(11))

    # --- on, free: nothing changes but one read ------------------------------------------
    g, evo, stub, graph = build(taken)
    stub.script = [{**ASK, "actions": [create(14, end=at(15, 30))]}]
    st = await turn(graph, "@mary agendar aqui em casa, 14 as 15:30")
    check("[free] the card goes out on one model call, after one read",
          "Confirmando" in last(evo) and "Posso agendar?" in last(evo) and len(stub.calls) == 1
          and len(g.busy_reads()) == 1 and (st.get("pending_action") or {}).get("start") == at(14))

    # --- on, taken: the case from the chat ------------------------------------------------
    g, evo, stub, graph = build(taken)
    stub.script = [
        {**ASK, "actions": [create(11, end=at(12, 30), location="Rua A 253")]},
        {"lang": "pt", "state": "awaiting_answer", "message": SUGGEST, "actions": [],
         "workflow": {"task": "calendar.create", "known_inputs": [],
                      "open_questions": [{"field": "start", "reason": "11:00 is taken"}]}},
    ]
    st = await turn(graph, "@mary agendar aqui em casa, 11 as 12:30")
    check("[taken] no card", "Confirmando" not in last(evo) and "Posso agendar" not in last(evo))
    check("[taken] she says he is not free, and offers one time", SUGGEST in last(evo))
    check("[taken] exactly one message went out", len(evo.sent) == 1)
    check("[taken] nothing is held for a yes",
          st.get("pending_action") is None and st.get("pending_card") is None)
    check("[taken] the model was asked twice: the booking, then the note", len(stub.calls) == 2)
    saw = seen_by_model(stub)
    check("[taken] the note reached the model, with the free times",
          "[calendar.create not proposed] Marcelo is not free then" in saw
          and "12:00 to 20:00" in saw, detail=saw[-260:])
    check("[taken] nothing the model was handed names the other event",
          SECRET not in saw and PLACE not in saw and "canary" not in saw.lower())
    check("[taken] nothing sent to the chat names it either",
          all(SECRET not in t and PLACE not in t for _, t in evo.sent))
    check("[taken] she is waiting for an answer", st.get("llm_state") == "awaiting_answer")
    check("[taken] nothing was written to his calendar", g.n("insert") == 0)

    # The other person answers her: a new time, checked, and the card is Marcelo's to approve.
    stub.script = [{**ASK, "actions": [create(12, 30, end=at(14), location="Rua A 253")]}]
    st = await turn(graph, "12:30 pra mim ok", mine=False)
    check("[settle] the partner's answer reaches her and she proposes the new time",
          len(stub.calls) == 3 and (st.get("pending_action") or {}).get("start") == at(12, 30))
    check("[settle] the new time was read too, and its card went out",
          len(g.busy_reads()) == 2 and "Confirmando" in last(evo) and "12:30" in last(evo))
    st = await turn(graph, "sim", mine=False)
    check("[settle] the partner's yes books nothing", g.n("insert") == 0
          and st.get("pending_action") is not None)
    st = await turn(graph, "sim")
    body = next((kw["body"] for v, kw in g._events.calls if v == "insert"), {})
    check("[settle] his yes books it, at the new time, with no further model call",
          g.n("insert") == 1 and len(stub.calls) == 3
          and body.get("start", {}).get("dateTime") == at(12, 30)
          and body.get("end", {}).get("dateTime") == at(14))
    check("[settle] the result card went out", "Agendado" in last(evo))

    # --- he says to book it anyway --------------------------------------------------------
    g, evo, stub, graph = build(taken)
    stub.script = [{**ASK, "actions": [create(11)]},
                   {"lang": "pt", "state": "awaiting_answer", "message": LINE_PT}]
    await turn(graph, "@mary agendar 11h")
    stub.script = [{**ASK, "actions": [create(11)]}]
    st = await turn(graph, "marca assim mesmo")
    check("[anyway] the same time, on his word, gets its card",
          "Confirmando" in last(evo) and (st.get("pending_action") or {}).get("start") == at(11))
    await turn(graph, "sim")
    check("[anyway] and his yes books it", g.n("insert") == 1)

    # --- the partner insisting is not him deciding -----------------------------------------
    g, evo, stub, graph = build(taken)
    stub.script = [{**ASK, "actions": [create(11)]},
                   {"lang": "pt", "state": "awaiting_answer", "message": LINE_PT}]
    await turn(graph, "@mary agendar 11h")
    stub.script = [{**ASK, "actions": [create(11)]},
                   {"lang": "pt", "message": "Marcos, o Marcelo não está livre às 11h."}]
    st = await turn(graph, "só posso às 11h", mine=False)
    check("[partner insists] the same time is held back again, no card",
          "Confirmando" not in last(evo) and st.get("pending_action") is None
          and "não está livre" in last(evo))

    # --- a fresh tag is a fresh question ---------------------------------------------------
    stub.script = [{**ASK, "actions": [create(11)]},
                   {"lang": "pt", "state": "awaiting_answer", "message": LINE_PT}]
    st = await turn(graph, "@mary agendar 11h")
    check("[new tag] told in an earlier loop does not carry over: held back again",
          last(evo).endswith(LINE_PT) and st.get("pending_action") is None)

    # --- the model says nothing: the line is said for it -----------------------------------
    g, evo, stub, graph = build(taken)
    stub.script = [{**ASK, "actions": [create(11)]}, {"lang": "pt", "message": None}]
    st = await turn(graph, "@mary agendar 11h")
    check("[silent model] he is still told, in his language",
          last(evo).endswith(LINE_PT) and len(evo.sent) == 1)
    check("[silent model] and she waits for an answer", st.get("llm_state") == "awaiting_answer")

    # --- the model keeps sending the same time: bounded, and he is still told ---------------
    g, evo, stub, graph = build(taken, max_tool_actions=2)
    stub.script = [{**ASK, "actions": [create(11)]}] * 6
    st = await turn(graph, "@mary agendar 11h")
    check("[stubborn model] the loop stops at the read-back bound", len(stub.calls) == 3,
          detail=f"{len(stub.calls)} calls")
    check("[stubborn model] one message, the line, and no card",
          len(evo.sent) == 1 and last(evo).endswith(LINE_PT) and st.get("pending_action") is None)

    # --- she picked the hour herself inside a window: a free one goes out, unannounced ------
    g, evo, stub, graph = build(taken)
    stub.script = [{**ASK, "actions": [create(11)]}, {**ASK, "actions": [create(14)]}]
    st = await turn(graph, "@mary agendar quarta de manhã ou à tarde")
    check("[her own pick] the second time is free: its card goes out, nothing announced",
          len(evo.sent) == 1 and "Confirmando" in last(evo)
          and (st.get("pending_action") or {}).get("start") == at(14))

    # --- Google does not answer: the card goes out as before ------------------------------
    g, evo, stub, graph = build(taken, raise_on={"list": _Err(403)})
    stub.script = [{**ASK, "actions": [create(11)]}]
    st = await turn(graph, "@mary agendar 11h")
    check("[read fails] the booking is not lost: the card goes out, one model call",
          "Confirmando" in last(evo) and len(stub.calls) == 1
          and (st.get("pending_action") or {}).get("start") == at(11))

    # --- a card is standing, and he moves it onto a taken hour ----------------------------
    g, evo, stub, graph = build(taken)
    stub.script = [{**ASK, "actions": [create(14)]}]
    await turn(graph, "@mary agendar 14h")
    stub.script = [{**ASK, "actions": [create(11)]},
                   {"lang": "pt", "state": "awaiting_answer", "message": LINE_PT}]
    st = await turn(graph, "na verdade muda pra 11h")
    check("[standing card] the old card is withdrawn with the time he moved off",
          st.get("pending_action") is None and st.get("pending_card") is None
          and st.get("last_confirm_sig") is None)
    stub.script = [{"lang": "pt", "message": None}]
    await turn(graph, "sim")
    check("[standing card] a yes after that books nothing", g.n("insert") == 0)
    stub.script = [{**ASK, "actions": [create(14)]}]
    st = await turn(graph, "deixa às 14h então")
    check("[standing card] going back to 14h shows its card again (not swallowed as a repeat)",
          "Confirmando" in last(evo) and (st.get("pending_action") or {}).get("start") == at(14))

    # --- his preset length is what is checked ---------------------------------------------
    p = Presets(default_minutes=45)
    await p.set_many({MEETING_MINUTES: 90})
    g, evo, stub, graph = build([ev("B1", at(12), at(13))], presets=p, presets_enabled=True)
    stub.script = [{**ASK, "actions": [create(11)]},
                   {"lang": "pt", "state": "awaiting_answer", "message": LINE_PT}]
    st = await turn(graph, "@mary agendar 11h")
    check("[presets] 11:00 for his 90 minutes runs into a 12:00 meeting: held back",
          last(evo).endswith(LINE_PT) and st.get("pending_action") is None)

    # --- an update that moves an event ----------------------------------------------------
    cal = [ev("E1", at(15), at(16)), ev("B1", at(11), at(12))]
    g, evo, stub, graph = build(cal)
    stub.script = [
        {**ASK, "actions": [{"task": "calendar.find", "query": "jantar",
                             "time_min": at(0), "time_max": at(23)}]},
        {**ASK, "actions": [{"task": "calendar.update", "event_id": "E1", "start": at(10, 30)}]},
        {"lang": "pt", "state": "awaiting_answer", "message": LINE_PT},
    ]
    st = await turn(graph, "@mary remarca a reunião das 15h para 10:30")
    check("[update] moved onto a taken hour: no card, he is told, nothing patched",
          last(evo).endswith(LINE_PT) and st.get("pending_action") is None and g.n("patch") == 0)
    check("[update] the note is an update's",
          "[calendar.update not proposed]" in seen_by_model(stub))
    stub.script = [{**ASK, "actions": [{"task": "calendar.update", "event_id": "E1",
                                        "start": at(15, 30)}]}]
    st = await turn(graph, "então 15:30")
    check("[update] moved within its own hour: not a clash with itself, the card goes out",
          "Confirmando alteração" in last(evo)
          and (st.get("pending_action") or {}).get("start") == at(15, 30))


async def main() -> int:
    await p1_read()
    await p2_span()
    await p3_free()
    await p4_note()
    p5_gating()
    await p6_graph()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
