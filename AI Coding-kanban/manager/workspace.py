"""The WORKING FOLDER — everything that belongs to *your* project.

The system (this repo: `manager/`, `web/`, `run.sh`, the default worker templates and the
migrations) is replaced wholesale on every update. The working folder is not: it is yours,
it survives every update, and an update *migrates* it rather than clobbering it.

    <workspace>/
    ├── .kanban-version        which system + schema version this folder is at
    ├── .env                   this project's config (repo, model, port, mock…)
    ├── board.json             cards, threads, managers
    ├── pipelines.json         your columns and gates
    ├── cards/<pipeline>/<column>/<card>/   every card's folder
    ├── cards/trash/
    ├── workers/               YOUR agent prompts — never overwritten by an update
    ├── .defaults/workers/     the pristine defaults yours were scaffolded from
    │                          (the baseline for the three-way merge on update)
    └── .backups/              a copy of the whole folder, taken before each migration

Where it lives, in order of precedence:

    1. $MANAGER_WORKSPACE  (or $MANAGER_DATA_DIR, the older name for the same thing)
    2. the `.workspace` pointer file written into the system folder on first run
    3. ~/.manager-kanban/<repo-name>/

Nothing here derives a path from where the *system* happens to sit on disk. That was the
old design and it is what made the system unupgradable.
"""
from __future__ import annotations

import json
import os
import shutil
import time

from .models import slugify
from .version import SYSTEM_DIR, system_version

VERSION_FILE = ".kanban-version"
POINTER_FILE = ".workspace"  # written into the SYSTEM folder: "the workspace I last used"
DEFAULT_HOME = os.path.join(os.path.expanduser("~"), ".manager-kanban")
KEEP_BACKUPS = 5


class WorkspaceError(RuntimeError):
    """Raised when the working folder cannot be used safely. Always fail loudly here."""


