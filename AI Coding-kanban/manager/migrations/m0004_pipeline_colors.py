"""Give each pipeline a colour, and keep it in the working folder where the human owns it.

The colour is not decoration: it is how a card's ORIGIN stays visible after the card has
left the pipeline it came from. A feature is green and a fix is yellow all the way through
build, which is red — so the build columns show, at a glance, what is new work and what is a
repair. See the UI: a card is painted from the pipeline it BELONGS to, never from the one it
is currently parked in.

Colours are state. They land in `pipelines.json` next to the columns, they are editable per
pipeline, and an update never repaints a board the human has recoloured — this migration
only fills in what is missing.
"""
from __future__ import annotations

import os

from ..models import DEFAULT_PIPELINE_COLORS, PIPELINES
from ..pipelines import valid_color
from ._helpers import read_json, write_json

NUMBER = 4
DESCRIPTION = "give each pipeline an editable colour (feature green, maintenance yellow, build red)"


def migrate(ws) -> list[str]:
    path = os.path.join(ws.path, "pipelines.json")
    data = read_json(path)
    if data is None:
        return []  # no config yet — PipelineConfig seeds the colours itself

    stored = data.get("colors") or {}
    added = {
        p: DEFAULT_PIPELINE_COLORS[p]
        for p in PIPELINES
        if not valid_color(stored.get(p, ""))  # keep anything the human already set
    }
    if not added:
        return []

    data["colors"] = {**stored, **added}
    write_json(path, data)
    return [
        "painted the pipelines: new-feature green, maintenance yellow, build red — "
        "editable per pipeline from the board (cards take a darker shade of the pipeline "
        "they belong to)"
    ]
