"""PgHistoryStore — the HistoryStore (types.py) over the lisa_search index (sql_schema.py).

The queries behind the history agent's read-only tools. Every one runs in a READ-ONLY transaction
with a 10 s statement timeout (set on the connection, so no query can forget it), and none raises:
a bad regex, a timeout or a missing index is logged and answered with an empty result, which the
agent can recover from; an exception in the reply path it cannot.

Matching. The index keeps `norm`, the words + file name folded by index_text.fold (lowercase,
accents off, whitespace collapsed), with a trigram GIN index. A term is folded the same way and
matched as a substring — `norm LIKE '%term%'`, which the trigram index answers for any term of 3+
characters ("eua" finds "EUA", "acelerad" finds "Aceleradora"). With regex=True a term is a POSIX
regex matched with `~*` against the same column (accents stripped from the pattern, case left to
`~*`). Shorter terms still work, as a scan of ~160k short rows (well under a second). Lisa's own
messages, the owner's "@lisa …" requests to her, and reactions are left out unless include_lisa /
kinds ask for them.

Search returns the page of hits, the total, and the chats the hits fall in, from ONE pass over the
matches (a materialised CTE), then fetches the full rows only for the hits it returns. Snippets are
cut in Python from the original text, around the first match.

topics_together finds chats where every topic (a list of alternatives) comes up within
`within_days` of the others: the matching rows of the chats that have ALL topics come back sorted,
and the tightest window holding one of each is found per chat with a sliding window.

Names: a sender resolves through lisa_search.jids (the raw jid -> the best name the indexer found
for that person); the owner's own messages carry his name."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from . import index_text as it
from .sql_schema import ident
from .types import KINDS, ChatSummary, Hit, ReadResult, SearchResult, TopicChat

log = logging.getLogger("mary.history.store")

STATEMENT_TIMEOUT_MS = 10_000
SNIPPET = 300
READ_CAP = 1500
GET_CAP = 4000
BY_CHAT = 20
TOPIC_ROWS = 50_000
NAME_SIMILARITY = 0.45

_HIT_COLS = """m.msg_id, m.chat, c.name AS chat_name, c.kind AS chat_kind, m.ts, m.from_me,
    m.by_lisa, j.name AS sender_name, m.push_name, m.kind, m.text, m.file_name, m.mimetype,
    m.seconds, m.transcript, m.quoted_msg_id, m.src_id"""
_CHAT_COLS = "chat, name, kind, phone, n_messages, first_ts, last_ts"


def _empty_search() -> SearchResult:
    return {"total": 0, "hits": [], "by_chat": []}


def _clamp(n: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(n)))
    except (TypeError, ValueError):
        return default


# A voice note still "pending" this long after it was sent will never be transcribed: WhatsApp
# stops serving the audio after about 17-18 days (measured 2026-11-16: 17.0 days served, 17.9
# refused). Reported as expired, so the agent does not spend a slow, failing download on it.
VOICE_GONE_AFTER_SECONDS = 18 * 86400


def _voice_status(r: dict) -> Optional[str]:
    status = r.get("transcript")
    if status == "pending" and r.get("ts") and time.time() - int(r["ts"]) > VOICE_GONE_AFTER_SECONDS:
        return "expired"
    return status


class PgHistoryStore:
    def __init__(self, dsn: str, *, schema: str = "lisa_search", owner_name: Optional[str] = None,
                 statement_timeout_ms: int = STATEMENT_TIMEOUT_MS) -> None:
        self.dsn = dsn
        self.schema = ident(schema)
        self.owner_name = owner_name
        self.timeout_ms = int(statement_timeout_ms)
        self.ext: Optional[str] = None
        self._pool: Any = None

    # -- lifecycle ---------------------------------------------------------------------------
    async def open(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(
            self.dsn, min_size=1, max_size=4, open=False,
            kwargs={"application_name": "lisa_history_store",
                    "options": f"-c default_transaction_read_only=on "
                               f"-c statement_timeout={self.timeout_ms}"})
        await self._pool.open()
        try:
            rows = await self._q(
                "SELECT extnamespace::regnamespace::text AS ext FROM pg_extension "
                "WHERE extname = 'pg_trgm'")
            self.ext = ident(rows[0]["ext"].strip('"')) if rows else None
            if self.owner_name is None:
                rows = await self._q(
                    f"SELECT value FROM {self.schema}.sync_state WHERE key = 'owner_name'")
                self.owner_name = (rows[0]["value"] if rows else None) or None
        except Exception as exc:  # the index may not exist yet: queries will say so, empty
            log.warning("history store: index not readable yet: %s", exc)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _q(self, sql: str, params: Any = None, conn=None) -> list[dict]:
        from psycopg.rows import dict_row

        if conn is not None:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()
        async with self._pool.connection() as c:
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()

    # -- shared pieces -----------------------------------------------------------------------
    def _from(self) -> str:
        s = self.schema
        return (f"FROM {s}.messages m LEFT JOIN {s}.chats c ON c.chat = m.chat "
                f"LEFT JOIN {s}.jids j ON j.jid = m.sender_jid")

    async def _resolve_chat(self, conn, chat: Optional[str]) -> Optional[str]:
        """The canonical key for whatever the caller holds: the canonical key itself, the @lid
        twin, a bare phone number or group id."""
        if not chat:
            return None
        c = it.bare_jid(chat)
        cands = [c] if "@" in c else [f"{c}@s.whatsapp.net", f"{c}@g.us", f"{c}@lid", c]
        rows = await self._q(
            f"""SELECT canonical AS k FROM {self.schema}.jids WHERE jid = ANY(%(c)s)
                UNION ALL SELECT chat FROM {self.schema}.chats WHERE chat = ANY(%(c)s)""",
            {"c": cands}, conn)
        return rows[0]["k"] if rows else cands[0]

    async def _expand(self, conn, rows: list[dict]) -> None:
        """@mentions are stored as raw jid numbers; give them the names the index knows."""
        jids = sorted({j for r in rows for j in it.mention_jids(r.get("text") or "")})
        if not jids:
            return
        found = await self._q(f"SELECT jid, name FROM {self.schema}.jids "
                              f"WHERE jid = ANY(%s) AND name IS NOT NULL", (jids,), conn)
        names = {f["jid"]: f["name"] for f in found}
        for r in rows:
            r["text"] = it.expand_mentions(r.get("text") or "", names)

    def _filters(self, p: dict, *, chat: Optional[str], from_me: Optional[bool],
                 kinds: Optional[list], after: Optional[int], before: Optional[int],
                 include_lisa: bool) -> list[str]:
        w: list[str] = []
        if chat:
            w.append("m.chat = %(chat)s")
            p["chat"] = chat
        if from_me is not None:
            w.append("m.from_me = %(from_me)s")
            p["from_me"] = bool(from_me)
        ks = [k for k in (kinds or []) if k in KINDS]
        if kinds and ks:
            w.append("m.kind = ANY(%(kinds)s)")
            p["kinds"] = ks
        else:
            w.append("m.kind <> 'reaction'")
        if after is not None:
            w.append("m.ts >= %(after)s")
            p["after"] = int(after)
        if before is not None:
            w.append("m.ts < %(before)s")
            p["before"] = int(before)
        if not include_lisa:
            w.append("NOT m.by_lisa AND NOT m.to_lisa")
        return w

    @staticmethod
    def _term_conds(p: dict, terms: list[str], regex: bool, prefix: str = "t") -> list[str]:
        out = []
        for i, t in enumerate(terms):
            k = f"{prefix}{i}"
            if regex:
                out.append(f"m.norm ~* %({k})s")
                p[k] = it.fold_regex(t)
            else:
                out.append(f"m.norm LIKE %({k})s")
                p[k] = it.like_pattern(t)
        return out

    def _hit(self, r: dict, *, needles: Optional[list] = None, regex: bool = False,
             cap: int = READ_CAP) -> Hit:
        text = r.get("text") or ""
        text = it.snippet(text, needles or [], width=SNIPPET, regex=regex) if needles is not None \
            else it.cap(text, cap)
        if r["from_me"]:
            sender = self.owner_name or "owner"
        else:
            sender = (r.get("sender_name") or r.get("push_name")
                      or (r.get("chat_name") if r.get("chat_kind") == "contact" else None))
        return {"msg_id": r["msg_id"], "chat": r["chat"], "chat_name": r.get("chat_name"),
                "ts": int(r["ts"]), "from_me": bool(r["from_me"]), "by_lisa": bool(r["by_lisa"]),
                "sender": sender, "kind": r["kind"], "text": text, "file_name": r.get("file_name"),
                "mimetype": r.get("mimetype"), "seconds": r.get("seconds"),
                "transcript": _voice_status(r) if r["kind"] == "voice" else None,
                "quoted_msg_id": r.get("quoted_msg_id")}

    @staticmethod
    def _chat(r: Optional[dict], chat: Optional[str] = None, n_hits: Optional[int] = None) -> ChatSummary:
        if not r:
            out: ChatSummary = {"chat": chat or "", "name": None, "kind": it.chat_kind_of(chat or "", None),
                                "phone": it.phone_of(chat), "n_messages": 0, "first_ts": 0, "last_ts": 0}
        else:
            out = {"chat": r["chat"], "name": r.get("name"), "kind": r["kind"], "phone": r.get("phone"),
                   "n_messages": int(r.get("n_messages") or 0), "first_ts": int(r.get("first_ts") or 0),
                   "last_ts": int(r.get("last_ts") or 0)}
        if n_hits is not None:
            out["n_hits"] = int(n_hits)
        return out

    def _log(self, what: str, t0: float, **kw) -> None:
        ms = int((time.monotonic() - t0) * 1000)
        log.info("history store %s %d ms %s", what, ms, kw if kw else "")

    # -- search ------------------------------------------------------------------------------
    async def search(self, *, terms: list[str], match: str = "all", regex: bool = False,
                     chat: Optional[str] = None, from_me: Optional[bool] = None,
                     kinds: Optional[list[str]] = None, after: Optional[int] = None,
                     before: Optional[int] = None, include_lisa: bool = False,
                     limit: int = 30) -> SearchResult:
        t0 = time.monotonic()
        if self._pool is None:
            return _empty_search()
        terms = [t.strip() for t in (terms or []) if isinstance(t, str) and t.strip()]
        limit = _clamp(limit, 1, 200, 30)
        try:
            async with self._pool.connection() as conn:
                canon = await self._resolve_chat(conn, chat)
                p: dict = {"limit": limit, "nchats": BY_CHAT}
                where = self._filters(p, chat=canon, from_me=from_me, kinds=kinds, after=after,
                                      before=before, include_lisa=include_lisa)
                tc = self._term_conds(p, terms, regex)
                if tc:
                    where.append("(" + (" OR " if match == "any" else " AND ").join(tc) + ")")
                score = " + ".join(f"({c})::int" for c in tc) if (tc and match == "any") else "0"
                s = self.schema
                agg = await self._q(
                    f"""WITH h AS MATERIALIZED (
                          SELECT m.msg_id, m.chat, m.ts, m.src_id, {score} AS score
                          FROM {s}.messages m WHERE {" AND ".join(where)})
                        SELECT (SELECT count(*) FROM h) AS total,
                          (SELECT coalesce(json_agg(json_build_array(x.chat, x.n)
                                    ORDER BY x.n DESC, x.l DESC), '[]'::json)
                           FROM (SELECT chat, count(*) AS n, max(ts) AS l FROM h GROUP BY chat
                                 ORDER BY n DESC, l DESC LIMIT %(nchats)s) x) AS by_chat,
                          (SELECT coalesce(json_agg(y.msg_id ORDER BY y.score DESC, y.ts DESC,
                                    y.src_id DESC), '[]'::json)
                           FROM (SELECT msg_id, score, ts, src_id FROM h
                                 ORDER BY score DESC, ts DESC, src_id DESC LIMIT %(limit)s) y) AS top""",
                    p, conn)
                total = int(agg[0]["total"])
                top = agg[0]["top"] or []
                counts = agg[0]["by_chat"] or []
                rows = await self._q(f"SELECT {_HIT_COLS} {self._from()} WHERE m.msg_id = ANY(%s)",
                                     (top,), conn) if top else []
                await self._expand(conn, rows)
                chats = await self._q(
                    f"SELECT {_CHAT_COLS} FROM {s}.chats WHERE chat = ANY(%s)",
                    ([c for c, _ in counts],), conn) if counts else []
            order = {mid: i for i, mid in enumerate(top)}
            rows.sort(key=lambda r: order.get(r["msg_id"], 1 << 30))
            by_name = {c["chat"]: c for c in chats}
            result: SearchResult = {
                "total": total,
                "hits": [self._hit(r, needles=terms, regex=regex) for r in rows],
                "by_chat": [self._chat(by_name.get(c), c, n) for c, n in counts],
            }
            self._log("search", t0, n=len(terms), total=total)
            return result
        except Exception as exc:
            log.warning("history search failed: %s", exc)
            return _empty_search()

    # -- topics together ---------------------------------------------------------------------
    async def topics_together(self, *, topics: list[list[str]], within_days: int = 14,
                              after: Optional[int] = None, before: Optional[int] = None,
                              limit: int = 15) -> list[TopicChat]:
        t0 = time.monotonic()
        if self._pool is None:
            return []
        clean: list[list[str]] = []
        for t in topics or []:
            alts = [t] if isinstance(t, str) else list(t or [])
            alts = [a.strip() for a in alts if isinstance(a, str) and a.strip()]
            if alts:
                clean.append(alts)
        if not clean or len(clean) > 8:
            return []
        limit = _clamp(limit, 1, 50, 15)
        span = max(0, int(within_days or 0)) * 86400
        try:
            p: dict = {"cap": TOPIC_ROWS}
            where = self._filters(p, chat=None, from_me=None, kinds=None, after=after, before=before,
                                  include_lisa=False)
            tconds = []
            for i, alts in enumerate(clean):
                cs = self._term_conds(p, alts, False, prefix=f"t{i}_")
                tconds.append("(" + " OR ".join(cs) + ")")
            s = self.schema
            cols = ", ".join(f"{c} AS t{i}" for i, c in enumerate(tconds))
            have_all = " AND ".join(f"bool_or(t{i})" for i in range(len(tconds)))
            tcols = ", ".join(f"h.t{i}" for i in range(len(tconds)))
            async with self._pool.connection() as conn:
                rows = await self._q(
                    f"""WITH h AS MATERIALIZED (
                          SELECT m.msg_id, m.chat, m.ts, m.src_id, {cols}
                          FROM {s}.messages m
                          WHERE {" AND ".join(where)} AND ({" OR ".join(tconds)})),
                        ok AS (SELECT chat FROM h GROUP BY chat HAVING {have_all})
                        SELECT h.chat, h.ts, h.msg_id, {tcols}
                        FROM h JOIN ok USING (chat) ORDER BY h.chat, h.ts, h.src_id
                        LIMIT %(cap)s""", p, conn)
                found = self._windows(rows, len(clean), span)
                found.sort(key=lambda f: (f["w"], -f["n"], -f["last"]))
                found = found[:limit]
                ids = sorted({mid for f in found for mid in f["examples"]})
                chats = [f["chat"] for f in found]
                hit_rows = await self._q(f"SELECT {_HIT_COLS} {self._from()} WHERE m.msg_id = ANY(%s)",
                                         (ids,), conn) if ids else []
                await self._expand(conn, hit_rows)
                chat_rows = await self._q(f"SELECT {_CHAT_COLS} FROM {s}.chats WHERE chat = ANY(%s)",
                                          (chats,), conn) if chats else []
            hits = {r["msg_id"]: r for r in hit_rows}
            cmap = {c["chat"]: c for c in chat_rows}
            needles = [a for alts in clean for a in alts]
            out: list[TopicChat] = []
            for f in found:
                ex = [hits[mid] for mid in f["examples"] if mid in hits]
                ex.sort(key=lambda r: (r["ts"], r["src_id"]))
                out.append({"chat": self._chat(cmap.get(f["chat"]), f["chat"], f["n"]),
                            "first_ts": int(f["first"]), "last_ts": int(f["last"]),
                            "examples": [self._hit(r, needles=needles) for r in ex]})
            self._log("topics_together", t0, topics=len(clean), rows=len(rows), chats=len(out))
            return out
        except Exception as exc:
            log.warning("history topics_together failed: %s", exc)
            return []

    @staticmethod
    def _windows(rows: list[dict], k: int, span: int) -> list[dict]:
        """Per chat, the tightest stretch of time holding at least one hit of every topic (rows are
        sorted by chat, then time). Kept when it is no longer than `span` seconds."""
        out: list[dict] = []
        i = 0
        while i < len(rows):
            chat = rows[i]["chat"]
            j = i
            while j < len(rows) and rows[j]["chat"] == chat:
                j += 1
            ev = [(int(r["ts"]), r["msg_id"], [t for t in range(k) if r[f"t{t}"]]) for r in rows[i:j]]
            i = j
            counts, covered, left, best = [0] * k, 0, 0, None
            for right, (_, _, ts_topics) in enumerate(ev):
                for t in ts_topics:
                    counts[t] += 1
                    covered += counts[t] == 1
                while covered == k:
                    w = ev[right][0] - ev[left][0]
                    if best is None or w < best[0]:
                        best = (w, left, right)
                    for t in ev[left][2]:
                        counts[t] -= 1
                        covered -= counts[t] == 0
                    left += 1
            if best is None or best[0] > span:
                continue
            w, lo, hi = best
            window = ev[lo:hi + 1]
            # Examples: the hits that make up the window — the first and last of each topic in it.
            picked: list[str] = []
            for t in range(k):
                mids = [mid for _, mid, tt in window if t in tt]
                for mid in (mids[0], mids[-1]):
                    if mid not in picked:
                        picked.append(mid)
            out.append({"chat": chat, "w": w, "n": len(ev), "first": window[0][0],
                        "last": window[-1][0], "examples": picked})
        return out

    # -- chats -------------------------------------------------------------------------------
    async def list_chats(self, *, name: Optional[str] = None, kind: Optional[str] = None,
                         active_after: Optional[int] = None, active_before: Optional[int] = None,
                         limit: int = 20) -> list[ChatSummary]:
        t0 = time.monotonic()
        if self._pool is None:
            return []
        limit = _clamp(limit, 1, 200, 20)
        try:
            p: dict = {"limit": limit}
            w = ["n_messages > 0"]
            if kind in ("contact", "group", "self"):
                w.append("kind = %(kind)s")
                p["kind"] = kind
            if active_after is not None:
                w.append("last_ts >= %(aa)s")
                p["aa"] = int(active_after)
            if active_before is not None:
                w.append("first_ts < %(ab)s")
                p["ab"] = int(active_before)
            order = "last_ts DESC NULLS LAST"
            sel = _CHAT_COLS
            q = it.fold(name or "").strip()
            if q:
                words = [x for x in re.split(r"[\s,]+", q) if x]
                contains = " AND ".join(f"names_norm LIKE %(w{i})s" for i in range(len(words)))
                for i, x in enumerate(words):
                    p[f"w{i}"] = it.like_pattern(x)
                p["q"] = q
                alts = [f"({contains})"]
                if self.ext:
                    sim = f"{self.ext}.word_similarity(%(q)s, names_norm)"
                    alts.append(f"{sim} >= {NAME_SIMILARITY}")
                else:
                    sim = "0"
                digits = re.sub(r"\D", "", name or "")
                if len(digits) >= 4:
                    alts.append("phone LIKE %(dig)s")
                    p["dig"] = f"%{digits}%"
                w.append("(" + " OR ".join(alts) + ")")
                sel = f"{_CHAT_COLS}, ({contains}) AS exact, {sim} AS sim"
                order = (f"exact DESC, CASE WHEN ({contains}) THEN 0 ELSE {sim} END DESC, "
                         f"last_ts DESC NULLS LAST")
            rows = await self._q(
                f"SELECT {sel} FROM {self.schema}.chats WHERE {' AND '.join(w)} "
                f"ORDER BY {order} LIMIT %(limit)s", p)
            self._log("list_chats", t0, n=len(rows))
            return [self._chat(r) for r in rows]
        except Exception as exc:
            log.warning("history list_chats failed: %s", exc)
            return []

    async def read_chat(self, *, chat: str, around_msg_id: Optional[str] = None,
                        after: Optional[int] = None, before: Optional[int] = None,
                        limit: int = 40) -> ReadResult:
        t0 = time.monotonic()
        empty: ReadResult = {"chat": self._chat(None, chat), "messages": [], "more_before": False,
                             "more_after": False}
        if self._pool is None:
            return empty
        limit = _clamp(limit, 1, 200, 40)
        s = self.schema
        try:
            async with self._pool.connection() as conn:
                canon = await self._resolve_chat(conn, chat)
                anchor = None
                if around_msg_id:
                    a = await self._q(f"SELECT chat, ts, src_id FROM {s}.messages WHERE msg_id = %s",
                                      (around_msg_id,), conn)
                    if a:
                        anchor = a[0]
                        canon = anchor["chat"]  # the message's own chat, whatever was passed
                if not canon:
                    return empty
                base = f"SELECT {_HIT_COLS} {self._from()} WHERE m.chat = %(chat)s AND m.kind <> 'reaction'"
                p: dict = {"chat": canon}
                if anchor is not None:
                    half = limit // 2
                    p.update(ts=anchor["ts"], src=anchor["src_id"], nb=half, na=limit - half)
                    older = await self._q(
                        f"{base} AND (m.ts, m.src_id) < (%(ts)s, %(src)s) "
                        f"ORDER BY m.ts DESC, m.src_id DESC LIMIT %(nb)s", p, conn)
                    newer = await self._q(
                        f"{base} AND (m.ts, m.src_id) >= (%(ts)s, %(src)s) "
                        f"ORDER BY m.ts, m.src_id LIMIT %(na)s", p, conn)
                    rows = list(reversed(older)) + newer
                elif after is not None:
                    p.update(after=int(after), n=limit)
                    extra = ""
                    if before is not None:
                        p["before"] = int(before)
                        extra = " AND m.ts < %(before)s"
                    rows = await self._q(f"{base} AND m.ts >= %(after)s{extra} "
                                         f"ORDER BY m.ts, m.src_id LIMIT %(n)s", p, conn)
                else:
                    p["n"] = limit
                    extra = ""
                    if before is not None:
                        p["before"] = int(before)
                        extra = " AND m.ts < %(before)s"
                    rows = list(reversed(await self._q(
                        f"{base}{extra} ORDER BY m.ts DESC, m.src_id DESC LIMIT %(n)s", p, conn)))
                more_before = more_after = False
                if rows:
                    f, l = rows[0], rows[-1]
                    ex = await self._q(
                        f"""SELECT
                              EXISTS (SELECT 1 FROM {s}.messages m WHERE m.chat = %(chat)s
                                      AND m.kind <> 'reaction'
                                      AND (m.ts, m.src_id) < (%(fts)s, %(fsrc)s)) AS b,
                              EXISTS (SELECT 1 FROM {s}.messages m WHERE m.chat = %(chat)s
                                      AND m.kind <> 'reaction'
                                      AND (m.ts, m.src_id) > (%(lts)s, %(lsrc)s)) AS a""",
                        {"chat": canon, "fts": f["ts"], "fsrc": f["src_id"], "lts": l["ts"],
                         "lsrc": l["src_id"]}, conn)
                    more_before, more_after = bool(ex[0]["b"]), bool(ex[0]["a"])
                crow = await self._q(f"SELECT {_CHAT_COLS} FROM {s}.chats WHERE chat = %s",
                                     (canon,), conn)
                await self._expand(conn, rows)
            self._log("read_chat", t0, n=len(rows))
            return {"chat": self._chat(crow[0] if crow else None, canon),
                    "messages": [self._hit(r, cap=READ_CAP) for r in rows],
                    "more_before": more_before, "more_after": more_after}
        except Exception as exc:
            log.warning("history read_chat failed: %s", exc)
            return empty

    async def get_message(self, msg_id: str) -> Optional[Hit]:
        if self._pool is None or not msg_id:
            return None
        try:
            async with self._pool.connection() as conn:
                rows = await self._q(f"SELECT {_HIT_COLS} {self._from()} WHERE m.msg_id = %s",
                                     (msg_id,), conn)
                await self._expand(conn, rows)
            return self._hit(rows[0], cap=GET_CAP) if rows else None
        except Exception as exc:
            log.warning("history get_message failed: %s", exc)
            return None

    async def coverage(self) -> dict:
        out = {"first_ts": 0, "last_ts": 0, "n_messages": 0, "n_chats": 0, "voice_total": 0,
               "voice_transcribed": 0, "complete": False}
        if self._pool is None:
            return out
        s = self.schema
        try:
            async with self._pool.connection() as conn:
                r = (await self._q(
                    f"""SELECT min(ts) AS first_ts, max(ts) AS last_ts,
                          count(*) FILTER (WHERE kind <> 'reaction') AS n_messages,
                          count(*) FILTER (WHERE kind = 'voice') AS voice_total,
                          count(*) FILTER (WHERE kind = 'voice' AND transcript = 'done')
                            AS voice_transcribed
                        FROM {s}.messages""", None, conn))[0]
                n = (await self._q(f"SELECT count(*) AS n FROM {s}.chats WHERE n_messages > 0",
                                   None, conn))[0]["n"]
                st = await self._q(f"SELECT key, value FROM {s}.sync_state "
                                   f"WHERE key IN ('backfill_done')", None, conn)
            out.update({k: int(r[k] or 0) for k in ("first_ts", "last_ts", "n_messages",
                                                     "voice_total", "voice_transcribed")})
            out["n_chats"] = int(n or 0)
            out["complete"] = any(x["key"] == "backfill_done" and x["value"] for x in st)
            return out
        except Exception as exc:
            log.warning("history coverage failed: %s", exc)
            return out
