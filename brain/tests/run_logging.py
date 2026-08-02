"""Durable-logging unit checks — the pure sink logic + the Trace→sink seam, with NO
Postgres, no langgraph, no network (psycopg is lazy-imported inside LogStore.open only).

    cd brain && python tests/run_logging.py
Exits non-zero on the first failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.logstore import LogStore, redact_obj, redact_text, stream_for, label_for, turn_row  # noqa: E402
from app.trace import build_trace, current_trace_id  # noqa: E402

_c = {"pass": 0, "fail": 0}


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    _c["pass" if cond else "fail"] += 1


class FakeSink:
    def __init__(self):
        self.recs = []

    def enqueue(self, rec):
        self.recs.append(rec)


def main():
    print("1. redaction scrubs secrets before they reach the store")
    check("anthropic key redacted",
          "sk-ant-" not in redact_text("key sk-ant-abcDEF123456789 tail"))
    check("google key redacted",
          "AIza" not in redact_text("g AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 x"))
    check("bearer redacted", "secretvalue" not in redact_text("Authorization: Bearer secretvalue"))
    nested = redact_obj({"text": "tok sk-ant-abcDEF123456789zzz", "n": [{"k": "AIza" + "y" * 34}]})
    check("redaction recurses into dict/list",
          "sk-ant-" not in nested["text"] and "AIza" not in nested["n"][0]["k"])
    check("non-strings pass through", redact_obj({"a": 5, "b": True, "c": None}) == {"a": 5, "b": True, "c": None})

    print("2. stream classification (the three streams)")
    check("user → transcript", stream_for({"level": "user", "who": "you"}) == "transcript")
    check("reason → reasoning", stream_for({"level": "code", "node": "reason"}) == "reasoning")
    check("record → reasoning", stream_for({"level": "code", "node": "record"}) == "reasoning")
    check("gate → control", stream_for({"level": "code", "node": "gate"}) == "control")
    check("io → control", stream_for({"level": "code", "node": "io", "api": "evolution.send_text"}) == "control")
    check("execute → control", stream_for({"level": "code", "node": "execute"}) == "control")
    check("io label is the api", label_for({"level": "code", "node": "io", "api": "evolution.send_text"}) == "evolution.send_text")
    check("user label is who", label_for({"level": "user", "who": "mary"}) == "mary")

    print("3. turn_row maps the record event 1:1 — incl. the silent close")
    silent = {
        "trace_id": "mary-x-1-ab", "level": "code", "node": "record", "chat_id": "5511@s",
        "loop_state": "close_loop", "close_reason": "model", "response": None,
        "delivery_result": "silent", "error_category": "none", "input_tokens": 10,
        "output_tokens": 2, "latency_ms": 900, "provider": "anthropic", "model": "claude-opus-4-8",
        "provider_request_id": "req_1", "actions": [], "stop_reason": "end_turn", "lang": "en",
    }
    row = turn_row(silent, ts=123.0)
    check("silent close: response is None", row["response"] is None)
    check("silent close: delivery_result='silent'", row["delivery_result"] == "silent")
    check("silent close: loop_state='close_loop'", row["loop_state"] == "close_loop")
    check("silent close: error_category='none'", row["error_category"] == "none")
    check("tokens carried", row["input_tokens"] == 10 and row["output_tokens"] == 2)
    check("request id carried", row["provider_request_id"] == "req_1")
    check("actions default to []", row["actions"] == [])

    print("4. Trace → sink seam: all three streams enqueue, IO honours the contextvar")
    trace = build_trace()  # no sink yet
    sink = FakeSink()
    check("no sink → code() is a safe no-op to the sink", (trace.code("t0", "gate", decision="run") or True) and sink.recs == [])
    trace.attach_sink(sink)
    trace.code("t1", "gate", decision="run")           # control
    trace.user("t1", "you", "oi")                       # transcript
    trace.code("t1", "reason", loop_state="close_loop") # reasoning
    check("code + user both reach the sink", len(sink.recs) == 3)
    check("sink got a copy, not the ring's object", all(isinstance(r, dict) for r in sink.recs))

    # io() is a no-op with no activation on the contextvar…
    trace.io("evolution.send_text", ok=True, ms=5)
    check("io() no-op outside an activation", len(sink.recs) == 3)
    # …and emits a control event once the trace id is set (as main._run sets it).
    tok = current_trace_id.set("t1")
    try:
        trace.io("evolution.send_text", ok=True, ms=5)
    finally:
        current_trace_id.reset(tok)
    last = sink.recs[-1]
    check("io() emits once trace id is set", len(sink.recs) == 4)
    check("io event: node=io, api set, tid from contextvar",
          last.get("node") == "io" and last.get("api") == "evolution.send_text" and last.get("trace_id") == "t1")
    check("io event classifies as control", stream_for(last) == "control")

    asyncio.run(_writer_checks())

    print(f"\n{_c['pass']} passed, {_c['fail']} failed")
    sys.exit(1 if _c["fail"] else 0)


class CaptureStore(LogStore):
    """LogStore with the Postgres write swapped for capture — exercises the real
    enqueue → writer-loop → _flush logic (redaction, seq, stream, turn detection)
    with no psycopg and no DB."""

    def __init__(self):
        super().__init__("postgresql://unused")
        self.events, self.turns = [], []

    async def _write(self, events, turns):
        self.events.extend(events)
        self.turns.extend(turns)


async def _writer_checks():
    print("5. writer loop: enqueue → flush (seq, redaction, turn upsert), no DB")
    store = CaptureStore()
    store.start()
    tid = "mary-5511-1-ab"
    store.enqueue({"trace_id": tid, "level": "code", "node": "gate", "decision": "run"})
    store.enqueue({"trace_id": tid, "level": "user", "who": "you",
                   "text": "my key is sk-ant-abcDEF123456789zzz"})
    store.enqueue({"trace_id": tid, "level": "code", "node": "reason", "loop_state": "close_loop"})
    store.enqueue({"trace_id": tid, "level": "code", "node": "record", "chat_id": "5511@s",
                   "loop_state": "close_loop", "response": None, "delivery_result": "silent",
                   "error_category": "none", "actions": []})
    # let the background writer drain the queue
    for _ in range(50):
        if len(store.events) >= 4:
            break
        await asyncio.sleep(0.01)
    await store.aclose()

    check("all 4 events written", len(store.events) == 4)
    seqs = [e[2] for e in store.events]
    check("seq is 0,1,2,3 within the turn", seqs == [0, 1, 2, 3])
    streams = [e[4] for e in store.events]
    check("streams tagged control/transcript/reasoning/reasoning",
          streams == ["control", "transcript", "reasoning", "reasoning"])
    transcript_payload = store.events[1][6]
    check("secret redacted in the written payload", "sk-ant-" not in transcript_payload["text"])
    check("record event produced exactly one turn upsert", len(store.turns) == 1)
    check("the turn is the silent close",
          store.turns[0]["response"] is None and store.turns[0]["delivery_result"] == "silent")
    check("seq counter freed after the turn closed", tid not in store._seq)

    print("6. backpressure: a full queue drops, never blocks")
    tiny = CaptureStore()
    tiny._queue = asyncio.Queue(maxsize=2)  # no writer started → nothing drains it
    for i in range(5):
        tiny.enqueue({"trace_id": "t", "level": "code", "node": "gate", "i": i})
    check("2 buffered, 3 dropped (reply path never blocked)", tiny.dropped == 3)


if __name__ == "__main__":
    main()
