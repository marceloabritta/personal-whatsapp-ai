"""HistoryIndexer — keeps the lisa_search index (sql_schema.py) in step with Evolution's "Message".

WHY AN INDEX AT ALL. "Message" is Evolution's table: 200 MB of raw protobuf-as-JSON whose words sit
under a dozen different keys, with no index but its primary key. A search over it is a sequential
scan that unpacks every row, and a chat is stored under two ids. lisa_search holds the words once,
folded for accent-blind matching, under ONE canonical chat key, with a trigram index — and Evolution's
own tables are only ever read.

THE CURSOR. "Message" rows have cuid v1 ids, which sort by the moment Evolution STORED the row
(index_text.cuid_ms). So "what is new" is a range scan of the primary key — `id > cursor ORDER BY id`
— and costs nothing however large the table grows. That is also exactly what catches a late
backlog: when Evolution stores hours of old messages at once, their sent-times are old but their
ids are new. Two things can still slip past a plain `id > cursor`: a row whose id was minted just
before the cursor's but committed just after it, and a batch Evolution writes in one long
transaction. So every incremental pass re-reads the last OVERLAP (15 min) of ids; the upsert makes
that free when nothing changed. There is no index on "messageTimestamp", and none is needed.

THE FIRST START is the same loop from an empty cursor: a backfill in pages of `batch` rows (500),
each read in its own short statement and written in its own short transaction, with a pause
between pages so the gateway sharing the database (1 CPU) never waits on us. The cursor is saved
with every page, so a restart resumes where it stopped.

IDENTITY. A 1:1 chat is stored under an opaque `<n>@lid` and, for messages sent through the API,
under the phone jid. Evolution reports the twin as key.remoteJidAlt (1:1) and key.participantAlt
(group senders). Every such pair teaches an alias lid -> phone jid; the canonical chat is the phone
jid when one is known, else the lid. An alias learned late RE-KEYS what was indexed under the lid
(the chat row merges, its messages move), so a person is never split in two.

NAMES come from every source, best first: the owner's own address book (Directory, when contacts
is on), Evolution's "Chat".name and "Contact".pushName, and the latest pushName the person sent
with a message. A bare number is never a name.

VOICE NOTES are indexed with no words until a transcript exists. Transcripts land in
<log_schema>.transcripts (keyed by key.id) — before or after the note is indexed — and each pass
pulls the new ones in. A note the voice front gave up on is marked from lisa_search.voice_attempts
when that table exists.

Never raises into the app: each step logs its failure and the next pass retries. All writes go to
lisa_search."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from . import index_text as it
from .sql_schema import ddl, ident

log = logging.getLogger("mary.history.indexer")

OVERLAP_MS = 15 * 60 * 1000       # ids re-read on every incremental pass (commit-order slack)
NAMES_EVERY_S = 900               # names are rebuilt at least this often (address-book changes)
NAMES_SOON_S = 300                # ... and within this long of a new pushName
TRANSCRIPT_OVERLAP = "10 minutes"
_VOICE_FAILED = ("failed", "error", "too_long", "too_large", "skipped", "unsupported", "gone")


class HistoryIndexer:
    def __init__(self, dsn: str, *, schema: str = "lisa_search", settings=None, directory=None,
                 batch_size: Optional[int] = None, pause_seconds: Optional[float] = None) -> None:
        self.dsn = dsn
        self.schema = ident(schema)
        self.settings = settings
        self.directory = directory
        s = settings
        self.interval = float(getattr(s, "history_index_interval_seconds", 60) or 60)
        self.batch = int(batch_size or getattr(s, "history_index_batch", 0) or 500)
        self.pause = float(pause_seconds if pause_seconds is not None
                           else getattr(s, "history_index_pause_seconds", 0.25))
        self.owner_name = getattr(s, "owner_name", "") or ""
        self.log_schema = ident(getattr(s, "log_schema", "mary_log") or "mary_log")
        self.instance_name = getattr(s, "evolution_instance", "") or ""
        try:
            self.tags = list(getattr(s, "tags", []) or [])
        except Exception:
            self.tags = []
        self._pool: Any = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.ext: Optional[str] = None
        self._owner_jid: Optional[str] = None
        self._instance_id: Optional[str] = None
        self._identity_done = False
        self._alias: dict[str, str] = {}       # lid -> phone jid
        self._aliases_loaded = False
        self._names_at = 0.0
        self._names_dirty = True
        self._names_soon = False
        self._known_jids: set[str] = set()
        self._stats_all = True          # recount every chat on the first pass after a start
        self._dir_ready = False
        self.last_pass: dict = {}

    # -- lifecycle ---------------------------------------------------------------------------
    async def open(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(
            self.dsn, min_size=1, max_size=2, open=False,
            kwargs={"autocommit": True, "application_name": "lisa_history_indexer",
                    "options": "-c statement_timeout=60000 -c lock_timeout=10000"})
        await self._pool.open()
        await self._ensure_schema()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
            self._task = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never into the app; the next pass retries
                log.warning("history index pass failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # -- one pass ----------------------------------------------------------------------------
    async def run_once(self) -> dict:
        """One sync pass. Each step is independent: a failure is logged and the rest still run."""
        t0 = time.monotonic()
        stats: dict = {"ingested": 0, "pages": 0, "edits": 0, "aliases": 0, "transcripts": 0,
                       "voice_marked": 0, "names": 0, "chats": 0}
        touched: set[str] = set()
        for name, step in (("identity", self._identity), ("aliases", self._load_aliases),
                           ("ingest", lambda: self._ingest(stats, touched)),
                           ("transcripts", lambda: self._transcripts(stats)),
                           ("voice", lambda: self._voice_attempts(stats)),
                           ("names", lambda: self._names(stats)),
                           ("chat_stats", lambda: self._chat_stats(touched, stats))):
            if self._stop.is_set() and name == "ingest":
                break
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stats.setdefault("errors", []).append(name)
                log.warning("history index %s failed: %s", name, exc)
        stats["ms"] = int((time.monotonic() - t0) * 1000)
        self.last_pass = stats
        if stats["ingested"] or stats["transcripts"] or stats.get("errors") or stats["aliases"]:
            log.info("%s", json.dumps({"history_index": stats}, separators=(",", ":")))
        return stats

    # -- schema & identity -------------------------------------------------------------------
    async def _ensure_schema(self) -> None:
        s = self.schema
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
                ext = await self._ext_schema(conn)
                if ext is None:
                    await conn.execute(f"CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA {s}")
                    ext = await self._ext_schema(conn) or s
                self.ext = ident(ext)
                await conn.execute(ddl(s, self.ext))

    @staticmethod
    async def _ext_schema(conn) -> Optional[str]:
        cur = await conn.execute(
            "SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'pg_trgm'")
        row = await cur.fetchone()
        return row[0].strip('"') if row else None

    async def _identity(self) -> None:
        """The owner's phone jid (his chat with himself is `self`) and the Evolution instance whose
        rows are his. Settings first; Evolution's "Instance" row otherwise."""
        if self._identity_done:
            return
        owner = it.bare_jid(getattr(self.settings, "owner_jid", "") or "")
        if owner and "@" not in owner:
            owner = f"{owner}@s.whatsapp.net"
        rows: list = []
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    'SELECT id, "ownerJid", name FROM "Instance" ORDER BY (name = %s) DESC LIMIT 2',
                    (self.instance_name,))
                rows = await cur.fetchall()
        except Exception as exc:  # no "Instance" table: every row is the owner's
            log.warning("history index: no Evolution instance row (%s); indexing every row", exc)
        pick = [r for r in rows if r[2] == self.instance_name] or (rows if len(rows) == 1 else [])
        if pick:
            self._instance_id = pick[0][0]
            owner = owner or it.bare_jid(pick[0][1] or "")
        self._owner_jid = owner or None
        self._identity_done = True
        await self._put_state({"owner_name": self.owner_name, "owner_jid": self._owner_jid or ""})

    async def _load_aliases(self) -> None:
        if self._aliases_loaded:
            return
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT jid, canonical FROM {self.schema}.jids WHERE jid <> canonical")
            self._alias = {j: c for j, c in await cur.fetchall()}
        self._aliases_loaded = True

    def _canon(self, jid: Optional[str]) -> Optional[str]:
        if not jid:
            return None
        return self._alias.get(jid, jid)

    # -- state -------------------------------------------------------------------------------
    async def _get_state(self, conn) -> dict:
        cur = await conn.execute(f"SELECT key, value FROM {self.schema}.sync_state")
        return {k: v for k, v in await cur.fetchall()}

    async def _put_state(self, values: dict, conn=None) -> None:
        sql = (f"INSERT INTO {self.schema}.sync_state (key, value, updated_at) VALUES (%s, %s, now()) "
               f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()")
        rows = [(k, None if v is None else str(v)) for k, v in values.items()]
        if conn is not None:
            async with conn.cursor() as cur:
                await cur.executemany(sql, rows)
            return
        async with self._pool.connection() as c:
            async with c.cursor() as cur:
                await cur.executemany(sql, rows)

    # -- messages ----------------------------------------------------------------------------
    async def _ingest(self, stats: dict, touched: set) -> None:
        async with self._pool.connection() as conn:
            state = await self._get_state(conn)
        cursor = state.get("msg_cursor") or ""
        backfilling = state.get("backfill_done") is None
        ms = it.cuid_ms(cursor)
        start = it.cuid_floor(ms - OVERLAP_MS) if (ms and not backfilling) else cursor
        if cursor and ms is None:
            log.warning("history index: cursor %r is not a cuid; re-reading from it", cursor[:12])
        inst = ' AND "instanceId" = %s' if self._instance_id else ""
        sql = ('SELECT id, key::text, "pushName", "messageType", message::text, '
               '"contextInfo"->>\'stanzaId\', "messageTimestamp" '
               f'FROM "Message" WHERE id > %s{inst} ORDER BY id LIMIT %s')
        exhausted = False
        rows: list = []
        while not self._stop.is_set():
            params = (start, self._instance_id, self.batch) if inst else (start, self.batch)
            async with self._pool.connection() as conn:
                cur = await conn.execute(sql, params)
                rows = await cur.fetchall()
            if not rows:
                exhausted = True
                break
            page = await asyncio.to_thread(self._process, rows)
            last = rows[-1][0]
            new_cursor = max(cursor, last)
            try:
                await self._write_page(page, new_cursor, stats, touched)
            except Exception:
                # The page rolled back but _process already learned its aliases in memory; reload
                # them from the table so the retry re-keys what it has to.
                self._aliases_loaded = False
                raise
            cursor, start = new_cursor, last
            stats["pages"] += 1
            if len(rows) < self.batch:
                exhausted = True
                break
            if self.pause:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.pause)
                except asyncio.TimeoutError:
                    pass
        if exhausted and backfilling:
            await self._put_state({"backfill_done": int(time.time())})
            self._names_dirty = True
            self._stats_all = True
            log.info("%s", json.dumps({"history_index": "backfill_done", "cursor": cursor}))
        if rows and it.cuid_ms(rows[-1][0]) is None:
            log.warning("history index: Evolution ids are no longer cuid v1; the cursor still works "
                        "but the overlap re-read is off")

    def _process(self, rows: list) -> dict:
        """Rows of "Message" -> what to write. Pure CPU; runs off the event loop."""
        msgs: dict[str, dict] = {}
        edits: list[dict] = []
        jids: dict[str, dict] = {}
        chats: dict[str, dict] = {}
        learned: dict[str, str] = {}
        voice_ids: list[str] = []
        owner = self._owner_jid

        def seen(jid: Optional[str], push: Optional[str] = None, ts: Optional[int] = None) -> None:
            if not jid:
                return
            j = jids.setdefault(jid, {"jid": jid, "push_name": None, "push_ts": None})
            name = it.clean_name(push, jid)
            if name and ts is not None and (j["push_ts"] is None or ts >= j["push_ts"]):
                j["push_name"], j["push_ts"] = name, ts

        def learn(lid: Optional[str], phone: Optional[str]) -> None:
            if (it.jid_domain(lid) == "lid" and it.jid_domain(phone) == "s.whatsapp.net"
                    and self._alias.get(lid) != phone):
                learned[lid] = phone
                self._alias[lid] = phone

        for src_id, key_txt, push, mtype, msg_txt, stanza, ts in rows:
            try:
                key = json.loads(key_txt) if key_txt else {}
            except ValueError:
                continue
            jid = it.bare_jid(key.get("remoteJid"))
            if not jid or jid.endswith("@broadcast") or not key.get("id"):
                continue
            alt = it.bare_jid(key.get("remoteJidAlt"))
            part = it.bare_jid(key.get("participant"))
            part_alt = it.bare_jid(key.get("participantAlt"))
            from_me = key.get("fromMe") in (True, "true")
            # Teach aliases from both shapes, whichever side the phone is on.
            learn(jid, alt)
            learn(alt, jid)
            learn(part, part_alt)
            learn(part_alt, part)
            try:
                message = json.loads(msg_txt) if msg_txt else None
            except ValueError:
                message = None
            rec = it.extract(mtype, message, context_info={"stanzaId": stanza} if stanza else None,
                             from_me=from_me, owner_name=self.owner_name, tags=self.tags)
            group = it.jid_domain(jid) in ("g.us", "newsletter")
            chat = jid if group else self._canon(jid)
            # Who wrote it, as a raw jid: the jids table resolves it to a name (and follows a lid
            # learned later). The owner's own messages are his, wherever they were sent.
            if from_me:
                sender = owner
            elif group:
                sender = part_alt or part or None
                seen(part, push, ts)
                seen(part_alt, push, ts)
            else:
                sender = jid
                seen(jid, push, ts)
            seen(jid)
            seen(alt)
            if rec is None:
                continue
            ts = int(ts or 0)
            if rec["edit_of"]:
                edits.append({"target": rec["edit_of"], "text": rec["text"], "msg_id": key["id"],
                              "rec": rec, "src_id": src_id, "chat": chat, "jid": jid, "ts": ts,
                              "from_me": from_me, "sender": sender, "push": push})
                continue
            row = {"msg_id": key["id"], "src_id": src_id, "chat": chat, "jid": jid, "ts": ts,
                   "from_me": from_me, "by_lisa": rec["by_lisa"], "to_lisa": rec["to_lisa"],
                   "sender_jid": sender, "push_name": it.clean_name(push) if not from_me else None,
                   "kind": rec["kind"], "text": rec["text"], "file_name": rec["file_name"],
                   "mimetype": rec["mimetype"], "seconds": rec["seconds"],
                   "quoted_msg_id": rec["quoted_msg_id"],
                   "transcript": "pending" if rec["kind"] == "voice" else None,
                   "norm": it.search_norm(rec["text"], rec["file_name"])}
            prev = msgs.get(row["msg_id"])
            if prev is not None:
                # The same message stored twice: keep the copy with words, and the earliest time
                # (a re-sent copy carries a fresh timestamp).
                if not row["text"] and prev["text"]:
                    row = {**prev}
                row["ts"] = min(prev["ts"], ts) if prev["ts"] else ts
                row["src_id"] = min(prev["src_id"], src_id)
            msgs[row["msg_id"]] = row
            if row["kind"] == "voice":
                voice_ids.append(row["msg_id"])
            c = chats.setdefault(chat, {"chat": chat, "aliases": set()})
            if jid != chat:
                c["aliases"].add(jid)
            if alt and alt != chat and not group:
                c["aliases"].add(alt)
        for e in edits:
            c = chats.setdefault(e["chat"], {"chat": e["chat"], "aliases": set()})
        # Re-canonicalise with everything learned in this page (a lid's alias may come later in it).
        for row in msgs.values():
            if it.jid_domain(row["chat"]) == "lid":
                row["chat"] = self._canon(row["chat"])
        for e in edits:
            if it.jid_domain(e["chat"]) == "lid":
                e["chat"] = self._canon(e["chat"])
        merged: dict[str, dict] = {}
        for c in chats.values():
            canon = self._canon(c["chat"]) if it.jid_domain(c["chat"]) == "lid" else c["chat"]
            m = merged.setdefault(canon, {"chat": canon, "aliases": set()})
            m["aliases"] |= c["aliases"] | ({c["chat"]} if c["chat"] != canon else set())
            m["aliases"].discard(canon)
        for m in merged.values():
            m["kind"] = it.chat_kind_of(m["chat"], owner)
            m["phone"] = it.phone_of(m["chat"])
            m["aliases"] = sorted(m["aliases"])
        for j in jids.values():
            j["canonical"] = self._canon(j["jid"])
        if owner:
            jids.setdefault(owner, {"jid": owner, "push_name": None, "push_ts": None,
                                    "canonical": owner})
        return {"msgs": list(msgs.values()), "edits": edits, "jids": list(jids.values()),
                "chats": list(merged.values()), "learned": learned, "voice_ids": voice_ids}

    async def _write_page(self, page: dict, cursor: str, stats: dict, touched: set) -> None:
        s = self.schema
        async with self._pool.connection() as conn:
            async with conn.transaction():
                # Transcripts that already exist for this page's voice notes.
                if page["voice_ids"]:
                    found = await self._transcripts_for(conn, page["voice_ids"])
                    for row in page["msgs"]:
                        t = found.get(row["msg_id"])
                        if row["kind"] == "voice" and t is not None:
                            row["text"], row["transcript"] = t, "done"
                            row["norm"] = it.search_norm(t, row["file_name"])
                async with conn.cursor() as cur:
                    if page["learned"]:
                        await self._rekey(cur, page["learned"], touched)
                        stats["aliases"] += len(page["learned"])
                    if page["jids"]:
                        await cur.executemany(
                            f"""INSERT INTO {s}.jids (jid, canonical, push_name, push_ts)
                                VALUES (%(jid)s, %(canonical)s, %(push_name)s, %(push_ts)s)
                                ON CONFLICT (jid) DO UPDATE SET
                                  canonical = EXCLUDED.canonical,
                                  push_name = CASE WHEN EXCLUDED.push_ts IS NOT NULL AND
                                      EXCLUDED.push_ts >= coalesce({s}.jids.push_ts, 0)
                                      THEN EXCLUDED.push_name ELSE {s}.jids.push_name END,
                                  push_ts = GREATEST({s}.jids.push_ts, EXCLUDED.push_ts)
                                WHERE {s}.jids.canonical IS DISTINCT FROM EXCLUDED.canonical
                                   OR (EXCLUDED.push_ts IS NOT NULL AND
                                       EXCLUDED.push_ts >= coalesce({s}.jids.push_ts, 0) AND
                                       EXCLUDED.push_name IS DISTINCT FROM {s}.jids.push_name)""",
                            page["jids"])
                        fresh = [j["jid"] for j in page["jids"] if j["jid"] not in self._known_jids]
                        if fresh or any(j.get("push_name") for j in page["jids"]):
                            self._names_soon = True
                        if fresh:
                            self._names_dirty = True
                            self._known_jids.update(fresh)
                    if page["chats"]:
                        await cur.executemany(
                            f"""INSERT INTO {s}.chats (chat, kind, phone, aliases)
                                VALUES (%(chat)s, %(kind)s, %(phone)s, %(aliases)s)
                                ON CONFLICT (chat) DO UPDATE SET
                                  kind = EXCLUDED.kind,
                                  phone = coalesce(EXCLUDED.phone, {s}.chats.phone),
                                  aliases = ARRAY(SELECT DISTINCT a FROM unnest(
                                      {s}.chats.aliases || EXCLUDED.aliases) a ORDER BY a)
                                WHERE {s}.chats.kind IS DISTINCT FROM EXCLUDED.kind
                                   OR NOT (EXCLUDED.aliases <@ {s}.chats.aliases)
                                   OR (EXCLUDED.phone IS NOT NULL AND
                                       {s}.chats.phone IS DISTINCT FROM EXCLUDED.phone)""",
                            page["chats"])
                        touched.update(c["chat"] for c in page["chats"])
                    if page["msgs"]:
                        await cur.executemany(
                            f"""INSERT INTO {s}.messages AS m (msg_id, src_id, chat, jid, ts, from_me,
                                  by_lisa, to_lisa, sender_jid, push_name, kind, text, file_name,
                                  mimetype, seconds, quoted_msg_id, transcript, norm)
                                VALUES (%(msg_id)s, %(src_id)s, %(chat)s, %(jid)s, %(ts)s, %(from_me)s,
                                  %(by_lisa)s, %(to_lisa)s, %(sender_jid)s, %(push_name)s, %(kind)s,
                                  %(text)s, %(file_name)s, %(mimetype)s, %(seconds)s,
                                  %(quoted_msg_id)s, %(transcript)s, %(norm)s)
                                ON CONFLICT (msg_id) DO UPDATE SET
                                  ts = LEAST(m.ts, EXCLUDED.ts),
                                  text = CASE WHEN EXCLUDED.text <> '' AND NOT m.edited
                                              THEN EXCLUDED.text ELSE m.text END,
                                  norm = CASE WHEN EXCLUDED.text <> '' AND NOT m.edited
                                              THEN EXCLUDED.norm ELSE m.norm END,
                                  file_name = coalesce(m.file_name, EXCLUDED.file_name),
                                  mimetype = coalesce(m.mimetype, EXCLUDED.mimetype),
                                  seconds = coalesce(m.seconds, EXCLUDED.seconds),
                                  quoted_msg_id = coalesce(m.quoted_msg_id, EXCLUDED.quoted_msg_id),
                                  transcript = CASE WHEN EXCLUDED.transcript = 'done'
                                               THEN 'done' ELSE m.transcript END
                                WHERE EXCLUDED.ts < m.ts
                                   OR (EXCLUDED.text <> '' AND NOT m.edited
                                       AND EXCLUDED.text IS DISTINCT FROM m.text)
                                   OR (EXCLUDED.transcript = 'done'
                                       AND m.transcript IS DISTINCT FROM 'done')""",
                            page["msgs"])
                        stats["ingested"] += len(page["msgs"])
                    if page["edits"]:
                        stats["edits"] += await self._apply_edits(cur, page["edits"], touched)
                await self._put_state({"msg_cursor": cursor}, conn)

    async def _apply_edits(self, cur, edits: list, touched: set) -> int:
        """An edit rewrites the message it targets. When that message was never stored, the edit
        is indexed on its own (it is the only copy of what was said), replying to the target."""
        s = self.schema
        await cur.execute(f"SELECT msg_id, file_name FROM {s}.messages WHERE msg_id = ANY(%s)",
                          ([e["target"] for e in edits],))
        have = {r[0]: r[1] for r in await cur.fetchall()}
        n = 0
        for e in edits:
            if e["target"] in have:
                await cur.execute(
                    f"""UPDATE {s}.messages SET text = %s, norm = %s, edited = true
                        WHERE msg_id = %s AND text IS DISTINCT FROM %s""",
                    (e["text"], it.search_norm(e["text"], have[e["target"]]), e["target"], e["text"]))
            else:
                r = e["rec"]
                await cur.execute(
                    f"""INSERT INTO {s}.messages (msg_id, src_id, chat, jid, ts, from_me, by_lisa,
                          to_lisa, sender_jid, push_name, kind, text, quoted_msg_id, edited, norm)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s)
                        ON CONFLICT (msg_id) DO NOTHING""",
                    (e["msg_id"], e["src_id"], e["chat"], e["jid"], e["ts"], e["from_me"],
                     r["by_lisa"], r["to_lisa"], e["sender"],
                     None if e["from_me"] else it.clean_name(e["push"]), r["kind"], e["text"],
                     e["target"], it.search_norm(e["text"], None)))
                touched.add(e["chat"])
            n += 1
        return n

    async def _rekey(self, cur, learned: dict, touched: set) -> None:
        """lid -> phone jid learned: move what was indexed under the lid to the phone jid."""
        s = self.schema
        for lid, phone in learned.items():
            await cur.execute(f"UPDATE {s}.messages SET chat = %s WHERE chat = %s", (phone, lid))
            moved = cur.rowcount or 0
            await cur.execute(f"UPDATE {s}.jids SET canonical = %s WHERE jid = %s", (phone, lid))
            await cur.execute(f"SELECT aliases FROM {s}.chats WHERE chat = %s", (lid,))
            row = await cur.fetchone()
            if row is not None or moved:
                aliases = sorted({lid, *((row[0] if row else None) or [])} - {phone})
                await cur.execute(
                    f"""INSERT INTO {s}.chats (chat, kind, phone, aliases)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (chat) DO UPDATE SET aliases = ARRAY(SELECT DISTINCT a FROM
                            unnest({s}.chats.aliases || EXCLUDED.aliases) a ORDER BY a)""",
                    (phone, it.chat_kind_of(phone, self._owner_jid), it.phone_of(phone), aliases))
                await cur.execute(f"DELETE FROM {s}.chats WHERE chat = %s", (lid,))
                touched.add(phone)
                touched.discard(lid)

    # -- voice notes -------------------------------------------------------------------------
    async def _transcripts_for(self, conn, ids: list) -> dict:
        if not await self._exists(conn, f"{self.log_schema}.transcripts"):
            return {}
        cur = await conn.execute(
            f"SELECT wa_id, text FROM {self.log_schema}.transcripts WHERE wa_id = ANY(%s)", (ids,))
        return {k: (v or "") for k, v in await cur.fetchall()}

    @staticmethod
    async def _exists(conn, rel: str) -> bool:
        cur = await conn.execute("SELECT to_regclass(%s) IS NOT NULL", (rel,))
        return bool((await cur.fetchone())[0])

    async def _transcripts(self, stats: dict) -> None:
        """Transcripts written since the last pass (with a little overlap) -> the voice notes."""
        s, ls = self.schema, self.log_schema
        async with self._pool.connection() as conn:
            if not await self._exists(conn, f"{ls}.transcripts"):
                return
            state = await self._get_state(conn)
            since = state.get("transcripts_cursor") or "-infinity"
            cur = await conn.execute(
                f"""SELECT wa_id, text, created_at::text FROM {ls}.transcripts
                    WHERE created_at > %s::timestamptz - interval '{TRANSCRIPT_OVERLAP}'
                    ORDER BY created_at""", (since,))
            rows = await cur.fetchall()
            if not rows:
                return
            async with conn.transaction():
                async with conn.cursor() as c:
                    await c.executemany(
                        f"""UPDATE {s}.messages SET text = %(t)s, norm = %(n)s, transcript = 'done'
                            WHERE msg_id = %(id)s AND kind = 'voice'
                              AND (transcript IS DISTINCT FROM 'done' OR text IS DISTINCT FROM %(t)s)""",
                        [{"id": w, "t": t or "", "n": it.fold(t or "")} for w, t, _ in rows])
                    stats["transcripts"] += max(c.rowcount or 0, 0)
                await self._put_state({"transcripts_cursor": rows[-1][2]}, conn)

    async def _voice_attempts(self, stats: dict) -> None:
        """Voice notes the voice front gave up on (lisa_search.voice_attempts, when it exists)."""
        s = self.schema
        async with self._pool.connection() as conn:
            if not await self._exists(conn, f"{s}.voice_attempts"):
                return
            cur = await conn.execute(
                f"""UPDATE {s}.messages m
                    SET transcript = CASE WHEN va.status = 'expired' THEN 'expired' ELSE 'failed' END
                    FROM {s}.voice_attempts va
                    WHERE va.msg_id = m.msg_id AND m.kind = 'voice'
                      AND m.transcript IS DISTINCT FROM 'done'
                      AND va.status IN ('expired', {", ".join("'%s'" % v for v in _VOICE_FAILED)})
                      AND m.transcript IS DISTINCT FROM
                          CASE WHEN va.status = 'expired' THEN 'expired' ELSE 'failed' END""")
            stats["voice_marked"] += cur.rowcount or 0

    # -- names -------------------------------------------------------------------------------
    async def _names(self, stats: dict) -> None:
        d = self.directory
        dir_ready = bool(d is not None and getattr(d, "ready", False))
        age = time.monotonic() - self._names_at
        due = (self._names_dirty or dir_ready != self._dir_ready or age > NAMES_EVERY_S
               or (self._names_soon and age > NAMES_SOON_S))
        if not due:
            return
        s = self.schema
        inst = ' WHERE "instanceId" = %s' if self._instance_id else ""
        params = (self._instance_id,) if inst else ()
        async with self._pool.connection() as conn:
            # Evolution's names. Small tables (thousands of rows): read whole, keep what changed.
            cur = await conn.execute(f'SELECT "remoteJid", name FROM "Chat"{inst}', params)
            chat_names = {it.bare_jid(j): n for j, n in await cur.fetchall() if j}
            cur = await conn.execute(f'SELECT "remoteJid", "pushName" FROM "Contact"{inst}', params)
            contact_names = {it.bare_jid(j): n for j, n in await cur.fetchall() if j}
            cur = await conn.execute(
                f"SELECT jid, canonical, chat_name, contact_name, push_name, push_ts, name FROM {s}.jids")
            jrows = {r[0]: list(r) for r in await cur.fetchall()}
            cur = await conn.execute(f"SELECT chat, kind, phone, name, names_norm FROM {s}.chats")
            crows = await cur.fetchall()

        # Every jid Evolution names, whether or not it has written yet (a group subject, a contact).
        for jid in set(chat_names) | set(contact_names):
            if jid not in jrows and not jid.endswith("@broadcast"):
                jrows[jid] = [jid, self._canon(jid), None, None, None, None, None]
        jid_updates = []
        by_canon: dict[str, list] = {}
        for jid, r in jrows.items():
            cn = it.clean_name(chat_names.get(jid), jid)
            kn = it.clean_name(contact_names.get(jid), jid)
            r[1] = self._canon(jid) or r[1]
            if (cn, kn) != (r[2], r[3]):
                r[2], r[3] = cn, kn
                r.append("dirty")
            by_canon.setdefault(r[1], []).append(r)

        best: dict[str, tuple[Optional[str], list]] = {}
        for canon, rs in by_canon.items():
            best[canon] = self._best_name(canon, rs, dir_ready)
        for jid, r in jrows.items():
            name = best.get(r[1], (None, []))[0]
            if name != r[6] or "dirty" in r:
                jid_updates.append({"jid": jid, "canonical": r[1], "chat_name": r[2],
                                    "contact_name": r[3], "name": name})
        chat_updates = []
        for chat, kind, phone, name, names_norm in crows:
            nm, all_names = best.get(chat, (None, []))
            if kind == "self":
                nm = self.owner_name or nm
                all_names = [self.owner_name, *all_names] if self.owner_name else all_names
            norm = it.fold(" | ".join(dict.fromkeys(n for n in all_names if n)))
            if (nm, norm) != (name, names_norm):
                chat_updates.append({"chat": chat, "name": nm, "norm": norm})
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    if jid_updates:
                        await cur.executemany(
                            f"""INSERT INTO {s}.jids (jid, canonical, chat_name, contact_name, name)
                                VALUES (%(jid)s, %(canonical)s, %(chat_name)s, %(contact_name)s, %(name)s)
                                ON CONFLICT (jid) DO UPDATE SET canonical = EXCLUDED.canonical,
                                  chat_name = EXCLUDED.chat_name, contact_name = EXCLUDED.contact_name,
                                  name = EXCLUDED.name""", jid_updates)
                    if chat_updates:
                        await cur.executemany(
                            f"UPDATE {s}.chats SET name = %(name)s, names_norm = %(norm)s "
                            f"WHERE chat = %(chat)s", chat_updates)
        stats["names"] += len(jid_updates) + len(chat_updates)
        self._known_jids = set(jrows)
        self._names_dirty = self._names_soon = False
        self._dir_ready = dir_ready
        self._names_at = time.monotonic()

    def _best_name(self, canon: str, rows: list, dir_ready: bool) -> tuple[Optional[str], list]:
        """(best name, every name) for one canonical jid. The owner's address book first (it is
        how HE names the person, and only a number held by exactly one card counts), then
        Evolution's "Chat".name, its "Contact".pushName, and the newest name the person sent with
        a message. Every name lands in names_norm, so list_chats finds the chat by any of them."""
        if self._owner_jid and canon == self._owner_jid and self.owner_name:
            return self.owner_name, [self.owner_name]
        book = None
        phone = it.phone_of(canon)
        if dir_ready and phone:
            try:
                cards = self.directory.by_phone(phone) or []
                names = {(c.get("name") or "").strip() for c in cards} - {""}
                book = names.pop() if len(names) == 1 else None
            except Exception:
                book = None
        # The canonical jid's own row first, so its names win over an alias's.
        rows = sorted(rows, key=lambda r: r[0] != canon)
        chat_n = next((r[2] for r in rows if r[2]), None)
        if it.jid_domain(canon) in ("g.us", "newsletter"):
            # A group's "Contact".pushName is whoever wrote last, not the group: the subject only.
            return chat_n, [chat_n] if chat_n else []
        contact_n = next((r[3] for r in rows if r[3]), None)
        pushes = sorted((r for r in rows if r[4]), key=lambda r: -(r[5] or 0))
        push_n = pushes[0][4] if pushes else None
        all_names = [n for n in (book, chat_n, contact_n, push_n) if n]
        all_names += [r[2] for r in rows if r[2]] + [r[3] for r in rows if r[3]]
        all_names += [r[4] for r in pushes]
        all_names = list(dict.fromkeys(all_names))
        return (all_names[0] if all_names else None), all_names

    # -- chat counts -------------------------------------------------------------------------
    async def _chat_stats(self, touched: set, stats: dict) -> None:
        """Counts and first/last times of the chats this pass wrote to — of every chat on the
        first pass after a start, so a pass cut short (a restart mid-backfill) never leaves one
        stale."""
        if not touched and not self._stats_all:
            return
        s = self.schema
        where, params = ("", ()) if self._stats_all else ("WHERE chat = ANY(%s)", (sorted(touched),))
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""UPDATE {s}.chats c SET n_messages = x.n, first_ts = x.f, last_ts = x.l
                    FROM (SELECT chat, count(*) FILTER (WHERE kind <> 'reaction') AS n,
                                 min(ts) AS f, max(ts) AS l
                          FROM {s}.messages {where} GROUP BY chat) x
                    WHERE c.chat = x.chat
                      AND (c.n_messages, c.first_ts, c.last_ts) IS DISTINCT FROM (x.n, x.f, x.l)""",
                params)
            stats["chats"] += cur.rowcount or 0
        self._stats_all = False
