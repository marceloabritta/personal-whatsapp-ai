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

    # --- the union-cap guard (the outage backstop) ---
    n = count_unions(schema)
    check("schema union/array count <= 16", n <= 16, detail=f"count={n}")

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

    # 1. clean write (confirmed create) -> act; one reason pass, no read-back.
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [{"message": "Booked your 3pm.", "actions": [
        {"task": "calendar.create", "title": "Call", "start": "2026-08-05T15:00:00-03:00",
         "confirmed": True}]}]
    await _invoke(graph, _upsert("@mary book a call at 3pm", mid="c1"))
    check("[create] handler ran exactly once", cal.n("create") == 1)
    check("[create] clean write did NOT read back (single reason pass)", len(stub.calls) == 1)
    check("[create] confirmation sent", len(evo.sent) == 1 and "Booked" in evo.sent[-1][1])
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
        {"message": "Moved it to Friday.", "actions": [
            {"task": "calendar.update", "event_id": "E9", "confirmed": True,
             "start": "2026-08-07T15:00:00-03:00"}]},
    ]
    await _invoke(graph, _upsert("@mary move the review to Friday", mid="fu1"))
    check("[find->update] update ran on the resolved id",
          cal.n("find") == 1 and cal.n("update") == 1)
    check("[find->update] the id passed to update was the found one",
          next((i["event_id"] for v, i in cal.calls if v == "update"), None) == "E9")
    check("[find->update] final confirmation sent", "Moved it" in evo.sent[-1][1])

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


def _finish() -> None:
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    unit_checks()
    asyncio.run(graph_checks())
    _finish()
