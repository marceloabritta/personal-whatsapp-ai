"""Step-3 verification — the skills framework + the programmatic orchestrator.

Asserts the per-domain fan-out is well-formed (each skill gets its OWN enforced-JSON schema,
under Anthropic's 16 union / 24 optional caps), the hybrid router picks domains correctly, and
the skill-owned confirm/render policies drive the tool loop through the graph. No network, no
Postgres, no Anthropic key, no Google.

    cd "Mary flow" && python tests/run_step3.py
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
from app.skills import (  # noqa: E402
    SKILLS,
    confirm_policies,
    count_optionals,
    count_unions,
    handlers,
    has_actions,
    output_schema_for,
    render_policies,
    server_tools_for,
    system_prompt_for,
)
from app.skills.calendar import calendar_matcher  # noqa: E402
from app.skills.router import route_domain  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
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


# ============================ P1 — per-domain schema + prompt fan-out ======================

def unit_checks() -> None:
    print("Step-3 P1 — per-domain skills fan-out")
    settings = Settings()

    # --- calendar: the local-action schema (base + actions + workflow) ---
    cal = output_schema_for("calendar")
    top = set(cal.get("required", []))
    check("calendar top-level = reasoning/state/message/lang/actions/workflow",
          top == {"reasoning", "state", "message", "lang", "actions", "workflow"},
          detail=str(sorted(top)))
    check("calendar actions is an array whose items is a single anyOf",
          cal["properties"]["actions"]["type"] == "array"
          and "anyOf" in cal["properties"]["actions"]["items"])
    tasks = {_task_const(b) for b in _branches(cal)}
    check("calendar actions anyOf exposes exactly the 5 calendar tasks",
          tasks == {f"calendar.{v}" for v in ("create", "list", "find", "update", "delete")},
          detail=str(sorted(tasks)))
    req = {_task_const(b): set(b["required"]) for b in _branches(cal)}
    check("create requires task+title+start", req["calendar.create"] == {"task", "title", "start"})
    check("update requires task+event_id", req["calendar.update"] == {"task", "event_id"})
    create = next(b for b in _branches(cal) if _task_const(b) == "calendar.create")
    check("optional 'end' is plain-typed, not null-unioned",
          create["properties"]["end"] == {"type": "string"})
    check("every branch sets additionalProperties:false",
          all(b.get("additionalProperties") is False for b in _branches(cal)))

    # the two Anthropic schema-compilation caps — now checked PER DOMAIN.
    nu, no = count_unions(cal), count_optionals(cal)
    check("calendar schema union/array count <= 16", nu <= 16, detail=f"count={nu}")
    check("calendar schema optional-param count <= 24", no <= 24, detail=f"count={no}")

    # --- web: the native-tools lean schema (base only, NO actions) ---
    web = output_schema_for("web")
    check("web schema is the lean base (reasoning/state/message/lang)",
          set(web.get("required", [])) == {"reasoning", "state", "message", "lang"},
          detail=str(sorted(web.get("required", []))))
    check("web schema has NO actions/workflow fields",
          "actions" not in web["properties"] and "workflow" not in web["properties"])
    check("web schema is trivially under the caps",
          count_unions(web) <= 16 and count_optionals(web) == 0)
    check("has_actions: calendar True, web False",
          has_actions("calendar") and not has_actions("web"))

    # --- per-domain system prompt: the actions contract renders only for local skills ---
    sp_cal = system_prompt_for("calendar", settings)
    sp_web = system_prompt_for("web", settings)
    check("calendar prompt carries the actions/workflow contract",
          '"actions"' in sp_cal and '"workflow"' in sp_cal)
    check("web prompt has NO actions/workflow contract", '"actions"' not in sp_web)
    check("web prompt offers web search", "web search" in sp_web.lower())
    check("both prompts substitute the owner name (no stray {owner_name})",
          "Marcelo" in sp_cal and "{owner_name}" not in sp_cal and "{owner_name}" not in sp_web)

    # --- runtime fan-out ---
    h = handlers(settings)
    check("handlers builds a 'calendar' handler and no 'web' handler",
          "calendar" in h and "web" not in h)
    cp, rp = confirm_policies(), render_policies()
    check("calendar confirm policy gates {create,update,delete}",
          getattr(cp["calendar"], "needs", None) == {"create", "update", "delete"})
    check("web has no confirm policy (None)", cp["web"] is None)
    check("calendar render is LLMReadback, web render is None",
          type(cp["calendar"]).__name__ == "FlagConfirm"
          and type(rp["calendar"]).__name__ == "LLMReadback" and rp["web"] is None)
    st = server_tools_for("web", settings)
    check("web server_tools = web_search + web_fetch (with max_uses)",
          [t["type"] for t in st] == ["web_search_20260209", "web_fetch_20260209"]
          and st[0]["max_uses"] == settings.web_search_max_uses)
    check("calendar has no server tools", server_tools_for("calendar", settings) is None)

    # --- reasoner injection ---
    r = AnthropicReasoner(settings, output_schema=cal)
    check("reasoner stores the injected output_schema", r.output_schema is cal)
    r2 = AnthropicReasoner(settings)
    check("reasoner falls back to the calendar schema",
          set(r2.output_schema.get("required", [])) == top)

    # --- deps wiring ---
    deps = build_deps(settings)
    check("deps carries the calendar handler", "calendar" in (deps.tools or {}))
    check("deps carries confirm + render policies",
          bool(deps.confirm_policies) and bool(deps.render_policies))
    check("deps.reasoner default schema stays under the caps",
          count_unions(deps.reasoner.output_schema) <= 16)


# ============================ P1b — the hybrid router =====================================

class _ClsReasoner:
    """A reasoner stub exposing only classify() — for the router's ambiguity path."""

    def __init__(self, domain: str | None = None, boom: bool = False) -> None:
        self.domain, self.boom, self.calls = domain, boom, 0

    async def classify(self, *, system, text, schema, max_tokens=32, effort="low"):
        self.calls += 1
        if self.boom:
            raise RuntimeError("classifier down")
        return {"domain": self.domain}


