"""The queue, as the worker on the Mac reaches it.

The Mac cannot be called from the droplet, so it asks. Once a day `fixer/run.py` runs these over
SSH, inside the running container, where the database address already is:

    docker exec -i lisa_brain python -m app.fixdesk.cli pull   <host>      < info.json
    docker exec -i lisa_brain python -m app.fixdesk.cli claim  <id> investigating|fixing
    docker exec -i lisa_brain python -m app.fixdesk.cli plan   <id>        < plan.json
    docker exec -i lisa_brain python -m app.fixdesk.cli result <id>        < result.json
    docker exec -i lisa_brain python -m app.fixdesk.cli release <id>       < {"error": "..."}
    docker exec -i lisa_brain python -m app.fixdesk.cli undone <id>        < {"ok": true, ...}

Each prints one JSON object on stdout and exits 0; anything else is a failure. Nothing is sent
from here: the desk in the Lisa process sends what the queue says is due."""
from __future__ import annotations

import asyncio
import json
import sys

from ..config import load_settings
from .store import FixStore


def _stdin_json() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    return json.loads(raw) if raw.strip() else {}


async def _run(argv: list[str]) -> dict:
    if not argv:
        return {"ok": False, "error": "usage: cli pull|claim|plan|result|release|undone ..."}
    s = load_settings()
    store = FixStore(s.database_url, schema=s.log_schema)
    await store.open()
    try:
        cmd = argv[0]
        if cmd == "pull":
            host = argv[1] if len(argv) > 1 else "mac"
            await store.heartbeat(host, _stdin_json())
            return {"ok": True, "enabled": bool(s.fix_loop_enabled), **(await store.pull())}
        case_id = int(argv[1])
        if cmd == "claim":
            row = await store.claim(case_id, argv[2])
            return {"ok": row is not None}
        if cmd == "plan":
            body = _stdin_json()
            to = await store.save_plan(case_id, body.get("plan") or {}, body.get("session_id"))
            return {"ok": to is not None, "status": to}
        if cmd == "result":
            to = await store.save_result(case_id, _stdin_json())
            return {"ok": to is not None, "status": to}
        if cmd == "release":
            to = await store.release(case_id, str(_stdin_json().get("error") or "unknown"))
            return {"ok": to is not None, "status": to}
        if cmd == "undone":
            to = await store.save_undo(case_id, _stdin_json())
            return {"ok": to is not None, "status": to}
        return {"ok": False, "error": f"unknown command {cmd}"}
    finally:
        await store.aclose()


def main() -> int:
    try:
        out = asyncio.run(_run(sys.argv[1:]))
    except Exception as exc:
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    print(json.dumps(out, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
