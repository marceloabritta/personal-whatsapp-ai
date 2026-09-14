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

    # Durable transcript cache — shares DATABASE_URL, isolated in the log schema. Best-effort:
    # if it can't open, the in-process LRU is the whole cache and the reply path is unchanged.
    tstore = None
    if s.database_url and s.transcription_enabled:
        from .cache import TranscriptStore

        tstore = TranscriptStore(s.database_url, schema=s.log_schema)
        try:
            await tstore.open()
            if deps.transcription is not None:
                deps.transcription.store = tstore
            log.info("%s", '{"boot":"transcript-cache"}')
        except Exception as exc:  # cache must never block startup
            log.warning("transcript-cache disabled: %s", exc)
            tstore = None

    # Auto-transcription roster — the rules the gate reads on every voice note. Durable tier
    # when a DB is present; otherwise the in-memory roster stands alone (dev/tests). Best-effort:
    # a roster that cannot load matches nothing, so the feature is simply inert.
    rstore = None
    if s.database_url and s.auto_transcribe_enabled:
        from .roster import RosterStore

        rstore = RosterStore(s.database_url, schema=s.log_schema)
        try:
            await rstore.open()
            deps.roster.store = rstore
            await deps.roster.refresh(force=True)
            deps.roster.start()
            log.info("%s", '{"boot":"transcribe-roster"}')
        except Exception as exc:  # the roster must never block startup
            log.warning("transcribe-roster disabled: %s", exc)
            rstore = None

    # The owner's own JID, for recognising his chat with himself (where setup runs). Configured
    # explicitly, or asked of Evolution once at boot so a fresh deployment needs no hand-copied
    # phone number. Unknown → no chat is the self-chat and setup is simply unreachable.
    if s.setup_enabled and not s.owner_jid:
        try:
            owner_jid = await deps.evolution.fetch_owner_jid()
            if owner_jid:
                deps.settings.owner_jid = owner_jid
                log.info('{"boot":"owner-jid","source":"evolution"}')
            else:
                log.warning("owner jid unknown — setup stays unreachable until OWNER_JID is set")
        except Exception as exc:
            log.warning("owner jid lookup failed: %s", exc)

    # Contact memory — the address book the calendar skill reads. Best-effort in every direction:
    # a scope that was not granted, a store that will not open, or a first sync that fails all leave
    # the directory cold, and a cold directory serves no block AND refuses every write, so Lisa
    # behaves exactly as she does today rather than re-creating people she already has.
    cstore = None
    if deps.directory is not None:
        ok, detail = await asyncio.to_thread(deps.directory.people.check_scopes)
        if not ok:
            log.warning("contacts disabled: %s", detail)
            deps.directory = None
            cal = (deps.tools or {}).get("calendar")
            if cal is not None:
                cal.directory = None
        else:
            if s.database_url:
                from .contactstore import ContactStore

                cstore = ContactStore(s.database_url, schema=s.log_schema)
                try:
                    await cstore.open()
                    deps.directory.store = cstore
                    snap = await cstore.load()
                    if snap:
                        deps.directory.load(snap["contacts"], snap["links"],
                                            snap["sync_token"], ready=bool(snap["contacts"]))
                    log.info("%s", '{"boot":"contacts-store"}')
                except Exception as exc:
                    log.warning("contacts store disabled: %s", exc)
                    cstore = None
            else:
                # No durable tier means no outbox, and a write with no record is not worth
                # making — the directory still serves reads once it has synced.
                log.info("%s", '{"boot":"contacts-memory-only"}')
            deps.directory.start()
            log.info("%s", '{"boot":"contacts-directory"}')

    # Session review — grades each turn once a session closes (app/review/). Best-effort and
    # strictly downstream: it reads the log, writes only its own tables, and nothing in the reply
    # path ever awaits it. Off unless REVIEW_ENABLED, so it ships inert to a flow that hasn't
    # opted in.
    # `store` is the LogStore: review READS its loops/events tables, so without it there is
    # nothing to review and pending_loops would query a table that does not exist.
    reviewer = reaper = None
    if store is not None and s.review_enabled:
        from .review import ReviewStore, Reviewer
        from .review.reaper import Reaper

        rstore = ReviewStore(s.database_url, schema=s.log_schema)
        try:
            await rstore.open()
            reviewer = Reviewer(s, rstore)
            reaper = Reaper(reviewer, s)
            reaper.start()
            log.info("%s", '{"boot":"session-review"}')
        except Exception as exc:  # review must never block startup
            log.warning("session-review disabled: %s", exc)
            reviewer = reaper = None

    app.state.deps = deps
    app.state.logstore = store
    app.state.reaper = reaper
    deps.reaper = reaper  # act_node offers closed loops here; None is a no-op
    app.state.graph = build_graph(deps, checkpointer)
    try:
        yield
    finally:
        if deps.directory is not None:
            await deps.directory.aclose()
        if cstore is not None:
            await cstore.aclose()
        if reaper is not None:
            await reaper.aclose()
            await reviewer.store.aclose()
        await deps.roster.aclose()
        if rstore is not None:
            await rstore.aclose()
        if tstore is not None:
            await tstore.aclose()
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
