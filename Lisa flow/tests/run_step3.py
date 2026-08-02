"""Step-3 (P1) verification — the tool framework skeleton.

Asserts the registry fans out correctly and the enforced-JSON output schema is well-formed
and stays under Anthropic's 16 union/array cap. No network, no Postgres, no Anthropic key,
no Google. This is the offline guard that the schema can't regress into the silent-Lisa bug.

    cd "Lisa flow" && python tests/run_step3.py
Exits non-zero on the first failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import Settings  # noqa: E402
from app.deps import Deps, build_deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.reasoning.anthropic import AnthropicReasoner  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.tools.registry import (  # noqa: E402
    TOOLS,
    build_output_schema,
    build_task_prompts,
    build_tools_prompt,
    confirm_first,
    count_optionals,
    count_unions,
    local_handlers,
)
from app.trace import build_trace  # noqa: E402

OWNER_JID = "5511976001033@s.whatsapp.net"

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    tail = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{tail}")
    _checks["pass" if cond else "fail"] += 1


def _branches(schema: dict) -> list[dict]:
    return schema["properties"]["actions"]["items"]["anyOf"]


def _task_const(branch: dict) -> str:
    return branch["properties"]["task"]["const"]


def unit_checks() -> None:
    print("Step-3 P1 — tool framework skeleton")
    schema = build_output_schema()

    # --- output schema shape ---
    top = set(schema.get("required", []))
    check("top-level required = reasoning/state/message/lang/actions/workflow",
          top == {"reasoning", "state", "message", "lang", "actions", "workflow"},
          detail=str(sorted(top)))
    check("actions is an array whose items is a single anyOf",
          schema["properties"]["actions"]["type"] == "array"
          and "anyOf" in schema["properties"]["actions"]["items"])
    check("message and workflow are the nullable fields",
          schema["properties"]["message"]["anyOf"][-1] == {"type": "null"}
          and schema["properties"]["workflow"]["anyOf"][-1] == {"type": "null"})

    # --- the verb branches ---
    tasks = {_task_const(b) for b in _branches(schema)}
    check("actions anyOf exposes exactly the 5 calendar tasks",
          tasks == {f"calendar.{v}" for v in ("create", "list", "find", "update", "delete")},
          detail=str(sorted(tasks)))

    req = {_task_const(b): set(b["required"]) for b in _branches(schema)}
    check("create requires task+title+start", req["calendar.create"] == {"task", "title", "start"})
    check("list requires only task", req["calendar.list"] == {"task"})
    check("find requires only task", req["calendar.find"] == {"task"})
    check("update requires task+event_id", req["calendar.update"] == {"task", "event_id"})
    check("delete requires task+event_id", req["calendar.delete"] == {"task", "event_id"})

    # optionals must be present-but-not-required (plain-typed), never anyOf:[T,null]
    create = next(b for b in _branches(schema) if _task_const(b) == "calendar.create")
    end_field = create["properties"]["end"]
    check("optional 'end' is plain-typed, not null-unioned",
          end_field == {"type": "string"}, detail=str(end_field))
    check("every branch sets additionalProperties:false",
          all(b.get("additionalProperties") is False for b in _branches(schema)))

    # --- the two Anthropic schema-compilation caps (outage backstops) ---
    n = count_unions(schema)
    check("schema union/array count <= 16", n <= 16, detail=f"count={n}")
    opt = count_optionals(schema)
    # Anthropic rejects > 24 optional params (grammar compilation). Keep real margin.
    check("schema optional-param count <= 24", opt <= 24, detail=f"count={opt}")

    # --- prompt fan-out ---
    tp = build_tools_prompt(owner_name="Marcelo")
    check("tools_prompt lists calendar as run-via-actions with its verbs",
          "calendar (run via actions)" in tp and "create, list, find, update, delete" in tp)
    check("tools_prompt substitutes the owner name", "Marcelo" in tp, detail=repr(tp[:60]))

    task = build_task_prompts(owner_name="Marcelo")
    check("task_prompts renders the calendar guidance", "Calendar actions" in task)
    check("task_prompts substitutes the owner name (no stray {owner_name})",
          "Marcelo" in task and "{owner_name}" not in task)
    check("empty registry -> empty task_prompts (no dangling header)",
          build_task_prompts(tools={}, owner_name="X") == "")

    # --- runtime fan-out ---
    settings = Settings()
    handlers = local_handlers(tools=TOOLS, settings=settings)
    check("local_handlers builds a 'calendar' handler", "calendar" in handlers)
    cf = confirm_first()
    check("confirm_first = {create,update,delete} for calendar",
          cf.get("calendar") == {"create", "update", "delete"}, detail=str(cf))

    # --- reasoner injection ---
    r = AnthropicReasoner(settings, output_schema=schema, mcp_servers=[])
    check("reasoner stores the injected output_schema", r.output_schema is schema)
    r2 = AnthropicReasoner(settings)
    check("reasoner falls back to building the schema from the registry",
          set(r2.output_schema.get("required", [])) == top)

    # --- deps wiring ---
    deps = build_deps(settings)
    check("deps carries the calendar handler", "calendar" in (deps.tools or {}))
    check("deps carries tools_prompt + task_prompts",
          bool(deps.tools_prompt) and bool(deps.task_prompts))
    check("deps.reasoner got the built schema",
          count_unions(deps.reasoner.output_schema) <= 16)


# ============================ P2 — the tool loop (graph-driven) ============================

class StubReasoner:
    """Scripted reasoner. Each script entry is one respond() return; missing keys default."""

    def __init__(self) -> None:
        self.calls: list = []
        self.script: list = []

    async def respond(self, *, system, messages):
        self.calls.append({"system": system, "messages": list(messages)})
        base = {"state": "keep_listening", "message": None, "lang": "en", "actions": [],
                "workflow": None, "usage": {"input": 1, "output": 1},
                "provider_request_id": "req_stub", "stop_reason": "end_turn",
                "tool_calls": [], "error_category": "none"}
        if self.script:
            base.update(self.script.pop(0))
        return base


class StubCalendar:
    """Records calls; returns per-verb scripted ActionResults (default ok)."""

    def __init__(self) -> None:
        self.calls: list = []
        self.responses: dict = {}

    async def run(self, verb, inputs):
        self.calls.append((verb, dict(inputs)))
        r = self.responses.get(verb)
        if callable(r):
            return r(inputs)
        return r if r is not None else {"ok": True, "summary": f"{verb} ok", "data": {}}

    def n(self, verb: str) -> int:
        return sum(1 for v, _ in self.calls if v == verb)


class FakeEvolution:
    instance = "secretaria"

    def __init__(self, history=None) -> None:
        self.sent: list = []
        self.history = history or []

    async def send_text(self, number, text):
        mid = f"echo{len(self.sent)}"
        self.sent.append((number, text))
        return mid

    async def fetch_history(self, jid):
        return list(self.history)


def _upsert(text, *, from_me=True, mid="m1", jid=OWNER_JID):
    return {"data": {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": mid},
        "message": {"conversation": text},
        "messageTimestamp": 1730000000, "pushName": "Tester",
    }}


def make_toolenv(history=None, max_tool_actions=4):
    # NB: mary_trigger_tag only binds via its env alias (MARY_TRIGGER_TAG), so we use the
    # default "@mary" trigger here; the live Lisa sets @mary through the env alias.
    settings = Settings(evolution_apikey="x", loop_ttl_seconds=60,
                        context_window_messages=30, max_tool_actions=max_tool_actions)
    evo = FakeEvolution(history)
    stub = StubReasoner()
    cal = StubCalendar()
    deps = Deps(settings=settings, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=stub,
                redis=None, tools={"calendar": cal},
                confirm_first={"calendar": {"create", "update", "delete"}},
                tools_prompt="", task_prompts="")
    return deps, evo, stub, cal, build_graph(deps, MemorySaver())


async def _invoke(graph, body, jid=OWNER_JID):
    config = {"configurable": {"thread_id": make_thread_id("secretaria", jid)}}
    return await graph.ainvoke({"raw": body}, config=config)


async def graph_checks() -> None:
    print("\nStep-3 P2 — the tool loop")

    # 1. a successful write reads back so it confirms FROM the result (never announced before).
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [
        {"message": None, "actions": [
            {"task": "calendar.create", "title": "Call", "start": "2026-08-05T15:00:00-03:00",
             "confirmed": True}]},
        {"message": "Booked your 3pm."},
    ]
    await _invoke(graph, _upsert("@mary book a call at 3pm", mid="c1"))
    check("[create] handler ran exactly once", cal.n("create") == 1)
    check("[create] the write reads back so it can confirm (two reason passes)",
          len(stub.calls) == 2)
    check("[create] confirmation sent from the result", len(evo.sent) == 1
          and "Booked" in evo.sent[-1][1])
    check("[create] execute != close — window stays open", deps.sessions.is_open(OWNER_JID))

    # 2. read (find) -> read-back -> reply.
    deps, evo, stub, cal, graph = make_toolenv()
    cal.responses["find"] = {"ok": True, "summary": "1 match: dentist Fri 15:00 [id=E1]",
                             "data": {"items": [{"event_id": "E1", "title": "dentist"}]}}
    stub.script = [
        {"message": None, "actions": [{"task": "calendar.find", "query": "dentist"}]},
        {"message": "Found it — dentist Friday 15:00."},
    ]
    st = await _invoke(graph, _upsert("@mary when is my dentist?", mid="f1"))
    check("[find] handler ran", cal.n("find") == 1)
    check("[find] read triggered a read-back (two reason passes)", len(stub.calls) == 2)
    check("[find] reply reflects the observation", "Found it" in evo.sent[-1][1])
    check("[find] event id remembered for later edits", "E1" in (st.get("seen_event_ids") or []))

    # 3. failure -> read-back -> honest; no false "done" sent before the result.
    deps, evo, stub, cal, graph = make_toolenv()
    cal.responses["create"] = {"ok": False, "error": "auth", "summary": "auth error"}
    stub.script = [
        {"message": "Done!", "actions": [
            {"task": "calendar.create", "title": "X", "start": "2026-08-05T09:00:00-03:00",
             "confirmed": True}]},
        {"message": "Sorry — I couldn't create it (auth error)."},
    ]
    await _invoke(graph, _upsert("@mary create X", mid="e1"))
    check("[fail] read-back happened on failure", len(stub.calls) == 2)
    check("[fail] the premature 'Done!' was NOT sent", all("Done!" not in t for _, t in evo.sent))
    check("[fail] honest error is what got sent", "couldn't create" in evo.sent[-1][1])

    # 4. confirm gate: an unconfirmed write is blocked before the handler.
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [
        {"message": "Creating.", "actions": [
            {"task": "calendar.create", "title": "Y", "start": "2026-08-05T10:00:00-03:00"}]},
        {"message": "Want me to create Y at 10:00?"},
    ]
    await _invoke(graph, _upsert("@mary put Y at 10", mid="g1"))
    check("[confirm-gate] handler NOT called without confirmation", cal.n("create") == 0)
    check("[confirm-gate] bounced back to ask", len(stub.calls) == 2 and "Want me" in evo.sent[-1][1])

    # 5. resolved-id gate: update on an unseen id is blocked (no invented ids reach the API).
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [
        {"message": "Moving it.", "actions": [
            {"task": "calendar.update", "event_id": "GHOST", "confirmed": True,
             "start": "2026-08-06T15:00:00-03:00"}]},
        {"message": "Let me find that event first."},
    ]
    await _invoke(graph, _upsert("@mary move my meeting", mid="u1"))
    check("[id-gate] update NOT called on an unresolved id", cal.n("update") == 0)
    check("[id-gate] bounced back to search first", len(stub.calls) == 2)

    # 6. find -> update: a resolved id passes the gate and the write runs.
    deps, evo, stub, cal, graph = make_toolenv()
    cal.responses["find"] = {"ok": True, "summary": "1 match [id=E9]",
                             "data": {"items": [{"event_id": "E9"}]}}
    stub.script = [
        {"message": None, "actions": [{"task": "calendar.find", "query": "review"}]},
        {"message": None, "actions": [
            {"task": "calendar.update", "event_id": "E9", "confirmed": True,
             "start": "2026-08-07T15:00:00-03:00"}]},
        {"message": "Moved it to Friday."},
    ]
    await _invoke(graph, _upsert("@mary move the review to Friday", mid="fu1"))
    check("[find->update] update ran on the resolved id",
          cal.n("find") == 1 and cal.n("update") == 1)
    check("[find->update] the id passed to update was the found one",
          next((i["event_id"] for v, i in cal.calls if v == "update"), None) == "E9")
    check("[find->update] final confirmation sent from the result", "Moved it" in evo.sent[-1][1])

    # 7. workflow persists across turns, then is cleared by a fresh tag (anti-delirium).
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [{"message": "What time works?", "workflow": {
        "task": "calendar.create", "known_inputs": [{"field": "title", "value": "lunch"}],
        "open_questions": [{"field": "start", "reason": "no time given"}]}}]
    st = await _invoke(graph, _upsert("@mary set up lunch with Ana", mid="w1"))
    check("[workflow] gather ran no action", cal.calls == [])
    check("[workflow] the goal is remembered in state",
          (st.get("workflow") or {}).get("task") == "calendar.create")
    stub.script = [{"message": "Sure."}]
    st = await _invoke(graph, _upsert("@mary actually, what's the weather?", mid="w2"))
    check("[workflow] a fresh tag wipes the goal (no cross-loop leak)", st.get("workflow") is None)
    check("[workflow] a fresh tag wipes remembered ids too", (st.get("seen_event_ids") or []) == [])

    # 8. the read-back loop is bounded by max_tool_actions.
    deps, evo, stub, cal, graph = make_toolenv(max_tool_actions=2)
    cal.responses["find"] = {"ok": True, "summary": "still looking", "data": {}}
    stub.script = [
        {"actions": [{"task": "calendar.find", "query": "a"}]},
        {"actions": [{"task": "calendar.find", "query": "b"}]},
        {"actions": [{"task": "calendar.find", "query": "c"}]},
        {"message": "stopping"},
    ]
    await _invoke(graph, _upsert("@mary find stuff", mid="b1"))
    check("[bound] read-back stopped at max_tool_actions", cal.n("find") == 2)


# ===================== P3 — the Google Calendar handler (fake service) =====================

class _GErr(Exception):
    """Mimic a googleapiclient HttpError carrying resp.status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.resp = type("R", (), {"status": status})()


