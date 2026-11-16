"""Wiring for history search: what deps builds, and what the FastAPI lifespan starts and stops.

Three switches, all OFF by default, each independent:
  HISTORY_INDEX_ENABLED          the indexer keeps lisa_search up to date (background task)
  HISTORY_VOICE_CAPTURE_ENABLED  every voice note in every chat is transcribed (background task)
  HISTORY_ENABLED                the skill: "@lisa ..." in the self-chat can search
Every start here is best-effort: a part that cannot open is logged and left off, and the reply
path runs as it would without it."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("mary.history")


@dataclass
class HistoryRuntime:
    """What the skill needs at reply time. Built by deps when HISTORY_ENABLED; opened in lifespan."""
    store: Any
    media: Any
    settings: Any


def build_runtime(settings, evolution, transcription) -> HistoryRuntime | None:
    if not (settings.history_enabled and settings.database_url):
        return None
    from .media import EvolutionHistoryMedia
    from .store import PgHistoryStore

    dsn = settings.history_database_url or settings.database_url
    store = PgHistoryStore(dsn, schema=settings.history_schema)
    media = EvolutionHistoryMedia(evolution, transcription, settings)
    media.store = store  # names and dates a file from the index, not a findMessages round trip
    return HistoryRuntime(store=store, media=media, settings=settings)


async def start(deps) -> list:
    """Open the runtime and start the background parts that are switched on. Returns the things
    to close at shutdown, in order."""
    s = deps.settings
    closers: list = []
    if not s.database_url:
        return closers

    if s.history_index_enabled:
        from .indexer import HistoryIndexer

        idx = HistoryIndexer(s.database_url, schema=s.history_schema, settings=s)
        try:
            await idx.open()
            idx.start()
            closers.append(idx)
            log.info('{"boot":"history-indexer"}')
        except Exception as exc:
            log.warning("history indexer disabled: %s", exc)

    if s.history_voice_capture_enabled and deps.transcription is not None:
        from .voice import VoiceCapture

        vc = VoiceCapture(s.database_url, deps.transcription, s)
        try:
            await vc.open()
            vc.start()
            closers.append(vc)
            log.info('{"boot":"history-voice-capture"}')
        except Exception as exc:
            log.warning("history voice capture disabled: %s", exc)

    if deps.history is not None:
        try:
            await deps.history.store.open()
            closers.append(deps.history.store)
            log.info('{"boot":"history-search"}')
        except Exception as exc:
            log.warning("history search disabled: %s", exc)
            deps.history = None
    return closers


async def stop(closers: list) -> None:
    for c in reversed(closers):
        try:
            await c.aclose()
        except Exception as exc:
            log.warning("history close failed: %s", exc)
