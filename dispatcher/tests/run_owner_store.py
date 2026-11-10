"""The dispatcher keeps routing when Redis is down, restarting or silent.

Redis only remembers which flow owns a chat's open window. It used to be asked before the
forward was queued, with nothing catching a failure, so with REDIS_URL set and Redis down
every webhook answered 500 and the message never reached the flow. These checks drive the real
OwnerStore and the real /webhook route against a Redis that refuses connections, one that
accepts and never answers, and a stand-in that can be switched off and on. No Redis server, no
network beyond localhost, no flow running.

    pip install -r requirements.txt && python tests/run_owner_store.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _name in ("REDIS_URL", "AUTO_TRANSCRIBE_URL", "LISA_URL", "LISA_TAGS"):
    os.environ.pop(_name, None)  # the app reads its routes from the environment at import

import httpx  # noqa: E402

import app as disp  # noqa: E402
from app import OwnerStore  # noqa: E402

logging.getLogger().handlers = [logging.NullHandler()]  # keep the route log out of the results

LISA = disp.cfg.routes[0][1]
REFUSED = "redis://127.0.0.1:1/4"  # nothing listens on port 1: the connection is refused
CHAT = "5511999990000@s.whatsapp.net"
OTHER_CHAT = "5511888880000@s.whatsapp.net"

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


class FakeRedis:
    """Stands in for redis.asyncio.Redis: a dict with expiry that can be switched off."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, float]] = {}
        self.down = False
        self.calls = 0

    def _call(self) -> None:
        self.calls += 1
        if self.down:
            raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def ping(self) -> bool:
        self._call()
        return True

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._call()
        self.data[key] = (value, time.time() + (ex or 0))
        return True

    async def get(self, key: str) -> str | None:
        self._call()
        value, expiry = self.data.get(key, (None, 0.0))
        return value if value and time.time() < expiry else None


class LogLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def count(self, needle: str) -> int:
        return sum(needle in line for line in self.lines)


def fake_store(ttl: int = 60) -> tuple[OwnerStore, FakeRedis]:
    store, fake = OwnerStore(None, ttl), FakeRedis()
    store._redis = fake
    return store, fake


def pause_over(store: OwnerStore) -> None:
    """As if REDIS_RETRY_AFTER had passed since the last failure."""
    store._skip_until = 0.0


async def healthy_checks() -> None:
    print("\nRedis healthy — it stays the first answer")
    store, fake = fake_store(ttl=60)
    await store.set(CHAT, LISA)
    key = OwnerStore._key(CHAT)
    check("a window is written to Redis under the same key, with the TTL",
          key == f"disp:owner:{CHAT}" and fake.data.get(key, ("", 0))[0] == LISA
          and 58 < fake.data[key][1] - time.time() <= 60)
    check("and to memory", store._mem.get(CHAT, ("", 0))[0] == LISA)
    check("memory's copy lapses no later than Redis's", store._mem[CHAT][1] <= fake.data[key][1])
    fake.data[key] = ("http://from-redis/webhook", fake.data[key][1])
    check("a read asks Redis first", await store.get(CHAT) == "http://from-redis/webhook")
    check("a chat with no window has none", await store.get(OTHER_CHAT) is None)
    check("state says redis", store.state() == "redis" and await store.ping() == "redis")

    print("\nNo Redis configured — memory only, as before")
    mem = OwnerStore(None, ttl=60)
    await mem.set(CHAT, LISA)
    check("a window is kept and read back", await mem.get(CHAT) == LISA and mem.state() == "memory")
    mem._mem[CHAT] = (LISA, time.time() - 1)
    check("a lapsed window is gone", await mem.get(CHAT) is None and CHAT not in mem._mem)


async def outage_checks() -> None:
    print("\nRedis goes down mid-conversation, then comes back")
    logs = LogLines()
    disp.log.addHandler(logs)
    store, fake = fake_store(ttl=60)
    await store.set(CHAT, LISA)  # opened while Redis was healthy
    fake.down = True
    check("a read does not raise and memory answers", await store.get(CHAT) == LISA)
    check("state says memory_redis_down", store.state() == "memory_redis_down")

    calls = fake.calls
    await store.set(OTHER_CHAT, LISA)  # opened during the outage
    await store.get(CHAT)
    await store.get(OTHER_CHAT)
    check("after a failure Redis is left alone", fake.calls == calls, f"{fake.calls - calls} calls")
    check("a window opened during the outage is held in memory", await store.get(OTHER_CHAT) == LISA)

    pause_over(store)
    await store.get(CHAT)
    check("once the pause is over Redis is tried again", fake.calls == calls + 1)
    for _ in range(3):
        pause_over(store)
        await store.get(CHAT)
    check("a whole outage logs one redis_down line", logs.count("redis_down") == 1,
          f"{logs.count('redis_down')} lines")

    fake.down = False
    check("until the pause is over, a Redis that is back is not asked yet",
          await store.get(CHAT) == LISA and store.state() == "memory_redis_down")
    pause_over(store)
    check("Redis is back: the window opened during the outage still answers",
          await store.get(OTHER_CHAT) == LISA and OwnerStore._key(OTHER_CHAT) not in fake.data)
    check("state says redis again, with one redis_back line",
          store.state() == "redis" and logs.count("redis_back") == 1)
    await store.set(OTHER_CHAT, LISA)
    check("its next refresh puts it in Redis", fake.data.get(OwnerStore._key(OTHER_CHAT), ("", 0))[0] == LISA)

    store._mem[CHAT] = (LISA, time.time() - 1)
    fake.data.pop(OwnerStore._key(CHAT))
    check("a window that lapsed in both is gone", await store.get(CHAT) is None)
    disp.log.removeHandler(logs)

    print("\nMemory does not grow without bound")
    small, _ = fake_store(ttl=60)
    small.MEM_SWEEP_AT = 3
    for n in range(3):
        small._mem[f"old{n}"] = (LISA, time.time() - 1)
    await small.set(CHAT, LISA)
    check("lapsed windows are swept out on a write", list(small._mem) == [CHAT], str(list(small._mem)))