class _Req:
    def __init__(self, result=None, raises=None) -> None:
        self._r, self._e = result, raises

    def execute(self):
        if self._e:
            raise self._e
        return self._r


class _Events:
    def __init__(self, store: dict, raise_on: dict | None = None) -> None:
        self.store = store
        self.raise_on = raise_on or {}
        self.calls: list = []

    def list(self, **kw):
        self.calls.append(("list", kw))
        items = list(self.store["items"])
        q = kw.get("q")
        if q:
            def hay(e):
                return (e.get("summary", "") + " " + (e.get("location") or "") + " "
                        + " ".join(a.get("email", "") for a in e.get("attendees") or [])).lower()
            items = [e for e in items if q.lower() in hay(e)]
        return _Req({"items": items})

    def insert(self, **kw):
        self.calls.append(("insert", kw))
        if "insert" in self.raise_on:
            return _Req(raises=self.raise_on["insert"])
        ev = dict(kw["body"])
        ev["id"] = "NEW1"
        ev["htmlLink"] = "http://cal/NEW1"
        if kw.get("conferenceDataVersion"):
            ev["hangoutLink"] = "http://meet.google.com/abc-defg-hij"
        self.store["items"].append(ev)
        return _Req(ev)

    def get(self, **kw):
        self.calls.append(("get", kw))
        if "get" in self.raise_on:
            return _Req(raises=self.raise_on["get"])
        for e in self.store["items"]:
            if e.get("id") == kw["eventId"]:
                return _Req(e)
        return _Req(raises=_GErr(404))

    def patch(self, **kw):
        self.calls.append(("patch", kw))
        for e in self.store["items"]:
            if e.get("id") == kw["eventId"]:
                e.update(kw["body"])
                return _Req(e)
        return _Req(raises=_GErr(404))

    def delete(self, **kw):
        self.calls.append(("delete", kw))
        self.store["items"] = [e for e in self.store["items"] if e.get("id") != kw["eventId"]]
        return _Req({})


