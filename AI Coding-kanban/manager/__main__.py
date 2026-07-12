"""Entry point: `python -m manager` starts the localhost board server."""
from __future__ import annotations

import os
import sys

import uvicorn


def main() -> None:
    host = os.environ.get("MANAGER_HOST", "127.0.0.1")  # localhost only
    port = int(os.environ.get("MANAGER_PORT", "4173"))
    # `manager.server:app` is import-string form so reload/workers behave.
    banner(host, port)
    uvicorn.run("manager.server:app", host=host, port=port, log_level="info")


def banner(host: str, port: int) -> None:
    from .server import REPO_DIR
    from .manager import ManagerConfig

    mock = ManagerConfig(REPO_DIR, "").mock
    mode = "MOCK (no ANTHROPIC_API_KEY set)" if mock else "LIVE (Agent SDK)"
    print("\n  manager-kanban", file=sys.stderr)
    print(f"  board:   http://{host}:{port}", file=sys.stderr)
    print(f"  repo:    {REPO_DIR}", file=sys.stderr)
    print(f"  manager: {mode}\n", file=sys.stderr)


if __name__ == "__main__":
    main()
