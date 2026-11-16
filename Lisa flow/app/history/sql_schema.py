"""The lisa_search schema: the index of every stored WhatsApp message, and what it needs to stay
in step with Evolution. Created by the indexer (the only writer); read by the store.

  messages    one row per WhatsApp message (key.id), under its CANONICAL chat; `norm` is the folded
              words + file name (index_text.fold) and carries the only trigram index
  chats       one row per conversation: canonical key, the other jids it was seen under, best name
              and every name it goes by (folded, for fuzzy lookups), counts and first/last time
  jids        every raw jid seen — a chat, a sender, a "Chat"/"Contact" row — with the canonical
              key it belongs to and the names each source gives it; `name` is the best one for
              its canonical, so a sender resolves with one join
  sync_state  the indexer's cursors and flags (key/value)

Sized for a small box (1 CPU, ~1 GB RAM, shared with the live gateway): ONE GIN trigram index, on
`messages.norm` only. The chats table holds a few thousand rows and is matched by a scan, so it gets
none. Two btrees on messages: (chat, ts) for reading a chat in order and for per-chat counts, and
(ts) for time filters and coverage. The GIN index keeps a small pending list so that each indexer
batch pays its own share of the maintenance instead of a large flush every few minutes.

pg_trgm is the one extension: installed INTO this schema when the database has none, so nothing
lands in public. When pg_trgm already lives elsewhere, that copy is used — `ext` is its schema.
Folding is done in Python, so unaccent is not needed."""
from __future__ import annotations

import re

_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def ident(name: str) -> str:
    """A schema name safe to splice into SQL (it comes from settings, but splice nothing unchecked)."""
    if not _IDENT.fullmatch(name or ""):
        raise ValueError(f"not a plain schema name: {name!r}")
    return name


def ddl(s: str, ext: str) -> str:
    s, ext = ident(s), ident(ext)
    return f"""
    CREATE TABLE IF NOT EXISTS {s}.messages (
        msg_id        text PRIMARY KEY,
        src_id        text NOT NULL,
        chat          text NOT NULL,
        jid           text NOT NULL,
        ts            bigint NOT NULL,
        from_me       boolean NOT NULL,
        by_lisa       boolean NOT NULL DEFAULT false,
        to_lisa       boolean NOT NULL DEFAULT false,
        sender_jid    text,
        push_name     text,
        kind          text NOT NULL,
        text          text NOT NULL DEFAULT '',
        file_name     text,
        mimetype      text,
        seconds       integer,
        quoted_msg_id text,
        transcript    text,
        edited        boolean NOT NULL DEFAULT false,
        norm          text NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS messages_chat_ts ON {s}.messages (chat, ts);
    CREATE INDEX IF NOT EXISTS messages_ts ON {s}.messages (ts);
    CREATE INDEX IF NOT EXISTS messages_norm_trgm ON {s}.messages
        USING gin (norm {ext}.gin_trgm_ops) WITH (fastupdate = on, gin_pending_list_limit = 1024);

    CREATE TABLE IF NOT EXISTS {s}.chats (
        chat        text PRIMARY KEY,
        aliases     text[] NOT NULL DEFAULT '{{}}',
        kind        text NOT NULL,
        phone       text,
        name        text,
        names_norm  text NOT NULL DEFAULT '',
        n_messages  integer NOT NULL DEFAULT 0,
        first_ts    bigint,
        last_ts     bigint
    );

    CREATE TABLE IF NOT EXISTS {s}.jids (
        jid           text PRIMARY KEY,
        canonical     text NOT NULL,
        chat_name     text,
        contact_name  text,
        push_name     text,
        push_ts       bigint,
        name          text
    );

    CREATE TABLE IF NOT EXISTS {s}.sync_state (
        key         text PRIMARY KEY,
        value       text,
        updated_at  timestamptz NOT NULL DEFAULT now()
    );
    """