class Workspace:
    """One project's working folder. Owns nothing but paths, versions and backups —
    the Board owns what's inside it."""

    def __init__(self, path: str, repo_dir: str = ""):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.repo_dir = os.path.abspath(repo_dir) if repo_dir else ""

    # ---- the paths inside it -----------------------------------------
    @property
    def data_dir(self) -> str:
        """board.json, pipelines.json and cards/ all live directly in the workspace."""
        return self.path

    @property
    def workers_dir(self) -> str:
        return os.environ.get("MANAGER_WORKERS_DIR") or os.path.join(self.path, "workers")

    @property
    def baseline_dir(self) -> str:
        """The pristine defaults this folder's workers were scaffolded from."""
        return os.path.join(self.path, ".defaults", "workers")

    @property
    def env_path(self) -> str:
        return os.path.join(self.path, ".env")

    @property
    def board_path(self) -> str:
        return os.path.join(self.path, "board.json")

    @property
    def version_path(self) -> str:
        return os.path.join(self.path, VERSION_FILE)

    @property
    def backups_dir(self) -> str:
        return os.path.join(self.path, ".backups")

    @property
    def exists(self) -> bool:
        return os.path.exists(self.version_path) or os.path.exists(self.board_path)

    # ---- the version stamp -------------------------------------------
    def read_version(self) -> dict:
        try:
            with open(self.version_path, "r", encoding="utf-8") as fh:
                v = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return v if isinstance(v, dict) else {}

    def schema_version(self) -> int:
        """Which migration this folder has been brought up to. 0 = never migrated."""
        try:
            return int(self.read_version().get("schema_version", 0))
        except (TypeError, ValueError):
            return 0

    def stamp(self, schema_version: int) -> None:
        v = self.read_version()
        v.update(
            {
                "system_version": system_version(),
                "schema_version": int(schema_version),
                "repo": self.repo_dir or v.get("repo", ""),
                "updated_at": time.time(),
            }
        )
        v.setdefault("created_at", v["updated_at"])
        _write_json(self.version_path, v)

    # ---- creating / adopting -----------------------------------------
    def ensure(self) -> list[str]:
        """Make the folder exist and be structurally sound. Returns human-readable notes
        about anything it had to do (a first-time create, adopting an old `data/` folder)."""
        notes: list[str] = []
        fresh = not self.exists

        os.makedirs(self.path, exist_ok=True)
        if fresh:
            notes += self._adopt_legacy()
            fresh = not self.exists  # adoption may have filled it
        else:
            self._refuse_to_strand_an_old_board()

        for d in (self.workers_dir, self.baseline_dir, os.path.join(self.path, "cards")):
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.env_path):
            _write_text(self.env_path, _ENV_TEMPLATE.format(repo=self.repo_dir or ""))
            notes.append(f"wrote a starter .env at {self.env_path}")

        # The manager's standing orders. Scaffolded from the system default, then the
        # human's forever — `ensure` here never overwrites. A folder born current runs no
        # migration, so this is the ONLY thing that gives a fresh install its policy file;
        # older folders get it from migration 0002 instead.
        from . import policy

        if policy.ensure(self.path):
            notes.append(f"wrote the manager's standing orders at {policy.path_for(self.path)}")

        self._check_repo_match()

        if fresh:
            # A folder created by today's system is born current: there is nothing older
            # to migrate. Only folders that predate a migration ever run it.
            from . import migrations

            self.stamp(migrations.LATEST)
            notes.append(f"created working folder {self.path} (schema v{migrations.LATEST})")
        return notes

    def _refuse_to_strand_an_old_board(self) -> None:
        """There is an old `data/board.json` in the system folder, and this working folder
        ALREADY has a board — so adoption will not fire, and those old cards would simply be
        ignored. Silently serving the wrong board is the worst thing this could do, so it
        refuses and makes the human choose.

        This is not hypothetical: copy a new system folder (carrying a stale `.workspace`
        pointer) on top of an old install, and you land here. The board would have come up
        looking fine, and empty.
        """
        found = find_legacy_state(SYSTEM_DIR)
        if not found:
            return
        data_dir, _ = found
        raise WorkspaceError(
            f"there is an un-adopted board at {data_dir}, and the working folder\n"
            f"  {self.path}\n"
            f"already has one. I will not pick for you, and I will not quietly ignore the old one.\n\n"
            f"  · to USE the old board:   MANAGER_WORKSPACE=<a fresh path> ./run.sh adopt {SYSTEM_DIR}\n"
            f"  · to KEEP the current one and retire the old: move {data_dir} aside yourself.\n\n"
            f"(A stale `.workspace` pointer file copied in from another install causes exactly this.)"
        )

    def _check_repo_match(self) -> None:
        """A workspace belongs to one repo. Two projects with the same folder name would
        otherwise silently share cards — refuse instead."""
        recorded = (self.read_version().get("repo") or "").rstrip("/")
        if recorded and self.repo_dir and recorded != self.repo_dir.rstrip("/"):
            raise WorkspaceError(
                f"working folder {self.path} belongs to repo {recorded}, "
                f"but this run is for {self.repo_dir}.\n"
                f"Set MANAGER_WORKSPACE=/some/other/path to give this repo its own folder."
            )

    def _adopt_legacy(self) -> list[str]:
        """The old layout kept `data/` and `workers/` inside the system folder, where the
        next update would have destroyed them. If the new code has landed on top of an old
        install, bring its state across rather than stranding it."""
        if not find_legacy_state(SYSTEM_DIR):
            return []
        return self.adopt(SYSTEM_DIR, move_originals=True)

    def adopt(self, old_install: str, move_originals: bool = False) -> list[str]:
        """Bring the state of an older install into this working folder.

        `old_install` may be an old system folder (which has `data/` and `workers/` inside
        it) or the data folder itself. Copies — never moves — so the source survives even if
        this goes wrong. Refuses to write over a working folder that already has a board:
        merging two boards is not something this should ever guess at.
        """
        found = find_legacy_state(old_install)
        if not found:
            raise WorkspaceError(
                f"no board state found in {old_install}.\n"
                f"Expected a board.json there, or a data/board.json inside it."
            )
        data_dir, workers_dir = found

        if os.path.exists(self.board_path):
            raise WorkspaceError(
                f"{self.path} already holds a board — refusing to overwrite it with the one "
                f"in {data_dir}.\n"
                f"If you meant to adopt into a clean folder, point MANAGER_WORKSPACE at one."
            )

        os.makedirs(self.path, exist_ok=True)
        notes = [f"adopting the board in {data_dir}"]
        _copy_into(data_dir, self.path)
        if workers_dir:
            _copy_into(workers_dir, os.path.join(self.path, "workers"))
            notes.append(f"copied your worker prompts from {workers_dir}")

        if move_originals:
            # In-place upgrade: the originals sit exactly where an update would clobber them,
            # so get them out of the way — but move, never delete.
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for old in (data_dir, workers_dir):
                if old and os.path.isdir(old):
                    shutil.move(old, f"{old}.migrated-{stamp}")
            notes.append(
                f"the originals were moved aside to *.migrated-{stamp} (not deleted) — "
                f"remove them once the board looks right"
            )
        else:
            notes.append(f"{data_dir} was COPIED, not moved — the old install is untouched")
        return notes

    # ---- backups ------------------------------------------------------
    def backup(self, label: str = "") -> str:
        """Copy the whole working folder aside. Taken before every migration: a half-applied
        migration that leaves cards broken is the worst outcome available."""
        os.makedirs(self.backups_dir, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-v{self.schema_version()}{'-' + label if label else ''}"
        dest = os.path.join(self.backups_dir, name)
        shutil.copytree(
            self.path,
            dest,
            ignore=shutil.ignore_patterns(".backups", "*.tmp"),
            dirs_exist_ok=True,
        )
        self._prune_backups()
        return dest

    def _prune_backups(self) -> None:
        try:
            kept = sorted(os.listdir(self.backups_dir))
        except OSError:
            return
        for old in kept[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(self.backups_dir, old), ignore_errors=True)


# ---------------------------------------------------------------------------
# finding the state of an older install
# ---------------------------------------------------------------------------
def find_legacy_state(path: str) -> tuple[str, str | None] | None:
    """Where does this folder keep its board? Returns (data_dir, workers_dir|None), or None.

    Handles both things a human is likely to point at:
      * an old SYSTEM folder   — board lives in <path>/data, prompts in <path>/workers
      * the data folder itself — board.json is right there
    """
    path = os.path.abspath(os.path.expanduser(path))
    inner = os.path.join(path, "data")
    if os.path.isfile(os.path.join(inner, "board.json")):
        workers = os.path.join(path, "workers")
        return inner, workers if os.path.isdir(workers) else None
    if os.path.isfile(os.path.join(path, "board.json")):
        workers = os.path.join(path, "workers")
        if not os.path.isdir(workers):
            sibling = os.path.join(os.path.dirname(path), "workers")
            workers = sibling if os.path.isdir(sibling) else ""
        return path, workers or None
    return None


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def default_path_for(repo_dir: str) -> str:
    return os.path.join(DEFAULT_HOME, slugify(os.path.basename(os.path.abspath(repo_dir)), "project"))


def resolve(repo_dir: str, system_dir: str = SYSTEM_DIR) -> Workspace:
    """Find this project's working folder without ever deriving it from where the system
    happens to sit. See the module docstring for the precedence."""
    explicit = os.environ.get("MANAGER_WORKSPACE") or os.environ.get("MANAGER_DATA_DIR")
    if explicit:
        return Workspace(explicit, repo_dir)

    pointer = os.path.join(system_dir, POINTER_FILE)
    try:
        with open(pointer, "r", encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except OSError:
        recorded = ""
    if recorded:
        return Workspace(recorded, repo_dir)

    ws = Workspace(default_path_for(repo_dir), repo_dir)
    _write_text(pointer, ws.path + "\n")  # so the next run finds the same folder
    return ws


def load_env(ws: Workspace, system_dir: str = SYSTEM_DIR) -> None:
    """Load `.env` from the system folder (legacy) then the working folder (authoritative).
    A variable already set in the real environment always wins over both."""
    for path in (os.path.join(system_dir, ".env"), ws.env_path):
        for key, value in _parse_env(path).items():
            os.environ.setdefault(key, value)


def _parse_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
_ENV_TEMPLATE = """\
# This project's config. It lives in the WORKING FOLDER, so an update never touches it.
# Anything already exported in your shell wins over what's here.

# The repo the manager operates on.
MANAGER_REPO_DIR={repo}

# Live mode needs either an API key here, or a logged-in Claude Code CLI (`claude login`).
ANTHROPIC_API_KEY=

# 1 forces the scripted mock manager, 0 forces the real one. Unset = auto-detect.
# MANAGER_MOCK=

# MANAGER_PORT=4173
# MANAGER_MODEL=
# MANAGER_PERMISSION_MODE=bypassPermissions
"""


def _write_json(path: str, payload: dict) -> None:
    _write_text(path, json.dumps(payload, indent=2) + "\n")


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _copy_into(src: str, dst: str) -> None:
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