async def real_client_checks() -> None:
    print("\nThe real Redis client against a dead Redis")
    store = OwnerStore(REFUSED, ttl=60)
    check("a client is built without connecting", store._redis is not None and store.state() == "redis")
    t = time.monotonic()
    try:
        await store.set(CHAT, LISA)
        url = await store.get(CHAT)
        raised = ""
    except Exception as exc:  # the bug: this used to reach the webhook
        url, raised = None, f"{type(exc).__name__}: {exc}"
    took = time.monotonic() - t
    check("connection refused: set and get do not raise", not raised, raised)
    check("and the window is answered from memory, at once", url == LISA and took < 0.5, f"{took:.2f}s")
    check("ping reports memory_redis_down", await store.ping() == "memory_redis_down")

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)  # accept, then never answer: a frozen or overloaded Redis

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    store = OwnerStore(f"redis://127.0.0.1:{port}/4", ttl=60)
    t = time.monotonic()
    try:
        await store.set(CHAT, LISA)
        raised = ""
    except Exception as exc:
        raised = f"{type(exc).__name__}: {exc}"
    first = time.monotonic() - t
    check("a Redis that never answers: the write does not raise", not raised, raised)
    check("and costs about REDIS_TIMEOUT, not the client's default",
          0.5 < first < OwnerStore.REDIS_TIMEOUT + 1.0, f"{first:.2f}s")
    t = time.monotonic()
    url = await store.get(CHAT)
    check("the next message does not wait at all", url == LISA and time.monotonic() - t < 0.1,
          f"{time.monotonic() - t:.2f}s")
    server.close()


def upsert(text: str, from_me: bool, jid: str = CHAT) -> dict:
    return {"event": "messages.upsert", "instance": "secretaria",
            "data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": "MSG"},
                     "message": {"conversation": text}}}


async def webhook_checks() -> None:
    forwards: list[str] = []

    async def record_forward(url: str, body: dict) -> None:
        forwards.append(url)

    disp._forward = record_forward
    # raise_app_exceptions=False: an unhandled error becomes the 500 Evolution would see.
    transport = httpx.ASGITransport(app=disp.app, raise_app_exceptions=False)

    async def post(client: httpx.AsyncClient, body: dict) -> tuple[int, int]:
        forwards.clear()
        r = await client.post("/webhook", json=body)
        return r.status_code, len(forwards)

    print("\n/webhook with Redis refusing connections")
    disp.app.state.owners = OwnerStore(REFUSED, ttl=60)
    async with httpx.AsyncClient(transport=transport, base_url="http://dispatcher") as client:
        check("his tagged message answers 200 and is forwarded once",
              await post(client, upsert("@lisa marca um café amanhã", True)) == (200, 1))
        check("the contact's reply in that chat follows the window",
              await post(client, upsert("pode ser às 10h", False)) == (200, 1))
        check("his own untagged follow-up does too",
              await post(client, upsert("fechado", True)) == (200, 1))
        check("a chat with no window is dropped, with a 200",
              await post(client, upsert("oi", False, OTHER_CHAT)) == (200, 0))
        check("a contact typing the tag does not open a window",
              await post(client, upsert("@lisa oi", False, OTHER_CHAT)) == (200, 0))
        health = (await client.get("/")).json()
        check("health reports the store as memory_redis_down",
              health.get("owner_store") == "memory_redis_down", str(health.get("owner_store")))

    print("\n/webhook with Redis healthy — routing unchanged")
    store, fake = fake_store(ttl=60)
    disp.app.state.owners = store
    async with httpx.AsyncClient(transport=transport, base_url="http://dispatcher") as client:
        check("a tagged message is forwarded and the window is in Redis",
              await post(client, upsert("@lisa oi", True)) == (200, 1)
              and fake.data.get(OwnerStore._key(CHAT), ("", 0))[0] == LISA)
        check("an untagged reply follows it", await post(client, upsert("oi", False)) == (200, 1))
        before = fake.data[OwnerStore._key(CHAT)][1]
        fake.data[OwnerStore._key(CHAT)] = (LISA, before - 30)
        echo = f"*[Assistente IA do {disp.cfg.owner_name}]:* Feito."
        check("her own echoed reply is forwarded but does not refresh the window",
              await post(client, upsert(echo, True)) == (200, 1)
              and fake.data[OwnerStore._key(CHAT)][1] == before - 30)
        check("a chat with no window is dropped",
              await post(client, upsert("oi", False, OTHER_CHAT)) == (200, 0))
        check("health reports the store as redis", (await client.get("/")).json().get("owner_store") == "redis")


async def main() -> int:
    await healthy_checks()
    await outage_checks()
    await real_client_checks()
    await webhook_checks()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
