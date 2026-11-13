"""A message sent while its flow is restarting still arrives: once, and in order.

The dispatcher used to POST each forward once and drop it on any failure, so the seconds a
flow has no listener during a redeploy swallowed whatever was sent in them. These checks drive
the real Forwarder and the real /webhook route against a stand-in flow on localhost that can be
stopped, started, and told to answer any status. No Redis, no Docker, no network beyond
localhost. The pauses are shortened so the whole file runs in a few seconds; the rules are the
ones production runs with.

    pip install -r requirements.txt && python tests/run_forward_retry.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
FLOW = f"http://127.0.0.1:{PORT}/webhook"
for _name in ("REDIS_URL", "AUTO_TRANSCRIBE_URL", "LISA_TAGS", "FORWARD_RETRY_SECONDS"):
    os.environ.pop(_name, None)
os.environ["LISA_URL"] = FLOW  # the app reads its routes from the environment at import

import httpx  # noqa: E402

import app as disp  # noqa: E402
from app import Forwarder, OwnerStore  # noqa: E402

logging.getLogger().handlers = [logging.NullHandler()]  # keep the route log out of the results

CHAT = "5511999990000@s.whatsapp.net"
OTHER_CHAT = "5511888880000@s.whatsapp.net"

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


class Flow:
    """A stand-in flow: records the id of every message it is sent, in order. `stop` closes the
    listener AND the connections it holds, as a container being recreated does."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.got: list[str] = []
        self.status = 200
        self.status_for: dict[str, int] = {}  # chat jid -> status, when it differs
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for w in list(self._writers):
            w.close()
        await self._server.wait_closed()
        self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = int(re.search(rb"content-length: *(\d+)", head, re.I).group(1))
                key = json.loads(await reader.readexactly(length))["data"]["key"]
                status = self.status_for.get(key["remoteJid"], self.status)
                self.got.append(key["id"] if status == 200 else f"{key['id']}:{status}")
                writer.write(b"HTTP/1.1 %d X\r\ncontent-length: 0\r\n\r\n" % status)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, AttributeError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()


class LogLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def count(self, needle: str) -> int:
        return sum(needle in line for line in self.lines)


def upsert(msg_id: str, text: str = "oi", from_me: bool = True, jid: str = CHAT) -> dict:
    return {"event": "messages.upsert", "instance": "secretaria",
            "data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": msg_id},
                     "message": {"conversation": text}}}


async def until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


def quick(http: httpx.AsyncClient, retry_for: float = 3.0) -> Forwarder:
    """The real Forwarder, with the pauses between tries cut to 50 ms."""
    fw = Forwarder(http, retry_for)
    fw.BACKOFF = (0.05,)
    return fw


async def delivery_checks(flow: Flow, http: httpx.AsyncClient, logs: LogLines) -> None:
    print("\nThe flow is up — one POST each, nothing retried")
    fw = quick(http)
    check("a message is taken", fw.submit(FLOW, upsert("A1")))
    await until(lambda: flow.got == ["A1"])
    check("and reaches the flow once", flow.got == ["A1"], str(flow.got))
    await until(lambda: fw.state()["pending"] == 0)
    check("nothing is left waiting and nothing was retried",
          fw.state() == {"pending": 0, "retry_seconds": 3.0, "delivered_late": 0,
                         "gave_up": 0, "dropped": 0} and logs.count('"forward"') == 0,
          f"{fw.state()} {logs.lines}")

    print("\nThe flow is restarting — the message waits for it")
    flow.got.clear()
    await flow.stop()
    fw.submit(FLOW, upsert("B1", "@lisa marca um café amanhã"))
    await asyncio.sleep(0.4)
    check("while it is down nothing arrives, and the message is still held",
          flow.got == [] and fw.state()["pending"] == 1, f"{flow.got} {fw.state()}")
    await flow.start()
    await until(lambda: fw.state()["pending"] == 0)
    check("once it is back the message arrives, exactly once", flow.got == ["B1"], str(flow.got))
    check("the wait is logged once going in and once coming out",
          logs.count('"forward": "retrying"') == 1 and logs.count('"forward": "delivered"') == 1,
          str(logs.lines))
    done = json.loads(next(line for line in logs.lines if '"delivered"' in line))
    check("the log says how many tries and how long, and nothing of what was said",
          done["attempts"] > 1 and done["waited"] >= 0.3 and done["jid"] == CHAT
          and not any("café" in line for line in logs.lines), str(done))
    check("it is counted as delivered late", fw.state()["delivered_late"] == 1)
    await asyncio.sleep(0.3)
    check("and is never sent again afterwards", flow.got == ["B1"], str(flow.got))

    logs.lines.clear()
    await flow.stop()
    fw.submit(FLOW, upsert("B2"))
    fw.submit(FLOW, upsert("B3"))  # stands in line behind B2's retry; its own first try works
    await asyncio.sleep(0.2)
    await flow.start()
    await until(lambda: fw.state()["pending"] == 0)
    late = [json.loads(line) for line in logs.lines if '"delivered"' in line]
    check("a message that stood in line behind a retry is logged and counted as late too",
          len(late) == 2 and late[1]["attempts"] == 1 and late[1]["waited"] >= 0.1
          and fw.state()["delivered_late"] == 3 and logs.count("retrying") == 1, str(late))

    print("\nA connection the flow closed on its way down")
    flow.got.clear()
    fw.submit(FLOW, upsert("C1"))
    await until(lambda: flow.got == ["C1"])  # the client now holds an open connection to it
    await flow.stop()
    await flow.start()
    fw.submit(FLOW, upsert("C2"))
    await until(lambda: fw.state()["pending"] == 0)
    check("the next message still arrives, once", flow.got == ["C1", "C2"], str(flow.got))


