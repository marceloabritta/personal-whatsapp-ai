"""reason — call the provider-neutral Reasoner, get the enforced JSON.

Converts the checkpointed messages to a neutral [{role, content}] list, calls the
reasoner, and records its decision (next_message / loop_state / actions / workflow)
onto the state. Only an actual utterance is appended to history as an assistant turn."""
from __future__ import annotations

import time

from ..prompt import build_system_prompt
from ..state import MessageState
from ..trace import Trace


def _to_neutral(messages: list) -> list[dict]:
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        else:
            role = "assistant" if getattr(m, "type", "") == "ai" else "user"
            out.append({"role": role, "content": getattr(m, "content", "")})
    return out


async def reason_node(
    state: MessageState, *, reasoner, settings, tools_prompt: str, task_prompts: str = "",
    trace: Trace
) -> dict:
    tid = state["trace_id"]
    system = build_system_prompt(settings.owner_name, settings.primary_tag, tools_prompt,
                                 task_prompts)
    convo = _to_neutral(state.get("messages"))

    t0 = time.monotonic()
    result = await reasoner.respond(system=system, messages=convo)
    latency_ms = int((time.monotonic() - t0) * 1000)

    loop_state = result.get("loop_state") or "keep_listening"
    next_message = result.get("next_message")
    actions = result.get("actions") or []
    workflow = result.get("workflow")
    usage = result.get("usage") or {}

    trace.code(
        tid, node="reason", provider=settings.llm_provider, model=settings.claude_model,
        loop_state=loop_state, actions=[a.get("task") for a in actions],
        workflow_task=(workflow or {}).get("task"),
        stop_reason=result.get("stop_reason"), request_id=result.get("provider_request_id"),
        usage=usage, tools=result.get("tool_calls") or [], latency_ms=latency_ms,
    )

    update: dict = {
        "lang": result.get("lang"),
        "next_message": next_message,
        "loop_state": loop_state,
        "actions": actions,
        "workflow": workflow,
        "provider": settings.llm_provider,
        "model": settings.claude_model,
        "provider_request_id": result.get("provider_request_id"),
        "usage": usage,
        "latency_ms": latency_ms,
        "stop_reason": result.get("stop_reason"),
        "tool_calls": result.get("tool_calls") or [],
        "error_category": result.get("error_category") or "none",
    }
    # `act` is the single place a sent message enters history — reason never appends,
    # so every WhatsApp message is recorded exactly once (and silences never are).
    return update


def route_after_reason(state: MessageState) -> str:
    return "execute" if state.get("actions") else "act"
