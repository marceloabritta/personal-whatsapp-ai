"""reason — call the provider-neutral Reasoner over the thread, get enforced JSON.

Skill-scoped: `state["domain"]` (set by `route`) picks the skill, and everything is that one
skill's — the system prompt, the enforced-output schema, and the native server tools attached to
the call (calendar attaches none; web loads web_search/web_fetch). Converts the checkpointed
messages to the neutral [{role, content}] shape and calls the reasoner. On a calendar readback
this is `reason ②`, writing the reply from the tool result (see the respond node)."""
from __future__ import annotations

import time

from ..skills import output_schema_for, server_tools_for, system_prompt_for
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
    domain = state.get("domain") or settings.default_domain
    locked_lang = state.get("session_lang")  # set once per loop; None right after a fresh tag

    system = system_prompt_for(domain, settings, session_lang=locked_lang)
    schema = output_schema_for(domain)
    server_tools = server_tools_for(domain, settings)
    convo = _to_neutral(state.get("messages"))

    t0 = time.monotonic()
    result = await reasoner.respond(
        system=system, messages=convo, output_schema=schema, server_tools=server_tools,
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    llm_state = result.get("state") or "keep_listening"
    message = result.get("message")
    usage = result.get("usage") or {}

    actions = result.get("actions") or []
    workflow = result.get("workflow")

    # Reasoning stream: the model's own turn — its stated rationale, the enforced-JSON decision,
    # and the metadata for the record.
    trace.code(
        tid, node="reason", loop_id=state.get("loop_id"), domain=domain,
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
    """Actions to run → confirm (the skill gates them); otherwise straight to act (send/close)."""
    return "confirm" if state.get("actions") else "act"
