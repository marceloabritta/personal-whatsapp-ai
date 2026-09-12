"""The review layer's own tables, and the reads that feed the judge.

Three tables in the existing log schema. The review layer NEVER writes to `loops` or `events` —
it only reads them, so a bug here cannot corrupt the conversation log it is reviewing.

The reconstruction in `load_turns` leans on one property of the log: `events.seq` is a single
counter per loop shared across all three streams, so a `record` at seq N sits in exact position
against the transcript lines around it. "What could she see when she spoke" is a `seq < N` filter,
not a timestamp guess."""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("mary.review.store")

_AI = "AI Assistant"
# How the context node labels a line it transcribed, e.g. "Marcelo (voice message — transcribed)".
# The clip's own id is what lisa_log.transcripts is keyed on.
_VOICE = "voice message"


def _is_human(who: Optional[str]) -> bool:
    """Transcript speakers carry suffixes — "Marcelo (image)", "Caio (voice message —
    transcribed)". Only the assistant's own lines are not human."""
    return not (who or "").startswith(_AI)


class ReviewStore:
    """Owns a pool against the same database as the log. Opened lazily so importing this
    module needs no DB (dev, unit tests)."""

    def __init__(self, dsn: str, *, schema: str = "mary_log") -> None:
        self.dsn = dsn
        self.schema = schema
        self._pool: Any = None

    async def open(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=4, open=False)
        await self._pool.open()
        async with self._pool.connection() as conn:
            await conn.execute(self._ddl())

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- reads ---------------------------------------------------------------
    async def pending_loops(
        self, *, judge_version: str, settle_seconds: float, batch: int,
        scope_version: str | None = None,
    ) -> list[str]:
        """Loops that have gone quiet and carry no review at this rubric.

        `settle_seconds` is the window TTL plus a grace margin — long enough that the window has
        truly expired AND the log writer has flushed that loop's last events to disk."""
        from psycopg.rows import dict_row

        s = self.schema
        q = f"""
            SELECT l.loop_id
              FROM {s}.loops l
              LEFT JOIN {s}.loop_reviews r
                     ON r.loop_id = l.loop_id AND r.judge_version = %(jv)s
             WHERE r.loop_id IS NULL
               AND l.updated_at < now() - make_interval(secs => %(settle)s)
               {"AND l.prompt_version = %(scope)s" if scope_version else ""}
             ORDER BY l.updated_at
             LIMIT %(batch)s
        """
        params = {"jv": judge_version, "settle": settle_seconds, "batch": batch,
                  "scope": scope_version}
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(q, params)
                return [r["loop_id"] for r in await cur.fetchall()]

    async def load_turns(self, loop_id: str) -> list[dict]:
        """Every turn in one loop, each with the chat as it stood and the facts timing needs.

        One ordered fetch, assembled in Python — the alignment is a running scan over seq, which
        is far clearer here than as correlated subqueries."""
        from psycopg.rows import dict_row

        s = self.schema
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""SELECT seq, ts, stream, label, who, wa_message_id, text,
                                 trace_id, payload
                          FROM {s}.events WHERE loop_id = %s ORDER BY seq""",
                    (loop_id,),
                )
                events = await cur.fetchall()
                await cur.execute(
                    f"SELECT chat_id, prompt_version FROM {s}.loops WHERE loop_id = %s",
                    (loop_id,),
                )
                loop = await cur.fetchone() or {}

        # The loop's domain, from the route node. One loop sticks to one domain, so the first
        # route event describes the whole loop.
        domain = next(
            (e["payload"].get("domain") for e in events
             if e["label"] == "route" and (e["payload"] or {}).get("domain")),
            None,
        )

        lines: list[dict] = []          # transcript so far, oldest first
        last_human: Optional[dict] = None
        last_audio_id: Optional[str] = None
        turns: list[dict] = []

        for e in events:
            if e["stream"] == "transcript":
                line = {
                    "seq": e["seq"], "who": e["who"], "text": e["text"],
                    "wa_id": e["wa_message_id"],
                    "wa_ts": _int_or_none((e["payload"] or {}).get("ts")),
                    "trace_id": e["trace_id"],
                }
                lines.append(line)
                if _is_human(e["who"]) and line["wa_ts"]:
                    last_human = line
                # The voice note is usually the message being REPLIED to, not the activation
                # message — so keying the duration lookup on the activation id (as this did)
                # missed every time and the transcription budget silently never scaled.
                if _VOICE in (e["who"] or "") and e["wa_message_id"]:
                    last_audio_id = e["wa_message_id"]
                continue

            if e["label"] != "record":
                continue

            p = e["payload"] or {}
            activation_id = p.get("activation_message_id")
            turns.append({
                "loop_id": loop_id,
                "seq": e["seq"],
                "chat_id": loop.get("chat_id") or p.get("chat_id"),
                "prompt_version": loop.get("prompt_version") or p.get("prompt_version"),
                "domain": domain,
                "ts": e["ts"],
                "reply_text": p.get("response"),
                "silent": p.get("response") is None,
                # timing facts
                "reply_ts": e["ts"].timestamp() if e["ts"] is not None else None,
                "last_human_ts": (last_human or {}).get("wa_ts"),
                "last_human_id": (last_human or {}).get("wa_id"),
                # the conversation moved on while she worked — reported, never a gap
                "overtaken": bool(
                    last_human and activation_id and last_human.get("wa_id") != activation_id
                ),
                "audio_id": last_audio_id,
                "model_ms": p.get("latency_ms"),
                "delivery": p.get("delivery_result"),
                "turn_error": p.get("error_category"),
                "error": (p.get("error_category") or "none") != "none",
                # The chat as it stood. act_node writes the reply's TRANSCRIPT line before it
                # emits the record, so a naive `seq < N` slice hands the judge the very message
                # it is judging and it duly reports a duplicate — of itself. Both rows carry the
                # activation's trace_id, so dropping the assistant line written by THIS
                # activation is exact. Human lines from the same activation stay: they are what
                # she was answering.
                "lines": _context_for(lines, e["trace_id"]),
            })

        return turns

    async def audio_seconds(self, wa_ids: list[str]) -> dict[str, float]:
        """Voice-note lengths for the transcription budget, keyed by message id."""
        ids = [i for i in wa_ids if i]
        if not ids:
            return {}
        s = self.schema
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT wa_id, duration_sec FROM {s}.transcripts WHERE wa_id = ANY(%s)",
                    (ids,),
                )
                return {r[0]: float(r[1]) for r in await cur.fetchall() if r[1] is not None}

    # -- writes --------------------------------------------------------------
    async def write_review(self, row: dict, gaps: list[dict]) -> None:
        """One turn's verdict plus its findings, in one transaction.

        Upsert on (loop_id, seq, judge_version) so a crash mid-loop can be resumed without
        duplicating rows; the findings are replaced wholesale on a re-judge."""
        s = self.schema
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""INSERT INTO {s}.reviews
                        (loop_id, seq, chat_id, ts, judge_version, judge_model, turn_kind,
                         domain, prompt_version, verdict, confidence, rationale, proposed_gap,
                         reply_text, error, task_class, wait_seconds, last_human_id, overtaken,
                         model_ms, timing_band, delivery, turn_error)
                        VALUES (%(loop_id)s, %(seq)s, %(chat_id)s, %(ts)s, %(judge_version)s,
                         %(judge_model)s, %(turn_kind)s, %(domain)s, %(prompt_version)s,
                         %(verdict)s, %(confidence)s, %(rationale)s, %(proposed_gap)s,
                         %(reply_text)s, %(error)s, %(task_class)s, %(wait_seconds)s,
                         %(last_human_id)s, %(overtaken)s, %(model_ms)s, %(timing_band)s,
                         %(delivery)s, %(turn_error)s)
                        ON CONFLICT (loop_id, seq, judge_version) DO UPDATE SET
                         reviewed_at = now(), judge_model = EXCLUDED.judge_model,
                         verdict = EXCLUDED.verdict, confidence = EXCLUDED.confidence,
                         rationale = EXCLUDED.rationale, proposed_gap = EXCLUDED.proposed_gap,
                         error = EXCLUDED.error, task_class = EXCLUDED.task_class,
                         wait_seconds = EXCLUDED.wait_seconds, timing_band = EXCLUDED.timing_band
                        RETURNING id""",
                    row,
                )
                review_id = (await cur.fetchone())[0]
                await cur.execute(f"DELETE FROM {s}.findings WHERE review_id = %s", (review_id,))
                if gaps:
                    await cur.executemany(
                        f"""INSERT INTO {s}.findings
                            (review_id, loop_id, ts, code, severity, evidence, source)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        [(review_id, row["loop_id"], row["ts"], g["code"], g["severity"],
                          g.get("evidence"), g.get("source", "judge")) for g in gaps],
                    )

    async def mark_done(self, loop_id: str, *, judge_version: str, turns: int, bad: int) -> None:
        s = self.schema
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {s}.loop_reviews (loop_id, judge_version, turns, bad)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (loop_id, judge_version) DO UPDATE SET
                      reviewed_at = now(), turns = EXCLUDED.turns, bad = EXCLUDED.bad""",
                (loop_id, judge_version, turns, bad),
            )

    async def purge(self, *, events_days: int) -> None:
        """Reviews are pruned with the events they describe; findings cascade."""
        s = self.schema
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"DELETE FROM {s}.reviews WHERE ts < now() - make_interval(days => %s)",
                    (events_days,),
                )
        except Exception as exc:
            log.warning("review purge skipped: %s", exc)

    # -- schema --------------------------------------------------------------
    def _ddl(self) -> str:
        s = self.schema
        return f"""
        CREATE TABLE IF NOT EXISTS {s}.reviews (
            id             bigserial PRIMARY KEY,
            loop_id        text NOT NULL,
            seq            int  NOT NULL,
            chat_id        text,
            ts             timestamptz NOT NULL,
            reviewed_at    timestamptz NOT NULL DEFAULT now(),
            judge_version  text NOT NULL,
            judge_model    text,
            turn_kind      text NOT NULL,        -- 'reply' | 'silence'
            domain         text,
            prompt_version text,
            verdict        text NOT NULL,        -- good | acceptable | bad | error
            confidence     text,
            rationale      text,
            proposed_gap   text,
            reply_text     text,
            error          text,                 -- set when the judge call itself failed

            -- timing half: measured, not judged
            task_class     text,
            wait_seconds   numeric,              -- last human message -> reply on the wire
            last_human_id  text,                 -- which message the wait is measured from
            overtaken      boolean,              -- newer human messages landed mid-turn
            model_ms       int,                  -- the record's latency_ms, for comparison
            timing_band    text,                 -- good | slow | breach | unknown
            delivery       text,
            turn_error     text
        );
        CREATE UNIQUE INDEX IF NOT EXISTS reviews_turn
            ON {s}.reviews (loop_id, seq, judge_version);
        CREATE INDEX IF NOT EXISTS reviews_time ON {s}.reviews (ts DESC);
        CREATE INDEX IF NOT EXISTS reviews_slow ON {s}.reviews (timing_band, ts DESC);

        CREATE TABLE IF NOT EXISTS {s}.findings (
            id         bigserial PRIMARY KEY,
            review_id  bigint NOT NULL REFERENCES {s}.reviews(id) ON DELETE CASCADE,
            loop_id    text NOT NULL,
            ts         timestamptz NOT NULL,
            code       text NOT NULL,
            severity   text NOT NULL,
            evidence   text,
            source     text NOT NULL DEFAULT 'judge'   -- judge | timing
        );
        CREATE INDEX IF NOT EXISTS findings_code ON {s}.findings (code, ts DESC);
        CREATE INDEX IF NOT EXISTS findings_review ON {s}.findings (review_id);

        CREATE TABLE IF NOT EXISTS {s}.loop_reviews (
            loop_id       text NOT NULL,
            judge_version text NOT NULL,
            reviewed_at   timestamptz NOT NULL DEFAULT now(),
            turns         int NOT NULL DEFAULT 0,
            bad           int NOT NULL DEFAULT 0,
            PRIMARY KEY (loop_id, judge_version)
        );
        """


def _context_for(lines: list[dict], trace_id: Optional[str]) -> list[dict]:
    """The chat as it stood before this activation spoke — a COPY, so later lines cannot leak
    backwards, minus the assistant line this same activation just wrote."""
    return [
        dict(ln) for ln in lines
        if not (ln.get("trace_id") == trace_id and not _is_human(ln.get("who")))
    ]


def _int_or_none(v: Any) -> Optional[int]:
    try:
        n = int(v)
        return n or None
    except (TypeError, ValueError):
        return None
