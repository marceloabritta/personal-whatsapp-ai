"""The first migration, and the one everything else depends on: give the working folder a
version, a layout, and a merge baseline for its prompts.

Before this, a working folder had no version number at all — which means no update could
ever know what to do with it. Folders created by this version of the system are born at the
latest schema and never run this; folders that predate it (a `data/` dir that used to live
inside the system folder) run it exactly once.
"""
from __future__ import annotations

import os

from ..models import PIPELINES
from ..workers import parse_markdown, read_default, retitle
from ._helpers import ensure_dirs, set_board_key

NUMBER = 1
DESCRIPTION = "give the working folder a schema version, its layout, and a prompt baseline"


def migrate(ws) -> list[str]:
    notes: list[str] = []

    made = ensure_dirs(
        os.path.join(ws.path, "cards"),
        os.path.join(ws.path, "cards", "trash"),
        ws.workers_dir,
        ws.baseline_dir,
    )
    if made:
        notes.append(f"created {len(made)} folder(s) the new layout expects")

    if set_board_key(ws, "schema_version", NUMBER):
        notes.append("stamped a schema version into board.json")

    baselines = _backfill_baselines(ws)
    if baselines:
        notes.append(
            f"recorded a merge baseline for {baselines} untouched worker prompt(s) — "
            f"upstream improvements to those will now reach you automatically"
        )
    return notes


def _backfill_baselines(ws) -> int:
    """A folder that predates the three-way merge has no `.defaults/` snapshot, so we cannot
    tell "you never touched this" from "you rewrote it".

    We only claim a baseline where we can PROVE the file is untouched: it is byte-identical
    to the default this system ships. Anything else is left without a baseline, which the
    updater reads as "assume the human edited it" — it keeps their file and reports the
    upstream diff. We never guess in the direction that would overwrite someone's work.
    """
    count = 0
    for pipeline in PIPELINES:
        pdir = os.path.join(ws.workers_dir, pipeline)
        if not os.path.isdir(pdir):
            continue
        for name in sorted(os.listdir(pdir)):
            if not name.endswith(".md"):
                continue
            slug = name[:-3]
            if os.path.exists(os.path.join(ws.baseline_dir, pipeline, name)):
                continue
            default = read_default(pipeline, slug)
            if not default:
                continue  # a column you invented: there is no default to merge from
            with open(os.path.join(pdir, name), "r", encoding="utf-8") as fh:
                yours = fh.read()
            if _same(yours, _retitled_like(default, yours)):
                os.makedirs(os.path.join(ws.baseline_dir, pipeline), exist_ok=True)
                with open(os.path.join(ws.baseline_dir, pipeline, name), "w", encoding="utf-8") as fh:
                    fh.write(yours)
                count += 1
    return count


def _retitled_like(default: str, yours: str) -> str:
    """A renamed column is not an edit to the prompt. Compare with the title neutralized."""
    meta, _ = parse_markdown(yours)
    title = meta.get("title")
    return retitle(default, title) if title else default


def _same(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())
