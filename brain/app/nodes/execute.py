"""execute — run every action in this turn's `actions`, in order.

A task is "domain.verb" → dispatch to that local tool's handler. Each result is
appended to the thread as an observation the model reads on the read-back. Read-back
happens only if an action was a read (search/list) or any failed — a clean write
goes straight to `act` with the message + loop_state the model already chose."""
from __future__ import annotations

import time

from ..state import MessageState
from ..trace import Trace


async def execute_node(
    state: MessageState, *, tools: dict, settings, trace: Trace
) -> dict:
    tid = state["trace_id"]
    actions = state.get("actions") or []
    count = state.get("action_count", 0)

    observations: list[dict] = []
    results: list[dict] = []
    any_read = False
    any_fail = False

    for a in actions:
        task = (a or {}).get("task") or ""
        inputs = (a or {}).get("inputs") or {}
        domain, _, verb = task.partition(".")
        handler = tools.get(domain)
        t0 = time.monotonic()
        if handler is None:
            res = {"ok": False, "summary": f"no handler for {task}", "error": "unknown_tool"}
        else:
            res = await handler.run(verb, inputs)
        ms = int((time.monotonic() - t0) * 1000)
        ok = bool(res.get("ok"))
        any_read = any_read or verb == "list"
        any_fail = any_fail or not ok
        count += 1
        observations.append({"role": "user", "content": f"[{task} result] {res.get('summary','')}"})
        results.append({
            "task": task, "ok": ok,
            "event_id": (res.get("data") or {}).get("event_id"),
            "error": res.get("error"), "need": res.get("need"),
        })
        # inputs are recorded so a tool run can be replayed; the sink redacts secrets.
        trace.code(tid, node="execute", task=task, ok=ok, inputs=inputs, ms=ms,
                   error=res.get("error"), event_id=(res.get("data") or {}).get("event_id"))

    readback = (any_read or any_fail) and count < settings.max_tool_actions
    return {
        "messages": observations,
        "action_results": (state.get("action_results") or []) + results,
        "action_count": count,
        "_readback": readback,
        # After a read-back the model speaks again, so clear this turn's directives:
        "actions": [] if readback else actions,
    }


def route_after_execute(state: MessageState) -> str:
    return "reason" if state.get("_readback") else "act"
