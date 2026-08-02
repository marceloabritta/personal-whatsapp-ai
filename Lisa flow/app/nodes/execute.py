"""execute — run the model's actions, then route.

Runs each action in state["actions"] in order through its local handler, appends every result
to the thread as an observation the model reads back, and enforces two structural gates that
do not depend on the prompt behaving:

  1. resolved-id gate  — update/delete only run if their event_id was surfaced by a find/list
                         earlier in THIS loop (seen_event_ids). No invented ids reach Google.
  2. confirm gate      — a confirm_first verb (create/update/delete) only runs when the model
                         has set inputs.confirmed=true (a go-ahead this loop).

Read-back rule: a read (list/find), a failure, or a blocked gate routes back to `reason` so
the model reads the observation and speaks truthfully — bounded by settings.max_tool_actions
so the loop can never spin. A clean write goes straight to `act`."""
from __future__ import annotations

from ..state import MessageState
from ..trace import Trace

_READ_VERBS = {"list", "find"}


def _collect_ids(result: dict) -> list[str]:
    """Event ids a result surfaced — so a later update/delete can be gated on them."""
    ids: list[str] = []
    data = result.get("data") or {}
    if isinstance(data, dict):
        if data.get("event_id"):
            ids.append(data["event_id"])
        for it in data.get("items") or []:
            if isinstance(it, dict) and it.get("event_id"):
                ids.append(it["event_id"])
    return ids


async def execute_node(
    state: MessageState, *, tools: dict, confirm_first: dict, settings, trace: Trace
) -> dict:
    tid = state["trace_id"]
    actions = state.get("actions") or []
    seen: list[str] = list(state.get("seen_event_ids") or [])
    hops = int(state.get("tool_hops") or 0) + 1

    results: list[dict] = []
    observations: list[dict] = []
    any_read = False
    any_fail = False

    for action in actions:
        task = (action or {}).get("task", "")
        domain, _, verb = task.partition(".")
        inputs = {k: v for k, v in (action or {}).items() if k != "task"}

        # gate 1 — resolved-id (update/delete must target an event surfaced by a prior search)
        if verb in ("update", "delete") and inputs.get("event_id") not in seen:
            res = {"ok": False, "error": "unresolved_id",
                   "summary": f"Cannot {verb}: that event was not found via a prior search — "
                              f"run find first, then {verb} the id it returns."}
        # gate 2 — confirm before write
        elif verb in confirm_first.get(domain, set()) and not inputs.get("confirmed"):
            res = {"ok": False, "error": "unconfirmed",
                   "summary": f"Not executed — {task} needs the owner's go-ahead first. "
                              f"Restate the plan and wait for confirmation."}
        else:
            handler = tools.get(domain)
            if handler is None:
                res = {"ok": False, "error": "unknown_tool",
                       "summary": f"No handler for {task}."}
            else:
                res = await handler.run(verb, inputs)
                if verb in _READ_VERBS:
                    any_read = True
                seen.extend(i for i in _collect_ids(res) if i not in seen)

        res = dict(res or {})
        res.setdefault("task", task)
        if not res.get("ok"):
            any_fail = True
        results.append(res)
        observations.append({
            "role": "user",
            "content": f"[{task} result] {res.get('summary', '')}".rstrip(),
        })

    # Read, failure, or blocked gate → let the model read the result and reply truthfully,
    # bounded so it can't spin. A clean write goes straight to act.
    readback = (any_read or any_fail) and hops < settings.max_tool_actions

    trace.code(
        tid, node="execute", loop_id=state.get("loop_id"),
        hops=hops, ran=len(results), any_read=any_read, any_fail=any_fail,
        readback=readback, results=[{"task": r.get("task"), "ok": r.get("ok"),
                                     "error": r.get("error")} for r in results],
    )

    return {
        "messages": observations,
        "action_results": (state.get("action_results") or []) + results,
        "seen_event_ids": seen,
        "tool_hops": hops,
        "needs_readback": readback,
        # Clear the directives so a read-back reason pass starts from a clean slate.
        "actions": [],
    }


def route_after_execute(state: MessageState) -> str:
    return "reason" if state.get("needs_readback") else "act"