async def router_checks() -> None:
    print("\nStep-3 P1b — the hybrid router")
    s = Settings(default_domain="web")

    check("matcher: strong scheduling word -> yes",
          calendar_matcher("schedule a meeting on friday") == "yes")
    check("matcher: no calendar signal -> no", calendar_matcher("what's the weather?") == "no")
    check("matcher: weak/time-only signal -> maybe", calendar_matcher("move it to monday") == "maybe")

    # yes -> calendar via matcher, NO classifier call
    r = _ClsReasoner(domain="calendar")
    d, how = await route_domain({"text": "reagendar a reuniao"}, s, reasoner=r)
    check("router: matcher 'yes' routes to calendar with no LLM",
          d == "calendar" and how == "matcher" and r.calls == 0)

    # no -> web default, NO classifier call
    d, how = await route_domain({"text": "tell me a joke"}, s, reasoner=r)
    check("router: no signal falls back to web (default), no LLM",
          d == "web" and how == "default" and r.calls == 0)

    # maybe -> classifier decides
    r2 = _ClsReasoner(domain="calendar")
    d, how = await route_domain({"text": "cancel it tomorrow"}, s, reasoner=r2)
    check("router: ambiguous escalates to the classifier exactly once",
          d == "calendar" and how == "classifier" and r2.calls == 1)

    # classifier error -> safe default (web)
    r3 = _ClsReasoner(boom=True)
    d, how = await route_domain({"text": "cancel it tomorrow"}, s, reasoner=r3)
    check("router: classifier failure falls back to web", d == "web" and how == "default")


# ============================ P2 — the tool loop (graph-driven) ============================

class StubReasoner:
    """Scripted reasoner. Each script entry is one respond() return; missing keys default.
    classify() returns a fixed domain so any stray 'maybe' still routes to calendar."""

    def __init__(self, classify_domain: str = "calendar") -> None:
        self.calls: list = []
        self.script: list = []
        self._classify_domain = classify_domain

    async def respond(self, *, system, messages, output_schema=None, server_tools=None):
        self.calls.append({"system": system, "messages": list(messages),
                           "server_tools": server_tools})
        base = {"state": "keep_listening", "message": None, "lang": "en", "actions": [],
                "workflow": None, "usage": {"input": 1, "output": 1},
                "provider_request_id": "req_stub", "stop_reason": "end_turn",
                "tool_calls": [], "error_category": "none"}
        if self.script:
            base.update(self.script.pop(0))
        return base

    async def classify(self, *, system, text, schema, max_tokens=32, effort="low"):
        return {"domain": self._classify_domain}


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
    # Trigger texts in these tests carry a STRONG calendar word, so the matcher routes them to
    # the calendar skill with no classifier call — the tool loop is what's under test here.
    settings = Settings(evolution_apikey="x", loop_ttl_seconds=60,
                        context_window_messages=30, max_tool_actions=max_tool_actions)
    evo = FakeEvolution(history)
    stub = StubReasoner()
    cal = StubCalendar()
    deps = Deps(settings=settings, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=stub,
                redis=None, tools={"calendar": cal},
                confirm_policies=confirm_policies(), render_policies=render_policies())
    return deps, evo, stub, cal, build_graph(deps, MemorySaver())


