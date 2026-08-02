"""Board state: cards, managers, the pipeline config, and the folder tree on disk.

Every mutation goes through the Board so that (a) state is persisted, (b) the
card's FOLDER is kept in sync with the column the card sits in, and (c) the
change is pushed to every connected UI.

The folder tree mirrors the board exactly:

    data/cards/<pipeline>/<column-slug>/<card-id>-<card-slug>/
    data/cards/trash/<card-id>-<card-slug>/

Move a card and its folder moves with it. Rename a column and every folder under
it is renamed. The Board is transport-agnostic: it takes an async `broadcaster`
and knows nothing about FastAPI.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from typing import Awaitable, Callable

from .models import (
    BACKLOG,
    BUILD,
    KINDS,
    ORIGIN_PIPELINES,
    PIPELINES,
    PLAN,
    ROUTABLE,
    UNSET,
    Card,
    ChatMessage,
    Column,
    ManagerAgent,
    WorkerChat,
    kind_for_pipeline,
    valid_kind,
)
from .pipelines import PipelineConfig
from .workers import WorkerStore

Broadcaster = Callable[[dict], Awaitable[None]]

CARDS_DIRNAME = "cards"
TRASH_DIRNAME = "trash"


def _schema_version() -> int:
    """The schema this code writes. Imported lazily: migrations import the board's models."""
    from . import migrations

    return migrations.LATEST


SCHEMA_VERSION = _schema_version()


