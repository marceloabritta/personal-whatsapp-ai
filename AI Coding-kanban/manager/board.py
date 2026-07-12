"""Board state store: in-memory cards + JSON persistence + change broadcast.

All mutations go through the Board so that (a) state is persisted to disk and
(b) every change is pushed to connected UIs. The Board is deliberately
transport-agnostic: it takes an async `broadcaster` callback and does not know
about FastAPI or WebSockets.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Awaitable, Callable

from .models import Card, ChatMessage, Column, COLUMN_ORDER, GATES


Broadcaster = Callable[[dict], Awaitable[None]]


class Board:
    def __init__(self, data_dir: str, broadcaster: Broadcaster | None = None):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "board.json")
        self.cards: dict[str, Card] = {}
        self.order: list[str] = []
        self._broadcaster = broadcaster
        self._lock = asyncio.Lock()
        os.makedirs(data_dir, exist_ok=True)
        self._load()

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
        # include any cards missing from order (defensive)
        for cid in self.cards:
            if cid not in self.order:
                self.order.append(cid)

    def _save(self) -> None:
        payload = {
            "cards": [self.cards[cid].to_dict() for cid in self.order if cid in self.cards],
            "order": self.order,
        }
        # atomic write
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

    # ---- snapshots ---------------------------------------------------
    def snapshot(self) -> dict:
        """Compact board view (cards without full thread) for the board render."""
        cards = []
        for cid in self.order:
            c = self.cards[cid]
            cards.append({
                "id": c.id, "title": c.title, "description": c.description,
                "column": c.column, "stage": c.stage, "gate": c.gate,
                "busy": c.busy, "error": c.error, "artifacts": c.artifacts,
                "updated_at": c.updated_at, "unread": False,
            })
        return {"type": "board", "columns": [c.value for c in COLUMN_ORDER], "cards": cards}

    def card_view(self, card_id: str) -> dict | None:
        c = self.cards.get(card_id)
        if not c:
            return None
        d = c.to_dict()
        d["type"] = "card"
        return d

    async def _emit(self, message: dict) -> None:
        if self._broadcaster:
            await self._broadcaster(message)

    async def broadcast_board(self) -> None:
        await self._emit(self.snapshot())

    # ---- mutations ---------------------------------------------------
    async def add_card(self, title: str, description: str = "") -> Card:
        async with self._lock:
            card = Card.new(title, description)
            self.cards[card.id] = card
            self.order.append(card.id)
            self._save()
        await self.broadcast_board()
        return card

    async def move_card(self, card_id: str, column: str) -> Card | None:
        column = Column(column).value  # validates
        async with self._lock:
            c = self.cards.get(card_id)
            if not c:
                return None
            c.column = column
            c.gate = GATES.get(column)  # set/clear the human gate for the new column
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
