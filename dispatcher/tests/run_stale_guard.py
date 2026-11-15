"""A message that reaches the dispatcher long after it was sent is not acted on.

On 2026-11-13/14 WhatsApp held this number's incoming messages for up to 25 hours (a Baileys
bug: status stanzas never acked, evolution-api #2778), and every reconnect replayed a slice of
the backlog. Each replayed "@lisa ..." was forwarded as if he had just typed it, so Lisa
answered day-old requests, and the voice notes in it would have been transcribed into the chat
a day late. These checks drive the real /webhook route with messages of different ages. No
Redis, no flow running, no network.

    pip install -r requirements.txt && python tests/run_stale_guard.py
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
from app import DrainBreaker, OwnerStore, message_age  # noqa: E402

LISA = disp.cfg.routes[0][1]
TRANSCRIBE = "http://transcribe.test/webhook"
CHAT = "5511999990000@s.whatsapp.net"
OTHER_CHAT = "5511888880000@s.whatsapp.net"

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def upsert(text: str, from_me: bool, sent: object, jid: str = CHAT, audio: bool = False) -> dict:
    message = {"audioMessage": {"seconds": 4}} if audio else {"conversation": text}
    data = {"key": {"remoteJid": jid, "fromMe": from_me, "id": "MSG"}, "message": message}
    if sent is not None:
        data["messageTimestamp"] = sent
    return {"event": "messages.upsert", "instance": "secretaria", "data": data}


def ago(seconds: float) -> int:
    return int(time.time() - seconds)


def unit_checks() -> None:
    print("message_age")
    now = 1_800_000_000.0
    check("seconds", message_age({"messageTimestamp": 1_799_999_400}, now) == 600)
    check("a string of seconds", message_age({"messageTimestamp": "1799999400"}, now) == 600)
    check("milliseconds", message_age({"messageTimestamp": 1_799_999_400_000}, now) == 600)
    check("a protobuf Long", message_age({"messageTimestamp": {"low": 1_799_999_400, "high": 0}}, now) == 600)
    check("no time at all", message_age({}, now) is None)
    check("zero", message_age({"messageTimestamp": 0}, now) is None)
    check("garbage", message_age({"messageTimestamp": "soon"}, now) is None)


async def webhook_checks() -> None:
    forwards: list[str] = []

    async def record_forward(url: str, body: dict) -> None:
        forwards.append(url)

    disp._forward = record_forward
    disp.cfg.auto_transcribe_url = TRANSCRIBE
    disp.breaker = DrainBreaker(0, 60, 180)  # the age guard alone; the breaker has its own file
    lines = Lines()
    disp.log.addHandler(lines)
    disp.log.propagate = False
    transport = httpx.ASGITransport(app=disp.app, raise_app_exceptions=False)

    async def post(client: httpx.AsyncClient, body: dict) -> tuple[int, list[str]]:
        forwards.clear()
        r = await client.post("/webhook", json=body)
        return r.status_code, list(forwards)

    print("\n/webhook with the default limit (600 s)")
    disp.app.state.owners = OwnerStore(None, ttl=60)
    async with httpx.AsyncClient(transport=transport, base_url="http://dispatcher") as client:
        check("a tag sent just now is forwarded",
              await post(client, upsert("@lisa o que tenho hoje?", True, ago(2))) == (200, [LISA]))
        check("a tag sent 9 minutes ago is still forwarded",
              await post(client, upsert("@lisa agendar", True, ago(540), OTHER_CHAT)) == (200, [LISA]))
        disp.app.state.owners = OwnerStore(None, ttl=60)
        lines.lines.clear()
        check("a tag sent 25 hours ago is not forwarded, with a 200",
              await post(client, upsert("@lisa agendar", True, ago(25 * 3600))) == (200, []))
        check("... and it opens no window: the next live reply in that chat is dropped",
              await post(client, upsert("quinta 8am?", False, ago(1))) == (200, []))
        stale = [json.loads(l) for l in lines.lines if '"route":"stale"' in l]
        check("... and it is logged as stale, with its age",
              len(stale) == 1 and stale[0]["jid"] == CHAT and abs(stale[0]["age_s"] - 25 * 3600) <= 5,
              str(lines.lines[:3]))
        await post(client, upsert("@lisa oi", True, ago(1)))
        check("an untagged reply in an open window is forwarded when live",
              await post(client, upsert("pode ser", False, ago(3))) == (200, [LISA]))
        check("... but not when it is 30 minutes old",
              await post(client, upsert("pode ser", False, ago(1800))) == (200, []))
        check("a live voice note reaches transcription",
              (await post(client, upsert("", False, ago(5), OTHER_CHAT, audio=True)))[1] == [TRANSCRIBE])
        check("a voice note from yesterday does not",
              await post(client, upsert("", False, ago(86400), OTHER_CHAT, audio=True)) == (200, []))
        check("a payload with no send time is treated as live",
              await post(client, upsert("@lisa oi", True, None)) == (200, [LISA]))
        check("a send time in milliseconds is read as such",
              await post(client, upsert("@lisa oi", True, int(time.time() * 1000) - 2000)) == (200, [LISA]))
        health = (await client.get("/")).json()
        check("health reports the limit", health.get("max_message_age") == 600, str(health.get("max_message_age")))

    print("\n/webhook with the guard off (MAX_MESSAGE_AGE_SECONDS=0)")
    disp.cfg.max_message_age = 0
    disp.app.state.owners = OwnerStore(None, ttl=60)
    async with httpx.AsyncClient(transport=transport, base_url="http://dispatcher") as client:
        check("a tag sent 25 hours ago is forwarded, as before the guard",
              await post(client, upsert("@lisa agendar", True, ago(25 * 3600))) == (200, [LISA]))
    disp.cfg.max_message_age = 600


async def main() -> int:
    unit_checks()
    await webhook_checks()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
