"""VoiceCapture — every voice note in every chat, transcribed while WhatsApp still serves it.

Why. History search reads words. A conversation held in voice notes is invisible to it unless the
notes were transcribed, and that can only happen for a short while: Evolution keeps the message
record but never the audio, and re-downloads it from WhatsApp's CDN by message id. A probe on
2026-11-16 found notes 7 and 14 days old still served and notes 21, 28, 35 and 45 days old
refused ("Failed to fetch stream"); the log holds a transcript made from a 16-day-old note. A note
not transcribed within about two and a half weeks is gone for good.

A pass, every HISTORY_VOICE_INTERVAL_SECONDS:
  1. finds voice notes in Evolution's "Message" table (`audioMessage`, and inside the ephemeral
     and view-once wrappers) sent within HISTORY_VOICE_BACKFILL_DAYS and at least
     HISTORY_VOICE_MIN_AGE_SECONDS ago, that have no transcript yet and nothing final in
     `<history_schema>.voice_attempts`. Newest first, in small batches with a fresh read between
     them, so a note that just arrived never waits behind the backfill;
  2. transcribes each through TranscriptionService.get(), HISTORY_VOICE_CONCURRENCY at a time.
     The service writes the transcript into `<log_schema>.transcripts`, the table every other
     path reads; this module only records how each attempt ended, in `voice_attempts`.

How a note ends up (voice_attempts.status):
  done      transcribed, or silent ("empty"); the text is in transcripts
  skipped   longer than HISTORY_VOICE_MAX_SECONDS (declared length), or no audio in the record
  expired   the download failed and the note is HISTORY_VOICE_EXPIRE_DAYS old or more; or it
            failed HISTORY_VOICE_MAX_ATTEMPTS times; or it aged out of the window while pending
  failed    the provider failed HISTORY_VOICE_MAX_ATTEMPTS times
  pending   failed for now; tried again after a backoff (10 min, doubling, 12 h at most)

Rails. A 24-hour cap on transcribed minutes (HISTORY_VOICE_DAILY_MINUTES) so a flood cannot run
up the bill. Three failures in a row that are not plain expiry end the pass, so an outage costs a
few attempts rather than the whole backlog's. An auth failure (no or bad key) pauses capture for
15 minutes without counting against any note. Nothing here raises into the app: a pass that
breaks is logged and the next one tries again. aclose() cancels whatever is in flight; a note cut
off mid-way has no row and is simply tried again after the restart.

Cost of the read. "Message" has no index on time or type, so each read is a sequential scan: in
November 2026 (160k rows, 130 MB) it took 0.4-0.9 s. It runs once per pass, plus once per batch
while a backlog drains.

The database sits behind VoiceRepo, so the tests run the whole loop without Postgres."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

log = logging.getLogger("mary.history.voice")

DAY = 86400.0

# Evolution's message table (Prisma keeps it in `public`).
MESSAGE_TABLE = 'public."Message"'

STATUSES = ("done", "expired", "failed", "skipped", "pending")

# Where a voice note can sit in a stored message. Evolution files a plain one, and a view-once one
# (it carries "viewOnce": true inside), under messageType 'audioMessage'; on 2026-11-16 production
# held no wrapped audio at all. The wrappers are covered anyway: a disappearing-chat image is
# filed as 'ephemeralMessage', and a voice note that ever arrives that way must not be missed.
_PLAIN_TYPES = ("audioMessage", "pttMessage")
_WRAPPER_TYPES = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
                  "viewOnceMessageV2Extension")
_AUDIO_PATHS = (
    ("audioMessage",),
    ("pttMessage",),
    ("ephemeralMessage", "message", "audioMessage"),
    ("viewOnceMessage", "message", "audioMessage"),
    ("viewOnceMessageV2", "message", "audioMessage"),
    ("viewOnceMessageV2Extension", "message", "audioMessage"),
)

_BACKOFF_BASE = 600.0         # first retry after 10 minutes, doubling
_BACKOFF_MAX = 12 * 3600.0    # never more than 12 hours between tries
_AUTH_PAUSE = 900.0           # a missing/bad provider key: stop for 15 minutes
_BREAKER = 3                  # unhealthy failures in a row that end a pass
_MAX_BATCHES = 50             # batches in one pass, a bound on any loop
_UNKNOWN_SECONDS = 60         # what a note with no declared length counts for against the cap
_FIRST_DELAY = 15.0           # let boot settle before the first pass
_CLOSE_TIMEOUT = 5.0          # aclose() waits this long for the cancelled task

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass
class VoiceNote:
    msg_id: str                  # WhatsApp key.id, = transcripts.wa_id
    chat: str                    # key.remoteJid as Evolution stored it
    ts: int                      # sent at, unix seconds
    seconds: Optional[int]       # declared length
    has_media: bool              # the record holds an audio node to download from
    attempts: int = 0            # tries made before this one


class VoiceRepo(Protocol):
    """What VoiceCapture needs from the database. PgVoiceRepo below; the tests use a fake."""

    async def open(self) -> None: ...

    async def aclose(self) -> None: ...

    async def candidates(self, *, since: int, until: int, limit: int) -> list[VoiceNote]:
        """Voice notes sent in [since, until] with no transcript and nothing final recorded, and
        none waiting out a backoff. Newest first, at most `limit`."""
        ...

    async def record(self, note: VoiceNote, *, status: str, attempts: int,
                     last_error: Optional[str], next_at: Optional[float],
                     seconds: Optional[int]) -> None: ...

    async def minutes_since(self, since: float) -> float:
        """Audio minutes marked done since `since` (unix seconds)."""
        ...

    async def age_out(self, *, before: int) -> int:
        """Pending notes sent before `before` (out of the window, never picked again) become
        expired. Returns how many."""
        ...


def backoff(attempts: int) -> float:
    """Seconds to wait after the `attempts`-th failed try."""
    return min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** max(0, attempts - 1)))


class _Pass:
    """The running totals of one pass, shared by its concurrent tries."""

    def __init__(self, *, used: float, cap: float) -> None:
        self.used = used           # minutes counted against the cap (24 h, plus this pass)
        self.cap = cap
        self.reserved = 0.0        # minutes of the tries in flight
        self.halt: Optional[str] = None   # "cap" | "auth" | "unhealthy" | "db"
        self.unhealthy = 0         # failures in a row that were not plain expiry
        self.last_error: Optional[str] = None


class VoiceCapture:
    """The background loop. Built and started by history/runtime.py when
    HISTORY_VOICE_CAPTURE_ENABLED: VoiceCapture(dsn, transcription, settings); open(); start();
    aclose() at shutdown. The constructor never raises, so a bad setting cannot stop the boot."""

    def __init__(self, dsn: str, transcription, settings, *, repo: Optional[VoiceRepo] = None,
                 clock: Optional[Callable[[], float]] = None,
                 first_delay: float = _FIRST_DELAY) -> None:
        self.dsn = dsn
        self.transcription = transcription
        self.settings = settings
        self.repo: Any = repo if repo is not None else PgVoiceRepo(
            dsn, log_schema=getattr(settings, "log_schema", "mary_log"),
            history_schema=getattr(settings, "history_schema", "lisa_search"))
        self._now = clock or time.time
        self._first_delay = first_delay
        self._task: Optional[asyncio.Task] = None
        self._paused_until = 0.0
        self._capped = False       # the cap has bitten and that was logged

    # -- lifecycle -------------------------------------------------------------------------
    async def open(self) -> None:
        if not getattr(self.settings, "transcription_enabled", True):
            raise RuntimeError("transcription is off (TRANSCRIPTION_ENABLED)")
        await self.repo.open()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=_CLOSE_TIMEOUT)
            if task in done and not task.cancelled():
                task.exception()  # retrieved, so asyncio does not complain about it
        try:
            await self.repo.aclose()
        except Exception as exc:
            log.warning("voice capture close failed: %s", exc)

    async def _loop(self) -> None:
        await asyncio.sleep(self._first_delay)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a broken pass must never end the loop
                log.warning("voice capture pass failed: %s", exc)
            await asyncio.sleep(max(1.0, float(self.settings.history_voice_interval_seconds)))

    # -- one pass --------------------------------------------------------------------------
    async def run_once(self) -> dict:
        """One pass: batches, newest first, until nothing is due or the pass halts. Returns the
        counts it logged (or would have: an idle pass logs nothing)."""
        s = self.settings
        counts = {"done": 0, "expired": 0, "failed": 0, "skipped": 0, "pending": 0,
                  "minutes": 0.0}
        now = self._now()
        if now < self._paused_until:
            return counts
        since = int(now - float(s.history_voice_backfill_days) * DAY)
        until = int(now - max(0, int(s.history_voice_min_age_seconds)))
        counts["expired"] += await self.repo.age_out(before=since)

        cap = float(s.history_voice_daily_minutes or 0)
        used = await self.repo.minutes_since(now - DAY) if cap > 0 else 0.0
        p = _Pass(used=used, cap=cap)
        batch = max(4, self._concurrency() * 4)
        seen: set[str] = set()
        for _ in range(_MAX_BATCHES):
            notes = await self.repo.candidates(since=since, until=until, limit=batch)
            todo = [n for n in notes if n.msg_id not in seen]
            if not todo:
                break
            seen.update(n.msg_id for n in todo)
            todo.sort(key=lambda n: n.ts, reverse=True)
            await self._run_batch(todo, p, counts)
            if p.halt or len(notes) < batch:
                break

        self._log_halt(p)
        counts["minutes"] = round(counts["minutes"], 1)
        if any(counts[k] for k in STATUSES):
            log.info(json.dumps({"voice_capture": counts}))
        return counts

    def _concurrency(self) -> int:
        return max(1, int(self.settings.history_voice_concurrency or 1))

    async def _run_batch(self, notes: list[VoiceNote], p: _Pass, counts: dict) -> None:
        sem = asyncio.Semaphore(self._concurrency())

        async def one(note: VoiceNote) -> None:
            try:
                await self._one(note, sem, p, counts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # most likely the bookkeeping write: stop for this pass
                log.warning("voice capture: note not settled: %s", exc)
                p.halt = p.halt or "db"

        await asyncio.gather(*(one(n) for n in notes))

    async def _one(self, note: VoiceNote, sem: asyncio.Semaphore, p: _Pass, counts: dict) -> None:
        s = self.settings
        if p.halt:
            return
        if not note.has_media:
            await self._record(note, counts, "skipped", attempts=note.attempts, error="no_media")
            return
        max_s = int(s.history_voice_max_seconds or 0)
        if max_s and note.seconds and note.seconds > max_s:
            await self._record(note, counts, "skipped", attempts=note.attempts, error="too_long")
            return
        async with sem:
            if p.halt:
                return
            est = (note.seconds or _UNKNOWN_SECONDS) / 60.0
            if p.cap and p.used + p.reserved + est > p.cap:
                p.halt = p.halt or "cap"
                return
            p.reserved += est
            try:
                result = await self.transcription.get(note.msg_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the service never raises; if it ever does, retry later
                log.warning("voice capture: transcription raised: %s", exc)
                result = {"text": "", "error": "provider"}
            finally:
                p.reserved -= est
            await self._settle(note, result or {}, p, counts)

    async def _settle(self, note: VoiceNote, result: dict, p: _Pass, counts: dict) -> None:
        s = self.settings
        now = self._now()
        err = result.get("error")
        attempts = note.attempts + 1

        if err in (None, "empty"):
            secs = note.seconds or _as_int(result.get("duration_sec"))
            p.used += (secs or _UNKNOWN_SECONDS) / 60.0   # against the cap, as estimated
            p.unhealthy = 0
            counts["minutes"] += (secs or 0) / 60.0
            await self._record(note, counts, "done", attempts=attempts, error=err, seconds=secs)
            return

        if err == "auth":  # configuration, not this note: nothing is counted against it
            self._paused_until = now + _AUTH_PAUSE
            p.halt = "auth"
            return

        if err == "download" and now - note.ts >= float(s.history_voice_expire_days) * DAY:
            # Expected for a note this old: WhatsApp no longer serves it. Not a sign of an outage.
            await self._record(note, counts, "expired", attempts=attempts, error="download")
            return

        p.unhealthy += 1
        p.last_error = err
        if p.unhealthy >= _BREAKER:
            p.halt = p.halt or "unhealthy"
        if attempts >= max(1, int(s.history_voice_max_attempts)):
            final = "expired" if err == "download" else "failed"
            await self._record(note, counts, final, attempts=attempts, error=err)
        else:
            await self._record(note, counts, "pending", attempts=attempts,
                               error=err or "unknown", next_at=now + backoff(attempts))

    async def _record(self, note: VoiceNote, counts: dict, status: str, *, attempts: int,
                      error: Optional[str], next_at: Optional[float] = None,
                      seconds: Optional[int] = None) -> None:
        await self.repo.record(note, status=status, attempts=attempts, last_error=error,
                               next_at=next_at,
                               seconds=seconds if seconds is not None else note.seconds)
        counts[status] += 1

    def _log_halt(self, p: _Pass) -> None:
        if p.halt == "cap":
            if not self._capped:
                self._capped = True
                log.warning(json.dumps({"voice_capture": {
                    "cap": "reached", "limit_minutes": p.cap,
                    "minutes_24h": round(p.used, 1)}}))
            return
        self._capped = False
        if p.halt == "auth":
            log.warning(json.dumps({"voice_capture": {
                "paused": "auth", "seconds": int(_AUTH_PAUSE)}}))
        elif p.halt == "unhealthy":
            log.warning(json.dumps({"voice_capture": {
                "halted": "unhealthy", "last_error": p.last_error}}))


def _as_int(v) -> Optional[int]:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


# -- Postgres ---------------------------------------------------------------------------------

def _audio_expr(alias: str = "m") -> str:
    paths = ["{}.message{}".format(alias, "".join(f"->'{k}'" for k in path))
             for path in _AUDIO_PATHS]
    return "COALESCE(" + ", ".join(paths) + ")"


def candidates_sql(*, table: str = MESSAGE_TABLE, log_schema: str, history_schema: str,
                   with_transcripts: bool = True) -> str:
    """The pass's query. Parameters: %(since)s, %(until)s, %(limit)s.

    One row per key.id (Evolution sometimes stores a message twice; the copy that holds audio
    wins). A record with no audio in it is still returned when it is filed as a voice note, so it
    gets marked skipped instead of being looked at forever. Without the transcripts table (the
    durable transcript cache is off) only voice_attempts rules notes out."""
    types = ", ".join(f"'{t}'" for t in _PLAIN_TYPES + _WRAPPER_TYPES)
    plain = ", ".join(f"'{t}'" for t in _PLAIN_TYPES)
    transcribed = (
        f"\n  AND NOT EXISTS (SELECT 1 FROM {log_schema}.transcripts t WHERE t.wa_id = a.msg_id)"
        if with_transcripts else "")
    return f"""
