"""Anthropic behind the seam — official SDK, enforced JSON, native server tools,
adaptive thinking + effort, the server-tool pause_turn loop, and prompt caching.

Enforced JSON (output_config.format) composes with the server tools and thinking;
only citations/prefill conflict with forced output, and we use neither.

Prompt caching (settings.prompt_cache_enabled). Anthropic reuses a prompt it has read in the
last five minutes, at a tenth of the input price, but only as a PREFIX: tools, then system, then
messages, up to the first byte that differs. Two things are marked here:
  - the system prompt's stable prefixes, where prompt.SystemPrompt says they end. The first is
    the same for every call to a skill, in any chat, so it is read back on nearly every call.
  - the conversation, by the top-level `cache_control` (the API marks the last block and moves
    the mark as the thread grows). It is read back when nothing before it changed: the later
    passes of one activation, and every hop of a server-tool turn, which re-sends all of its
    search results each time.
What was read and written comes back in `usage`; a prompt that stops being read back raises no
error, only the bill, so those two numbers are the only way to see it."""
from __future__ import annotations

import json
import logging

from .base import ReasonResult

log = logging.getLogger("mary.reasoner.anthropic")

_MAX_TOOL_HOPS = 8  # bound the server-tool / MCP pause_turn loop
_MCP_BETA = "mcp-client-2025-11-20"
# The 5-minute entry. Every read renews it for free, and four in five calls to a skill come
# within five minutes of the last; the 1-hour entry costs twice as much to write as this one
# does, and would have caught 2 more calls in 128.
_CACHE = {"type": "ephemeral"}


def system_blocks(system):
    """The system prompt as Anthropic text blocks, cut where its reusable prefixes end, with a
    cache breakpoint on each block that ends one. Joined, the blocks are the prompt, byte for
    byte. A prompt that carries no offsets (a plain str) comes back as it is: sent whole, with
    nothing marked."""
    text = str(system)
    blocks: list[dict] = []
    start = 0
    for end in getattr(system, "stable_ends", ()):
        if start < end < len(text):
            blocks.append({"type": "text", "text": text[start:end], "cache_control": _CACHE})
            start = end
    if not blocks:
        return text
    blocks.append({"type": "text", "text": text[start:]})
    return blocks


def add_usage(usage: dict, u) -> None:
    """Fold one response's token counts into the turn's running totals.

    With caching on, the API's `input_tokens` is only what was neither read from the cache nor
    written to it — on a good turn, a few hundred of eight thousand. `input` here stays what it
    always meant, everything she was sent, and the two cache counts say how much of that was
    read back (billed at a tenth) and how much was stored (billed at a quarter more)."""
    read = getattr(u, "cache_read_input_tokens", 0) or 0
    write = getattr(u, "cache_creation_input_tokens", 0) or 0
    usage["input"] += (getattr(u, "input_tokens", 0) or 0) + read + write
    usage["output"] += getattr(u, "output_tokens", 0) or 0
    usage["cache_read"] += read
    usage["cache_write"] += write


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
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        resp = None
        cache = bool(getattr(self.s, "prompt_cache_enabled", False))

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
                    system=system_blocks(system) if cache else str(system),
                    messages=convo,
                    thinking=thinking,
                    output_config={
                        "effort": effort,
                        "format": {"type": "json_schema", "schema": schema},
                    },
                )
                if cache:
                    kwargs["cache_control"] = _CACHE
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
                add_usage(usage, resp.usage)
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
            call = data.get("call")
            if state not in ("keep_listening", "awaiting_answer", "close"):
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
            actions=actions, workflow=workflow, call=call, usage=usage,
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
