"""Every weekday Lisa writes is checked against its date, and a moved event's card tells the truth.

On Tuesday 2026-11-24 a group agreed on "quinta 15hrs" (Thursday the 26th). Lisa's reasoning said
"Thursday (2026-11-27)", her question said "quinta, 27/11", and the meeting went in on Friday the
27th. Asked to fix it, she moved it to the 26th, and the change card said "Os convidados não serão
avisados" when Google had in fact emailed them: `update` never said whether it notified anyone.

This file holds:
  1. the weekday check itself (skills/weekday_check.py): actions and message text;
  2. the reason node sending her back once with the note, and taking her second answer;
  3. `weekday` never reaching the calendar tool, and the schema caps still met;
  4. the update result saying whether guests were emailed, and the card saying the same.

No network, no Anthropic key.

    python tests/run_weekday.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import Settings  # noqa: E402
from app.nodes.reason import reason_node  # noqa: E402
from app.skills import SKILLS, output_schema_for  # noqa: E402
from app.skills.base import count_optionals, count_unions  # noqa: E402
from app.skills.calendar_format import fmt_update  # noqa: E402
from app.skills.weekday_check import correction, mismatches, strip_weekday  # noqa: E402
from app.tools.calendar import GoogleCalendarService  # noqa: E402
from app.trace import build_trace  # noqa: E402
from run_step3 import StubReasoner  # noqa: E402

_checks = {"pass": 0, "fail": 0}
TODAY = date(2026, 11, 24)  # a Tuesday


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def create(start: str, weekday: str | None, **kw) -> dict:
    a = {"task": "calendar.create", "title": "Acme", "start": start, **kw}
    if weekday is not None:
        a["weekday"] = weekday
    return a


def test_check() -> None:
    print("weekday check")
    # The incident, as she sent it.
    bad = {"actions": [create("2026-11-27T15:00:00-03:00", "Thursday")]}
    found = mismatches(bad, TODAY)
    check("Thursday with 2026-11-27 is caught", len(found) == 1 and found[0]["date"] == date(2026, 11, 27))
    note = correction(found)
    check("the note names the real weekday", "2026-11-27 is a Friday" in note, note)
    check("and the Thursday of that week", "Thursday of that week is 2026-11-26" in note, note)
    check("the right pair passes",
          not mismatches({"actions": [create("2026-11-26T15:00:00-03:00", "Thursday")]}, TODAY))
    check("an all-day date is read too",
          len(mismatches({"actions": [create("2026-11-27", "Thursday", all_day=True)]}, TODAY)) == 1)
    check("no weekday, no check", not mismatches({"actions": [create("2026-11-27T15:00:00-03:00", None)]}, TODAY))
    check("an update that does not move the event is not checked",
          not mismatches({"actions": [{"task": "calendar.update", "event_id": "E1",
                                       "weekday": "Monday", "location": "x"}]}, TODAY))
    check("an update that moves it is",
          len(mismatches({"actions": [{"task": "calendar.update", "event_id": "E1", "weekday": "Thursday",
                                       "start": "2026-11-27T15:00:00-03:00"}]}, TODAY)) == 1)
    check("other verbs are left alone",
          not mismatches({"actions": [{"task": "calendar.find", "time_min": "2026-11-27"}]}, TODAY))

    print("weekday check — her message")
    msg = lambda m: mismatches({"message": m, "actions": []}, TODAY)
    check("'quinta, 27/11' (her question that day) is caught", len(msg("Vou agendar quinta, 27/11, às 15h.")) == 1)
    check("'quinta, 26/11' passes", not msg("Vou agendar quinta, 26/11, às 15h."))
    check("'quinta-feira (27/11/2026)' is caught", len(msg("Fica quinta-feira (27/11/2026)?")) == 1)
    check("'Terça (hoje)' style without a date is ignored", not msg("Terça, às 13h, pode ser?"))
    check("'sexta 27 de novembro' passes", not msg("Pode ser sexta 27 de novembro?"))
    check("'jueves 27 de noviembre' is caught", len(msg("¿El jueves 27 de noviembre?")) == 1)
    check("'Thursday, Nov 27' is caught", len(msg("Booking Thursday, Nov 27 at 3pm.")) == 1)
    check("'Thursday 11/26' (month first) passes", not msg("Booking Thursday 11/26 at 3pm."))
    check("accents do not matter: 'terça 24/11' passes", not msg("Hoje, terça 24/11."))
    check("a weekday and a date apart are not paired",
          not msg("Na quinta eu vejo. O evento de 27/11 continua."))
    check("a year boundary reads the nearest year",
          not mismatches({"message": "Sexta, 01/01", "actions": []}, date(2026, 12, 28)))

    print("strip")
    stripped = strip_weekday(bad)
    check("weekday is taken off every action", "weekday" not in stripped["actions"][0])
    check("the rest of the action stays", stripped["actions"][0]["start"] == "2026-11-27T15:00:00-03:00")
    plain = {"actions": [create("2026-11-26T15:00:00-03:00", None)]}
    check("nothing to strip returns the same object", strip_weekday(plain) is plain)


def _state() -> dict:
    return {"trace_id": "t-weekday", "loop_id": "loop-weekday", "domain": "calendar",
            "remote_jid": "120363000000000001@g.us", "is_group": True, "from_me": True,
            "messages": [{"role": "user", "content": "[Marcelo] Ok então quinta 15hrs!\n"
                                                     "[Marcelo] @Lisa agendar."}]}


async def test_reason_node() -> None:
    print("reason node")
    s = Settings()
    good = create("2026-11-26T15:00:00-03:00", "Thursday")
    # The check is pinned to the incident's day: today's real date would move the weekdays.
    import app.prompt as prompt_mod
    real_today_in = prompt_mod.today_in
    prompt_mod.today_in = lambda tz: TODAY.isoformat()
    try:
        r = StubReasoner()
        r.script = [{"actions": [create("2026-11-27T15:00:00-03:00", "Thursday")],
                     "reasoning": "Thursday (2026-11-27)", "usage": {"input": 10, "output": 2}},
                    {"actions": [good], "usage": {"input": 12, "output": 3}}]
        out = await reason_node(_state(), reasoner=r, settings=s, trace=build_trace())
        check("a wrong pair is sent back once", len(r.calls) == 2, str(len(r.calls)))
        last = r.calls[1]["messages"][-1]
        check("the note is the last message, from the user side",
              last["role"] == "user" and "2026-11-27 is a Friday" in last["content"], str(last)[:200])
        check("the first answer is shown to her before it",
              r.calls[1]["messages"][-2]["role"] == "assistant"
              and "2026-11-27" in r.calls[1]["messages"][-2]["content"])
        check("her second answer is the one used",
              out["actions"] and out["actions"][0]["start"] == "2026-11-26T15:00:00-03:00", str(out["actions"]))
        check("and it carries no weekday", "weekday" not in out["actions"][0])
        check("both calls' tokens are counted", out["usage"].get("input") == 22, str(out["usage"]))

        r = StubReasoner()
        r.script = [{"actions": [good]}]
        out = await reason_node(_state(), reasoner=r, settings=s, trace=build_trace())
        check("a right pair costs no second call", len(r.calls) == 1)
        check("and its weekday is still dropped", "weekday" not in out["actions"][0])

        r = StubReasoner()
        wrong = create("2026-11-27T15:00:00-03:00", "Thursday")
        r.script = [{"actions": [wrong]}, {"actions": [dict(wrong)]}]
        out = await reason_node(_state(), reasoner=r, settings=s, trace=build_trace())
        check("only one retry, even when the second is wrong too", len(r.calls) == 2)
        check("the second answer still goes ahead (the card shows the real weekday)",
              out["actions"][0]["start"].startswith("2026-11-27") and "weekday" not in out["actions"][0])

        r = StubReasoner()
        r.script = [{"actions": [wrong]}, {"error_category": "provider", "stop_reason": "error",
                                           "actions": []}]
        out = await reason_node(_state(), reasoner=r, settings=s, trace=build_trace())
        check("a failed retry keeps the first answer, not silence",
              out["actions"] and out["actions"][0]["start"].startswith("2026-11-27"))

        r = StubReasoner()
        r.script = [{"message": "Vou agendar quinta, 27/11, às 15h. Qual o email do Paulo?",
                     "state": "awaiting_answer"},
                    {"message": "Vou agendar quinta, 26/11, às 15h. Qual o email do Paulo?",
                     "state": "awaiting_answer"}]
        out = await reason_node(_state(), reasoner=r, settings=s, trace=build_trace())
        check("a wrong pair in a question is sent back too", len(r.calls) == 2)
        check("and the corrected question is what goes out", "26/11" in (out["reply_body"] or ""))
    finally:
        prompt_mod.today_in = real_today_in


def test_schema() -> None:
    print("schema")
    s = Settings()
    sch = output_schema_for("calendar", settings=s)
    check("optionals still within Anthropic's 24", count_optionals(sch) <= 24, str(count_optionals(sch)))
    check("unions still within 16", count_unions(sch) <= 16, str(count_unions(sch)))
    branches = sch["properties"]["actions"]["items"]["anyOf"]
    req = {b["properties"]["task"]["const"]: b.get("required", []) for b in branches}
    check("create requires weekday", "weekday" in req.get("calendar.create", []))
    check("update requires weekday", "weekday" in req.get("calendar.update", []))
    check("the calendar skill has the recheck", SKILLS["calendar"].recheck is not None)


async def test_update_notice() -> None:
    print("update card — were the guests emailed?")
    calls = []
    event = {"id": "E1", "summary": "Acme", "location": "Rua Exemplo 100",
             "start": {"dateTime": "2026-11-27T15:00:00-03:00"},
             "end": {"dateTime": "2026-11-27T15:30:00-03:00"},
             "attendees": [{"email": "a@example.com"}, {"email": "b@example.com"}]}

    class Ev:
        def get(self, **kw):
            class R:
                def execute(s_): return dict(event)
            return R()

        def patch(self, **kw):
            calls.append(kw)

            class R:
                def execute(s_):
                    return {**event, "start": {"dateTime": "2026-11-26T15:00:00-03:00"},
                            "end": {"dateTime": "2026-11-26T15:30:00-03:00"}}
            return R()

    class Svc:
        def events(self): return Ev()

    g = GoogleCalendarService(Settings())
    g._svc = Svc()
    r = await g.run("update", {"event_id": "E1", "start": "2026-11-26T15:00:00-03:00"})
    check("a plain move emails the guests", calls[-1]["sendUpdates"] == "all")
    check("and the result says so", (r.get("data") or {}).get("notified") is True, str(r)[:300])
    card = fmt_update([r], {"session_lang": "pt"})
    check("the card says they were told", "Os convidados foram avisados." in card, card)
    check("and not that they won't be", "não serão avisados" not in card, card)

    r = await g.run("update", {"event_id": "E1", "start": "2026-11-26T15:00:00-03:00",
                               "send_invites": False})
    check("a quiet move emails nobody", calls[-1]["sendUpdates"] == "none")
    card = fmt_update([r], {"session_lang": "pt"})
    check("and its card says so", "Os convidados não serão avisados." in card, card)


async def main() -> None:
    test_check()
    await test_reason_node()
    test_schema()
    await test_update_notice()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
