"""Transcription service + transcript cache.

`context` re-fetches the full history on every reseed, so a naive transcriber would re-bill
and re-poll every window audio on each @mary tag. This service memoises transcripts by
WhatsApp message id across two tiers:

  1. an in-process bounded LRU — kills same-loop and read-back repeats for free;
  2. an optional durable table (mary_log.transcripts) — survives restarts and is shared
     across workers. Best-effort: if the DB is off, tier 1 is the whole cache, exactly as
     the LogStore degrades.

Both the fast-path node and the context node call `service.get(wa_id)`, so a clip the owner
replies to AND that also sits in the window is transcribed at most once. The service never
raises: a download/provider failure comes back as a TranscriptResult with `error` set."""
from __future__ import annotations

import base64
import logging
from collections import OrderedDict
from typing import Any, Optional

from .transcription import Transcriber, TranscriptResult

log = logging.getLogger("mary.cache")

# error kinds that are transient / configuration — never cached, so a later turn retries.
_TRANSIENT = {"auth", "download", "provider", "timeout"}


class TranscriptionService:
    """Download-and-transcribe with a two-tier cache. One instance per process (deps)."""

    def __init__(
        self, evolution: Any, transcriber: Transcriber, settings: Any,
        *, store: Optional["TranscriptStore"] = None,
    ) -> None:
        self.evolution = evolution
        self.transcriber = transcriber
        self.s = settings
        self.store = store
        self._lru: "OrderedDict[str, dict]" = OrderedDict()
        self._lru_max = settings.transcription_cache_max

    async def get(self, wa_id: str, *, language: str | None = None) -> TranscriptResult:
        """Transcript for a WhatsApp audio message id. Cache → durable → download+transcribe.
        Returns a TranscriptResult; `error` is None on success, "empty" for silent audio, or
        a transient kind on failure (not cached)."""
        if not wa_id:
            return {"text": "", "error": "download"}

        cached = self._lru.get(wa_id)
        if cached is not None:
            self._lru.move_to_end(wa_id)
            return self._result(cached)

        if self.store is not None:
            row = await self.store.get(wa_id)
            if row is not None:
                self._remember(wa_id, row)
                return self._result(row)

        media = await self.evolution.get_media_base64(wa_id)
        if not media:
            return {"text": "", "error": "download"}
        try:
            audio = base64.b64decode(media["base64"])
        except (ValueError, TypeError):
            return {"text": "", "error": "download"}

        lang = language if language is not None else self._default_language()
        result = await self.transcriber.transcribe(
            audio, mimetype=media.get("mimetype") or "audio/ogg", language=lang
        )
        if result.get("error") in _TRANSIENT:
            return result  # transient — let the caller degrade, don't poison the cache

        entry = {
            "text": result.get("text") or "",
            "duration_sec": result.get("duration_sec"),
            "language": result.get("language"),
        }
        self._remember(wa_id, entry)
        if self.store is not None:
            await self.store.put(wa_id, entry)
        return self._result(entry)

    def _default_language(self) -> str | None:
        lang = (self.s.assemblyai_language or "auto").strip()
        return None if lang.lower() == "auto" else lang

    @staticmethod
    def _result(entry: dict) -> TranscriptResult:
        text = entry.get("text") or ""
        return {
            "text": text,
            "duration_sec": entry.get("duration_sec"),
            "language": entry.get("language"),
            "error": None if text else "empty",
        }

    def _remember(self, wa_id: str, entry: dict) -> None:
        self._lru[wa_id] = entry
        self._lru.move_to_end(wa_id)
        while len(self._lru) > self._lru_max:
            self._lru.popitem(last=False)


class TranscriptStore:
    """Durable transcript cache in Postgres (mary_log.transcripts). Best-effort — every op
    swallows its error so a DB hiccup can never delay or fail a reply. Shares DATABASE_URL,
    isolated in the log schema. psycopg is imported lazily so this module needs no DB present."""

    def __init__(self, dsn: str, *, schema: str = "mary_log") -> None:
        self.dsn = dsn
        self.schema = schema
        self._pool: Any = None

    async def open(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=2, open=False)
        await self._pool.open()
        async with self._pool.connection() as conn:
            await conn.execute(self._ddl())

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def get(self, wa_id: str) -> Optional[dict]:
        if self._pool is None:
            return None
        try:
            from psycopg.rows import dict_row

            async with self._pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"SELECT text, duration_sec, language "
                        f"FROM {self.schema}.transcripts WHERE wa_id = %s",
                        (wa_id,),
                    )
                    return await cur.fetchone()
        except Exception as exc:  # never let the cache break the reply path
            log.warning("transcript cache get failed: %s", exc)
            return None

    async def put(self, wa_id: str, entry: dict) -> None:
        if self._pool is None:
            return
        try:
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"""INSERT INTO {self.schema}.transcripts (wa_id, text, duration_sec, language)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (wa_id) DO NOTHING""",
                    (wa_id, entry.get("text") or "", entry.get("duration_sec"),
                     entry.get("language")),
                )
        except Exception as exc:
            log.warning("transcript cache put failed: %s", exc)

    def _ddl(self) -> str:
        s = self.schema
        return f"""
        CREATE SCHEMA IF NOT EXISTS {s};
        CREATE TABLE IF NOT EXISTS {s}.transcripts (
            wa_id        text PRIMARY KEY,
            text         text NOT NULL,
            duration_sec real,
            language     text,
            created_at   timestamptz NOT NULL DEFAULT now()
        );
        """
