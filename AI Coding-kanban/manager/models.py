"""Data model for the manager-kanban board.

A Card is a unit of product work that flows through six columns. Within a
column a card has a finer-grained `stage` that reflects where the manager
(or a worker) currently is in the pipeline.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Column(str, Enum):
    IDEAS = "ideas"
    PLANNING = "planning"
    PLANS_READY = "plans_ready"
    BUILDING = "building"
    BUILD_REVIEW = "build_review"
    SHIPPED = "shipped"


# Human-facing column titles, in board order.
COLUMN_ORDER: list[Column] = [
    Column.IDEAS,
    Column.PLANNING,
    Column.PLANS_READY,
    Column.BUILDING,
    Column.BUILD_REVIEW,
    Column.SHIPPED,
]

COLUMN_TITLES: dict[str, str] = {
    Column.IDEAS: "Ideas",
    Column.PLANNING: "Planning",
    Column.PLANS_READY: "Plans Ready",
    Column.BUILDING: "Building",
    Column.BUILD_REVIEW: "Build ready for review",
    Column.SHIPPED: "Shipped",
}

# The two points where the manager MUST stop and wait for the human.
# Keyed by the column the card is sitting in when the gate is active.
GATES: dict[str, str] = {
    Column.PLANS_READY: "plan_approval",   # approve the plan before building
    Column.BUILD_REVIEW: "ship_approval",  # approve the build before shipping
}


@dataclass
class ChatMessage:
    role: str          # "user" | "manager" | "system" | "tool"
    text: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Card:
    id: str
    title: str
    description: str = ""
    column: str = Column.IDEAS.value
    stage: str = "idea"
    session_id: str | None = None          # SDK conversation for this card
    gate: str | None = None                # pending human gate, if any
    error: str | None = None
    busy: bool = False                     # manager currently working this card
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> repo-relative path
    thread: list[ChatMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def new(title: str, description: str = "") -> "Card":
        return Card(id=uuid.uuid4().hex[:8], title=title, description=description)

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["thread"] = [m.to_dict() for m in self.thread]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Card":
        thread = [ChatMessage(**m) for m in d.get("thread", [])]
        d = {**d, "thread": thread}
        # tolerate older/newer schemas by keeping only known fields
        known = {f for f in Card.__dataclass_fields__}  # type: ignore[attr-defined]
        d = {k: v for k, v in d.items() if k in known}
        return Card(**d)