class Board:
    def __init__(
        self,
        data_dir: str,
        workers_dir: str | None = None,
        baseline_dir: str | None = None,
        broadcaster: Broadcaster | None = None,
    ):
        # `data_dir` is the working folder. Everything below it is state; nothing here is
        # ever derived from where the SYSTEM sits on disk — that is what made the old layout
        # unupgradable, and the workers dir defaults inside the data dir for the same reason.
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "board.json")
        self.cards: dict[str, Card] = {}
        self.order: list[str] = []
        self.managers: dict[str, ManagerAgent] = {}
        self.worker_chats: dict[str, WorkerChat] = {}  # "<pipeline>/<slug>" -> conversation
        self.schema_version: int = SCHEMA_VERSION
        self._broadcaster = broadcaster
        self._lock = asyncio.Lock()

        os.makedirs(data_dir, exist_ok=True)
        self.pipelines = PipelineConfig(data_dir)
        self.workers = WorkerStore(
            workers_dir or os.path.join(data_dir, "workers"),
            baseline_dir=baseline_dir,
            root=data_dir,
        )
        self._load()
        self._bootstrap()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        self.cards = {c["id"]: Card.from_dict(c) for c in raw.get("cards", [])}
        self.order = [cid for cid in raw.get("order", []) if cid in self.cards]
        for cid in self.cards:  # defensive: never lose a card to a bad order list
            if cid not in self.order:
                self.order.append(cid)
        self.managers = {m["id"]: ManagerAgent.from_dict(m) for m in raw.get("managers", [])}
        self.worker_chats = {
            k: WorkerChat.from_dict(v) for k, v in (raw.get("worker_chats") or {}).items()
        }
        self.schema_version = int(raw.get("schema_version", 0) or 0)

    def _bootstrap(self) -> None:
        """Make sure the invariants hold: one manager exists, every card has a manager,
        a valid column, and a folder on disk. Also creates every worker file."""
        dirty = False

        if self.schema_version != SCHEMA_VERSION:
            # A folder that reaches the Board has already been migrated (the server refuses
            # to start otherwise) — so this is just the stamp for a folder created today.
            self.schema_version = SCHEMA_VERSION
            dirty = True

        # NOTE: `busy` is deliberately NOT cleared here. A card that loads with busy=True is
        # the durable evidence that a run was cut off mid-flight, and clearing it on boot
        # would destroy the very fact the recovery path needs — hiding the spinner while the
        # lost work stayed lost. It is READ and ACTED ON at startup instead; see
        # manager/recovery.py, which resumes the run and only then clears the flag.

        if not self.managers:
            m = ManagerAgent.new("Manager")
            self.managers[m.id] = m
            dirty = True
        default_manager = next(iter(self.managers))

        for col in self.pipelines.all_columns():
            os.makedirs(self._column_dir(col), exist_ok=True)
            self.workers.ensure(col)  # writes workers/<pipeline>/<slug>.md if absent
        os.makedirs(os.path.join(self.data_dir, CARDS_DIRNAME, TRASH_DIRNAME), exist_ok=True)

        for card in self.cards.values():
            if not card.manager_id or card.manager_id not in self.managers:
                card.manager_id = default_manager
                dirty = True
            if card.trashed:
                continue
            # A card whose column vanished (deleted, or an old data file) goes BACK TO THE
            # BACKLOG — unrouted, keeping its type. That is the honest answer now: it is a
            # card nobody has placed. Dropping it into some pipeline's first column instead
            # would be a guess, and it would put work into a flow the human never chose.
            if card.pipeline != BACKLOG and not self.pipelines.get(card.column):
                card.pipeline, card.column = BACKLOG, ""
                dirty = True
            if self._ensure_card_dir(card):
                dirty = True

        if dirty:
            self._save()

    def busy_cards(self) -> list[str]:
        """Cards that were mid-run when the process last died. The recovery path's input."""
        return [c.id for c in self.cards.values() if c.busy]

    def busy_managers(self) -> list[str]:
        return [m.id for m in self.managers.values() if m.busy]

    def _save(self) -> None:
        payload = {
            "schema_version": self.schema_version,
            "cards": [self.cards[cid].to_dict() for cid in self.order if cid in self.cards],
            "order": self.order,
            "managers": [m.to_dict() for m in self.managers.values()],
            "worker_chats": {k: w.to_dict() for k, w in self.worker_chats.items()},
        }
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def set_broadcaster(self, broadcaster: Broadcaster) -> None:
        self._broadcaster = broadcaster

    # ---- the folder tree ---------------------------------------------
    def _column_dir(self, col: Column) -> str:
        return os.path.join(self.data_dir, CARDS_DIRNAME, col.pipeline, col.slug)

    def _rel(self, abs_path: str) -> str:
        return os.path.relpath(abs_path, self.data_dir)

    def abs_dir(self, card: Card) -> str:
        """Absolute path of the card's folder."""
        return os.path.join(self.data_dir, card.dir)

    def _backlog_dir(self) -> str:
        return os.path.join(self.data_dir, CARDS_DIRNAME, BACKLOG)

    def _ensure_card_dir(self, card: Card) -> bool:
        """Make the card's folder exist at the location its column implies. Returns True
        if anything changed on disk or on the card.

        A backlog card has no column, but it still gets a folder — the human may drop a
        screenshot or a log in it long before anyone decides which pipeline it belongs to,
        and the folder travels with the card when it is finally routed.
        """
        col = self.pipelines.get(card.column)
        if not col and card.pipeline != BACKLOG:
            return False
        parent = self._backlog_dir() if not col else self._column_dir(col)
        want_abs = os.path.join(parent, card.folder_name())
        want_rel = self._rel(want_abs)
        have_abs = os.path.join(self.data_dir, card.dir) if card.dir else ""

        if card.dir == want_rel and os.path.isdir(want_abs):
            return False
        os.makedirs(os.path.dirname(want_abs), exist_ok=True)
        if have_abs and os.path.isdir(have_abs) and os.path.abspath(have_abs) != os.path.abspath(want_abs):
            self._move_dir(have_abs, want_abs)
        else:
            os.makedirs(want_abs, exist_ok=True)
        card.dir = want_rel
        return True

    @staticmethod
    def _move_dir(src: str, dst: str) -> None:
        """Move a card folder, merging into the destination if it somehow already exists."""
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            shutil.move(src, dst)
            return
        for name in os.listdir(src):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if os.path.exists(d):
                continue
            shutil.move(s, d)
        try:
            os.rmdir(src)
        except OSError:
            pass

    # ---- snapshots ---------------------------------------------------
    def snapshot(self) -> dict:
        cards = []
        for cid in self.order:
            c = self.cards.get(cid)
            if not c or c.trashed:
                continue
            col = self.pipelines.get(c.column)
            cards.append(
                {
                    "id": c.id,
                    "title": c.title,
                    "description": c.description,
                    "pipeline": c.pipeline,
                    "kind": c.kind,
                    "column": c.column,
                    "manager_id": c.manager_id,
                    "stage": c.stage,
                    "gate": bool(col and col.gate),
                    "busy": c.busy,
                    "working": c.working,
                    "error": c.error,
                    "dir": c.dir,
                    "artifacts": c.artifacts,
                    "updated_at": c.updated_at,
                }
            )
        return {
            "type": "board",
            "pipelines": self.pipelines.snapshot(),
            "cards": cards,
            "managers": [
                {"id": m.id, "name": m.name, "emoji": m.emoji, "busy": m.busy}
                for m in self.managers.values()
            ],
            "trash_count": sum(1 for c in self.cards.values() if c.trashed),
        }

    def card_view(self, card_id: str) -> dict | None:
        c = self.cards.get(card_id)
        if not c:
            return None
        d = c.to_dict()
        d["type"] = "card"
        col = self.pipelines.get(c.column)
        d["column_title"] = col.title if col else "—"
        d["gate"] = bool(col and col.gate)
        d["abs_dir"] = self.abs_dir(c)
        return d

    def manager_view(self, manager_id: str) -> dict | None:
        m = self.managers.get(manager_id)
        if not m:
            return None
        d = m.to_dict()
        d["type"] = "manager"
        d["card_count"] = sum(
            1 for c in self.cards.values() if c.manager_id == manager_id and not c.trashed
        )
        return d

    def trash_view(self) -> dict:
        items = []
        for cid in self.order:
            c = self.cards.get(cid)
            if not c or not c.trashed:
                continue
            items.append(
                {"id": c.id, "title": c.title, "dir": c.dir, "updated_at": c.updated_at}
            )
        return {"type": "trash", "cards": items}

    async def _emit(self, message: dict) -> None:
        if self._broadcaster:
            await self._broadcaster(message)

    async def broadcast_board(self) -> None:
        await self._emit(self.snapshot())

    # ---- cards -------------------------------------------------------
    async def add_card(
        self,
        title: str,
        description: str = "",
        manager_id: str | None = None,
        pipeline: str = BACKLOG,
        kind: str = "",
    ) -> Card:
        """Every card is born in the BACKLOG, unrouted.

        That is the whole point of the backlog: a card arrives as a request, and *which
        pipeline it belongs down* is a decision — the manager's, or the human's — not
        something the creation form should be quietly making for you.

        `kind` may be left empty. The card is then `unset`, and the manager is expected to
        classify it immediately (see Manager.triage_card). A card must not leave the backlog
        still holding `unset`.

        `pipeline` is still accepted so a caller can put a card straight into a pipeline —
        the manager's routing does exactly that.
        """
        async with self._lock:
            card = Card.new(title, description)
            # A card may be born in the backlog, or straight into a pipeline you can ROUTE
            # to. Never into `build`: nothing reaches the build pipeline that has not been
            # planned, and that is a rule of the board, not a convention of the UI.
            if pipeline not in ROUTABLE:
                card.pipeline, card.column = BACKLOG, ""
                card.kind = valid_kind(kind) or UNSET
                card.stage = "backlog"
            else:
                first = self.pipelines.first(pipeline)
                card.pipeline, card.column = pipeline, first.id
                card.kind = valid_kind(kind) or kind_for_pipeline(pipeline, UNSET)
                card.stage = first.slug
            card.manager_id = (
                manager_id if manager_id in self.managers else next(iter(self.managers))
            )
            self._ensure_card_dir(card)
            self.cards[card.id] = card
            self.order.append(card.id)
            self._save()
        await self.broadcast_board()
        return card

    async def move_card(self, card_id: str, column_id: str) -> Card | None:
        """Move a card to a column (any column, any pipeline) and move its folder.

        CROSSING into an origin pipeline restamps the card's kind — drag a card into
        Maintenance and it becomes a maintenance card, colour and all. Crossing into BUILD
        leaves the kind alone: that is how a fix stays visibly a fix beside the features.

        A move WITHIN a pipeline never touches the kind. It is the human's field — they can
        flip it on the card itself — and re-deriving it on every move would silently undo
        that the next time the card advanced a column.
        """
        async with self._lock:
            c = self.cards.get(card_id)
            col = self.pipelines.get(column_id)
            if not c or not col:
                return None
            if col.pipeline != c.pipeline:
                c.kind = kind_for_pipeline(col.pipeline, c.kind)
            c.pipeline = col.pipeline
            c.column = col.id
            c.stage = col.slug
            c.trashed = False
            self._ensure_card_dir(c)
            c.touch()
            self._save()
        await self.broadcast_board()
        return c

    def next_column(self, card_id: str) -> Column | None:
        """The next column for this card, or None at the end of a pipeline. Crossing from
        plan into build is deliberately NOT automatic — that is the human gate."""
        c = self.cards.get(card_id)
        if not c:
            return None
        return self.pipelines.next_column(c.column)

    async def promote_to_build(self, card_id: str) -> Card | None:
        """Hand a card from the end of an ORIGIN pipeline to the start of the build pipeline.

        Both origins promote here — a planned feature and a diagnosed fix arrive in the same
        first build column. The card keeps its kind on the way across.
        """
        c = self.cards.get(card_id)
        if not c or c.pipeline not in ORIGIN_PIPELINES:
            return None
        return await self.move_card(card_id, self.pipelines.first(BUILD).id)

    async def assign_card(self, card_id: str, manager_id: str) -> Card | None:
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or manager_id not in self.managers:
                return None
            c.manager_id = manager_id
            c.touch()
            self._save()
        await self.broadcast_board()
        return c

    async def set_stage(self, card_id: str, stage: str, error: str | None = None) -> Card | None:
        async with self._lock:
            c = self.cards.get(card_id)
            if not c:
                return None
            c.stage = stage
            c.error = error
            c.touch()
            self._save()
        await self.broadcast_board()
        return c

    async def set_busy(self, card_id: str, busy: bool) -> None:
        async with self._lock:
            c = self.cards.get(card_id)
            if not c:
                return
            c.busy = busy
            c.touch()
            self._save()
        await self.broadcast_board()

    async def append_note(self, card_id: str, text: str) -> ChatMessage | None:
        """A decision, filed on the card. NOT a message to the human.

        The manager's notes used to land in the chat with the same weight as something he
        actually needed you to answer — so "document every decision" and "keep the chat
        clean" were fighting each other, and the chat lost. A note is a record: it is kept,
        it is readable, and it does not interrupt anyone.
        """
        async with self._lock:
            c = self.cards.get(card_id)
            if not c:
                return None
            msg = ChatMessage(role="note", text=text)
            c.thread.append(msg)
            c.touch()
            self._save()
        await self._emit({"type": "message", "card_id": card_id, "message": msg.to_dict()})
        return msg

    async def set_stopped_worker(self, card_id: str, name: str, session_id: str | None) -> None:
        """Remember a worker we stopped, so it can be RESUMED rather than restarted.

        Stopping a worker is only acceptable because nothing it knew is thrown away: the
        files it wrote are on disk, and this is the thread it was thinking in.
        """
        async with self._lock:
            c = self.cards.get(card_id)
            if not c:
                return
            c.worker_name, c.worker_session = (name, session_id) if session_id else ("", None)
            self._save()

    async def clear_stopped_worker(self, card_id: str) -> None:
        """It finished properly. There is nothing left to resume."""
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or not (c.worker_session or c.worker_name):
                return
            c.worker_name, c.worker_session = "", None
            self._save()

    async def set_working(self, card_id: str, who: str) -> None:
        """Who is actually working on this card right now. "" = the manager himself."""
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or c.working == who:
                return
            c.working = who
            self._save()
        await self.broadcast_board()

    async def set_session(self, card_id: str, session_id: str) -> None:
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or not session_id or c.session_id == session_id:
                return
            c.session_id = session_id
            self._save()

    async def set_artifact(self, card_id: str, name: str, path: str) -> None:
        async with self._lock:
            c = self.cards.get(card_id)
            if not c:
                return
            c.artifacts[name] = path
            c.touch()
            self._save()
        await self.broadcast_board()

    async def append_message(self, card_id: str, role: str, text: str) -> ChatMessage | None:
        async with self._lock:
            c = self.cards.get(card_id)
            if not c:
                return None
            msg = ChatMessage(role=role, text=text)
            c.thread.append(msg)
            c.touch()
            self._save()
        await self._emit({"type": "message", "card_id": card_id, "message": msg.to_dict()})
        return msg

    # ---- trash -------------------------------------------------------
    async def trash_card(self, card_id: str) -> Card | None:
        """Archive: the card leaves the board and its folder moves to cards/trash/.
        Nothing is destroyed — `restore_card` puts it back where it was."""
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or c.trashed:
                return None
            dest = os.path.join(
                self.data_dir, CARDS_DIRNAME, TRASH_DIRNAME, c.folder_name()
            )
            src = self.abs_dir(c)
            if os.path.isdir(src):
                self._move_dir(src, dest)
            else:
                os.makedirs(dest, exist_ok=True)
            c.trashed_from = {"pipeline": c.pipeline, "column": c.column}
            c.trashed = True
            c.busy = False
            c.dir = self._rel(dest)
            c.touch()
            self._save()
        await self.broadcast_board()
        await self._emit(self.trash_view())
        return c

    async def restore_card(self, card_id: str) -> Card | None:
        """Put a trashed card back in the column it came from (or the plan inbox if that
        column has since been deleted)."""
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or not c.trashed:
                return None
            origin = c.trashed_from or {}
            col = self.pipelines.get(origin.get("column", ""))
            if not col:
                # Its column is gone, or it was in the backlog when it was trashed. Either
                # way the honest place to put it back is the backlog — not a guessed column.
                c.pipeline, c.column, c.stage = BACKLOG, "", "backlog"
                c.trashed = False
                self._ensure_card_dir(c)
                c.touch()
                self._save()
                await self.broadcast_board()
                return c
            c.pipeline, c.column = col.pipeline, col.id
            c.kind = kind_for_pipeline(col.pipeline, c.kind)
            c.stage = col.slug
            c.trashed = False
            c.trashed_from = None
            self._ensure_card_dir(c)
            c.touch()
            self._save()
        await self.broadcast_board()
        await self._emit(self.trash_view())
        return c

    async def purge_card(self, card_id: str) -> bool:
        """Permanently delete a trashed card and its folder. Only works from the trash."""
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or not c.trashed:
                return False
            path = self.abs_dir(c)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            self.cards.pop(card_id, None)
            if card_id in self.order:
                self.order.remove(card_id)
            self._save()
        await self.broadcast_board()
        await self._emit(self.trash_view())
        return True

    # ---- managers ----------------------------------------------------
    async def add_manager(self, name: str, emoji: str = "🧭") -> ManagerAgent:
        async with self._lock:
            m = ManagerAgent.new((name or "").strip() or "Manager", emoji or "🧭")
            self.managers[m.id] = m
            self._save()
        await self.broadcast_board()
        return m

    async def remove_manager(self, manager_id: str) -> bool:
        """Delete a manager; its cards fall to the first remaining manager. The last
        manager cannot be deleted."""
        async with self._lock:
            if manager_id not in self.managers or len(self.managers) <= 1:
                return False
            self.managers.pop(manager_id)
            fallback = next(iter(self.managers))
            for c in self.cards.values():
                if c.manager_id == manager_id:
                    c.manager_id = fallback
            self._save()
        await self.broadcast_board()
        return True

    async def set_manager_busy(self, manager_id: str, busy: bool) -> None:
        async with self._lock:
            m = self.managers.get(manager_id)
            if not m:
                return
            m.busy = busy
            self._save()
        await self.broadcast_board()

    async def set_manager_session(self, manager_id: str, session_id: str) -> None:
        async with self._lock:
            m = self.managers.get(manager_id)
            if not m or not session_id or m.session_id == session_id:
                return
            m.session_id = session_id
            self._save()

    async def append_manager_message(
        self, manager_id: str, role: str, text: str
    ) -> ChatMessage | None:
        async with self._lock:
            m = self.managers.get(manager_id)
            if not m:
                return None
            msg = ChatMessage(role=role, text=text)
            m.thread.append(msg)
            self._save()
        await self._emit(
            {"type": "manager_message", "manager_id": manager_id, "message": msg.to_dict()}
        )
        return msg

    async def route_card(self, card_id: str, pipeline: str) -> Card | None:
        """Send a card OUT of the backlog and into the first column of a pipeline.

        Refuses a card that still has no type. "No card leaves the backlog untyped" is the
        one invariant this layer exists to hold, and enforcing it here — rather than trusting
        the manager to remember — is the difference between an invariant and a hope.
        """
        c = self.cards.get(card_id)
        if not c or pipeline not in ROUTABLE:
            return None
        if c.kind not in KINDS:
            return None
        return await self.move_card(card_id, self.pipelines.first(pipeline).id)

    async def send_to_backlog(self, card_id: str) -> Card | None:
        """Pull a card back out of a pipeline. It keeps its type and its folder."""
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or c.pipeline == BACKLOG:
                return None
            c.pipeline, c.column, c.stage = BACKLOG, "", "backlog"
            c.trashed = False
            self._ensure_card_dir(c)
            c.touch()
            self._save()
        await self.broadcast_board()
        return c

    def untyped_cards(self) -> list[str]:
        """Cards nobody has classified. The manager owes each of these a decision."""
        return [c.id for c in self.cards.values() if not c.trashed and c.kind not in KINDS]

    # ---- worker chats (one conversation per column's worker) ----------
    @staticmethod
    def worker_key(pipeline: str, slug: str) -> str:
        return f"{pipeline}/{slug}"

    def worker_chat(self, key: str) -> WorkerChat:
        """The conversation about this worker, created empty the first time it is opened."""
        w = self.worker_chats.get(key)
        if not w:
            w = WorkerChat(key=key)
            self.worker_chats[key] = w
        return w

    def worker_chat_view(self, key: str) -> dict:
        w = self.worker_chat(key)
        return {
            "type": "worker_chat",
            "key": key,
            "busy": w.busy,
            "thread": [m.to_dict() for m in w.thread],
        }

    async def set_worker_busy(self, key: str, busy: bool) -> None:
        async with self._lock:
            self.worker_chat(key).busy = busy
            self._save()
        await self._emit(self.worker_chat_view(key))

    async def set_worker_session(self, key: str, session_id: str) -> None:
        async with self._lock:
            w = self.worker_chat(key)
            if not session_id or w.session_id == session_id:
                return
            w.session_id = session_id
            self._save()

    async def append_worker_message(self, key: str, role: str, text: str) -> ChatMessage:
        async with self._lock:
            msg = ChatMessage(role=role, text=text)
            self.worker_chat(key).thread.append(msg)
            self._save()
        await self._emit({"type": "worker_message", "key": key, "message": msg.to_dict()})
        return msg

    async def set_card_kind(self, card_id: str, kind: str) -> Card | None:
        """What a card IS — a new feature, or a fix. The human's call, and theirs alone.

        It does NOT move the card. A fix can be found in any pipeline (that is the point of
        it surviving promotion into build), so changing the kind only repaints it and
        re-files what it counts as. Where it sits is a separate question, answered by
        dragging it.
        """
        k = valid_kind(kind)
        if not k:
            return None
        async with self._lock:
            c = self.cards.get(card_id)
            if not c or c.kind == k:
                return c
            c.kind = k
            c.touch()
            self._save()
        await self.broadcast_board()
        return c

    # ---- pipelines ---------------------------------------------------
    async def set_pipeline_color(self, pipeline: str, color: str) -> str | None:
        """Repaint a pipeline. The cards that BELONG to it follow, one shade darker — see
        the UI: a card is coloured by its kind, not by the column it is parked in."""
        async with self._lock:
            c = self.pipelines.set_color(pipeline, color)
        if c:
            await self.broadcast_board()
        return c

    # ---- columns (config + the disk migration each change implies) ----
    async def add_column(
        self,
        pipeline: str,
        title: str,
        index: int | None = None,
        gate: bool = False,
        entry: str = "",
        work: str = "",
        exit_: str = "",
        output: str = "",
    ) -> Column:
        """Create a column. The entry/exit contract the human typed becomes the new
        column's worker file; anything left blank is scaffolded for the manager to fill in."""
        async with self._lock:
            col = self.pipelines.add_column(pipeline, title, index, gate)
            os.makedirs(self._column_dir(col), exist_ok=True)
            self.workers.ensure(col, entry=entry, work=work, exit_=exit_, output=output)
            self._save()
        await self.broadcast_board()
        return col

    async def rename_column(self, column_id: str, title: str) -> Column | None:
        """Renaming a column renames its folder and its worker file, and moves every card
        folder underneath it."""
        async with self._lock:
            before = self.pipelines.get(column_id)
            if not before:
                return None
            old_slug, old_dir = before.slug, self._column_dir(before)
            result = self.pipelines.rename_column(column_id, title)
            if not result:
                return None
            col, _ = result
            new_dir = self._column_dir(col)
            if old_slug != col.slug:
                if os.path.isdir(old_dir):
                    self._move_dir(old_dir, new_dir)
                os.makedirs(new_dir, exist_ok=True)
                self.workers.rename(col.pipeline, old_slug, col.slug, col.title)
                # The conversation about this worker is keyed by its slug, so it must follow
                # the rename or it is orphaned — and the human's chat about the contract is
                # not something a retitle is allowed to throw away.
                old_key = self.worker_key(col.pipeline, old_slug)
                chat = self.worker_chats.pop(old_key, None)
                if chat:
                    chat.key = self.worker_key(col.pipeline, col.slug)
                    self.worker_chats[chat.key] = chat
                for c in self.cards.values():
                    if c.column == col.id and not c.trashed:
                        self._ensure_card_dir(c)
                        c.touch()
            self.workers.ensure(col)
            self._save()
        await self.broadcast_board()
        return col

    async def set_column_gate(self, column_id: str, gate: bool) -> Column | None:
        async with self._lock:
            col = self.pipelines.set_gate(column_id, gate)
        if col:
            await self.broadcast_board()
        return col

    async def reorder_column(self, column_id: str, index: int) -> Column | None:
        async with self._lock:
            col = self.pipelines.reorder_column(column_id, index)
        if col:
            await self.broadcast_board()
        return col

    async def delete_column(self, column_id: str) -> tuple[Column, int]:
        """Delete a column. Any cards still in it fall back to the previous column (or the
        next one, if it was the first). The worker file is archived, not destroyed."""
        async with self._lock:
            col = self.pipelines.get(column_id)
            if not col:
                raise ValueError("no such column")
            cols = self.pipelines.columns[col.pipeline]
            if len(cols) <= 1:
                raise ValueError("a pipeline must keep at least one column")
            i = self.pipelines.index_of(column_id)
            fallback = cols[i - 1] if i > 0 else cols[i + 1]

            stranded = [
                c for c in self.cards.values() if c.column == column_id and not c.trashed
            ]
            for c in stranded:
                c.pipeline, c.column = fallback.pipeline, fallback.id
                c.stage = fallback.slug
                self._ensure_card_dir(c)
                c.touch()

            old_dir = self._column_dir(col)
            self.pipelines.remove_column(column_id)
            self.workers.delete(col.pipeline, col.slug)
            if os.path.isdir(old_dir) and not os.listdir(old_dir):
                try:
                    os.rmdir(old_dir)
                except OSError:
                    pass
            self._save()
        await self.broadcast_board()
        return col, len(stranded)
