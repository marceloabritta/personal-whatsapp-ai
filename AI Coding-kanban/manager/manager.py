"""The manager service: drives one Claude Agent SDK conversation per card.

Public entry point is `Manager.handle_user_message(card_id, text)`. It routes the
message into the card's persistent session, streams the manager's output and tool
activity back onto the board, and persists the session id so context survives
restarts.

A `mock` mode runs a deterministic scripted pipeline so the whole board — every
column and both human gates — can be exercised without an API key. The real mode
uses the Agent SDK with the worker subagents defined in agents.py.
"""
from __future__ import annotations

import asyncio
import os

from .agents import build_workers, manager_prompt_for
from .board import Board
from .models import Column


class ManagerConfig:
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
        # Mock unless explicitly disabled or an API key is present.
        if mock is None:
            env = os.environ.get("MANAGER_MOCK")
            if env is not None:
                mock = env not in ("0", "false", "no", "")
            else:
                mock = not bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.mock = mock


class Manager:
    def __init__(self, board: Board, config: ManagerConfig):
        self.board = board
        self.config = config
        self.workers = build_workers()
        self._locks: dict[str, asyncio.Lock] = {}

    def _artifact_dir_rel(self, card_id: str) -> str:
        """Repo-relative path where this card's SCOPE/PLAN artifacts live."""
        abs_dir = os.path.join(self.config.data_dir, "cards", card_id)
        return os.path.relpath(abs_dir, self.config.repo_dir)

    # -- public entry --------------------------------------------------
    async def handle_user_message(self, card_id: str, text: str) -> None:
        card = self.board.cards.get(card_id)
        if not card:
            return
        await self.board.append_message(card_id, "user", text)
        lock = self._locks.setdefault(card_id, asyncio.Lock())
        if lock.locked():
            await self.board.append_message(
                card_id, "system", "Manager is still working the previous message; queued."
            )
        async with lock:
            await self.board.set_busy(card_id, True)
            try:
                if self.config.mock:
                    await self._run_mock(card_id, text)
                else:
                    await self._run_real(card_id, text)
            except Exception as e:  # noqa: BLE001 - surface any failure to the UI
                await self.board.append_message(card_id, "system", f"⚠️ manager error: {e}")
            finally:
                await self.board.set_busy(card_id, False)

    # -- real SDK path -------------------------------------------------
    def _board_server(self, card_id: str):
        from claude_agent_sdk import tool, create_sdk_mcp_server

        board = self.board

        @tool("move_card", "Move this card to a board column", {"column": str})
        async def move_card(args):
            await board.move_card(card_id, args["column"])
            return {"content": [{"type": "text", "text": f"card moved to {args['column']}"}]}

        @tool("set_stage", "Set the fine-grained status label of this card", {"stage": str})
        async def set_stage(args):
            await board.set_stage(card_id, args["stage"])
            return {"content": [{"type": "text", "text": f"stage set to {args['stage']}"}]}

        @tool("note", "Post a short status note into the card chat", {"text": str})
        async def note(args):
            await board.append_message(card_id, "manager", args["text"])
            return {"content": [{"type": "text", "text": "noted"}]}

        return create_sdk_mcp_server(name="board", version="1.0.0", tools=[move_card, set_stage, note])

    async def _run_real(self, card_id: str, text: str) -> None:
        from claude_agent_sdk import (
            query,
            ClaudeAgentOptions,
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
        )

        card = self.board.cards[card_id]
        options = ClaudeAgentOptions(
            cwd=self.config.repo_dir,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": manager_prompt_for(card, self._artifact_dir_rel(card_id)),
            },
            agents=self.workers,
            mcp_servers={"board": self._board_server(card_id)},
            permission_mode=self.config.permission_mode,  # headless: no blocking prompts
            model=self.config.model,
            resume=card.session_id,
        )

        async for msg in query(prompt=text, options=options):
            sid = getattr(msg, "session_id", None)
            if sid:
                await self.board.set_session(card_id, sid)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        t = (block.text or "").strip()
                        if t:
                            await self.board.append_message(card_id, "manager", t)
                    elif isinstance(block, ToolUseBlock):
                        await self._on_tool_use(card_id, block)
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    await self.board.append_message(
                        card_id, "system", f"⚠️ {msg.result or 'run ended with an error'}"
                    )

    async def _on_tool_use(self, card_id: str, block) -> None:
        name = block.name
        # board tools already broadcast their own effects; skip echoing them.
        if name.startswith("mcp__board__"):
            return
        inp = block.input or {}
        if name in ("Task", "Agent"):
            worker = inp.get("subagent_type") or inp.get("agent") or inp.get("name") or "worker"
            await self.board.append_message(card_id, "system", f"→ delegating to {worker}")
        elif name == "Bash":
            cmd = str(inp.get("command", ""))[:120]
            await self.board.append_message(card_id, "system", f"⌘ {cmd}")
        elif name in ("Write", "Edit"):
            path = inp.get("file_path") or inp.get("path") or ""
            await self.board.append_message(card_id, "system", f"✎ {name} {path}")

    # -- mock path (no API key needed) ---------------------------------
    async def _artifact_path(self, card_id: str, name: str) -> str:
        rel = os.path.join(self._artifact_dir_rel(card_id), name)
        abs_dir = os.path.join(self.config.data_dir, "cards", card_id)
        os.makedirs(abs_dir, exist_ok=True)
        path = os.path.join(abs_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# {name} for card {card_id}\n\n(demo artifact written by mock manager)\n")
        await self.board.set_artifact(card_id, name, rel)
        return path

    async def _step(self, card_id: str, stage: str, note: str, delay: float = 0.6) -> None:
        await self.board.set_stage(card_id, stage)
        await self.board.append_message(card_id, "manager", note)
        await asyncio.sleep(delay)

    async def _run_mock(self, card_id: str, text: str) -> None:
        card = self.board.cards[card_id]
        col = card.column
        low = text.strip().lower()

        approve_words = ("approve", "approved", "build it", "go ahead", "lgtm", "ship the plan")
        ship_words = ("ship", "ship it", "release", "deploy")

        if col in (Column.PLANS_READY.value,) and any(w in low for w in approve_words):
            await self._mock_build(card_id)
            return
        if col in (Column.BUILD_REVIEW.value,) and any(w in low for w in ship_words):
            await self.board.move_card(card_id, Column.SHIPPED.value)
            await self.board.set_stage(card_id, "shipped")
            await self.board.append_message(card_id, "manager", "🚢 Shipped to prod. Card closed.")
            return
        if col == Column.SHIPPED.value:
            await self.board.append_message(card_id, "manager", "This card is already shipped.")
            return

        # default / "start": run the planning stage
        await self._mock_plan(card_id)

    async def _mock_plan(self, card_id: str) -> None:
        await self.board.move_card(card_id, Column.PLANNING.value)
        await self._step(card_id, "scoping", "Delegating to **scoper** to map the user flow…")
        await self._artifact_path(card_id, "SCOPE.md")
        await self._step(card_id, "scope_review", "Delegating to **critic** for a scope review — applying one round of fixes.")
        await self._step(card_id, "planning", "Delegating to **planner** to draft the implementation plan…")
        await self._artifact_path(card_id, "PLAN.md")
        await self._step(card_id, "plan_review", "Delegating to **critic** for a plan review — applying one round of fixes.")
        await self.board.move_card(card_id, Column.PLANS_READY.value)
        await self.board.set_stage(card_id, "awaiting_plan_approval")
        await self.board.append_message(
            card_id, "manager",
            "📋 **Plan ready for approval.** SCOPE.md and PLAN.md are written. "
            "Reply **approve** to start building, or tell me what to change.",
        )

    async def _mock_build(self, card_id: str) -> None:
        await self.board.move_card(card_id, Column.BUILDING.value)
        await self._step(card_id, "drift_check", "Delegating to **drift_checker** — verdict: NO MATERIAL DRIFT.")
        await self._step(card_id, "preflight", "Delegating to **preflight** — verdict: GO.")
        await self._step(card_id, "writing_tests", "Delegating to **test_writer** — tests written (failing, as expected).")
        await self._step(card_id, "writing_code", "Delegating to **coder** — implementation written.")
        await self._step(card_id, "running_tests", "Running the test suite… ✅ all tests pass.")
        await self.board.move_card(card_id, Column.BUILD_REVIEW.value)
        await self.board.set_stage(card_id, "awaiting_review")
        await self.board.append_message(
            card_id, "manager",
            "🧪 **Build ready for review.** Tests are green. Reply **ship it** to release, "
            "or tell me what to fix.",
        )
