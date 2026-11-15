"""While a backlog is being replayed, nothing is acted on, not even messages that look fresh.

On 2026-11-15 a patched Evolution released 25 hours of held messages in about 15 minutes. The
age guard dropped some 1,000 of them, but 133 of his own phone messages from that day arrived
stamped with their delivery time, and Lisa answered or transcribed eight of them into chats
with other people. These checks drive the real DrainBreaker (with a clock the test moves) and
the real /webhook route. No Redis, no flow running, no network.

    pip install -r requirements.txt && python tests/run_drain_breaker.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _name in ("REDIS_URL", "AUTO_TRANSCRIBE_URL", "LISA_URL", "LISA_TAGS", "MAX_MESSAGE_AGE_SECONDS",
              "DRAIN_TRIGGER", "DRAIN_WINDOW_SECONDS", "DRAIN_QUIET_SECONDS"):
    os.environ.pop(_name, None)  # the app reads its config from the environment at import

import httpx  # noqa: E402

import app as disp  # noqa: E402
from app import DrainBreaker, OwnerStore  # noqa: E402

LISA = disp.cfg.routes[0][1]
TRANSCRIBE = "http://transcribe.test/webhook"
CHAT = "5511999990000@s.whatsapp.net"
OTHER_CHAT = "5511888880000@s.whatsapp.net"
GROUP = "120363000000000000@g.us"

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def upsert(text: str, from_me: bool, age_s: float, jid: str = CHAT, audio: bool = False) -> dict:
    message = {"audioMessage": {"seconds": 4}} if audio else {"conversation": text}
    return {"event": "messages.upsert", "instance": "secretaria",
            "data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": "MSG"}, "message": message,
                     "messageTimestamp": int(time.time() - age_s)}}


def defaults_checks() -> None:
    print("defaults")
    check("trigger 5, window 60 s, quiet 180 s",
          (disp.cfg.drain_trigger, disp.cfg.drain_window, disp.cfg.drain_quiet) == (5, 60.0, 180.0))


async def webhook_checks() -> None:
    forwards: list[str] = []

    async def record_forward(url: str, body: dict) -> None:
        forwards.append(url)

    disp._forward = record_forward
    disp.cfg.auto_transcribe_url = TRANSCRIBE
    lines = Lines()
    disp.log.addHandler(lines)
    disp.log.propagate = False
    transport = httpx.ASGITransport(app=disp.app, raise_app_exceptions=False)

    async def post(client: httpx.AsyncClient, body: dict) -> tuple[int, list[str]]:
        forwards.clear()
        r = await client.post("/webhook", json=body)
        return r.status_code, list(forwards)

    clock = Clock()
    disp.breaker = DrainBreaker(5, 60, 180, clock=clock)
    disp.app.state.owners = OwnerStore(None, ttl=420)
    day = 25 * 3600

    async with httpx.AsyncClient(transport=transport, base_url="http://dispatcher") as client:
        print("\na trickle of late messages is not a replay")
        for _ in range(4):
            await post(client, upsert("bom dia", False, day, GROUP))
            clock.tick(5)
        check("four stale messages in 20 s do not start a drain", not disp.breaker.draining())
        check("... and a live tag still reaches Lisa",
              await post(client, upsert("@lisa o que tenho hoje?", True, 2)) == (200, [LISA]))
        disp.app.state.owners = OwnerStore(None, ttl=420)
        disp.breaker = DrainBreaker(5, 60, 180, clock=clock)
        for _ in range(6):
            await post(client, upsert("kkk", False, day, GROUP))
            clock.tick(20)
        check("six stale messages 20 s apart (never five within 60 s) do not start one either",
              not disp.breaker.draining())

        print("\nthe 2026-11-15 replay")
        disp.breaker = DrainBreaker(5, 60, 180, clock=clock)
        lines.lines.clear()
        for i in range(50):
            await post(client, upsert(f"msg {i}", i % 2 == 0, day - i * 60, GROUP))
            clock.tick(0.5)
        check("fifty day-old messages in 25 s start a drain", disp.breaker.draining())
        started = [json.loads(l) for l in lines.lines if l.startswith('{"drain": "started"')]
        check("... logged once, as it starts", len(started) == 1, str(started))
        check("his re-sent '@lisa agendar', stamped as if sent just now, is held",
              await post(client, upsert("@lisa agendar", True, 3)) == (200, []))
        check("... and it opens no window: the reply after it is held too, then dropped",
              await post(client, upsert("quarta 16h?", False, 2)) == (200, [])
              and await disp.app.state.owners.get(CHAT) is None)
        check("a fresh-looking voice note is not transcribed into the chat",
              await post(client, upsert("", True, 1, OTHER_CHAT, audio=True)) == (200, []))
        held = [l for l in lines.lines if '"route":"draining"' in l]
        check("each held message is logged as draining", len(held) == 3, str(len(held)))
        health = (await client.get("/")).json()["drain"]
        check("health shows the drain and what it held",
              health["draining"] is True and health["held_this_drain"] == 3 and health["drains"] == 1, str(health))

        print("\nthe replay goes on, then stops")
        for _ in range(10):
            clock.tick(30)
            await post(client, upsert("late", False, day, GROUP))
        check("stale messages still arriving keep it on", disp.breaker.draining())
        clock.tick(179)
        check("179 s after the last stale message it is still on",
              await post(client, upsert("@lisa oi", True, 1)) == (200, []))
        clock.tick(1)
        lines.lines.clear()
        check("180 s after it, a live tag reaches Lisa again",
              await post(client, upsert("@lisa agendar", True, 1)) == (200, [LISA]))
        ended = [json.loads(l) for l in lines.lines if l.startswith('{"drain": "ended"')]
        check("... and the end is logged with what it held",
              len(ended) == 1 and ended[0]["held"] == 4, str(ended))
        check("the window that tag opened works as before",
              await post(client, upsert("quarta 16h?", False, 2)) == (200, [LISA]))
        check("a live voice note is transcribed as before",
              (await post(client, upsert("", False, 1, OTHER_CHAT, audio=True)))[1] == [TRANSCRIBE])
        health = (await client.get("/")).json()["drain"]
        check("health shows it over", health["draining"] is False and health["drains"] == 1
              and health["held"] == 4, str(health))

        print("\nDRAIN_TRIGGER=0")
        disp.breaker = DrainBreaker(0, 60, 180, clock=clock)
        for i in range(50):
            await post(client, upsert("x", False, day, GROUP))
        check("fifty stale messages start nothing", not disp.breaker.draining())
        check("... and a fresh-looking tag is forwarded, as before the breaker",
              await post(client, upsert("@lisa oi", True, 1, OTHER_CHAT)) == (200, [LISA]))


async def main() -> int:
    defaults_checks()
    await webhook_checks()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
