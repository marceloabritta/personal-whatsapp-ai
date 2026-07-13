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

from .agents import board_prompt_for, manager_prompt_for, worker_chat_prompt_for
from .board import Board
from .journal import CARD, MANAGER, WORKER, Journal
from .models import BUILD, ORIGIN_PIPELINES, PIPELINE_TITLES, PIPELINES, PLAN

log = logging.getLogger("manager")

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

    # ---- helpers -----------------------------------------------------
    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _all_columns(self):
        return self.board.pipelines.all_columns()

    def _worker_defs(self):
        return self.board.workers.definitions(self._all_columns())

    # ---- public entry: a card ----------------------------------------
    async def handle_card_message(self, card_id: str, text: str, resuming: bool = False) -> None:
        card = self.board.cards.get(card_id)
        if not card:
            return
        if not resuming:
            # A resume prompt is machinery, not something the human typed. Don't put it in
            # their thread as if they had said it.
            await self.board.append_message(card_id, "user", text)
        lock = self._lock_for(f"card:{card_id}")
        if lock.locked():
            await self.board.append_message(
                card_id, "system", "Manager is still working the previous message; queued."
            )
        async with lock:
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
                    await self._real_card(card_id, text)
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
                if finished:
                    self.journal.finish(CARD, card_id)
                    await self.board.set_busy(card_id, False)

    # ---- public entry: the board-level chat ---------------------------
    async def handle_board_message(self, manager_id: str, text: str, resuming: bool = False) -> None:
        m = self.board.managers.get(manager_id)
        if not m:
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

    async def handle_worker_message(self, key: str, text: str, resuming: bool = False) -> None:
        """A conversation about ONE column's worker — its contract, not any card.

        Same shape as the card and board handlers, deliberately: journal the run BEFORE it
        starts, mark busy, run, strike it off. A new kind of long-running work that is not
        journalled is a new kind of work that cannot be recovered when the process dies.
        """
        col = self._column_for_key(key)
        if not col:
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
                    await self._mock_worker(key, col, text)
                else:
                    await self._real_worker(key, col, text)
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

        @tool("note", "Post a status note into this card's chat", {"text": str})
        async def note(args):
            await board.append_message(card_id, "manager", args["text"])
            return ok("noted")

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
                card_info, set_stage, note, move_next, move_card,
                promote_to_build, list_columns, read_worker, write_worker,
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
    async def _real_card(self, card_id: str, text: str) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, query

        card = self.board.cards[card_id]
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
            agents=self._worker_defs(),
            mcp_servers={"board": self._card_tools(card_id)},
            permission_mode=self.config.permission_mode,
            model=self.config.model,
            resume=card.session_id,
        )
        await self._pump(
            query(prompt=text, options=options),
            on_session=lambda sid: self.board.set_session(card_id, sid),
            on_text=lambda t: self.board.append_message(card_id, "manager", t),
            on_system=lambda t: self.board.append_message(card_id, "system", t),
            on_worker=lambda t: self.board.append_message(card_id, "worker", t),
        )

    async def _real_board(self, manager_id: str, text: str) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, query

        m = self.board.managers[manager_id]
        options = ClaudeAgentOptions(
            cwd=self.config.repo_dir,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": board_prompt_for(
                    m, self.board.pipelines, self.board.workers, self.board.data_dir
                ),
            },
            # No workers here on purpose: the board chat shapes the pipeline, it doesn't run it.
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
        )

    async def _real_worker(self, key: str, col, text: str) -> None:
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

    async def _pump(self, stream, on_session, on_text, on_system, on_worker) -> None:
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
                            pending[block.id] = str(
                                inp.get("subagent_type") or inp.get("agent") or "worker"
                            )
                        if label:
                            await on_system(label)

            elif isinstance(msg, UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                        worker = pending.pop(block.tool_use_id)
                        report = _flatten_result(block.content)
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

            await self.board.set_stage(card_id, col.slug)
            await self.board.append_message(
                card_id, "system", f"→ delegating to **{worker.agent_name}**"
            )
            await asyncio.sleep(0.4)

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
