"""Skill-owned confirmation policies.

A policy decides whether a pending mutating action may run this turn. The `confirm` node only
delegates to it — the rule lives in the skill, not in the graph. Two implementations ship:

  FlagConfirm  — programmatic; the calendar behaviour we have today (a write runs only once the
                 model has set inputs.confirmed=true, after the owner said yes).
  LLMConfirm   — a v2 seam: the skill makes its own scoped model call to judge a go-ahead from
                 the conversation. Declared here so a future skill can opt in without touching
                 the graph; not used in v1."""
from __future__ import annotations

from typing import Any, Optional, Protocol, TypedDict, runtime_checkable


class ConfirmDecision(TypedDict, total=False):
    ok: bool                 # may this action proceed to execute?
    message: Optional[str]   # optional prompt the skill authors itself (else the node frames one)
    reason: str              # short classification, for the trace


@runtime_checkable
class ConfirmPolicy(Protocol):
    needs: set               # verbs this policy gates; others pass straight through
    async def confirm(self, *, action: dict, state: dict, deps: Any) -> ConfirmDecision: ...


class FlagConfirm:
    """Programmatic: a gated verb runs only when the model set inputs.confirmed=true — exactly
    the structural gate the calendar has today, now owned by the calendar skill."""

    def __init__(self, needs) -> None:
        self.needs = set(needs)

    async def confirm(self, *, action: dict, state: dict, deps: Any) -> ConfirmDecision:
        ok = bool(action.get("confirmed"))
        return {"ok": ok, "reason": "flag" if ok else "unconfirmed"}


class LLMConfirm:
    """v2 seam — the skill judges a yes with its own model call. Not wired in v1."""

    def __init__(self, needs) -> None:
        self.needs = set(needs)

    async def confirm(self, *, action: dict, state: dict, deps: Any) -> ConfirmDecision:
        raise NotImplementedError("LLMConfirm is a v2 seam; v1 skills use FlagConfirm or None")
