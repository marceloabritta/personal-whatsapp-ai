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


def _collect_chat_keys(result: dict) -> list[str]:
    """Chat keys a result surfaced (`data.seen_keys`) — so a later setup write can be gated on
    them. The generic twin of `_collect_ids`: any skill can publish resolved ids this way."""
    data = result.get("data") or {}
    keys = data.get("seen_keys") if isinstance(data, dict) else None
    return [k for k in (keys or []) if isinstance(k, str) and k]


def _collect_chat_views(result: dict) -> dict:
    """{chat_key: view} a result surfaced — carries the jid, label and kind a write needs, so
    none of it has to be guessed or re-derived from the key."""
    out: dict = {}
    data = result.get("data") or {}
    if not isinstance(data, dict):
        return out
    for c in data.get("candidates") or []:
        if isinstance(c, dict) and c.get("chat_key"):
            out[c["chat_key"]] = c
    for bucket in ("contacts", "groups"):
        for r in data.get(bucket) or []:
            if isinstance(r, dict) and r.get("chat_key"):
                out[r["chat_key"]] = r
    if data.get("chat_key"):
        out[data["chat_key"]] = data
    return out


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


async def execute_node(state: MessageState, *, tools: dict, resolve_gates: dict | None = None,
                       settings, trace: Trace) -> dict:
    tid = state["trace_id"]
    actions = state.get("actions") or []
    seen: list[str] = list(state.get("seen_event_ids") or [])
    seen_events: dict = dict(state.get("seen_events") or {})
    seen_chat_keys: list[str] = list(state.get("seen_chat_keys") or [])
    seen_chats: dict = dict(state.get("seen_chats") or {})
    listed_chats: dict = dict(state.get("listed_chats") or {})
    gates = resolve_gates or {}
    hops = int(state.get("tool_hops") or 0) + 1

    results: list[dict] = []
    observations: list[dict] = []
    any_read = False
    any_fail = False

    for action in actions:
        task = (action or {}).get("task", "")
        domain, _, verb = task.partition(".")
        # Underscore keys are the graph's own bookkeeping (the approval stamp), never tool
        # inputs — strip them so nothing internal reaches a handler or Google.
        inputs = {k: v for k, v in (action or {}).items()
                  if k != "task" and not k.startswith("_")}

        # Resolved-id gate — tool safety, not user confirmation. The RULE belongs to the skill
        # (calendar: an event surfaced by a prior search; setup: a chat surfaced by a card, a
        # group pick or the last list), so the skill supplies it and this node just applies it.
        # A gate may also PATCH the inputs — that is how "edit 5" becomes a chat key.
        gate = gates.get(domain)
        gate_error = None
        if gate is not None:
            patched, gate_error = gate(verb, inputs, {**state, "seen_event_ids": seen,
                                                     "seen_chat_keys": seen_chat_keys,
                                                     "seen_chats": seen_chats,
                                                     "listed_chats": listed_chats})
            if patched is not None:
                inputs = patched
        if gate_error:
            res = {"ok": False, **gate_error}
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
                seen_chat_keys.extend(
                    k for k in _collect_chat_keys(res) if k not in seen_chat_keys)
                seen_chats.update(_collect_chat_views(res))
                ordinals = (res.get("data") or {}).get("ordinals")
                if isinstance(ordinals, dict):
                    listed_chats = dict(ordinals)  # a new list REPLACES the old numbering

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
        "seen_chat_keys": seen_chat_keys,
        "seen_chats": seen_chats,
        "listed_chats": listed_chats,
        "tool_hops": hops,
        "last_ran": len(results),
        "last_results": results,
        # Clear the directives so a read-back reason pass starts from a clean slate.
        "actions": [],
    }
