"""End-to-end verification for the history index (indexer.py) and its queries (store.py).

Runs the REAL indexer and store against a throwaway Postgres 15, seeded with tables shaped like
Evolution's "Message" / "Chat" / "Contact" / "Instance" and Lisa's <log_schema>.transcripts. Every
id, number, name and line of text here is made up.

    cd "Lisa flow" && python tests/run_history_store.py
    HISTORY_TEST_DATABASE_URL=postgresql://... python tests/run_history_store.py   # your own server
    HISTORY_PERF=1 python tests/run_history_store.py                               # + 160k-row timing

With no URL it starts `postgres:15` in Docker and removes it afterwards; with neither Docker nor a
URL it prints SKIPPED and exits 0. Exits non-zero if any check fails.

Covers: text extraction for every message shape (wrappers, captions, files, polls, edits, quotes),
the @lid <-> phone merge and a late re-key, names from every source and the address book, Lisa's own
messages and the owner's requests to her, voice-note transcripts arriving before and after, voice
attempts, accent/case-insensitive search, regex, match all/any, every filter, by_chat,
topics_together, fuzzy list_chats, read_chat windows, get_message, coverage, a late backlog with old
timestamps, a crash mid-backfill and its resume, incremental-sync idempotency, and that nothing
raises on a missing table or a bad regex."""
from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.history import index_text as it  # noqa: E402

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    tail = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{tail}")
    _checks["pass" if cond else "fail"] += 1


# ============================ made-up people ==============================================
OWNER = "5511999990000@s.whatsapp.net"
OWNER_LID = "900000000000001@lid"
ANA = "5511888880000@s.whatsapp.net"
ANA_LID = "111111111111111@lid"
BRUNO_LID = "222222222222222@lid"
BRUNO = "5511777770000@s.whatsapp.net"
CARLA_LID = "333333333333333@lid"
DANI_LID = "444444444444444@lid"
DANI = "5521666660000@s.whatsapp.net"
EDU = "5511555550000@s.whatsapp.net"
GROUP = "120363000000000000@g.us"
GROUP2 = "120363000000000001@g.us"
GROUP3 = "120363000000000002@g.us"
OLD_GROUP = "5511999990000-1600000000@g.us"
INSTANCE = "inst-0001"
DAY = 86400
T0 = 1_790_000_000  # a made-up Tuesday


