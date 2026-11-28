"""Live check — does a time said on another clock reach the card on both clocks, with the real model?

On 2026-11-26 a contact wrote "Wednesday the 9th at 8am PT" and the card read "13:00". The next
day a coffee in San Francisco at "10am" got a card reading "10:00 AM". Neither named a zone. Since
then every create and update carries a `timezone` that code checks against the offset in "start"
(skills/timezone_check.py), and the card shows the time on both clocks.

This replays those chats relative to NOW, with made-up people, through the real graph and the real
model, up to the card:
  - "named": someone names the zone ("8am PT", "3pm ET", "10h no horário de Lisboa");
  - "place": nobody names it, and the meeting is in person in another city;
  - "home": an ordinary booking on his own clock, which must read exactly as it always did;
  - "forced": the model's first answer is replaced by the mistake of 2026-11-26 (the hour already
    moved to his clock), so the send-back runs against the real API and the real model has to
    read the note and correct it.
Every model answer is recorded, so the run also shows Anthropic accepting the schema (any provider
error fails it) and how often the check fired on its own.

A stub calendar and a fake WhatsApp: nothing is written anywhere and nothing is sent to anyone.
Runs on the droplet like the other live checks (tests/live_key.py), billed to the tests key:

    docker run --rm --env-file <(docker exec lisa_brain env) --env-file /opt/evolution/tests.env \\
      -v "<tree>/tests:/app/tests:ro" <image> python tests/live_timezone.py

LIVE_REPEAT (default 5) runs each scenario that many times. LIVE_ONLY=name,name runs just those.
Exits non-zero on any miss.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.graph import build_graph  # noqa: E402
from app.prompt import _WEEKDAYS, now_in  # noqa: E402
from live_partner import GROUP_JID, Chat, held, settings  # noqa: E402
from live_weekday import Spy  # noqa: E402

REPEAT = int(os.environ.get("LIVE_REPEAT", "5"))
ONLY = {n for n in os.environ.get("LIVE_ONLY", "").split(",") if n}
PT = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LA, NY, LISBON = "America/Los_Angeles", "America/New_York", "Europe/Lisbon"
ANA = "ana.pereira@example.com"


def chat(jid: str | None = None, forced: dict | None = None) -> tuple[Chat, Spy]:
    c = Chat([], jid=jid) if jid else Chat([])
    spy = Spy(c.deps.reasoner, forced)
    c.deps.reasoner = spy
    c.graph = build_graph(c.deps, MemorySaver())
    return c, spy


def the_day(offset: int) -> date:
    now, _ = now_in(settings().calendar_timezone)
    return now.date() + timedelta(days=offset)


async def to_card(c: Chat, tag: str) -> dict:
    """Tag her, and answer up to two questions the way he would, until a card is held."""
    st = await c.say(tag)
    for reply in ("isso, pode montar", "sim, isso mesmo, monta o card"):
        if held(st):
            break
        st = await c.say(reply)
    return held(st)


def verdict(p: dict, card: str, day: date, hour: int, zone: str, his_label: str = "BRT") -> tuple[bool, str]:
    """The held booking is that hour on that zone's clock, and the card shows both clocks."""
    want = datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(zone))
    try:
        got = datetime.fromisoformat(str(p.get("start")))
    except ValueError:
        got = None
    right_moment = got is not None and got.tzinfo is not None and got == want
    on_its_clock = right_moment and got.utcoffset() == want.utcoffset()
    zoned = p.get("timezone") == zone
    his = want.astimezone(ZoneInfo(settings().calendar_timezone))
    two = f"{his:%H:%M}" in card and his_label in card
    ok = right_moment and on_its_clock and zoned and two and "weekday" not in p
    return ok, (f"want {want.isoformat()} in {zone}; got start={p.get('start')!r} "
                f"timezone={p.get('timezone')!r}; both clocks on card={two}; card: {card[:220]!r}")


# --- the scenarios ----------------------------------------------------------------------------