async def order_checks(flow: Flow, http: httpx.AsyncClient) -> None:
    print("\nOrder within a chat, and chats apart")
    flow.got.clear()
    fw = quick(http)
    await flow.stop()
    for msg_id in ("D1", "D2", "D3"):  # his order, a detail, his "sim"
        fw.submit(FLOW, upsert(msg_id))
    fw.submit(FLOW, upsert("E1", jid=OTHER_CHAT))
    await asyncio.sleep(0.3)
    await flow.start()
    await until(lambda: fw.state()["pending"] == 0)
    mine = [m for m in flow.got if m.startswith("D")]
    check("three messages sent during the outage arrive in the order he sent them",
          mine == ["D1", "D2", "D3"], str(flow.got))
    check("each exactly once, the other chat's too", sorted(flow.got) == ["D1", "D2", "D3", "E1"],
          str(flow.got))

    flow.got.clear()
    flow.status_for = {CHAT: 503}  # one chat's messages cannot get through; the flow is up
    fw.submit(FLOW, upsert("F1"))
    await until(lambda: "F1:503" in flow.got)
    fw.submit(FLOW, upsert("G1", jid=OTHER_CHAT))
    reached = await until(lambda: "G1" in flow.got, timeout=1.0)
    check("a chat waiting on a retry does not hold another chat back",
          reached and fw.state()["pending"] == 1, f"{flow.got[-3:]} {fw.state()}")
    await until(lambda: flow.got.count("F1:503") >= 2)
    flow.status_for = {}
    await until(lambda: fw.state()["pending"] == 0)
    check("a 503 is tried again until the flow takes the message",
          flow.got[-1] == "F1" and flow.got.count("F1") == 1 and flow.got.count("F1:503") >= 2,
          str(flow.got[-4:]))


