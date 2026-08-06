"""execute — run the (already-confirmed) actions, then hand off to `respond`.

Runs each action in state["actions"] in order through its skill's handler and appends every result
to the thread as an observation. Confirmation is no longer here — it ran at the `confirm` node,
owned by the skill. One structural gate remains, because it is tool safety rather than user
confirmation:

  resolved-id gate — update/delete only run if their event_id was surfaced by a find/list earlier
                     in THIS loop (seen_event_ids). No invented ids reach Google.

Whether the result reads back through a second reason call, or is formatted programmatically, is
the skill's `render` policy — decided next, at the `respond` node."""
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


def _collect_views(result: dict) -> dict:
    """Full event views a result surfaced ({id: view}) — feeds the programmatic confirmation
    for update/delete (title/time), so the model never has to compose that text."""
    out: dict = {}
    data = result.get("data") or {}
    if isinstance(data, dict):
        for it in data.get("items") or []:
            if isinstance(it, dict) and it.get("event_id"):
                out[it["event_id"]] = it
        if data.get("event_id") and data.get("title"):
            out[data["event_id"]] = data
    return out


async def execute_node(state: MessageState, *, tools: dict, settings, trace: Trace) -> dict:
    tid = state["trace_id"]
    actions = state.get("actions") or []
    seen: list[str] = list(state.get("seen_event_ids") or [])
    seen_events: dict = dict(state.get("seen_events") or {})
    hops = int(state.get("tool_hops") or 0) + 1

    results: list[dict] = []
    observations: list[dict] = []
    any_read = False
    any_fail = False

    for action in actions:
        task = (action or {}).get("task", "")
        domain, _, verb = task.partition(".")
        inputs = {k: v for k, v in (action or {}).items() if k != "task"}

        # resolved-id gate — update/delete must target an event surfaced by a prior search
        if verb in ("update", "delete") and inputs.get("event_id") not in seen:
            res = {"ok": False, "error": "unresolved_id",
                   "summary": f"Cannot {verb}: that event was not found via a prior search — "
                              f"run find first, then {verb} the id it returns."}
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
                seen_events.update(_collect_views(res))

        res = dict(res or {})
        res.setdefault("task", task)
        if not res.get("ok"):
            any_fail = True
        results.append(res)
        observations.append({
            "role": "user",
            "content": f"[{task} result] {res.get('summary', '')}".rstrip(),
        })

    trace.code(
        tid, node="execute", loop_id=state.get("loop_id"),
        hops=hops, ran=len(results), any_read=any_read, any_fail=any_fail,
        results=[{"task": r.get("task"), "ok": r.get("ok"), "error": r.get("error")}
                 for r in results],
    )

    return {
        "messages": observations,
        "action_results": (state.get("action_results") or []) + results,
        "seen_event_ids": seen,
        "seen_events": seen_events,
        "tool_hops": hops,
        "last_ran": len(results),
        "last_results": results,
        # Clear the directives so a read-back reason pass starts from a clean slate.
        "actions": [],
    }
