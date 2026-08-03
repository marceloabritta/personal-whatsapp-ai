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
        # The enforced-JSON schema is built from the tool registry and injected here. Fall
        # back to building it directly so the reasoner is usable without going through deps.
        if output_schema is None:
            from ..tools.registry import build_output_schema

            output_schema = build_output_schema()
        self.output_schema = output_schema
        self._client = None  # lazy — so importing this module never needs a key

    def _client_or_make(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key)
        return self._client

    def _tools(self) -> list[dict]:
        # Web tools OFF. On Sonnet 5 with thinking disabled they get invoked on every
        # calendar turn (code_execution hops) — ~4x input tokens, ~2x latency. Winning
        # config = lean, no web tools. Re-add on demand via a declared action, not upfront.
        return []

    async def respond(self, *, system: str, messages: list) -> ReasonResult:
        client = self._client_or_make()
        convo = [{"role": m["role"], "content": m["content"]} for m in messages]
        tool_names: list[str] = []
        usage = {"input": 0, "output": 0}
        resp = None

        try:
            for _ in range(_MAX_TOOL_HOPS):
                kwargs = dict(
                    model=self.s.claude_model,
                    max_tokens=self.s.claude_max_tokens,
                    system=system,
                    messages=convo,
                    # TESTING SPEED IMPROVEMENT: no adaptive thinking (Sonnet 5 default run).
                    thinking={"type": "disabled"},
                    output_config={
                        "effort": self.s.claude_effort,
                        "format": {"type": "json_schema", "schema": self.output_schema},
                    },
                )
                # Only attach tools when there are any (web tools removed for the speed test).
                tools = self._tools()
                if tools:
                    kwargs["tools"] = tools
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
