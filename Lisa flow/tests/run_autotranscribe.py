"""End-to-end verification for automatic transcription + the setup skill.

Drives the REAL compiled graph with an in-memory checkpointer, a stub transcriber, a fake
Evolution and a stub reasoner. No network, no Postgres, no AssemblyAI key, no Anthropic key.

    cd "Lisa flow" && python tests/run_autotranscribe.py
Exits non-zero if any check fails.

Covers: roster matching, the gate's automatic branch, quoted delivery and its silence on
failure, the shared formatting contract with the manual path, the cost rails, the card/group
resolution paths, the ordinal handles, and the two structural gates (self-chat, resolved id)."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app import chatfind  # noqa: E402
from app.cache import TranscriptionService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.roster import DailyCap, Roster, make_rule, normalize_direction  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import (  # noqa: E402
    SKILLS, confirm_policies, count_optionals, count_unions, handlers,
    output_schema_for, render_policies, resolve_gates, routable,
)
from app.skills.setup import setup_matcher, setup_resolve_gate  # noqa: E402
from app.skills.setup_format import fmt_list  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402
from app.transcribe_reply import inline_body  # noqa: E402
from app.whatsapp import chat_key, contact_cards  # noqa: E402

OWNER = "5511976001033"
OWNER_JID = f"{OWNER}@s.whatsapp.net"
MAE = "5511976004417"
MAE_JID = f"{MAE}@s.whatsapp.net"
GROUP = "120363042-1699"
GROUP_JID = f"{GROUP}@g.us"

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    tail = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{tail}")
    _checks["pass" if cond else "fail"] += 1


# ============================ stubs =======================================================

class FakeEvolution:
    def __init__(self, chats=None, groups=None) -> None:
        self.sent: list[dict] = []
        self.media: list[dict] = []
        self.history: dict[str, list[dict]] = {}
        self._chats = chats or []
        self._groups = groups or []
        self.fail_chats = False

    async def send_text(self, number, text, *, quoted=None):
        self.sent.append({"number": number, "text": text, "quoted": quoted})
        return f"sent{len(self.sent)}"

    async def send_media(self, number, *, mediatype, mimetype, media_b64, filename, caption,
                         quoted=None):
        self.media.append({"number": number, "filename": filename, "caption": caption,
                           "quoted": quoted})
        return True

    async def get_media_base64(self, message_id, *, convert_to_mp4=False):
        import base64
        return {"base64": base64.b64encode(message_id.encode()).decode(), "mimetype": "audio/ogg"}

    async def fetch_history(self, remote_jid):
        return list(self.history.get(remote_jid, []))

    async def find_chats(self):
        return [] if self.fail_chats else list(self._chats)

    async def fetch_groups(self):
        return [] if self.fail_chats else list(self._groups)

    async def fetch_owner_jid(self):
        return OWNER_JID


class StubTranscriber:
    """Maps the audio bytes back to the wa id FakeEvolution encoded, so results are scriptable
    per id and the call log proves the cache is shared."""

    def __init__(self, script=None) -> None:
        self.script = script or {}
        self.calls: list[str] = []

    async def transcribe(self, audio: bytes, *, mimetype, language):
        wa_id = audio.decode()
        self.calls.append(wa_id)
        return self.script.get(wa_id, {"text": f"transcript of {wa_id}", "duration_sec": 5.0,
                                       "language": "en", "error": None})


class StubReasoner:
    """Returns scripted enforced-JSON decisions, one per reason call."""

    def __init__(self, replies=None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    async def respond(self, *, system, messages, output_schema=None, server_tools=None,
                      model=None, effort=None, think=False):
        self.calls.append({"system": system, "messages": list(messages),
                           "schema": output_schema})
        base = {"state": "keep_listening", "message": None, "lang": "en", "actions": [],
                "workflow": None, "usage": {}, "provider_request_id": "req",
                "stop_reason": "end_turn", "tool_calls": [], "error_category": "none"}
        if self.replies:
            base.update(self.replies.pop(0))
        return base

    async def classify(self, *, system, messages, schema, max_tokens=32, effort="low"):
        return {"domain": "web"}


def build(settings=None, *, transcriber=None, reasoner=None, evolution=None, roster=None):
    s = settings or Settings(
        MARY_TRIGGER_TAG="@lisa", owner_jid=OWNER_JID, auto_transcribe_enabled=True,
        transcription_enabled=True, anthropic_api_key="", database_url=None, redis_url=None,
    )
    ev = evolution or FakeEvolution()
    tr = transcriber or StubTranscriber()
    rs = roster if roster is not None else Roster()
    tools = handlers(s)
    if "setup" in tools:
        tools["setup"].roster = rs
        tools["setup"].evolution = ev
    deps = Deps(
        settings=s, evolution=ev, sessions=InMemorySessions(ttl=s.loop_ttl_seconds),
        echoes=InMemoryEchoes(), trace=build_trace(), reasoner=reasoner or StubReasoner(),
        transcription=TranscriptionService(ev, tr, s),
        tools=tools, confirm_policies=confirm_policies(), render_policies=render_policies(),
        resolve_gates=resolve_gates(), roster=rs, caps=DailyCap(s.auto_transcribe_daily_cap),
    )
    return deps, build_graph(deps, MemorySaver()), ev, tr, rs


def audio_upsert(*, mid, jid, from_me, seconds=5, alt=None):
    key = {"id": mid, "remoteJid": jid, "fromMe": from_me}
    if alt:
        key["remoteJidAlt"] = alt
    return {"data": {"key": key, "message": {"audioMessage": {"seconds": seconds}},
                     "messageTimestamp": 1730000000, "pushName": "Someone"}}


def text_upsert(text, *, mid="t1", jid=OWNER_JID, from_me=True):
    return {"data": {"key": {"id": mid, "remoteJid": jid, "fromMe": from_me},
                     "message": {"conversation": text}, "messageTimestamp": 1730000000,
                     "pushName": "Marcelo"}}


def card_upsert(*, mid="c1", jid=OWNER_JID, name="Mãe", number=MAE):
    vcard = (f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\n"
             f"TEL;type=CELL;type=VOICE;waid={number}:+55 11 97600-4417\nEND:VCARD")
    return {"data": {"key": {"id": mid, "remoteJid": jid, "fromMe": True},
                     "message": {"contactMessage": {"displayName": name, "vcard": vcard}},
                     "messageTimestamp": 1730000000, "pushName": "Marcelo"}}


async def invoke(graph, body, jid=OWNER_JID):
    tid = make_thread_id("secretaria", jid)
    return await graph.ainvoke({"raw": body}, config={"configurable": {"thread_id": tid}})


# ============================ P1 — the roster =============================================

async def p1_roster():
    print("P1 — roster matching")
    r = Roster()
    await r.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound", label="Mãe"))
    await r.upsert(make_rule(chat_key=GROUP, chat_jid=GROUP_JID, direction="outbound",
                             kind="group", label="Futebol"))
    await r.upsert(make_rule(chat_key="777", chat_jid="777@s.whatsapp.net", direction="both",
                             alt_key="888"))

    check("inbound matches a received clip", bool(r.should_transcribe([MAE], False)))
    check("inbound ignores one I sent", not r.should_transcribe([MAE], True))
    check("outbound matches one I sent", bool(r.should_transcribe([GROUP], True)))
    check("outbound ignores a received clip", not r.should_transcribe([GROUP], False))
    check("both matches either way",
          bool(r.should_transcribe(["777"], True)) and bool(r.should_transcribe(["777"], False)))
    check("an unlisted chat never matches", not r.should_transcribe(["999"], False))
    check("the @lid alias matches the same rule", bool(r.should_transcribe(["888"], False)))
    check("removal stops matching, alias included",
          not (await r.remove("777")) is None
          and not r.should_transcribe(["777"], False)
          and not r.should_transcribe(["888"], False))

    closed = Roster(store=object())  # a store that has never loaded
    check("fail closed: an unloaded roster matches nothing",
          closed.should_transcribe([MAE], False) is None)
    check("direction aliases normalise",
          normalize_direction("in & out") == "both" and normalize_direction("IN") == "inbound"
          and normalize_direction("sideways") is None)

    cap = DailyCap(2)
    cap.record("x"); cap.record("x")
    check("daily cap stops one chat only", not cap.allows("x") and cap.allows("y"))
    check("cap of 0 means no cap", DailyCap(0).allows("x"))


# ============================ P2 — the automatic path =====================================

async def p2_auto():
    print("P2 — the automatic path")
    deps, graph, ev, tr, roster = build()
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound",
                                  label="Mãe"))

    await invoke(graph, audio_upsert(mid="a1", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("an enrolled inbound voice note is transcribed", len(ev.sent) == 1)
    sent = ev.sent[0] if ev.sent else {}
    check("the reply quotes the original audio",
          (sent.get("quoted") or {}).get("id") == "a1", detail=str(sent.get("quoted")))
    check("it is addressed to the chat", sent.get("number") == MAE)
    check("the body is the shared transcript format",
          inline_body("transcript of a1", "en") in (sent.get("text") or ""))
    check("no model call was made", not deps.reasoner.calls)

    # Direction mismatch: the same chat, but audio I sent.
    ev.sent.clear()
    await invoke(graph, audio_upsert(mid="a2", jid=MAE_JID, from_me=True), jid=MAE_JID)
    check("a direction the rule excludes stays silent", not ev.sent)

    # A chat that is not enrolled at all.
    await invoke(graph, audio_upsert(mid="a3", jid="5511999@s.whatsapp.net", from_me=False),
                 jid="5511999@s.whatsapp.net")
    check("a non-enrolled chat stays silent", not ev.sent)
    check("and it never reached the provider", "a3" not in tr.calls)

    # Groups address the full JID.
    deps2, graph2, ev2, _, roster2 = build()
    await roster2.upsert(make_rule(chat_key=GROUP, chat_jid=GROUP_JID, direction="both",
                                   kind="group", label="Futebol"))
    await invoke(graph2, audio_upsert(mid="g1", jid=GROUP_JID, from_me=True), jid=GROUP_JID)
    check("a group is addressed by its full JID",
          bool(ev2.sent) and ev2.sent[0]["number"] == GROUP_JID)


async def p3_delivery():
    print("P3 — delivery, failure and the cost rails")
    # Long clip -> a .txt document, still quoted.
    tr = StubTranscriber({"L1": {"text": "a very long transcript", "duration_sec": 999.0,
                                 "language": "en", "error": None}})
    deps, graph, ev, _, roster = build(transcriber=tr)
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    await invoke(graph, audio_upsert(mid="L1", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("a long clip is delivered as a .txt", len(ev.media) == 1 and not ev.sent)
    check("the document is quoted too",
          bool(ev.media) and (ev.media[0].get("quoted") or {}).get("id") == "L1")

    # Provider failure -> silence by default.
    tr = StubTranscriber({"F1": {"text": "", "error": "provider"}})
    deps, graph, ev, _, roster = build(transcriber=tr)
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    await invoke(graph, audio_upsert(mid="F1", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("a provider failure says nothing in the chat", not ev.sent and not ev.media)

    # Empty audio -> silence.
    tr = StubTranscriber({"E1": {"text": "", "error": "empty"}})
    deps, graph, ev, _, roster = build(transcriber=tr)
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    await invoke(graph, audio_upsert(mid="E1", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("a silent clip says nothing", not ev.sent)

    # Failures CAN be reported when debugging.
    s = Settings(MARY_TRIGGER_TAG="@lisa", owner_jid=OWNER_JID, auto_transcribe_enabled=True,
                 auto_transcribe_report_failures=True, database_url=None, redis_url=None)
    tr = StubTranscriber({"F2": {"text": "", "error": "provider"}})
    deps, graph, ev, _, roster = build(s, transcriber=tr)
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    await invoke(graph, audio_upsert(mid="F2", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("REPORT_FAILURES=true speaks up instead", len(ev.sent) == 1)

    # Over-long clip: skipped before the provider is touched.
    s = Settings(MARY_TRIGGER_TAG="@lisa", owner_jid=OWNER_JID, auto_transcribe_enabled=True,
                 auto_transcribe_max_seconds=60, database_url=None, redis_url=None)
    deps, graph, ev, tr, roster = build(s)
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    await invoke(graph, audio_upsert(mid="X1", jid=MAE_JID, from_me=False, seconds=600),
                 jid=MAE_JID)
    check("an over-long clip is skipped", not ev.sent)
    check("and never reaches the provider", "X1" not in tr.calls)

    # Daily cap.
    s = Settings(MARY_TRIGGER_TAG="@lisa", owner_jid=OWNER_JID, auto_transcribe_enabled=True,
                 auto_transcribe_daily_cap=1, database_url=None, redis_url=None)
    deps, graph, ev, tr, roster = build(s)
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    await invoke(graph, audio_upsert(mid="C1", jid=MAE_JID, from_me=False), jid=MAE_JID)
    await invoke(graph, audio_upsert(mid="C2", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("the daily cap stops the second clip", len(ev.sent) == 1)
    check("the capped clip never reached the provider", "C2" not in tr.calls)

    # The master switch.
    s = Settings(MARY_TRIGGER_TAG="@lisa", owner_jid=OWNER_JID, auto_transcribe_enabled=False,
                 database_url=None, redis_url=None)
    deps, graph, ev, _, roster = build(s)
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    await invoke(graph, audio_upsert(mid="O1", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("AUTO_TRANSCRIBE_ENABLED=false disables the path entirely", not ev.sent)

    # Echo safety: Lisa's own outbound id is remembered so it can never be re-ingested.
    deps, graph, ev, _, roster = build()
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    await invoke(graph, audio_upsert(mid="a9", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("the transcript's own message id is recorded as an echo",
          deps.echoes.is_ours(MAE_JID, ev.sent[0]["text"] and "sent1"))


async def p4_window():
    print("P4 — an enrolled chat that is ALSO mid-conversation")
    deps, graph, ev, tr, roster = build()
    await roster.upsert(make_rule(chat_key=MAE, chat_jid=MAE_JID, direction="inbound"))
    deps.sessions.open(MAE_JID)  # a live @lisa window on the same chat
    ev.history[MAE_JID] = [
        {"id": "a5", "from_me": False, "text": "", "push_name": "Mãe", "ts": 1730000000,
         "is_audio": True, "media_type": "audio", "media_mimetype": None,
         "media_filename": None, "contact_cards": []},
    ]
    await invoke(graph, audio_upsert(mid="a5", jid=MAE_JID, from_me=False), jid=MAE_JID)
    check("the transcript is still posted", bool(ev.sent))
    check("and the normal turn runs as well", bool(deps.reasoner.calls))
    check("the provider was called ONCE for that clip (cache shared)",
          tr.calls.count("a5") == 1, detail=str(tr.calls))


# ============================ P5 — setup ==================================================

async def p5_setup_structure():
    print("P5 — the setup skill's shape")
    sc = output_schema_for("setup")
    tasks = {b["properties"]["task"]["const"] for b in sc["properties"]["actions"]["items"]["anyOf"]}
    check("setup exposes exactly its six verbs",
          tasks == {f"setup.{v}" for v in ("menu", "list", "resolve", "enroll", "update", "remove")})
    check("schema is under the 16-union cap", count_unions(sc) <= 16, detail=str(count_unions(sc)))
    check("schema is under the 24-optional cap", count_optionals(sc) <= 24,
          detail=str(count_optionals(sc)))
    check("setup is routable only in the self-chat",
          "setup" in routable({"is_self_chat": True})
          and "setup" not in routable({"is_self_chat": False}))
    check("the matcher fires on configuration words", setup_matcher("@lisa setup") == "yes")
    check("but NOT on a transcription request",
          setup_matcher("transcreve esse audio pra mim") == "no")
    check("every skill still has its own schema under the caps",
          all(count_unions(output_schema_for(d)) <= 16 and count_optionals(output_schema_for(d)) <= 24
              for d in SKILLS))


async def p6_cards_and_groups():
    print("P6 — naming a chat: cards and group search")
    vc = "TEL;type=CELL;waid=5511976004417:+55 11 97600-4417"
    check("a vCard's waid is the chat key",
          contact_cards({"contactMessage": {"displayName": "Mãe", "vcard": vc}})[0]["number"] == MAE)
    check("a card with no waid falls back to the printed digits",
          contact_cards({"contactMessage": {"vcard": "TEL;type=CELL:+55 (11) 98888-7777"}})[0]["number"]
          == "5511988887777")
    check("a card with no usable number is dropped, not guessed",
          contact_cards({"contactMessage": {"vcard": "FN:no phone"}}) == [])
    check("several forwarded at once all come through",
          len(contact_cards({"contactsArrayMessage": {"contacts": [
              {"vcard": "TEL;waid=111:+1"}, {"vcard": "TEL;waid=222:+2"}]}})) == 2)

    groups = [
        {"chat_key": "g1", "label": "Futebol de terça", "last_ts": 1757700000},
        {"chat_key": "g2", "label": "Futebol terça, quadra nova", "last_ts": 1755000000},
        {"chat_key": "g3", "label": "Casa Abritta", "last_ts": 1757790000},
    ]
    r = chatfind.rank("futebol", groups)
    check("a partial name matches both football groups",
          r["matched"] and len(r["candidates"]) == 2)
    check("RECENCY breaks the tie — the one I used today is first",
          r["candidates"][0]["chat_key"] == "g1")
    r = chatfind.rank("futebol terca", groups)
    check("accents and word order are tolerated", r["matched"])
    r = chatfind.rank("Casa Abritta", groups)
    check("an exact subject resolves to one", r["candidates"][0]["chat_key"] == "g3")
    r = chatfind.rank("zzz nothing", groups)
    check("nothing above threshold → the recent list, flagged as not-a-match",
          not r["matched"] and r["candidates"][0]["chat_key"] == "g3")
    r = chatfind.rank("", groups)
    check("no query at all → the recent list too", not r["matched"] and len(r["candidates"]) == 3)

    merged = chatfind.merge_groups(
        [{"kind": "group", "key": "g1", "jid": GROUP_JID, "label": "", "last_ts": 42}],
        [{"key": "g1", "jid": GROUP_JID, "label": "Futebol de terça", "size": 14}])
    check("the group subject wins over a thin chat label",
          merged[0]["label"] == "Futebol de terça" and merged[0]["last_ts"] == 42)


async def p7_gates():
    print("P7 — the structural gates")
    st = {"is_self_chat": True, "listed_chats": {"5": "e"}, "seen_chat_keys": ["e"],
          "seen_chats": {"e": {"label": "Futebol", "kind": "group", "chat_jid": GROUP_JID}}}
    patched, err = setup_resolve_gate("update", {"ordinal": 5, "direction": "outbound"}, st)
    check("an ordinal resolves to the chat it was printed against",
          err is None and patched["chat_key"] == "e")
    check("and the write is enriched with what is already known",
          patched.get("chat_jid") == GROUP_JID and patched.get("label") == "Futebol")
    check("the ordinal itself never reaches the handler", "ordinal" not in patched)
    _, err = setup_resolve_gate("update", {"ordinal": 99, "direction": "inbound"}, st)
    check("an ordinal past the end is refused", err and err["error"] == "unresolved_id")
    _, err = setup_resolve_gate("enroll", {"chat_key": "never-seen", "direction": "inbound"}, st)
    check("a key never surfaced this loop is refused", err and err["error"] == "unresolved_id")
    _, err = setup_resolve_gate("enroll", {"direction": "inbound"}, st)
    check("naming no chat at all is refused", err and err["error"] == "unresolved_id")
    _, err = setup_resolve_gate("list", {}, {"is_self_chat": False})
    check("EVERY setup action is refused outside the self-chat",
          err and err["error"] == "not_self_chat")
    stale = {"is_self_chat": True, "listed_chats": {}, "seen_chat_keys": [], "seen_chats": {}}
    _, err = setup_resolve_gate("remove", {"ordinal": 5}, stale)
    check("a number from a previous loop resolves to nothing",
          err and err["error"] == "unresolved_id")


async def p8_crud():
    print("P8 — CRUD through the real graph")
    chats = [{"kind": "group", "key": GROUP, "jid": GROUP_JID, "label": "Futebol de terça",
              "last_ts": 1757700000}]
    groups = [{"key": GROUP, "jid": GROUP_JID, "label": "Futebol de terça", "size": 14}]
    ev = FakeEvolution(chats=chats, groups=groups)

    # Script the model: list → (card) enroll → resolve → enroll group → list → update → remove.
    reasoner = StubReasoner([
        {"actions": [{"task": "setup.list"}]},
        {"actions": [{"task": "setup.enroll", "chat_key": MAE, "direction": "both",
                      "label": "Mãe", "confirmed": False}]},
        {"actions": [{"task": "setup.resolve", "query": "futebol"}]},
        {"message": "Which one?", "state": "keep_listening"},
        {"actions": [{"task": "setup.enroll", "chat_key": GROUP, "direction": "inbound",
                      "confirmed": False}]},
        {"actions": [{"task": "setup.list"}]},
        {"actions": [{"task": "setup.update", "ordinal": 2, "direction": "outbound",
                      "confirmed": False}]},
        {"actions": [{"task": "setup.remove", "ordinal": 2, "confirmed": False}]},
    ])
    deps, graph, ev, tr, roster = build(evolution=ev, reasoner=reasoner)
    ev.history[OWNER_JID] = []

    async def turn(body):
        ev.history.setdefault(OWNER_JID, [])
        return await invoke(graph, body)

    # 1. open setup → list (empty)
    st = await turn(text_upsert("@lisa setup", mid="s1"))
    check("setup routes in the self-chat", st.get("domain") == "setup")
    check("the empty list is rendered in code",
          "nothing enrolled yet" in (st.get("reply") or "").lower(),
          detail=(st.get("reply") or "")[:60])

    # 2. a forwarded card, then the enrol proposal
    ev.history[OWNER_JID].append(
        {"id": "c1", "from_me": True, "text": "", "push_name": "Marcelo", "ts": 1730000001,
         "is_audio": False, "media_type": "text", "media_mimetype": None, "media_filename": None,
         "contact_cards": [{"name": "Mãe", "number": MAE}]})
    st = await turn(card_upsert(mid="c1"))
    check("a forwarded card becomes a resolved chat key",
          MAE in (st.get("seen_chat_keys") or []))
    check("the card is visible to the model as a transcript line",
          any("contact card" in str(getattr(m, "content", "")) for m in st.get("messages") or []))
    check("nothing is written yet — a confirmation is pending",
          bool(st.get("pending_action")) and roster.get(MAE) is None)
    check("the confirmation names the contact and the direction",
          "Mãe" in (st.get("reply") or "") and "in & out" in (st.get("reply") or ""),
          detail=(st.get("reply") or "")[:80])

    # 3. the owner's yes actually writes it
    st = await turn(text_upsert("sim", mid="s3"))
    check("a clean yes writes the rule", roster.get(MAE) is not None)
    check("the rule carries the direction confirmed",
          (roster.get(MAE) or {}).get("direction") == "both")
    check("the gate now transcribes that chat",
          bool(roster.should_transcribe([MAE], False)))

    # 4. a group by name → candidates, no write
    st = await turn(text_upsert("@lisa setup add the futebol group", mid="s4"))
    check("a group search surfaces real candidates",
          GROUP in (st.get("seen_chat_keys") or []))
    check("a resolve on its own writes nothing", roster.get(GROUP) is None)

    # 5. enrol the group, confirm
    st = await turn(text_upsert("o primeiro", mid="s5"))
    st = await turn(text_upsert("sim", mid="s6"))
    check("the group is enrolled after its own confirmation",
          (roster.get(GROUP) or {}).get("direction") == "inbound")
    check("and it is stored as a group",
          (roster.get(GROUP) or {}).get("kind") == "group", detail=str(roster.get(GROUP)))

    # 6. list → numbering across both sections
    st = await turn(text_upsert("@lisa setup what chats are active?", mid="s7"))
    reply = st.get("reply") or ""
    check("the list titles contacts and groups separately",
          "Contacts" in reply and "Groups" in reply, detail=reply[:70])
    check("numbering runs continuously across the sections",
          " 1. " in reply and " 2. " in reply)
    check("the ordinals are remembered for the next turn",
          (st.get("listed_chats") or {}).get("2") == GROUP, detail=str(st.get("listed_chats")))

    # 7. edit by number
    st = await turn(text_upsert("edit group 2 to outbound", mid="s8"))
    check("an edit by number echoes the number AND the name it resolved to",
          "2. Futebol de terça" in (st.get("reply") or ""), detail=(st.get("reply") or "")[:80])
    check("still nothing written before the yes",
          (roster.get(GROUP) or {}).get("direction") == "inbound")
    st = await turn(text_upsert("isso", mid="s9"))
    check("the direction changed after the yes",
          (roster.get(GROUP) or {}).get("direction") == "outbound")

    # 8. remove by number
    st = await turn(text_upsert("remove 2", mid="s10"))
    st = await turn(text_upsert("sim", mid="s11"))
    check("remove drops the row", roster.get(GROUP) is None)
    check("and the gate stops matching it", not roster.should_transcribe([GROUP], True))
    check("the removal re-lists what is left",
          "Mãe" in (st.get("reply") or ""), detail=(st.get("reply") or "")[:80])


async def p9_list_render():
    print("P9 — the list is rendered by the formatter, not the model")
    data = {"contacts": [{"n": 1, "chat_key": "a", "label": "Mãe", "direction": "both",
                          "kind": "contact"},
                         {"n": 2, "chat_key": "b", "label": "Rafael", "direction": "inbound",
                          "kind": "contact"}],
            "groups": [{"n": 3, "chat_key": "c", "label": "Casa Abritta", "direction": "inbound",
                        "kind": "group"}]}
    out = fmt_list([{"data": data}], {})
    check("the header counts the chats", out.startswith("Transcription — 3 chats"))
    check("contacts are titled", "*Contacts*" in out)
    check("groups are titled", "*Groups*" in out)
    check("the group continues the contacts' numbering", " 3. Casa Abritta" in out)
    check("directions use the fixed labels",
          "in & out" in out and "inbound" in out and "received" not in out)
    empty = fmt_list([{"data": {"contacts": [], "groups": []}}], {})
    check("an empty roster explains both ways in",
          "contact card" in empty and "group" in empty)
    only_contacts = fmt_list([{"data": {"contacts": data["contacts"], "groups": []}}], {})
    check("an empty section is omitted entirely", "*Groups*" not in only_contacts)


async def main() -> None:
    for fn in (p1_roster, p2_auto, p3_delivery, p4_window, p5_setup_structure,
               p6_cards_and_groups, p7_gates, p8_crud, p9_list_render):
        await fn()
        print()
    total = _checks["pass"] + _checks["fail"]
    print(f"{_checks['pass']}/{total} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
