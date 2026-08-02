"""Add the Expedited pipeline, and open the backlog above the board.

Two things, and they exist for each other: the backlog is where a card waits to be *routed*,
and routing is only an interesting decision once there is more than one way down.

  * **Expedited** — scope → plan (GATE) → build (GATE) → shipped. The fast lane, for work
    that is small, contained and low-risk. Seeded as ordinary editable columns, exactly like
    the others; if the human already has a `exped` key, this touches nothing.

  * **The backlog** — cards with `pipeline: "backlog"` and no column. Nothing to migrate:
    every existing card is already in a pipeline, and it stays exactly where it is. The
    backlog starts empty and fills as new cards are created.

Cards keep their type. Anything that somehow has no type is left `unset` rather than guessed
at — the manager is asked to classify it, and a guess written into the file would be
indistinguishable from a decision someone actually made.
"""
from __future__ import annotations

import os

from ..models import DEFAULT_PIPELINE_COLORS, EXPED, PIPELINES, Column, slugify
from ..pipelines import DEFAULT_COLUMNS, valid_color
from ._helpers import read_json, write_json

NUMBER = 6
DESCRIPTION = "add the Expedited pipeline (the fast lane) and the backlog above the board"


def migrate(ws) -> list[str]:
    notes: list[str] = []
    path = os.path.join(ws.path, "pipelines.json")
    data = read_json(path)
    if data is None:
        return []  # no config yet — PipelineConfig seeds all four itself

    dirty = False

    if not data.get(EXPED):
        data[EXPED] = [
            Column.new(EXPED, title, slug=slug or slugify(title), gate=gate).to_dict()
            for title, slug, gate in DEFAULT_COLUMNS[EXPED]
        ]
        gates = [t for t, _s, g in DEFAULT_COLUMNS[EXPED] if g]
        notes.append(
            "seeded the Expedited pipeline: "
            + " → ".join(t for t, _s, _g in DEFAULT_COLUMNS[EXPED])
            + f" (gated at {' and '.join(gates)} — nothing is built or shipped without you)"
        )
        dirty = True

    colors = data.get("colors") or {}
    if not valid_color(colors.get(EXPED, "")):
        colors[EXPED] = DEFAULT_PIPELINE_COLORS[EXPED]
        data["colors"] = colors
        notes.append("painted it blue — the fast lane")
        dirty = True

    if dirty:
        # Keep the file in board order, so it reads the way the board looks.
        ordered = {p: data.get(p, []) for p in PIPELINES}
        ordered["colors"] = data.get("colors", {})
        write_json(path, ordered)
        notes.append(
            "new cards are now created in the BACKLOG, above the pipelines — the manager "
            "gives each one a type, and routes it when you say to start"
        )

    return notes
