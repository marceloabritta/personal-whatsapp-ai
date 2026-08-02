"""FastAPI entry — the webhook Evolution POSTs every message to.

Replies 200 immediately, then runs the graph in the background, scoped to the chat's
checkpoint thread. Correctness rails: message-id idempotency + a per-thread lock so
two fast messages in one chat can't race the checkpoint or the window."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from .deps import build_deps
from .graph import build_graph
from .logstore import token_ok
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

    logstore = None
    if deps.settings.database_url:
        from .logstore import LogStore

        logstore = LogStore(
            deps.settings.database_url,
            schema=deps.settings.log_schema,
            queue_max=deps.settings.log_queue_max,
            retention_events_days=deps.settings.log_retention_events_days,
            retention_turns_days=deps.settings.log_retention_turns_days,
        )
        await logstore.open()
        logstore.start()
        deps.trace.attach_sink(logstore)
        log.info("%s", '{"boot":"logstore-postgres"}')

    app.state.deps = deps
    app.state.logstore = logstore  # None when DATABASE_URL unset; guarded in the read API
    app.state.graph = build_graph(deps, checkpointer)
    try:
        yield
    finally:
        if logstore is not None:
            await logstore.aclose()
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


# --- Read API (P3) — inspect the durable log without SSH/psql. Both carry PII, so both
#     require a bearer token (LOG_API_TOKEN); with it unset the endpoints refuse (503). ---
def _require_log_api(request: Request):
    deps = request.app.state.deps
    ls = request.app.state.logstore
    if ls is None or not deps.settings.log_api_token:
        raise HTTPException(status_code=503, detail="log API not configured")
    if not token_ok(request.headers.get("authorization", ""), deps.settings.log_api_token):
        raise HTTPException(status_code=401, detail="unauthorized")
    return ls


@app.get("/trace/{trace_id}")
async def read_trace(trace_id: str, request: Request, stream: str | None = None) -> dict:
    """One activation, replayed: the turn summary + its ordered events across the three
    streams (optionally filtered to one via ?stream=transcript|reasoning|control)."""
    ls = _require_log_api(request)
    data = await ls.read_turn(trace_id, stream=stream)
    if data is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return data


@app.get("/turns")
async def read_turns(
    request: Request, chat: str | None = None, since: str | None = None, limit: int = 50
) -> dict:
    """Recent activations, newest first — the 'all calls that ran' view. Filter by
    ?chat=<jid> and/or ?since=<ISO timestamp>; ?limit caps the page (max 500)."""
    ls = _require_log_api(request)
    return {"turns": await ls.read_turns(chat=chat, since=since, limit=limit)}


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    body = await request.json()
    data = body.get("data") or {}
    key = data.get("key") or {}
    msg_id = key.get("id")

    chat_jid = key.get("remoteJid") or ""
    deps = request.app.state.deps

    if _already_seen(msg_id):
        # A webhook retry we've already run — record the drop under a stable id so the
        # duplicate is visible in the control stream, then ack.
        deps.trace.code(f"dedup-{msg_id}", node="webhook", event="dup_drop",
                        chat_id=chat_jid, msg_id=msg_id)
        return Response(status_code=200)

    number = chat_jid.split("@")[0]
    trace_id = deps.trace.start(number)  # minted here so the contextvar can carry it
    thread_id = make_thread_id(deps.settings.evolution_instance, chat_jid)
    background.add_task(_run, request.app, body, thread_id, trace_id)
    return Response(status_code=200)


async def _run(app: FastAPI, body: dict, thread_id: str, trace_id: str) -> None:
    # Set the trace id on the context BEFORE ainvoke, so every graph node task inherits
    # it (copy_context at task creation) and the IO clients can trace through it.
    from .trace import current_trace_id

    current_trace_id.set(trace_id)
    deps = app.state.deps
    lock = _locks.setdefault(thread_id, asyncio.Lock())
    async with lock:
        try:
            config = {"configurable": {"thread_id": thread_id}}
            await app.state.graph.ainvoke({"raw": body, "trace_id": trace_id}, config=config)
        except Exception as exc:  # a bad payload must never crash the worker
            log.exception("graph run failed")
            deps.trace.code(trace_id, node="webhook", event="run_failed", error=str(exc))
