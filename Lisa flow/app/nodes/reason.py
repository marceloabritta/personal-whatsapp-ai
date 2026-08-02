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


async def reason_node(
    state: MessageState, *, reasoner, settings, trace: Trace,
    tools_prompt: str = "", task_prompts: str = "",
) -> dict:
    tid = state["trace_id"]
    locked_lang = state.get("session_lang")  # set once per loop; None right after a fresh tag
    system = build_system_prompt(
        settings.owner_name, settings.primary_tag,
        tools_prompt=tools_prompt, task_prompts=task_prompts, session_lang=locked_lang,
    )
    convo = _to_neutral(state.get("messages"))

    t0 = time.monotonic()
    result = await reasoner.respond(system=system, messages=convo)
    latency_ms = int((time.monotonic() - t0) * 1000)

    llm_state = result.get("state") or "keep_listening"
    message = result.get("message")
    usage = result.get("usage") or {}

    actions = result.get("actions") or []
    workflow = result.get("workflow")

    # Reasoning stream: the model's own turn — its stated rationale, the enforced-JSON
    # decision it reached, and the metadata for the record.
    trace.code(
        tid, node="reason", loop_id=state.get("loop_id"),
        provider=settings.llm_provider, model=settings.claude_model,
        state=llm_state, message=message, lang=result.get("lang"),
        reasoning=result.get("reasoning"), stop_reason=result.get("stop_reason"),
        request_id=result.get("provider_request_id"), usage=usage,
        tools=result.get("tool_calls") or [], actions=actions, latency_ms=latency_ms,
    )

    # NB: history is appended in `act` (the single place a *sent* message enters the thread),
    # so a read-back reason pass never leaves a phantom, undelivered assistant turn behind.
    update: dict = {
        "llm_state": llm_state,
        "reply_body": message,
        "lang": result.get("lang"),
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
    # Lock the session language on the FIRST pass after a tag (when it isn't set yet), so every
    # later read-back pass and window continuation is told to keep writing in it.
    if not locked_lang and result.get("lang"):
        update["session_lang"] = result["lang"]
    return update


def route_after_reason(state: MessageState) -> str:
    """Actions to run → execute; otherwise straight to act (send/close)."""
    return "execute" if state.get("actions") else "act"