async def _invoke(graph, body, jid=OWNER_JID):
    config = {"configurable": {"thread_id": make_thread_id("secretaria", jid)}}
    return await graph.ainvoke({"raw": body}, config=config)


async def graph_checks() -> None:
    print("\nStep-3 P2 — the tool loop (skill-owned confirm + render)")

    # 0. domain routing lands on calendar, and the calendar call attaches NO web tools.
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [{"message": "Hi."}]
    st = await _invoke(graph, _upsert("@mary schedule a call at 3pm", mid="d0"))
    check("[route] a scheduling turn routes to calendar", st.get("domain") == "calendar")
    check("[route] the calendar reason call attaches no server tools",
          stub.calls[0]["server_tools"] is None)

    # 1. a successful write reads back so it confirms FROM the result (LLMReadback render).
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [
        {"message": None, "actions": [
            {"task": "calendar.create", "title": "Call", "start": "2026-08-05T15:00:00-03:00",
             "confirmed": True}]},
        {"message": "Booked your 3pm."},
    ]
    await _invoke(graph, _upsert("@mary schedule a call at 3pm", mid="c1"))
    check("[create] handler ran exactly once", cal.n("create") == 1)
    check("[create] the write reads back via reason ② (two reason passes)", len(stub.calls) == 2)
    check("[create] confirmation sent from the result",
          len(evo.sent) == 1 and "Booked" in evo.sent[-1][1])
    check("[create] execute != close — window stays open", deps.sessions.is_open(OWNER_JID))

    # 2. read (find) -> read-back -> reply.
    deps, evo, stub, cal, graph = make_toolenv()
    cal.responses["find"] = {"ok": True, "summary": "1 match: dentist Fri 15:00 [id=E1]",
                             "data": {"items": [{"event_id": "E1", "title": "dentist"}]}}
    stub.script = [
        {"message": None, "actions": [{"task": "calendar.find", "query": "dentist"}]},
        {"message": "Found it — dentist Friday 15:00."},
    ]
    st = await _invoke(graph, _upsert("@mary find my dentist appointment", mid="f1"))
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
    await _invoke(graph, _upsert("@mary schedule X", mid="e1"))
    check("[fail] read-back happened on failure", len(stub.calls) == 2)
    check("[fail] the premature 'Done!' was NOT sent", all("Done!" not in t for _, t in evo.sent))
    check("[fail] honest error is what got sent", "couldn't create" in evo.sent[-1][1])

    # 4. skill-owned confirm gate: an unconfirmed write is blocked before the handler.
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [
        {"message": "Creating.", "actions": [
            {"task": "calendar.create", "title": "Y", "start": "2026-08-05T10:00:00-03:00"}]},
        {"message": "Want me to create Y at 10:00?"},
    ]
    await _invoke(graph, _upsert("@mary schedule Y at 10", mid="g1"))
    check("[confirm] handler NOT called without confirmation", cal.n("create") == 0)
    check("[confirm] bounced back to ask", len(stub.calls) == 2 and "Want me" in evo.sent[-1][1])

    # 5. resolved-id gate (stays in execute): update on an unseen id is blocked.
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [
        {"message": "Moving it.", "actions": [
            {"task": "calendar.update", "event_id": "GHOST", "confirmed": True,
             "start": "2026-08-06T15:00:00-03:00"}]},
        {"message": "Let me find that event first."},
    ]
    await _invoke(graph, _upsert("@mary reschedule my meeting", mid="u1"))
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
    await _invoke(graph, _upsert("@mary reschedule the review to Friday", mid="fu1"))
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
    st = await _invoke(graph, _upsert("@mary schedule lunch with Ana", mid="w1"))
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
    await _invoke(graph, _upsert("@mary check my agenda", mid="b1"))
    check("[bound] read-back stopped at max_tool_actions", cal.n("find") == 2)

    # 9. language locks at the tag and is fed to every later pass (the PT-drift bug).
    deps, evo, stub, cal, graph = make_toolenv()
    cal.responses["find"] = {"ok": True, "summary": "1 [id=E1]", "data": {"items": [{"event_id": "E1"}]}}
    stub.script = [
        {"message": None, "lang": "en", "actions": [{"task": "calendar.find", "query": "x"}]},
        {"message": "Feito.", "lang": "pt"},  # model tries to drift to PT on the read-back pass
    ]
    await _invoke(graph, _upsert("@mary what's on my agenda friday?", mid="l1"))
    check("[lang] first pass judges from the tag (no lock yet)",
          'in "en"' not in stub.calls[0]["system"])
    check("[lang] read-back pass is TOLD to stay in the locked language",
          'in "en"' in stub.calls[1]["system"] or 'to "en"' in stub.calls[1]["system"])
    check("[lang] header stays locked EN despite the model drifting to PT",
          evo.sent[-1][1].startswith("*[Marcelo's AI Assistant]:*"))

    # 10. web routing: a general turn routes to web, loads web tools, and takes the reason->act
    #     path (no confirm/execute/readback — web has no actions, no render policy).
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [{"message": "It's sunny in Lisbon."}]
    st = await _invoke(graph, _upsert("@mary what's the weather in Lisbon?", mid="web1"))
    check("[web] general turn routes to web", st.get("domain") == "web")
    check("[web] the web reason call attaches web_search/web_fetch",
          [t["type"] for t in (stub.calls[0]["server_tools"] or [])]
          == ["web_search_20260209", "web_fetch_20260209"])
    check("[web] single reason pass (no readback), reply sent",
          len(stub.calls) == 1 and "sunny" in evo.sent[-1][1])
    check("[web] no calendar handler touched", cal.calls == [])


