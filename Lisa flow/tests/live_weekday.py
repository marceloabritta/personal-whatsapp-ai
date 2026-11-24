"""Live check — does a weekday the group agreed on land on its own date, with the real model?

On Tuesday 2026-11-24 a group agreed on "quinta 15hrs". Lisa booked Friday the 27th: her reasoning
read "Thursday (2026-11-27)". Since then every create and update carries a `weekday` that code
checks against "start" (skills/weekday_check.py), and a mismatch sends her back once.

This replays that chat relative to NOW, through the real graph and the real model, up to the card:
  - "agreed": the group settles on a weekday 2 to 6 days ahead, in the words used that day;
  - "forced": the model's first answer is replaced by the very mistake of 2026-11-24 (that weekday
    with the next day's date), so the send-back runs against the real API, and the real model has
    to read the note and correct it.
Every model answer is recorded, so the run also shows Anthropic accepting the schema (any provider
error fails it), how often the model filled `weekday`, and how often the check fired on its own.

A stub calendar and a fake WhatsApp: nothing is written anywhere and nothing is sent to anyone.
Runs on the droplet like the other live checks (tests/live_key.py), billed to the tests key:

    docker run --rm --env-file <(docker exec lisa_brain env) --env-file /opt/evolution/tests.env \\
      -v "<tree>/tests:/app/tests:ro" <image> python tests/live_weekday.py

LIVE_REPEAT (default 5) runs each scenario that many times. Exits non-zero on any miss.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.graph import build_graph  # noqa: E402
from app.prompt import _WEEKDAYS, now_in  # noqa: E402
from live_partner import GROUP_JID, Chat, held, settings  # noqa: E402

REPEAT = int(os.environ.get("LIVE_REPEAT", "5"))
PT = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
SHORT = ("2a", "3a", "4a", "5a", "6a", "sábado", "domingo")


class Spy:
    """The real reasoner, with every answer kept, and optionally its first answer replaced."""

    def __init__(self, real, forced_first: dict | None = None) -> None:
        self.real, self.forced, self.answers, self.sent = real, forced_first, [], []

    async def respond(self, **kw):
        self.sent.append(kw.get("messages") or [])
        if self.forced is not None:
            out, self.forced = self.forced, None
            self.answers.append({**out, "_forced": True})
            return out
        out = await self.real.respond(**kw)
        self.answers.append(out)
        return out

    def __getattr__(self, name):
        return getattr(self.real, name)


def group(spy_forced: dict | None = None) -> tuple[Chat, Spy]:
    c = Chat([], jid=GROUP_JID)
    spy = Spy(c.deps.reasoner, spy_forced)
    c.deps.reasoner = spy
    c.graph = build_graph(c.deps, MemorySaver())
    return c, spy


def the_chat(c: Chat, wd: str, first: str) -> None:
    """The group of 2026-11-24, with made-up people."""
    c.me("vamos agendar de nos falarmos para a Acme conhecer o time")
    c.other(f"Fala pessoal, muito prazer! Topam falar na {first} a tarde?", name="Pedro")
    c.me("Eu posso. =)")
    c.other("Consigo. on-line ou presencial?", name="Joao")
    c.other("so consigo no nosso escritorio, pode ser?", name="Pedro")
    c.me("Vamos la no escritorio. Melhor se verem ao vivo.")
    c.me("15hrs?")
    c.other("fechado!", name="Pedro")
    c.me(f"Ok entao {wd} 15hrs!")


async def to_card(c: Chat, tag: str) -> dict:
    """Tag her, and answer up to two questions the way he would, until a card is held."""
    st = await c.say(tag)
    for reply in ("sem convidados, so agenda pra mim", "isso, pode montar"):
        if held(st):
            break
        st = await c.say(reply)
    return held(st)


async def agreed(offset: int, short: bool) -> tuple[bool, str, Spy]:
    now, _ = now_in(settings().calendar_timezone)
    day = now.date() + timedelta(days=offset)
    wd = PT[day.weekday()]
    c, spy = group()
    the_chat(c, wd, SHORT[day.weekday()] if short else wd)
    p = await to_card(c, "@lisa agendar. Endereco: Rua Exemplo 100.")
    got = str(p.get("start") or "")[:10]
    ok = got == day.isoformat() and "weekday" not in p
    return ok, f"want {day} ({wd}), got {got or '(no card)'}; said: {c.last()[:160]!r}", spy


async def forced(offset: int) -> tuple[bool, str, Spy]:
    now, _ = now_in(settings().calendar_timezone)
    day = now.date() + timedelta(days=offset)
    wrong = day + timedelta(days=1)
    wd = PT[day.weekday()]
    mistake = {
        "state": "keep_listening", "message": None, "lang": "pt",
        "reasoning": f"Need to book {_WEEKDAYS[day.weekday()]} ({wrong.isoformat()}) 15:00.",
        "actions": [{"task": "calendar.create", "title": "Reuniao Acme",
                     "start": f"{wrong.isoformat()}T15:00:00-03:00",
                     "weekday": _WEEKDAYS[day.weekday()], "location": "Rua Exemplo 100"}],
        "workflow": None, "call": None, "usage": {"input": 0, "output": 0},
        "provider_request_id": None, "stop_reason": "end_turn", "tool_calls": [],
        "error_category": "none",
    }
    c, spy = group(mistake)
    the_chat(c, wd, wd)
    p = await to_card(c, "@lisa agendar. Endereco: Rua Exemplo 100.")
    got = str(p.get("start") or "")[:10]
    noted = any(isinstance(m, dict) and m.get("role") == "user"
                and "does not match its date" in str(m.get("content"))
                for msgs in spy.sent for m in msgs)
    ok = noted and got == day.isoformat() and "weekday" not in p
    return ok, (f"want {day} ({wd}) after the send-back, got {got or '(no card)'}; "
                f"note sent={noted}; said: {c.last()[:160]!r}"), spy


async def main() -> int:
    now, tz = now_in(settings().calendar_timezone)
    print(f"Owner's clock: {_WEEKDAYS[now.weekday()]} {now:%Y-%m-%d %H:%M} ({tz}), "
          f"{REPEAT} run(s) each\n")
    plan = []
    for offset in (2, 3, 4, 6):
        plan.append((f"agreed: '{PT[(now.date() + timedelta(days=offset)).weekday()]}' (+{offset}d)",
                     lambda o=offset: agreed(o, False)))
    plan.append(("agreed: '5a'-style first mention (+2d)", lambda: agreed(2, True)))
    plan.append(("forced: the 2026-11-24 mistake, corrected live (+2d)", lambda: forced(2)))
    plan.append(("forced: the 2026-11-24 mistake, corrected live (+3d)", lambda: forced(3)))

    failed = 0
    answers = []
    for name, make in plan:
        results = await asyncio.gather(*(make() for _ in range(REPEAT)), return_exceptions=True)
        n_ok = 0
        for r in results:
            if isinstance(r, Exception):
                print(f"   [ERROR] {name}: {type(r).__name__}: {r}")
                continue
            ok, detail, spy = r
            answers.extend(spy.answers)
            n_ok += ok
            if not ok:
                print(f"   [MISS] {detail}")
        failed += REPEAT - n_ok
        print(f"[{'PASS' if n_ok == REPEAT else 'FAIL'}] {name}: {n_ok}/{REPEAT}")

    real = [a for a in answers if not a.get("_forced")]
    errors = [a for a in real if (a.get("error_category") or "none") != "none"]
    writes = [x for a in real for x in (a.get("actions") or [])
              if x.get("task") in ("calendar.create", "calendar.update")]
    filled = [x for x in writes if x.get("weekday")]
    print(f"\nModel calls: {len(real)}, provider errors: {len(errors)}")
    print(f"Creates/updates from the model: {len(writes)}, with weekday: {len(filled)}")
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