async def named_pt() -> tuple[bool, str, Spy]:
    """2026-11-26, one-to-one, in English: "8am PT", and he says to mind the zone."""
    day = the_day(5)
    c, spy = chat()
    c.other("Hi Marcelo, Ana here. It's a pleasure to meet you! Might you be able to do a remote "
            "convo next week?", name="Ana")
    c.me("yes, no problem. do you want to suggest a date and time?")
    c.other(f"Awesome! How about {_WEEKDAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day} "
            f"at 8am PT?", name="Ana")
    c.me("ok! what is your email?")
    c.other(f"{ANA} - thanks Marcelo!", name="Ana")
    p = await to_card(c, "@lisa book this. title Ana & Marcelo. Google meet. Be mindful of the "
                         "timezone, its PT.")
    ok, detail = verdict(p, c.last(), day, 8, LA)
    return ok and "PT (Pacific)" in c.last(), detail, spy


async def named_pt_group() -> tuple[bool, str, Spy]:
    """2026-11-27, a group, in Portuguese: a coffee in San Francisco, "é PT"."""
    day = the_day(5)
    wd = PT[day.weekday()]
    c, spy = chat(GROUP_JID)
    c.other("Prazer em conhecer! Que dias você está livre semana que vem para um café em sf?",
            name="Rafael")
    c.me("Estarei ai a semana toda")
    c.me(f"{wd} de manha, talvez?")
    c.other("Por mim funciona. Você se importa de nos encontrarmos perto do Salesforce park?",
            name="Rafael")
    c.me("claro que não, vamos lá. qual seu email? aí te mando invite!")
    c.other("rafael.souza@example.com", name="Rafael")
    c.me(f"10am {wd} no salesforce park?")
    c.other("Fechado", name="Rafael")
    p = await to_card(c, "@lisa agendar. atenção para o time zone, é PT.")
    ok, detail = verdict(p, c.last(), day, 10, LA)
    return ok and "PT (Pacífico)" in c.last(), detail, spy


async def place_implied() -> tuple[bool, str, Spy]:
    """The same coffee, and nobody says a zone: an in-person meeting is on that place's clock."""
    day = the_day(5)
    wd = PT[day.weekday()]
    c, spy = chat(GROUP_JID)
    c.other("Prazer em conhecer! Que dias você está livre semana que vem para um café em San "
            "Francisco?", name="Rafael")
    c.me("Estarei ai em San Francisco a semana toda")
    c.other("Você se importa de nos encontrarmos no Salesforce Park, em San Francisco?",
            name="Rafael")
    c.me("claro que não. qual seu email? aí te mando invite!")
    c.other("rafael.souza@example.com", name="Rafael")
    c.me(f"10am {wd} no Salesforce Park?")
    c.other("Fechado", name="Rafael")
    p = await to_card(c, "@lisa agendar.")
    ok, detail = verdict(p, c.last(), day, 10, LA)
    return ok, detail, spy


async def named_et() -> tuple[bool, str, Spy]:
    """Eastern time, said by the other person, with no reminder from him."""
    day = the_day(4)
    c, spy = chat()
    c.other(f"Marcelo, can we talk {_WEEKDAYS[day.weekday()]} {MONTHS[day.month - 1]} {day.day}, "
            f"3pm ET? My email is {ANA}", name="Ana")
    c.me("works for me")
    p = await to_card(c, "@lisa book it, video call. title Ana & Marcelo")
    ok, detail = verdict(p, c.last(), day, 15, NY)
    return ok and "ET (Eastern)" in c.last(), detail, spy


async def named_lisbon() -> tuple[bool, str, Spy]:
    """A zone named by its city, in Portuguese."""
    day = the_day(6)
    wd = PT[day.weekday()]
    c, spy = chat()
    c.other(f"Marcelo, podemos falar na {wd} que vem? Estou em Lisboa. Meu email: {ANA}", name="Ana")
    c.me("podemos sim! que horas?")
    c.other("10h no horário de Lisboa fica bom pra você?", name="Ana")
    c.me("fechado")
    p = await to_card(c, "@lisa agendar, chamada de vídeo. título Ana & Marcelo")
    ok, detail = verdict(p, c.last(), day, 10, LISBON)
    return ok and "(Lisboa)" in c.last(), detail, spy


async def home() -> tuple[bool, str, Spy]:
    """An ordinary booking: his own clock, one time line, no zone words, no field left over."""
    day = the_day(1)
    c, spy = chat(GROUP_JID)
    p = await to_card(c, "@lisa agendar revisao do orcamento amanha 13hrs, sem convidados")
    card = c.last()
    start = str(p.get("start") or "")
    ok = (start.startswith(f"{day.isoformat()}T13:00") and start.endswith("-03:00")
          and "timezone" not in p and "BRT" not in card and "GMT" not in card and "13:00" in card)
    return ok, f"got start={start!r} timezone={p.get('timezone')!r}; card: {card[:200]!r}", spy


