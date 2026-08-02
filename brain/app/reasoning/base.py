"""The reasoning seam. The graph depends only on this — never on a provider SDK."""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict

# The enforced-JSON contract every provider must return the model into.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": ["keep_listening", "close"]},
        "message": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        # ISO 639-1 code of the language the reply is written in (the language of
        # the tagged message that started the session). Drives the reply header.
        "lang": {"type": "string"},
    },
    "required": ["state", "message", "lang"],
    "additionalProperties": False,
}


class ReasonResult(TypedDict, total=False):
    state: str  # "keep_listening" | "close"
    message: Optional[str]  # prose to send, or None (silence)
    lang: Optional[str]  # ISO 639-1 code the reply is written in
    usage: Optional[dict]  # {"input": int, "output": int}
    provider_request_id: Optional[str]
    stop_reason: Optional[str]
    tool_calls: list  # names of server tools used this turn
    error_category: str  # "none" | "provider"


class Reasoner(Protocol):
    async def respond(self, *, system: str, messages: list) -> ReasonResult:
        """Given the system prompt and a neutral [{role, content}] history, return the
        enforced-JSON decision plus metadata. Tool use / thinking / the pause_turn loop
        are the provider's private concern."""
        ...
