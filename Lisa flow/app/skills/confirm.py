"""Skill-owned confirmation policies.

A confirm policy owns the whole confirmation step for a skill, all of it programmatic by default:

  needs               which verbs need a go-ahead before they run
  compose(action, st) build the confirmation PROMPT the owner sees (per verb) — no LLM writes it
  detect(text)        decide if the owner's reply is a clean "yes" (classify_confirmation)
  confirm(action)     the structural gate — may this action run this turn?

`FlagConfirm` is the calendar policy: a write runs only when `confirmed` is set (by the owner's
yes, resolved in code); it composes the confirmation from per-verb formatters the skill supplies.
`LLMConfirm` is a v2 seam (a skill judging the yes with its own model call), not wired in v1."""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, TypedDict, runtime_checkable

from ..intent import classify_confirmation


class ConfirmDecision(TypedDict, total=False):
    ok: bool                 # may this action proceed to execute?
    message: Optional[str]   # optional prompt the skill authors itself (else the node frames one)
    reason: str              # short classification, for the trace


@runtime_checkable
class ConfirmPolicy(Protocol):
    needs: set
    def compose(self, action: dict, state: dict) -> Optional[str]: ...
    def detect(self, text: str) -> str: ...
    async def confirm(self, *, action: dict, state: dict, deps: Any) -> ConfirmDecision: ...


class FlagConfirm:
    """Programmatic: a gated verb runs only when the model set inputs.confirmed=true — the yes is
    resolved in code (detect) and the confirmation prompt is composed in code (compose)."""

    def __init__(self, needs, compose_map: Optional[dict[str, Callable]] = None) -> None:
        self.needs = set(needs)
        # {verb: fn(action, state) -> str} — the skill's per-verb confirmation formatters.
        self._compose = dict(compose_map or {})

    def compose(self, action: dict, state: dict) -> Optional[str]:
        """The confirmation prompt for this action, or None if the skill can't build it (→ model)."""
        _, _, verb = (action or {}).get("task", "").partition(".")
        fn = self._compose.get(verb)
        try:
            return fn(action, state) if fn else None
        except Exception:  # any formatting gap → fall back to the model
            return None

    def detect(self, text: str) -> str:
        """"yes" | "other" — a clean affirmative reply? (skill default = the shared classifier)."""
        return classify_confirmation(text)

    async def confirm(self, *, action: dict, state: dict, deps: Any) -> ConfirmDecision:
        ok = bool(action.get("confirmed"))
        return {"ok": ok, "reason": "flag" if ok else "unconfirmed"}


class LLMConfirm:
    """v2 seam — the skill judges a yes with its own model call. Not wired in v1."""

    def __init__(self, needs) -> None:
        self.needs = set(needs)

    def compose(self, action: dict, state: dict) -> Optional[str]:
        return None

    def detect(self, text: str) -> str:
        return classify_confirmation(text)

    async def confirm(self, *, action: dict, state: dict, deps: Any) -> ConfirmDecision:
        raise NotImplementedError("LLMConfirm is a v2 seam; v1 skills use FlagConfirm or None")
