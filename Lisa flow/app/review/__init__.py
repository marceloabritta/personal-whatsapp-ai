"""Session review — a second model grades every turn once a session closes.

One entry point, three callers: the reaper (loops that went quiet), the close hook (a session
Lisa stepped out of), and the backfill CLI (history). Same code path for all three, which is why
backfilling the past and reviewing the present can never drift apart.

Two independent assessors meet here and nowhere else:
  judge   — reads the chat, transcript only, never sees a clock;
  timing  — reads the clock, pure arithmetic, never sees the text.
Keeping them apart is the point: the judge's opinion of a reply cannot be coloured by how long it
took, and a well-written answer cannot talk the scorer out of a budget breach."""
from __future__ import annotations

import asyncio
import logging

from .judge import Judge
from .store import ReviewStore
from .taxonomy import DEFAULT_TASK_CLASS
from .timing import Turn, score_timing

log = logging.getLogger("mary.review")

__all__ = ["Reviewer", "ReviewStore", "Judge"]


class Reviewer:
    """Judge + store + settings, wired once. Safe to share across tasks."""

    def __init__(self, settings, store: ReviewStore, judge: Judge | None = None) -> None:
        self.s = settings
        self.store = store
        self.judge = judge or Judge(settings)
        self._sem = asyncio.Semaphore(max(1, settings.review_concurrency))

    async def review_loop(self, loop_id: str) -> dict:
        """Grade every turn in one session. Idempotent per (loop, judge_version).

        Returns a small summary; never raises. A loop whose events have not landed yet comes back
        as `{"turns": 0}` WITHOUT being marked done, so the next sweep picks it up again."""
        jv = self.s.review_judge_version
        try:
            turns = await self.store.load_turns(loop_id)
        except Exception as exc:
            log.warning("review: could not load %s: %s", loop_id, exc)
            return {"loop_id": loop_id, "turns": 0, "error": str(exc)}

        if not turns:
            return {"loop_id": loop_id, "turns": 0}

        audio = await self._audio_for(turns)
        results = await asyncio.gather(
            *(self._review_turn(t, audio) for t in turns), return_exceptions=True
        )

        graded = [r for r in results if isinstance(r, dict)]
        for r in results:
            if isinstance(r, BaseException):
                log.warning("review: turn failed in %s: %s", loop_id, r)

        bad = sum(1 for r in graded if r.get("verdict") == "bad")
        try:
            await self.store.mark_done(loop_id, judge_version=jv, turns=len(graded), bad=bad)
        except Exception as exc:
            log.warning("review: could not mark %s done: %s", loop_id, exc)
        return {"loop_id": loop_id, "turns": len(graded), "bad": bad}

    async def _review_turn(self, turn: dict, audio: dict) -> dict:
        async with self._sem:
            verdict = await self.judge.judge(
                lines=turn["lines"], reply_text=turn["reply_text"]
            )

        # Timing is scored even when the judge failed — the clock does not depend on the model.
        task_class = verdict.get("task_class") or DEFAULT_TASK_CLASS
        score = score_timing(
            Turn(
                silent=turn["silent"],
                reply_ts=turn["reply_ts"],
                last_human_ts=turn["last_human_ts"],
                error=turn["error"],
                delivery=turn["delivery"],
                audio_sec=audio.get(turn.get("last_human_id")),
            ),
            task_class,
        )

        gaps = [dict(g, source="judge") for g in verdict["gaps"]]
        gaps += [{"code": c, "severity": sev, "evidence": _timing_evidence(c, score),
                  "source": "timing"} for c, sev in score.gaps]

        row = {
            "loop_id": turn["loop_id"], "seq": turn["seq"], "chat_id": turn["chat_id"],
            "ts": turn["ts"], "judge_version": self.s.review_judge_version,
            "judge_model": verdict.get("judge_model"),
            "turn_kind": "silence" if turn["silent"] else "reply",
            "domain": turn["domain"], "prompt_version": turn["prompt_version"],
            "verdict": verdict["verdict"], "confidence": verdict["confidence"],
            "rationale": verdict["rationale"], "proposed_gap": verdict["proposed_gap"],
            "reply_text": turn["reply_text"], "error": verdict.get("error"),
            "task_class": verdict.get("task_class"),
            "wait_seconds": score.wait_seconds, "last_human_id": turn["last_human_id"],
            "overtaken": turn["overtaken"], "model_ms": turn["model_ms"],
            "timing_band": score.band, "delivery": turn["delivery"],
            "turn_error": turn["turn_error"],
        }
        await self.store.write_review(row, gaps)
        return row

    async def _audio_for(self, turns: list[dict]) -> dict:
        try:
            return await self.store.audio_seconds([t.get("last_human_id") for t in turns])
        except Exception:  # the transcripts table is a nicety, not a dependency
            return {}


def _timing_evidence(code: str, score) -> str:
    secs = score.wait_seconds
    if secs is None:
        return code
    return f"{secs:.1f}s (band: {score.band})"
