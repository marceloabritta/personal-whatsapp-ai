"""The manager service.

Drives two kinds of Claude Agent SDK conversation:

    handle_card_message(card_id, text)       one session per CARD — the supervisor
                                             of that card's journey across the board.
    handle_board_message(manager_id, text)   one session per MANAGER — the board-level
                                             chat, where you shape pipelines and workers.

Both persist their session id, so context survives a restart.

Worker reports are surfaced onto the card's chat as they come back, so you can watch the
supervision happen: `→ delegating to plan__scoping`, then the worker's own ENTRY/WORK/
OUTPUT/EXIT/FLAGS report, then the manager's decision.

A `mock` mode runs the same shape with no API key: it walks whatever columns you have
configured, writes each column's artifact, and stops at every gate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time

from . import policy
from .agents import (
    board_prompt_for,
    manager_prompt_for,
    policy_chat_prompt_for,
    worker_chat_prompt_for,
)
from .board import Board
from .journal import CARD, MANAGER, WORKER, Journal, Run
from .pending import PendingQueue
from .models import (
    BACKLOG,
    BUILD,
    EXPED,
    FEATURE,
    KINDS,
    MAINT,
    MAINTENANCE,
    ORIGIN_PIPELINES,
    PIPELINE_TITLES,
    PIPELINES,
    PLAN,
    ROUTABLE,
)

log = logging.getLogger("manager")


async def _noop() -> None:
    return None


async def _collect(sink: list, text: str) -> None:
    sink.append(text)


async def _sync(fn, *a) -> None:
    """_pump awaits its callbacks; this lets a plain function be one."""
    fn(*a)

# The reserved prompt-chat key for the manager's OWN standing orders. A worker's key is
# always "<pipeline>/<slug>", so it always contains a "/" — this can never collide.
POLICY_KEY = "manager"

TRIAGE_PROMPT = """\
[AUTOMATIC — a card was just created and has no type.]

This card is in the BACKLOG and nobody has classified it. Read its title and description,
look at the codebase if you need to, and give it a type with `mcp__board__set_kind`:

  feature      — it does not exist yet, and we would be building it.
  maintenance  — it exists, and it is behaving wrongly.

Then STOP. Do not route it, do not start any work, do not ask the human anything. They will
tell you when to start. Reply with one short line saying what you typed it as and why."""

SAVE_AND_STOP = """\
[STOP — you are being wound down mid-task, on purpose. Nothing is wrong.]

Do NOT start anything new. Do not begin another file, another test, another command.

**Save your place, in one action:** write `WIP.md` into the card folder, containing:

  * **Done** — what you have actually finished, and which files you already wrote. Be exact;
    the next thing that reads this will trust it.
  * **Mid-flight** — what you were in the middle of when you were stopped, and whether it
    landed on disk or not.
  * **Next** — precisely what remains, in the order you would have done it.

Then end your turn. **You will be resumed in this same conversation**, with everything you know
still in front of you — so write this for yourself, not for a stranger.
Nothing you have done is being thrown away."""

# One wording for both ways a card gets wound down — the human pausing the board, and an
# update shipping — because from inside the conversation they are the same event: you were
# stopped, you saved your place, and now you are being picked back up.
CONTINUE_AFTER_PAUSE = """\
[AUTOMATIC — you were wound down part-way through this card, and are being picked back up.]

You were asked to stop: you finished what was in your hands and saved your place, rather than
starting the next column. Nobody has said anything new to you.

