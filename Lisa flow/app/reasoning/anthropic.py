"""Anthropic behind the seam — official SDK, enforced JSON, native server tools,
adaptive thinking + effort, and the server-tool pause_turn loop.

Enforced JSON (output_config.format) composes with the server tools and thinking;
only citations/prefill conflict with forced output, and we use neither."""
from __future__ import annotations

import json
import logging

from .base import OUTPUT_SCHEMA, ReasonResult

log = logging.getLogger("mary.reasoner.anthropic")

_MAX_TOOL_HOPS = 6  # bound the server-tool pause_turn loop


class AnthropicReasoner:
    def __init__(self, settings) -> None:
        self.s = settings
        self._client = None  # lazy — so importing this module never needs a key

    def _client_or_make(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key)
        return self._client

    def _tools(self) -> list[dict]:
        # No tools of our own — enable the model's server-side web tools. The
        # _20260209 variants carry dynamic filtering, so we do NOT also declare
        # code_execution (that would create a second execution env and confuse it).
        n = self.s.web_search_max_uses
        return [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": n},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": n},
        ]

    async def respond(self, *, system: str, messages: list) -> ReasonResult:
        client = self._client_or_make()
        convo = [{"role": m["role"], "content": m["content"]} for m in messages]
        tool_names: list[str] = []
        usage = {"input": 0, "output": 0}
        resp = None

        try:
            for _ in range(_MAX_TOOL_HOPS):
                resp = await client.messages.create(
                    model=self.s.claude_model,
                    max_tokens=self.s.claude_max_tokens,
                    system=system,
                    messages=convo,
                    tools=self._tools(),
                    thinking={"type": "adaptive"},
                    output_config={
                        "effort": self.s.claude_effort,
                        "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                    },
                )
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
            state=state, message=message, lang=lang, reasoning=reasoning, usage=usage,
            provider_request_id=request_id, stop_reason=stop_reason,
            tool_calls=tool_names, error_category="none",
        )
