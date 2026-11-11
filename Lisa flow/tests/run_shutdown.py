"""A turn she has accepted is finished before she stops.

On 2026-11-11 he sent a tagged order one second into a deploy. The old container accepted it,
started on a web answer, and was killed ten seconds later with nothing sent: Docker's default
wait is shorter than most web turns. These checks run the real webhook and `_run` from
app/main.py under a real uvicorn server, in this process, with a stub reasoner that takes as
long as it is told to, and ask the server to stop the way SIGTERM does. They also hold the
three numbers that have to agree for the wait to exist in production: uvicorn's limit in the
Dockerfile, Docker's in compose, and how long the dispatcher keeps a message for her.

No network beyond localhost, no Postgres, no Anthropic key.

    cd "Lisa flow" && python tests/run_shutdown.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app import main  # noqa: E402
from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402

CHAT = "5511999990000@s.whatsapp.net"
OTHER_CHAT = "5511888880000@s.whatsapp.net"
_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


class SlowReasoner:
    """Answers after `delay` seconds, like a web turn that is still searching."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.started = 0
        self.finished = 0

    async def respond(self, *, system, messages, output_schema=None, server_tools=None,
                      model=None, effort=None, think=False):
        self.started += 1
        n = self.started
        await asyncio.sleep(self.delay)
        self.finished += 1
        return {"state": "keep_listening", "message": f"resposta {n}", "lang": "pt",
                "usage": {"input": 1, "output": 1}, "provider_request_id": f"req_{n}",
                "stop_reason": "end_turn", "tool_calls": [], "error_category": "none"}

    async def classify(self, *, system, messages, schema, max_tokens=32, effort="low"):
        return {"domain": "web"}


class FakeEvolution:
    instance = "secretaria"

    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, number, text, *, quoted=None):
        self.sent.append((number, text))
        return f"echo{len(self.sent)}"

    async def fetch_history(self, jid):
        return []


class LogLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def count(self, needle: str) -> int:
        return sum(needle in line for line in self.lines)


def upsert(mid: str, text: str, jid: str = CHAT) -> dict:
    return {"data": {"key": {"remoteJid": jid, "fromMe": True, "id": mid},
                     "message": {"conversation": text},
                     "messageTimestamp": int(time.time()), "pushName": "Marcelo"}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


class Running:
    """The real app under a real uvicorn server on localhost, with stub dependencies.

    `limit` is --timeout-graceful-shutdown. `stop()` is what uvicorn's SIGTERM handler does:
    it sets should_exit, and the server shuts down exactly as it does in the container."""

    def __init__(self, delay: float, limit: float | None) -> None:
        settings = Settings(evolution_apikey="x", mary_trigger_tag="@lisa",
                            loop_ttl_seconds=60, context_window_messages=30)
        self.evo = FakeEvolution()
        self.reasoner = SlowReasoner(delay)
        deps = Deps(settings=settings, evolution=self.evo, sessions=InMemorySessions(ttl=60),
                    echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=self.reasoner,
                    redis=None)
        main.app.state.deps = deps
        main.app.state.graph = build_graph(deps, MemorySaver())
        main._seen.clear()
        main._locks.clear()
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(
            main.app, host="127.0.0.1", port=self.port, lifespan="off",
            timeout_graceful_shutdown=limit, log_level="critical"))
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> "Running":
        self.task = asyncio.create_task(self.server.serve())
        await until(lambda: self.server.started)
        return self

    async def __aexit__(self, *exc) -> None:
        if not self.task.done():
            self.server.should_exit = True
            self.server.force_exit = True
            await asyncio.wait([self.task], timeout=5)

    async def post(self, body: dict) -> int:
        async with httpx.AsyncClient(timeout=2.0) as client:
            return (await client.post(f"http://127.0.0.1:{self.port}/webhook", json=body)).status_code

    def stop(self) -> None:
        self.server.should_exit = True

    async def stopped(self, timeout: float) -> bool:
        await asyncio.wait([self.task], timeout=timeout)
        return self.task.done()


