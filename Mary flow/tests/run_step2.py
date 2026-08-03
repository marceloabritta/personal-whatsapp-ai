"""End-to-end Step-2 verification — drives the real compiled graph (with an in-memory
checkpointer) through the listening loop, using a scripted stub reasoner and a fake
Evolution. No network, no Postgres, no Anthropic key.

    cd brain && python tests/run_step2.py
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
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.identity import frame  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402

OWNER_JID = "5511976001033@s.whatsapp.net"
OTHER_JID = "5531888888888@s.whatsapp.net"
PT_JID = "5521777777777@s.whatsapp.net"
_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    _checks["pass" if cond else "fail"] += 1


class StubReasoner:
    def __init__(self) -> None:
        self.calls: list = []
        self.script: list = []

    async def respond(self, *, system, messages, output_schema=None, server_tools=None):
        self.calls.append({"system": system, "messages": list(messages)})
        if self.script:
            return self.script.pop(0)
        return {"state": "keep_listening", "message": None, "lang": "en",
                "usage": {"input": 1, "output": 1}, "provider_request_id": "req_stub",
                "stop_reason": "end_turn", "tool_calls": [], "error_category": "none"}

    async def classify(self, *, system, text, schema, max_tokens=32, effort="low"):
        # These are general/chat turns — the router's ambiguity band resolves to web.
        return {"domain": "web"}


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


def rec(mid, text, from_me=True, ts=0, push="Tester"):
    return {"id": mid, "from_me": from_me, "text": text, "push_name": push, "ts": ts}


def upsert(text, *, from_me=True, mid="m1", jid=OWNER_JID):
    return {"data": {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": mid},
        "message": {"conversation": text},
        "messageTimestamp": 1730000000, "pushName": "Tester",
    }}


def make_env(history=None):
    settings = Settings(evolution_apikey="x", mary_trigger_tag="@mary",
                        loop_ttl_seconds=60, context_window_messages=30)
    evo = FakeEvolution(history)
    stub = StubReasoner()
    deps = Deps(settings=settings, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=build_trace(), reasoner=stub,
                redis=None)
    graph = build_graph(deps, MemorySaver())
    return deps, evo, stub, graph


async def invoke(graph, body, jid=OWNER_JID):
    config = {"configurable": {"thread_id": make_thread_id("secretaria", jid)}}
    return await graph.ainvoke({"raw": body}, config=config)


async def main() -> None:
    # ---- Sequential conversation on one thread (memory + loop) ----
    deps, evo, stub, graph = make_env(history=[rec("h1", "@mary who won the match?")])

    print("1. owner opens the window with @mary")
    stub.script = [{"state": "keep_listening", "message": "On it — checking.", "lang": "en",
                    "usage": {"input": 5, "output": 3}, "provider_request_id": "req_1",
                    "stop_reason": "end_turn", "tool_calls": ["web_search"], "error_category": "none"}]
    await invoke(graph, upsert("@mary who won the match?", mid="a1"))
    check("one reply sent", len(evo.sent) == 1)
    check("reply framed with header", evo.sent[-1][1].startswith("*[Marcelo's AI Assistant]:*"))
    check("reply carries the model message", "On it" in evo.sent[-1][1])
    check("window is open", deps.sessions.is_open(OWNER_JID))
    check("record emitted", any(e.get("node") == "record" for e in deps.trace.events))
    rc = [e for e in deps.trace.events if e.get("node") == "record"][-1]
    check("record has state + provider_request_id", rc.get("state") == "keep_listening" and rc.get("provider_request_id") == "req_1")

    print("2. untagged, not for her → silent, window stays open")
    stub.script = [{"state": "keep_listening", "message": None, "usage": {},
                    "provider_request_id": "req_2", "stop_reason": "end_turn",
                    "tool_calls": [], "error_category": "none"}]
    await invoke(graph, upsert("hmm, need to leave early today", mid="a2"))
    check("nothing new sent (silent)", len(evo.sent) == 1)
    check("window still open", deps.sessions.is_open(OWNER_JID))

    print("3. the contact writes while the window is open → she may act (option a)")
    stub.script = [{"state": "keep_listening", "message": "Barcelona won 2–1.",
                    "usage": {}, "provider_request_id": "req_3", "stop_reason": "end_turn",
                    "tool_calls": [], "error_category": "none"}]
    await invoke(graph, upsert("do you know the score?", from_me=False, mid="a3"))
    check("contact message triggered a reply", len(evo.sent) == 2)
    check("memory carried prior turns", any(
        m["role"] == "assistant" and "On it" in m["content"] for m in stub.calls[-1]["messages"]))

    print("4. model closes with a message → sent AS-IS (no programmatic trailer)")
    stub.script = [{"state": "close",
                    "message": "Glad I could help! Talk soon 👋",
                    "usage": {}, "provider_request_id": "req_4", "stop_reason": "end_turn",
                    "tool_calls": [], "error_category": "none"}]
    await invoke(graph, upsert("@mary thanks", mid="a4"))
    convo4 = stub.calls[-1]["messages"]
    check("fresh @mary tag wiped the prior loop's memory (no context leak)", not any(
        ("On it" in m["content"]) or ("Barcelona" in m["content"]) for m in convo4))
    sent = evo.sent[-1][1]
    check("closing reply sent", len(evo.sent) == 3)
    check("message sent verbatim", "Glad I could help! Talk soon 👋" in sent)
    check("no programmatic trailer added", "signing off here" not in sent)
    check("window closed", not deps.sessions.is_open(OWNER_JID))
    rc = [e for e in deps.trace.events if e.get("node") == "record"][-1]
    check("record close_reason == model", rc.get("close_reason") == "model")

    print("5. after close, an untagged message is ignored")
    before = len(evo.sent)
    await invoke(graph, upsert("still there?", mid="a5"))
    check("no reply after close", len(evo.sent) == before)

    # ---- Isolated gate cases ----
    print("6. a contact's @mary with no open window is ignored")
    _, evo6, _, graph6 = make_env(history=[])
    await invoke(graph6, upsert("@mary hi", from_me=False, mid="b1", jid=OTHER_JID), jid=OTHER_JID)
    check("nothing sent (non-owner, no window)", len(evo6.sent) == 0)

    print("7. Mary's own echoed reply is ignored (no loop) — EN and PT headers")
    _, evo7, _, graph7 = make_env(history=[])
    st = await invoke(graph7, upsert(frame("On it — checking.", "Marcelo", "en"), from_me=True, mid="b2"))
    check("is_own detected (EN header)", st["is_own"] is True)
    st = await invoke(graph7, upsert(frame("Já verifico.", "Marcelo", "pt"), from_me=True, mid="b3"))
    check("is_own detected (PT header)", st["is_own"] is True)
    check("nothing sent (own echoes)", len(evo7.sent) == 0)

    print("8. a Portuguese tag → PT header, and the language is locked for the window")
    _, evoP, stubP, graphP = make_env(history=[rec("p0", "@mary qual a previsão?")])
    stubP.script = [{"state": "keep_listening", "message": "Vou verificar agora.", "lang": "pt",
                     "usage": {}, "provider_request_id": "req_p1", "stop_reason": "end_turn",
                     "tool_calls": [], "error_category": "none"}]
    await invoke(graphP, upsert("@mary qual a previsão?", mid="p1", jid=PT_JID), jid=PT_JID)
    check("PT header used", evoP.sent[-1][1].startswith("*[Assistente IA do Marcelo]:*"))
    # continuation: model drifts to en, but the session language stays PT
    stubP.script = [{"state": "keep_listening", "message": "Still here.", "lang": "en",
                     "usage": {}, "provider_request_id": "req_p2", "stop_reason": "end_turn",
                     "tool_calls": [], "error_category": "none"}]
    await invoke(graphP, upsert("e amanhã?", mid="p2", jid=PT_JID), jid=PT_JID)
    check("session language locked to PT (header stays PT)", evoP.sent[-1][1].startswith("*[Assistente IA do Marcelo]:*"))

    print("9. our own reply is filtered from ingestion by message id (header-independent)")
    depsE, evoE, stubE, graphE = make_env(history=[
        rec("u1", "@mary hi", from_me=True),
        # An echoed reply WITHOUT the header — only the recorded id can catch this.
        rec("selfecho", "On it (no header)", from_me=True),
    ])
    depsE.echoes.record(OWNER_JID, "selfecho")
    await invoke(graphE, upsert("@mary hi", mid="u1"))
    seed = " ".join(m["content"] for m in stubE.calls[-1]["messages"])
    check("owner's real message ingested", "hi" in seed)
    check("own reply dropped by id despite missing header", "On it (no header)" not in seed)

    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
