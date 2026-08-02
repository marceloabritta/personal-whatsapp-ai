"""Add the Expedited pipeline, and open the backlog above the board.

Two things, and they exist for each other: the backlog is where a card waits to be *routed*,
and routing is only an interesting decision once there is more than one way down.

  * **Expedited** — the fast lane, for work that is small, contained and low-risk. Its exact
    columns follow `DEFAULT_COLUMNS[EXPED]` (today: scope → plan GATE → build → ready to ship
    GATE → shipped). Seeded as ordinary editable columns, exactly like the others; if the
    human already has a `exped` key, this touches nothing.

  * **The backlog** — cards with `pipeline: "backlog"` and no column. Nothing to migrate:
    every existing card is already in a pipeline, and it stays exactly where it is. The
    backlog starts empty and fills as new cards are created.

Cards keep their type. Anything that somehow has no type is left `unset` rather than guessed
at — the manager is asked to classify it, and a guess written into the file would be
indistinguishable from a decision someone actually made.
"""
from __future__ import annotations

import os

from ..models import Column, slugify
from ..pipelines import valid_color
from ._helpers import read_json, write_json

NUMBER = 6
DESCRIPTION = "add the Expedited pipeline (the fast lane) and the backlog above the board"

# INLINED, deliberately. A migration is a historical record — it must keep running exactly as
# it did the day it shipped, even after the live model moves on. Expedited was removed from the
# model in m0008; this migration still seeds it (m0008, which runs after, then takes it back
# out), so the constants it needs live here, frozen, rather than being imported from a `models`
# that no longer has them. (title, slug, gate)
_EXPED = "exped"
_EXPED_COLUMNS = [
    ("Scope", "scope", False),
    ("Plan", "plan", True),
    ("Build", "build", False),
    ("Ready to Ship", "ready-to-ship", True),
    ("Shipped", "shipped", False),
]
_EXPED_COLOR = "#22304d"  # blue — the fast lane
# The board order as it was when this migration shipped (before m0008 reshaped it).
_ORDER = ("plan", "maint", _EXPED, "build")


def migrate(ws) -> list[str]:
    notes: list[str] = []
    path = os.path.join(ws.path, "pipelines.json")
    data = read_json(path)
    if data is None:
        return []  # no config yet — PipelineConfig seeds all four itself

    dirty = False

    if not data.get(_EXPED):
        data[_EXPED] = [
            Column.new(_EXPED, title, slug=slug or slugify(title), gate=gate).to_dict()
            for title, slug, gate in _EXPED_COLUMNS
        ]
        gates = [t for t, _s, g in _EXPED_COLUMNS if g]
        notes.append(
            "seeded the Expedited pipeline: "
            + " → ".join(t for t, _s, _g in _EXPED_COLUMNS)
            + f" (gated at {' and '.join(gates)} — nothing is built or shipped without you)"
        )
        dirty = True

    colors = data.get("colors") or {}
    if not valid_color(colors.get(_EXPED, "")):
        colors[_EXPED] = _EXPED_COLOR
        data["colors"] = colors
        notes.append("painted it blue — the fast lane")
        dirty = True

    if dirty:
        # Keep the file in board order, so it reads the way the board looks.
        ordered = {p: data.get(p, []) for p in _ORDER}
        ordered["colors"] = data.get("colors", {})
        write_json(path, ordered)
        notes.append(
            "new cards are now created in the BACKLOG, above the pipelines — the manager "
            "gives each one a type, and routes it when you say to start"
        )

    return notes