async def drain_checks(logs: LogLines) -> None:
    print("\nAsked to stop with a turn in flight — the turn is finished first")
    async with Running(delay=1.0, limit=10.0) as lisa:
        t = time.monotonic()
        check("his tagged order is accepted with a 200", await lisa.post(upsert("a1", "@lisa calcule o retorno")) == 200)
        await until(lambda: lisa.reasoner.started == 1)
        lisa.stop()
        await asyncio.sleep(0.3)
        check("while she finishes, the turn is still running and nothing has been cut",
              not lisa.task.done() and lisa.reasoner.finished == 0 and lisa.evo.sent == [])
        try:
            status = await lisa.post(upsert("a2", "@lisa outra coisa"))
            refused = f"answered {status}"
        except httpx.TransportError as exc:
            refused = type(exc).__name__
        check("a webhook sent meanwhile is refused at the door, for the dispatcher to send again",
              refused == "ConnectError", refused)
        done = await lisa.stopped(timeout=5.0)
        took = time.monotonic() - t
        check("she exits only once the turn is over", done and 0.9 < took < 3.0, f"{took:.2f}s done={done}")
        check("and its reply went out", len(lisa.evo.sent) == 1 and "resposta 1" in lisa.evo.sent[0][1],
              str(lisa.evo.sent))
        check("the refused message was never started here", lisa.reasoner.started == 1)
        await asyncio.sleep(0.1)
        check("nothing is logged as cut", logs.count("turn_cut") == 0, str(logs.lines))

    print("\nTwo of his messages in one chat, both accepted before the stop")
    async with Running(delay=0.4, limit=10.0) as lisa:
        await lisa.post(upsert("b1", "@lisa primeira"))
        await lisa.post(upsert("b2", "@lisa segunda"))
        await until(lambda: lisa.reasoner.started == 1)
        lisa.stop()
        done = await lisa.stopped(timeout=5.0)
        said = [text for _, text in lisa.evo.sent]
        check("both are answered, one after the other, before she exits",
              done and len(said) == 2 and "resposta 1" in said[0] and "resposta 2" in said[1], str(said))

    print("\nAsked to stop with nothing in flight")
    async with Running(delay=0.1, limit=10.0) as lisa:
        await lisa.post(upsert("c1", "@lisa oi"))
        await until(lambda: len(lisa.evo.sent) == 1)
        t = time.monotonic()
        lisa.stop()
        done = await lisa.stopped(timeout=3.0)
        check("she exits at once: a deploy is no slower than before",
              done and time.monotonic() - t < 1.0, f"{time.monotonic() - t:.2f}s")


async def limit_checks(logs: LogLines) -> None:
    print("\nA turn that outlasts the limit")
    logs.lines.clear()
    async with Running(delay=60.0, limit=0.5) as lisa:
        await lisa.post(upsert("d1", "@lisa pesquise isso", jid=OTHER_CHAT))
        await until(lambda: lisa.reasoner.started == 1)
        t = time.monotonic()
        lisa.stop()
        done = await lisa.stopped(timeout=5.0)
        took = time.monotonic() - t
        check("she waits for the limit and then exits", done and 0.4 < took < 2.0, f"{took:.2f}s done={done}")
        check("the turn was cut: no reply went out", lisa.reasoner.finished == 0 and lisa.evo.sent == [])
        # The server returns as soon as it has cancelled the turn; the turn's own last words
        # come a moment later, as the cancellation unwinds (in the container, before exit).
        await until(lambda: logs.count("turn_cut") >= 1, timeout=2.0)
        thread = make_thread_id("secretaria", OTHER_CHAT)
        check("and that is logged, once, with the chat's thread",
              logs.count("turn_cut") == 1 and logs.count(f'"thread":"{thread}"') == 1,
              str(logs.lines))


def production_numbers() -> None:
    print("\nThe numbers production runs with agree with each other")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = {name: os.path.join(root, *parts) for name, parts in {
        "dockerfile": ("Lisa flow", "Dockerfile"),
        "compose": ("evolution", "docker-compose.yml"),
        "dispatcher": ("dispatcher", "app.py")}.items()}
    if not all(os.path.exists(p) for p in paths.values()):
        # In the image only app/ and the mounted tests/ exist; the rest of the repo is not there.
        print("  [SKIP] the Dockerfile, compose file and dispatcher are not here to read")
        return
    dockerfile = open(paths["dockerfile"]).read()
    cmd = re.search(r'^CMD (\[.*\])$', dockerfile, re.M)
    limit = re.search(r'"--timeout-graceful-shutdown", "(\d+)"', cmd.group(1) if cmd else "")
    check("uvicorn is started in exec form, so it is PID 1 and gets the SIGTERM",
          bool(cmd) and cmd.group(1).startswith('["uvicorn", "app.main:app"'), str(cmd and cmd.group(1)))
    check("with a limit on how long it waits for turns in flight", bool(limit))
    lisa_block = re.search(r"^  lisa:\n(.*?)(?=^  \w[\w-]*:\n|\Z)", open(paths["compose"]).read(), re.M | re.S)
    grace = re.search(r"^    stop_grace_period: (\d+)s$", lisa_block.group(1) if lisa_block else "", re.M)
    check("the lisa service gives her a stop_grace_period", bool(grace))
    retry = re.search(r'FORWARD_RETRY_SECONDS", "(\d+)"', open(paths["dispatcher"]).read())
    check("the dispatcher has a default for how long it keeps a message", bool(retry))
    if not (limit and grace and retry):
        return
    limit_s, grace_s, retry_s = int(limit.group(1)), int(grace.group(1)), int(retry.group(1))
    check("her limit covers the longest turn seen in a month of the log (81 s)", limit_s >= 90, f"{limit_s}s")
    check("Docker waits longer than she does, with time left to close her stores",
          grace_s >= limit_s + 15, f"grace {grace_s}s, limit {limit_s}s")
    check("the dispatcher keeps a message longer than her whole stop plus her boot",
          retry_s >= grace_s + 20, f"retry {retry_s}s, grace {grace_s}s")


async def amain() -> int:
    logs = LogLines()
    main.log.addHandler(logs)
    await drain_checks(logs)
    await limit_checks(logs)
    production_numbers()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
