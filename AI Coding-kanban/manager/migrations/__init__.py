"""Migrations: what an update tells the working folder to do.

An update is not just new code. It is new code **plus an ordered set of migrations that
adapt the working folder to it**. The system carries a `VERSION`; the working folder
records the schema version it was last migrated to. The migrations are what close the gap.

Rules, and they are not negotiable:

  * **Ordered.**       `m0001_…`, `m0002_…`. They run lowest-first, never out of order.
  * **Forward-only.**  There is no down-migration. Restore the backup instead.
  * **Idempotent.**    Running one twice must be a no-op. Assume it will happen.
  * **Loud.**          If one fails, the update stops right there and says so. A folder is
                       backed up before any of this runs (see Workspace.backup) — a
                       half-applied migration is the worst outcome available.

Writing one, for the feature you are about to ship:

    manager/migrations/m0002_remote_vm.py

    NUMBER = 2
    DESCRIPTION = "add the remote-VM config keys, defaulted to local-only"

    def migrate(ws):
        # ws is a Workspace. Mutate the FOLDER, not the code. Return notes for the human.
        add_env_key(ws, "MANAGER_REMOTE_HOST", "")
        return ["added MANAGER_REMOTE_HOST (empty = stay local)"]

Then bump `VERSION`. That is the whole contract: a user pulls, runs ./update.sh, and their
folder gains the capability with their cards, columns and prompts untouched.
"""
from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass
from typing import Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILENAME = re.compile(r"^m(\d{4})_([a-z0-9_]+)\.py$")


@dataclass
class Migration:
    number: int
    name: str
    description: str
    run: Callable

    @property
    def label(self) -> str:
        return f"{self.number:04d} {self.description or self.name}"


def discover() -> list[Migration]:
    out: list[Migration] = []
    for fn in sorted(os.listdir(_HERE)):
        m = _FILENAME.match(fn)
        if not m:
            continue
        mod = importlib.import_module(f"{__name__}.{fn[:-3]}")
        out.append(
            Migration(
                number=int(m.group(1)),
                name=m.group(2),
                description=getattr(mod, "DESCRIPTION", ""),
                run=getattr(mod, "migrate"),
            )
        )
    out.sort(key=lambda x: x.number)
    numbers = [x.number for x in out]
    if len(set(numbers)) != len(numbers):
        raise RuntimeError(f"duplicate migration numbers: {numbers}")
    return out


def latest() -> int:
    """The schema version a folder is current at once every migration has run."""
    found = discover()
    return found[-1].number if found else 0


def pending(ws) -> list[Migration]:
    at = ws.schema_version()
    return [m for m in discover() if m.number > at]


def apply(ws, backup: bool = True) -> tuple[list[str], str | None]:
    """Bring a working folder up to `latest()`. Returns (notes, backup_path).

    Raises whatever a migration raises, having stamped nothing — the folder is left at the
    last version that fully applied, and the backup path is on the exception message.
    """
    todo = pending(ws)
    if not todo:
        return [], None

    backup_path = ws.backup(label="premigrate") if backup else None
    notes: list[str] = []
    for m in todo:
        try:
            notes.append(f"migration {m.label}")
            for note in m.run(ws) or []:
                notes.append(f"  · {note}")
        except Exception as e:  # noqa: BLE001 — fail loudly, never half-apply in silence
            raise MigrationFailed(m, backup_path, e) from e
        ws.stamp(m.number)  # stamp each one: a later failure never re-runs an earlier one

    # A folder records its schema in two places: the version file (authoritative) and
    # `board.json`, which is what the Board reads when it loads. Reconcile them here, once,
    # rather than making every future migration remember to — miss it and the two disagree,
    # which is exactly the kind of drift that makes the next upgrade unanswerable.
    from ._helpers import set_board_key

    set_board_key(ws, "schema_version", todo[-1].number)
    return notes, backup_path


class MigrationFailed(RuntimeError):
    def __init__(self, migration: Migration, backup_path: str | None, cause: Exception):
        self.migration = migration
        self.backup_path = backup_path
        super().__init__(
            f"migration {migration.label} FAILED: {cause}\n"
            f"Your working folder was NOT fully migrated and the system may not run against it.\n"
            + (
                f"A backup taken before any migration ran is at:\n  {backup_path}\n"
                if backup_path
                else ""
            )
            + "Nothing further was applied. Fix the cause, or restore the backup."
        )


LATEST = latest()