WITH raw AS (
    SELECT m.key->>'id'         AS msg_id,
           m.key->>'remoteJid'  AS chat,
           m."messageTimestamp" AS ts,
           m."messageType"      AS mtype,
           {_audio_expr("m")} AS audio
    FROM {table} m
    WHERE m."messageType" IN ({types})
      AND m."messageTimestamp" BETWEEN %(since)s AND %(until)s
), audio AS (
    SELECT DISTINCT ON (msg_id) msg_id, chat, ts, audio
    FROM raw
    WHERE msg_id IS NOT NULL
      AND (audio IS NOT NULL OR mtype IN ({plain}))
    ORDER BY msg_id, (jsonb_typeof(audio) = 'object') DESC NULLS LAST, ts DESC
)
SELECT a.msg_id, a.chat, a.ts,
       CASE WHEN a.audio->>'seconds' ~ '^[0-9]+([.][0-9]+)?$'
            THEN round((a.audio->>'seconds')::numeric)::int END AS seconds,
       COALESCE(jsonb_typeof(a.audio) = 'object', false) AS has_media,
       COALESCE(v.attempts, 0) AS attempts
FROM audio a
LEFT JOIN {history_schema}.voice_attempts v ON v.msg_id = a.msg_id
WHERE (v.msg_id IS NULL
       OR (v.status = 'pending' AND (v.next_at IS NULL OR v.next_at <= now()))){transcribed}
