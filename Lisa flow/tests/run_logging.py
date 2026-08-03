"""Durable loop-log verification, three layers, first failure exits non-zero:

  A. pure functions — stream classification, redaction, the loops-row mapping (no DB);
  B. fake-sink end-to-end — drives the REAL compiled graph and asserts the three
     streams land, grouped by loop, with each @mary tag a fresh loop (no DB, no key);
  C. real Postgres — if MARY_TEST_DATABASE_URL is set, opens a LogStore against a
     throwaway schema and round-trips a synthetic loop (dedup + upsert + read).

    cd brain && python tests/run_logging.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app import logstore as ls  # noqa: E402
from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.identity import frame  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402

OWNER_JID = "5511976001033@s.whatsapp.net"
_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    _checks["pass" if cond else "fail"] += 1


# ============================ A. pure functions ============================
def test_pure() -> None:
    print("A. pure functions (no DB)")

    check("stream: user → transcript", ls.stream_for({"level": "user", "who": "x"}) == "transcript")
    check("stream: reason → reasoning", ls.stream_for({"level": "code", "node": "reason"}) == "reasoning")
    check("stream: gate → control", ls.stream_for({"level": "code", "node": "gate"}) == "control")
    check("stream: record → control", ls.stream_for({"level": "code", "node": "record"}) == "control")
    check("label: transcript = who", ls.label_for({"level": "user", "who": "Marcelo"}) == "Marcelo")
    check("label: control = node", ls.label_for({"level": "code", "node": "context"}) == "context")

    check("redact: anthropic key", "sk-ant-" not in ls.redact_text("key sk-ant-abc123456789xyz done"))
    check("redact: bearer", "secretbearervalue" not in ls.redact_text("Authorization: Bearer secretbearervalue"))
    check("redact: apikey=", ls.redact_text("apikey=supersecretlongvalue").endswith("«redacted»"))
    check("redact: plain text untouched", ls.redact_text("who won the match?") == "who won the match?")
    nested = ls.redact_obj({"a": ["sk-ant-abcdefghij0123456789"], "b": "hi"})
    check("redact_obj: nested leaf", "sk-ant-" not in nested["a"][0] and nested["b"] == "hi")

    close = ls.loop_row({"loop_id": "L", "chat_id": "C", "state": "close",
                         "response": "bye", "input_tokens": 5, "output_tokens": 2,
                         "provider": "anthropic", "loop_started_ts": 100}, ts=200.0)
    check("loop_row: close sets ended + end_reason", close["ended_ts"] == 200.0 and close["end_reason"] == "model")
    check("loop_row: reply counted", close["replies"] == 1)
    check("loop_row: started_ts preserved", close["started_ts"] == 100)
    keep = ls.loop_row({"loop_id": "L", "state": "keep_listening", "response": None}, ts=200.0)
    check("loop_row: open → no end", keep["ended_ts"] is None and keep["end_reason"] is None)
    check("loop_row: silent → 0 replies", keep["replies"] == 0)


# ============================ B. fake-sink e2e ============================
class FakeSink:
    """Stands in for the LogStore — captures exactly what Trace would persist."""
    def __init__(self) -> None:
        self.recs: list = []

    def enqueue(self, rec: dict) -> None:
        self.recs.append(rec)


class StubReasoner:
    def __init__(self) -> None:
        self.calls: list = []
        self.script: list = []

    async def respond(self, *, system, messages, output_schema=None, server_tools=None):
        self.calls.append({"messages": list(messages)})
        if self.script:
            return self.script.pop(0)
        return {"state": "keep_listening", "message": None, "lang": "en", "reasoning": None,
                "usage": {"input": 1, "output": 1}, "provider_request_id": "req_stub",
                "stop_reason": "end_turn", "tool_calls": [], "error_category": "none"}

    async def classify(self, *, system, text, schema, max_tokens=32, effort="low"):
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


def wa(mid, text, from_me=True, ts=0):
    return {"id": mid, "from_me": from_me, "text": text, "push_name": "Tester", "ts": ts}


def upsert(text, *, from_me=True, mid="m1", jid=OWNER_JID):
    return {"data": {
        "key": {"remoteJid": jid, "fromMe": from_me, "id": mid},
        "message": {"conversation": text},
        "messageTimestamp": 1730000000, "pushName": "Tester",
    }}


async def invoke(graph, body, jid=OWNER_JID):
    config = {"configurable": {"thread_id": make_thread_id("secretaria", jid)}}
    return await graph.ainvoke({"raw": body}, config=config)


async def test_e2e() -> None:
    print("B. fake-sink end-to-end (real graph, no DB)")
    # History before the tag: owner + contact + a PRIOR Mary reply (own, has header).
    history = [
        wa("h1", "hey are you around?", from_me=False, ts=1),
        wa("h2", frame("Yes — here.", "Marcelo", "en"), from_me=True, ts=2),
        wa("h3", "@mary what's the score?", from_me=True, ts=3),
    ]
    settings = Settings(evolution_apikey="x", mary_trigger_tag="@mary",
                        loop_ttl_seconds=60, context_window_messages=30)
    evo, stub = FakeEvolution(history), StubReasoner()
    sink = FakeSink()
    trace = build_trace()
    trace.attach_sink(sink)
    deps = Deps(settings=settings, evolution=evo, sessions=InMemorySessions(ttl=60),
                echoes=InMemoryEchoes(ttl=3600), trace=trace, reasoner=stub, redis=None)
    graph = build_graph(deps, MemorySaver())

    # -- loop 1: fresh tag opens it, model replies, keeps listening --
    stub.script = [{"state": "keep_listening", "message": "Barcelona won 2–1.", "lang": "en",
                    "reasoning": "Owner asked the score; found it and answered, staying available.",
                    "usage": {"input": 20, "output": 8}, "provider_request_id": "req_1",
                    "stop_reason": "end_turn", "tool_calls": ["web_search"], "error_category": "none"}]
    await invoke(graph, upsert("@mary what's the score?", mid="h3"))

    check("every persisted record is loop-scoped", all(r.get("loop_id") for r in sink.recs))
    loop_ids = {r["loop_id"] for r in sink.recs}
    check("one loop so far", len(loop_ids) == 1)
    L1 = next(iter(loop_ids))

    by_stream: dict[str, list] = {"transcript": [], "reasoning": [], "control": []}
    for r in sink.recs:
        by_stream[ls.stream_for(r)].append(r)
    check("transcript stream populated", len(by_stream["transcript"]) >= 3)
    check("reasoning stream populated", len(by_stream["reasoning"]) == 1)
    check("control stream has gate+context+record", {"gate", "context", "record"} <=
          {r.get("node") for r in by_stream["control"]})

    tx_texts = [r["text"] for r in by_stream["transcript"]]
    tx_whos = {r["who"] for r in by_stream["transcript"]}
    check("transcript seeded the 30-before-tag (contact line present)",
          any("are you around" in t for t in tx_texts))
    check("transcript includes BOTH sides (prior Mary reply, labelled)",
          "AI Assistant" in tx_whos and any("Yes — here." in t for t in tx_texts))
    check("transcript captured Mary's new reply", any("Barcelona won" in t for t in tx_texts))
    check("Mary's reply carries its sent id", any(
        r["who"] == "AI Assistant" and r.get("wa_id") == "echo0" for r in by_stream["transcript"]))

    rz = by_stream["reasoning"][0]
    check("reasoning stream has the model's rationale", "score" in (rz.get("reasoning") or ""))
    check("reasoning stream has the decision", rz.get("state") == "keep_listening" and "Barcelona won" in (rz.get("message") or ""))

    rec1 = [r for r in by_stream["control"] if r.get("node") == "record"][-1]
    check("record is tagged with the loop id", rec1.get("loop_id") == L1)

    # -- continuation: untagged follow-up, same loop --
    n_before = len(sink.recs)
    stub.script = [{"state": "keep_listening", "message": None, "lang": "en", "reasoning": None,
                    "usage": {}, "provider_request_id": "req_2", "stop_reason": "end_turn",
                    "tool_calls": [], "error_category": "none"}]
    await invoke(graph, upsert("thanks!", from_me=False, mid="c1"))
    cont = sink.recs[n_before:]
    check("continuation stays in the same loop", {r["loop_id"] for r in cont} == {L1})
    check("continuation transcript logged only the new inbound (no re-seed)",
          [r["text"] for r in cont if ls.stream_for(r) == "transcript"] == ["thanks!"])

    # -- loop 2: a second tag mints a DISTINCT loop --
    n_before = len(sink.recs)
    stub.script = [{"state": "close", "message": "All done 👋", "lang": "en", "reasoning": None,
                    "usage": {"input": 3, "output": 2}, "provider_request_id": "req_3",
                    "stop_reason": "end_turn", "tool_calls": [], "error_category": "none"}]
    await invoke(graph, upsert("@mary one more thing", mid="d1"))
    new_ids = {r["loop_id"] for r in sink.recs[n_before:]}
    check("second @mary tag opened a NEW loop", len(new_ids) == 1 and L1 not in new_ids)
    rec2 = [r for r in sink.recs[n_before:] if r.get("node") == "record"][-1]
    check("closing record marks state=close", rec2.get("state") == "close")

    # -- ignored traffic never reaches the log --
    n_before = len(sink.recs)
    await invoke(graph, upsert("random noise", from_me=False, mid="e1"))  # window closed → ignored
    check("an ignored message writes nothing durable", len(sink.recs) == n_before)


# ============================ C. real Postgres ============================
async def test_real_db() -> None:
    dsn = os.environ.get("MARY_TEST_DATABASE_URL")
    if not dsn:
        print("C. real Postgres — SKIPPED (set MARY_TEST_DATABASE_URL to run)")
        return
    print("C. real Postgres (throwaway schema)")
    schema = "mary_log_selftest"

    # Pre-seed the OLD (pre-revert) shape so the migration guard has something to fix.
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
        await c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await c.execute(f"CREATE SCHEMA {schema}")
        await c.execute(f"CREATE TABLE {schema}.turns (trace_id text primary key, chat_id text)")
        await c.execute(f"CREATE TABLE {schema}.events (id bigserial primary key, "
                        "trace_id text, chat_id text, seq int, ts timestamptz, "
                        "stream text, label text, payload jsonb)")
        await c.execute(f"INSERT INTO {schema}.events (trace_id, seq, ts, stream, payload) "
                        "VALUES ('old', 0, now(), 'control', '{}'::jsonb)")

    store = ls.LogStore(dsn, schema=schema)
    await store.open()  # runs the migration guard + DDL
    async with store._pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM information_schema.tables "
                                 f"WHERE table_schema='{schema}' AND table_name='turns'")
        check("migration dropped old turns", (await cur.fetchone())[0] == 0)
        cur = await conn.execute("SELECT count(*) FROM information_schema.columns WHERE "
                                 f"table_schema='{schema}' AND table_name='events' AND column_name='loop_id'")
        check("migration rebuilt events (loop_id present)", (await cur.fetchone())[0] == 1)
    try:
        L = "loop-test-1"
        base = {"trace_id": "t1", "loop_id": L, "chat_id": "C1"}
        # transcript ×2 with the SAME wa id → dedup must keep one; a reasoning event;
        # then two record events (keep → close) → upsert must increment + close.
        await store._flush([
            {**base, "level": "user", "who": "Marcelo", "text": "hi", "wa_id": "m1"},
            {**base, "level": "user", "who": "Marcelo", "text": "hi (dup)", "wa_id": "m1"},
            {**base, "level": "code", "node": "reason", "state": "keep_listening",
             "reasoning": "rationale… sk-ant-shouldberedacted0123456789", "message": "hello"},
            {**base, "node": "record", "state": "keep_listening", "response": "hello",
             "input_tokens": 10, "output_tokens": 4, "loop_started_ts": 1000,
             "provider": "anthropic", "model": "claude", "trigger": "tag"},
        ])
        await store._flush([
            {**base, "node": "record", "state": "close", "response": "bye",
             "input_tokens": 3, "output_tokens": 1, "loop_started_ts": 1000},
        ])
        got = await store.read_loop(L)
        loop, events = got["loop"], got["events"]
        check("loop row upserted", loop is not None and loop["loop_id"] == L)
        check("activations incremented across flushes", loop["activations"] == 2)
        check("tokens accumulated", loop["input_tokens"] == 13 and loop["output_tokens"] == 5)
        check("loop closed", loop["end_reason"] == "model" and loop["ended_at"] is not None)
        tx = [e for e in events if e["stream"] == "transcript"]
        check("transcript deduped by wa id", len(tx) == 1)
        rz = [e for e in events if e["stream"] == "reasoning"]
        check("reasoning persisted", len(rz) == 1)
        check("secret redacted at rest", "sk-ant-" not in str(rz[0]["payload"]))

        await store.open()  # idempotent second boot — must NOT wipe the new tables
        got2 = await store.read_loop(L)
        check("second open() preserves data (migration idempotent)",
              got2 is not None and got2["loop"]["activations"] == 2)
    finally:
        async with store._pool.connection() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await store.aclose()


async def main() -> None:
    test_pure()
    await test_e2e()
    await test_real_db()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
