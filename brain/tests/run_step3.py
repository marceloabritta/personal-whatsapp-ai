"""End-to-end Step-3 verification — the tool loop, with a scripted stub reasoner and a
stub calendar handler (no network, no Postgres, no Anthropic key, no Google).

    cd brain && python tests/run_step3.py
Exits non-zero on the first failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.identity import frame  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.tools.registry import build_output_schema  # noqa: E402
from app.trace import build_trace  # noqa: E402

OWNER = "5511976001033@s.whatsapp.net"
OTHER = "5531888888888@s.whatsapp.net"
_c = {"pass": 0, "fail": 0}


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    _c["pass" if cond else "fail"] += 1


class StubReasoner:
    def __init__(self):
        self.calls = 0
        self.script = []

    async def respond(self, *, system, messages):
        self.calls += 1
        r = dict(self.script.pop(0)) if self.script else {}
        r.setdefault("lang", "pt")
        r.setdefault("next_message", None)
        r.setdefault("loop_state", "keep_listening")
        r.setdefault("actions", [])
        r.setdefault("workflow", None)
        for k, v in {"usage": {}, "provider_request_id": "req", "stop_reason": "end_turn",
                     "tool_calls": [], "error_category": "none"}.items():
            r.setdefault(k, v)
        return r


class StubCalendar:
    def __init__(self):
        self.calls = []
        self.results = {}

    async def run(self, verb, inputs):
        self.calls.append((verb, inputs))
        return self.results.get(verb, {"ok": True, "summary": f"{verb} ok",
                                       "data": {"event_id": "ev1"}})


class StubEvolution:
    def __init__(self, instance="secretaria"):
        self.instance = instance
        self.sent = []
        self.history = [{"id": "h0", "from_me": True, "text": "@mary",
                         "push_name": "T", "ts": 1}]

    async def send_text(self, number, text):
        self.sent.append((number, text))
        return True

    async def fetch_history(self, jid):
        return self.history


def upsert(text, *, from_me=True, mid="m1", jid=OWNER):
    return {"data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": mid},
                     "message": {"conversation": text}, "messageTimestamp": 1730000000,
                     "pushName": "Tester"}}


def make_env():
    settings = Settings(evolution_apikey="x", mary_trigger_tag="@mary",
                        loop_ttl_seconds=60, max_tool_actions=4)
    evo = StubEvolution(settings.evolution_instance)
    stub = StubReasoner()
    cal = StubCalendar()
    deps = Deps(settings=settings, evolution=evo, sessions=InMemorySessions(ttl=60),
                trace=build_trace(), reasoner=stub, tools={"calendar": cal},
                tools_prompt="calendar", redis=None)
    graph = build_graph(deps, MemorySaver())
    return deps, evo, stub, cal, graph


async def invoke(graph, body, jid=OWNER):
    cfg = {"configurable": {"thread_id": make_thread_id("secretaria", jid)}}
    return await graph.ainvoke({"raw": body}, config=cfg)


async def main():
    ISO = "2026-08-03T15:00:00-03:00"

    print("1. create (clean write) + close")
    _, evo, stub, cal, g = make_env()
    stub.script = [{"next_message": "Feito, marquei a call!", "loop_state": "close_loop",
                    "actions": [{"task": "calendar.create",
                                 "inputs": {"title": "Call Paulo", "start": ISO,
                                            "attendees": ["paulo@x.com"], "virtual": False}}]}]
    cal.results["create"] = {"ok": True, "summary": "Created 'Call Paulo'", "data": {"event_id": "ev1"}}
    await invoke(g, upsert("@mary marca call com paulo amanhã 15h"))
    check("create handler called", any(v == "create" for v, _ in cal.calls))
    check("confirmation sent", evo.sent and "Feito" in evo.sent[-1][1])
    check("no read-back (one reason call)", stub.calls == 1)

    print("2. list (read) → read-back → schedule reply")
    _, evo, stub, cal, g = make_env()
    stub.script = [
        {"next_message": None, "actions": [{"task": "calendar.list", "inputs": {"query": "amanhã"}}]},
        {"next_message": "Você tem 2 eventos amanhã.", "loop_state": "keep_listening"},
    ]
    cal.results["list"] = {"ok": True, "summary": "2 events...", "data": {"items": [{"event_id": "e1"}]}}
    await invoke(g, upsert("@mary o que tenho amanhã?"))
    check("list handler called", any(v == "list" for v, _ in cal.calls))
    check("read-back happened (two reason calls)", stub.calls == 2)
    check("schedule reply sent", evo.sent and "2 eventos" in evo.sent[-1][1])

    print("3. failure → read-back → honest error (no false 'done')")
    _, evo, stub, cal, g = make_env()
    stub.script = [
        {"next_message": "Feito!", "loop_state": "close_loop",
         "actions": [{"task": "calendar.create", "inputs": {"title": "X", "start": ISO}}]},
        {"next_message": "Não consegui criar, deu erro.", "loop_state": "keep_listening"},
    ]
    cal.results["create"] = {"ok": False, "summary": "calendar error", "error": "boom"}
    await invoke(g, upsert("@mary marca X amanhã 15h"))
    check("read-back on failure", stub.calls == 2)
    check("honest error sent", evo.sent and "Não consegui" in evo.sent[-1][1])
    check("no false 'Feito!' sent", all("Feito!" not in t for _, t in evo.sent))

    print("4. execution ≠ closure (execute + keep_listening keeps the window open)")
    deps4, evo, stub, cal, g = make_env()
    stub.script = [{"next_message": "Marquei!", "loop_state": "keep_listening",
                    "actions": [{"task": "calendar.create", "inputs": {"title": "X", "start": ISO}}]}]
    cal.results["create"] = {"ok": True, "summary": "ok", "data": {"event_id": "ev1"}}
    await invoke(g, upsert("@mary marca X amanhã 15h"))
    check("window still open after a successful execute", deps4.sessions.is_open(OWNER))

    print("5. workflow gather (asks, keeps listening, persists workflow)")
    deps5, evo, stub, cal, g = make_env()
    stub.script = [{"next_message": "Paulo, qual seu e-mail?", "loop_state": "keep_listening",
                    "workflow": {"task": "calendar.create",
                                 "known_inputs": [{"field": "title", "value": "Call Paulo"}],
                                 "open_questions": [{"field": "attendee_email", "reason": "invite"}]}}]
    await invoke(g, upsert("@mary marca call com paulo amanhã"))
    check("no action run while gathering", cal.calls == [])
    check("question sent", evo.sent and "e-mail" in evo.sent[-1][1])
    snap = await g.aget_state({"configurable": {"thread_id": make_thread_id("secretaria", OWNER)}})
    check("workflow persisted", (snap.values.get("workflow") or {}).get("task") == "calendar.create")

    print("6. multiple actions in one call (delete + update), in order")
    _, evo, stub, cal, g = make_env()
    stub.script = [{"next_message": "Feito: cancelei e adicionei.", "loop_state": "close_loop",
                    "actions": [{"task": "calendar.delete", "inputs": {"event_id": "e1"}},
                                {"task": "calendar.update", "inputs": {"event_id": "e2",
                                                                       "attendees": ["paulo@x.com"]}}]}]
    cal.results["delete"] = {"ok": True, "summary": "cancelled", "data": {"event_id": "e1"}}
    cal.results["update"] = {"ok": True, "summary": "updated", "data": {"event_id": "e2"}}
    await invoke(g, upsert("@mary cancela a do paulo e me adiciona na do caio"))
    check("both actions ran in order", [v for v, _ in cal.calls] == ["delete", "update"])
    check("combined confirmation sent", evo.sent and "cancelei" in evo.sent[-1][1])

    print("7. gate: own echo + non-owner ignored")
    _, evo7, stub7, cal7, g7 = make_env()
    st = await invoke(g7, upsert(frame("oi", "Marcelo", "pt"), from_me=True, mid="b1"))
    check("own echo ignored", st["is_own"] is True and len(evo7.sent) == 0)
    _, evo8, _, _, g8 = make_env()
    await invoke(g8, upsert("@mary hi", from_me=False, mid="b2", jid=OTHER), jid=OTHER)
    check("non-owner (no window) ignored", len(evo8.sent) == 0)

    print("8. output schema is well-formed (actions anyOf built from registry)")
    sch = build_output_schema()
    tasks = {b["properties"]["task"]["const"] for b in sch["properties"]["actions"]["items"]["anyOf"]}
    check("schema exposes all 4 calendar tasks",
          tasks == {"calendar.create", "calendar.list", "calendar.update", "calendar.delete"})

    print(f"\n{_c['pass']} passed, {_c['fail']} failed")
    sys.exit(1 if _c["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
