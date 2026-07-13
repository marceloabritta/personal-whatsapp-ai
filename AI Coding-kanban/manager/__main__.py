"""Entry point.

    python -m manager                  start the board (the default)
    python -m manager status           what version am I on, what would an update do — offline
    python -m manager migrate          bring the working folder up to this system's schema
    python -m manager adopt <path>     take over the state of an OLD install elsewhere on disk
    python -m manager where            print the working folder's path and nothing else

`./run.sh` calls migrate-then-serve; `./update.sh` pulls first and then calls migrate.
Upgrading from a pre-0.2 install? See UPGRADING.md — it is written for whoever (or whatever)
is doing the upgrade, and it is the only document they need.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import migrations, update as updater
from .manager import ManagerConfig
from .version import SYSTEM_DIR, system_version
from .workspace import Workspace, WorkspaceError, load_env, resolve


def _context() -> tuple[str, Workspace]:
    repo = os.path.abspath(os.environ.get("MANAGER_REPO_DIR") or os.path.dirname(SYSTEM_DIR))
    ws = resolve(repo)
    ws.repo_dir = repo
    load_env(ws)  # the working folder's .env — the real environment still wins
    return repo, ws


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="manager", description="manager-kanban")
    p.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "migrate", "adopt", "status", "where"],
    )
    p.add_argument(
        "path",
        nargs="?",
        help="for `adopt`: the OLD install to take the board state from",
    )
    args = p.parse_args(argv)

    try:
        repo, ws = _context()
    except WorkspaceError as e:
        print(f"\n  ✗ {e}\n", file=sys.stderr)
        return 2

    if args.command == "where":
        print(ws.path)
        return 0
    if args.command == "status":
        return _status(repo, ws)
    if args.command == "adopt":
        return _adopt(ws, args.path)
    if args.command == "migrate":
        return _migrate(ws)
    return _serve(repo, ws)


def _adopt(ws: Workspace, path: str | None) -> int:
    """Take over an older install's cards, columns and prompts, then migrate them forward.

    This is the path for "here is a new system folder, and my old one is over there" — the
    upgrade that a `git pull` cannot do for you because the new code never landed on top of
    the old install.
    """
    if not path:
        print("\n  usage: python -m manager adopt /path/to/old/manager-kanban\n", file=sys.stderr)
        return 2
    try:
        notes = ws.adopt(path)
    except WorkspaceError as e:
        print(f"\n  ✗ {e}\n", file=sys.stderr)
        return 2
    print()
    for n in notes:
        print(f"  {n}")
    print("  now migrating it forward…")
    return _migrate(ws)


def _status(repo: str, ws: Workspace) -> int:
    s = updater.status(ws)
    print(f"\n  system version   {s['system_version']}   ({SYSTEM_DIR})")
    print(f"  repo             {repo}")
    print(f"  working folder   {s['workspace']}")

    old = s["unadopted_board"]
    if old and s["workspace_exists"]:
        # The dangerous one: an old board is sitting right here, and the working folder this
        # run resolved to already has a different board. Say so before anything else.
        print(f"\n  ⚠ AN OLD BOARD IS SITTING HERE, UN-ADOPTED:\n      {old}")
        print("    ...and the working folder above already has a board of its own, so it will")
        print("    NOT be adopted automatically. Those cards are one command from being")
        print("    ignored entirely. Read UPGRADING.md §2 before you do anything else.")
        print("    A stale `.workspace` pointer file copied from another install does this.\n")
        return 1
    if old:
        print(f"\n  ⚠ an old board is sitting here: {old}")
        print("    ./run.sh will adopt it into the working folder above, and move the")
        print("    original aside (never delete it). See UPGRADING.md §3.\n")
        return 0

    if not s["workspace_exists"]:
        print(f"\n  This folder does not exist yet. ./run.sh will create it, at schema "
              f"v{s['schema_latest']}.\n")
        return 0
    print(f"  schema           v{s['schema_version']} of v{s['schema_latest']}")
    if s["pending"]:
        print("\n  pending migrations:")
        for m in s["pending"]:
            print(f"    - {m}")
        print("\n  run ./update.sh (or: python -m manager migrate) to apply them.\n")
    else:
        print("  up to date — nothing to migrate.\n")
    return 0


def _migrate(ws: Workspace) -> int:
    try:
        notes = updater.migrate(ws)
    except migrations.MigrationFailed as e:
        print(f"\n  ✗ {e}\n", file=sys.stderr)
        return 1
    print()
    for n in notes:
        print(f"  {n}")
    print(flush=True)  # flush: the server's banner goes to stderr right after this
    return 0


def _serve(repo: str, ws: Workspace) -> int:
    import uvicorn

    # Migrate before serving, never during. The server refuses to run against a folder it
    # has not brought up to its own schema (see manager/update.py: preflight).
    if updater.preflight(ws) or not ws.exists:
        if _migrate(ws) != 0:
            return 1

    host = os.environ.get("MANAGER_HOST", "127.0.0.1")  # localhost only
    port = int(os.environ.get("MANAGER_PORT", "4173"))
    _banner(host, port, repo, ws)
    uvicorn.run("manager.server:app", host=host, port=port, log_level="info")
    return 0


def _banner(host: str, port: int, repo: str, ws: Workspace) -> None:
    cfg = ManagerConfig(repo, ws.data_dir)
    mode = "MOCK (scripted, no real agent)" if cfg.mock else "LIVE (Agent SDK)"
    out = sys.stderr
    print(f"\n  manager-kanban {system_version()}  (schema v{ws.schema_version()})", file=out)
    print(f"  board:    http://{host}:{port}", file=out)
    print(f"  repo:     {repo}", file=out)
    print(f"  folder:   {ws.path}", file=out)
    print(f"  manager:  {mode} — {cfg.mock_reason}\n", file=out)


if __name__ == "__main__":
    raise SystemExit(main())