# ============================ part 1: pure extraction =====================================
def p1_extract() -> None:
    print("P1 — what a message says")
    X = it.extract
    r = X("conversation", {"conversation": "Olá mundo", "messageContextInfo": {"x": 1}})
    check("plain conversation", r and r["kind"] == "text" and r["text"] == "Olá mundo")
    r = X("ephemeralMessage", {"ephemeralMessage": {"message": {"extendedTextMessage": {
        "text": "sumindo", "contextInfo": {"stanzaId": "Q1", "quotedMessage": {"conversation": "SEGREDO"}}}}}})
    check("disappearing chat: extendedTextMessage under ephemeralMessage",
          r and r["text"] == "sumindo" and r["quoted_msg_id"] == "Q1")
    check("a quoted message's text is never indexed as the reply's", "SEGREDO" not in (r or {}).get("text", ""))
    r = X("imageMessage", {"imageMessage": {"caption": "foto do evento", "mimetype": "image/jpeg",
                                            "jpegThumbnail": "AAAA"}})
    check("image caption + mimetype", r and r["kind"] == "image" and r["text"] == "foto do evento"
          and r["mimetype"] == "image/jpeg")
    r = X("imageMessage", {"imageMessage": {"mimetype": "image/jpeg"}})
    check("an image with no caption is still kept", r and r["kind"] == "image" and r["text"] == "")
    r = X("videoMessage", {"videoMessage": {"caption": "pitch", "seconds": 42, "mimetype": "video/mp4"}})
    check("video caption + seconds", r and r["kind"] == "video" and r["seconds"] == 42)
    r = X("documentMessage", {"documentMessage": {"fileName": "Proposta Turuk.pdf", "title": "Proposta Turuk",
                                                  "caption": "segue", "mimetype": "application/pdf"}})
    check("document: file name, caption, title folded into the file name",
          r and r["kind"] == "document" and r["file_name"] == "Proposta Turuk.pdf" and r["text"] == "segue")
    r = X("documentWithCaptionMessage", {"documentWithCaptionMessage": {"message": {"documentMessage": {
        "fileName": "deck.pdf", "caption": "o deck"}}}})
    check("documentWithCaptionMessage wrapper", r and r["file_name"] == "deck.pdf" and r["text"] == "o deck")
    r = X("viewOnceMessageV2", {"viewOnceMessageV2": {"message": {"imageMessage": {"caption": "uma vez"}}}})
    check("view-once wrapper", r and r["kind"] == "image" and r["text"] == "uma vez")
    r = X("associatedChildMessage", {"associatedChildMessage": {"message": {"videoMessage": {"caption": "album"}}}})
    check("album child (associatedChildMessage)", r and r["kind"] == "video" and r["text"] == "album")
    r = X("lottieStickerMessage", {"lottieStickerMessage": {"message": {"stickerMessage": {"mimetype": "x"}}}})
    check("lottie sticker", r and r["kind"] == "sticker")
    r = X("audioMessage", {"audioMessage": {"seconds": 17, "ptt": True, "mimetype": "audio/ogg"}})
    check("voice note: kind voice, no words yet, seconds", r and r["kind"] == "voice" and r["text"] == ""
          and r["seconds"] == 17)
    r = X("contactMessage", {"contactMessage": {"displayName": "Fulano Tal", "vcard": "TEL:+55 11 0000"}})
    check("contact card: the name, not the vCard", r and r["kind"] == "contact" and r["text"] == "Fulano Tal")
    r = X("contactsArrayMessage", {"contactsArrayMessage": {"displayName": "2 contatos", "contacts": [
        {"displayName": "Um"}, {"displayName": "Dois"}]}})
    check("several contact cards", r and "Dois" in r["text"])
    r = X("locationMessage", {"locationMessage": {"name": "Cubo Itaú", "address": "Rua Exemplo 1",
                                                  "degreesLatitude": 1}})
    check("location name + address", r and r["kind"] == "location" and "Rua Exemplo 1" in r["text"])
    r = X("pollCreationMessageV3", {"pollCreationMessageV3": {"name": "Qual dia?", "options": [
        {"optionName": "Terça"}, {"optionName": "Quinta"}]}})
    check("poll question + options", r and r["kind"] == "poll" and "Quinta" in r["text"])
    r = X("reactionMessage", {"reactionMessage": {"text": "👍", "key": {"id": "TARGET1"}}})
    check("reaction: the emoji, pointing at its message", r and r["kind"] == "reaction"
          and r["quoted_msg_id"] == "TARGET1")
    check("a removed reaction is skipped", X("reactionMessage", {"reactionMessage": {"text": "", "key": {"id": "T"}}}) is None)
    r = X("templateMessage", {"templateMessage": {"hydratedTemplate": {"hydratedTitleText": "Pedido",
                                                                       "hydratedContentText": "confirmado"}}})
    check("template text", r and "confirmado" in r["text"])
    r = X("buttonsMessage", {"buttonsMessage": {"contentText": "Escolha", "buttons": []}})
    check("buttons text", r and r["text"] == "Escolha")
    r = X("listResponseMessage", {"listResponseMessage": {"title": "Opção B"}})
    check("list response", r and r["text"] == "Opção B")
    r = X("interactiveMessage", {"interactiveMessage": {"body": {"text": "corpo"}}})
    check("interactive body", r and r["text"] == "corpo")
    r = X("protocolMessage", {"protocolMessage": {"type": 14, "key": {"id": "ORIG"},
                                                  "editedMessage": {"conversation": "novo texto"}}})
    check("an edit names its target and carries the new words",
          r and r["edit_of"] == "ORIG" and r["text"] == "novo texto")
    r = X("protocolMessage", {"protocolMessage": {"type": 14, "key": {"id": "ORIG"}, "editedMessage": {
        "extendedTextMessage": {"text": "editado"}}}})
    check("an edit of an extended text", r and r["text"] == "editado")
    for t, m in (("secretEncryptedMessage", {"secretEncryptedMessage": {"encPayload": "x"}}),
                 ("pollUpdateMessage", {"pollUpdateMessage": {"vote": {}}}),
                 ("albumMessage", {"albumMessage": {"expectedImageCount": 3}}),
                 ("protocolMessage", {"protocolMessage": {"type": 0, "key": {"id": "X"}}}),
                 ("unknown", {"senderKeyDistributionMessage": {"x": 1}, "messageContextInfo": {}}),
                 ("unknown", {"call": {"callKey": "x"}}),
                 ("conversation", None)):
        check(f"skipped: {t} {list((m or {}).keys())[:1]}", X(t, m) is None)
    r = X("futureMessage", {"futureMessage": {"body": {"text": "algo novo"}, "contextInfo": {
        "quotedMessage": {"conversation": "nope"}}}})
    check("an unknown future type falls back to the text walk, skipping quotes",
          r and r["kind"] == "other" and r["text"] == "algo novo")
    r = X("conversation", {"conversation": "x"}, context_info={"stanzaId": "QQ"})
    check("the quoted id from the row's own contextInfo column", r["quoted_msg_id"] == "QQ")
    for hdr in ("*[Marcelo's AI Assistant]:*\n\nCorpo", "[Assistente IA do Marcelo]: Corpo",
                "*[Assistente IA do Marcelo]:*\n\nCorpo", "[Marcelo's AI Assistant]:\nCorpo",
                "_[Mary]:_ Corpo"):
        r = X("conversation", {"conversation": hdr}, from_me=True, owner_name="Marcelo")
        check(f"Lisa's header recognised and cut: {hdr[:14]!r}", r["by_lisa"] and r["text"] == "Corpo")
    r = X("conversation", {"conversation": "[Marcelo's AI Assistant]: x"}, from_me=False, owner_name="Marcelo")
    check("someone else quoting the header is not Lisa", not r["by_lisa"])
    r = X("conversation", {"conversation": "@lisa quem falou de Turuk?"}, from_me=True,
          owner_name="Marcelo", tags=["@lisa"])
    check("the owner's @lisa request is flagged to_lisa", r["to_lisa"] and not r["by_lisa"])
    r = X("conversation", {"conversation": "@lisandro oi"}, from_me=True, owner_name="Marcelo", tags=["@lisa"])
    check("…but not a word that merely starts with the tag", not r["to_lisa"])
    r = X("conversation", {"conversation": "@Brain agenda amanhã"}, from_me=True, owner_name="Marcelo",
          tags=["@lisa"])
    check("…and an older tag (@brain) counts too", r["to_lisa"])
    r = X("documentMessage", {"documentMessage": {"fileName": "Contrato.pdf", "caption": "Contrato.pdf"}})
    check("a caption that repeats the file name is not repeated", r["text"] == "" and r["file_name"] == "Contrato.pdf")
    check("@mentions expand to known names, unknown ones stay",
          it.expand_mentions("oi @111111111111111 e @222222222222222", {ANA_LID: "Ana"})
          == "oi @Ana e @222222222222222")

    print("P1b — folding, snippets, ids, names")
    check("fold: accents, case, whitespace", it.fold("São  PAULO\nÉ") == "sao paulo e")
    check("fold of a ligature", it.fold("ﬁm") == "fim")
    check("LIKE wildcards escaped", it.like_pattern("50%_x") == "%50\\%\\_x%")
    long = ("palavra " * 200) + "Reunião em São Paulo com a aceleradora" + (" fim" * 200)
    sn = it.snippet(long, ["sao paulo"])
    check("snippet ~300 chars around the match, accents intact",
          "São Paulo" in sn and len(sn) <= 304 and sn.startswith("…") and sn.endswith("…"), f"{len(sn)}")
    check("short texts are returned whole", it.snippet("curto", ["x"]) == "curto")
    sn = it.snippet(long, [r"aceler\w+"], regex=True)
    check("regex snippet", "aceleradora" in sn)
    ms = 1_794_843_762_644
    check("cuid timestamp read back", it.cuid_ms("cmwjfmuok0bwimk4j9tgj5nt9") == ms)
    check("cuid floor sorts at or before the id", it.cuid_floor(ms) <= "cmwjfmuok0bwimk4j9tgj5nt9"
          and it.cuid_floor(ms + 1) > "cmwjfmuok0bwimk4j9tgj5nt9")
    check("a non-cuid id has no time", it.cuid_ms("3EB0ABCDEF") is None)
    check("a bare number is not a name", it.clean_name("+55 11 98888-0000") is None)
    check("the jid's own digits are not a name", it.clean_name("5511888880000", ANA) is None)
    check("a real name is kept", it.clean_name("  Ana Paula ") == "Ana Paula")
    check("'Você' is not a name", it.clean_name("Você") is None)
    check("an old-style group id is a group", it.chat_kind_of("5511999990000-1600000000@g.us", OWNER) == "group")
    check("device suffix dropped", it.bare_jid("5511888880000:12@s.whatsapp.net") == ANA)


# ============================ Postgres plumbing ===========================================
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PG:
    """A database to test in: the caller's server (a fresh database on it), or a throwaway
    postgres:15 container."""

    def __init__(self) -> None:
        self.container = None
        self.admin = None
        self.dsn = None
        self.dbname = None

    async def __aenter__(self):
        import psycopg

        url = os.environ.get("HISTORY_TEST_DATABASE_URL") or os.environ.get("MARY_TEST_DATABASE_URL")
        if not url:
            if not shutil.which("docker") or subprocess.run(
                    ["docker", "info"], capture_output=True).returncode != 0:
                return None
            port = _free_port()
            self.container = f"lisa-history-test-{uuid.uuid4().hex[:8]}"
            r = subprocess.run(["docker", "run", "-d", "--rm", "--name", self.container,
                                "-e", "POSTGRES_PASSWORD=pw", "-p", f"127.0.0.1:{port}:5432",
                                "postgres:15"], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  (docker run failed: {r.stderr.strip()[:200]})")
                self.container = None
                return None
            url = f"postgresql://postgres:pw@127.0.0.1:{port}/postgres"
            for _ in range(60):
                try:
                    async with await psycopg.AsyncConnection.connect(url, connect_timeout=2) as c:
                        await c.execute("SELECT 1")
                    break
                except Exception:
                    await asyncio.sleep(1)
        self.admin = url
        self.dbname = f"hs_test_{uuid.uuid4().hex[:8]}"
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
            await c.execute(f"CREATE DATABASE {self.dbname}")
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        d = conninfo_to_dict(url)
        d["dbname"] = self.dbname
        self.dsn = make_conninfo(**d)
        return self

    async def __aexit__(self, *exc):
        import psycopg

        try:
            if self.admin and self.dbname:
                async with await psycopg.AsyncConnection.connect(self.admin, autocommit=True) as c:
                    await c.execute(f"DROP DATABASE IF EXISTS {self.dbname} WITH (FORCE)")
        except Exception:
            pass
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)


