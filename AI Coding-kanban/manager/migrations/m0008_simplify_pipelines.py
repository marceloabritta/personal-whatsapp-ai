"""Simplify the board to three pipelines and drop every review cycle.

The board used to carry four pipelines and long review chains (Scoping → Scope Review →
Planning → Plan Review → …). This collapses it to the shape the human actually wants:

    Plan          Idea  → Plan*            (new stuff)
    Maintenance   Report → Plan*           (the fix)
    Build         Build → Ship             (the shared destination)

    (* = the one gate — the human approves the plan before any code is written)

The Expedited pipeline is removed entirely; its cards are re-homed by where they were. Every
other card is moved from a now-deleted column to its nearest survivor, and its folder moves on
disk with it. New columns get their (new, much simpler) worker files scaffolded from the
system defaults the first time the board loads them — nothing to do here for prompts.

Idempotent: if the board is already at the new shape, this is a no-op. Forward-only; the
working folder is backed up before any migration runs, so a bad run is recovered by restore.
"""
from __future__ import annotations

import os
import shutil

from ..models import DEFAULT_PIPELINE_COLORS, MAINTENANCE, PIPELINES, Column, slugify
from ..pipelines import DEFAULT_COLUMNS, valid_color
from ._helpers import read_json, write_json

NUMBER = 8
DESCRIPTION = "simplify to three pipelines (Plan, Maintenance, Build), drop every review cycle"

# New pipeline order after this migration.
NEW_PIPELINES = ("plan", "maint", "build")


def _slugs(cols) -> list[str]:
    return [c.get("slug") for c in (cols or [])]


def _already_new(data: dict) -> bool:
    """True once the board is at the new shape — the guard that makes this idempotent."""
    return (
        "exped" not in data
        and _slugs(data.get("plan")) == ["idea", "plan"]
        and _slugs(data.get("maint")) == ["report", "plan"]
        and _slugs(data.get("build")) == ["build", "ship"]
    )


def _remap(old_pipeline: str, old_slug: str, kind: str) -> tuple[str, str]:
    """Where a card sitting in (old_pipeline, old_slug) should land in the new board."""
    if old_pipeline == "plan":
        return ("plan", "idea") if old_slug == "ideas" else ("plan", "plan")
    if old_pipeline == "maint":
        return ("maint", "report") if old_slug == "report" else ("maint", "plan")
    if old_pipeline == "exped":
        # The fast lane is gone. Re-home by how far the card had got, and by what it IS.
        if old_slug == "scope":
            return ("maint", "report") if kind == MAINTENANCE else ("plan", "idea")
        if old_slug == "plan":
            return ("maint", "plan") if kind == MAINTENANCE else ("plan", "plan")
        if old_slug == "build":
            return ("build", "build")
        return ("build", "ship")  # ready-to-ship, shipped
    if old_pipeline == "build":
        if old_slug in ("preflight", "tests", "coding", "build-review"):
            return ("build", "build")
        return ("build", "ship")  # ready-to-ship, shipped
    # Anything unrecognised is parked in Build rather than dropped.
    return ("build", "build")


def _merge_move(src: str, dst: str) -> None:
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


def migrate(ws) -> list[str]:
    pipes_path = os.path.join(ws.path, "pipelines.json")
    data = read_json(pipes_path)
    if data is None:
        return []  # no config yet — PipelineConfig seeds the new shape itself

    if _already_new(data):
        return []

    notes: list[str] = []

    # ---- remember the OLD column layout, so we can remap by column id ----
    old_by_id: dict[str, tuple[str, str]] = {}
    for p in ("plan", "maint", "exped", "build"):
        for c in data.get(p, []):
            if c.get("id"):
                old_by_id[c["id"]] = (p, c.get("slug", ""))

    # ---- build the NEW pipelines.json ----
    new_data: dict = {}
    new_id: dict[tuple[str, str], str] = {}
    for p in NEW_PIPELINES:
        cols = []
        for title, slug, gate in DEFAULT_COLUMNS[p]:
            col = Column.new(p, title, slug=slug or slugify(title), gate=gate)
            cols.append(col.to_dict())
            new_id[(p, slug)] = col.id
        new_data[p] = cols
    old_colors = data.get("colors") or {}
    new_data["colors"] = {
        p: (valid_color(old_colors.get(p, "")) or DEFAULT_PIPELINE_COLORS[p]) for p in NEW_PIPELINES
    }
    write_json(pipes_path, new_data)
    notes.append(
        "reshaped the board: Plan (Idea → Plan), Maintenance (Report → Plan), Build (Build → "
        "Ship). The Expedited pipeline and every review column are gone; the only gate is Plan."
    )

    # ---- relocate every card off a now-deleted column ----
    board = read_json(ws.board_path)
    if board is None:
        return notes

    moved = 0
    for card in board.get("cards", []):
        pipeline = card.get("pipeline")
        col_id = card.get("column")
        if not col_id or pipeline == "backlog":
            continue  # backlog cards have no column; they stay put

        old = old_by_id.get(col_id) or (pipeline, card.get("stage", ""))
        new_pipeline, new_slug = _remap(old[0], old[1], card.get("kind", ""))
        target_id = new_id.get((new_pipeline, new_slug))
        if not target_id:
            continue

        card["pipeline"] = new_pipeline
        card["column"] = target_id
        card["stage"] = new_slug

        # A trashed card lives under cards/trash/; remap its fields for a clean restore but
        # leave its folder where it is.
        if card.get("trashed"):
            tf = card.get("trashed_from")
            if isinstance(tf, dict):
                tf["pipeline"] = new_pipeline
                tf["column"] = target_id
            continue

        old_dir = card.get("dir") or ""
        folder = os.path.basename(old_dir) if old_dir else f"{card.get('id','card')}-{slugify(card.get('title',''), 'card')}"
        new_dir = os.path.join("cards", new_pipeline, new_slug, folder)
        src = os.path.join(ws.path, old_dir) if old_dir else ""
        dst = os.path.join(ws.path, new_dir)
        if src and os.path.isdir(src) and os.path.abspath(src) != os.path.abspath(dst):
            _merge_move(src, dst)
        card["dir"] = new_dir
        moved += 1

    write_json(ws.board_path, board)
    if moved:
        notes.append(f"moved {moved} card(s) off deleted columns into the new ones, folders and all")
    return notes
