"""Skill-owned response assembly.

After `execute` runs a skill's action, how the result becomes the WhatsApp reply is the skill's
choice — not a graph default. Two implementations ship:

  LLMReadback  — the tool result reads back into a SECOND reason call (reason ②) that writes the
                 message from what actually happened. This is the calendar behaviour today; the
                 `respond` node routes to `reason`, and no text is assembled here.
  Programmatic — a pure formatter: build the reply string from the action results with no second
                 model call. The `respond` node calls `assemble(...)` and goes straight to `act`.

A native skill (web) has no `execute`, so it has no render policy (render=None) — its reply is
simply what its single reason call wrote."""
from __future__ import annotations

from typing import Callable


class LLMReadback:
    """Route the tool result back into a reason call, which writes the reply. No local assembly."""

    mode = "llm"


class Programmatic:
    """Deterministic reply assembly from the action results — no second model call.

    `fmt(results, state) -> str` receives the list of ActionResult dicts collected this loop and
    the graph state, and returns the message text to send."""

    mode = "code"

    def __init__(self, fmt: Callable[[list, dict], str]) -> None:
        self.fmt = fmt

    async def assemble(self, *, results: list, state: dict) -> str:
        return self.fmt(results, state)
