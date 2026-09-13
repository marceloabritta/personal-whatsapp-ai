"""The auto-transcription roster — which chats are transcribed, and in which direction.

One row per enrolled chat. The gate consults it on EVERY audio message, so the read has to be
free: `should_transcribe` is a pure dictionary lookup against an in-process snapshot and never
awaits I/O. The snapshot is refreshed on a TTL by a background task (and forced after every
write), mirroring how `cache.TranscriptStore` rides alongside the reply path without ever
blocking it.

Two tiers, like the transcript cache:
  1. the in-process snapshot — the authority the gate reads;
  2. an optional Postgres table (`<log_schema>.transcribe_rules`) — survives restarts and is
     shared across workers. With no DATABASE_URL the snapshot IS the roster (dev/tests).

FAIL CLOSED. If a store is configured but has never loaded, `should_transcribe` matches nothing.
A database hiccup must make Lisa quiet — never make her transcribe a chat that was not enrolled
into a chat that is not hers."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any, Optional

log = logging.getLogger("mary.roster")

# The three directions, in the words they are always shown in. `both` is stored; it renders as
# "in & out" (see skills/setup_format.py) — the only value whose label differs from its key.
INBOUND = "inbound"
OUTBOUND = "outbound"
BOTH = "both"
DIRECTIONS = (INBOUND, OUTBOUND, BOTH)

DIRECTION_ALIASES = {
    "in": INBOUND, "inbound": INBOUND, "received": INBOUND,
    "out": OUTBOUND, "outbound": OUTBOUND, "sent": OUTBOUND,
    "both": BOTH, "in & out": BOTH, "in and out": BOTH, "in&out": BOTH,
    # The direction is always offered as a numbered list, so answering "1" has to mean
    # something here too — the number may arrive as the direction itself.
    "1": INBOUND, "2": OUTBOUND, "3": BOTH,
}

# The numbered choice, in the fixed order it is always presented in.
DIRECTION_CHOICES = ((1, INBOUND), (2, OUTBOUND), (3, BOTH))

# Blanket rules. These are not chats — they are the default for a whole KIND of chat, stored as
# rows with kind="scope" so there is one table, one snapshot and one code path.
ALL_CONTACTS = "all_contacts"
ALL_GROUPS = "all_groups"
SCOPE_KEYS = (ALL_CONTACTS, ALL_GROUPS)
SCOPE_ALIASES = {
    "all_contacts": ALL_CONTACTS, "all contacts": ALL_CONTACTS, "all chats": ALL_CONTACTS,
    "contacts": ALL_CONTACTS, "everyone": ALL_CONTACTS, "todos os contatos": ALL_CONTACTS,
    "all_groups": ALL_GROUPS, "all groups": ALL_GROUPS, "groups": ALL_GROUPS,
    "todos os grupos": ALL_GROUPS,
}
SCOPE_KIND = "scope"


def normalize_scope(value: str | None) -> Optional[str]:
    """A blanket-rule key, or None when this is an ordinary chat key."""
    return SCOPE_ALIASES.get((value or "").strip().lower())


def scope_for_kind(kind: str) -> str:
    return ALL_GROUPS if kind == "group" else ALL_CONTACTS


def normalize_direction(value: str | None) -> Optional[str]:
    """A direction as stored, or None when it is not one of the three."""
    return DIRECTION_ALIASES.get((value or "").strip().lower())


def make_scope(key: str, direction: str) -> dict:
    """A blanket rule row. It names no chat, so it carries no JID and no label."""
    return {"chat_key": key, "chat_jid": "", "alt_key": None, "label": None,
            "kind": SCOPE_KIND, "direction": direction}


def make_rule(*, chat_key: str, chat_jid: str, direction: str, kind: str = "contact",
              label: str | None = None, alt_key: str | None = None) -> dict:
    return {"chat_key": chat_key, "chat_jid": chat_jid, "alt_key": alt_key or None,
            "label": label or None, "kind": kind, "direction": direction}


class Roster:
    """The rules, in memory, with an optional durable tier behind them."""

    def __init__(self, store: Optional["RosterStore"] = None, *, ttl: float = 30.0) -> None:
        self.store = store
        self.ttl = ttl
        self._rules: dict[str, dict] = {}      # chat_key -> rule
        self._index: dict[str, dict] = {}      # chat_key AND alt_key -> rule
        self._loaded_at = 0.0
        # With no store, memory is the whole roster and is ready immediately. With one, nothing
        # matches until a load has succeeded.
        self._ready = store is None
        self._task: asyncio.Task | None = None

    # --- the hot path (pure, no I/O) ------------------------------------------------------
    def should_transcribe(self, keys: list[str] | tuple, from_me: bool,
                          kind: str = "contact") -> Optional[dict]:
        """The rule covering this chat and direction, or None.

        `keys` are the chat's identities — its own key plus the `@lid`/phone twin Evolution
        reports as `remoteJidAlt`, since a 1:1 persists inbound under one and outbound under the
        other. Called from the gate on every audio message: no awaits, no exceptions.

        A chat's own rule and the blanket rule for its kind COMPOSE — either one covering the
        direction is enough. That is what makes "all contacts: outbound, and Mãe: in & out" mean
        what it reads like: everyone gets my audio written out, and Mãe's comes back too. An
        override would instead have silently switched the blanket rule off for Mãe."""
        if not self._ready:
            return None
        want = OUTBOUND if from_me else INBOUND
        for key in keys:
            if not key:
                continue
            rule = self._index.get(key)
            if rule and rule.get("kind") != SCOPE_KIND and rule["direction"] in (want, BOTH):
                return rule
        scope = self._rules.get(scope_for_kind(kind))
        if scope and scope["direction"] in (want, BOTH):
            return scope
        return None

    def snapshot(self) -> list[dict]:
        """Every CHAT rule, for the setup flow's list. Sorted by label within kind.
        Blanket rules are excluded — they are not chats; see `scopes()`."""
        return sorted(
            (r for r in self._rules.values() if r.get("kind") != SCOPE_KIND),
            key=lambda r: (r.get("kind") != "contact", (r.get("label") or r["chat_key"]).lower()),
        )

    def scopes(self) -> list[dict]:
        """The blanket rules that are set, in a fixed order (contacts, then groups)."""
        return [self._rules[k] for k in SCOPE_KEYS if k in self._rules]

    def get(self, chat_key: str) -> Optional[dict]:
        return self._rules.get(chat_key)

    @property
    def ready(self) -> bool:
        return self._ready

    # --- refresh --------------------------------------------------------------------------
    def _reindex(self) -> None:
        index: dict[str, dict] = {}
        for rule in self._rules.values():
            index[rule["chat_key"]] = rule
            if rule.get("alt_key"):
                index[rule["alt_key"]] = rule
        self._index = index

    async def refresh(self, *, force: bool = False) -> None:
        """Reload from the store when the snapshot is stale. No store → nothing to do."""
        if self.store is None:
            return
        if not force and self._ready and (time.monotonic() - self._loaded_at) < self.ttl:
            return
        rows = await self.store.list_rules()
        if rows is None:  # the store failed; keep serving the snapshot we already trust
            return
        self._rules = {r["chat_key"]: r for r in rows}
        self._reindex()
        self._loaded_at = time.monotonic()
        self._ready = True

    def start(self) -> None:
        """Background TTL refresh, so the gate's snapshot follows writes made by another worker
        without the reply path ever awaiting the database."""
        if self.store is None or self._task is not None:
            return

        async def _loop() -> None:
            while True:
                try:
                    await self.refresh(force=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a refresh failure must never kill the task
                    log.warning("roster refresh failed: %s", exc)
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

    # --- writes (always through the store, then straight into the snapshot) ----------------
    async def upsert(self, rule: dict) -> dict:
        if self.store is not None:
            await self.store.upsert(rule)
        self._rules[rule["chat_key"]] = dict(rule)
        self._reindex()
        self._ready = True
        return self._rules[rule["chat_key"]]

    async def set_direction(self, chat_key: str, direction: str) -> Optional[dict]:
        rule = self._rules.get(chat_key)
        if rule is None:
            return None
        updated = {**rule, "direction": direction}
        return await self.upsert(updated)

    async def remove(self, chat_key: str) -> Optional[dict]:
        rule = self._rules.pop(chat_key, None)
        if rule is None:
            return None
        if self.store is not None:
            await self.store.remove(chat_key)
        self._reindex()
        return rule


class DailyCap:
    """Per-chat transcripts per calendar day — the runaway guard.

    In-process and reset by a restart, which is the right trade: it exists to stop a pathological
    day, not to be an accounting record. Counting in Postgres would put a write on the hot path
    for a bound that is never normally approached."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._day: str = ""
        self._counts: dict[str, int] = {}

    def _roll(self) -> None:
        today = date.today().isoformat()
        if today != self._day:
            self._day, self._counts = today, {}

    def allows(self, chat_key: str) -> bool:
        if self.limit <= 0:
            return True
        self._roll()
        return self._counts.get(chat_key, 0) < self.limit

    def record(self, chat_key: str) -> None:
        if self.limit <= 0:
            return
        self._roll()
        self._counts[chat_key] = self._counts.get(chat_key, 0) + 1


