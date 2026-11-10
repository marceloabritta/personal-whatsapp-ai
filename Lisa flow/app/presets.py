"""The owner's meeting presets — what Lisa books with when he does not say otherwise.

Four presets, each changed from setup in his chat with himself:

  meeting_minutes  how long a meeting lasts when no end is given
  video_call       whether a timed meeting with no place gets a Google Meet
  notify_guests    whether guests are emailed the invite (and later changes / cancellations)
  places           named addresses — "casa", "the office" — he can book at by name

Every preset that has never been set reads as TODAY'S behaviour: the configured default length,
a Meet when there is nowhere to go, guests emailed. So an empty table, a store that failed to
load, or the feature switched off all leave Lisa booking exactly as she did before presets
existed. That is the failure direction on purpose: a database hiccup may cost the owner his
preferences for a while, never put a meeting at an address he did not give.

Two tiers, like the roster:
  1. the in-process snapshot — what every read consults, pure and synchronous (the calendar's
     prompt block and the confirm node both read it inside the reply path);
  2. an optional Postgres table (`<log_schema>.owner_presets`) — survives restarts, shared across
     workers. With no DATABASE_URL the snapshot IS the presets (dev/tests)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from typing import Any, Optional

log = logging.getLogger("mary.presets")

MEETING_MINUTES = "meeting_minutes"
VIDEO_CALL = "video_call"
NOTIFY_GUESTS = "notify_guests"
SETTINGS_KEYS = (MEETING_MINUTES, VIDEO_CALL, NOTIFY_GUESTS)

# A place is one row per name, keyed by its folded name so "Casa" and "casa" are the same place.
PLACE_PREFIX = "place:"

# Bounds on what a preset may hold. A five-minute meeting is real; a ten-hour one is a typo for
# an all-day block, which the calendar already models as its own kind.
MIN_MINUTES = 5
MAX_MINUTES = 480
MAX_NAME = 40
MAX_ADDRESS = 300


def fold(text: str | None) -> str:
    """Casefold, strip accents and collapse spaces — how two spellings of one place name meet
    ("Escritório" / "escritorio ")."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


def place_key(name: str) -> str:
    return PLACE_PREFIX + fold(name)


class Presets:
    """The presets, in memory, with an optional durable tier behind them."""

    def __init__(self, *, default_minutes: int = 45, store: Optional["PresetStore"] = None,
                 ttl: float = 30.0) -> None:
        self.default_minutes = int(default_minutes)
        self.store = store
        self.ttl = ttl
        self._rows: dict[str, Any] = {}     # key -> value, exactly as stored
        self._loaded_at = 0.0
        self._task: asyncio.Task | None = None

    # --- reads (pure, no I/O) ----------------------------------------------------------------
    def meeting_minutes(self) -> int:
        value = self._rows.get(MEETING_MINUTES)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) \
            else self.default_minutes

    def video_call(self) -> bool:
        value = self._rows.get(VIDEO_CALL)
        return value if isinstance(value, bool) else True

    def notify_guests(self) -> bool:
        value = self._rows.get(NOTIFY_GUESTS)
        return value if isinstance(value, bool) else True

    def is_set(self, key: str) -> bool:
        """Has the owner set this preset himself (as opposed to it reading as the default)?"""
        return key in self._rows

    def places(self) -> list[dict]:
        """Every saved place, {name, address}, sorted by name."""
        out = [dict(v) for k, v in self._rows.items()
               if k.startswith(PLACE_PREFIX) and isinstance(v, dict) and v.get("address")]
        return sorted(out, key=lambda p: fold(p.get("name")))

    def place(self, name: str | None) -> Optional[dict]:
        """The saved place this name refers to, or None. Exact after folding — a near miss is
        not a place, because a near miss would put a meeting at an address he did not mean."""
        if not fold(name):
            return None
        value = self._rows.get(place_key(name or ""))
        return dict(value) if isinstance(value, dict) else None

    def snapshot(self) -> dict:
        """Everything, resolved to its effective value — what the setup list and the
        confirmation composers read."""
        return {
            MEETING_MINUTES: self.meeting_minutes(),
            VIDEO_CALL: self.video_call(),
            NOTIFY_GUESTS: self.notify_guests(),
            "places": self.places(),
            "set": [k for k in SETTINGS_KEYS if k in self._rows],
        }

    # --- refresh -----------------------------------------------------------------------------
    async def refresh(self, *, force: bool = False) -> None:
        """Reload from the store when the snapshot is stale. No store -> nothing to do."""
        if self.store is None:
            return
        if not force and self._loaded_at and (time.monotonic() - self._loaded_at) < self.ttl:
            return
        rows = await self.store.load()
        if rows is None:  # the store failed; keep serving the snapshot we already have
            return
        self._rows = rows
        self._loaded_at = time.monotonic()

    def start(self) -> None:
        """Background TTL refresh, so a write made by another worker reaches this one's prompt
        and cards without the reply path ever awaiting the database."""
        if self.store is None or self._task is not None:
            return

        async def _loop() -> None:
            while True:
                try:
                    await self.refresh(force=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a refresh failure must never kill the task
                    log.warning("presets refresh failed: %s", exc)
                await asyncio.sleep(self.ttl)

        self._task = asyncio.create_task(_loop())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # --- writes (always through the store, then straight into the snapshot) ------------------
    async def set_many(self, values: dict) -> dict:
        """Write several settings at once. Only the three SETTINGS_KEYS are accepted; the caller
        validates the values."""
        clean = {k: v for k, v in values.items() if k in SETTINGS_KEYS}
        for key, value in clean.items():
            if self.store is not None:
                await self.store.upsert(key, value)
            self._rows[key] = value
        return self.snapshot()

    async def save_place(self, name: str, address: str) -> tuple[dict, Optional[dict]]:
        """(place, the place it replaced or None). Saving an existing name replaces its address
        and takes the new spelling of the name."""
        key = place_key(name)
        before = self._rows.get(key)
        value = {"name": " ".join(name.split()), "address": " ".join(address.split())}
        if self.store is not None:
            await self.store.upsert(key, value)
        self._rows[key] = value
        return dict(value), (dict(before) if isinstance(before, dict) else None)

    async def remove_place(self, name: str) -> Optional[dict]:
        key = place_key(name)
        if key not in self._rows:
            return None
        if self.store is not None:
            await self.store.remove(key)
        gone = self._rows.pop(key)
        return dict(gone) if isinstance(gone, dict) else None


class PresetStore:
    """The durable tier, in Postgres. Shares DATABASE_URL, isolated in the log schema.

    `load` returns None on failure ("I could not tell you", distinct from "nothing is set"), so a
    refresh keeps the last good snapshot. Writes raise, because the owner who asked for a change
    has to be told it did not happen."""

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

    async def load(self) -> Optional[dict]:
        if self._pool is None:
            return None
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(f"SELECT key, value FROM {self.schema}.owner_presets")
                    rows = await cur.fetchall()
            out: dict = {}
            for key, value in rows:
                out[key] = json.loads(value) if isinstance(value, str) else value
            return out
        except Exception as exc:
            log.warning("presets load failed: %s", exc)
            return None

    async def upsert(self, key: str, value: Any) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {self.schema}.owner_presets (key, value)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (key, json.dumps(value)),
            )

    async def remove(self, key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"DELETE FROM {self.schema}.owner_presets WHERE key = %s", (key,)
            )

    def _ddl(self) -> str:
        s = self.schema
        return f"""
        CREATE SCHEMA IF NOT EXISTS {s};
        CREATE TABLE IF NOT EXISTS {s}.owner_presets (
            key        text PRIMARY KEY,
            value      jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
