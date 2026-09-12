"""One turn in, one verdict out. The only place the review layer spends tokens.

Deliberately its OWN Anthropic call rather than a detour through the graph's reasoner: the
reasoner parses the reply-path contract (state/message/actions) and is tuned for the reply path.
Keeping the judge separate means nothing here can ever change how Lisa answers.

The judge model defaults to a DIFFERENT model from the one being graded — a model judging its own
output favours it. Never raises: a failure comes back as a verdict of "error" so the turn is
recorded as unjudged rather than silently skipped."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .prompt import build_judge_prompt, build_turn_message, render_transcript
from .taxonomy import judge_schema, normalise

log = logging.getLogger("mary.review.judge")


class Judge:
    def __init__(self, settings: Any) -> None:
        self.s = settings
        self._client = None  # lazy — importing this module never needs a key
        self._schema = judge_schema()
        self._system = build_judge_prompt(
            owner_name=settings.owner_name, tag=settings.primary_tag
        )

    def _client_or_make(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.s.anthropic_api_key)
        return self._client

    async def judge(self, *, lines: list[dict], reply_text: Optional[str]) -> dict:
        """Grade one turn from the transcript alone.

        `lines` is every transcript line before this turn, oldest first. `reply_text` is what she
        sent, or None when she stayed silent — silence is a decision and is judged too."""
        transcript = render_transcript(lines, max_lines=self.s.review_max_context_lines)
        content = build_turn_message(transcript, reply_text)

        try:
            client = self._client_or_make()
            resp = await client.messages.create(
                model=self.s.review_model,
                max_tokens=self.s.review_max_tokens,
                system=self._system,
                messages=[{"role": "user", "content": content}],
                thinking={"type": "disabled"},
                output_config={
                    "effort": self.s.review_effort,
                    "format": {"type": "json_schema", "schema": self._schema},
                },
            )
        except Exception as exc:  # transport, auth, rate limit — record it, never raise
            log.warning("judge call failed: %s", exc)
            return _error(str(exc)[:400])

        text = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
        )
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            log.error("judge returned unparseable JSON: %r", text[:300])
            return _error("unparseable json")

        out = normalise(raw)
        usage = getattr(resp, "usage", None)
        out["error"] = None
        out["judge_model"] = self.s.review_model
        out["input_tokens"] = getattr(usage, "input_tokens", 0) or 0
        out["output_tokens"] = getattr(usage, "output_tokens", 0) or 0
        return out


def _error(msg: str) -> dict:
    return {
        "verdict": "error", "confidence": "low", "rationale": "", "task_class": None,
        "gaps": [], "proposed_gap": "", "error": msg,
        "judge_model": None, "input_tokens": 0, "output_tokens": 0,
    }