class RosterStore:
    """The durable tier, in Postgres. Shares DATABASE_URL, isolated in the log schema.

    Reads return None on failure (meaning "I could not tell you", distinct from "no rules"), so
    a refresh keeps the last good snapshot instead of silently emptying the roster. Writes raise,
    because a write that failed must be reported to the person who asked for it — unlike the
    transcript cache, where a lost write costs nothing."""

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

    async def list_rules(self) -> Optional[list[dict]]:
        if self._pool is None:
            return None
        try:
            from psycopg.rows import dict_row

            async with self._pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"SELECT chat_key, chat_jid, alt_key, label, kind, direction "
                        f"FROM {self.schema}.transcribe_rules"
                    )
                    return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("roster list failed: %s", exc)
            return None

    async def upsert(self, rule: dict) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {self.schema}.transcribe_rules
                        (chat_key, chat_jid, alt_key, label, kind, direction)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chat_key) DO UPDATE SET
                        chat_jid = EXCLUDED.chat_jid, alt_key = EXCLUDED.alt_key,
                        label = EXCLUDED.label, kind = EXCLUDED.kind,
                        direction = EXCLUDED.direction, updated_at = now()""",
                (rule["chat_key"], rule["chat_jid"], rule.get("alt_key"), rule.get("label"),
                 rule.get("kind") or "contact", rule["direction"]),
            )

    async def remove(self, chat_key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"DELETE FROM {self.schema}.transcribe_rules WHERE chat_key = %s", (chat_key,)
            )

    def _ddl(self) -> str:
        s = self.schema
        return f"""
        CREATE SCHEMA IF NOT EXISTS {s};
        CREATE TABLE IF NOT EXISTS {s}.transcribe_rules (
            chat_key   text PRIMARY KEY,
            chat_jid   text NOT NULL,
            alt_key    text,
            label      text,
            kind       text NOT NULL DEFAULT 'contact',
            direction  text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
