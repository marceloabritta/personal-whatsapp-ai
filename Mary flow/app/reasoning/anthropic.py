"""Anthropic behind the seam — official SDK, enforced JSON, native server tools,
adaptive thinking + effort, and the server-tool pause_turn loop.

Enforced JSON (output_config.format) composes with the server tools and thinking;
only citations/prefill conflict with forced output, and we use neither."""
from __future__ import annotations

import json
import logging

from .base import ReasonResult

log = logging.getLogger("mary.reasoner.anthropic")

_MAX_TOOL_HOPS = 8  # bound the server-tool / MCP pause_turn loop
_MCP_BETA = "mcp-client-2025-11-20"


class AnthropicReasoner:
    def __init__(self, settings, output_schema: dict | None = None,
                 mcp_servers: list[dict] | None = None) -> None:
        self.s = settings
        self.mcp_servers = mcp_servers or []
        # Default enforced-JSON schema, used only when respond() is called without a per-call
        # schema. The reason node always passes the routed skill's schema, so this is a fallback;
        # build the calendar (local) schema so the reasoner is usable standalone.
        if output_schema is None:
            from ..skills import output_schema_for

            output_schema = output_schema_for("calendar")
        self.output_schema = output_schema
        self._client = None  # lazy — so importing this module never needs a key

    def _client_or_make(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key)
        return self._client

    async def respond(
        self, *, system: str, messages: list,
        output_schema: dict | None = None, server_tools: list | None = None,
        model: str | None = None, effort: str | None = None, think: bool = False,
    ) -> ReasonResult:
        client = self._client_or_make()
        convo = [{"role": m["role"], "content": m["content"]} for m in messages]
        schema = output_schema or self.output_schema
        tool_names: list[str] = []
        usage = {"input": 0, "output": 0}
        resp = None

        # Per-skill runtime (resolved by the caller, settings defaults already applied). A skill sets
        # think=True to get a real thinking channel — the web path uses it because its long
        # server-tool turns were degenerating the forced-JSON output into silence. Under a forced
        # output_config Sonnet 5 only accepts ADAPTIVE thinking, whose depth is set by effort (the
        # "enabled" + budget_tokens form is rejected for this model); off stays disabled for speed.
        model = model or self.s.claude_model
        effort = effort or self.s.claude_effort
        thinking = {"type": "adaptive"} if think else {"type": "disabled"}

        try:
            for _ in range(_MAX_TOOL_HOPS):
                kwargs = dict(
                    model=model,
                    max_tokens=self.s.claude_max_tokens,
                    system=system,
                    messages=convo,
                    thinking=thinking,
                    output_config={
                        "effort": effort,
                        "format": {"type": "json_schema", "schema": schema},
                    },
                )
                # Native server tools are attached PER CALL by the routed skill (web loads
                # web_search/web_fetch; calendar loads none — saving the ~6.4k-token overhead).
                if server_tools:
                    kwargs["tools"] = server_tools
                # MCP tools (if any registered) ride the beta connector API. Empty today,
                # so the normal path runs; the branch activates when an MCP tool is added.
                if self.mcp_servers:
                    resp = await client.beta.messages.create(
                        **kwargs, mcp_servers=self.mcp_servers, betas=[_MCP_BETA],
                    )
                else:
                    resp = await client.messages.create(**kwargs)
                usage["input"] += getattr(resp.usage, "input_tokens", 0) or 0
                usage["output"] += getattr(resp.usage, "output_tokens", 0) or 0
                for block in resp.content:
                    if getattr(block, "type", None) == "server_tool_use":
                        tool_names.append(getattr(block, "name", "tool"))
                if resp.stop_reason == "pause_turn":
                    convo.append({"role": "assistant", "content": resp.content})
                    continue
                break
        except Exception as exc:  # any SDK/transport error → safe silence
            log.exception("anthropic call failed: %s", exc)
            return ReasonResult(
                state="keep_listening", message=None, usage=usage,
                provider_request_id=None, stop_reason="error",
                tool_calls=tool_names, error_category="provider",
            )

        request_id = getattr(resp, "_request_id", None)
        stop_reason = resp.stop_reason

        if stop_reason == "refusal":  # declined — stay quiet, keep listening
            return ReasonResult(
                state="keep_listening", message=None, lang=None, usage=usage,
                provider_request_id=request_id, stop_reason=stop_reason,
                tool_calls=tool_names, error_category="none",
            )

        text = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
        )
        try:
            data = json.loads(text)
            state = data.get("state") or "keep_listening"
            message = data.get("message")
            lang = data.get("lang")
            reasoning = data.get("reasoning")  # the model's stated rationale, for the log
            actions = data.get("actions") or []
            workflow = data.get("workflow")
            if state not in ("keep_listening", "close"):
                state = "keep_listening"
        except (ValueError, TypeError):
            log.error("could not parse enforced JSON: %r", text[:300])
            return ReasonResult(
                state="keep_listening", message=None, lang=None, usage=usage,
                provider_request_id=request_id, stop_reason=stop_reason,
                tool_calls=tool_names, error_category="provider",
            )

        return ReasonResult(
            state=state, message=message, lang=lang, reasoning=reasoning,
            actions=actions, workflow=workflow, usage=usage,
            provider_request_id=request_id, stop_reason=stop_reason,
            tool_calls=tool_names, error_category="none",
        )

    async def classify(
        self, *, system: str, messages: list, schema: dict,
        max_tokens: int = 32, effort: str = "low",
    ) -> dict:
        """One cheap enforced-JSON call, no tools, over a neutral conversation, returning the raw
        parsed object. Used by the router's domain classifier; raises on failure so the caller
        falls back to a default."""
        client = self._client_or_make()
        convo = [{"role": m["role"], "content": m["content"]} for m in messages]
        resp = await client.messages.create(
            model=self.s.claude_model,
            max_tokens=max_tokens,
            system=system,
            messages=convo,
            thinking={"type": "disabled"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
        out = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
        )
        return json.loads(out)