async def forced() -> tuple[bool, str, Spy]:
    """Her first answer is the 2026-11-26 mistake; the note has to bring back 8am Pacific."""
    day = the_day(5)
    his = datetime(day.year, day.month, day.day, 8, tzinfo=ZoneInfo(LA)).astimezone(
        ZoneInfo(settings().calendar_timezone))
    mistake = {
        "state": "keep_listening", "message": None, "lang": "en",
        "reasoning": "8am PT is 1pm in Sao Paulo.",
        "actions": [{"task": "calendar.create", "title": "Ana & Marcelo", "virtual": True,
                     "start": his.isoformat(), "weekday": _WEEKDAYS[his.weekday()],
                     "timezone": LA, "attendees": [ANA]}],
        "workflow": None, "call": None, "usage": {"input": 0, "output": 0},
        "provider_request_id": None, "stop_reason": "end_turn", "tool_calls": [],
        "error_category": "none",
    }
    c, spy = chat(forced=mistake)
    c.other(f"Awesome! How about {_WEEKDAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day} "
            f"at 8am PT? My email is {ANA}", name="Ana")
    c.me("ok!")
    p = await to_card(c, "@lisa book this. title Ana & Marcelo. Google meet. Be mindful of the "
                         "timezone, its PT.")
    noted = any(isinstance(m, dict) and m.get("role") == "user"
                and "does not match its time" in str(m.get("content"))
                for msgs in spy.sent for m in msgs)
    ok, detail = verdict(p, c.last(), day, 8, LA)
    return ok and noted, f"note sent={noted}; {detail}", spy


PLAN = [
    ("named_pt", "named: '8am PT', one-to-one, he says to mind the zone", named_pt),
    ("named_pt_group", "named: coffee in SF, 'é PT', group", named_pt_group),
    ("place_implied", "place: coffee in San Francisco, nobody names a zone", place_implied),
    ("named_et", "named: '3pm ET', no reminder from him", named_et),
    ("named_lisbon", "named: '10h no horário de Lisboa'", named_lisbon),
    ("home", "home: 'amanha 13hrs' reads as it always did", home),
    ("forced", "forced: the 2026-11-26 mistake, corrected live", forced),
]


async def main() -> int:
    now, tz = now_in(settings().calendar_timezone)
    print(f"Owner's clock: {_WEEKDAYS[now.weekday()]} {now:%Y-%m-%d %H:%M} ({tz}), "
          f"{REPEAT} run(s) each\n")
    failed = 0
    answers, sent = [], []
    for key, name, make in PLAN:
        if ONLY and key not in ONLY:
            continue
        results = await asyncio.gather(*(make() for _ in range(REPEAT)), return_exceptions=True)
        n_ok = 0
        for r in results:
            if isinstance(r, Exception):
                print(f"   [ERROR] {name}: {type(r).__name__}: {r}")
                continue
            ok, detail, spy = r
            answers.extend(spy.answers)
            sent.extend(spy.sent)
            n_ok += ok
            if not ok:
                print(f"   [MISS] {detail}")
        failed += REPEAT - n_ok
        print(f"[{'PASS' if n_ok == REPEAT else 'FAIL'}] {name}: {n_ok}/{REPEAT}")

    real = [a for a in answers if not a.get("_forced")]
    errors = [a for a in real if (a.get("error_category") or "none") != "none"]
    writes = [x for a in real for x in (a.get("actions") or [])
              if x.get("task") in ("calendar.create", "calendar.update")]
    filled = [x for x in writes if x.get("timezone")]
    sent_back = sum(1 for msgs in sent for m in msgs[-1:]
                    if isinstance(m, dict) and "does not match its time" in str(m.get("content")))
    print(f"\nModel calls: {len(real)}, provider errors: {len(errors)}")
    print(f"Creates/updates from the model: {len(writes)}, with timezone: {len(filled)}")
    print(f"Zone send-backs (including the forced ones): {sent_back}")
    if errors:
        print("Provider errors mean Anthropic refused the request (schema?):",
              [a.get("stop_reason") for a in errors][:5])
    failed += len(errors)
    print(f"\n{'ALL PASSED' if not failed else f'{failed} miss(es)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    from live_key import require_tests_key

    require_tests_key()
    sys.exit(asyncio.run(main()))
