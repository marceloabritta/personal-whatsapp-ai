"""A time said on another clock is shown on both clocks, on every card.

On 2026-11-26 a contact wrote "Wednesday the 9th at 8am PT" and the owner told Lisa to book it,
minding the zone. The event went in right, and the card read "09/Dec - Wednesday, 13:00": his own
clock, unnamed, under a chat that only ever said 8am. The next day a coffee in San Francisco,
"10am quarta", got "02/dez - Quarta, 10:00 AM": the other clock, unnamed too. He had to write
"10am na hora local de San Francisco" before saying yes.

This file holds:
  1. the zone check (skills/timezone_check.py): the offset in "start" against the zone she named,
     the note that sends her back, and what is left on the action afterwards;
  2. what a zone is called on a card (zones.py);
  3. the cards: booking, change and cancellation, in pt / en / es, and a booking on his own clock
     reading exactly as it always did;
  4. the reason node sending her back once and taking her second answer;
  5. the calendar tool storing the event in that zone and reading it back from Google;
  6. the whole way through the graph: the card that goes out, and what Google is sent on his yes;
  7. the schema caps.

No network, no Anthropic key.

    python tests/run_timezone.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import Settings  # noqa: E402
from app.nodes.reason import reason_node  # noqa: E402
from app.skills import SKILLS, output_schema_for, system_prompt_for  # noqa: E402
from app.skills.base import count_optionals, count_unions  # noqa: E402
from app.skills.calendar import calendar_recheck  # noqa: E402
from app.skills.calendar_format import (  # noqa: E402
    compose_create, compose_delete, compose_update, fmt_delete,
)
from app.skills.timezone_check import correction, mismatches, settle  # noqa: E402
from app.trace import build_trace  # noqa: E402
from app.zones import zone_key, zone_label, zone_of  # noqa: E402
from run_busy import _Req, build, handler, last, turn  # noqa: E402
from run_step3 import StubReasoner  # noqa: E402

_checks = {"pass": 0, "fail": 0}
HOME = "America/Sao_Paulo"
LA = "America/Los_Angeles"
TAG = Settings().primary_tag          # the tag the default settings answer to


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def create(start: str, zone: str | None, **kw) -> dict:
    a = {"task": "calendar.create", "title": "Marcelo & Acme", "start": start, **kw}
    if zone is not None:
        a["timezone"] = zone
    return a


def state(lang: str = "pt", **kw) -> dict:
    return {"session_lang": lang, "owner_today": "2026-11-27", "owner_tz": HOME, **kw}


def test_check() -> None:
    print("the zone check")
    ok = {"actions": [create("2026-12-09T08:00:00-08:00", LA)]}
    check("8am with -08:00 in Los Angeles in December agrees", not mismatches(ok))
    # The 2026-11-26 booking as she wrote it: the hour already moved to his clock.
    moved = {"actions": [create("2026-12-09T13:00:00-03:00", LA)]}
    found = mismatches(moved)
    check("an hour already converted to his clock is caught",
          len(found) == 1 and found[0]["kind"] == "offset" and found[0]["offset"] == "-08:00",
          str(found))
    note = correction(found)
    check("the note says what the zone's offset is that day",
          "America/Los_Angeles is UTC-08:00" in note and "Do not convert" in note, note)
    summer = mismatches({"actions": [create("2026-12-09T08:00:00-07:00", LA)]})
    check("the summer offset in winter is caught", len(summer) == 1 and summer[0]["offset"] == "-08:00")
    check("and the summer offset in summer agrees",
          not mismatches({"actions": [create("2026-07-09T08:00:00-07:00", LA)]}))
    check("his own zone with another zone's offset is caught",
          len(mismatches({"actions": [create("2026-12-02T10:00:00-08:00", HOME)]})) == 1)
    bad = mismatches({"actions": [create("2026-12-09T08:00:00-08:00", "Pacific Time")]})
    check("a name that is no zone is caught", len(bad) == 1 and bad[0]["kind"] == "unknown")
    check("and the note asks for an IANA name", "not an IANA time zone name" in correction(bad))
    check("an all-day event is not checked",
          not mismatches({"actions": [create("2026-12-09", LA, all_day=True)]}))
    check("no timezone, no check", not mismatches({"actions": [create("2026-12-09T08:00:00-08:00", None)]}))
    check("an update that does not move the event is not checked",
          not mismatches({"actions": [{"task": "calendar.update", "event_id": "E1",
                                       "timezone": LA, "location": "x"}]}))
    check("an update that moves it is",
          len(mismatches({"actions": [{"task": "calendar.update", "event_id": "E1", "timezone": LA,
                                       "start": "2026-12-09T13:00:00-03:00"}]})) == 1)
    check("a start with no offset is left for settle",
          not mismatches({"actions": [create("2026-12-09T08:00:00", LA)]}))
    check("other verbs are left alone",
          not mismatches({"actions": [{"task": "calendar.find", "timezone": "x"}]}))

    print("what is left on the action")
    kept = settle(ok, HOME)["actions"][0]
    check("another clock keeps its zone", kept.get("timezone") == LA and kept["start"].endswith("-08:00"))
    home = settle({"actions": [create("2026-12-09T08:00:00-03:00", HOME)]}, HOME)["actions"][0]
    check("his own clock loses the field", "timezone" not in home and home["start"].endswith("-03:00"))
    same = settle({"actions": [create("2026-12-09T08:00:00-03:00",
                                      "America/Argentina/Buenos_Aires")]}, HOME)["actions"][0]
    check("another zone on the same clock loses it too", "timezone" not in same)
    alias = settle({"actions": [create("2026-12-09T08:00:00-08:00", "US/Pacific")]}, HOME)["actions"][0]
    check("an old name becomes the IANA one", alias.get("timezone") == LA, str(alias))
    naive = settle({"actions": [create("2026-12-09T08:00:00", LA)]}, HOME)["actions"][0]
    check("a start with no offset gets the zone's",
          naive["start"] == "2026-12-09T08:00:00-08:00" and naive.get("timezone") == LA, str(naive))
    junk = settle({"actions": [create("2026-12-09T08:00:00-08:00", "Pacific Time")]}, HOME)["actions"][0]
    check("a name that is no zone is dropped, the time untouched",
          "timezone" not in junk and junk["start"] == "2026-12-09T08:00:00-08:00")
    allday = settle({"actions": [create("2026-12-09", LA, all_day=True)]}, HOME)["actions"][0]
    check("an all-day event carries no zone", "timezone" not in allday)
    still = settle({"actions": [{"task": "calendar.update", "event_id": "E1", "timezone": LA,
                                 "attendees": []}]}, HOME)["actions"][0]
    check("an update that does not move the event carries none", "timezone" not in still)
    plain = {"actions": [create("2026-12-09T08:00:00-03:00", None)]}
    check("nothing to settle returns the same object", settle(plain, HOME) is plain)

    print("recheck: both checks, one note")
    s = Settings()
    cleaned, note = calendar_recheck({"actions": [create("2026-12-09T13:00:00-03:00", LA,
                                                         weekday="Wednesday")]}, {}, s)
    check("a zone mismatch sends her back", bool(note) and "time zone" in note)
    check("weekday is off, the zone is on", "weekday" not in cleaned["actions"][0]
          and cleaned["actions"][0].get("timezone") == LA)
    _, both = calendar_recheck({"actions": [create("2026-12-10T13:00:00-03:00", LA,
                                                   weekday="Wednesday")]}, {}, s)
    check("a wrong weekday and a wrong zone are both in the note",
          "weekday" in both and "time zone" in both, both)
    _, none = calendar_recheck({"actions": [create("2026-12-09T08:00:00-08:00", LA,
                                                   weekday="Wednesday")]}, {}, s)
    check("a right pair costs no note", none is None)


def test_labels() -> None:
    print("what a zone is called")
    w = datetime.fromisoformat("2026-12-02T10:00:00-08:00")
    check("Pacific, in his language", zone_label(LA, w, "pt") == "PT (Pacífico)"
          and zone_label(LA, w, "en") == "PT (Pacific)")
    check("his own", zone_label(HOME, w, "pt") == "BRT (Brasília)")
    check("Eastern", zone_label("America/New_York", w, "en") == "ET (Eastern)")
    check("a zone with no table entry: its abbreviation and city",
          zone_label("Europe/Berlin", w, "pt") == "CET (Berlim)", zone_label("Europe/Berlin", w, "pt"))
    check("daylight saving picks the abbreviation",
          zone_label("Europe/Berlin", datetime.fromisoformat("2026-07-02T10:00:00+02:00"), "en")
          == "CEST (Berlin)")
    check("no abbreviation: the distance from GMT",
          zone_label("America/Argentina/Buenos_Aires", w, "es") == "GMT-3 (Buenos Aires)")
    check("no zone at all: the distance of the time itself", zone_label(None, w, "pt") == "GMT-8")
    check("old names still resolve", zone_key("US/Eastern") == "America/New_York"
          and zone_of("US/Pacific") is not None)
    check("nonsense is not a zone", zone_key("Pacific Time") is None and zone_of("") is None)


def test_cards() -> None:
    print("the booking card")
    coffee = create("2026-12-02T10:00:00-08:00", LA, title="Marcelo & Rafael",
                    location="Salesforce Park, San Francisco, CA")
    card = compose_create(coffee, state("pt")).split("\n")
    check("[pt] the time where it was agreed, with its zone",
          card[3] == "02/dez - Quarta, 10:00 AM PT (Pacífico)", repr(card))
    check("[pt] then the same moment on his clock", card[4] == "15:00 BRT (Brasília)", repr(card))
    check("[pt] the place follows", card[5].startswith("Salesforce Park"))
    en = compose_create(create("2026-12-09T08:00:00-08:00", LA), state("en")).split("\n")
    check("[en] both clocks", en[3] == "09/Dec - Wednesday, 08:00 AM PT (Pacific)"
          and en[4] == "13:00 BRT (Brasília)", repr(en))
    es = compose_create(create("2026-12-09T08:00:00-08:00", LA), state("es")).split("\n")
    check("[es] both clocks", es[3] == "09/dic - Miércoles, 08:00 AM PT (Pacífico)"
          and es[4] == "13:00 BRT (Brasilia)", repr(es))
    late = compose_create(create("2026-12-02T21:00:00-08:00", LA), state("pt")).split("\n")
    check("another day on his clock: his line says which",
          late[3] == "02/dez - Quarta, 21:00 PT (Pacífico)"
          and late[4] == "03/dez - Quinta, 02:00 AM BRT (Brasília)", repr(late))
    soon = compose_create(create("2026-11-28T10:00:00-08:00", LA), state("pt")).split("\n")
    check("tomorrow is still named", soon[3] == "28/nov - Sábado (amanhã), 10:00 AM PT (Pacífico)",
          repr(soon))

    home_action = create("2026-12-09T08:00:00-03:00", None)
    check("his own clock: the card he always got, to the byte",
          compose_create(home_action, state("pt"))
          == compose_create(home_action, {"session_lang": "pt", "owner_today": "2026-11-27"})
          and "BRT" not in compose_create(home_action, state("pt")))
    bare = compose_create(create("2026-12-02T10:00:00-08:00", None), state("pt")).split("\n")
    check("another offset with no zone named still shows both clocks",
          bare[3] == "02/dez - Quarta, 10:00 AM GMT-8" and bare[4] == "15:00 BRT (Brasília)",
          repr(bare))
    check("no home zone on the state: one line, as before",
          compose_create(coffee, {"session_lang": "pt"}).split("\n")[3] == "02/dez - Quarta, 10:00 AM")
    check("an all-day event has no clock",
          "BRT" not in compose_create(create("2026-12-02", None, all_day=True), state("pt")))

    print("the change and cancellation cards")
    ev = {"event_id": "E1", "title": "Ana & Marcelo", "start": "2026-12-09T13:00:00-03:00",
          "end": "2026-12-09T13:30:00-03:00", "all_day": False, "attendees": [], "timezone": LA}
    st = state("en", seen_events={"E1": ev})
    moved = compose_update({"task": "calendar.update", "event_id": "E1", "timezone": LA,
                            "start": "2026-12-09T08:30:00-08:00"}, st).split("\n")
    check("the event as it stands, on both clocks",
          moved[3] == "09/Dec - Wednesday, 08:00 AM PT (Pacific)" and moved[4] == "13:00 BRT (Brasília)",
          repr(moved))
    check("the new time, on both clocks",
          moved[6] == "New time: 09/Dec - Wednesday, 08:30 AM PT (Pacific)"
          and moved[7] == "13:30 BRT (Brasília)", repr(moved))
    back = compose_update({"task": "calendar.update", "event_id": "E1",
                           "start": "2026-12-09T15:00:00-03:00"}, st).split("\n")
    check("moved to a time on his own clock: the new time is one line",
          back[6] == "New time: 09/Dec - Wednesday, 15:00" and back[7] == "", repr(back))
    guests = compose_update({"task": "calendar.update", "event_id": "E1",
                             "attendees": ["g@example.com"]}, st)
    check("a change that leaves the time alone still shows both clocks",
          "08:00 AM PT (Pacific)\n13:00 BRT (Brasília)" in guests, guests)
    gone = compose_delete({"task": "calendar.delete", "event_id": "E1"}, st)
    check("the cancellation question shows both clocks",
          "08:00 AM PT (Pacific)\n13:00 BRT (Brasília)" in gone, gone)
    done = fmt_delete([{"ok": True, "data": {**ev, "had_attendees": False}}], state("en"))
    check("and so does the cancelled card", "08:00 AM PT (Pacific)\n13:00 BRT (Brasília)" in done, done)


async def test_reason_node() -> None:
    print("reason node")
    s = Settings()
    st = {"trace_id": "t-zone", "loop_id": "loop-zone", "domain": "calendar",
          "remote_jid": "5511900000001@s.whatsapp.net", "is_group": False, "from_me": True,
          "messages": [{"role": "user", "content": "[Ana] Wednesday the 9th at 8am PT?\n"
                                                   "[Marcelo] @Lisa book this, mind the time zone."}]}
    wrong = create("2026-12-09T13:00:00-03:00", LA, weekday="Wednesday")
    right = create("2026-12-09T08:00:00-08:00", LA, weekday="Wednesday")

    r = StubReasoner()
    r.script = [{"actions": [wrong], "usage": {"input": 10, "output": 2}},
                {"actions": [right], "usage": {"input": 12, "output": 3}}]
    out = await reason_node(dict(st), reasoner=r, settings=s, trace=build_trace())
    check("a converted hour is sent back once", len(r.calls) == 2, str(len(r.calls)))
    note = r.calls[1]["messages"][-1]
    check("the note is the last message, from the user side",
          note["role"] == "user" and "America/Los_Angeles is UTC-08:00" in note["content"],
          str(note)[:200])
    check("her second answer is the one used",
          out["actions"][0]["start"] == "2026-12-09T08:00:00-08:00"
          and out["actions"][0].get("timezone") == LA, str(out["actions"]))
    check("both calls' tokens are counted", out["usage"].get("input") == 22, str(out["usage"]))

    r = StubReasoner()
    r.script = [{"actions": [right]}]
    out = await reason_node(dict(st), reasoner=r, settings=s, trace=build_trace())
    check("a right pair costs no second call", len(r.calls) == 1)
    check("and the zone stays on the action", out["actions"][0].get("timezone") == LA)

    r = StubReasoner()
    r.script = [{"actions": [create("2026-12-09T08:00:00-03:00", HOME, weekday="Wednesday")]}]
    out = await reason_node(dict(st), reasoner=r, settings=s, trace=build_trace())
    check("a booking on his own clock leaves with no zone field",
          len(r.calls) == 1 and "timezone" not in out["actions"][0])

    r = StubReasoner()
    r.script = [{"actions": [wrong]}, {"actions": [dict(wrong)]}]
    out = await reason_node(dict(st), reasoner=r, settings=s, trace=build_trace())
    check("only one retry, even when the second is wrong too", len(r.calls) == 2)
    card = compose_create(out["actions"][0], state("en"))
    check("and its card still shows both clocks, worked out from the moment itself",
          "08:00 AM PT (Pacific)\n13:00 BRT (Brasília)" in card, card)


async def test_tool() -> None:
    print("the calendar tool")
    h = handler()
    r = await h.run("create", {"title": "Marcelo & Acme", "start": "2026-12-09T08:00:00-08:00",
                               "end": "2026-12-09T08:30:00-08:00", "timezone": LA,
                               "location": "Rua Exemplo 100"})
    body = h._svc._events.calls[-1][1]["body"]
    check("the event is stored in the zone the time was said in",
          body["start"] == {"dateTime": "2026-12-09T08:00:00-08:00", "timeZone": LA}
          and body["end"]["timeZone"] == LA, str(body["start"]))
    check("and its view carries that zone", r["ok"] and r["data"].get("timezone") == LA, str(r.get("data")))

    h = handler()
    r = await h.run("create", {"title": "X", "start": "2026-12-09T08:00:00-03:00",
                               "location": "Rua Exemplo 100"})
    body = h._svc._events.calls[-1][1]["body"]
    check("no zone on the action: stored in his own, as before", body["start"]["timeZone"] == HOME)
    check("and its view has no zone field", "timezone" not in r["data"])

    stored = {"id": "E1", "summary": "Ana & Marcelo",
              "start": {"dateTime": "2026-12-09T13:00:00-03:00", "timeZone": LA},
              "end": {"dateTime": "2026-12-09T13:30:00-03:00", "timeZone": LA}}
    utc = {"id": "E2", "summary": "Booked by a tool",
           "start": {"dateTime": "2026-12-09T16:00:00-03:00", "timeZone": "UTC"},
           "end": {"dateTime": "2026-12-09T16:30:00-03:00", "timeZone": "UTC"}}
    h = handler([stored, utc])
    found = await h.run("find", {"query": "Ana"})
    check("an event read back says which zone it was booked in",
          found["data"]["items"][0].get("timezone") == LA)
    check("and the model is told the hour on that clock",
          "Wed 08:00 in America/Los_Angeles" in found["summary"], found["summary"])
    other = await h.run("find", {"query": "Booked by a tool"})
    check("UTC is nobody's clock: no zone, one line",
          "timezone" not in other["data"]["items"][0] and " in UTC" not in other["summary"])

    r = await h.run("update", {"event_id": "E1", "start": "2026-12-09T08:30:00-08:00", "timezone": LA})
    patch = h._svc._events.calls[-1][1]["body"]
    check("a move on that clock keeps the event in its zone",
          patch["start"]["timeZone"] == LA and patch["start"]["dateTime"] == "2026-12-09T08:30:00-08:00",
          str(patch))
    h._svc._events.delete = lambda **kw: _Req({})      # the shared fake has no delete
    r = await h.run("delete", {"event_id": "E1"})
    check("the cancelled event still says its zone", r["data"].get("timezone") == LA, str(r["data"]))


async def test_graph() -> None:
    print("through the graph")
    google, evo, stub, graph = build([], on=True)
    stub.script = [{"lang": "pt", "actions": [create(
        "2026-12-02T10:00:00-08:00", LA, weekday="Wednesday", title="Marcelo & Rafael",
        location="Salesforce Park, San Francisco, CA")]}]
    st = await turn(graph, f"{TAG} agendar. atenção para o time zone, é PT.")
    card = last(evo)
    check("the card that goes out shows both clocks",
          "02/dez - Quarta, 10:00 AM PT (Pacífico)\n15:00 BRT (Brasília)" in card, card)
    check("the held booking carries the zone and no weekday",
          (st.get("pending_action") or {}).get("timezone") == LA
          and "weekday" not in (st.get("pending_action") or {}))
    await turn(graph, "sim")
    inserts = [kw for v, kw in google._events.calls if v == "insert"]
    check("his yes books it in that zone",
          len(inserts) == 1 and inserts[0]["body"]["start"]
          == {"dateTime": "2026-12-02T10:00:00-08:00", "timeZone": LA}, str(inserts))

    google, evo, stub, graph = build([], on=True)
    stub.script = [{"lang": "pt", "actions": [create(
        "2026-12-02T10:00:00-03:00", HOME, weekday="Wednesday", location="Rua Exemplo 100")]}]
    await turn(graph, f"{TAG} agendar quarta 10h na Rua Exemplo 100")
    card = last(evo)
    check("a booking on his clock: one time line, no zone words",
          "02/dez - Quarta, 10:00 AM\nRua Exemplo 100" in card and "BRT" not in card, card)


def test_schema() -> None:
    print("schema and prompt")
    s = Settings()
    sch = output_schema_for("calendar", settings=s)
    check("optionals still within Anthropic's 24", count_optionals(sch) <= 24, str(count_optionals(sch)))
    check("unions still within 16", count_unions(sch) <= 16, str(count_unions(sch)))
    branches = sch["properties"]["actions"]["items"]["anyOf"]
    req = {b["properties"]["task"]["const"]: b.get("required", []) for b in branches}
    check("create requires timezone", "timezone" in req.get("calendar.create", []))
    check("update requires timezone", "timezone" in req.get("calendar.update", []))
    check("the calendar skill has the recheck", SKILLS["calendar"].recheck is not None)
    prompt = str(system_prompt_for("calendar", s))
    check("the prompt names his own zone as the default",
          f"own clock is {s.calendar_timezone}" in prompt)
    check("and leaves no template field unfilled", "{owner_tz}" not in prompt)


def main() -> int:
    test_check()
    test_labels()
    test_cards()
    asyncio.run(test_reason_node())
    asyncio.run(test_tool())
    asyncio.run(test_graph())
    test_schema()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
