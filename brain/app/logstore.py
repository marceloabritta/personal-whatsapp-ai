"""Durable turn log — the Postgres sink behind `Trace`.

Every `trace.code` / `trace.user` event is enqueued here and written to Postgres by a
single background task, so the three streams of every activation — the chat transcript,
the AI reasoning, and the programmatic control flow — survive a container restart and a
redeploy (unlike stdout + the in-memory ring, which don't).

Two tables in a dedicated schema:
  turns   — one row per activation, upserted from the `record` event (the "all calls
            that ran" view: model, tokens, latency, loop_state, delivery, request id…).
  events  — append-only, one row per event, tagged by `stream`, full payload in JSONB.

Design rails (same spirit as trace.py's "never let logging break the reply path"):
  * enqueue is non-blocking — a full queue drops the record and bumps `dropped`;
  * the writer swallows every DB error — a hiccup can never delay or fail a reply;
  * secrets are REDACTED on the way IN, so nothing sensitive ever reaches disk.

psycopg is imported lazily inside open() so this module imports with no DB present
(dev, tests, the pure-function unit tests)."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from typing import Any, Optional

log = logging.getLogger("mary.logstore")


def token_ok(auth_header: str, configured: str) -> bool:
    """Constant-time check of an `Authorization: Bearer <token>` header against the
    configured read-API token. False if either side is empty or the scheme is wrong —
    so an unset token can never authorise (the endpoint denies before reaching here)."""
    if not configured:
        return False
    prefix = "Bearer "
    if not auth_header or not auth_header.startswith(prefix):
        return False
    return hmac.compare_digest(auth_header[len(prefix):], configured)

_STOP = object()  # writer-loop shutdown sentinel
_SEQ_CAP = 4096  # bound the per-turn seq counters (freed on `record`; capped for turns
                 # that never emit one — webhook dup_drop / run_failed / mid-turn errors)

# --- stream classification --------------------------------------------------
# Which of the three streams an event belongs to. `level=="user"` is the transcript;
# the model's own turn (reason) and the turn summary (record) are the reasoning stream;
# everything else the code does — gate, context, execute, io, webhook — is control.
_REASONING_NODES = {"reason", "record"}


def stream_for(rec: dict) -> str:
    if rec.get("level") == "user":
        return "transcript"
    if rec.get("node") in _REASONING_NODES:
        return "reasoning"
    return "control"


def label_for(rec: dict) -> Optional[str]:
    if rec.get("level") == "user":
        return rec.get("who")
    return rec.get("api") or rec.get("node")


# --- redaction (ported from secretary/1. Orchestrator/lib/logbuffer.js) ------
# Defence in depth: the store is private, but a secret must never enter it in the first
# place. Specific, well-labelled patterns run first; the blunt high-entropy sweep runs
# LAST so it doesn't mask what a named pattern would have caught. An over-redacted log
# line is a cost we happily pay for a leaked key we don't.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "«redacted:anthropic-key»"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "«redacted:google-key»"),
    (re.compile(r"1//[A-Za-z0-9_-]{20,}"), "«redacted:google-refresh-token»"),
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer «redacted»"),
    (re.compile(r"((?:api[-_]?key|apikey|authorization|token|secret|password)\s*[:=]\s*)\S+",
                re.IGNORECASE), r"\1«redacted»"),
    (re.compile(r"\b[A-Za-z0-9_-]{60,}\b"), "«redacted:blob»"),
]


def redact_text(text: str) -> str:
    s = str(text)
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s


def redact_obj(obj: Any) -> Any:
    """Redact string leaves anywhere in a dict/list/scalar, structure preserved."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj


# --- turn row mapping -------------------------------------------------------
# The `record` event (nodes/act.py) already carries every per-turn field; this maps it
# 1:1 onto a turns row. Kept pure + module-level so it's unit-testable without a pool.
_TURN_FIELDS = (
    "provider", "model", "prompt_version", "provider_request_id",
    "latency_ms", "stop_reason", "loop_state", "close_reason", "lang",
    "workflow_task", "delivery_result", "error_category",
)


def turn_row(rec: dict, ts: float) -> dict:
    usage = None  # tokens are already flattened onto the record event
    return {
        "trace_id": rec.get("trace_id"),
        "chat_id": rec.get("chat_id"),
        "ts": ts,
        "trigger": rec.get("trigger"),
        "input_tokens": rec.get("input_tokens"),
        "output_tokens": rec.get("output_tokens"),
        "actions": rec.get("actions") or [],
        "response": rec.get("response"),
        **{f: rec.get(f) for f in _TURN_FIELDS},
    }


