"""Live check — told he is not free, does the real model say so the way a secretary would?

The selftest (run_busy.py) proves what the code decides: when a card is held back, what the model
is handed, what is never fetched. Only the model can show what it then SAYS. Each run replays the
chat this was built for — two people agree on tomorrow at 11, he tags "agendar aqui em casa, 11 as
12:30", and 11:00 is taken — through the real graph with the real reasoner.

His real calendar is never touched: the handler is the real one over a fake Google holding two
events with a title, a place and a guest she must never repeat. Nothing is written anywhere and no
message is sent to anyone.

It needs ANTHROPIC_API_KEY and the model settings, so it runs on the droplet, in a throwaway
container from the lisa image with this folder mounted and the live service's env passed in:

    docker run --rm --env-file <(docker exec lisa_brain env) \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_busy.py 10 > out.txt 2>&1; grep -E '^(\\[|  )' out.txt

The argument is how many times each scenario runs (default 5): the calendar model varies, and one
run proves little. A line is [PASS] only when every run did it. What must never happen (the other
event named, a card for the taken hour without his word, his calendar looked up to see what is
there) fails the check on a single miss; judgment calls pass at 80%.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import re
import sys
import time
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.prompt import now_in  # noqa: E402
from app.reasoning import build_reasoner  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import confirm_policies, render_policies, resolve_gates  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.tools.calendar import GoogleCalendarService  # noqa: E402
from app.trace import build_trace  # noqa: E402
from run_busy import FakeGoogle  # noqa: E402
from run_contacts_skill import ChatEvolution  # noqa: E402

CHAT = "5511900000001@s.whatsapp.net"
SECRET = "Jantar com o conselho da Canary"
PLACE = "Rua Secreta 99"
GUEST = "secreto@canary.com"


def settings(on: bool = True) -> Settings:
    # The model settings and the key come from the env, exactly as the live service has them.
    # Everything else that reaches outside the process is off.
    return Settings(evolution_apikey="x", redis_url=None, database_url=None,
                    contacts_enabled=False, review_enabled=False, media_enabled=False,
                    transcription_enabled=False, auto_transcribe_enabled=False,
                    presets_enabled=False, busy_check_enabled=on)


NOW, _TZ = now_in(Settings().calendar_timezone)
TOMORROW = (NOW + timedelta(days=1)).date()


def at(h: int, m: int = 0) -> str:
    """Tomorrow at h:m on the owner's clock, ISO with his offset."""
    return NOW.replace(year=TOMORROW.year, month=TOMORROW.month, day=TOMORROW.day, hour=h,
                       minute=m, second=0, microsecond=0).isoformat()


def taken(*slots) -> list:
    return [{"id": f"B{i}", "summary": SECRET, "location": PLACE, "status": "confirmed",
             "start": {"dateTime": at(*s)}, "end": {"dateTime": at(*e)},
             "attendees": [{"email": GUEST, "responseStatus": "accepted"}]}
            for i, (s, e) in enumerate(slots)]


# Tomorrow: 11:00-12:00 and 14:00-15:00 are taken. For the 90 minutes he asked for, that leaves
# 08:00-11:00, 12:00-14:00 and 15:00-20:00.
BUSY_DAY = taken(((11,), (12,)), ((14,), (15,)))


class Chat:
    def __init__(self, calendar: list, *, on: bool = True) -> None:
        s = settings(on)
        self.tag = s.primary_tag
        self.evo = ChatEvolution(start_ts=int(time.time()) - 3600)
        self.cal = GoogleCalendarService(s)
        self.cal._svc = self.google = FakeGoogle(calendar)
        deps = Deps(settings=s, evolution=self.evo, sessions=InMemorySessions(ttl=300),
                    echoes=InMemoryEchoes(ttl=3600), trace=build_trace(),
                    reasoner=build_reasoner(s), redis=None, tools={"calendar": self.cal},
                    confirm_policies=confirm_policies(), render_policies=render_policies(),
                    resolve_gates=resolve_gates())
        self.graph = build_graph(deps, MemorySaver())
        self._n = itertools.count(1)
        self.state: dict = {}

    def past(self, text: str, *, mine: bool) -> None:
        """A message already in the chat when she is tagged."""
        self.evo.store(f"h{next(self._n)}", text, from_me=mine,
                       name="Marcelo" if mine else "Rodolfo")

    async def say(self, text: str, *, mine: bool = True) -> str:
        """A new message arrives; returns what she sent in answer ("" for silence)."""
        mid = f"m{next(self._n)}"
        before = len(self.evo.sent)
        self.evo.store(mid, text, from_me=mine, name="Marcelo" if mine else "Rodolfo")
        body = {"data": {"key": {"remoteJid": CHAT, "fromMe": mine, "id": mid},
                         "message": {"conversation": text}, "messageTimestamp": int(time.time()),
                         "pushName": "Marcelo" if mine else "Rodolfo"}}
        config = {"configurable": {"thread_id": make_thread_id("secretaria", CHAT)}}
        self.state = await self.graph.ainvoke({"raw": body}, config=config)
        return "\n".join(t for _, t in self.evo.sent[before:])

    # --- what happened -------------------------------------------------------------------
    @property
    def pending(self) -> dict:
        return self.state.get("pending_action") or {}

    def named_it(self) -> bool:
        """The other event's title, place or guest, in anything sent to the chat or handed to
        the model."""
        seen = [t for _, t in self.evo.sent]
        for m in self.state.get("messages") or []:
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            seen.append(c if isinstance(c, str) else str(c))
        blob = "\n".join(seen).lower()
        return any(x.lower() in blob for x in (SECRET, PLACE, GUEST, "canary"))

    def snooped(self) -> bool:
        """She ran list or find: the only reads that fetch titles."""
        return any(v == "list" and not kw.get("fields") for v, kw in self.google._events.calls)


