"""Add the maintenance pipeline, and give every existing card a `kind`.

A working folder created before this migration has a `pipelines.json` with two keys and
cards with no `kind` field. Both need bringing forward, and neither may lose anything:

  * **The columns are seeded, not imposed.** They land as the same editable state the plan
    and build columns already are — rename them, gate them, reorder them, delete them. If a
    `maint` key is already present (the human got there first), this touches nothing.

  * **Every existing card becomes a `feature`.** That is the honest default: every card on
    the board today was filed before maintenance existed, so it came in through the plan
    pipeline. A card that somehow already sits in `maint` is stamped `maintenance` instead.

The worker prompts and the on-disk column folders are NOT created here — `Board._bootstrap`
already materialises a worker file and a folder for every column it finds, from
`workers.default/`, on the next start. Doing it twice would just be a second, worse
implementation of the same thing.
"""
from __future__ import annotations

from ..models import FEATURE, MAINT, MAINTENANCE, PIPELINES, Column, slugify
from ..pipelines import DEFAULT_COLUMNS
from ._helpers import read_json, write_json

NUMBER = 3
DESCRIPTION = "add the maintenance pipeline, and mark every existing card as a feature"


def migrate(ws) -> list[str]:
    notes: list[str] = []

    if _seed_maint_columns(ws):
        titles = " → ".join(t for t, _s, _g in DEFAULT_COLUMNS[MAINT])
        notes.append(f"seeded the maintenance pipeline: {titles} (yours now — edit freely)")

    stamped = _backfill_card_kinds(ws)
    if stamped:
        notes.append(
            f"marked {stamped} existing card(s) as '{FEATURE}' — they predate maintenance, "
            f"so that is what they are"
        )

    return notes


def _seed_maint_columns(ws) -> bool:
    """Write the default maintenance columns into pipelines.json. Idempotent."""
    import os

    path = os.path.join(ws.path, "pipelines.json")
    data = read_json(path)
    if data is None:
        return False  # no config yet: PipelineConfig will seed all three from scratch
    if data.get(MAINT):
        return False  # already there — never second-guess the human's columns

    data[MAINT] = [
        Column.new(MAINT, title, slug=slug or slugify(title), gate=gate).to_dict()
        for title, slug, gate in DEFAULT_COLUMNS[MAINT]
    ]
    # Keep the file in board order, so it reads the way the board looks.
    write_json(path, {p: data.get(p, []) for p in PIPELINES})
    return True


def _backfill_card_kinds(ws) -> int:
    """Give every card a kind. Cards already carrying one are left exactly as they are."""
    data = read_json(ws.board_path)
    if data is None:
        return 0

    changed = 0
    for card in data.get("cards", []):
        if card.get("kind"):
            continue
        card["kind"] = MAINTENANCE if card.get("pipeline") == MAINT else FEATURE
        changed += 1

    if changed:
        write_json(ws.board_path, data)
    return changed
