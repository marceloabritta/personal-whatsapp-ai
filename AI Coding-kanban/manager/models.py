"""Data model for the manager-kanban board.

Two fixed pipelines — `plan` and `build`. The COLUMNS inside each pipeline are
not fixed: the human defines them from the UI. Everything else derives from that
column list:

  * each column owns a folder    -> data/cards/<pipeline>/<column-slug>/
  * each column owns a worker    -> workers/<pipeline>/<column-slug>.md
  * a column may be a GATE, meaning the manager must stop and wait for a human
    after that column's worker has run.

A Card lives in exactly one column at a time, and its folder physically moves
with it.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Pipelines are fixed; their columns are not.
#
# Two of them are ORIGINS — a card is born in one of them and they answer a different
# question: `plan` is "should we build this thing that does not exist?", `maint` is "why is
# the thing we built behaving wrongly?". They are not the same work and they do not want the
# same columns, which is why maintenance is its own pipeline and not a column of plan.
#
# `build` is the shared destination. Both origins feed it, and a card that reaches it keeps
# its `kind` — see Card.kind, which is the whole reason that field is stored rather than
# derived from the pipeline it is sitting in.
# ---------------------------------------------------------------------------
PLAN = "plan"
MAINT = "maint"
EXPED = "exped"
BUILD = "build"

# The BACKLOG is not a pipeline, and calling it one would be a lie that costs us later: it
# has no columns, no contracts and no workers. It is where a card LIVES BEFORE it has been
# routed — everything is created there, and the only questions it answers are "what type is
# this?" and "which pipeline should it go down?". Both are the manager's job, not a worker's.
BACKLOG = "backlog"

# Order matters: this is the order the board renders in, top to bottom.
PIPELINES: tuple[str, ...] = (PLAN, MAINT, EXPED, BUILD)
PIPELINE_TITLES: dict[str, str] = {
    BACKLOG: "Backlog",
    PLAN: "New Feature Plan",
    MAINT: "Maintenance",
    EXPED: "Expedited",
    BUILD: "Build",
}

# Where the manager may ROUTE a card out of the backlog. Build is not on this list: nothing
# reaches the build pipeline that has not been planned first.
ROUTABLE: tuple[str, ...] = (PLAN, MAINT, EXPED)

# The colour each pipeline is painted, and — one shade darker — the cards that BELONG to it.
# These are DEFAULTS: the colour is state, editable per pipeline in the UI, so an upgrade
# never repaints a board the human has recoloured.
DEFAULT_PIPELINE_COLORS: dict[str, str] = {
    PLAN: "#1d3b2a",  # green — new work
    MAINT: "#3d3620",  # yellow — repair
    EXPED: "#22304d",  # blue — the fast lane
    BUILD: "#3b2222",  # red — shipping, the expensive end
}

# The pipelines a card can START in. Each one stamps its kind on the cards it holds.
ORIGIN_PIPELINES: tuple[str, ...] = (PLAN, MAINT)

# ---------------------------------------------------------------------------
# What a card IS — as opposed to where it currently sits.
# ---------------------------------------------------------------------------
FEATURE = "feature"
MAINTENANCE = "maintenance"

# A card whose type nobody has decided YET. It exists so that "the manager assigns a type
# right after creation" can be true: without it, an unclassified card would already be
# silently a feature, and the classification would be a no-op nobody could audit.
# It is a WAITING ROOM, not a resting place — a card must not leave the backlog holding it.
UNSET = "unset"

KINDS: tuple[str, ...] = (FEATURE, MAINTENANCE)  # the types a card may actually BE
KIND_TITLES: dict[str, str] = {
    FEATURE: "New feature",
    MAINTENANCE: "Maintenance",
    UNSET: "Needs a type",
}
KIND_BY_PIPELINE: dict[str, str] = {PLAN: FEATURE, MAINT: MAINTENANCE}

# The pipeline a card of each kind BELONGS to — what it is coloured from, wherever it sits.
PIPELINE_BY_KIND: dict[str, str] = {FEATURE: PLAN, MAINTENANCE: MAINT}


def valid_kind(kind: str) -> str | None:
    """A settable type. `unset` is deliberately NOT settable: you may only ever move a card
    OUT of it. Nothing gets to un-decide a card that has already been classified."""
    k = (kind or "").strip().lower()
    return k if k in KINDS else None


def kind_for_pipeline(pipeline: str, current: str = FEATURE) -> str:
    """The kind a card takes on when it CROSSES INTO `pipeline`.

    An origin pipeline stamps its kind on a card entering it from elsewhere. BUILD
    deliberately does not: a card arriving there is already a feature or a fix, and that is
    precisely what the human wants to still be able to see once both are queued together.

    Note the word *crosses*. This is applied on a pipeline change, never on an ordinary move
    between columns — because the kind is the human's to set (they can flip it on the card),
    and re-deriving it on every move would quietly undo them on the card's next advance.
    """
    return KIND_BY_PIPELINE.get(pipeline, current)


def slugify(text: str, fallback: str = "column") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or fallback


def new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Column:
    """One column of one pipeline. `slug` is the on-disk name (folder + worker file)."""

    id: str
    pipeline: str
    slug: str
    title: str
    gate: bool = False  # after this column's worker runs, stop and wait for the human

    @staticmethod
    def new(pipeline: str, title: str, slug: str | None = None, gate: bool = False) -> "Column":
        return Column(
            id=new_id(),
            pipeline=pipeline,
            slug=slug or slugify(title),
            title=title,
            gate=gate,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Column":
        known = {f for f in Column.__dataclass_fields__}  # type: ignore[attr-defined]
        return Column(**{k: v for k, v in d.items() if k in known})


@dataclass
class ChatMessage:
    role: str  # "user" | "manager" | "worker" | "system"
    text: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ManagerAgent:
    """A manager. Owns cards, and also has a board-level chat of its own.

    Several managers can exist at once; each card is assigned to exactly one.
    The board-level thread (`thread`) is where you talk to a manager about the
    board as a whole — the pipelines, the workers, which cards to create —
    rather than about one card. It is backed by its own SDK session, separate
    from every card session.
    """

    id: str
    name: str
    emoji: str = "🧭"
    session_id: str | None = None  # SDK conversation for the board-level chat
    busy: bool = False
    thread: list[ChatMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def new(name: str, emoji: str = "🧭") -> "ManagerAgent":
        return ManagerAgent(id=new_id(), name=name, emoji=emoji)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["thread"] = [m.to_dict() for m in self.thread]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ManagerAgent":
        thread = [ChatMessage(**m) for m in d.get("thread", [])]
        d = {**d, "thread": thread}
        known = {f for f in ManagerAgent.__dataclass_fields__}  # type: ignore[attr-defined]
        return ManagerAgent(**{k: v for k, v in d.items() if k in known})


@dataclass
class WorkerChat:
    """A conversation with the manager ABOUT ONE COLUMN'S WORKER.

    Its own subject, so its own session — not a corner of the board chat. Asking "make this
    reviewer stop inventing work" is a different conversation from "what's in flight?", and
    pouring both into one thread would bury each in the other.

    Keyed `<pipeline>/<slug>`, which is exactly where the worker's file lives. The key
    follows the column when it is renamed (see Board.rename_column) — the conversation is
    about the *contract*, and the contract survives its own title changing.
    """

    key: str  # "<pipeline>/<slug>"
    session_id: str | None = None
    busy: bool = False
    thread: list[ChatMessage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["thread"] = [m.to_dict() for m in self.thread]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "WorkerChat":
        thread = [ChatMessage(**m) for m in d.get("thread", [])]
        d = {**d, "thread": thread}
        known = {f for f in WorkerChat.__dataclass_fields__}  # type: ignore[attr-defined]
        return WorkerChat(**{k: v for k, v in d.items() if k in known})


@dataclass
class Card:
    id: str
    title: str
    description: str = ""
    pipeline: str = BACKLOG  # every card is born in the backlog, unrouted
    kind: str = UNSET  # "feature" | "maintenance" | "unset" — what it IS, not where it sits
    column: str = ""  # Column.id — EMPTY while the card is in the backlog
    manager_id: str = ""  # ManagerAgent.id that owns this card
    stage: str = "new"  # free-text fine-grained status
    session_id: str | None = None  # SDK conversation for this card
    error: str | None = None
    busy: bool = False  # a worker/manager is currently running for this card
    # WHO is working, while busy. "" = the manager himself; otherwise the worker's agent
    # name. The board used to say only "working", which told you nothing about whether the
    # manager was thinking for two seconds or a coder had been running for twenty minutes.
    working: str = ""
    # A worker that was STOPPED mid-task, and the SDK session it was stopped in. This is how
    # it comes back with everything it knew — what it had read, decided, half-written —
    # instead of a fresh worker starting the column from a blank sheet. Cleared the moment a
    # worker finishes properly.
    worker_session: str | None = None
    worker_name: str = ""
    dir: str = ""  # card folder, relative to the data dir
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> path rel. to data dir
    thread: list[ChatMessage] = field(default_factory=list)
    trashed: bool = False
    trashed_from: dict[str, str] | None = None  # {"pipeline":…, "column":…} for restore
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def new(title: str, description: str = "") -> "Card":
        return Card(id=new_id(), title=title, description=description)

    def touch(self) -> None:
        self.updated_at = time.time()

    def folder_name(self) -> str:
        """Stable folder name for this card: <id>-<title-slug>."""
        return f"{self.id}-{slugify(self.title, fallback='card')}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["thread"] = [m.to_dict() for m in self.thread]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Card":
        thread = [ChatMessage(**m) for m in d.get("thread", [])]
        d = {**d, "thread": thread}
        known = {f for f in Card.__dataclass_fields__}  # type: ignore[attr-defined]
        return Card(**{k: v for k, v in d.items() if k in known})
