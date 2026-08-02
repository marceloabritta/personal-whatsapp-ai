"""reason — call the provider-neutral Reasoner over the thread, get enforced JSON.

Converts the checkpointed messages to the neutral [{role, content}] shape, calls the
reasoner, and (only if she spoke) appends her reply as an assistant turn."""
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
        else:  # a langchain BaseMessage (restored from the checkpoint)
            role = "assistant" if getattr(m, "type", "") == "ai" else "user"
            out.append({"role": role, "content": getattr(m, "content", "")})
    return out


async def reason_node(state: MessageState, *, reasoner, settings, trace: Trace) -> dict:
    tid = state["trace_id"]
    system = build_system_prompt(settings.owner_name, settings.primary_tag)
    convo = _to_neutral(state.get("messages"))

    t0 = time.monotonic()
    result = await reasoner.respond(system=system, messages=convo)
    latency_ms = int((time.monotonic() - t0) * 1000)

    llm_state = result.get("state") or "keep_listening"
    message = result.get("message")
    usage = result.get("usage") or {}

    trace.code(
        tid, node="reason", provider=settings.llm_provider, model=settings.claude_model,
        state=llm_state, stop_reason=result.get("stop_reason"),
        request_id=result.get("provider_request_id"), usage=usage,
        tools=result.get("tool_calls") or [], latency_ms=latency_ms,
    )

    update: dict = {
        "llm_state": llm_state,
        "reply_body": message,
        "provider": settings.llm_provider,
        "model": settings.claude_model,
        "provider_request_id": result.get("provider_request_id"),
        "usage": usage,
        "latency_ms": latency_ms,
        "stop_reason": result.get("stop_reason"),
        "tool_calls": result.get("tool_calls") or [],
        "error_category": result.get("error_category") or "none",
    }
    if message:  # only record actual utterances in history
        update["messages"] = [{"role": "assistant", "content": message}]
    return update
