"""Durable loop log — the Postgres sink behind `Trace`.

A "loop" is one listening window: it opens on an @mary tag and runs across every
activation until the model closes it or the 60s window times out. Each @mary tag
starts a NEW loop (same rule the checkpointer reset uses). Every loop-scoped trace
event is enqueued here and written to Postgres by a single background task, so the
three streams of a loop survive a container restart and a redeploy:

  transcript — the chat, from the 30 messages that seeded the tag through the reply
               that closed the loop (owner, contact, and Mary), deduped by message id;
  reasoning  — the model's own turn: its thinking, the enforced-JSON decision, tokens;
  control    — the machinery: gate/context/record, decisions, timings, request ids.

Two tables in a dedicated schema:
  loops  — one row per loop, UPSERTED on every activation's `record` event (started/
           updated/ended, tag trigger, activation & reply counts, last state, model,
           accumulated tokens). This is the live "persist updates" summary.
  events — append-only, one row per event, tagged by `stream`, ordered by a per-loop
           `seq`, full payload in JSONB.

Design rails (same spirit as trace.py's "never let logging break the reply path"):
  * enqueue is non-blocking — a full queue drops the record and bumps `dropped`;
  * the writer swallows every DB error — a hiccup can never delay or fail a reply;
  * secrets are REDACTED on the way IN, so nothing sensitive ever reaches disk.

psycopg is imported lazily inside open() so this module imports with no DB present
(dev, tests, the pure-function unit tests)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

log = logging.getLogger("mary.logstore")

_STOP = object()  # writer-loop shutdown sentinel
_SEQ_CAP = 8192  # bound the per-loop seq counters (freed when a loop closes; capped so
                 # loops that never emit a close — timeout / mid-turn error — can't leak)


# --- stream classification --------------------------------------------------
# `level=="user"` is the chat transcript; the model's own turn (reason) is the
# reasoning stream; everything else the code does — gate, context, record — is control.
def stream_for(rec: dict) -> str:
    if rec.get("level") == "user":
        return "transcript"
    if rec.get("node") == "reason":
        return "reasoning"
    return "control"


def label_for(rec: dict) -> Optional[str]:
    if rec.get("level") == "user":
        return rec.get("who")
    return rec.get("node")


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


# --- loop row mapping -------------------------------------------------------
# The `record` event (nodes/act.py) carries every per-activation field; this maps it
# onto a loops upsert. Kept pure + module-level so it's unit-testable without a pool.
def loop_row(rec: dict, ts: float) -> dict:
    closed = rec.get("state") == "close"
    return {
        "loop_id": rec.get("loop_id"),
        "chat_id": rec.get("chat_id"),
        "started_ts": rec.get("loop_started_ts") or ts,
        "updated_ts": ts,
        "ended_ts": ts if closed else None,
        "end_reason": "model" if closed else None,
        "trigger": rec.get("trigger"),
        "replies": 1 if rec.get("response") else 0,
        "last_loop_state": rec.get("state"),
        "provider": rec.get("provider"),
        "model": rec.get("model"),
        "prompt_version": rec.get("prompt_version"),
        "lang": rec.get("lang"),
        "last_request_id": rec.get("provider_request_id"),
        "input_tokens": rec.get("input_tokens") or 0,
        "output_tokens": rec.get("output_tokens") or 0,
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
        retention_loops_days: int = 365,
    ) -> None:
        self.dsn = dsn
        self.schema = schema
        self.retention_events_days = retention_events_days
        self.retention_loops_days = retention_loops_days
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
        await self._purge()  # best-effort trim on boot

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
        if not rec.get("loop_id"):  # only loop-scoped records are durable
            return
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

    def _next_seq(self, loop_id: str) -> int:
        seq = self._seq.get(loop_id, 0)
        self._seq[loop_id] = seq + 1
        if len(self._seq) > _SEQ_CAP:  # evict oldest so orphan loops can't leak
            self._seq.pop(next(iter(self._seq)), None)
        return seq

    async def _flush(self, batch: list[dict]) -> None:
        events, loops = [], []
        for rec in batch:
            ts = rec.get("_ts") or time.time()
            loop_id = rec.get("loop_id")
            seq = self._next_seq(loop_id)
            stream = stream_for(rec)
            payload = redact_obj({k: v for k, v in rec.items() if not k.startswith("_")})
            events.append({
                "loop_id": loop_id,
                "trace_id": rec.get("trace_id"),
                "seq": seq,
                "ts": ts,
                "stream": stream,
                "label": label_for(rec),
                "who": rec.get("who") if stream == "transcript" else None,
                "wa_message_id": rec.get("wa_id") if stream == "transcript" else None,
                "text": redact_text(rec["text"]) if (stream == "transcript" and rec.get("text")) else None,
                "payload": payload,
            })
            if rec.get("node") == "record":
                loops.append(loop_row(rec, ts))
                if rec.get("state") == "close":
                    self._seq.pop(loop_id, None)  # loop closed — free the counter
        try:
            await self._write(events, loops)
        except Exception as exc:  # a broken write must never break the reply path
            log.warning("logstore flush failed (%d events dropped): %s", len(events), exc)

    async def _write(self, events: list[dict], loops: list[dict]) -> None:
        from psycopg.types.json import Jsonb

        s = self.schema
        async with self._pool.connection() as conn:
            if events:
                rows = [(
                    e["loop_id"], e["trace_id"], e["seq"], e["ts"], e["stream"],
                    e["label"], e["who"], e["wa_message_id"], e["text"], Jsonb(e["payload"]),
                ) for e in events]
                # ON CONFLICT DO NOTHING covers the transcript (loop_id, wa_message_id)
                # dedup index; no other stream carries a unique constraint.
                await conn.cursor().executemany(
                    f"""INSERT INTO {s}.events
                        (loop_id, trace_id, seq, ts, stream, label, who, wa_message_id, text, payload)
                        VALUES (%s, %s, %s, to_timestamp(%s), %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING""",
                    rows,
                )
            for lp in loops:
                await conn.execute(
                    f"""INSERT INTO {s}.loops
                        (loop_id, chat_id, started_at, updated_at, ended_at, end_reason,
                         trigger, activations, replies, last_loop_state, provider, model,
                         prompt_version, lang, last_request_id, input_tokens, output_tokens)
                        VALUES (%(loop_id)s, %(chat_id)s, to_timestamp(%(started_ts)s),
                         to_timestamp(%(updated_ts)s), to_timestamp(%(ended_ts)s::float8),
                         %(end_reason)s, %(trigger)s, 1, %(replies)s, %(last_loop_state)s,
                         %(provider)s, %(model)s, %(prompt_version)s, %(lang)s,
                         %(last_request_id)s, %(input_tokens)s, %(output_tokens)s)
                        ON CONFLICT (loop_id) DO UPDATE SET
                         updated_at = EXCLUDED.updated_at,
                         started_at = LEAST({s}.loops.started_at, EXCLUDED.started_at),
                         ended_at = COALESCE(EXCLUDED.ended_at, {s}.loops.ended_at),
                         end_reason = COALESCE(EXCLUDED.end_reason, {s}.loops.end_reason),
                         activations = {s}.loops.activations + 1,
                         replies = {s}.loops.replies + EXCLUDED.replies,
                         last_loop_state = EXCLUDED.last_loop_state,
                         provider = EXCLUDED.provider, model = EXCLUDED.model,
                         prompt_version = EXCLUDED.prompt_version, lang = EXCLUDED.lang,
                         last_request_id = EXCLUDED.last_request_id,
                         input_tokens = {s}.loops.input_tokens + EXCLUDED.input_tokens,
                         output_tokens = {s}.loops.output_tokens + EXCLUDED.output_tokens""",
                    lp,
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
                    f"DELETE FROM {s}.loops WHERE updated_at < now() - make_interval(days => %s)",
                    (self.retention_loops_days,),
                )
        except Exception as exc:
            log.warning("logstore purge skipped: %s", exc)

    # -- reads (used by the selftest and a future /loops endpoint) -----------
    async def read_loop(self, loop_id: str, *, stream: str | None = None) -> Optional[dict]:
        """One loop: the loops row plus its events, ordered, optionally one stream."""
        from psycopg.rows import dict_row

        s = self.schema
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(f"SELECT * FROM {s}.loops WHERE loop_id = %s", (loop_id,))
                loop = await cur.fetchone()
                q = (f"SELECT seq, ts, stream, label, who, wa_message_id, text, payload "
                     f"FROM {s}.events WHERE loop_id = %s")
                params: list = [loop_id]
                if stream:
                    q += " AND stream = %s"
                    params.append(stream)
                q += " ORDER BY seq"
                await cur.execute(q, params)
                events = await cur.fetchall()
        if loop is None and not events:
            return None
        return {"loop_id": loop_id, "loop": loop, "events": events}

    # -- schema --------------------------------------------------------------
    def _ddl(self) -> str:
        s = self.schema
        return f"""
        CREATE SCHEMA IF NOT EXISTS {s};

        -- One-time migration off the pre-revert turn-log (its schema outlived the code
        -- we reverted). The old `turns` is superseded by `loops`; the old `events` lacks
        -- loop_id/who/wa_message_id/text, so CREATE IF NOT EXISTS can't reconcile it, and
        -- its inserts would fail. Both held disposable pre-revert data. Idempotent: once
        -- the new `events` (with loop_id) exists, neither branch fires again.
        DROP TABLE IF EXISTS {s}.turns;
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = '{s}' AND table_name = 'events'
                         AND column_name = 'trace_id')
               AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = '{s}' AND table_name = 'events'
                         AND column_name = 'loop_id')
            THEN
                DROP TABLE {s}.events;
            END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS {s}.loops (
            loop_id         text PRIMARY KEY,
            chat_id         text,
            started_at      timestamptz NOT NULL,
            updated_at      timestamptz NOT NULL,
            ended_at        timestamptz,
            end_reason      text,               -- 'model' | 'timeout' | null (still open)
            trigger         text,
            activations     int  NOT NULL DEFAULT 0,
            replies         int  NOT NULL DEFAULT 0,
            last_loop_state text,
            provider        text, model text, prompt_version text, lang text,
            last_request_id text,
            input_tokens    bigint NOT NULL DEFAULT 0,
            output_tokens   bigint NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS loops_chat_time ON {s}.loops (chat_id, started_at DESC);

        CREATE TABLE IF NOT EXISTS {s}.events (
            id            bigserial PRIMARY KEY,
            loop_id       text NOT NULL,
            trace_id      text,
            seq           int  NOT NULL,
            ts            timestamptz NOT NULL,
            stream        text NOT NULL,           -- transcript | reasoning | control
            label         text,
            who           text,                    -- transcript speaker
            wa_message_id text,                    -- transcript message id (dedup)
            text          text,                    -- transcript line
            payload       jsonb NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_loop ON {s}.events (loop_id, seq);
        CREATE INDEX IF NOT EXISTS events_stream ON {s}.events (loop_id, stream, seq);
        CREATE UNIQUE INDEX IF NOT EXISTS events_transcript_dedup
            ON {s}.events (loop_id, wa_message_id)
            WHERE stream = 'transcript' AND wa_message_id IS NOT NULL;
        """
