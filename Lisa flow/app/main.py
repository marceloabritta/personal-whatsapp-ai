"""FastAPI entry — the webhook Evolution POSTs every message to.

Replies 200 immediately, then runs the graph in the background, scoped to the chat's
checkpoint thread. Correctness rails: message-id idempotency + a per-thread lock so
two fast messages in one chat can't race the checkpoint or the window."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response

from .deps import build_deps
from .graph import build_graph
from .threads import make_thread_id

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("mary.webhook")


@asynccontextmanager
async def lifespan(app: FastAPI):
    deps = build_deps()
    cp_cm = None
    if deps.settings.database_url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        cp_cm = AsyncPostgresSaver.from_conn_string(deps.settings.database_url)
        checkpointer = await cp_cm.__aenter__()
        await checkpointer.setup()  # idempotent; creates the checkpoint tables
        log.info("%s", '{"boot":"postgres-checkpointer"}')
    else:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
        log.info("%s", '{"boot":"in-memory-checkpointer"}')

    # Durable loop log — shares DATABASE_URL, isolated in its own schema. Best-effort:
    # if it can't open, the reply path runs exactly as before (stdout + ring only).
    store = None
    s = deps.settings
    if s.database_url and s.log_enabled:
        from .logstore import LogStore

        store = LogStore(
            s.database_url, schema=s.log_schema, queue_max=s.log_queue_max,
            retention_events_days=s.log_retention_events_days,
            retention_loops_days=s.log_retention_loops_days,
        )
        try:
            await store.open()
            store.start()
            deps.trace.attach_sink(store)
            log.info("%s", '{"boot":"loop-logstore"}')
        except Exception as exc:  # logging must never block startup
            log.warning("loop-logstore disabled: %s", exc)
            store = None

    app.state.deps = deps
    app.state.logstore = store
    app.state.graph = build_graph(deps, checkpointer)
    try:
        yield
    finally:
        if store is not None:
            await store.aclose()
        if cp_cm is not None:
            await cp_cm.__aexit__(None, None, None)


app = FastAPI(title="Mary brain", version="0.2.0", lifespan=lifespan)

# Message-id idempotency (webhook retries). Bounded LRU, per process.
_seen: "OrderedDict[str, None]" = OrderedDict()
_SEEN_MAX = 2000
# Per-thread serialization so activations in one chat never race.
_locks: dict[str, asyncio.Lock] = {}


def _already_seen(msg_id: str | None) -> bool:
    if not msg_id:
        return False
    if msg_id in _seen:
        return True
    _seen[msg_id] = None
    if len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return False


@app.get("/")
async def health(request: Request) -> dict:
    deps = request.app.state.deps
    return {
        "ok": True,
        "service": "mary-brain",
        "version": "0.2.0",
        "tags": deps.settings.tags,
        "model": deps.settings.claude_model,
    }


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    body = await request.json()
    data = body.get("data") or {}
    key = data.get("key") or {}
    msg_id = key.get("id")

    if _already_seen(msg_id):
        return Response(status_code=200)

    chat_jid = key.get("remoteJid") or ""
    deps = request.app.state.deps
    thread_id = make_thread_id(deps.settings.evolution_instance, chat_jid)
    background.add_task(_run, request.app, body, thread_id)
    return Response(status_code=200)


async def _run(app: FastAPI, body: dict, thread_id: str) -> None:
    lock = _locks.setdefault(thread_id, asyncio.Lock())
    async with lock:
        try:
            config = {"configurable": {"thread_id": thread_id}}
            await app.state.graph.ainvoke({"raw": body}, config=config)
        except Exception:  # a bad payload must never crash the worker
            log.exception("graph run failed")