# ============================ P3 — a programmatic render skill (stub) ======================

class _StrReasoner:
    """Emits one create action then would keep talking — but a Programmatic render should end
    the turn at `act` with a formatted reply, so respond must NOT loop back to reason."""

    def __init__(self) -> None:
        self.calls = 0

    async def respond(self, *, system, messages, output_schema=None, server_tools=None):
        self.calls += 1
        base = {"state": "keep_listening", "message": None, "lang": "en", "workflow": None,
                "usage": {"input": 1, "output": 1}, "provider_request_id": "r",
                "stop_reason": "end_turn", "tool_calls": [], "error_category": "none"}
        if self.calls == 1:
            base["actions"] = [{"task": "calendar.create", "title": "Z",
                                "start": "2026-08-05T15:00:00-03:00", "confirmed": True}]
        else:
            base["message"], base["actions"] = "SHOULD NOT SEND", []
        return base

    async def classify(self, *, system, text, schema, max_tokens=32, effort="low"):
        return {"domain": "calendar"}


async def render_checks() -> None:
    print("\nStep-3 P3 — a Programmatic render skill (no second model call)")
    from app.skills.render import Programmatic

    fmt = lambda results, state: f"Done: {results[0]['summary']}"
    settings = Settings(evolution_apikey="x", loop_ttl_seconds=60, context_window_messages=30)
    evo, cal, reasoner = FakeEvolution(), StubCalendar(), _StrReasoner()
    deps = Deps(settings=settings, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=reasoner,
                redis=None, tools={"calendar": cal},
                confirm_policies=confirm_policies(),
                render_policies={"calendar": Programmatic(fmt), "web": None})
    graph = build_graph(deps, MemorySaver())
    await _invoke(graph, _upsert("@mary schedule Z at 3pm", mid="pr1"))
    check("[render] the write ran", cal.n("create") == 1)
    check("[render] exactly ONE model call — no LLM readback", reasoner.calls == 1)
    check("[render] reply was assembled programmatically from the result",
          evo.sent and "Done: create ok" in evo.sent[-1][1])