class LogStore:
    """Owns the pool, the queue, and the writer task. One instance per process,
    attached to the shared Trace in the FastAPI lifespan."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "mary_log",
        queue_max: int = 10_000,
        batch_max: int = 200,
        retention_events_days: int = 90,
        retention_turns_days: int = 365,
    ) -> None:
        self.dsn = dsn
        self.schema = schema
        self.retention_events_days = retention_events_days
        self.retention_turns_days = retention_turns_days
        self._batch_max = batch_max
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self._pool: Any = None
        self._task: Optional[asyncio.Task] = None
        self._seq: dict[str, int] = {}
        self.dropped = 0

    # -- lifecycle -----------------------------------------------------------
    async def open(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=4, open=False)
        await self._pool.open()
        async with self._pool.connection() as conn:
            await conn.execute(self._ddl())
        await self._purge()  # best-effort trim on boot; P4 will schedule it

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._writer_loop())

    async def aclose(self) -> None:
        if self._task is not None:
            await self._queue.put(_STOP)  # drain what's buffered, then exit
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, Exception):  # never hang shutdown
                self._task.cancel()
        if self._pool is not None:
            await self._pool.close()

    # -- ingest (called from Trace, hot path) --------------------------------
    def enqueue(self, rec: dict) -> None:
        """Non-blocking. A full queue drops the record — logging never stalls a reply."""
        rec["_ts"] = time.time()
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            self.dropped += 1

    # -- writer --------------------------------------------------------------
    async def _writer_loop(self) -> None:
        while True:
            rec = await self._queue.get()
            if rec is _STOP:
                return
            batch = [rec]
            while len(batch) < self._batch_max:
                try:
                    nxt = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is _STOP:
                    await self._flush(batch)
                    return
                batch.append(nxt)
            await self._flush(batch)

    async def _flush(self, batch: list[dict]) -> None:
        events, turns = [], []
        for rec in batch:
            ts = rec.get("_ts") or time.time()
            tid = rec.get("trace_id") or "unknown"
            seq = self._seq.get(tid, 0)
            self._seq[tid] = seq + 1
            if len(self._seq) > _SEQ_CAP:  # evict the oldest so orphan turns can't leak
                self._seq.pop(next(iter(self._seq)), None)
            payload = redact_obj({k: v for k, v in rec.items() if not k.startswith("_")})
            events.append((
                tid, rec.get("chat_id") or rec.get("chat"), seq, ts,
                stream_for(rec), label_for(rec), payload,
            ))
            if rec.get("node") == "record":
                turns.append(turn_row(rec, ts))
                self._seq.pop(tid, None)  # turn closed — free the counter
        try:
            await self._write(events, turns)
        except Exception as exc:  # a broken write must never break the reply path
            log.warning("logstore flush failed (%d events dropped): %s", len(events), exc)

    async def _write(self, events: list[tuple], turns: list[dict]) -> None:
        from psycopg.types.json import Jsonb

        s = self.schema
        async with self._pool.connection() as conn:
            if events:
                rows = [(t, c, q, ts, st, lb, Jsonb(pl)) for (t, c, q, ts, st, lb, pl) in events]
                await conn.cursor().executemany(
                    f"INSERT INTO {s}.events "
                    "(trace_id, chat_id, seq, ts, stream, label, payload) "
                    "VALUES (%s, %s, %s, to_timestamp(%s), %s, %s, %s)",
                    rows,
                )
            for tr in turns:
                await conn.execute(
                    f"""INSERT INTO {s}.turns
                        (trace_id, chat_id, started_at, ended_at, trigger, provider, model,
                         prompt_version, provider_request_id, input_tokens, output_tokens,
                         latency_ms, stop_reason, loop_state, close_reason, lang, actions,
                         workflow_task, response, delivery_result, error_category)
                        VALUES (%(trace_id)s, %(chat_id)s, to_timestamp(%(ts)s), to_timestamp(%(ts)s),
                         %(trigger)s, %(provider)s, %(model)s, %(prompt_version)s,
                         %(provider_request_id)s, %(input_tokens)s, %(output_tokens)s,
                         %(latency_ms)s, %(stop_reason)s, %(loop_state)s, %(close_reason)s,
                         %(lang)s, %(actions)s, %(workflow_task)s, %(response)s,
                         %(delivery_result)s, %(error_category)s)
                        ON CONFLICT (trace_id) DO UPDATE SET
                         ended_at = EXCLUDED.ended_at,
                         started_at = LEAST({s}.turns.started_at, EXCLUDED.started_at),
                         provider = EXCLUDED.provider, model = EXCLUDED.model,
                         prompt_version = EXCLUDED.prompt_version,
                         provider_request_id = EXCLUDED.provider_request_id,
                         input_tokens = EXCLUDED.input_tokens, output_tokens = EXCLUDED.output_tokens,
                         latency_ms = EXCLUDED.latency_ms, stop_reason = EXCLUDED.stop_reason,
                         loop_state = EXCLUDED.loop_state, close_reason = EXCLUDED.close_reason,
                         lang = EXCLUDED.lang, actions = EXCLUDED.actions,
                         workflow_task = EXCLUDED.workflow_task, response = EXCLUDED.response,
                         delivery_result = EXCLUDED.delivery_result,
                         error_category = EXCLUDED.error_category""",
                    {**tr, "actions": Jsonb(tr["actions"])},
                )

    async def _purge(self) -> None:
        s = self.schema
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"DELETE FROM {s}.events WHERE ts < now() - make_interval(days => %s)",
                    (self.retention_events_days,),
                )
                await conn.execute(
                    f"DELETE FROM {s}.turns WHERE ended_at < now() - make_interval(days => %s)",
                    (self.retention_turns_days,),
                )
        except Exception as exc:
            log.warning("logstore purge skipped: %s", exc)

    # -- reads (P3 read API) -------------------------------------------------
    async def read_turn(self, trace_id: str, *, stream: str | None = None) -> Optional[dict]:
        """One activation: the turns row (may be None for a control-only trace like a
        dedup drop) plus its events, ordered. Optional single-stream filter."""
        from psycopg.rows import dict_row

        s = self.schema
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(f"SELECT * FROM {s}.turns WHERE trace_id = %s", (trace_id,))
                turn = await cur.fetchone()
                q = (f"SELECT seq, ts, stream, label, payload FROM {s}.events "
                     "WHERE trace_id = %s")
                params: list = [trace_id]
                if stream:
                    q += " AND stream = %s"
                    params.append(stream)
                q += " ORDER BY seq"
                await cur.execute(q, params)
                events = await cur.fetchall()
        if turn is None and not events:
            return None
        return {"trace_id": trace_id, "turn": turn, "events": events}

    async def read_turns(
        self, *, chat: str | None = None, since: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Recent activations, newest first — the 'all calls that ran' view."""
        from psycopg.rows import dict_row

        s = self.schema
        limit = max(1, min(limit, 500))  # bound the page
        q = f"SELECT * FROM {s}.turns"
        clauses: list = []
        params: list = []
        if chat:
            clauses.append("chat_id = %s")
            params.append(chat)
        if since:
            clauses.append("started_at >= %s::timestamptz")
            params.append(since)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY started_at DESC LIMIT %s"
        params.append(limit)
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(q, params)
                return await cur.fetchall()

    # -- schema --------------------------------------------------------------
    def _ddl(self) -> str:
        s = self.schema
        return f"""
        CREATE SCHEMA IF NOT EXISTS {s};

        CREATE TABLE IF NOT EXISTS {s}.turns (
            trace_id      text PRIMARY KEY,
            chat_id       text,
            started_at    timestamptz NOT NULL,
            ended_at      timestamptz,
            trigger       text,
            provider      text, model text, prompt_version text,
            provider_request_id text,
            input_tokens  int, output_tokens int, latency_ms int,
            stop_reason   text,
            loop_state    text, close_reason text,
            lang          text, actions jsonb, workflow_task text,
            response      text, delivery_result text, error_category text
        );
        CREATE INDEX IF NOT EXISTS turns_chat_time ON {s}.turns (chat_id, started_at DESC);

        CREATE TABLE IF NOT EXISTS {s}.events (
            id       bigserial PRIMARY KEY,
            trace_id text NOT NULL,
            chat_id  text,
            seq      int NOT NULL,
            ts       timestamptz NOT NULL,
            stream   text NOT NULL,
            label    text,
            payload  jsonb NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_trace  ON {s}.events (trace_id, seq);
        CREATE INDEX IF NOT EXISTS events_stream ON {s}.events (chat_id, stream, ts DESC);
        """