async def limit_checks(flow: Flow, http: httpx.AsyncClient, logs: LogLines) -> None:
    print("\nAn answer from the flow is final")
    flow.got.clear()
    logs.lines.clear()
    fw = quick(http)
    flow.status = 400
    fw.submit(FLOW, upsert("H1"))
    await until(lambda: fw.state()["pending"] == 0)
    await asyncio.sleep(0.2)
    check("a 400 is not retried", flow.got == ["H1:400"], str(flow.got))
    check("it is logged as rejected, with the status",
          logs.count('"forward": "rejected"') == 1 and logs.count('"status": 400') == 1
          and logs.count("retrying") == 0, str(logs.lines))
    flow.status = 200

    print("\nAn outage longer than the limit")
    flow.got.clear()
    logs.lines.clear()
    fw = quick(http, retry_for=0.6)
    await flow.stop()
    t = time.monotonic()
    fw.submit(FLOW, upsert("I1"))
    fw.submit(FLOW, upsert("I2"))  # behind I1 in the same chat
    await until(lambda: fw.state()["pending"] == 0, timeout=4.0)
    took = time.monotonic() - t
    check("both are given up, and counted", fw.state()["gave_up"] == 2, str(fw.state()))
    check("each within the limit from when it arrived, not one limit after the other",
          0.4 < took < 1.1, f"{took:.2f}s")
    gave = [json.loads(line) for line in logs.lines if '"gave_up"' in line]
    check("each is logged once, with its tries, its wait and the error",
          len(gave) == 2 and gave[0]["attempts"] > 1 and gave[0]["error"]
          and all(g["waited"] <= 0.7 for g in gave), str(gave))
    await flow.start()
    await asyncio.sleep(0.3)
    check("a message given up is not sent when the flow returns", flow.got == [], str(flow.got))
    fw.submit(FLOW, upsert("I3"))
    await until(lambda: flow.got == ["I3"])
    check("and the next message in that chat goes through as usual", flow.got == ["I3"], str(flow.got))

    print("\nFORWARD_RETRY_SECONDS=0 — one try, as before")
    flow.got.clear()
    logs.lines.clear()
    once = quick(http, retry_for=0.0)
    await flow.stop()
    once.submit(FLOW, upsert("J1"))
    await until(lambda: once.state()["pending"] == 0)
    check("it is tried once and given up", once.state()["gave_up"] == 1
          and json.loads(logs.lines[-1])["attempts"] == 1 and logs.count("retrying") == 0,
          str(logs.lines))

    print("\nThe lines are bounded")
    logs.lines.clear()
    small = quick(http, retry_for=30.0)
    small.MAX_PER_CHAT = 3
    taken = [small.submit(FLOW, upsert(f"K{n}")) for n in range(5)]
    check("past MAX_PER_CHAT a chat's new messages are dropped, the older ones kept",
          taken == [True, True, True, False, False] and small.state()["dropped"] == 2
          and small.state()["pending"] == 3, f"{taken} {small.state()}")
    check("another chat still has room", small.submit(FLOW, upsert("L1", jid=OTHER_CHAT)))
    small.MAX_PENDING = 4
    check("past MAX_PENDING nothing more is taken from anyone",
          not small.submit(FLOW, upsert("M1", jid="5511777770000@s.whatsapp.net"))
          and small.state()["dropped"] == 3)
    check("every drop is logged, without the message",
          logs.count('"forward": "dropped"') == 3, str(logs.lines))
    t = time.monotonic()
    await small.aclose()
    check("shutdown stops the waiting at once and leaves nothing pending",
          time.monotonic() - t < 0.5 and small.state()["pending"] == 0
          and not small._workers and not small._lines, f"{small.state()}")

    print("\nA failure that waiting cannot fix")
    flow.got.clear()
    logs.lines.clear()
    await flow.start()
    fw = quick(http)
    fw.submit(FLOW, {"data": {"key": {"remoteJid": CHAT, "id": "N1"}, "blob": object()}})
    fw.submit(FLOW, upsert("N2"))
    await until(lambda: fw.state()["pending"] == 0)
    check("a message that cannot be sent at all is logged and skipped, not retried",
          logs.count('"forward":"failed"') == 1 and logs.count("retrying") == 0, str(logs.lines))
    check("and the next one in that chat still arrives", flow.got == ["N2"], str(flow.got))


async def webhook_checks(flow: Flow, http: httpx.AsyncClient) -> None:
    print("\n/webhook while the flow restarts — the real route, the real forward")
    flow.got.clear()
    disp.app.state.owners = OwnerStore(None, ttl=60)
    disp.app.state.http = http
    disp.app.state.forwards = quick(http)
    transport = httpx.ASGITransport(app=disp.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://dispatcher") as client:
        await flow.stop()
        t = time.monotonic()
        r1 = await client.post("/webhook", json=upsert("W1", "@lisa agendar amanhã 13h"))
        r2 = await client.post("/webhook", json=upsert("W2", "sim"))
        r3 = await client.post("/webhook", json=upsert("W3", "pode ser", from_me=False))
        took = time.monotonic() - t
        check("his order, his 'sim' and the other person's reply each answer 200 at once",
              (r1.status_code, r2.status_code, r3.status_code) == (200, 200, 200) and took < 0.5,
              f"{took:.2f}s")
        health = (await client.get("/")).json()
        check("health shows three forwards waiting",
              (health.get("forwards") or {}).get("pending") == 3, str(health.get("forwards")))
        await asyncio.sleep(0.3)
        await flow.start()
        await until(lambda: len(flow.got) == 3)
        check("when the flow is back they arrive once each, in the order they were sent",
              flow.got == ["W1", "W2", "W3"], str(flow.got))
        health = (await client.get("/")).json()
        check("health shows none waiting and three delivered late",
              (health.get("forwards") or {}).get("pending") == 0
              and health["forwards"]["delivered_late"] == 3, str(health.get("forwards")))
        r = await client.post("/webhook", json=upsert("W4", "oi", from_me=False, jid=OTHER_CHAT))
        await asyncio.sleep(0.1)
        check("a chat with no window is still dropped, not forwarded",
              r.status_code == 200 and "W4" not in flow.got)
    await disp.app.state.forwards.aclose()


async def main() -> int:
    flow = Flow(PORT)
    await flow.start()
    logs = LogLines()
    disp.log.addHandler(logs)
    async with httpx.AsyncClient(timeout=2.0) as http:
        await delivery_checks(flow, http, logs)
        await order_checks(flow, http)
        await limit_checks(flow, http, logs)
        await webhook_checks(flow, http)
    await flow.stop()
    check("the defaults production runs with: 150 seconds, pauses up to 2",
          disp.cfg.forward_retry == 150.0 and Forwarder.BACKOFF[-1] == 2.0
          and Forwarder.RETRY_STATUS == {502, 503, 504})
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
