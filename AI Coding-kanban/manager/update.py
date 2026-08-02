"""Bringing a working folder along with a system upgrade.

The requirement, precisely: upgrade the system upstream — teach it to run on a VM, say —
come back to any working folder, pull, and **that folder gains the new capability**, while
its cards, its column structure and its agent prompts all survive untouched. The update
itself carries the instructions for what the working folder must do to come along.

So an update is three things, in this order:

    1. BACK UP     the whole working folder, before anything is touched.
    2. MIGRATE     run every migration between the folder's schema version and the system's,
                   in order, and stamp each one as it lands. Fail loudly, never half-apply.
    3. MERGE       three-way merge the default worker prompts (see manager/prompts.py):
                   prompts you never edited get upstream's improvements; prompts you did
                   edit are kept, and the diff is reported so you can merge deliberately.

`./update.sh` does the `git pull` and the `pip install` around this. This module is
everything that touches YOUR folder, and it is callable on its own:

    python -m manager migrate      # steps 1-3 against the folder, no git, no network
    python -m manager status       # what version am I on, what's pending — offline
"""
from __future__ import annotations

import os

from . import migrations, prompts
from .board import Board
from .version import SYSTEM_DIR, system_version
from .workers import WorkerStore
from .workspace import Workspace, find_legacy_state

REPORT_NAME = "PROMPT_CHANGES.md"


def status(ws: Workspace) -> dict:
    """What version am I on, what version is this folder at, what would an update do?
    Answerable offline — that is a requirement, not a nicety.

    It also has to answer the question the upgrade actually turns on: **is there an old board
    sitting here that nobody has adopted yet?** This is the first command an upgrade runs, so
    it must never report "up to date" while someone's cards are stranded next to it.
    """
    legacy = find_legacy_state(SYSTEM_DIR)
    return {
        "system_version": system_version(),
        "workspace": ws.path,
        "workspace_exists": ws.exists,
        "schema_version": ws.schema_version(),
        "schema_latest": migrations.LATEST,
        "pending": [m.label for m in migrations.pending(ws)] if ws.exists else [],
        "unadopted_board": legacy[0] if legacy else None,
    }


def migrate(ws: Workspace, apply_prompts: bool = True) -> list[str]:
    """Steps 1-3. Returns the notes to print. Raises MigrationFailed if a migration blows
    up — with the backup path in the message."""
    notes: list[str] = []

    was = ws.schema_version()
    existed = ws.exists
    notes += ws.ensure()

    applied, backup = migrations.apply(ws)
    if applied:
        notes.append(f"backed up the working folder first → {backup}")
        notes += applied
        notes.append(f"schema v{was} → v{ws.schema_version()}")
    elif existed:
        notes.append(f"working folder is already at schema v{ws.schema_version()} — nothing to migrate")

    notes += _sync_prompts(ws, apply=apply_prompts)
    ws.stamp(ws.schema_version())  # refresh the system version stamp
    return notes


def _sync_prompts(ws: Workspace, apply: bool = True) -> list[str]:
    store = WorkerStore(ws.workers_dir, baseline_dir=ws.baseline_dir)
    result = prompts.sync(store, apply=apply)
    if not result.updated and not result.kept:
        return ["worker prompts: no upstream changes"]

    notes = ["worker prompts:"] + result.describe()
    report = prompts.write_report(
        os.path.join(ws.path, REPORT_NAME), result, system_version()
    )
    if report:
        notes.append(f"  → the diffs you have not taken are written to {report}")
    return notes


def preflight(ws: Workspace) -> list[str]:
    """What the SERVER checks before it will run. A server running against a working folder
    it has not migrated is exactly the half-broken state we refuse to be in."""
    if not ws.exists:
        return []
    todo = migrations.pending(ws)
    if not todo:
        return []
    return [
        f"This working folder is at schema v{ws.schema_version()}; this system needs "
        f"v{migrations.LATEST}.",
        "Pending:",
        *[f"  - {m.label}" for m in todo],
        "",
        "Run  ./update.sh   (or: python -m manager migrate)  — it backs the folder up first.",
    ]


def board_for(ws: Workspace, **kwargs) -> Board:
    """The one place a Board is constructed from a workspace, so nothing else has to know
    which folder holds what."""
    return Board(
        ws.data_dir,
        workers_dir=ws.workers_dir,
        baseline_dir=ws.baseline_dir,
        **kwargs,
    )