ORDER BY a.ts DESC
LIMIT %(limit)s
"""


def record_sql(history_schema: str) -> str:
    return f"""
INSERT INTO {history_schema}.voice_attempts AS v
    (msg_id, chat, ts, seconds, attempts, status, last_error, next_at, updated_at)
VALUES (%(msg_id)s, %(chat)s, %(ts)s, %(seconds)s, %(attempts)s, %(status)s, %(last_error)s,
        to_timestamp(%(next_at)s::float8), now())
ON CONFLICT (msg_id) DO UPDATE SET
    chat = EXCLUDED.chat, ts = EXCLUDED.ts, seconds = COALESCE(EXCLUDED.seconds, v.seconds),
    attempts = EXCLUDED.attempts, status = EXCLUDED.status, last_error = EXCLUDED.last_error,
    next_at = EXCLUDED.next_at, updated_at = now()
"""


def minutes_sql(history_schema: str) -> str:
    return (f"SELECT COALESCE(SUM(seconds), 0) / 60.0 FROM {history_schema}.voice_attempts "
            f"WHERE status = 'done' AND updated_at >= to_timestamp(%s)")


def age_out_sql(history_schema: str) -> str:
    return (f"UPDATE {history_schema}.voice_attempts "
            f"SET status = 'expired', last_error = COALESCE(last_error || '; ', '') || 'aged out', "
            f"next_at = NULL, updated_at = now() "
            f"WHERE status = 'pending' AND ts < %s")


def ddl(history_schema: str) -> str:
    s = history_schema
    statuses = ", ".join(f"'{x}'" for x in STATUSES)
    return f"""
    CREATE SCHEMA IF NOT EXISTS {s};
    CREATE TABLE IF NOT EXISTS {s}.voice_attempts (
        msg_id     text PRIMARY KEY,         -- WhatsApp key.id, = <log_schema>.transcripts.wa_id
        chat       text,                     -- key.remoteJid as Evolution stored it
        ts         bigint,                   -- sent at, unix seconds
        seconds    int,                      -- declared length (measured, when none was declared)
        attempts   int NOT NULL DEFAULT 0,   -- transcriptions tried
        status     text NOT NULL CHECK (status IN ({statuses})),
        last_error text,                     -- download | provider | timeout | empty | too_long | no_media
        next_at    timestamptz,              -- pending: not tried again before this
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS voice_attempts_status ON {s}.voice_attempts (status, updated_at);
    """


class PgVoiceRepo:
    """voice_attempts, and the read of Evolution's "Message". Shares DATABASE_URL. psycopg is
    imported lazily, so this module imports with no database present."""

    def __init__(self, dsn: str, *, log_schema: str, history_schema: str,
                 table: str = MESSAGE_TABLE) -> None:
        self.dsn = dsn
        self.log_schema = log_schema
        self.history_schema = history_schema
        self.table = table
        self._pool: Any = None
        self._has_transcripts = False

    async def open(self) -> None:
        for name in (self.log_schema, self.history_schema):
            if not _IDENT.match(name or ""):
                raise ValueError(f"not a plain schema name: {name!r}")
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=2, open=False)
        await pool.open()
        self._pool = pool
        try:
            async with pool.connection() as conn:
                await conn.execute(ddl(self.history_schema))
            await self._check_transcripts()
        except BaseException:
            # runtime.py leaves a part that failed to open out of its closers: close it here.
            self._pool = None
            await pool.close()
            raise
        if not self._has_transcripts:
            log.warning("voice capture: %s.transcripts is missing; only voice_attempts is used",
                        self.log_schema)

    async def aclose(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.close()

    async def _check_transcripts(self) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT to_regclass(%s) IS NOT NULL",
                                     (f"{self.log_schema}.transcripts",))
            row = await cur.fetchone()
        self._has_transcripts = bool(row and row[0])

    async def candidates(self, *, since: int, until: int, limit: int) -> list[VoiceNote]:
        if not self._has_transcripts:
            await self._check_transcripts()
        sql = candidates_sql(table=self.table, log_schema=self.log_schema,
                             history_schema=self.history_schema,
                             with_transcripts=self._has_transcripts)
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, {"since": since, "until": until, "limit": limit})
            rows = await cur.fetchall()
        return [VoiceNote(msg_id=r[0], chat=r[1] or "", ts=int(r[2]), seconds=r[3],
                          has_media=bool(r[4]), attempts=int(r[5] or 0)) for r in rows]

    async def record(self, note: VoiceNote, *, status: str, attempts: int,
                     last_error: Optional[str], next_at: Optional[float],
                     seconds: Optional[int]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(record_sql(self.history_schema), {
                "msg_id": note.msg_id, "chat": note.chat, "ts": note.ts, "seconds": seconds,
                "attempts": attempts, "status": status, "last_error": last_error,
                "next_at": next_at,
            })

    async def minutes_since(self, since: float) -> float:
        async with self._pool.connection() as conn:
            cur = await conn.execute(minutes_sql(self.history_schema), (since,))
            row = await cur.fetchone()
        return float(row[0] or 0) if row else 0.0

    async def age_out(self, *, before: int) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(age_out_sql(self.history_schema), (before,))
            return max(0, cur.rowcount or 0)
