"""Anthropic behind the seam — official SDK, enforced JSON, native web tools, optional
MCP connector tools, adaptive thinking + effort, and the server-tool pause_turn loop.

The enforced-JSON schema and the MCP servers are injected at construction (built from
the tool registry in deps). Enforced JSON composes with server tools + MCP + thinking."""
from __future__ import annotations

import json
import logging

from .base import ReasonResult

log = logging.getLogger("mary.reasoner.anthropic")

_MAX_TOOL_HOPS = 8  # bound the server-tool / MCP pause_turn loop


class AnthropicReasoner:
    def __init__(self, settings, *, output_schema: dict, mcp_servers: list | None = None) -> None:
        self.s = settings
        self.output_schema = output_schema
        self.mcp_servers = mcp_servers or []
        self._client = None

    def _client_or_make(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key)
        return self._client

    def _tools(self) -> list[dict]:
        n = self.s.web_search_max_uses
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": n},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": n},
        ]
        for srv in self.mcp_servers:  # anthropic_mcp tools → an mcp_toolset each
            tools.append({"type": "mcp_toolset", "mcp_server_name": srv["name"]})
        return tools

    def _fallback(self, usage, request_id, stop_reason, tools, category) -> ReasonResult:
        return ReasonResult(
            lang=None, next_message=None, loop_state="keep_listening", actions=[], workflow=None,
            usage=usage, provider_request_id=request_id, stop_reason=stop_reason,
            tool_calls=tools, error_category=category,
        )

    async def respond(self, *, system: str, messages: list) -> ReasonResult:
        client = self._client_or_make()
        convo = [{"role": m["role"], "content": m["content"]} for m in messages]
        tool_names: list[str] = []
        usage = {"input": 0, "output": 0}
        resp = None

        create_kwargs = dict(
            model=self.s.claude_model,
            max_tokens=self.s.claude_max_tokens,
            system=system,
            tools=self._tools(),
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.s.claude_effort,
                "format": {"type": "json_schema", "schema": self.output_schema},
            },
        )
        if self.mcp_servers:
            create_kwargs["mcp_servers"] = self.mcp_servers
            create_kwargs["betas"] = ["mcp-client-2025-11-20"]

        try:
            for _ in range(_MAX_TOOL_HOPS):
                if self.mcp_servers:
                    resp = await client.beta.messages.create(messages=convo, **create_kwargs)
                else:
                    resp = await client.messages.create(messages=convo, **create_kwargs)
                usage["input"] += getattr(resp.usage, "input_tokens", 0) or 0
                usage["output"] += getattr(resp.usage, "output_tokens", 0) or 0
                for b in resp.content:
                    if getattr(b, "type", None) in ("server_tool_use", "mcp_tool_use"):
                        tool_names.append(getattr(b, "name", "tool"))
                if resp.stop_reason == "pause_turn":
                    convo.append({"role": "assistant", "content": resp.content})
                    continue
                break
        except Exception as exc:
            log.exception("anthropic call failed: %s", exc)
            return self._fallback(usage, None, "error", tool_names, "provider")

        request_id = getattr(resp, "_request_id", None)
        stop_reason = resp.stop_reason
        if stop_reason == "refusal":
            return self._fallback(usage, request_id, stop_reason, tool_names, "none")

        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            log.error("could not parse enforced JSON: %r", text[:300])
            return self._fallback(usage, request_id, stop_reason, tool_names, "provider")

        loop_state = data.get("loop_state")
        if loop_state not in ("keep_listening", "close_loop"):
            loop_state = "keep_listening"
        return ReasonResult(
            lang=data.get("lang"),
            next_message=data.get("next_message"),
            loop_state=loop_state,
            actions=data.get("actions") or [],
            workflow=data.get("workflow"),
            usage=usage, provider_request_id=request_id, stop_reason=stop_reason,
            tool_calls=tool_names, error_category="none",
        )