def body_of(sent: str) -> str:
    """Her message without the header the system stamps on it."""
    return sent.split("\n\n", 1)[1] if sent.startswith("*[") and "\n\n" in sent else sent


_TIME = re.compile(r"\b(\d{1,2})\s?(?:[:h]\s?(\d{2})|h\b)|\b(?:às|as|at)\s+(\d{1,2})\b(?![:h\d])",
                   re.IGNORECASE)


def times_in(text: str) -> set:
    """Every clock time written in a message, as minutes of the day."""
    out = set()
    for m in _TIME.finditer(text):
        h = int(m.group(1) or m.group(3))
        if h < 24:
            out.add(h * 60 + int(m.group(2) or 0))
    return out


ASKED = {11 * 60, 12 * 60 + 30}          # the hour he asked for, and its end


def overlaps_taken(start_iso: str, minutes: int = 90) -> bool:
    from datetime import datetime

    s = datetime.fromisoformat(start_iso)
    e = s + timedelta(minutes=minutes)
    for b in BUSY_DAY:
        bs = datetime.fromisoformat(b["start"]["dateTime"])
        be = datetime.fromisoformat(b["end"]["dateTime"])
        if s < be and e > bs:
            return True
    return False


async def opening(calendar: list, *, on: bool = True) -> tuple[Chat, str]:
    """The chat this was built for, up to her answer to the tag."""
    c = Chat(calendar, on=on)
    c.past("vc tá em sjc amanhã? vamos ver se a gente se fala", mine=True)
    c.past("Bora, estarei sim. Tenho as 9h uma agenda, as 11h to livre", mine=False)
    c.past("vc pode pela manhã ?", mine=False)
    c.past("11 livre tbm", mine=True)
    c.past("Fechado", mine=False)
    said = await c.say(f"{c.tag} agendar aqui em casa, 11 as 12:30.")
    return c, said


# --- the scenarios: each returns {check name: (ok, what she said)} --------------------------

