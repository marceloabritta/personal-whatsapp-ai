"""End-to-end verification for the owner's meeting presets.

Drives the REAL compiled graph with an in-memory checkpointer, a fake Evolution, a fake Google
Calendar service and a stub reasoner. No network, no Postgres, no Anthropic key.

    cd "Lisa flow" && python tests/run_presets.py
Exits non-zero if any check fails.

Covers: the store and its fail-to-default direction, how presets are written into a calendar write
(length, video call, guest emails, places), that the card and the Google request read the same
values, the setup verbs and their cards, that PRESETS_ENABLED=false is the pre-feature build, the
calendar's prompt block, the setup loop holding its answers, and both flows through the graph."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.cache import TranscriptionService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.presets import Presets, fold  # noqa: E402
from app.roster import DailyCap, Roster  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import (  # noqa: E402
    SKILLS, confirm_policies, context_block_for, count_optionals, count_unions, handlers,
    output_schema_for, render_policies, resolve_gates, system_prompt_for,
)
from app.skills.calendar import presets_block  # noqa: E402
from app.skills.calendar_format import compose_create, compose_delete  # noqa: E402
from app.skills.router import _classify_system, route_domain  # noqa: E402
from app.skills.setup_format import (  # noqa: E402
    compose_remove_place, compose_save_place, compose_set_presets, fmt_list, fmt_menu,
    fmt_presets, fmt_remove_place, fmt_save_place, fmt_set_presets,
)
from app.threads import make_thread_id  # noqa: E402
from app.tools.calendar import GoogleCalendarService, apply_presets  # noqa: E402
from app.tools.setup import RosterService  # noqa: E402
from app.trace import build_trace  # noqa: E402

OWNER = "5511976001033"
OWNER_JID = f"{OWNER}@s.whatsapp.net"
ANA = "5511976004417"
ANA_JID = f"{ANA}@s.whatsapp.net"
START = "2026-11-12T10:00:00-03:00"

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    tail = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{tail}")
    _checks["pass" if cond else "fail"] += 1


def settings(**kw) -> Settings:
    base = dict(MARY_TRIGGER_TAG="@lisa", owner_jid=OWNER_JID, presets_enabled=True,
                anthropic_api_key="", database_url=None, redis_url=None, evolution_apikey="x")
    base.update(kw)
    return Settings(**base)


async def presets_with(**values) -> Presets:
    p = Presets(default_minutes=45)
    settings_part = {k: v for k, v in values.items() if k != "places"}
    if settings_part:
        await p.set_many(settings_part)
    for name, address in (values.get("places") or {}).items():
        await p.save_place(name, address)
    return p


# ============================ fakes =======================================================

class _Req:
    def __init__(self, result=None) -> None:
        self._r = result

    def execute(self):
        return self._r


class _Events:
    def __init__(self) -> None:
        self.calls: list = []
        self.items: list = []

    def insert(self, **kw):
        self.calls.append(("insert", kw))
        ev = dict(kw["body"])
        ev["id"] = "NEW1"
        ev["htmlLink"] = "http://cal/NEW1"
        # Only a REQUESTED conference produces a Meet — conferenceData: null on an insert does not.
        if ((ev.get("conferenceData") or {}).get("createRequest")
                and kw.get("conferenceDataVersion")):
            ev["hangoutLink"] = "http://meet.google.com/abc-defg-hij"
        self.items.append(ev)
        return _Req(ev)

    def get(self, **kw):
        self.calls.append(("get", kw))
        return _Req(next((e for e in self.items if e.get("id") == kw["eventId"]), {}))


class FakeGoogle:
    def __init__(self) -> None:
        self._events = _Events()

    def events(self):
        return self._events


class FakeEvolution:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.history: dict[str, list[dict]] = {}

    async def send_text(self, number, text, *, quoted=None):
        self.sent.append({"number": number, "text": text})
        return f"sent{len(self.sent)}"

    async def fetch_history(self, remote_jid):
        return list(self.history.get(remote_jid, []))

    async def find_chats(self):
        return []

    async def fetch_groups(self):
        return []

    async def fetch_owner_jid(self):
        return OWNER_JID


class StubReasoner:
    def __init__(self, replies=None, domain="web") -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.domain = domain

    async def respond(self, *, system, messages, output_schema=None, server_tools=None,
                      model=None, effort=None, think=False):
        self.calls.append({"system": system, "schema": output_schema})
        base = {"state": "keep_listening", "message": None, "lang": "en", "actions": [],
                "workflow": None, "usage": {}, "provider_request_id": "req",
                "stop_reason": "end_turn", "tool_calls": [], "error_category": "none"}
        if self.replies:
            base.update(self.replies.pop(0))
        return base

    async def classify(self, *, system, messages, schema, max_tokens=32, effort="low"):
        return {"domain": self.domain}


class FailingStore:
    """A durable tier that never answers a read and refuses every write."""

    async def load(self):
        return None

    async def upsert(self, key, value):
        raise RuntimeError("db down")

    async def remove(self, key):
        raise RuntimeError("db down")


def build(s: Settings, *, reasoner=None, presets=None):
    ev = FakeEvolution()
    roster = Roster()
    tools = handlers(s)
    tools["setup"].roster = roster
    tools["setup"].evolution = ev
    if presets is None and s.presets_enabled:
        presets = Presets(default_minutes=s.default_meeting_minutes)
    tools["setup"].presets = presets
    tools["calendar"].presets = presets
    tools["calendar"]._svc = FakeGoogle()
    deps = Deps(
        settings=s, evolution=ev, sessions=InMemorySessions(ttl=s.loop_ttl_seconds),
        echoes=InMemoryEchoes(), trace=build_trace(), reasoner=reasoner or StubReasoner(),
        transcription=TranscriptionService(ev, None, s),
        tools=tools, confirm_policies=confirm_policies(), render_policies=render_policies(),
        resolve_gates=resolve_gates(), roster=roster, caps=DailyCap(0), presets=presets,
    )
    return deps, build_graph(deps, MemorySaver()), ev


def text_upsert(text, *, mid, jid=OWNER_JID, from_me=True):
    return {"data": {"key": {"id": mid, "remoteJid": jid, "fromMe": from_me},
                     "message": {"conversation": text}, "messageTimestamp": 1762873200,
                     "pushName": "Marcelo"}}


async def invoke(graph, body, jid=OWNER_JID):
    tid = make_thread_id("secretaria", jid)
    return await graph.ainvoke({"raw": body}, config={"configurable": {"thread_id": tid}})


def inserted(deps) -> dict:
    return next((kw for v, kw in deps.tools["calendar"]._svc.events().calls if v == "insert"), {})


# ============================ P1 — the store ==============================================

async def p1_store():
    print("P1 — the store")
    p = Presets(default_minutes=45)
    check("an empty store reads as today's behaviour",
          p.meeting_minutes() == 45 and p.video_call() and p.notify_guests() and not p.places())
    check("and nothing counts as set", p.snapshot()["set"] == [])

    await p.set_many({"meeting_minutes": 30, "video_call": False, "bogus": 1})
    check("settings are written", p.meeting_minutes() == 30 and not p.video_call())
    check("an unknown key is ignored", "bogus" not in p._rows)
    check("only what he set counts as set", p.snapshot()["set"] == ["meeting_minutes", "video_call"])

    place, before = await p.save_place("Escritório", "Av. Paulista 1000")
    check("a place is saved under its own spelling", place["name"] == "Escritório" and before is None)
    check("a place is found by any spelling of its name",
          (p.place("escritorio") or {}).get("address") == "Av. Paulista 1000"
          and (p.place("  ESCRITÓRIO ") or {}).get("address") == "Av. Paulista 1000")
    check("a near miss is not a place", p.place("escritorios") is None and p.place("") is None)
    _, before = await p.save_place("escritorio", "Rua Funchal 500")
    check("saving the same name replaces its address",
          before["address"] == "Av. Paulista 1000" and len(p.places()) == 1
          and p.places()[0]["address"] == "Rua Funchal 500")
    await p.save_place("casa", "Rua Harmonia 123")
    check("places list sorted by name", [x["name"] for x in p.places()] == ["casa", "escritorio"])
    gone = await p.remove_place("Casa")
    check("a place is removed by any spelling", gone["name"] == "casa" and p.place("casa") is None)
    check("removing a missing place reports None", await p.remove_place("gym") is None)
    check("fold collapses accents, case and spaces", fold("  Minha  CASA ") == "minha casa")

    # Fail to default: a durable tier that never loaded serves today's behaviour.
    q = Presets(default_minutes=45, store=FailingStore())
    await q.refresh(force=True)
    check("a store that cannot load leaves every preset at its default",
          q.meeting_minutes() == 45 and q.video_call() and q.notify_guests())
    try:
        await q.set_many({"meeting_minutes": 20})
        raised = False
    except RuntimeError:
        raised = True
    check("a failed write raises, so the owner is told", raised)
    check("and the failed write did not land in the snapshot", q.meeting_minutes() == 45)


# ============================ P2 — writing presets into a write ===========================

async def p2_apply():
    print("P2 — presets written into a calendar write")
    create = {"task": "calendar.create", "title": "Sync", "start": START}

    check("presets off: the action is untouched", apply_presets(dict(create), None) == create)

    p = Presets(default_minutes=45)
    out = apply_presets(dict(create), p)
    check("nothing set: only the length the handler would have used anyway",
          out == {**create, "duration_min": 45}, detail=str(out))
    check("nothing set: the card is the pre-presets card",
          compose_create(out, {}) == compose_create(create, {}))

    p = await presets_with(meeting_minutes=30)
    check("standard length fills a create with no end",
          apply_presets(dict(create), p).get("duration_min") == 30)
    check("an explicit end wins",
          "duration_min" not in apply_presets({**create, "end": "2026-11-12T11:00:00-03:00"}, p))
    check("an all-day event takes no length",
          "duration_min" not in apply_presets({**create, "start": "2026-11-12", "all_day": True}, p))
    check("an update keeps its own length — no length is stamped on it",
          "duration_min" not in apply_presets(
              {"task": "calendar.update", "event_id": "e1", "start": START}, p))

    p = await presets_with(video_call=False)
    out = apply_presets(dict(create), p)
    check("video off: a meeting with nowhere to go gets no Meet", out.get("virtual") is False)
    check("video off: a meeting with a place is left alone",
          "virtual" not in apply_presets({**create, "location": "Starbucks"}, p))
    check("video off: an explicit call stands",
          apply_presets({**create, "virtual": True}, p).get("virtual") is True)
    check("video off: an all-day event is not touched",
          "virtual" not in apply_presets({**create, "start": "2026-11-12", "all_day": True}, p))
    check("video off: an update never gains or loses a call",
          "virtual" not in apply_presets({"task": "calendar.update", "event_id": "e1",
                                          "title": "x"}, p))
    p_on = Presets(default_minutes=45)
    check("video on (default): the Meet default stays with resolve_virtual",
          "virtual" not in apply_presets(dict(create), p_on))

    p = await presets_with(notify_guests=False)
    for verb in ("create", "update", "delete"):
        a = {"task": f"calendar.{verb}", "event_id": "e1", "title": "t", "start": START}
        check(f"guest emails off: {verb} is sent quietly",
              apply_presets(a, p).get("send_invites") is False)
    check("guest emails off: an explicit invite stands",
          apply_presets({**create, "send_invites": True}, p).get("send_invites") is True)
    check("guest emails on (default): nothing is stamped",
          "send_invites" not in apply_presets(dict(create), p_on))
    check("a read is never touched",
          apply_presets({"task": "calendar.find", "query": "x"}, p) == {"task": "calendar.find",
                                                                          "query": "x"})

    p = await presets_with(places={"Casa": "Rua Harmonia 123, São Paulo"})
    check("a location that IS a saved place becomes its address",
          apply_presets({**create, "location": "casa"}, p)["location"]
          == "Rua Harmonia 123, São Paulo")
    check("a place on an update too",
          apply_presets({"task": "calendar.update", "event_id": "e1", "location": "Casa"},
                        p)["location"] == "Rua Harmonia 123, São Paulo")
    check("any other location is left exactly as written",
          apply_presets({**create, "location": "Casa da Ana"}, p)["location"] == "Casa da Ana")
    p = await presets_with(video_call=False, places={"casa": "Rua Harmonia 123"})
    out = apply_presets({**create, "location": "casa"}, p)
    check("a saved place is somewhere to go, so it is not a video call",
          "virtual" not in out and out["location"] == "Rua Harmonia 123")
    check("filling twice changes nothing more (idempotent)",
          apply_presets(apply_presets(dict(create), p), p) == apply_presets(dict(create), p))


# ============================ P3 — the card and Google agree ==============================

async def p3_agreement():
    print("P3 — the card and the Google request read the same values")
    p = await presets_with(meeting_minutes=30, video_call=False, notify_guests=False)
    action = apply_presets({"task": "calendar.create", "title": "Sync", "start": START,
                            "attendees": ["ana@acme.com"]}, p)
    card = compose_create(action, {"session_lang": "en"})
    check("the card shows no video call", "Video call" not in card, detail=card)
    check("the card says the guests won't be notified", "won't be notified" in card, detail=card)

    h = GoogleCalendarService(Settings())
    h.presets = p
    h._svc = FakeGoogle()
    inputs = {k: v for k, v in action.items() if k != "task"}
    r = await h.run("create", inputs)
    kw = next(kw for v, kw in h._svc.events().calls if v == "insert")
    body = kw["body"]
    check("the write succeeded", r.get("ok"), detail=str(r.get("summary")))
    check("the event lasts the standard length",
          body["end"]["dateTime"] == "2026-11-12T10:30:00-03:00", detail=body["end"]["dateTime"])
    check("no Meet was requested",
          not (body.get("conferenceData") or {}).get("createRequest"))
    check("Google was told not to email anyone", kw["sendUpdates"] == "none")
    check("the result says so too", r["data"].get("notified") is False and not r["data"]["meet_link"])

    # A delete card follows the guest-email preset as well.
    seen = {"e1": {"event_id": "e1", "title": "Sync", "start": START, "end": START,
                   "attendees": ["ana@acme.com"]}}
    d = apply_presets({"task": "calendar.delete", "event_id": "e1"}, p)
    check("a cancellation card says the guests won't be notified",
          "won't be notified" in (compose_delete(d, {"seen_events": seen}) or ""))

    # The handler's own fallback length follows the preset when presets are attached.
    h2 = GoogleCalendarService(Settings())
    check("the handler's fallback length is the preset", (h2.presets is None
          and h2._default_minutes() == 45) and (setattr(h2, "presets", p) or h2._default_minutes() == 30))


# ============================ P4 — the setup verbs ========================================

async def p4_setup_verbs():
    print("P4 — the setup verbs")
    s = settings()
    h = RosterService(s, roster=Roster(), presets=Presets(default_minutes=45))

    menu = await h.run("menu", {})
    check("the menu offers Transcription and Meetings",
          [i["key"] for i in menu["data"]["items"]] == ["transcription", "meetings"])

    r = await h.run("list", {})
    check("the bare list carries the presets under the transcription list",
          "presets" in r["data"] and "ordinals" in r["data"])
    r = await h.run("list", {"item": "meetings"})
    check("item 'meetings' is the presets alone, with no ordinals",
          r["data"].get("item") == "meetings" and "ordinals" not in r["data"])
    r = await h.run("list", {"item": "reuniões"})
    check("the item may be named in Portuguese", r["data"].get("item") == "meetings")
    r = await h.run("list", {"item": "transcription"})
    check("item 'transcription' leaves the presets out", "presets" not in r["data"])

    r = await h.run("set_presets", {})
    check("a set with nothing in it is refused", not r["ok"] and r["error"] == "validation")
    r = await h.run("set_presets", {"meeting_minutes": 2})
    check("a two-minute meeting is refused", not r["ok"] and r["error"] == "invalid_value")
    r = await h.run("set_presets", {"meeting_minutes": True})
    check("a boolean is not a length", not r["ok"])
    r = await h.run("set_presets", {"video_call": "no"})
    check("video_call must be a boolean", not r["ok"] and r["error"] == "invalid_value")
    r = await h.run("set_presets", {"meeting_minutes": 30, "notify_guests": False})
    check("several settings in one write",
          r["ok"] and h.presets.meeting_minutes() == 30 and not h.presets.notify_guests())
    check("the write reports what changed from what",
          r["data"]["changed"] == {"meeting_minutes": {"from": 45, "to": 30},
                                   "notify_guests": {"from": True, "to": False}})

    r = await h.run("save_place", {"name": "casa", "address": ""})
    check("a place with no address is refused", not r["ok"] and r["error"] == "validation")
    r = await h.run("save_place", {"name": "x" * 41, "address": "Rua A"})
    check("an over-long name is refused", not r["ok"])
    r = await h.run("save_place", {"name": " casa ", "address": "Rua  Harmonia 123"})
    check("a place is saved with its spacing tidied",
          r["ok"] and h.presets.place("casa")["address"] == "Rua Harmonia 123"
          and not r["data"]["existed"])
    r = await h.run("save_place", {"name": "Casa", "address": "Rua Girassol 50"})
    check("saving it again is a replacement, and says so",
          r["ok"] and r["data"]["existed"] and r["data"]["before"]["address"] == "Rua Harmonia 123")
    r = await h.run("remove_place", {"name": "gym"})
    check("removing a place that isn't saved is reported, naming the saved ones",
          not r["ok"] and r["error"] == "not_found" and "Casa" in r["summary"])
    r = await h.run("remove_place", {"name": "casa"})
    check("a place is removed", r["ok"] and not h.presets.places())

    off = RosterService(s, roster=Roster(), presets=None)
    r = await off.run("set_presets", {"meeting_minutes": 30})
    check("with no presets attached the verbs fail honestly",
          not r["ok"] and r["error"] == "store_unavailable")
    menu = await off.run("menu", {})
    check("and the menu is the one-item menu", len(menu["data"]["items"]) == 1)

    from app.skills.setup import setup_resolve_gate
    for verb, inputs in (("set_presets", {"meeting_minutes": 30}),
                         ("save_place", {"name": "casa", "address": "Rua A"}),
                         ("remove_place", {"name": "casa"})):
        patched, err = setup_resolve_gate(verb, inputs, {"is_self_chat": False})
        check(f"{verb} is refused outside the self-chat",
              patched is None and (err or {}).get("error") == "not_self_chat")
        patched, err = setup_resolve_gate(verb, inputs, {"is_self_chat": True})
        check(f"{verb} passes in the self-chat, inputs untouched", patched == inputs and err is None)

    bad = RosterService(s, roster=Roster(), presets=Presets(store=FailingStore()))
    r = await bad.run("set_presets", {"meeting_minutes": 30})
    check("a database failure on a write is reported, not swallowed",
          not r["ok"] and r["error"] == "store_unavailable")


# ============================ P5 — cards and receipts =====================================

async def p5_cards():
    print("P5 — the setup cards and receipts")
    snap = (await presets_with(places={"casa": "Rua Harmonia 123"})).snapshot()
    en, pt = {"session_lang": "en", "presets": snap}, {"session_lang": "pt", "presets": snap}

    card = compose_set_presets({"meeting_minutes": 30, "video_call": False}, en)
    check("a set card says from → to for each setting",
          "Standard length: 45 min → *30 min*" in card and "Video call: on → *off*" in card,
          detail=card)
    check("and asks to save", card.endswith("Save it?"))
    card = compose_set_presets({"notify_guests": False}, pt)
    check("in Portuguese, with the noun's own agreement",
          "E-mail aos convidados: ligado → *desligado*" in card and card.endswith("Salvo?"),
          detail=card)
    card = compose_set_presets({"meeting_minutes": 45}, en)
    check("a setting already at that value is still named",
          "Standard length: *45 min*" in card, detail=card)
    check("a set with nothing in it has no card", compose_set_presets({}, en) is None)

    check("a new place's card",
          compose_save_place({"name": "office", "address": "Av. Paulista 1000"}, en)
          == "Confirm — save the place *office*: Av. Paulista 1000?")
    check("a replaced place's card says from → to",
          compose_save_place({"name": "Casa", "address": "Rua Girassol 50"}, pt)
          == "Confirmar — *Casa*: mudar Rua Harmonia 123 → *Rua Girassol 50*?")
    check("a removal card names the address it forgets",
          compose_remove_place({"name": "CASA"}, en) == "Remove the place *casa* (Rua Harmonia 123)?")

    section = fmt_presets(snap, False)
    check("the Meetings section lists the settings and the places",
          "Standard length - 45 min" in section and "Video call - on" in section
          and "casa - Rua Harmonia 123" in section, detail=section)
    check("places are not numbered", "1." not in section)
    section = fmt_presets((await presets_with()).snapshot(), True)
    check("no places, said plainly (pt)", "Nenhum local salvo ainda." in section, detail=section)

    lst = fmt_list([{"data": {"scopes": [], "contacts": [], "groups": [], "presets": snap}}], en)
    check("the full list shows Transcription then Meetings",
          lst.index("Transcription") < lst.index("*Meetings*"), detail=lst)
    lst = fmt_list([{"data": {"item": "meetings", "presets": snap}}], en)
    check("the meetings list shows only Meetings", "Transcription" not in lst and "*Meetings*" in lst)

    check("the set receipt",
          fmt_set_presets([{"data": {"changed": {"meeting_minutes": {"from": 45, "to": 30}}}}], en)
          == "Done. Standard length → 30 min.")
    check("the place receipt",
          fmt_save_place([{"data": {"place": {"name": "casa", "address": "Rua A 1"}}}], pt)
          == "Pronto. casa → Rua A 1.")
    check("the removal receipt",
          fmt_remove_place([{"data": {"place": {"name": "casa"}}}], en)
          == "Removed. casa is off your places.")

    menu = fmt_menu([{"data": {"items": [{"title": "Transcription", "summary": "a"},
                                         {"title": "Meetings", "summary": "b"}]}}], en)
    check("a two-item menu says to pick a number", "say the number" in menu and "2. *Meetings*" in menu)


# ============================ P6 — off is the pre-feature build ===========================

async def p6_gating():
    print("P6 — PRESETS_ENABLED=false is the pre-feature build")
    off, on = settings(presets_enabled=False), settings()

    def verbs(s):
        sc = output_schema_for("setup", settings=s)
        return {b["properties"]["task"]["const"] for b in sc["properties"]["actions"]["items"]["anyOf"]}

    check("off: no preset verbs in the schema",
          not {"setup.set_presets", "setup.save_place", "setup.remove_place"} & verbs(off))
    check("on: all three are there",
          {"setup.set_presets", "setup.save_place", "setup.remove_place"} <= verbs(on))
    sc = output_schema_for("setup", settings=on)
    check("on: the schema stays under both caps",
          count_unions(sc) <= 16 and count_optionals(sc) <= 24,
          detail=f"{count_unions(sc)} unions / {count_optionals(sc)} optionals")
    check("the calendar schema is unchanged by presets",
          output_schema_for("calendar", settings=on) == output_schema_for("calendar", settings=off))

    check("off: the setup prompt says nothing about meetings",
          "**Meetings**" not in system_prompt_for("setup", off))
    check("on: it does", "**Meetings**" in system_prompt_for("setup", on))
    check("off: the classifier is not told about presets",
          "meeting presets" not in _classify_system("Marcelo", presets=False))
    check("on: it is", "meeting presets" in _classify_system("Marcelo", presets=True))

    check("off: the calendar is not told about defaults",
          "HIS DEFAULTS" not in system_prompt_for("calendar", off))
    check("on: it is told it cannot change one, and where they are changed",
          "you cannot change a default" in system_prompt_for("calendar", on))

    deps, _, _ = build(off)
    check("off: no presets object is built", deps.presets is None)
    block, _ = context_block_for("calendar", {"text": "x"},
                                 {"directory": None, "settings": off, "presets": None})
    check("off: the calendar gets no block", block is None)


# ============================ P7 — the calendar's prompt block ============================

async def p7_block():
    print("P7 — the calendar's presets block")
    check("nothing set: no block, so the prompt is the pre-presets one",
          presets_block(Presets(default_minutes=45), "Marcelo") is None)
    p = await presets_with(meeting_minutes=30, video_call=False, notify_guests=False,
                           places={"casa": "Rua Harmonia 123"})
    block = presets_block(p, "Marcelo") or ""
    check("the length is stated", "30 minutes" in block)
    check("video off says when to send virtual: true", '"virtual": true only when' in block)
    check("guest emails off says when to send send_invites: true",
          '"send_invites": true only when' in block)
    check("the places are listed with their addresses", "casa: Rua Harmonia 123" in block)
    p2 = await presets_with(video_call=True, notify_guests=True)
    check("a setting set back to its default adds no line",
          presets_block(p2, "Marcelo") is None)

    s = settings()
    block, _ = context_block_for("calendar", {"text": "marca reunião"},
                                 {"directory": None, "settings": s, "presets": p})
    check("the block reaches the calendar with no address book", "casa: Rua Harmonia 123" in (block or ""))
    prompt = system_prompt_for("calendar", s, context_block=block)
    check("and sits inside the calendar prompt", "Marcelo's presets" in prompt)


# ============================ P8 — a setup loop holds its answers =========================

async def p8_router():
    print("P8 — the router")
    s = settings()
    held = {"text": "reuniões de 30 minutos", "loop_domain": "setup", "loop_opened": False,
            "is_self_chat": True}
    d, how = await route_domain(held, s, reasoner=StubReasoner())
    check("inside setup, meeting vocabulary is a preset, not a booking",
          d == "setup" and how == "loop", detail=f"{d}/{how}")
    d, how = await route_domain({**held, "text": "não mandar convite"}, s, reasoner=StubReasoner())
    check("'convite' too", d == "setup")
    d, how = await route_domain({**held, "loop_opened": True,
                                 "text": "@lisa marca reunião amanhã 10h"}, s,
                                reasoner=StubReasoner())
    check("a fresh tag still re-decides — that is a booking", d == "calendar" and how == "matcher")
    d, how = await route_domain({"text": "agenda uma reuniao", "loop_domain": "web",
                                 "loop_opened": False}, s, reasoner=StubReasoner())
    check("a skill that does not hold its loop still loses it to a matcher", d == "calendar")
    d, _ = await route_domain({**held, "is_self_chat": False}, s, reasoner=StubReasoner())
    check("outside the self-chat setup is never held", d != "setup")
    check("only setup holds its loop", [n for n, sk in SKILLS.items() if sk.holds_loop] == ["setup"])

    # Called by name, setup beats the calendar's vocabulary in what follows (found live: "@lisa
    # setup não quero que os convidados recebam e-mail" went to the calendar, which promised).
    me = {"is_self_chat": True, "from_me": True, "loop_opened": True}
    for text in ("@lisa setup não quero que os convidados recebam e-mail por padrão",
                 "@lisa configurações: reuniões de 30 minutos",
                 "@lisa, Setup: salva o endereço casa",
                 "@lisa presets sem google meet"):
        d, how = await route_domain({**me, "text": text}, s, reasoner=StubReasoner())
        check(f"by name -> setup: {text!r}", d == "setup" and how == "invoked", detail=f"{d}/{how}")
    for text in ("@lisa marca uma reunião pra configurar o servidor amanhã",
                 "@lisa ajuste a reunião de amanhã pra 15h",
                 "@lisa marca reunião amanhã, depois vejo o setup"):
        d, how = await route_domain({**me, "text": text}, s, reasoner=StubReasoner())
        check(f"not by name -> calendar: {text!r}", d == "calendar", detail=f"{d}/{how}")
    d, how = await route_domain({**me, "is_self_chat": False,
                                 "text": "@lisa setup não quero e-mail pros convidados"}, s,
                                reasoner=StubReasoner())
    check("by name outside the self-chat is still not setup", d != "setup", detail=f"{d}/{how}")


# ============================ P9 — through the graph ======================================

async def p9_graph():
    print("P9 — through the graph")
    s = settings()
    reasoner = StubReasoner([
        # self-chat: set two presets in one action, then save a place
        {"actions": [{"task": "setup.set_presets", "meeting_minutes": 30, "video_call": False,
                      "confirmed": False}]},
        {"actions": [{"task": "setup.save_place", "name": "casa",
                      "address": "Rua Harmonia 123", "confirmed": False}]},
        # Ana's chat: a booking that names nowhere, then one at casa
        {"actions": [{"task": "calendar.create", "title": "Sync", "start": START,
                      "attendees": ["ana@acme.com"]}]},
    ])
    deps, graph, ev = build(s, reasoner=reasoner)
    ev.history[OWNER_JID] = []

    st = await invoke(graph, text_upsert("@lisa setup reuniões de 30 min e sem vídeo", mid="s1"))
    reply = st.get("reply") or ""
    check("the change is held for a yes, not written",
          bool(st.get("pending_action")) and deps.presets.meeting_minutes() == 45)
    check("the card shows both changes", "45 min → *30 min*" in reply and "→ *" in reply,
          detail=reply)
    st = await invoke(graph, text_upsert("sim", mid="s2"))
    check("the yes writes both",
          deps.presets.meeting_minutes() == 30 and deps.presets.video_call() is False)
    check("the receipt is composed in code",
          "Pronto. Duração padrão → 30 min; Chamada de vídeo → desligada." in (st.get("reply") or ""),
          detail=st.get("reply"))

    st = await invoke(graph, text_upsert("salva casa: Rua Harmonia 123", mid="s3"))
    check("an untagged answer inside setup stays in setup", st.get("domain") == "setup")
    st = await invoke(graph, text_upsert("salva", mid="s4"))
    check("the card's own word is a yes, and the place is saved",
          (deps.presets.place("casa") or {}).get("address") == "Rua Harmonia 123",
          detail=str(deps.presets.places()))

    # A booking in another chat now follows the presets.
    ev.history[ANA_JID] = []
    st = await invoke(graph, text_upsert("@lisa schedule a meeting with Ana tomorrow 10am",
                                         mid="a1", jid=ANA_JID), jid=ANA_JID)
    card = st.get("reply") or ""
    check("the booking card shows no video call", "Video call" not in card, detail=card)
    check("the held booking carries the preset length",
          (st.get("pending_action") or {}).get("duration_min") == 30)
    check("the booking prompt carried the presets block",
          "casa: Rua Harmonia 123" in reasoner.calls[-1]["system"])
    st = await invoke(graph, text_upsert("yes", mid="a2", jid=ANA_JID), jid=ANA_JID)
    body = inserted(deps).get("body") or {}
    check("the event lasts 30 minutes",
          (body.get("end") or {}).get("dateTime") == "2026-11-12T10:30:00-03:00",
          detail=str(body.get("end")))
    check("and has no Meet", not (body.get("conferenceData") or {}).get("createRequest"))
    check("guests are still emailed — that preset was not changed",
          inserted(deps).get("sendUpdates") == "all")

    # Changing a preset after the card cannot change what was approved.
    reasoner2 = StubReasoner([
        {"actions": [{"task": "calendar.create", "title": "Lunch", "start": START,
                      "location": "casa"}]},
    ])
    deps2, graph2, ev2 = build(s, reasoner=reasoner2,
                               presets=await presets_with(places={"casa": "Rua Harmonia 123"}))
    ev2.history[ANA_JID] = []
    st = await invoke(graph2, text_upsert("@lisa schedule lunch at casa tomorrow 10am",
                                          mid="b1", jid=ANA_JID), jid=ANA_JID)
    check("a saved place on the card is its address",
          "Rua Harmonia 123" in (st.get("reply") or ""), detail=st.get("reply"))
    await deps2.presets.save_place("casa", "Rua Nova 9")
    await invoke(graph2, text_upsert("sim", mid="b2", jid=ANA_JID), jid=ANA_JID)
    body = inserted(deps2).get("body") or {}
    check("the event goes to the address he approved, not the one saved since",
          body.get("location") == "Rua Harmonia 123", detail=str(body.get("location")))


# ============================ P10 — the durable tier ======================================

async def p10_postgres():
    dsn = os.environ.get("MARY_TEST_DATABASE_URL")
    if not dsn:
        print("P10 — real Postgres — SKIPPED (set MARY_TEST_DATABASE_URL to run)")
        return
    print("P10 — real Postgres (throwaway schema)")
    import psycopg

    from app.presets import PresetStore

    schema = "mary_presets_selftest"
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
        await c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    store = PresetStore(dsn, schema=schema)
    await store.open()
    try:
        p = Presets(default_minutes=45, store=store)
        await p.refresh(force=True)
        check("a fresh table reads as the defaults",
              p.meeting_minutes() == 45 and p.video_call() and not p.places())
        await p.set_many({"meeting_minutes": 25, "notify_guests": False})
        await p.save_place("Escritório", "Av. Paulista 1000")
        await p.save_place("casa", "Rua Harmonia 123")
        await p.remove_place("CASA")

        # A second instance — another worker, or the next boot — reads what the first wrote.
        q = Presets(default_minutes=45, store=store)
        await q.refresh(force=True)
        check("settings survive a restart, with their types",
              q.meeting_minutes() == 25 and q.notify_guests() is False and q.video_call() is True)
        check("places survive with their spelling",
              q.places() == [{"name": "Escritório", "address": "Av. Paulista 1000"}],
              detail=str(q.places()))
        check("only what was set counts as set",
              q.snapshot()["set"] == ["meeting_minutes", "notify_guests"])
        await q.set_many({"meeting_minutes": 50})
        await p.refresh(force=True)
        check("a write by one instance reaches the other on refresh", p.meeting_minutes() == 50)
        await store.open()  # opening twice is harmless: the DDL is idempotent
        check("the DDL is idempotent", (await store.load()) is not None)
    finally:
        await store.aclose()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
            await c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


async def main() -> int:
    for part in (p1_store, p2_apply, p3_agreement, p4_setup_verbs, p5_cards, p6_gating,
                 p7_block, p8_router, p9_graph, p10_postgres):
        await part()
        print()
    print(f"{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
