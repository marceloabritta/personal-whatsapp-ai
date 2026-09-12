"""When a closed session gets reviewed.

A session is a listening window. It ends one of two ways, and only one of them announces itself:

  model close  — act_node sets state "close"; we hear about it immediately.
  TTL timeout  — the Redis key simply expires. No callback, no event, nothing written.

Historically two thirds of sessions ended the second way, so a trigger built only on the close
hook would silently skip most of the traffic. The SWEEPER is therefore the primary mechanism —
it also covers container restarts and anything the hook dropped — and the hook is a latency
optimisation layered on top. Both converge on Reviewer.review_loop.

Rails, the same ones the log store lives by: this runs as its own task with its own pool, nothing
in the graph ever awaits it, and every exception is swallowed. Review must never delay, alter, or
break a reply."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("mary.review.reaper")

_STOP = object()


class Reaper:
    def __init__(self, reviewer, settings) -> None:
        self.reviewer = reviewer
        self.s = settings
        # Bounded: a full queue drops the id and the sweeper picks the loop up later, exactly as
        # LogStore.enqueue drops a record rather than stalling the hot path.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._tasks: list[asyncio.Task] = []
        self._failures: dict[str, int] = {}
        self.dropped = 0

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._sweep_loop()),
            asyncio.create_task(self._hook_loop()),
        ]

    async def aclose(self) -> None:
        try:
            self._queue.put_nowait(_STOP)
        except asyncio.QueueFull:
            pass
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []

    # -- the close hook (called from act_node; must never block) -------------
    def offer(self, loop_id: str | None) -> None:
        """Non-blocking. Lisa stepped out of this session — review it soon."""
        if not loop_id:
            return
        try:
            self._queue.put_nowait(loop_id)
        except asyncio.QueueFull:
            self.dropped += 1

    # -- workers -------------------------------------------------------------
    async def _hook_loop(self) -> None:
        """Model-closed sessions, reviewed promptly.

        The settle wait is not politeness: act_node's record event was handed to the log writer
        moments ago and may not be committed yet. If it still is not there we simply drop the id —
        the sweeper is the guarantee, this path is only the fast lane."""
        while True:
            loop_id = await self._queue.get()
            if loop_id is _STOP:
                return
            try:
                await asyncio.sleep(self.s.review_settle_seconds)
                await self._review(loop_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("review hook failed for %s: %s", loop_id, exc)

    async def _sweep_loop(self) -> None:
        """Everything else: TTL timeouts, restarts, and anything the hook missed."""
        while True:
            try:
                await asyncio.sleep(self.s.review_sweep_seconds)
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a broken sweep must never end the task
                log.warning("review sweep failed: %s", exc)

    async def sweep_once(self) -> int:
        """One pass. Returns how many loops were reviewed (0 is the normal quiet case)."""
        scope = self.s.review_scope_version or None
        loops = await self.reviewer.store.pending_loops(
            judge_version=self.s.review_judge_version,
            settle_seconds=self.s.review_settle_window,
            batch=self.s.review_batch,
            scope_version=scope,
        )
        done = 0
        for loop_id in loops:
            if await self._review(loop_id):
                done += 1
        return done

    async def _review(self, loop_id: str) -> bool:
        """Review one loop, tolerating a loop whose events have not landed yet.

        Three empty attempts and we mark it done anyway — a loop that will never have turns must
        not be able to wedge the sweeper, re-selected on every pass forever."""
        result = await self.reviewer.review_loop(loop_id)
        if result.get("turns"):
            self._failures.pop(loop_id, None)
            log.info(
                '{"review":"loop","loop_id":"%s","turns":%d,"bad":%d}',
                loop_id, result["turns"], result.get("bad", 0),
            )
            return True

        n = self._failures.get(loop_id, 0) + 1
        self._failures[loop_id] = n
        if n >= 3:
            self._failures.pop(loop_id, None)
            await self.reviewer.store.mark_done(
                loop_id, judge_version=self.s.review_judge_version, turns=0, bad=0
            )
            log.warning("review: %s had no reviewable turns after %d tries; marked done", loop_id, n)
        return False