class FakeGoogle:
    def __init__(self, items=None, raise_on=None) -> None:
        self.store = {"items": list(items or [])}
        self._events = _Events(self.store, raise_on)

    def events(self):
        return self._events


def _ev(eid, summary, start, end, attendees=None, location=None):
    return {"id": eid, "summary": summary,
            "start": {"dateTime": start}, "end": {"dateTime": end},
            "attendees": [{"email": a} for a in attendees or []],
            "location": location}


def _cal_handler(items=None, raise_on=None):
    from app.tools.calendar import GoogleCalendarService
    h = GoogleCalendarService(Settings())
    h._svc = FakeGoogle(items, raise_on)
    return h


async def calendar_checks() -> None:
    print("\nStep-3 P3 — Google Calendar handler (fake service)")
    tz = Settings().calendar_timezone

    # create: end defaults to start + 45; tz set; returns id.
    h = _cal_handler()
    r = await h.run("create", {"title": "Call Ana", "start": "2026-08-05T15:00:00-03:00",
                               "confirmed": True})
    body = next(kw["body"] for v, kw in h._svc.events().calls if v == "insert")
    check("[create] ok + event_id", r.get("ok") and r["data"]["event_id"] == "NEW1")
    check("[create] summary == 'Call Ana', tz stamped",
          body["summary"] == "Call Ana" and body["start"]["timeZone"] == tz)
    check("[create] end defaulted to start + 45min",
          body["end"]["dateTime"] == "2026-08-05T15:45:00-03:00")

    # create virtual: video wins (no location) + Meet link attached.
    h = _cal_handler()
    r = await h.run("create", {"title": "Sync", "start": "2026-08-05T09:00:00-03:00",
                               "virtual": True, "location": "ignored", "confirmed": True})
    ins = next(kw for v, kw in h._svc.events().calls if v == "insert")
    check("[create] virtual drops location + requests Meet",
          ins["body"]["location"] is None and "conferenceData" in ins["body"]
          and ins.get("conferenceDataVersion") == 1)
    check("[create] Meet link surfaced in result", "meet.google.com" in (r["data"]["meet_link"] or ""))

    # create with attendees + send_invites False -> sendUpdates none.
    h = _cal_handler()
    r = await h.run("create", {"title": "1:1", "start": "2026-08-05T11:00:00-03:00",
                               "attendees": ["ana@x.com"], "send_invites": False,
                               "confirmed": True})
    ins = next(kw for v, kw in h._svc.events().calls if v == "insert")
    check("[create] attendees set + invites suppressed",
          ins["body"]["attendees"] == [{"email": "ana@x.com"}] and ins["sendUpdates"] == "none")
    check("[create] summary notes the invite count", "invited 1" in r["summary"])

    # create validation.
    r = await _cal_handler().run("create", {"start": "2026-08-05T15:00:00-03:00"})
    check("[create] missing title -> validation error", r["ok"] is False and r["error"] == "validation")

    # list: tz-aware timeMin (never naive -> Google 400).
    import datetime as _dt
    h = _cal_handler([_ev("L1", "Standup", "2026-08-05T09:00:00-03:00", "2026-08-05T09:15:00-03:00")])
    r = await h.run("list", {})
    lm = next(kw["timeMin"] for v, kw in h._svc.events().calls if v == "list")
    check("[list] timeMin is tz-aware", _dt.datetime.fromisoformat(lm).tzinfo is not None)
    check("[list] returns items with ids in the summary",
          r["data"]["items"][0]["event_id"] == "L1" and "[id=L1]" in r["summary"])

    # find: full-text resolves the right event.
    seed = [
        _ev("D1", "Dentist appointment", "2026-08-07T15:00:00-03:00", "2026-08-07T16:00:00-03:00"),
        _ev("T1", "Team standup", "2026-08-05T09:00:00-03:00", "2026-08-05T09:15:00-03:00"),
        _ev("P1", "Lunch", "2026-08-06T12:00:00-03:00", "2026-08-06T13:00:00-03:00",
            attendees=["paulo@x.com"]),
    ]
    r = await _cal_handler(seed).run("find", {"query": "dentist"})
    check("[find] full-text resolves the dentist event",
          r["ok"] and r["data"]["items"][0]["event_id"] == "D1")
    r = await _cal_handler(seed).run("find", {"query": "lunch", "attendee": "paulo@x.com"})
    check("[find] person-anchored resolves the lunch", r["data"]["items"][0]["event_id"] == "P1")
    # fuzzy fallback: a typo the substring pass misses, ranking still surfaces it.
    r = await _cal_handler(seed).run("find", {"query": "dentst"})
    check("[find] fuzzy fallback tolerates a typo",
          r["ok"] and r["data"]["items"][0]["event_id"] == "D1")
    r = await _cal_handler(seed).run("find", {"query": "nonexistent-zzz"})
    check("[find] no match -> empty, still ok", r["ok"] and r["data"]["items"] == [])

    # update: reschedule with only a new start preserves the original 60min duration.
    h = _cal_handler([_ev("E1", "Review", "2026-08-06T15:00:00-03:00", "2026-08-06T16:00:00-03:00")])
    r = await h.run("update", {"event_id": "E1", "start": "2026-08-06T18:00:00-03:00",
                               "confirmed": True})
    patch = next(kw["body"] for v, kw in h._svc.events().calls if v == "patch")
    check("[update] get-then-patch ran", any(v == "get" for v, _ in h._svc.events().calls)
          and r["ok"])
    check("[update] original duration preserved (end = start + 60)",
          patch["end"]["dateTime"] == "2026-08-06T19:00:00-03:00")
    r = await _cal_handler().run("update", {"start": "2026-08-06T18:00:00-03:00"})
    check("[update] missing event_id -> validation", r["ok"] is False and r["error"] == "validation")

    # delete: removes the event, notifies attendees.
    h = _cal_handler([_ev("E1", "Review", "2026-08-06T15:00:00-03:00", "2026-08-06T16:00:00-03:00")])
    r = await h.run("delete", {"event_id": "E1", "confirmed": True})
    dkw = next(kw for v, kw in h._svc.events().calls if v == "delete")
    check("[delete] event removed", r["ok"] and h._svc.store["items"] == [])
    check("[delete] attendees notified (sendUpdates=all)", dkw["sendUpdates"] == "all")

    # error classification.
    r = await _cal_handler(raise_on={"insert": _GErr(403)}).run(
        "create", {"title": "X", "start": "2026-08-05T15:00:00-03:00", "confirmed": True})
    check("[error] 403 -> auth", r["ok"] is False and r["error"] == "auth")
    r = await _cal_handler(raise_on={"get": _GErr(404)}).run(
        "update", {"event_id": "GONE", "start": "2026-08-05T15:00:00-03:00", "confirmed": True})
    check("[error] 404 -> not_found", r["ok"] is False and r["error"] == "not_found")
    r = await _cal_handler().run("teleport", {})
    check("[error] unknown verb handled", r["ok"] is False and r["error"] == "unknown_verb")


def _finish() -> None:
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    unit_checks()
    asyncio.run(graph_checks())
    asyncio.run(calendar_checks())
    _finish()