EVOLUTION_DDL = """
CREATE TABLE "Instance" (id text PRIMARY KEY, name varchar, "ownerJid" varchar);
CREATE TABLE "Message" (
  id text PRIMARY KEY, key jsonb, "pushName" varchar, participant varchar, "messageType" varchar,
  message jsonb, "contextInfo" jsonb, "messageTimestamp" integer, "instanceId" text, status varchar);
CREATE INDEX "Message_instanceId_idx" ON "Message" ("instanceId");
CREATE TABLE "Chat" (id text PRIMARY KEY, "remoteJid" varchar, name varchar, "instanceId" text,
  "updatedAt" timestamp DEFAULT now());
CREATE TABLE "Contact" (id text PRIMARY KEY, "remoteJid" varchar, "pushName" varchar, "instanceId" text,
  "updatedAt" timestamp DEFAULT now());
CREATE SCHEMA lisa_log;
CREATE TABLE lisa_log.transcripts (wa_id text PRIMARY KEY, text text NOT NULL, duration_sec real,
  language text, created_at timestamptz NOT NULL DEFAULT now());
"""


class Evo:
    """Writes rows the way Evolution stores them: cuid v1 ids minted from a clock that only moves
    forward (the time the row was STORED), whatever the message's own timestamp says."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.clock_ms = (T0 - 400 * DAY) * 1000
        self.n = 0

    def cuid(self, stored_ms: int | None = None) -> str:
        self.clock_ms = max(self.clock_ms + 1, stored_ms or 0)
        self.n += 1
        noise = "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(8))
        c = it.cuid_floor(self.clock_ms) + it.cuid_floor(self.n)[-4:] + "mk4j" + noise
        assert len(c) == 25 and it.cuid_ms(c) == self.clock_ms
        return c

    async def msg(self, jid: str, text: str | None = None, *, mid: str, ts: int, from_me: bool = False,
                  alt: str | None = None, part: str | None = None, part_alt: str | None = None,
                  push: str | None = None, mtype: str = "conversation", message=None, ctx=None,
                  stored_ms: int | None = None, instance: str = INSTANCE) -> str:
        key = {"id": mid, "remoteJid": jid, "fromMe": from_me}
        if alt:
            key["remoteJidAlt"] = alt
        if part:
            key["participant"] = part
            key["addressingMode"] = "lid"
        if part_alt:
            key["participantAlt"] = part_alt
        if message is None and text is not None:
            message = {"conversation": text, "messageContextInfo": {"deviceListMetadata": {}}}
        rid = self.cuid(stored_ms if stored_ms is not None else ts * 1000)
        await self.conn.execute(
            'INSERT INTO "Message" (id, key, "pushName", "messageType", message, "contextInfo", '
            '"messageTimestamp", "instanceId", status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (rid, json.dumps(key), push, mtype, json.dumps(message) if message is not None else None,
             json.dumps(ctx) if ctx else None, ts, instance, "READ"))
        return rid


async def seed(evo: Evo) -> None:
    c = evo.conn
    await c.execute('INSERT INTO "Instance" VALUES (%s, %s, %s)', (INSTANCE, "secretaria", OWNER))
    await c.execute('INSERT INTO "Instance" VALUES (%s, %s, %s)', ("inst-other", "other", "5500000000000@s.whatsapp.net"))
    for jid, name in ((CARLA_LID, "Carla Dias"), (ANA_LID, None), (GROUP, "Founders Club"),
                      (GROUP2, "Viagem EUA"), (GROUP3, "São Paulo Tech")):
        await c.execute('INSERT INTO "Chat" (id, "remoteJid", name, "instanceId") VALUES (%s,%s,%s,%s)',
                        (uuid.uuid4().hex, jid, name, INSTANCE))
    for jid, name in ((ANA, "Ana Paula"), (EDU, "5511555550000"), (GROUP, "Dani"), (ANA_LID, "")):
        await c.execute('INSERT INTO "Contact" (id, "remoteJid", "pushName", "instanceId") VALUES (%s,%s,%s,%s)',
                        (uuid.uuid4().hex, jid, name, INSTANCE))
    m = evo.msg
    # Ana: stored under her @lid with the phone twin, plus Lisa's API send under the phone jid.
    await m(ANA_LID, "Oi! Vamos falar do demo day nos Estados Unidos?", mid="A1", ts=T0, alt=ANA, push="Aninha")
    await m(ANA_LID, "Claro, a aceleradora em São Paulo indicou", mid="A2", ts=T0 + 60, alt=ANA,
            from_me=True)
    await m(ANA_LID, "reunião às 10", mid="A3", ts=T0 + 120, alt=ANA, push="Aninha")
    await m(ANA_LID, "A Turuk vai junto pro EUA", mid="A4", ts=T0 + 2 * DAY, alt=ANA, push="Aninha")
    await m(ANA, "*[Marcelo's AI Assistant]:*\n\nCombinado, Turuk anotado na agenda", mid="A5",
            ts=T0 + 2 * DAY + 30, from_me=True)
    await m(ANA_LID, None, mid="A6", ts=T0 + 2 * DAY + 60, alt=ANA, push="Aninha", mtype="documentMessage",
            message={"documentMessage": {"fileName": "Proposta Turuk.pdf", "mimetype": "application/pdf",
                                         "jpegThumbnail": "A" * 4000}})
    await m(ANA_LID, None, mid="A7", ts=T0 + 2 * DAY + 90, alt=ANA, push="Aninha", mtype="reactionMessage",
            message={"reactionMessage": {"text": "❤️", "key": {"id": "A4"}}})
    await m(ANA_LID, None, mid="A8", ts=T0 + 2 * DAY + 120, alt=ANA, mtype="protocolMessage",
            message={"protocolMessage": {"type": 14, "key": {"id": "A3"},
                                         "editedMessage": {"conversation": "reunião às 11"}}})
    await m(ANA_LID, "sem alt aqui", mid="A9", ts=T0 + 3 * DAY, push="Aninha")
    long = ("bla " * 600) + "o investidor de Turuk ligou" + (" fim" * 300)
    await m(ANA_LID, long, mid="A10", ts=T0 + 3 * DAY + 10, alt=ANA, push="Aninha")
    await m(ANA_LID, "resposta", mid="A11", ts=T0 + 3 * DAY + 20, alt=ANA, push="Aninha",
            ctx={"stanzaId": "A10", "quotedMessage": {"conversation": "o investidor"}})
    # Voice notes: one transcribed before it is indexed, one after, one expired, one pending.
    for i, sec in ((1, 12), (2, 30), (3, 9), (4, 5), (5, 4000)):
        await m(ANA_LID, None, mid=f"VOICE{i}", ts=T0 + 4 * DAY + i, alt=ANA, push="Aninha",
                mtype="audioMessage", message={"audioMessage": {"seconds": sec, "ptt": True,
                                                                 "mimetype": "audio/ogg; codecs=opus"}})
    await c.execute("INSERT INTO lisa_log.transcripts (wa_id, text, duration_sec) VALUES (%s, %s, %s)",
                    ("VOICE1", "Então, sobre o fundo de Lisboa, falei com o pessoal", 12))
    # The same message stored twice: an empty placeholder, then the real one with a fresh time.
    await m(ANA_LID, "mensagem duplicada", mid="DUP1", ts=T0 + 5 * DAY, alt=ANA)
    await m(ANA_LID, "mensagem duplicada", mid="DUP1", ts=T0 + 5 * DAY + 999, alt=ANA)
    await m(ANA_LID, None, mid="DUP2", ts=T0 + 5 * DAY + 5, alt=ANA, mtype="conversation", message={})
    await m(ANA_LID, "agora com conteúdo", mid="DUP2", ts=T0 + 5 * DAY + 5, alt=ANA)

    # Bruno: only a @lid for now; his phone is learned later (re-key).
    for i in range(3):
        await m(BRUNO_LID, f"Bruno aqui, mensagem {i} sobre o hackathon", mid=f"B{i}", ts=T0 + i * 100,
                push="Bruno B.")
    # Carla: a @lid forever; both topics in one message.
    await m(CARLA_LID, "No demo day a Turuk apresentou", mid="C1", ts=T0 + 10 * DAY)
    await m(CARLA_LID, "beleza", mid="C2", ts=T0 + 10 * DAY + 5, from_me=True)
    # A group: Ana and Daniel write, and the owner.
    await m(GROUP, "Pessoal, o Demo Day é em Austin", mid="G1", ts=T0 + DAY, part=ANA_LID, part_alt=ANA,
            push="Aninha")
    await m(GROUP, "Eu vou! Levo a Turuk", mid="G2", ts=T0 + DAY + 50, part=DANI_LID, part_alt=DANI, push="Dani")
    await m(GROUP, "Fechado", mid="G3", ts=T0 + DAY + 80, part=OWNER_LID, from_me=True)
    await m(GROUP, "@lisa marca com todos", mid="G4", ts=T0 + DAY + 90, part=OWNER_LID, from_me=True,
            push="Você")
    await m(GROUP, "@444444444444444 você leva o pitch?", mid="G6", ts=T0 + DAY + 95, part=ANA_LID,
            part_alt=ANA, push="Aninha")
    await m(OLD_GROUP, "grupo antigo sobre Turuk? não", mid="OG1", ts=T0 + 8 * DAY, part=DANI, push="Dani")
    # A second group: the topics far apart (39 days).
    await m(GROUP2, "demo day foi ótimo", mid="H1", ts=T0, part=DANI_LID, part_alt=DANI, push="Dani")
    await m(GROUP2, "lembra da turuk?", mid="H2", ts=T0 + 39 * DAY, part=DANI_LID, part_alt=DANI, push="Dani")
    await m(GROUP3, "evento sexta", mid="S1", ts=T0 + 20 * DAY, part=DANI_LID, part_alt=DANI, push="Dani")
    # The owner's chat with himself: his note, his question to Lisa, her answer (via the API).
    await m(OWNER_LID, "nota: Turuk demo day", mid="O1", ts=T0 + 30 * DAY, alt=OWNER, from_me=True)
    await m(OWNER_LID, "@lisa quem falou de Turuk?", mid="O2", ts=T0 + 30 * DAY + 10, alt=OWNER,
            from_me=True)
    await m(OWNER, "*[Marcelo's AI Assistant]:*\n\nFoi a Ana, sobre Turuk", mid="O3",
            ts=T0 + 30 * DAY + 20, from_me=True, push="Você")
    await m(OWNER_LID, "lembrete encaminhado", mid="O4", ts=T0 + 30 * DAY + 30, alt=OWNER, from_me=False,
            push="Você")
    # Skipped shapes, a status broadcast and another instance's row.
    await m(ANA_LID, None, mid="X1", ts=T0 + 6 * DAY, alt=ANA, mtype="secretEncryptedMessage",
            message={"secretEncryptedMessage": {"encPayload": "zz"}})
    await m(ANA_LID, None, mid="X2", ts=T0 + 6 * DAY, alt=ANA, mtype="albumMessage",
            message={"albumMessage": {"expectedImageCount": 2}})
    await m("status@broadcast", "meu status Turuk", mid="X3", ts=T0 + 6 * DAY, part=ANA_LID)
    await m(ANA_LID, "outra instância Turuk", mid="X4", ts=T0 + 6 * DAY, alt=ANA, instance="inst-other")
    # Edu: a 1:1 by phone only, whose Contact name is just his number.
    await m(EDU, "Edu: proposta de viagem", mid="E1", ts=T0 + 7 * DAY, push="Eduardo")


class FakeDirectory:
    def __init__(self) -> None:
        self.ready = False
        self.cards = {"5521666660000": [{"resource_name": "people/1", "name": "Daniel Souza"}]}

    def by_phone(self, phone: str) -> list:
        return self.cards.get(phone, [])


def settings(**kw):
    base = dict(owner_name="Marcelo", owner_jid="", log_schema="lisa_log", evolution_instance="secretaria",
                tags=["@lisa"], history_index_interval_seconds=60)
    base.update(kw)
    return SimpleNamespace(**base)


async def snapshot(conn) -> tuple:
    out = []
    for t, k in (("messages", "msg_id"), ("chats", "chat"), ("jids", "jid")):
        cur = await conn.execute(
            f"SELECT md5(string_agg(x::text, '|' ORDER BY x.{k})), max(xmin::text::bigint) "
            f"FROM lisa_search.{t} x")
        out.append(await cur.fetchone())
    return tuple(out)


# ============================ part 2: the index ===========================================
async def p2_index(pg: PG) -> None:
    import psycopg

    from app.history.indexer import HistoryIndexer
    from app.history.store import PgHistoryStore

    print("P2 — the indexer: backfill, identity, names, voice")
    conn = await psycopg.AsyncConnection.connect(pg.dsn, autocommit=True)
    await conn.execute(EVOLUTION_DDL)
    evo = Evo(conn)
    await seed(evo)

    # A crash mid-backfill: the first indexer dies on its third page; a fresh one resumes.
    directory = FakeDirectory()
    crashy = HistoryIndexer(pg.dsn, settings=settings(), directory=directory, batch_size=5, pause_seconds=0)
    await crashy.open()
    real_write, calls = crashy._write_page, {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash")
        return await real_write(*a, **kw)

    crashy._write_page = flaky
    st = await crashy.run_once()
    check("a failing page is logged, not raised", "ingest" in st.get("errors", []), str(st.get("errors")))
    cur = await conn.execute("SELECT value FROM lisa_search.sync_state WHERE key = 'msg_cursor'")
    mid_cursor = (await cur.fetchone())[0]
    check("the cursor kept the pages written before the crash", it.cuid_ms(mid_cursor) is not None)
    await crashy.aclose()

    idx = HistoryIndexer(pg.dsn, settings=settings(), directory=directory, batch_size=5, pause_seconds=0)
    await idx.open()
    st = await idx.run_once()
    check("the resumed backfill completes", not st.get("errors") and st["pages"] >= 5, json.dumps(st))
    cur = await conn.execute("SELECT key, value FROM lisa_search.sync_state")
    state = dict(await cur.fetchall())
    check("backfill marked done", bool(state.get("backfill_done")))
    check("the owner's jid came from Evolution's Instance row", state.get("owner_jid") == OWNER)

    async def one(sql, *a):
        cur = await conn.execute(sql, a or None)
        return await cur.fetchone()

    async def all_(sql, *a):
        cur = await conn.execute(sql, a or None)
        return await cur.fetchall()

    n = (await one("SELECT count(*) FROM lisa_search.messages"))[0]
    ids = {r[0] for r in await all_("SELECT msg_id FROM lisa_search.messages")}
    check("skipped shapes, statuses and other instances stay out",
          not ids & {"X1", "X2", "X3", "X4", "A8"}, str(sorted(ids & {"X1", "X2", "X3", "X4", "A8"})))
    check("every message worth indexing is in", {"A1", "A6", "A7", "VOICE1", "C1", "G1", "O3", "E1"} <= ids,
          f"{n} rows")
    chats = {r[0]: r for r in await all_("SELECT chat, kind, name, aliases, n_messages, phone FROM lisa_search.chats")}
    check("Ana's @lid and phone jid are ONE chat, under the phone jid",
          ANA in chats and ANA_LID not in chats and ANA_LID in chats[ANA][3], str(chats.get(ANA)))
    a_chats = {r[0] for r in await all_("SELECT DISTINCT chat FROM lisa_search.messages WHERE msg_id LIKE 'A%'")}
    check("all of Ana's messages (incl. Lisa's API send and the one without alt) are under it",
          a_chats == {ANA}, str(a_chats))
    check("the owner's self-chat is kind self, under his phone jid",
          chats.get(OWNER, (0, ""))[1] == "self" and OWNER_LID not in chats)
    check("a @lid nobody mapped stays its own chat", CARLA_LID in chats and chats[CARLA_LID][1] == "contact")
    check("groups are groups", chats[GROUP][1] == "group")
    check("names: Ana from Evolution's Contact row", chats[ANA][2] == "Ana Paula", str(chats[ANA][2]))
    check("names: Carla from Evolution's Chat row on her @lid", chats[CARLA_LID][2] == "Carla Dias")
    check("names: Bruno from the name he sends with messages", chats[BRUNO_LID][2] == "Bruno B.")
    check("names: a number in Contact is not a name; the pushName wins", chats[EDU][2] == "Eduardo",
          str(chats[EDU][2]))
    check("names: the group subject, not Contact's last sender", chats[GROUP][2] == "Founders Club",
          str(chats[GROUP][2]))
    check("an old-style group id is a group chat", chats.get(OLD_GROUP, (0, ""))[1] == "group")
    r = await one("SELECT chat, sender_jid FROM lisa_search.messages WHERE msg_id = 'O4'")
    check("a fromMe=false row in the self-chat stays in the self-chat", r[0] == OWNER, str(r))
    check("names: the owner's own chat carries his name", chats[OWNER][2] == "Marcelo")
    check("phone digits on a merged chat", chats[ANA][5] == "5511888880000")
    check("counts leave reactions out", chats[ANA][4] == (await one(
        "SELECT count(*) FROM lisa_search.messages WHERE chat = %s AND kind <> 'reaction'", ANA))[0])
    r = await one("SELECT by_lisa, text FROM lisa_search.messages WHERE msg_id = 'A5'")
    check("Lisa's message flagged, header cut", r[0] and r[1] == "Combinado, Turuk anotado na agenda", str(r))
    r = await one("SELECT to_lisa, by_lisa FROM lisa_search.messages WHERE msg_id = 'O2'")
    check("the owner's @lisa request flagged to_lisa", r == (True, False))
    r = await one("SELECT text, edited FROM lisa_search.messages WHERE msg_id = 'A3'")
    check("an edit rewrote its target", r == ("reunião às 11", True), str(r))
    r = await one("SELECT text, ts FROM lisa_search.messages WHERE msg_id = 'DUP1'")
    check("a message stored twice is one row, at the earliest time (a re-send has a fresh one)",
          r == ("mensagem duplicada", T0 + 5 * DAY), str(r))
    r = await one("SELECT text FROM lisa_search.messages WHERE msg_id = 'DUP2'")
    check("an empty copy then a full one: the full one", r == ("agora com conteúdo",), str(r))
    r = await one("SELECT transcript, text FROM lisa_search.messages WHERE msg_id = 'VOICE1'")
    check("a transcript that existed before indexing is in", r[0] == "done" and "Lisboa" in r[1])
    r = await one("SELECT transcript, text, seconds FROM lisa_search.messages WHERE msg_id = 'VOICE2'")
    check("a voice note without a transcript is pending", r == ("pending", "", 30), str(r))
    r = await one("SELECT quoted_msg_id FROM lisa_search.messages WHERE msg_id = 'A11'")
    check("a reply knows what it quotes", r[0] == "A10")
    r = await one("SELECT sender_jid FROM lisa_search.messages WHERE msg_id = 'G3'")
    check("the owner's group message is his", r[0] == OWNER)

    # --- the store over the backfilled index --------------------------------------------------
    store = PgHistoryStore(pg.dsn)
    await store.open()
    await p3_store(store, conn)

    print("P4 — incremental sync: late arrivals, re-key, transcripts, names, idempotency")
    before = await snapshot(conn)
    st = await idx.run_once()
    after = await snapshot(conn)
    check("a pass with nothing new writes nothing (overlap re-read is free)", before == after,
          json.dumps(st))

    # Transcripts arriving after the note was indexed; the voice front giving up on another.
    await conn.execute("INSERT INTO lisa_log.transcripts (wa_id, text) VALUES ('VOICE2', 'o hackathon de Recife')")
    # The voice front's table, in its production shape.
    await conn.execute("""CREATE TABLE IF NOT EXISTS lisa_search.voice_attempts (
        msg_id text PRIMARY KEY, chat text, ts bigint, seconds integer, attempts integer NOT NULL DEFAULT 0,
        status text NOT NULL, last_error text, next_at timestamptz, updated_at timestamptz DEFAULT now())""")
    await conn.execute("INSERT INTO lisa_search.voice_attempts (msg_id, status) VALUES "
                       "('VOICE3', 'expired'), ('VOICE4', 'pending'), ('VOICE5', 'skipped')")
    # Bruno's phone becomes known; a late backlog lands with OLD timestamps but new ids.
    await evo.msg(BRUNO_LID, "agora com telefone", mid="B9", ts=T0 + 50 * DAY, alt=BRUNO, push="Bruno B.",
                  stored_ms=(T0 + 50 * DAY) * 1000)
    await evo.msg(CARLA_LID, "mensagem atrasada do backlog sobre Lisboa", mid="L1", ts=T0 + 10 * DAY + 2,
                  stored_ms=(T0 + 50 * DAY) * 1000 + 500)
    await evo.msg(GROUP, "Dani de novo", mid="G5", ts=T0 + 51 * DAY, part=DANI_LID, part_alt=DANI,
                  push="Dani", stored_ms=(T0 + 51 * DAY) * 1000)
    directory.ready = True
    st = await idx.run_once()
    check("the pass picked up the new rows", st["ingested"] >= 3 and not st.get("errors"), json.dumps(st))
    r = await one("SELECT transcript, text FROM lisa_search.messages WHERE msg_id = 'VOICE2'")
    check("a transcript that arrived later is pulled in", r == ("done", "o hackathon de Recife"), str(r))
    r = await one("SELECT transcript FROM lisa_search.messages WHERE msg_id = 'VOICE3'")
    check("an expired note is marked from voice_attempts", r[0] == "expired")
    r = await one("SELECT transcript FROM lisa_search.messages WHERE msg_id = 'VOICE4'")
    check("a note still being tried stays pending", r[0] == "pending")
    r = await one("SELECT transcript FROM lisa_search.messages WHERE msg_id = 'VOICE5'")
    check("a note the voice front skipped reads as failed", r[0] == "failed")
    rows = await all_("SELECT DISTINCT chat FROM lisa_search.messages WHERE msg_id LIKE 'B%'")
    check("Bruno's old @lid messages moved to his phone jid", rows == [(BRUNO,)], str(rows))
    rows = await all_("SELECT chat, aliases, name, n_messages FROM lisa_search.chats WHERE chat IN (%s, %s)",
                      BRUNO, BRUNO_LID)
    check("…and his chat rows merged into one, keeping the name and the count",
          len(rows) == 1 and rows[0][0] == BRUNO and BRUNO_LID in rows[0][1] and rows[0][2] == "Bruno B."
          and rows[0][3] == 4, str(rows))
    r = await one("SELECT ts, chat FROM lisa_search.messages WHERE msg_id = 'L1'")
    check("a late backlog row (old timestamp, new id) is indexed at its own time",
          r == (T0 + 10 * DAY + 2, CARLA_LID), str(r))
    r = await one("SELECT name FROM lisa_search.jids WHERE jid = %s", DANI_LID)
    check("the address book's name wins once the directory is ready", r[0] == "Daniel Souza", str(r))

    res = await store.search(terms=["recife"])
    check("the late transcript is searchable", [h["msg_id"] for h in res["hits"]] == ["VOICE2"])
    res = await store.search(terms=["hackathon"], chat=BRUNO_LID)
    check("searching by the old @lid finds the re-keyed chat", res["total"] == 3 and
          all(h["chat"] == BRUNO for h in res["hits"]), str(res["total"]))
    rc = await store.read_chat(chat=CARLA_LID)
    check("read_chat puts the backlog row at its sent time",
          [m["msg_id"] for m in rc["messages"]] == ["C1", "L1", "C2"], str([m["msg_id"] for m in rc["messages"]]))
    res = await store.search(terms=["Dani de novo"])
    check("a group sender shows the address-book name", res["hits"] and res["hits"][0]["sender"] == "Daniel Souza",
          str(res["hits"][:1]))

    before = await snapshot(conn)
    await idx.run_once()
    await idx.run_once()
    check("two more passes change nothing (idempotent)", before == await snapshot(conn))

    # An edit arriving later rewrites its target; a re-read of the original does not undo it.
    await evo.msg(BRUNO_LID, None, mid="B10", ts=T0 + 52 * DAY, alt=BRUNO, mtype="protocolMessage",
                  message={"protocolMessage": {"type": 14, "key": {"id": "B9"},
                                               "editedMessage": {"conversation": "agora com telefone novo"}}},
                  stored_ms=(T0 + 52 * DAY) * 1000)
    await idx.run_once()
    await idx.run_once()
    r = await one("SELECT text FROM lisa_search.messages WHERE msg_id = 'B9'")
    check("a later edit sticks across re-reads", r[0] == "agora com telefone novo", str(r))

    await store.aclose()
    await idx.aclose()

    print("P5 — never raises")
    bad = HistoryIndexer(pg.dsn.replace(pg.dbname, "postgres") if pg.container else pg.dsn,
                         schema="lisa_search_bad", settings=settings())
    try:
        await bad.open()
        await conn.execute("SELECT 1")
        st = await bad.run_once()
        check("an indexer on a database without Evolution's tables logs and returns",
              "ingest" in st.get("errors", []) or st["ingested"] == 0, json.dumps(st))
    except Exception as exc:
        check("an indexer on a database without Evolution's tables logs and returns", False, repr(exc))
    finally:
        await bad.aclose()
    ghost = PgHistoryStore(pg.dsn, schema="no_such_schema")
    await ghost.open()
    res = await ghost.search(terms=["x"])
    cov = await ghost.coverage()
    check("a store over a missing index answers empty", res == {"total": 0, "hits": [], "by_chat": []}
          and cov["n_messages"] == 0)
    check("…every method", await ghost.list_chats(name="a") == [] and await ghost.get_message("A1") is None
          and (await ghost.read_chat(chat=ANA))["messages"] == []
          and await ghost.topics_together(topics=[["a"], ["b"]]) == [])
    await ghost.aclose()
    await conn.close()


async def p3_store(store, conn) -> None:
    print("P3 — the store: search, topics, chats, reading")
    S = store.search
    res = await S(terms=["sao paulo"])
    check("accent-insensitive: 'sao paulo' finds 'São Paulo'", [h["msg_id"] for h in res["hits"]] == ["A2"])
    res = await S(terms=["eua"])
    check("case-insensitive: 'eua' finds 'EUA'", "A4" in [h["msg_id"] for h in res["hits"]])
    res = await S(terms=["acelerad"])
    check("substring: 'acelerad' finds 'aceleradora'", res["total"] == 1)
    res = await S(terms=["turuk"])
    ids = [h["msg_id"] for h in res["hits"]]
    check("Lisa's messages and the owner's requests to her are left out by default",
          "A5" not in ids and "O3" not in ids and "O2" not in ids and "O1" in ids, str(ids))
    res2 = await S(terms=["turuk"], include_lisa=True)
    ids2 = [h["msg_id"] for h in res2["hits"]]
    check("include_lisa brings them back", {"A5", "O3", "O2"} <= set(ids2) and res2["total"] == res["total"] + 3)
    check("results are newest first", [h["ts"] for h in res["hits"]] == sorted(
        [h["ts"] for h in res["hits"]], reverse=True))
    check("a file name is searchable", "A6" in ids)
    check("total counts every match, not just the page",
          (await S(terms=["turuk"], limit=2))["total"] == res["total"] and len((await S(terms=["turuk"], limit=2))["hits"]) == 2)
    byc = {c["chat"]: c["n_hits"] for c in res["by_chat"]}
    check("by_chat: hits per chat, most first", res["by_chat"][0]["n_hits"] == max(byc.values())
          and byc.get(ANA) == 3, str(byc))
    check("by_chat rows carry the chat's name", any(c["name"] == "Ana Paula" for c in res["by_chat"]))
    res = await S(terms=["demo day", "turuk"], match="all")
    check("match all: both terms in one message", {h["msg_id"] for h in res["hits"]} == {"C1", "O1"},
          str([h["msg_id"] for h in res["hits"]]))
    res = await S(terms=["demo day", "turuk"], match="any")
    check("match any: either term; messages with both rank first",
          res["total"] > 2 and {h["msg_id"] for h in res["hits"][:2]} == {"C1", "O1"},
          str([h["msg_id"] for h in res["hits"][:3]]))
    res = await S(terms=[r"\mdemo\s+day\M"], regex=True)
    check("regex", {"A1", "G1", "C1", "H1", "O1"} <= {h["msg_id"] for h in res["hits"]})
    res = await S(terms=[r"reuni[aã]o às 1[01]"], regex=True)
    check("regex is accent-blind too", [h["msg_id"] for h in res["hits"]] == ["A3"])
    res = await S(terms=["(unclosed"], regex=True)
    check("a bad regex answers empty instead of raising", res == {"total": 0, "hits": [], "by_chat": []})
    res = await S(terms=["reunião às 10"])
    check("an edited message is found by its new words only", res["total"] == 0
          and (await S(terms=["reuniao as 11"]))["total"] == 1)
    res = await S(terms=["turuk"], chat=ANA)
    check("filter: chat", res["total"] == 3 and all(h["chat"] == ANA for h in res["hits"]))
    res = await S(terms=["turuk"], chat=ANA_LID)
    check("filter: chat given as its @lid twin", res["total"] == 3)
    res = await S(terms=["turuk"], chat="5511888880000")
    check("filter: chat given as bare digits", res["total"] == 3)
    res = await S(terms=["turuk"], from_me=True)
    check("filter: from_me", {h["msg_id"] for h in res["hits"]} == {"O1"})
    res = await S(terms=["turuk"], kinds=["document"])
    check("filter: kinds", [h["msg_id"] for h in res["hits"]] == ["A6"]
          and res["hits"][0]["file_name"] == "Proposta Turuk.pdf")
    res = await S(terms=["turuk"], after=T0 + 9 * DAY, before=T0 + 31 * DAY)
    check("filter: after/before", {h["msg_id"] for h in res["hits"]} == {"C1", "O1"},
          str([h["msg_id"] for h in res["hits"]]))
    res = await S(terms=[], kinds=["reaction"])
    check("reactions only when asked for", [h["msg_id"] for h in res["hits"]] == ["A7"]
          and "A7" not in [h["msg_id"] for h in (await S(terms=[], chat=ANA, limit=100))["hits"]])
    res = await S(terms=["lisboa"])
    h = res["hits"][0] if res["hits"] else {}
    check("a transcript is searchable, as a voice hit", h.get("msg_id") == "VOICE1" and h.get("kind") == "voice"
          and h.get("transcript") == "done" and h.get("seconds") == 12)
    res = await S(terms=["investidor"])
    h = next((x for x in res["hits"] if x["msg_id"] == "A10"), {})
    check("a long message comes back as a ~300-char snippet around the match",
          "investidor de Turuk" in h.get("text", "") and len(h.get("text", "")) <= 304, str(len(h.get("text", ""))))
    check("a reply does not match on the text it quotes", "A11" not in [x["msg_id"] for x in res["hits"]])
    res = await S(terms=["Demo Day é em Austin"])
    h = res["hits"][0] if res["hits"] else {}
    check("hits carry sender, chat name, kind, flags",
          h.get("sender") == "Ana Paula" and h.get("chat_name") == "Founders Club" and h.get("kind") == "text"
          and h.get("from_me") is False and h.get("by_lisa") is False, str(h))
    res = await S(terms=["fechado"])
    check("the owner's own message names him", res["hits"] and res["hits"][0]["sender"] == "Marcelo")
    res = await S(terms=["mensagem 1 sobre"])
    check("a sender known only by pushName", res["hits"] and res["hits"][0]["sender"] == "Bruno B.")
    res = await S(terms=["leva o pitch"])
    check("an @mention comes back with the person's name", res["hits"] and
          res["hits"][0]["text"] == "@Dani você leva o pitch?", str(res["hits"][:1]))
    res = await S(terms=["lembrete encaminhado"])
    check("the self-chat's inbound row is named for the owner, not 'Você'",
          res["hits"] and res["hits"][0]["sender"] == "Marcelo", str(res["hits"][:1]))

    T = store.topics_together
    out = await T(topics=[["demo day"], ["turuk", "turuc"]], within_days=14)
    chats = [o["chat"]["chat"] for o in out]
    check("topics_together: tightest window first (one message holds both)",
          set(chats[:2]) == {CARLA_LID, OWNER}, str(chats))
    check("…then the chats where they met within the window, tighter first",
          chats[2:] == [GROUP, ANA], str(chats))
    check("…a chat where they were 39 days apart is left out", GROUP2 not in chats)
    check("…ties on the window go to the more recent chat", chats[0] == OWNER)
    ana = next(o for o in out if o["chat"]["chat"] == ANA)
    texts = " ".join(e["text"].lower() for e in ana["examples"])
    check("…examples cover every topic", "demo day" in texts and "turuk" in texts, texts[:120])
    check("…the window is where the topics meet", ana["first_ts"] == T0 and ana["last_ts"] <= T0 + 3 * DAY + 10
          and ana["first_ts"] <= ana["last_ts"], f"{ana['first_ts'] - T0}..{ana['last_ts'] - T0}")
    out = await T(topics=[["demo day"], ["turuk"]], within_days=60)
    check("a wider window brings the far chat in", GROUP2 in [o["chat"]["chat"] for o in out])
    out = await T(topics=[["demo day"], ["nada disso existe"]])
    check("a topic nobody mentioned: no chats", out == [])
    out = await T(topics=[["demo day"], ["turuk"]], after=T0 + 9 * DAY)
    check("topics_together honours after", {o["chat"]["chat"] for o in out} == {CARLA_LID, OWNER},
          str([o["chat"]["chat"] for o in out]))

    L = store.list_chats
    out = await L(name="ana")
    check("list_chats: a name", out and out[0]["chat"] == ANA, str([o["chat"] for o in out]))
    out = await L(name="carla diaz")
    check("list_chats: fuzzy (a typo)", out and out[0]["chat"] == CARLA_LID, str([o["name"] for o in out]))
    out = await L(name="sao paulo")
    check("list_chats: accent-insensitive", out and out[0]["name"] == "São Paulo Tech")
    out = await L(name="aninha")
    check("list_chats: any name the chat goes by (her own WhatsApp name)", out and out[0]["chat"] == ANA)
    out = await L(name="88888")
    check("list_chats: phone digits", [o["chat"] for o in out] == [ANA])
    out = await L(kind="group")
    check("list_chats: kind", out and all(o["kind"] == "group" for o in out))
    out = await L()
    check("list_chats: most recent first", [o["last_ts"] for o in out] == sorted(
        [o["last_ts"] for o in out], reverse=True))
    out = await L(active_after=T0 + 25 * DAY)
    check("list_chats: active_after", {o["chat"] for o in out} == {OWNER, GROUP2}, str([o["chat"] for o in out]))
    s = (await L(name="ana"))[0]
    check("a chat summary has every field", set(s) >= {"chat", "name", "kind", "phone", "n_messages",
                                                        "first_ts", "last_ts"} and s["n_messages"] > 5)

    R = store.read_chat
    rc = await R(chat=ANA, limit=200)
    ids = [m["msg_id"] for m in rc["messages"]]
    check("read_chat: in order, reactions left out, Lisa's message in",
          ids[:5] == ["A1", "A2", "A3", "A4", "A5"] and "A7" not in ids and not rc["more_before"]
          and not rc["more_after"], str(ids))
    check("read_chat: full text up to the cap", any(len(m["text"]) > 300 and len(m["text"]) <= 1500
                                                    for m in rc["messages"] if m["msg_id"] == "A10"))
    rc = await R(chat=ANA, around_msg_id="A6", limit=4)
    ids = [m["msg_id"] for m in rc["messages"]]
    check("read_chat around a message: ~half each side", ids == ["A4", "A5", "A6", "A9"]
          and rc["more_before"] and rc["more_after"], str(ids))
    rc = await R(chat=ANA_LID, limit=3)
    check("read_chat by @lid, latest window", rc["chat"]["chat"] == ANA and len(rc["messages"]) == 3
          and rc["more_before"] and not rc["more_after"])
    rc = await R(chat=ANA, after=T0 + 2 * DAY, before=T0 + 3 * DAY, limit=10)
    check("read_chat after/before", [m["msg_id"] for m in rc["messages"]] == ["A4", "A5", "A6"]
          and rc["more_before"] and rc["more_after"])
    rc = await R(chat=ANA, before=T0 + 100, limit=10)
    check("read_chat before only", [m["msg_id"] for m in rc["messages"]] == ["A1", "A2"])
    rc = await R(chat=GROUP, around_msg_id="A1", limit=4)
    check("read_chat around a message of another chat reads that message's chat",
          rc["chat"]["chat"] == ANA and "A1" in [m["msg_id"] for m in rc["messages"]])

    g = await store.get_message("A10")
    check("get_message: the whole message", g and g["msg_id"] == "A10" and "investidor de Turuk" in g["text"]
          and g["chat"] == ANA and g["sender"] == "Ana Paula")
    check("get_message: unknown id", await store.get_message("NOPE") is None)
    cov = await store.coverage()
    check("coverage", cov["first_ts"] == T0 and cov["voice_total"] == 5 and cov["voice_transcribed"] == 1
          and cov["n_chats"] >= 8 and cov["complete"] and cov["n_messages"] > 30, json.dumps(cov))


# ============================ part 6: scale (opt-in) ======================================
async def p6_perf(pg: PG) -> None:
    """160k synthetic rows in production's proportions; times the backfill and every query."""
    import psycopg

    from app.history.indexer import HistoryIndexer
    from app.history.store import PgHistoryStore

    print("P6 — 160k rows: backfill and query latency")
    perf_db = pg.dbname + "_perf"
    async with await psycopg.AsyncConnection.connect(pg.admin, autocommit=True) as c:
        await c.execute(f"CREATE DATABASE {perf_db}")
    dsn = pg.dsn.replace(pg.dbname, perf_db)
    try:
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        await conn.execute(EVOLUTION_DDL)
        await conn.execute('INSERT INTO "Instance" VALUES (%s, %s, %s)', (INSTANCE, "secretaria", OWNER))
        rnd = random.Random(7)
        words = ("reunião investidor proposta viagem amanhã cliente produto contrato evento demo "
                 "aceleradora São Paulo Lisboa time pitch rodada fundo planilha valuation café "
                 "almoço jantar semana próxima ligação call agenda deck feedback ok beleza obrigado "
                 "combinado mercado vendas marketing hiring vaga candidato entrevista").split()
        people = [(f"{100000000000000 + i}@lid", f"55119{i:08d}@s.whatsapp.net") for i in range(560)]
        groups = [f"1203630000{i:08d}@g.us" for i in range(120)]
        evo = Evo(conn)
        t_seed = time.monotonic()
        rows = []
        N = 160_000
        for i in range(N):
            ts = T0 - 140 * DAY + int(i * (140 * DAY / N))
            if rnd.random() < 0.58:
                g = groups[int(rnd.paretovariate(1.2)) % len(groups)]
                lid, phone = people[rnd.randrange(len(people))]
                key = {"id": f"P{i}", "remoteJid": g, "fromMe": rnd.random() < 0.1, "participant": lid,
                       "participantAlt": phone, "addressingMode": "lid"}
            else:
                lid, phone = people[int(rnd.paretovariate(1.1)) % len(people)]
                key = {"id": f"P{i}", "remoteJid": lid, "fromMe": rnd.random() < 0.5, "remoteJidAlt": phone}
            r = rnd.random()
            if r < 0.78:
                mt, msg = "conversation", {"conversation": " ".join(rnd.choice(words) for _ in range(
                    max(1, int(rnd.expovariate(1 / 9)))))}
            elif r < 0.86:
                mt, msg = "reactionMessage", {"reactionMessage": {"text": "👍", "key": {"id": f"P{i - 1}"}}}
            elif r < 0.92:
                mt, msg = "imageMessage", {"imageMessage": {"caption": rnd.choice(["", "olha isso", "foto"]),
                                                            "jpegThumbnail": "A" * 3000, "mimetype": "image/jpeg"}}
            elif r < 0.94:
                mt, msg = "stickerMessage", {"stickerMessage": {"mimetype": "image/webp"}}
            elif r < 0.96:
                mt, msg = "audioMessage", {"audioMessage": {"seconds": rnd.randint(2, 200), "ptt": True}}
            elif r < 0.97:
                mt, msg = "documentMessage", {"documentMessage": {"fileName": f"doc {i}.pdf"}}
            else:
                mt, msg = "secretEncryptedMessage", {"secretEncryptedMessage": {"encPayload": "x" * 200}}
            rows.append((evo.cuid(ts * 1000), json.dumps(key), "Fulano", mt, json.dumps(msg), ts, INSTANCE))
        async with conn.cursor() as cur:
            async with cur.copy('COPY "Message" (id, key, "pushName", "messageType", message, '
                                '"messageTimestamp", "instanceId") FROM STDIN') as cp:
                for r in rows:
                    await cp.write_row(r)
        await conn.execute('ANALYZE "Message"')
        print(f"  seeded {N} rows in {time.monotonic() - t_seed:.1f}s")
        pause = float(os.environ.get("HISTORY_PERF_PAUSE", "0.25"))
        idx = HistoryIndexer(dsn, settings=settings(owner_jid=OWNER), batch_size=500, pause_seconds=pause)
        await idx.open()
        t = time.monotonic()
        st = await idx.run_once()
        took = time.monotonic() - t
        print(f"  backfill: {took:.1f}s wall ({st['pages']} pages, pause {pause}s each; "
              f"{took - st['pages'] * pause:.1f}s of work)  {json.dumps(st)}")
        check("the backfill completes", not st.get("errors"))
        t = time.monotonic()
        st = await idx.run_once()
        print(f"  an incremental pass with nothing new: {(time.monotonic() - t) * 1000:.0f} ms {json.dumps(st)}")
        await conn.execute("ANALYZE lisa_search.messages")
        cur = await conn.execute(
            "SELECT pg_size_pretty(pg_table_size('lisa_search.messages')), "
            "pg_size_pretty(pg_indexes_size('lisa_search.messages')), "
            "pg_size_pretty(pg_relation_size('lisa_search.messages_norm_trgm')), "
            "pg_size_pretty(pg_total_relation_size('lisa_search.messages') + "
            "pg_total_relation_size('lisa_search.chats') + pg_total_relation_size('lisa_search.jids'))")
        print("  size: table %s, indexes %s (trigram GIN %s), schema total %s" % tuple(await cur.fetchone()))
        store = PgHistoryStore(dsn)
        await store.open()
        timings = []

        async def timed(label, coro):
            t = time.monotonic()
            res = await coro
            ms = (time.monotonic() - t) * 1000
            timings.append((label, ms))
            n = res.get("total") if isinstance(res, dict) and "total" in res else (
                len(res) if isinstance(res, list) else "")
            print(f"  {ms:7.0f} ms  {label}  {n}")
            return res

        await timed("search 'investidor' (warm-up)", store.search(terms=["investidor"]))
        await timed("search 'valuation'", store.search(terms=["valuation"]))
        await timed("search 'sao paulo' + 'lisboa' (all)", store.search(terms=["sao paulo", "lisboa"]))
        await timed("search 'pitch','deck','rodada' (any)", store.search(terms=["pitch", "deck", "rodada"], match="any"))
        await timed("search 'ok' (2 chars, no trigram)", store.search(terms=["ok"]))
        await timed("search regex \\mdemo\\M", store.search(terms=[r"\mdemo\M"], regex=True))
        await timed("search 'reuniao' in one chat", store.search(terms=["reuniao"], chat=groups[1]))
        await timed("search no terms, last 7 days", store.search(terms=[], after=T0 - 7 * DAY))
        await timed("topics_together [investidor] x [lisboa,portugal]",
                    store.topics_together(topics=[["investidor"], ["lisboa", "portugal"]]))
        await timed("topics_together [demo] x [us,eua] (short alt)",
                    store.topics_together(topics=[["demo"], ["us", "eua"]]))
        await timed("list_chats 'fulano'", store.list_chats(name="fulano"))
        await timed("list_chats recent", store.list_chats())
        await timed("read_chat latest 40", store.read_chat(chat=groups[0]))
        await timed("read_chat around a message", store.read_chat(chat=groups[0], around_msg_id="P5000"))
        await timed("coverage", store.coverage())
        worst = max(ms for _, ms in timings[1:])
        check("every query under 1 s on 160k rows", worst < 1000, f"worst {worst:.0f} ms")
        await store.aclose()
        await idx.aclose()
        await conn.close()
    finally:
        async with await psycopg.AsyncConnection.connect(pg.admin, autocommit=True) as c:
            await c.execute(f"DROP DATABASE IF EXISTS {perf_db} WITH (FORCE)")


async def main() -> int:
    p1_extract()
    print()
    async with PG() as pg:
        if pg is None:
            print("P2-P5 — real Postgres — SKIPPED (no Docker and no HISTORY_TEST_DATABASE_URL)")
        else:
            await p2_index(pg)
            if os.environ.get("HISTORY_PERF"):
                print()
                await p6_perf(pg)
    print()
    print(f"{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
