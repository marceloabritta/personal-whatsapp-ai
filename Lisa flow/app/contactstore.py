"""The address book's durable tier — Postgres, in the log schema, beside the transcript cache.

Three jobs, and the middle one is the important one:

  contacts        the mirror of Google's book, plus the two columns Google has nowhere to store
                  (`preferred` / `used_at` — which address the owner ACTUALLY uses).
  contact_outbox  the write-ahead record. A learned address is written HERE first and only then
                  handed to the drain worker, so a crash between "Lisa learned it" and "Google has
                  it" loses nothing. It is also the idempotency mechanism: People has no request
                  key, so a replayed row is made safe by the read-back in `people.add_email`.
  contact_sync    one row, the incremental cursor.
  contact_links   phone -> resource_name, so the identity resolution runs once per person ever.

Reads return None on failure ("I could not tell you"), distinct from [] ("nothing there"), so a
database hiccup keeps the last good snapshot instead of silently emptying the address book — the
rule RosterStore already follows. Writes raise, because a write that failed has to be retried.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("mary.contactstore")


class ContactStore:
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

    # --- reads ---------------------------------------------------------------------------

    async def load(self) -> Optional[dict]:
        """{"contacts": [...], "links": {...}, "sync_token": str|None} — or None on failure."""
        if self._pool is None:
            return None
        try:
            from psycopg.rows import dict_row

            async with self._pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"SELECT resource_name, etag, name, emails, phones, source, "
                        f"preferred, extract(epoch from used_at)::bigint AS used_at "
                        f"FROM {self.schema}.contacts")
                    rows = [dict(r) for r in await cur.fetchall()]
                    await cur.execute(
                        f"SELECT phone, resource_name FROM {self.schema}.contact_links")
                    links = {r["phone"]: r["resource_name"] for r in await cur.fetchall()}
                    await cur.execute(
                        f"SELECT sync_token FROM {self.schema}.contact_sync WHERE id = 1")
                    row = await cur.fetchone()
            for r in rows:
                r["emails"] = list(r.get("emails") or [])
                r["phones"] = list(r.get("phones") or [])
            return {"contacts": rows, "links": links,
                    "sync_token": (row or {}).get("sync_token")}
        except Exception as exc:
            log.warning("contact store load failed: %s", exc)
            return None

    async def pending(self, *, limit: int = 20) -> Optional[list]:
        if self._pool is None:
            return None
        try:
            from psycopg.rows import dict_row

            async with self._pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"SELECT id, kind, resource_name, name, email, attempts "
                        f"FROM {self.schema}.contact_outbox ORDER BY id LIMIT %s", (limit,))
                    return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("contact outbox read failed: %s", exc)
            return None

    # --- writes --------------------------------------------------------------------------

    async def enqueue(self, job: dict) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {self.schema}.contact_outbox (kind, resource_name, name, email)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (resource_name, email) DO NOTHING""",
                (job["kind"], job.get("resource_name") or "", job.get("name"), job["email"]))

    async def done(self, job_id: int) -> None:
        """Delete, not mark-done: the row holds a third party's address and should not linger."""
        async with self._pool.connection() as conn:
            await conn.execute(
                f"DELETE FROM {self.schema}.contact_outbox WHERE id = %s", (job_id,))

    async def fail(self, job_id: int, error: str, max_attempts: int) -> bool:
        """Record the failure. Returns True when the job is terminal (and drops it)."""
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""UPDATE {self.schema}.contact_outbox
                        SET attempts = attempts + 1, last_error = %s
                        WHERE id = %s RETURNING attempts""", (error[:500], job_id))
                row = await cur.fetchone()
            attempts = (row or [0])[0]
            if attempts >= max_attempts:
                await conn.execute(
                    f"DELETE FROM {self.schema}.contact_outbox WHERE id = %s", (job_id,))
                return True
        return False

    async def upsert(self, c: dict) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {self.schema}.contacts
                        (resource_name, etag, name, emails, phones, source, preferred, used_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, to_timestamp(%s))
                    ON CONFLICT (resource_name) DO UPDATE SET
                        etag = EXCLUDED.etag, name = EXCLUDED.name,
                        emails = EXCLUDED.emails, phones = EXCLUDED.phones,
                        source = EXCLUDED.source, preferred = EXCLUDED.preferred,
                        used_at = EXCLUDED.used_at, updated_at = now()""",
                (c["resource_name"], c.get("etag") or "", c.get("name") or "",
                 list(c.get("emails") or []), list(c.get("phones") or []),
                 c.get("source") or "google", c.get("preferred"), c.get("used_at")))

    async def replace(self, contacts: list, sync_token: Optional[str], *, full: bool) -> None:
        """Mirror the snapshot. A FULL sweep is authoritative and reaps anything it did not see."""
        async with self._pool.connection() as conn:
            for c in contacts:
                await conn.execute(
                    f"""INSERT INTO {self.schema}.contacts
                            (resource_name, etag, name, emails, phones, source, preferred, used_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, to_timestamp(%s))
                        ON CONFLICT (resource_name) DO UPDATE SET
                            etag = EXCLUDED.etag, name = EXCLUDED.name,
                            emails = EXCLUDED.emails, phones = EXCLUDED.phones,
                            source = EXCLUDED.source, preferred = EXCLUDED.preferred,
                            used_at = EXCLUDED.used_at, updated_at = now()""",
                    (c["resource_name"], c.get("etag") or "", c.get("name") or "",
                     list(c.get("emails") or []), list(c.get("phones") or []),
                     c.get("source") or "google", c.get("preferred"), c.get("used_at")))
            if full and contacts:
                keep = [c["resource_name"] for c in contacts]
                # Cascade: a contact the sweep did not return is gone from Google, so its link
                # must go too or a stale phone keeps resolving to a person who no longer exists.
                await conn.execute(
                    f"""DELETE FROM {self.schema}.contact_links WHERE resource_name IN (
                            SELECT resource_name FROM {self.schema}.contacts
                            WHERE resource_name <> ALL(%s))""", (keep,))
                await conn.execute(
                    f"DELETE FROM {self.schema}.contacts WHERE resource_name <> ALL(%s)", (keep,))
            if sync_token:
                await conn.execute(
                    f"""INSERT INTO {self.schema}.contact_sync (id, sync_token, synced_at)
                        VALUES (1, %s, now())
                        ON CONFLICT (id) DO UPDATE SET
                            sync_token = EXCLUDED.sync_token, synced_at = now()""",
                    (sync_token,))

    async def link(self, phone: str, resource_name: str, source: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {self.schema}.contact_links (phone, resource_name, source)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (phone) DO UPDATE SET
                        resource_name = EXCLUDED.resource_name, source = EXCLUDED.source,
                        linked_at = now()""",
                (phone, resource_name, source))

    def _ddl(self) -> str:
        s = self.schema
        return f"""
        CREATE SCHEMA IF NOT EXISTS {s};
        CREATE TABLE IF NOT EXISTS {s}.contacts (
            resource_name text PRIMARY KEY,
            etag          text NOT NULL DEFAULT '',
            name          text NOT NULL DEFAULT '',
            emails        text[] NOT NULL DEFAULT '{{}}',
            phones        text[] NOT NULL DEFAULT '{{}}',
            source        text NOT NULL DEFAULT 'google',
            preferred     text,
            used_at       timestamptz,
            updated_at    timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS {s}.contact_outbox (
            id            bigserial PRIMARY KEY,
            kind          text NOT NULL,
            resource_name text NOT NULL DEFAULT '',
            name          text,
            email         text NOT NULL,
            attempts      int  NOT NULL DEFAULT 0,
            last_error    text,
            created_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (resource_name, email)
        );
        CREATE TABLE IF NOT EXISTS {s}.contact_sync (
            id         int PRIMARY KEY DEFAULT 1,
            sync_token text,
            synced_at  timestamptz
        );
        CREATE TABLE IF NOT EXISTS {s}.contact_links (
            phone         text PRIMARY KEY,
            resource_name text NOT NULL,
            source        text NOT NULL,
            linked_at     timestamptz NOT NULL DEFAULT now()
        );
        """
