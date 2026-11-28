"""His answer to a fix plan reaches the fix desk, and nothing else does.

Lisa sends him fix plans in his chat with himself (Lisa flow/app/fixdesk/), and he answers with
a plain "fix it". That message has no tag, and her own message opened no window, so before this
rule the dispatcher dropped it. These checks drive the real /webhook route: what is forwarded
to FIX_REPLY_URL, what still goes to the flow, and that with the setting unset nothing changes.
No Redis, no flow running, no network.

    pip install -r requirements.txt && python tests/run_fix_reply.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _name in ("REDIS_URL", "AUTO_TRANSCRIBE_URL", "LISA_URL", "LISA_TAGS", "MAX_MESSAGE_AGE_SECONDS",
              "DRAIN_TRIGGER", "DRAIN_WINDOW_SECONDS", "DRAIN_QUIET_SECONDS", "FIX_REPLY_URL"):
    os.environ.pop(_name, None)  # the app reads its config from the environment at import

import httpx  # noqa: E402

import app as disp  # noqa: E402
from app import DrainBreaker, OwnerStore, is_fix_answer  # noqa: E402

LISA = disp.cfg.routes[0][1]
FIX = "http://lisa.test/fix/reply"
SELF = "5511900000001@s.whatsapp.net"
FRIEND = "5511900000002@s.whatsapp.net"

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def upsert(text: str, from_me: bool = True, jid: str = SELF, age: float = 2) -> dict:
    return {"event": "messages.upsert", "instance": "secretaria", "data": {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": "MSG"},
        "message": {"conversation": text}, "messageTimestamp": int(time.time() - age)}}


def unit_checks() -> None:
    print("is_fix_answer")
    for text in ("fix it", "Fix it", "FIX IT.", "  fix it F12", "fix it, but keep the limit",
                 "fix  it", "skip", "Skip F3"):
        check(f"yes: {text!r}", is_fix_answer(text))
    for text in ("", "fixit", "fix its name", "please fix it", "skipping lunch", "skipper",
                 "@lisa fix it", "fix"):
        check(f"no: {text!r}", not is_fix_answer(text))


async def webhook_checks() -> None:
    forwards: list[str] = []

    async def record_forward(url: str, body: dict) -> None:
        forwards.append(url)

    disp._forward = record_forward
    disp.breaker = DrainBreaker(0, 60, 180)
    disp.log.propagate = False
    transport = httpx.ASGITransport(app=disp.app, raise_app_exceptions=False)

    async def post(client: httpx.AsyncClient, body: dict) -> list[str]:
        forwards.clear()
        r = await client.post("/webhook", json=body)
        assert r.status_code == 200
        return list(forwards)

    print("\nFIX_REPLY_URL unset: nothing changes")
    disp.cfg.fix_reply_url = None
    disp.app.state.owners = OwnerStore(None, ttl=60)
    async with httpx.AsyncClient(transport=transport, base_url="http://dispatcher") as client:
        check("his untagged 'fix it' is dropped, as before", await post(client, upsert("fix it")) == [])
        check("health shows no fix route", (await client.get("/")).json().get("fix_reply_url") is None)

    print("\nFIX_REPLY_URL set")
    disp.cfg.fix_reply_url = FIX
    disp.app.state.owners = OwnerStore(None, ttl=60)
    async with httpx.AsyncClient(transport=transport, base_url="http://dispatcher") as client:
        check("his 'fix it' goes to the fix desk", await post(client, upsert("fix it")) == [FIX])
        check("... with a case tag and a note too",
              await post(client, upsert("Fix it F12, but keep the 5 minute limit")) == [FIX])
        check("his 'skip' goes to the fix desk", await post(client, upsert("skip")) == [FIX])
        check("... and it opened no window: his next note is dropped",
              await post(client, upsert("comprar leite")) == [])
        check("someone else's 'fix it' is dropped",
              await post(client, upsert("fix it", from_me=False, jid=FRIEND)) == [])
        check("'please fix it' is not an answer", await post(client, upsert("please fix it")) == [])
        check("her own message quoting the words is not an answer",
              await post(client, upsert("*[Marcelo's AI Assistant]:*\n\nfix it")) == [])
        check("a tagged 'fix it' is hers, through the tag route",
              await post(client, upsert("@lisa fix it")) == [LISA])
        check("... and while that window is live, a plain 'fix it' follows the window",
              await post(client, upsert("fix it")) == [LISA])
        check("in another chat with no window it still reaches the desk, which ignores it",
              await post(client, upsert("fix it", jid=FRIEND)) == [FIX])
        check("a 'fix it' from 25 hours ago is not acted on",
              await post(client, upsert("fix it", jid=FRIEND, age=25 * 3600)) == [])
        check("health shows the fix route", (await client.get("/")).json().get("fix_reply_url") == FIX)
    disp.cfg.fix_reply_url = None


async def main() -> int:
    unit_checks()
    await webhook_checks()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