Pick up exactly where you left off. Read the card folder on disk first — trust it, not your
memory of what you had dispatched — and carry the supervision loop on from the column the
card is actually in. Do not tell the human about the pause; they know. Do not start over."""

APPROVE_WORDS = (
    "approve", "approved", "go ahead", "lgtm", "build it", "ship it", "ship",
    "yes", "proceed", "continue", "do it", "promote",
)


class ManagerConfig:
    """Everything the manager needs, and the single answer to "am I live or mocked?".

    There is exactly one place that decides that, and it reports WHY — the old code decided
    it in two places (here, and a shell `echo` in run.sh that only looked at the API key)
    and so cheerfully announced MOCK while starting up live.
    """

    def __init__(
        self,
        repo_dir: str,
        data_dir: str,
        model: str | None = None,
        permission_mode: str | None = None,
        mock: bool | None = None,
    ):
        self.repo_dir = os.path.abspath(repo_dir)
        self.data_dir = data_dir
        self.model = model or os.environ.get("MANAGER_MODEL") or None
        self.permission_mode = permission_mode or os.environ.get(
            "MANAGER_PERMISSION_MODE", "bypassPermissions"
        )
        if mock is None:
            self.mock, self.mock_reason = _detect_mock()
        else:
            self.mock = mock
            self.mock_reason = "set explicitly"


def _detect_mock() -> tuple[bool, str]:
    """MANAGER_MOCK wins over everything. Otherwise: an API key OR a logged-in Claude Code
    CLI is enough to run live — the Agent SDK falls back to the CLI's OAuth session, so
    "no key" has never actually meant "no live mode"."""
    env = os.environ.get("MANAGER_MOCK")
    if env not in (None, ""):
        if env in ("0", "false", "no"):
            return False, "MANAGER_MOCK=0 (forced live)"
        return True, f"MANAGER_MOCK={env} (forced mock)"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY is set"
    if shutil.which("claude"):
        return False, "no API key, but the Claude Code CLI is installed (using its login)"
    return True, "no ANTHROPIC_API_KEY and no Claude Code CLI on PATH"


class Manager:
    def __init__(self, board: Board, config: ManagerConfig, journal: Journal | None = None):
        self.board = board
        self.config = config
        # Every run is written down before it starts and struck off when it ends. Anything
        # left in here at boot was killed mid-flight — see manager/recovery.py.
        self.journal = journal or Journal(config.data_dir)
        self._locks: dict[str, asyncio.Lock] = {}

        # ---- draining: how an update ships without destroying work ----
        # While draining we start NO new runs. The ones already in flight are allowed to
        # finish, and anything the human sends meanwhile is written to disk and dispatched
        # after the restart. Shipping by killing the process and letting recovery clean up
        # was losing a turn of work every single time; this is the fix.
        # DRAINING — winding down. No NEW work is fed into the pipeline (the delegate tool
        # refuses), but the manager is idle while the last workers finish, so the human can
        # still talk to him and still be answered. Taking their keyboard away for the whole
        # wind-down was a tax they never agreed to pay.
        self.draining = False
        # STOPPING — the process is committing to exit, right now. This is the only window in
        # which a message cannot be acted on, because there is nothing left to act with. It is
        # seconds long. THIS is what the queue is for.
        self.stopping = False
        self.pending = PendingQueue(config.data_dir)

        # PAUSED — draining because a HUMAN asked for it, rather than because an update is
        # shipping. Both wind the board down the same way; the difference is who resumes it.
        # An update resumes itself on the other side of the restart. A pause waits to be told.
        #
        # So it is DURABLE. A pause held only in memory would quietly undo itself the next
        # time the process started — you would come back to a board you had deliberately
        # stopped, running. The marker on disk is what makes "paused" mean paused.
        self._pause_marker = os.path.join(config.data_dir, ".paused")
        self.paused = os.path.exists(self._pause_marker)
        if self.paused:
            self.draining = True
            log.warning("the board is PAUSED — starting nothing until it is resumed")

        # What the manager asked for on his last turn, and which cards have a worker running
        # right now. `_workers_running` is what makes "the manager is idle while a worker
        # works" checkable rather than merely intended.
        self._delegations: dict[str, tuple[str, str]] = {}
        self._workers_running: set[str] = set()
        # The live handle on each running worker — what lets us TELL IT TO STOP.
        self._worker_clients: dict[str, object] = {}
        self._interrupted: set[str] = set()
        self._driving: set[str] = set()          # a supervision loop owns this card
        self._turn_kind: dict[str, str] = {}     # "drive" | "chat" | "reply"
        self._wound_down: set[str] = set()       # asked to stop early; owed a "carry on" later

    # ---- helpers -----------------------------------------------------
    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    # ---- draining ----------------------------------------------------
    def inflight(self) -> list[Run]:
        """Every run currently believed to be running. The journal IS this answer: an entry
        exists only between the moment a run is dispatched and the moment it finishes."""
        return self.journal.all()

    def begin_drain(self) -> list[Run]:
        """Wind down. Start NO new work — but keep answering the human.

        The delegate tool refuses from here on, so the pipeline empties instead of being
        topped up. The manager is idle while the last workers finish, so a message from the
        human is answered as normal. Returns what is still running.
        """
        self.draining = True
        runs = self.inflight()
        log.warning("draining: no new runs will start; %d still in flight", len(runs))
        return runs

    def end_drain(self) -> None:
        """Take work again. For an update that was called off — the board must not be left
        in a state where it silently ignores the human forever."""
        self.draining = False
        self.stopping = False
        log.warning("drain cancelled: accepting work again")

    # ---- pause: ONE wind-down, and shipping is just a caller of it ----
    async def pause(self, remember: bool = True) -> dict:
        """Stop the board where it stands. Start nothing new; tell what is running to stop.

        This is THE wind-down, and there is only one — an update ships by calling it (see
        server.restart_for_update). That is deliberate: pausing and shipping want exactly the
        same thing from the board, and two implementations of "stop safely" would be one too
        many. The difference between them is not what they stop, it is who resumes it.

        Nothing is killed. Each worker is told to stop, writes `WIP.md`, and hands back; the
        card remembers the session it was thinking in, so it is RESUMED in that conversation
        rather than started over, and a carry-on note is queued for whenever work resumes.

        `remember=False` for an update — the restart resumes itself on the other side, so it
        must NOT leave a pause marker behind, or the board would come back up stopped.
        """
        runs = self.begin_drain()
        stopped = await self.stop_workers()
        if remember:
            self.paused = True
            with open(self._pause_marker, "w", encoding="utf-8") as fh:
                fh.write(str(int(time.time())))
        log.warning(
            "paused (remembered=%s): told %d worker(s) to stop; %d run(s) winding down",
            remember, stopped, len(runs),
        )
        return {"stopped": stopped, "inflight": len(runs)}

    def unpause(self) -> None:
        """Willing to take work again — and that is ALL this does.

        It is half of resuming, and the less important half. The cards that were wound down
        are waiting on carry-on notes in the pending queue, and any run that was cut off is in
        the journal. Whoever calls this has to pick that held work back up (the server does —
        see `_take_work_again`), or the board sits there looking idle and perfectly healthy
        with everything it was doing quietly abandoned.

        It has to happen BEFORE that, though, not after: work picked up while the board is
        still draining would simply wind itself down again.
        """
        if os.path.exists(self._pause_marker):
            os.remove(self._pause_marker)
        self.paused = False
        self.end_drain()

    async def _wind_down_card(self, card_id: str, worker_name: str | None = None) -> None:
        """Stop this card where it stands, remember its place, and queue its carry-on.

        The carry-on is what makes a pause reversible rather than a quiet abandonment: it is
        the durable record that this card is owed a "pick it back up" when work resumes.
        """
        self._wound_down.discard(card_id)
        self._interrupted.discard(card_id)
        if worker_name:
            await self.board.set_stopped_worker(card_id, worker_name, None)
        await self.board.set_working(card_id, "")
        self.pending.add(CARD, card_id, CONTINUE_AFTER_PAUSE, from_human=False)
        log.warning("wound down %s; it resumes when the board takes work again", card_id)

    async def _queue_while_draining(self, kind: str, target_id: str, text: str) -> None:
        """Write the message down, tell the human it is safe, and start nothing."""
        self.pending.add(kind, target_id, text)
        note = (
            "⏸ The board is paused, so I am not starting anything new. **Your message is "
            "saved** — I will pick it up the moment you resume."
            if self.paused else
            "⏳ The system is being updated, so I am not starting anything new. **Your "
            "message is saved** — I will pick it up the moment I am back, in a few seconds."
        )
        if kind == CARD:
            await self.board.append_message(target_id, "user", text)
            await self.board.append_message(target_id, "system", note)
        elif kind == WORKER:
            await self.board.append_worker_message(target_id, "user", text)
            await self.board.append_worker_message(target_id, "system", note)
        else:
            await self.board.append_manager_message(target_id, "user", text)
            await self.board.append_manager_message(target_id, "system", note)

    async def dispatch_pending(self, spawn) -> list[str]:
        """After a restart: send everything that was queued during the drain.

        `resuming=True` — the message is already in the thread (we put it there when we
        queued it, so the human could see it was safe). Appending it again would show them
        their own words twice.
        """
        msgs = self.pending.all()
        if not msgs:
            return []
        self.pending.clear()
        notes = []
        for m in msgs:
            if m.kind == CARD and m.target_id in self.board.cards:
                # resuming=True → already in the thread (or machinery); don't post it twice.
                # from_human    → a PERSON is owed an answer; a card carrying on is not.
                spawn(
                    self.handle_card_message(
                        m.target_id, m.text, resuming=True, from_human=m.from_human
                    ),
                    f"{'queued' if m.from_human else 'winding-down'} card {m.target_id}",
                )
            elif m.kind == WORKER:
                spawn(self.handle_prompt_message(m.target_id, m.text, resuming=True), f"queued prompt {m.target_id}")
            elif m.kind == MANAGER and m.target_id in self.board.managers:
                spawn(self.handle_board_message(m.target_id, m.text, resuming=True), f"queued board {m.target_id}")
            else:
                continue
            notes.append(
                f"picked up a message you sent during the update ({m.kind} {m.target_id})"
                if m.from_human
                else f"resuming {m.target_id}, which wound down for the update"
            )
        return notes

    def _all_columns(self):
        return self.board.pipelines.all_columns()

    def _worker_defs(self):
        return self.board.workers.definitions(self._all_columns())

    # ---- public entry: a card ----------------------------------------
    async def handle_card_message(
        self,
        card_id: str,
        text: str,
        resuming: bool = False,
        from_human: bool | None = None,
    ) -> None:
        """`resuming` and `from_human` are TWO DIFFERENT QUESTIONS, and collapsing them into
        one flag cost the human an acknowledgement:

            resuming    — is this text already in the thread? (don't post it twice)
            from_human  — did a PERSON say this? (if so, he owes them an answer)

        A message queued during a ship is BOTH: it is replayed (so `resuming`), and it is
        the human talking (so `from_human`). Deriving one from the other filed his reply as
        a note and left them staring at silence. A crash-resume or a triage prompt is
        machinery: replayed, and owed nothing.
        """
        if from_human is None:
            from_human = not resuming
        card = self.board.cards.get(card_id)
        if not card:
            return
        # A RESUMING run is let through: it is either recovery finishing an interrupted run,
        # or the queue being replayed after the restart. Blocking those would deadlock the
        # very machinery the drain exists to serve.
        if self.stopping and not resuming:
            await self._queue_while_draining(CARD, card_id, text)
            return
        if not resuming:
            # A resume prompt is machinery, not something the human typed. Don't put it in
            # their thread as if they had said it.
            await self.board.append_message(card_id, "user", text)

        # A supervision run already owns this card. It does NOT own the manager: while a
        # worker is running he is idle, and the human must be able to talk to him. So this
        # becomes a CHAT turn on the same session — same context, same card — instead of
        # being queued behind work he is not actually doing.
        if card_id in self._driving and not self.config.mock:
            await self._chat_turn(card_id, text)
            return

        lock = self._lock_for(f"card:{card_id}")
        if lock.locked():
            await self.board.append_message(
                card_id, "system", "Manager is still working the previous message; queued."
            )
        async with lock:
            self._driving.add(card_id)
            await self.board.set_busy(card_id, True)
            col = self.board.pipelines.get(card.column)
            # ON DISK BEFORE THE RUN STARTS. If the process dies from here on, this entry is
            # the only thing that knows a run was ever dispatched.
            self.journal.start(
                CARD, card_id, text, session_id=card.session_id,
                column=col.title if col else "",
            )
            finished = False
            try:
                if self.config.mock:
                    await self._mock_card(card_id, text)
                else:
                    # `resuming` = recovery, a queued message replay, or triage — machinery,
                    # not the human speaking. Only a real message from them earns a reply.
                    await self._real_card(card_id, text, from_human=from_human)
                finished = True
            except asyncio.CancelledError:
                # A clean shutdown mid-run. Leave the journal entry exactly where it is, so
                # the next boot resumes this rather than dropping it on the floor.
                await self._note_shutdown(card_id, CARD)
                raise
            except Exception as e:  # noqa: BLE001 — always surface failure to the board
                await self.board.append_message(card_id, "system", f"⚠️ manager error: {e}")
                finished = True  # it failed, but it failed *here* — there is nothing to resume
            finally:
                self._driving.discard(card_id)
                self._turn_kind.pop(card_id, None)
                if finished:
                    self.journal.finish(CARD, card_id)
                    await self.board.set_working(card_id, "")
                    await self.board.set_busy(card_id, False)

    async def triage_card(self, card_id: str) -> None:
        """A card was created with no type. Ask the manager for one, straight away.

        "No card is left without a type" is only true if something makes it true. This is
        that something — it fires on creation, and it is a normal card run (journalled,
        recoverable), so a process death mid-triage does not leave the card unclassified.

        `resuming=True` because the prompt below is machinery, not something the human said;
        it must not appear in their thread as if they had typed it.
        """
        card = self.board.cards.get(card_id)
        if not card or card.kind in KINDS:
            return
        await self.board.append_message(
            card_id, "system", "No type set — asking the manager to classify this card."
        )
        await self.handle_card_message(card_id, TRIAGE_PROMPT, resuming=True)

    # ---- public entry: the board-level chat ---------------------------
    async def handle_board_message(self, manager_id: str, text: str, resuming: bool = False) -> None:
        m = self.board.managers.get(manager_id)
        if not m:
            return
        if self.stopping and not resuming:
            await self._queue_while_draining(MANAGER, manager_id, text)
            return
        if not resuming:
            await self.board.append_manager_message(manager_id, "user", text)
        lock = self._lock_for(f"mgr:{manager_id}")
        if lock.locked():
            await self.board.append_manager_message(
                manager_id, "system", "Still working the previous message; queued."
            )
        async with lock:
            await self.board.set_manager_busy(manager_id, True)
            self.journal.start(MANAGER, manager_id, text, session_id=m.session_id)
            finished = False
            try:
                if self.config.mock:
                    await self._mock_board(manager_id, text)
                else:
                    await self._real_board(manager_id, text)
                finished = True
            except asyncio.CancelledError:
                await self._note_shutdown(manager_id, MANAGER)
                raise
            except Exception as e:  # noqa: BLE001
                await self.board.append_manager_message(
                    manager_id, "system", f"⚠️ manager error: {e}"
                )
                finished = True
            finally:
                if finished:
                    self.journal.finish(MANAGER, manager_id)
                    await self.board.set_manager_busy(manager_id, False)

    async def handle_prompt_message(self, key: str, text: str, resuming: bool = False) -> None:
        """A conversation about ONE PROMPT.

        Two kinds of prompt, one machinery:
          * `"<pipeline>/<slug>"` — a column's worker. Its contract.
          * `POLICY_KEY` ("manager") — the manager's OWN standing orders. He edits himself.

        The reserved key cannot collide with a worker's: a worker key always contains a "/".

        Same shape as the card and board handlers, deliberately: journal the run BEFORE it
        starts, mark busy, run, strike it off. A new kind of long-running work that is not
        journalled is a new kind of work that cannot be recovered when the process dies.
        """
        is_policy = key == POLICY_KEY
        col = None if is_policy else self._column_for_key(key)
        if not is_policy and not col:
            return
        if self.stopping and not resuming:
            await self._queue_while_draining(WORKER, key, text)
            return
        if not resuming:
            await self.board.append_worker_message(key, "user", text)
        lock = self._lock_for(f"wrk:{key}")
        if lock.locked():
            await self.board.append_worker_message(
                key, "system", "Still working the previous message; queued."
            )
        async with lock:
            chat = self.board.worker_chat(key)
            await self.board.set_worker_busy(key, True)
            self.journal.start(WORKER, key, text, session_id=chat.session_id)
            finished = False
            try:
                if self.config.mock:
                    await (
                        self._mock_policy(key, text) if is_policy
                        else self._mock_worker(key, col, text)
                    )
                else:
                    await (
                        self._real_policy(key, text) if is_policy
                        else self._real_worker(key, col, text)
                    )
                finished = True
            except asyncio.CancelledError:
                await self._note_shutdown(key, WORKER)
                raise
            except Exception as e:  # noqa: BLE001
                await self.board.append_worker_message(key, "system", f"⚠️ manager error: {e}")
                finished = True
            finally:
                if finished:
                    self.journal.finish(WORKER, key)
                    await self.board.set_worker_busy(key, False)

    def _column_for_key(self, key: str):
        pipeline, _, slug = (key or "").partition("/")
        return self.board.pipelines.by_slug(pipeline, slug)

    async def _note_shutdown(self, target_id: str, kind: str) -> None:
        """Say so in the thread. A stop the operator asked for and a crash should not look
        the same to the human — but neither of them may quietly eat the work."""
        note = (
            "⏸ The server is shutting down mid-run. This run is recorded and will resume "
            "automatically when it comes back up."
        )
        try:
            if kind == CARD:
                await self.board.append_message(target_id, "system", note)
            elif kind == WORKER:
                await self.board.append_worker_message(target_id, "system", note)
            else:
                await self.board.append_manager_message(target_id, "system", note)
        except Exception:  # noqa: BLE001 — the loop may already be tearing down; never mask the cancel
            log.warning("could not post the shutdown note for %s %s", kind, target_id)

    # ==================================================================
    # MCP tool servers — the manager's hands
    # ==================================================================
    def _card_tools(self, card_id: str):
        from claude_agent_sdk import create_sdk_mcp_server, tool

        board = self.board
        pipelines = board.pipelines

        def ok(text: str) -> dict:
            return {"content": [{"type": "text", "text": text}]}

        @tool("card_info", "Where this card is, its folder, and the current column's contract", {})
        async def card_info(args):
            c = board.cards.get(card_id)
            if not c:
                return ok("card not found")
            col = pipelines.get(c.column)
            w = board.workers.ensure(col) if col else None
            abs_dir = board.abs_dir(c)
            try:
                files = sorted(os.listdir(abs_dir))
            except OSError:
                files = []
            return ok(
                json.dumps(
                    {
                        "card": {"id": c.id, "title": c.title, "description": c.description},
                        "pipeline": c.pipeline,
                        "column": col.title if col else None,
                        "is_gate": bool(col and col.gate),
                        "folder": abs_dir,
                        "files_in_folder": files,
                        "worker": w.agent_name if w else None,
                        "contract": w.contract() if w else {},
                        "next_column": (
                            n.title if (n := pipelines.next_column(c.column)) else None
                        ),
                    },
                    indent=2,
                )
            )

        @tool("set_stage", "Set the fine-grained status label shown on the card", {"stage": str})
        async def set_stage(args):
            await board.set_stage(card_id, args["stage"])
            return ok(f"stage set to {args['stage']}")

        @tool(
            "note",
            "FILE a decision on the card: what you chose and the one line of reasoning that "
            "matters. This is a RECORD, not a message — the human is not interrupted by it. "
            "This is where your thinking goes.",
            {"text": str},
        )
        async def note(args):
            await board.append_note(card_id, args["text"])
            return ok("filed on the card (the human was not interrupted)")

        @tool(
            "ask_human",
            "SPEAK TO THE HUMAN. Use this ONLY when you need something FROM them — a gate, a "
            "decision that is theirs, a blocker you cannot pass. Everything else belongs in "
            "a note. This is the only thing they are shown, so it must be worth the "
            "interruption: what you need, and what it costs them to decide.",
            {"text": str},
        )
        async def ask_human(args):
            await board.append_message(card_id, "manager", args["text"])
            return ok("sent to the human. Now END YOUR TURN and wait for them.")

        @tool("move_next", "Advance this card to the next column of its pipeline", {})
        async def move_next(args):
            c = board.cards.get(card_id)
            if not c:
                return ok("card not found")
            nxt = pipelines.next_column(c.column)
            if not nxt:
                if c.pipeline in ORIGIN_PIPELINES:
                    name = PIPELINE_TITLES.get(c.pipeline, c.pipeline).upper()
                    return ok(
                        f"This is the last column of the {name} pipeline. Crossing into BUILD "
                        "requires human approval — use promote_to_build only after the human "
                        "has approved."
                    )
                return ok("Already at the last column of the BUILD pipeline.")
            await board.move_card(card_id, nxt.id)
            c = board.cards[card_id]
            return ok(
                f"moved to '{nxt.title}'"
                + (" — this column is a GATE." if nxt.gate else "")
                + f" Card folder is now: {board.abs_dir(c)}"
            )

        @tool("move_card", "Move this card to any column of its pipeline (by title or slug)", {"column": str})
        async def move_card(args):
            c = board.cards.get(card_id)
            if not c:
                return ok("card not found")
            col = pipelines.resolve(c.pipeline, args["column"])
            if not col:
                titles = [x.title for x in pipelines.columns[c.pipeline]]
                return ok(f"no such column. Columns in this pipeline: {titles}")
            await board.move_card(card_id, col.id)
            return ok(f"moved to '{col.title}'. Card folder is now: {board.abs_dir(board.cards[card_id])}")

        @tool(
            "set_kind",
            "What this card IS: 'feature' (does not exist yet) or 'maintenance' (something "
            "built is behaving wrongly). Set it as soon as an untyped card reaches you.",
            {"kind": str},
        )
        async def set_kind(args):
            c = board.cards.get(card_id)
            if not c:
                return ok("card not found")
            if c.kind in KINDS:
                return ok(
                    f"this card is already a **{c.kind}** card. Its type is the human's to "
                    f"change, not yours to second-guess."
                )
            updated = await board.set_card_kind(card_id, args.get("kind", ""))
            if not updated:
                return ok("not a type. Use 'feature' or 'maintenance'.")
            return ok(f"typed as **{updated.kind}**. It is now routable.")

        @tool(
            "route_to",
            "Send this BACKLOG card into a pipeline: 'plan' (a real feature), 'maint' (a bug "
            "you cannot yet explain) or 'exped' (small, contained, low-risk — the fast lane).",
            {"pipeline": str},
        )
        async def route_to(args):
            c = board.cards.get(card_id)
            if not c:
                return ok("card not found")
            target = (args.get("pipeline") or "").strip().lower()
            if target not in ROUTABLE:
                return ok(f"route where? One of: {', '.join(ROUTABLE)}.")
            if c.kind not in KINDS:
                return ok(
                    "this card has no type yet. Give it one with set_kind first — nothing "
                    "leaves the backlog untyped."
                )
            routed = await board.route_card(card_id, target)
            if not routed:
                return ok("could not route it.")
            col = pipelines.get(routed.column)
            return ok(
                f"routed to **{PIPELINE_TITLES.get(target, target)}** → "
                f"'{col.title if col else '?'}'. Card folder is now: {board.abs_dir(routed)}"
            )

        @tool(
            "delegate",
            "Dispatch this column's worker, then END YOUR TURN. You do not run it and you do "
            "not wait for it — you will be woken with its report. `instructions` is the whole "
            "briefing: the worker starts with no context but the card folder and the repo.",
            {"worker": str, "instructions": str},
        )
        async def delegate(args):
            c = board.cards.get(card_id)
            if not c:
                return ok("card not found")
            if self.draining:
                # WINDING DOWN. This is the difference between "wait for the pipeline to
                # finish" (which can be an hour — one run drives a card through many columns)
                # and "finish what is in your hands and stop" (minutes). We cannot interrupt a
                # worker that is already running; we CAN stop putting more work into the pipe.
                self._wound_down.add(card_id)
                return ok(
                    ("**The board is PAUSED. Do NOT dispatch anything.**" if self.paused
                     else "**The system is restarting. Do NOT dispatch anything.**")
                    + " Wind down: judge what you already have, file it with `note`, and END "
                    "YOUR TURN. This card is remembered and picks itself back up automatically "
                    "when the board takes work again — nothing is lost, and you do not need to "
                    "tell the human."
                )
            if card_id in self._workers_running or self._turn_kind.get(card_id) == "chat":
                return ok(
                    "a worker is ALREADY running on this card, and you are answering the "
                    "human while it does. Its report is coming to you — judge it then. Do "
                    "not dispatch another; answer the human and end your turn."
                )
            col = pipelines.get(c.column)
            if not col:
                return ok("this card is in the backlog: there is no column and no worker.")
            want = (args.get("worker") or "").strip()
            target = board.workers.by_agent_name(pipelines.all_columns(), want) if want else col
            if not target:
                return ok(f"no such worker: '{want}'.")
            if target.id != col.id:
                return ok(
                    f"'{want}' is not this card's column. The card is in '{col.title}' and "
                    f"that is the only work it can have done. Move it first if that is wrong."
                )
            instructions = (args.get("instructions") or "").strip()
            if len(instructions) < 30:
                return ok(
                    "the briefing is too thin. The worker starts with FRESH CONTEXT and knows "
                    "nothing: give it the card folder path, the card, and what you want."
                )
            self._delegations[card_id] = (board.workers.runtime(target)["name"], instructions)
            return ok(
                f"dispatched **{target.title}**. END YOUR TURN NOW — say nothing further. "
                f"You will be woken with its report, and you will judge it then."
            )

        @tool("promote_to_build", "Hand this card from its planning pipeline (plan OR maintenance) to the build pipeline. HUMAN APPROVAL ONLY.", {})
        async def promote_to_build(args):
            c = board.cards.get(card_id)
            if not c:
                return ok("card not found")
            if c.pipeline not in ORIGIN_PIPELINES:
                return ok("card is already in the build pipeline")
            was = PIPELINE_TITLES.get(c.pipeline, c.pipeline)
            await board.promote_to_build(card_id)
            c = board.cards[card_id]
            col = pipelines.get(c.column)
            return ok(
                f"promoted from {was} to BUILD → '{col.title if col else '?'}' "
                f"(this card stays a **{c.kind}** card). "
                f"Card folder is now: {board.abs_dir(c)}"
            )

        @tool("list_columns", "The current columns of both pipelines, with their gates and workers", {})
        async def list_columns(args):
            return ok(json.dumps(self._columns_json(), indent=2))

        @tool("read_worker", "Read a column's worker instruction file", {"pipeline": str, "column": str})
        async def read_worker(args):
            return ok(self._read_worker(args.get("pipeline", ""), args.get("column", "")))

        @tool(
            "write_worker",
            "Replace a column's worker instruction file (read it first; keep the frontmatter and the four sections)",
            {"pipeline": str, "column": str, "markdown": str},
        )
        async def write_worker(args):
            return ok(
                self._write_worker(
                    args.get("pipeline", ""), args.get("column", ""), args.get("markdown", "")
                )
            )

        return create_sdk_mcp_server(
            name="board",
            version="2.0.0",
            tools=[
                card_info, set_stage, note, ask_human, move_next, move_card, set_kind, route_to,
                delegate, promote_to_build, list_columns, read_worker, write_worker,
            ],
        )

    def _board_tools(self, manager_id: str):
        from claude_agent_sdk import create_sdk_mcp_server, tool

        board = self.board
        pipelines = board.pipelines

        def ok(text: str) -> dict:
            return {"content": [{"type": "text", "text": text}]}

        @tool("list_cards", "Every card on the board with its pipeline, column and manager", {})
        async def list_cards(args):
            rows = []
            for cid in board.order:
                c = board.cards.get(cid)
                if not c or c.trashed:
                    continue
                col = pipelines.get(c.column)
                m = board.managers.get(c.manager_id)
                rows.append(
                    {
                        "id": c.id,
                        "title": c.title,
                        "pipeline": c.pipeline,
                        "column": col.title if col else "?",
                        "at_gate": bool(col and col.gate),
                        "busy": c.busy,
                        "manager": m.name if m else "?",
                        "folder": board.abs_dir(c),
                    }
                )
            return ok(json.dumps(rows, indent=2) if rows else "The board is empty.")

        @tool(
            "create_card",
            "Create a card. pipeline='plan' for a new feature (default), 'maint' for a bug "
            "or malfunction — a maintenance card, which is coloured differently and stays "
            "that colour all the way through build.",
            {"title": str, "description": str, "pipeline": str},
        )
        async def create_card(args):
            title = (args.get("title") or "").strip()
            if not title:
                return ok("a title is required")
            pipeline = (args.get("pipeline") or PLAN).strip().lower()
            if pipeline not in ORIGIN_PIPELINES:
                pipeline = PLAN
            c = await board.add_card(
                title, (args.get("description") or "").strip(), manager_id, pipeline=pipeline
            )
            col = pipelines.get(c.column)
            return ok(
                f"created {c.kind} card {c.id} — '{c.title}' in "
                f"{PIPELINE_TITLES.get(c.pipeline, c.pipeline)} → '{col.title if col else '?'}'. "
                f"Folder: {board.abs_dir(c)}"
            )

        @tool("move_card", "Move any card to any column, in any pipeline", {"card_id": str, "column": str})
        async def move_card(args):
            c = board.cards.get(args.get("card_id", ""))
            if not c:
                return ok("no such card")
            # The board chat moves cards anywhere, so look in the card's own pipeline first
            # and then in every other one — with three pipelines, "the other one" is no
            # longer a well-defined place.
            ref = args.get("column", "")
            col = next(
                (
                    found
                    for p in (c.pipeline, *(x for x in PIPELINES if x != c.pipeline))
                    if (found := pipelines.resolve(p, ref))
                ),
                None,
            )
            if not col:
                return ok("no such column")
            await board.move_card(c.id, col.id)
            return ok(f"'{c.title}' moved to {col.pipeline}/{col.title}")

        @tool("trash_card", "Archive a card (recoverable from the trash)", {"card_id": str})
        async def trash_card(args):
            c = await board.trash_card(args.get("card_id", ""))
            return ok(f"'{c.title}' moved to the trash" if c else "no such card")

        @tool("list_columns", "The current columns of both pipelines, with their gates and workers", {})
        async def list_columns(args):
            return ok(json.dumps(self._columns_json(), indent=2))

        @tool("read_worker", "Read a column's worker instruction file", {"pipeline": str, "column": str})
        async def read_worker(args):
            return ok(self._read_worker(args.get("pipeline", ""), args.get("column", "")))

        @tool(
            "write_worker",
            "Replace a column's worker instruction file (read it first; keep the frontmatter and the four sections)",
            {"pipeline": str, "column": str, "markdown": str},
        )
        async def write_worker(args):
            return ok(
                self._write_worker(
                    args.get("pipeline", ""), args.get("column", ""), args.get("markdown", "")
                )
            )

        return create_sdk_mcp_server(
            name="board",
            version="2.0.0",
            tools=[list_cards, create_card, move_card, trash_card, list_columns, read_worker, write_worker],
        )

    # ---- tool bodies shared by both servers ---------------------------
    def _columns_json(self) -> list[dict]:
        out = []
        for p in PIPELINES:
            for i, col in enumerate(self.board.pipelines.columns[p]):
                w = self.board.workers.ensure(col)
                out.append(
                    {
                        "pipeline": p,
                        "position": i + 1,
                        "column": col.title,
                        "slug": col.slug,
                        "gate": col.gate,
                        "worker": w.agent_name,
                        "contract": w.contract(),
                    }
                )
        return out

    def _read_worker(self, pipeline: str, column: str) -> str:
        col = self.board.pipelines.resolve(pipeline, column)
        if not col:
            return f"no such column '{column}' in pipeline '{pipeline}'"
        return self.board.workers.raw(col)

    def _write_worker(self, pipeline: str, column: str, markdown: str) -> str:
        col = self.board.pipelines.resolve(pipeline, column)
        if not col:
            return f"no such column '{column}' in pipeline '{pipeline}'"
        if not markdown.strip().startswith("---"):
            return "refused: the file must start with the '---' frontmatter block. Read it first."
        path = self.board.workers.write_raw(col, markdown)
        return f"worker for '{col.title}' rewritten: {path} (takes effect on the next delegation)"

    # ==================================================================
    # Real SDK paths
    # ==================================================================
    async def _real_card(self, card_id: str, text: str, from_human: bool = True) -> None:
        """Drive a card: manager turn → worker run → manager turn → …

        THE POINT OF THIS SHAPE. The worker used to run as a subagent INSIDE the manager's
        own query, so his session was held for the entire length of the worker's task. He was
        not thinking for those twenty minutes — he was just occupied — and the human asking
        him a question got queued behind work he was not doing.

        Now: he takes a short turn, calls `delegate`, and ENDS. His session goes idle. The
        worker runs as its own conversation. When it reports, we re-enter the manager's
        session — the SAME session, so the whole chat and everything he decided is still
        there — and he judges it.

        So the manager is idle exactly when a worker is working, which is when you want to be
        able to talk to him.
        """
        next_input = text
        # THE FIRST TURN IS A REPLY, if a human is the one who woke him.
        #
        # This is the bug I shipped an hour ago: his prose was filed as a note on every
        # supervision turn — including the one triggered by the human saying "go ahead". So
        # the orders landed, the work started, and the human got NOTHING BACK. Silence in
        # answer to an instruction is indistinguishable from a system that ignored you.
        #
        # The rule is not "supervision is quiet". It is: **whoever woke him is who he answers
        # to.** Woken by the human → reply to them. Woken by a worker's report → file it.
        kind = "reply" if from_human else "drive"
        for _ in range(40):  # a delegate-loop that never terminates is a bug, not a feature
            delegation = await self._manager_turn(card_id, next_input, kind=kind)
            kind = "drive"  # every turn after the first is the machine waking him, not you
            if not delegation:
                # Did he stop because the card is done, or because we told him to wind down?
                # Only the second one is owed a "carry on" when the board takes work again.
                if card_id in self._wound_down:
                    await self._wind_down_card(card_id)
                return
            worker, instructions = delegation
            report = await self._run_worker(card_id, worker, instructions)

            # We told it to stop. Do not make the manager judge a half-finished report — end
            # the run cleanly and let the card pick itself back up on the other side.
            if card_id in self._interrupted:
                await self._wind_down_card(card_id)
                return

            next_input = (
                f"[THE WORKER HAS REPORTED BACK — this is `{worker}`'s report, verbatim.]\n\n"
                f"{report}\n\n"
                "Now supervise it, exactly as your loop says: spot-check the claim against "
                "what is actually on disk, then decide. Do not take the report on trust."
            )
        await self.board.append_message(
            card_id,
            "system",
            "⚠️ Stopped: the manager delegated 40 times without finishing. Something is "
            "looping. Nothing is lost — look at the card folder and tell me what to do.",
        )

    async def _chat_turn(self, card_id: str, text: str) -> None:
        """The human, talking to the manager WHILE a worker runs. He is idle; answer now.

        He cannot dispatch anything from here (the `delegate` tool refuses on a chat turn) —
        a worker is already out, and its report is coming to him. But it is the same session,
        so what is said here is in front of him when he judges that report.
        """
        await self._manager_turn(card_id, text, kind="chat")

    async def _manager_turn(self, card_id: str, text: str, kind: str = "drive") -> tuple[str, str] | None:
        """ONE turn of the manager, on the card's own session. Returns a delegation, if he
        asked for one. Short by construction: he has no worker to run, only a tool that
        records that he wants one."""
        from claude_agent_sdk import ClaudeAgentOptions, query

        # Serialise turns on this session — never two at once — but hold the lock ONLY for
        # the turn. A worker run happens outside it, which is what leaves him free to talk.
        async with self._lock_for(f"session:{card_id}"):
            self._delegations.pop(card_id, None)
            self._turn_kind[card_id] = kind
            card = self.board.cards.get(card_id)
            if not card:
                return None
            if kind == "drive":
                await self.board.set_working(card_id, "")  # him, not a worker
            options = ClaudeAgentOptions(
                cwd=self.config.repo_dir,
                system_prompt={
                    "type": "preset",
                    "preset": "claude_code",
                    "append": manager_prompt_for(
                        card,
                        self.board.pipelines,
                        self.board.workers,
                        self.board.abs_dir(card),
                        self.board.data_dir,
                    ),
                },
                # NO `agents=`. That is the whole change: he cannot run a worker himself.
                mcp_servers={"board": self._card_tools(card_id)},
                permission_mode=self.config.permission_mode,
                model=self.config.model,
                resume=card.session_id,
            )
            # WHERE HIS PROSE GOES, and it depends on who started the turn.
            #   chat  — the human just spoke. He is answering them. It is a message.
            #   drive — nobody asked him anything; he is supervising. It is a RECORD, filed on
            #           the card. If he needs the human he must say so out loud, with
            #           `ask_human` — which is the only door into their chat.
            # This is structural on purpose. Asking him nicely to be quiet does not work; the
            # wall of reasoning arrives anyway, because narrating is what a turn produces.
            #   reply — the human just gave him an order. He answers them.
            #   chat  — the human spoke while a worker runs. He answers them.
            #   drive — a worker's report woke him. Nobody asked; it is a RECORD, filed on the
            #           card. To reach the human from here he must use `ask_human`, and he is
            #           told to do that only when he needs something.
            say = (
                (lambda t: self.board.append_message(card_id, "manager", t))
                if kind in ("chat", "reply")
                else (lambda t: self.board.append_note(card_id, t))
            )
            await self._pump(
                query(prompt=text, options=options),
                on_session=lambda sid: self.board.set_session(card_id, sid),
                on_text=say,
                on_system=lambda t: self.board.append_message(card_id, "system", t),
                on_worker=lambda t: self.board.append_message(card_id, "worker", t),
                on_activity=lambda t: self.board.append_message(card_id, "activity", t),
            )
            return self._delegations.pop(card_id, None)

    async def _run_worker(self, card_id: str, worker: str, instructions: str) -> str:
        """Run ONE worker, as its own conversation, and KEEP A HANDLE ON IT.

        The handle is the point. A worker is a long job — many minutes — and an update that
        waits for it politely is an update that takes ten minutes. So the worker runs through
        a `ClaudeSDKClient` (streaming), which can be **interrupted**: told to stop, mid-task.
        Whatever it has already written to disk stays written; the card folder is the hand-off
        and it is real. What it loses is the rest of a turn it had not finished anyway.

        `query()` — the one-shot call this used to use — cannot be interrupted at all. That is
        the whole reason for the rewrite.

        No board tools: a worker cannot move a card, cannot delegate, and cannot decide what
        happens next. It does the work of one column and hands back.
        """
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        card = self.board.cards.get(card_id)
        col = self.board.pipelines.get(card.column) if card else None
        if not col:
            return "ENTRY: BLOCKED — the card is not in a column, so there is no work to do."

        rt = self.board.workers.runtime(col)

        # WAS THIS WORKER STOPPED MID-TASK BY A RESTART? Then resume the conversation it was
        # stopped in — with everything it had read, decided and half-written still in front of
        # it — rather than starting the column again from a blank sheet. That is the whole
        # difference between "stopped" and "lost".
        resume_session = card.worker_session if card.worker_name == rt["name"] else None
        if resume_session:
            instructions = (
                "[RESUMED — the system restarted while you were working. This is the same "
                "conversation you were stopped in, so everything you knew is still here.]\n\n"
                "You wrote `WIP.md` into the card folder before you stopped. Read it, and read "
                "what is actually on disk now, then CARRY ON FROM THERE. Do not start over, and "
                "do not redo work you have already done.\n\n"
                "The original briefing, for reference:\n\n" + instructions
            )

        self._workers_running.add(card_id)
        await self.board.set_working(card_id, rt["name"])
        await self.board.append_message(
            card_id,
            "activity",
            f"→ {'resuming' if resume_session else 'delegating to'} **{rt['name']}**",
        )
        chunks: list[str] = []
        session_id: str | None = resume_session

        def _keep_session(sid: str) -> None:
            nonlocal session_id
            session_id = sid

        options = ClaudeAgentOptions(
            cwd=self.config.repo_dir,
            system_prompt={"type": "preset", "preset": "claude_code", "append": rt["prompt"]},
            allowed_tools=rt["tools"],
            permission_mode=self.config.permission_mode,
            model=rt["model"] or self.config.model,
            resume=resume_session,
        )
        client = ClaudeSDKClient(options=options)
        try:
            await client.connect()
            self._worker_clients[card_id] = client
            # If the human clicked Restart while we were connecting, stop it immediately —
            # otherwise it runs for ten minutes that nobody is waiting for.
            if self.draining:
                await self._interrupt_worker(card_id)
            await client.query(instructions)
            await self._pump(
                client.receive_response(),
                on_session=lambda sid: _sync(_keep_session, sid),
                on_text=lambda t: _collect(chunks, t),
                on_system=lambda t: self.board.append_message(card_id, "system", t),
                on_worker=lambda t: self.board.append_message(card_id, "worker", t),
                on_activity=lambda t: self.board.append_message(card_id, "activity", t),
            )

            # It was told to stop. Now give it the one thing it needs before it goes: a chance
            # to WRITE DOWN WHERE IT GOT TO. It is the same live session — it still knows
            # everything — and this costs seconds, not minutes.
            if card_id in self._interrupted:
                await self.board.append_message(
                    card_id, "activity", f"⏸ asking **{rt['name']}** to save its place"
                )
                await client.query(SAVE_AND_STOP)
                await self._pump(
                    client.receive_response(),
                    on_session=lambda sid: _sync(_keep_session, sid),
                    on_text=lambda t: _collect(chunks, t),
                    on_system=lambda t: self.board.append_message(card_id, "system", t),
                    on_worker=lambda t: self.board.append_message(card_id, "worker", t),
                    on_activity=lambda t: self.board.append_message(card_id, "activity", t),
                )
        except Exception as e:  # noqa: BLE001 — an interrupted stream can end untidily
            if card_id not in self._interrupted:
                raise
            log.warning("worker %s ended after an interrupt: %s", rt["name"], e)
        finally:
            self._worker_clients.pop(card_id, None)
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._workers_running.discard(card_id)
            await self.board.set_working(card_id, "")

        if card_id in self._interrupted:
            # Remember the thread it was thinking in. Next time this column is delegated we
            # RESUME it rather than start it, so nothing it knew is lost.
            await self.board.set_stopped_worker(card_id, rt["name"], session_id)
        else:
            await self.board.clear_stopped_worker(card_id)

        report = "\n\n".join(chunks).strip() or "(the worker returned nothing)"
        if card_id in self._interrupted:
            report = (
                "[STOPPED — it saved its place and will be RESUMED in this same conversation, "
                "with everything it knew.]\n\n" + report
            )
        await self.board.append_message(card_id, "worker", f"**{rt['name']}** reports:\n\n{report}")
        return report

    async def _interrupt_worker(self, card_id: str) -> bool:
        """Tell this card's worker to stop. Returns True if there was one to tell."""
        client = self._worker_clients.get(card_id)
        if not client:
            return False
        self._interrupted.add(card_id)
        try:
            await client.interrupt()
            log.warning("interrupted the worker on %s (winding down)", card_id)
            return True
        except Exception as e:  # noqa: BLE001 — it may have finished as we asked
            log.warning("could not interrupt the worker on %s: %s", card_id, e)
            return False

    async def stop_workers(self) -> int:
        """**Tell every running worker to stop.** This is what makes winding down take a minute
        instead of ten: the alternative is waiting for jobs that run for many minutes, none of
        which anyone is waiting on any more."""
        ids = list(self._worker_clients)
        stopped = 0
        for card_id in ids:
            if await self._interrupt_worker(card_id):
                stopped += 1
                await self.board.append_note(
                    card_id,
                    "⏸ Told the worker to stop. It is writing `WIP.md` — what it finished, what "
                    "it was mid-way through, what is left — and it will be RESUMED in the same "
                    "conversation when work starts again, with everything it knew. Nothing it "
                    "did is thrown away.",
                )
        return stopped

    async def _real_worker(self, key: str, col, text: str) -> None:
        """A conversation ABOUT one column's worker — its contract. Not a card run."""
        from claude_agent_sdk import ClaudeAgentOptions, query

        chat = self.board.worker_chat(key)
        worker = self.board.workers.ensure(col)
        options = ClaudeAgentOptions(
            cwd=self.config.repo_dir,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": worker_chat_prompt_for(
                    col, worker, self.board.pipelines, self.board.workers, self.board.data_dir
                ),
            },
            # No agents here: this conversation EDITS a worker, it never runs one.
            mcp_servers={"board": self._worker_tools(col)},
            permission_mode=self.config.permission_mode,
            model=self.config.model,
            resume=chat.session_id,
        )
        await self._pump(
            query(prompt=text, options=options),
            on_session=lambda sid: self.board.set_worker_session(key, sid),
            on_text=lambda t: self.board.append_worker_message(key, "manager", t),
            on_system=lambda t: self.board.append_worker_message(key, "system", t),
            on_worker=lambda t: self.board.append_worker_message(key, "worker", t),
            on_activity=lambda t: self.board.append_worker_message(key, "activity", t),
        )

    async def _real_policy(self, key: str, text: str) -> None:
        """The manager, talking about the file that governs him — and rewriting it."""
        from claude_agent_sdk import ClaudeAgentOptions, query

        chat = self.board.worker_chat(key)
        options = ClaudeAgentOptions(
            cwd=self.config.repo_dir,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                # NOTE: no policy.block() appended here, and that is deliberate. His standing
                # orders are the SUBJECT of this conversation, quoted in the prompt above —
                # appending them again as instructions would have him obeying the very text
                # he is meant to be editing.
                "append": policy_chat_prompt_for(self.board.data_dir),
            },
            mcp_servers={"board": self._policy_tools()},
            permission_mode=self.config.permission_mode,
            model=self.config.model,
            resume=chat.session_id,
        )
        await self._pump(
            query(prompt=text, options=options),
            on_session=lambda sid: self.board.set_worker_session(key, sid),
            on_text=lambda t: self.board.append_worker_message(key, "manager", t),
            on_system=lambda t: self.board.append_worker_message(key, "system", t),
            on_worker=lambda t: self.board.append_worker_message(key, "worker", t),
            on_activity=lambda t: self.board.append_worker_message(key, "activity", t),
        )

    async def _mock_policy(self, key: str, text: str) -> None:
        current = policy.read(self.board.data_dir)
        await self.board.append_worker_message(
            key,
            "manager",
            "*(mock mode — no API key, so I'm not really thinking.)*\n\n"
            f"These are my standing orders ({len(current.splitlines())} lines). "
            "Add your key to `.env` and I'll actually rewrite them with you.",
        )

    def _policy_tools(self):
        """Read and write the manager's own standing orders. Nothing else — this
        conversation has no business touching cards, columns or workers."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        data_dir = self.board.data_dir

        def ok(text: str) -> dict:
            return {"content": [{"type": "text", "text": text}]}

        @tool("read_policy", "Read your own standing orders (MANAGER.md)", {})
        async def read_policy(args):
            return ok(policy.read(data_dir) or "(empty — there are no standing orders yet)")

        @tool(
            "write_policy",
            "Replace your own standing orders. Read them first; rewrite the WHOLE file, "
            "preserving every rule the human did not ask you to change.",
            {"markdown": str},
        )
        async def write_policy(args):
            md = args.get("markdown", "")
            if not (md or "").strip():
                return ok("refused: that would erase the human's standing orders entirely.")
            path = policy.write(data_dir, md)
            return ok(f"written to {path}. It takes effect on my very next message.")

        return create_sdk_mcp_server(
            name="board", version="2.0.0", tools=[read_policy, write_policy]
        )

    def _worker_tools(self, col):
        """Read and write THIS worker, and see the board's shape. Nothing else — this
        conversation has no business moving cards."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        def ok(text: str) -> dict:
            return {"content": [{"type": "text", "text": text}]}

        @tool("read_worker", "Read a column's worker instruction file", {"pipeline": str, "column": str})
        async def read_worker(args):
            return ok(
                self._read_worker(
                    args.get("pipeline") or col.pipeline, args.get("column") or col.slug
                )
            )

        @tool(
            "write_worker",
            "Replace a column's worker instruction file (read it first; keep the frontmatter and the four sections)",
            {"pipeline": str, "column": str, "markdown": str},
        )
        async def write_worker(args):
            return ok(
                self._write_worker(
                    args.get("pipeline") or col.pipeline,
                    args.get("column") or col.slug,
                    args.get("markdown", ""),
                )
            )

        @tool("list_columns", "The current columns of every pipeline, with their gates and workers", {})
        async def list_columns(args):
            return ok(json.dumps(self._columns_json(), indent=2))

        return create_sdk_mcp_server(
            name="board",
            version="2.0.0",
            tools=[read_worker, write_worker, list_columns],
        )

    async def _pump(self, stream, on_session, on_text, on_system, on_worker, on_activity=None, on_working=None) -> None:
        """Drain an SDK query, projecting it onto a chat thread.

        The interesting part is the worker hand-back: when the manager delegates with the
        Agent tool we remember the tool_use id, and when its result comes back we post the
        worker's report into the thread. That report is exactly what the manager is about to
        make his decision on, so the human sees the same evidence he does.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        pending: dict[str, str] = {}  # tool_use_id -> worker name

        async for msg in stream:
            sid = getattr(msg, "session_id", None)
            if sid:
                await on_session(sid)

            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        t = (block.text or "").strip()
                        if t:
                            await on_text(t)
                    elif isinstance(block, ToolUseBlock):
                        label = self._describe_tool(block)
                        if block.name in ("Task", "Agent"):
                            inp = block.input or {}
                            who = str(inp.get("subagent_type") or inp.get("agent") or "worker")
                            pending[block.id] = who
                            # From here until its report comes back, the WORKER is the one
                            # working — not the manager. Say so on the board.
                            if on_working:
                                await on_working(who)
                        if label:
                            # ACTIVITY, not a message to the human. Every shell command the
                            # manager runs used to land in the chat with the same weight as
                            # something he actually said to you — which buried the one thing
                            # you were reading it for. Kept (it is the audit trail, and you
                            # can unhide it), but it is not addressed to you.
                            await (on_activity or on_system)(label)

            elif isinstance(msg, UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                        worker = pending.pop(block.tool_use_id)
                        report = _flatten_result(block.content)
                        if on_working:
                            await on_working("")  # the worker handed back; the manager has it
                        if report:
                            await on_worker(f"**{worker}** reports:\n\n{report}")

            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    await on_system(f"⚠️ {msg.result or 'run ended with an error'}")

    @staticmethod
    def _describe_tool(block) -> str:
        name = block.name
        if name.startswith("mcp__board__"):
            return ""  # board tools broadcast their own effects
        inp = block.input or {}
        if name in ("Task", "Agent"):
            w = inp.get("subagent_type") or inp.get("agent") or "worker"
            return f"→ delegating to **{w}**"
        if name == "Bash":
            return f"⌘ {str(inp.get('command', ''))[:140]}"
        if name in ("Write", "Edit"):
            return f"✎ {name} {inp.get('file_path') or inp.get('path') or ''}"
        return ""

    # ==================================================================
    # Mock paths (no API key needed) — same shape, scripted
    # ==================================================================
    async def _mock_worker(self, key: str, col, text: str) -> None:
        w = self.board.workers.ensure(col)
        c = w.contract()
        await self.board.append_worker_message(
            key,
            "manager",
            "*(mock mode — no API key, so I'm not really thinking.)*\n\n"
            f"This is the **{col.title}** worker of the "
            f"{PIPELINE_TITLES.get(col.pipeline, col.pipeline)} pipeline.\n\n"
            f"**Entry:** {(c['entry'] or '—').splitlines()[0]}\n"
            f"**Exit:** {(c['exit'] or '—').splitlines()[0]}\n\n"
            "Add your key to `.env` and I'll actually rewrite this contract for you.",
        )

    async def _real_board(self, manager_id: str, text: str) -> None:
        """The manager's OWN chat — about the board as a whole, not any one card.

        The board-level twin of `_manager_turn`: one `query()` on the manager's own session,
        with the board-wide tools (list/create/move/trash cards, read/write workers) and his
        board prompt. His prose is a reply to the human, because on this chat a human is always
        who woke him — there is no silent supervision here.
        """
        from claude_agent_sdk import ClaudeAgentOptions, query

        m = self.board.managers.get(manager_id)
        if not m:
            return
        options = ClaudeAgentOptions(
            cwd=self.config.repo_dir,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": board_prompt_for(
                    m, self.board.pipelines, self.board.workers, self.board.data_dir
                ),
            },
            mcp_servers={"board": self._board_tools(manager_id)},
            permission_mode=self.config.permission_mode,
            model=self.config.model,
            resume=m.session_id,
        )
        await self._pump(
            query(prompt=text, options=options),
            on_session=lambda sid: self.board.set_manager_session(manager_id, sid),
            on_text=lambda t: self.board.append_manager_message(manager_id, "manager", t),
            on_system=lambda t: self.board.append_manager_message(manager_id, "system", t),
            on_worker=lambda t: self.board.append_manager_message(manager_id, "worker", t),
            on_activity=lambda t: self.board.append_manager_message(manager_id, "activity", t),
        )

    async def _mock_board(self, manager_id: str, text: str) -> None:
        cols = self._columns_json()
        flows = "\n".join(
            f"**{PIPELINE_TITLES.get(p, p)}:** "
            + " → ".join(
                c["column"] + ("*" if c["gate"] else "") for c in cols if c["pipeline"] == p
            )
            for p in PIPELINES
        )
        await self.board.append_manager_message(
            manager_id,
            "manager",
            "*(mock mode — no API key, so I'm not really thinking.)*\n\n"
            f"{flows}\n\n"
            "`*` marks a gate. Add your key to `.env` and I'll actually reason about "
            "your pipelines, write workers, and manage cards.",
        )

    @staticmethod
    def _mock_artifact_name(worker) -> str | None:
        """The file a column's contract says it produces — parsed out of its Output section."""
        m = re.search(r"`([A-Za-z0-9_\-]+\.md)`", worker.contract().get("output", ""))
        return m.group(1) if m else None

    async def _mock_card(self, card_id: str, text: str) -> None:
        card = self.board.cards[card_id]

        # A backlog card has no column and no worker. Triage it: give it a type, and route it
        # if the human said to go. Same two decisions the real manager makes.
        if card.pipeline == BACKLOG:
            if card.kind not in KINDS:
                guess = (
                    MAINTENANCE
                    if re.search(
                        r"\b(bug|broken|fix|malfunction|error|fails?|slow|wrong|crash)\b",
                        f"{card.title} {card.description}",
                        re.I,
                    )
                    else FEATURE
                )
                await self.board.set_card_kind(card_id, guess)
                await self.board.append_message(
                    card_id, "manager", f"*(mock)* Typed this as a **{guess}** card."
                )
            # Triage TYPES a card. It never routes one — the human decides when work starts.
            # (And the machine prompt itself contains the word "start", which is exactly how
            # this went wrong the first time: the triage run read its own instructions as the
            # human saying "go", and threw an untouched card straight down a pipeline.)
            if text.lstrip().startswith("[AUTOMATIC"):
                return
            low = text.strip().lower()
            if "expedit" in low:
                target = EXPED
            elif any(w in low for w in ("start", "go", "work on", "build")):
                target = EXPED if self.board.cards[card_id].kind == FEATURE else MAINT
            else:
                return
            routed = await self.board.route_card(card_id, target)
            if routed:
                await self.board.append_message(
                    card_id,
                    "manager",
                    f"*(mock)* Routed to the **{PIPELINE_TITLES.get(target, target)}** pipeline.",
                )
            return

        col = self.board.pipelines.get(card.column)
        if not col:
            return
        approved = any(w in text.strip().lower() for w in APPROVE_WORDS)

        if col.gate:
            if not approved:
                await self.board.append_message(
                    card_id,
                    "manager",
                    f"**{col.title}** is a gate — I need your approval before this card moves on. "
                    "Reply **approve** when you're ready.",
                )
                return
            # cross the gate
            if (
                card.pipeline in ORIGIN_PIPELINES
                and self.board.pipelines.next_column(col.id) is None
            ):
                await self.board.promote_to_build(card_id)
                await self.board.append_message(
                    card_id, "manager", "✅ Approved. Handing the card to the **build** pipeline."
                )
            else:
                nxt = self.board.pipelines.next_column(col.id)
                if not nxt:
                    await self.board.append_message(card_id, "manager", "This card is at the end of the board.")
                    return
                await self.board.move_card(card_id, nxt.id)
                await self.board.append_message(card_id, "manager", f"✅ Approved. Moving to **{nxt.title}**.")

        await self._mock_advance(card_id)

    async def _mock_advance(self, card_id: str) -> None:
        """Walk the card forward through whatever columns are configured, stopping at gates."""
        for _ in range(50):  # safety valve against a pathological config
            card = self.board.cards[card_id]
            col = self.board.pipelines.get(card.column)
            if not col:
                return
            worker = self.board.workers.ensure(col)

            # The board is winding down — paused, or shipping. Stop before starting another
            # column, exactly as the real manager does when `delegate` refuses. Mock mode is
            # only worth having if it winds down like the real thing; it is what the tests
            # drive, so a mock that worked straight through a pause would assert nothing.
            if self.draining:
                await self._wind_down_card(card_id, worker.agent_name)
                return

            await self.board.set_stage(card_id, col.slug)
            await self.board.append_message(
                card_id, "activity", f"→ delegating to **{worker.agent_name}**"
            )
            await self.board.set_working(card_id, worker.agent_name)  # the WORKER, not him
            await asyncio.sleep(0.4)
            await self.board.set_working(card_id, "")

            produced = "none"
            name = self._mock_artifact_name(worker)
            if name:
                abs_dir = self.board.abs_dir(card)
                os.makedirs(abs_dir, exist_ok=True)
                with open(os.path.join(abs_dir, name), "w", encoding="utf-8") as fh:
                    fh.write(
                        f"# {name}\n\n_Mock artifact for card `{card.id}` — {card.title}_\n\n"
                        f"Written by the **{col.title}** worker.\n\n"
                        f"## Exit criteria this claims to meet\n\n{worker.contract().get('exit', '')}\n"
                    )
                await self.board.set_artifact(card_id, name, os.path.join(card.dir, name))
                produced = name

            await self.board.append_message(
                card_id,
                "worker",
                f"**{worker.agent_name}** reports:\n\n"
                f"ENTRY: PASS\nWORK: ran the {col.title} contract (mock).\n"
                f"OUTPUT: {produced}\nEXIT: MET\nFLAGS: none",
            )
            await asyncio.sleep(0.3)

            if col.gate:
                await self.board.append_message(
                    card_id,
                    "manager",
                    f"Checked **{col.title}** — exit criteria met.\n\n"
                    f"⏸ **{col.title}** is a gate. Reply **approve** to continue.",
                )
                return

            nxt = self.board.pipelines.next_column(col.id)
            if not nxt:
                await self.board.append_message(
                    card_id, "manager", f"**{col.title}** done — the card is at the end of the board."
                )
                return
            await self.board.append_message(
                card_id, "manager", f"Checked **{col.title}** — exit criteria met. Moving to **{nxt.title}**."
            )
            await self.board.move_card(card_id, nxt.id)

    # backwards-compatible alias
    handle_user_message = handle_card_message


def _flatten_result(content) -> str:
    """A tool result's content may be a string or a list of blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p).strip()
    return ""