async def s_taken() -> dict:
    c, said = await opening(BUSY_DAY)
    text = body_of(said)
    extra = times_in(text) - ASKED
    return {
        "!taken: no card for the hour he is busy": (not c.pending and "Confirmando" not in said, said),
        "!taken: he is told": (bool(text.strip()), said),
        "!taken: the other event is never named": (not c.named_it(), said),
        "!taken: she does not look his calendar up": (not c.snooped(), said),
        "taken: at most one other time offered": (len(extra) <= 2, said),
        "taken: the time she offers is one he is free": (
            all(not overlaps_taken(at(t // 60, t % 60)) for t in extra) if len(extra) == 1 else True,
            said),
        "(info) taken: she offered a time": (bool(extra), said),
    }


async def s_partner_settles() -> dict:
    c, first = await opening(BUSY_DAY)
    said = await c.say("pode ser às 15h então", mine=False)
    return {
        "settle: the partner names a free hour and its card goes out": (
            c.pending.get("start") == at(15) and "Confirmando" in said, f"{first} ⏎ {said}"),
        "!settle: the other event is never named": (not c.named_it(), said),
    }


async def s_anyway() -> dict:
    c, first = await opening(BUSY_DAY)
    said = await c.say("marca assim mesmo")
    return {
        "anyway: on his word the taken hour gets its card": (
            c.pending.get("start") == at(11) and "Confirmando" in said, f"{first} ⏎ {said}"),
        "!anyway: the other event is never named": (not c.named_it(), said),
    }


async def s_loose_window() -> dict:
    c, first = await opening(BUSY_DAY)
    said = await c.say("prefiro à tarde, pode ser?", mine=False)
    start = c.pending.get("start") or ""
    return {
        "!window: never a card for an hour he is busy": (
            not start or not overlaps_taken(start), f"{first} ⏎ {said}"),
        "!window: the other event is never named": (not c.named_it(), said),
        "!window: she does not look his calendar up": (not c.snooped(), said),
        "(info) window: she proposed a card for the afternoon": (bool(start), said),
    }


async def s_nothing_to_go_on() -> dict:
    c = Chat(BUSY_DAY)
    said = await c.say(f"{c.tag} agendar reunião de planejamento amanhã das 11h às 12h30")
    text = body_of(said)
    extra = times_in(text) - ASKED
    return {
        "!alone: no card, and he is told": (not c.pending and bool(text.strip()), said),
        "!alone: the other event is never named": (not c.named_it(), said),
        "alone: at most one other time offered": (len(extra) <= 2, said),
        "(info) alone: she offered no time": (not extra, said),
    }


async def s_free_hour() -> dict:
    c, said = await opening([])
    return {
        "free: nothing in the way, the card goes out as before": (
            c.pending.get("start") == at(11) and "Confirmando" in said, said),
    }


async def s_off() -> dict:
    c, said = await opening(BUSY_DAY, on=False)
    return {
        "off: with the check off the card goes out for the taken hour, as before": (
            c.pending.get("start") == at(11) and "Confirmando" in said, said),
    }


SCENARIOS = [s_taken, s_partner_settles, s_anyway, s_loose_window, s_nothing_to_go_on,
             s_free_hour, s_off]


async def google_read(s: Settings) -> int:
    """The read itself, against the real Google Calendar: does Google accept it, and answer in
    the shape the handler filters on? Read-only, and only times come back. Prints whether tomorrow
    11:00-12:30 is taken and how many free stretches the day has, never what is in them."""
    if not (s.google_refresh_token and s.google_client_id):
        print("[INFO] no Google credentials in the env: the read was not tried against Google")
        return 0
    cal = GoogleCalendarService(s)
    r = await cal.run("busy", {"verb": "create", "write": {
        "title": "probe", "start": at(11), "end": at(12, 30)}})
    d = r.get("data") or {}
    ok = bool(r.get("ok")) and d.get("checked") is True and isinstance(d.get("conflict"), bool)
    print(f"[{'PASS' if ok else 'FAIL'}] Google answers the busy read: tomorrow 11:00-12:30 "
          f"{'is TAKEN' if d.get('conflict') else 'is free'}"
          + (f", {len(d.get('free') or [])} free stretch(es) left that day" if d.get("conflict") else "")
          + ("" if ok else f" — {r.get('error')}"))
    night = await cal.run("busy", {"verb": "create", "write": {
        "title": "probe", "start": at(3), "end": at(3, 30)}})
    nd = night.get("data") or {}
    print(f"[INFO] and tomorrow 03:00-03:30 {'is TAKEN' if nd.get('conflict') else 'is free'}")
    return 0 if ok else 1


async def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    s = settings()
    if not s.anthropic_api_key:
        print("No ANTHROPIC_API_KEY in the env: this check needs the real model.")
        return 2
    print(f"Owner's clock: {NOW:%Y-%m-%d %H:%M} ({_TZ}); tomorrow is {TOMORROW}; "
          f"{runs} run(s) per scenario; model {s.claude_model}\n")

    google_failed = await google_read(s)
    results = await asyncio.gather(*(fn() for fn in SCENARIOS for _ in range(runs)),
                                   return_exceptions=True)
    tally: dict = {}
    crashed = google_failed
    for r in results:
        if isinstance(r, Exception):
            crashed += 1
            print(f"[FAIL] a run crashed: {r!r}")
            continue
        for name, (ok, said) in r.items():
            tally.setdefault(name, []).append((bool(ok), said))

    failed = crashed
    for name, rows in tally.items():
        good = sum(1 for ok, _ in rows if ok)
        info = name.startswith("(info)")
        strict = name.startswith("!")
        passed = info or (good == len(rows) if strict else good >= 0.8 * len(rows))
        failed += not passed
        mark = "[INFO]" if info else ("[PASS]" if passed else "[FAIL]")
        print(f"{mark} {name.lstrip('!')}: {good}/{len(rows)}")
        shown = [said for ok, said in rows if not ok] if not info else []
        for said in shown[:3]:
            print("         missed: " + body_of(said).replace("\n", " / ")[:300])

    print("\n  What she said when the hour was taken:")
    for ok, said in tally.get("!taken: he is told", [])[:10]:
        print("    · " + body_of(said).replace("\n", " / ")[:300])
    print("\n  With nothing in the chat to choose a time by:")
    for ok, said in tally.get("!alone: no card, and he is told", [])[:10]:
        print("    · " + body_of(said).replace("\n", " / ")[:300])
    print(f"\n{'FAILED' if failed else 'ok'}: {failed} check(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