# ===================== P4 — the Google Calendar handler (fake service) =====================

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
    print("\nStep-3 P4 — Google Calendar handler (fake service)")
    tz = Settings().calendar_timezone

    h = _cal_handler()
    r = await h.run("create", {"title": "Call Ana", "start": "2026-08-05T15:00:00-03:00",
                               "confirmed": True})
    body = next(kw["body"] for v, kw in h._svc.events().calls if v == "insert")
    check("[create] ok + event_id", r.get("ok") and r["data"]["event_id"] == "NEW1")
    check("[create] summary == 'Call Ana', tz stamped",
          body["summary"] == "Call Ana" and body["start"]["timeZone"] == tz)
    check("[create] end defaulted to start + 45min",
          body["end"]["dateTime"] == "2026-08-05T15:45:00-03:00")

    h = _cal_handler()
    r = await h.run("create", {"title": "Sync", "start": "2026-08-05T09:00:00-03:00",
                               "virtual": True, "location": "ignored", "confirmed": True})
    ins = next(kw for v, kw in h._svc.events().calls if v == "insert")
    check("[create] virtual drops location + requests Meet",
          ins["body"]["location"] is None and "conferenceData" in ins["body"])
    check("[create] summary says 'Video call' and does NOT paste the Meet link",
          "Video call" in r["summary"] and "meet.google.com" not in r["summary"])

    r = await _cal_handler().run("create", {"start": "2026-08-05T15:00:00-03:00"})
    check("[create] missing title -> validation error", r["ok"] is False and r["error"] == "validation")

    import datetime as _dt
    h = _cal_handler([
        _ev("L1", "Standup", "2026-08-05T09:00:00-03:00", "2026-08-05T09:15:00-03:00"),
        _ev("L2", "Review", "2026-08-05T15:00:00-03:00", "2026-08-05T16:00:00-03:00"),
        _ev("L3", "Dentist", "2026-08-06T11:00:00-03:00", "2026-08-06T12:00:00-03:00"),
    ])
    r = await h.run("list", {})
    lm = next(kw["timeMin"] for v, kw in h._svc.events().calls if v == "list")
    check("[list] timeMin is tz-aware", _dt.datetime.fromisoformat(lm).tzinfo is not None)
    check("[list] agenda header is 'DD/MMM - Weekday'", "05/Aug - Wednesday" in r["summary"])
    check("[list] events are 'HH:MM - Title', time-ordered under the day",
          "09:00 - Standup\n15:00 - Review" in r["summary"])
    check("[list] empty calendar says so", (await _cal_handler([]).run("list", {}))["summary"]
          == "No upcoming events.")

    seed = [
        _ev("D1", "Dentist appointment", "2026-08-07T15:00:00-03:00", "2026-08-07T16:00:00-03:00"),
        _ev("T1", "Team standup", "2026-08-05T09:00:00-03:00", "2026-08-05T09:15:00-03:00"),
        _ev("P1", "Lunch", "2026-08-06T12:00:00-03:00", "2026-08-06T13:00:00-03:00",
            attendees=["paulo@x.com"]),
    ]
    r = await _cal_handler(seed).run("find", {"query": "dentist"})
    check("[find] full-text resolves the dentist event",
          r["ok"] and r["data"]["items"][0]["event_id"] == "D1")
    r = await _cal_handler(seed).run("find", {"query": "dentst"})
    check("[find] fuzzy fallback tolerates a typo",
          r["ok"] and r["data"]["items"][0]["event_id"] == "D1")
    r = await _cal_handler(seed).run("find", {"query": "nonexistent-zzz"})
    check("[find] no match -> empty, still ok", r["ok"] and r["data"]["items"] == [])

    h = _cal_handler([_ev("E1", "Review", "2026-08-06T15:00:00-03:00", "2026-08-06T16:00:00-03:00")])
    r = await h.run("update", {"event_id": "E1", "start": "2026-08-06T18:00:00-03:00",
                               "confirmed": True})
    patch = next(kw["body"] for v, kw in h._svc.events().calls if v == "patch")
    check("[update] original duration preserved (end = start + 60)",
          patch["end"]["dateTime"] == "2026-08-06T19:00:00-03:00")

    h = _cal_handler([_ev("E1", "Review", "2026-08-06T15:00:00-03:00", "2026-08-06T16:00:00-03:00")])
    r = await h.run("delete", {"event_id": "E1", "confirmed": True})
    check("[delete] event removed", r["ok"] and h._svc.store["items"] == [])

    r = await _cal_handler(raise_on={"insert": _GErr(403)}).run(
        "create", {"title": "X", "start": "2026-08-05T15:00:00-03:00", "confirmed": True})
    check("[error] 403 -> auth", r["ok"] is False and r["error"] == "auth")
    r = await _cal_handler().run("teleport", {})
    check("[error] unknown verb handled", r["ok"] is False and r["error"] == "unknown_verb")


def _finish() -> None:
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    unit_checks()
    asyncio.run(router_checks())
    asyncio.run(graph_checks())
    asyncio.run(render_checks())
    asyncio.run(calendar_checks())
    _finish()
