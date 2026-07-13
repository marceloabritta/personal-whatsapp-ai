"""Waking up after being killed mid-run.

Runs at every startup, before the board is served. It reads the in-flight journal — whose
entries can only exist if a run began and never finished — and, for each one, re-enters the
card's SDK session and tells the manager the truth about what happened.

The resume is only possible because the session id is persisted on the card, so
`query(resume=card.session_id, ...)` hands the manager back everything it knew: what it had
already decided, what it had already delegated. That is the hook this hangs off.

The one thing that makes it CORRECT rather than merely clever is the prompt below. In the
incident that prompted all this, the worker was killed roughly one second before its `Write`
landed — so the manager's memory says "I delegated the scoper and it was finishing", while
the disk says `SCOPE.md` does not exist. A resumed manager that trusts its own memory would
sail straight past the missing artifact. So it is told, in as many words: **do not assume
any of it landed. Check the disk.**

Three outcomes per entry, and the board tells you which one you got:

    resumed    the manager is re-entering the run. The common case.
    exhausted  this run has already taken the process down MAX_ATTEMPTS times. We stop, and
               hand it to the human, rather than handing them a restart loop.
    orphaned   a card is busy but there is no journal entry (a board from before the journal
               existed, or a hand-edited file). Nothing to resume — say so honestly and
               clear the flag, rather than leave it spinning forever.
"""
from __future__ import annotations

import logging

from .board import Board
from .journal import CARD, MANAGER, WORKER, Journal, Run

log = logging.getLogger("manager.recovery")

RESUME_PROMPT = """\
[AUTOMATIC RESUME — the process running you was killed mid-flight.]

Your previous run on this card was interrupted: the manager process died while it was
working. It was triggered by this message from the human:

    {text}

You may have been part-way through a delegation, and a worker may have been killed before it
wrote its output. **Do not assume any of it landed.** Your memory of this run is not evidence.

Do this, in order:
1. List the card's folder on disk and read what is actually there.
2. Compare that against the current column's exit criteria.
3. Pick up exactly where the EVIDENCE says you left off — if a worker's artifact is missing
   or truncated, re-delegate that worker. If it is there and meets the exit criteria, carry
   on from there.

Tell the human in one line what you found on disk and what you are doing about it."""

INTERRUPTED_NOTE = (
    "⚠️ The manager process was killed while working on this card — the run was cut off "
    "mid-flight. Recovering automatically: re-entering the session and checking what "
    "actually reached disk."
)

EXHAUSTED_NOTE = (
    "🛑 This run has now been interrupted {attempts} times. I am not resuming it again "
    "automatically — something about it may be taking the process down, and a restart loop "
    "would be worse than stopping. Your card, its folder and this thread are intact. Send me "
    "a message when you want to pick it back up."
)

ORPHANED_NOTE = (
    "⚠️ This card was marked as being worked on when the server last stopped, but there is "
    "no record of the run itself, so I cannot resume it. (This happens for cards that were "
    "in flight before automatic recovery existed.) Nothing is lost except that turn — send "
    "the message again to pick it up."
)


class Recovery:
    """Reconciles what the board believes with what actually survived."""

    def __init__(self, board: Board, manager, journal: Journal):
        self.board = board
        self.manager = manager
        self.journal = journal

    async def run(self, dispatch) -> list[str]:
        """`dispatch(coro, label)` schedules the resume without blocking startup — the board
        must come up and be visible even while it is re-entering a long run."""
        notes: list[str] = []
        for run in self.journal.all():
            notes.append(await self._recover(run, dispatch))
        notes += await self._clear_orphans()
        return [n for n in notes if n]

    async def _recover(self, run: Run, dispatch) -> str:
        if run.kind == CARD and run.target_id not in self.board.cards:
            self.journal.finish(run.kind, run.target_id)  # card was deleted while we were dead
            return f"dropped an interrupted run for a card that no longer exists ({run.target_id})"
        if run.kind == MANAGER and run.target_id not in self.board.managers:
            self.journal.finish(run.kind, run.target_id)
            return f"dropped an interrupted run for a manager that no longer exists ({run.target_id})"
        if run.kind == WORKER and not self._column_exists(run.target_id):
            # The column was deleted, or renamed, while we were dead. Its worker chat is keyed
            # by slug, so there is nothing left to resume the conversation against.
            self.journal.finish(run.kind, run.target_id)
            return f"dropped an interrupted run for a worker that no longer exists ({run.target_id})"

        if self.journal.is_exhausted(run):
            return await self._give_up(run)

        attempts = self.journal.bump(run)  # count the attempt BEFORE making it
        log.warning(
            "resuming interrupted %s run %s (attempt %d), started %.0fs of work ago",
            run.kind, run.target_id, attempts, run.started_at,
        )

        if run.kind == CARD:
            await self.board.append_message(run.target_id, "system", INTERRUPTED_NOTE)
            dispatch(
                self.manager.handle_card_message(
                    run.target_id, RESUME_PROMPT.format(text=run.text), resuming=True
                ),
                f"resume card {run.target_id}",
            )
        elif run.kind == WORKER:
            await self.board.append_worker_message(run.target_id, "system", INTERRUPTED_NOTE)
            dispatch(
                self.manager.handle_worker_message(
                    run.target_id, RESUME_PROMPT.format(text=run.text), resuming=True
                ),
                f"resume worker {run.target_id}",
            )
        else:
            await self.board.append_manager_message(run.target_id, "system", INTERRUPTED_NOTE)
            dispatch(
                self.manager.handle_board_message(
                    run.target_id, RESUME_PROMPT.format(text=run.text), resuming=True
                ),
                f"resume manager {run.target_id}",
            )
        return f"resuming the interrupted {run.kind} run on {run.target_id} (attempt {attempts})"

    def _column_exists(self, key: str) -> bool:
        pipeline, _, slug = (key or "").partition("/")
        return self.board.pipelines.by_slug(pipeline, slug) is not None

    async def _give_up(self, run: Run) -> str:
        note = EXHAUSTED_NOTE.format(attempts=run.attempts)
        if run.kind == CARD:
            await self.board.append_message(run.target_id, "system", note)
            await self.board.set_busy(run.target_id, False)
        elif run.kind == WORKER:
            await self.board.append_worker_message(run.target_id, "system", note)
            await self.board.set_worker_busy(run.target_id, False)
        else:
            await self.board.append_manager_message(run.target_id, "system", note)
            await self.board.set_manager_busy(run.target_id, False)
        self.journal.finish(run.kind, run.target_id)
        log.error(
            "giving up on %s run %s after %d interrupted attempts", run.kind, run.target_id, run.attempts
        )
        return f"GAVE UP on the {run.kind} run for {run.target_id} after {run.attempts} interruptions"

    async def _clear_orphans(self) -> list[str]:
        """A card marked busy with no journal entry behind it. There is nothing to resume, so
        the flag is now just a lie — but say so in the thread rather than silently erasing it."""
        notes = []
        for card_id in self.board.busy_cards():
            if self.journal.get(CARD, card_id):
                continue  # a real run; already handled above
            await self.board.append_message(card_id, "system", ORPHANED_NOTE)
            await self.board.set_busy(card_id, False)
            notes.append(f"cleared a stuck 'busy' on {card_id} with no run behind it")
        for mid in self.board.busy_managers():
            if self.journal.get(MANAGER, mid):
                continue
            await self.board.set_manager_busy(mid, False)
        for key, chat in list(self.board.worker_chats.items()):
            if chat.busy and not self.journal.get(WORKER, key):
                await self.board.set_worker_busy(key, False)
        return notes
