"""FastAPI app: localhost board UI + REST + WebSocket live chat.

Run with:  python -m manager  (see __main__.py)

Binds to 127.0.0.1 only, so the board is visible solely to this machine.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import update as updater
from .journal import Journal
from .logs import setup_logging
from .models import PLAN
from .manager import Manager, ManagerConfig
from .recovery import Recovery
from .version import SYSTEM_DIR, system_version
from .workspace import load_env, resolve

log = logging.getLogger("manager")

# The SYSTEM side. Code and UI only — replaced wholesale on every update.
WEB_DIR = os.path.join(SYSTEM_DIR, "web")

# The REPO the manager works on, and the WORKING FOLDER that holds this project's state.
# Neither is derived from where this file happens to sit: see manager/workspace.py.
REPO_DIR = os.path.abspath(os.environ.get("MANAGER_REPO_DIR") or os.path.dirname(SYSTEM_DIR))
WORKSPACE = resolve(REPO_DIR)
WORKSPACE.repo_dir = REPO_DIR
load_env(WORKSPACE)  # so `uvicorn manager.server:app` sees the same config as ./run.sh

# Kept as module-level names because tests and tools import them.
DATA_DIR = WORKSPACE.data_dir
WORKERS_DIR = WORKSPACE.workers_dir


class Hub:
    """Tracks connected WebSockets and fans board events out to all of them."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._sockets.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets.discard(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            targets = list(self._sockets)
        dead = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._sockets.discard(ws)


def create_app() -> FastAPI:
    app = FastAPI(title="manager-kanban")
    hub = Hub()

    # Never serve a working folder this system has not migrated. A half-migrated folder
    # behind a running board is exactly the failure the update path exists to prevent.
    blocked = updater.preflight(WORKSPACE)
    if blocked:
        raise SystemExit("\n  " + "\n  ".join(blocked) + "\n")
    WORKSPACE.ensure()
    setup_logging(WORKSPACE.data_dir)  # a log file that survives the terminal that started us

    board = updater.board_for(WORKSPACE, broadcaster=hub.broadcast)
    config = ManagerConfig(repo_dir=REPO_DIR, data_dir=WORKSPACE.data_dir)
    journal = Journal(WORKSPACE.data_dir)
    manager = Manager(board, config, journal=journal)

    app.state.board = board
    app.state.manager = manager
    app.state.config = config
    app.state.workspace = WORKSPACE
    app.state.journal = journal

    @app.on_event("startup")
    async def recover_interrupted_runs():
        """A run that was in flight when the process died is resumed here, before anyone can
        touch the board. This is the whole point of the journal: without it, a killed run was
        simply gone, and the card span 'working' forever with nothing behind it."""
        notes = await Recovery(board, manager, journal).run(spawn)
        for n in notes:
            log.warning("recovery: %s", n)
        if not notes:
            log.info("recovery: nothing was in flight — clean start")

    def err(msg: str, code: int = 400):
        return JSONResponse({"error": msg}, status_code=code)

    def spawn(coro, label: str) -> None:
        """Fire-and-forget, but never silently. An agent run that dies with nobody reading
        its exception is indistinguishable from a frame that was never sent — which is
        precisely what made the last real bug so expensive to find."""
        task = asyncio.create_task(coro)

        def done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                log.exception("%s failed", label, exc_info=exc)

        task.add_done_callback(done)

    # ---- board / config ----------------------------------------------
    @app.get("/api/board")
    async def get_board():
        return JSONResponse(board.snapshot())

    @app.get("/api/config")
    async def get_config():
        return {
            "repo_dir": config.repo_dir,
            "workspace": WORKSPACE.path,
            "data_dir": config.data_dir,
            "workers_dir": board.workers.dir,
            "mock": config.mock,
            "mock_reason": config.mock_reason,
            "model": config.model or "(sdk default)",
            "permission_mode": config.permission_mode,
            "system_version": system_version(),
            "schema_version": WORKSPACE.schema_version(),
        }

    # ---- cards --------------------------------------------------------
    @app.get("/api/card/{card_id}")
    async def get_card(card_id: str):
        view = board.card_view(card_id)
        return JSONResponse(view) if view else err("not found", 404)

    @app.post("/api/card")
    async def create_card(payload: dict):
        title = (payload.get("title") or "").strip()
        if not title:
            return err("title required")
        card = await board.add_card(
            title,
            (payload.get("description") or "").strip(),
            payload.get("manager_id"),
            pipeline=payload.get("pipeline") or PLAN,
        )
        return JSONResponse({"id": card.id})

    @app.post("/api/card/{card_id}/move")
    async def move_card(card_id: str, payload: dict):
        c = await board.move_card(card_id, payload.get("column", ""))
        return JSONResponse({"ok": bool(c)})

    @app.post("/api/card/{card_id}/assign")
    async def assign_card(card_id: str, payload: dict):
        c = await board.assign_card(card_id, payload.get("manager_id", ""))
        return JSONResponse({"ok": bool(c)})

    @app.post("/api/card/{card_id}/kind")
    async def set_card_kind(card_id: str, payload: dict):
        c = await board.set_card_kind(card_id, payload.get("kind", ""))
        return JSONResponse({"kind": c.kind}) if c else err("not a card kind", 400)

    @app.post("/api/card/{card_id}/trash")
    async def trash_card(card_id: str):
        c = await board.trash_card(card_id)
        return JSONResponse({"ok": bool(c)})

    @app.post("/api/card/{card_id}/restore")
    async def restore_card(card_id: str):
        c = await board.restore_card(card_id)
        return JSONResponse({"ok": bool(c)})

    @app.delete("/api/card/{card_id}")
    async def purge_card(card_id: str):
        return JSONResponse({"ok": await board.purge_card(card_id)})

    @app.get("/api/card/{card_id}/file/{name}")
    async def read_artifact(card_id: str, name: str):
        """Read one file out of the card's folder. Path-traversal is refused."""
        card = board.cards.get(card_id)
        if not card:
            return err("not found", 404)
        root = os.path.realpath(board.abs_dir(card))
        path = os.path.realpath(os.path.join(root, name))
        if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
            return err("not found", 404)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return PlainTextResponse(fh.read())

    @app.get("/api/trash")
    async def get_trash():
        return JSONResponse(board.trash_view())

    # ---- managers -----------------------------------------------------
    @app.get("/api/manager/{manager_id}")
    async def get_manager(manager_id: str):
        view = board.manager_view(manager_id)
        return JSONResponse(view) if view else err("not found", 404)

    @app.post("/api/manager")
    async def create_manager(payload: dict):
        m = await board.add_manager(
            (payload.get("name") or "").strip(), (payload.get("emoji") or "🧭").strip()
        )
        return JSONResponse({"id": m.id})

    @app.delete("/api/manager/{manager_id}")
    async def delete_manager(manager_id: str):
        ok = await board.remove_manager(manager_id)
        return JSONResponse({"ok": ok}) if ok else err("cannot delete the last manager")

    # ---- columns ------------------------------------------------------
    @app.patch("/api/pipeline/{pipeline}")
    async def update_pipeline(pipeline: str, payload: dict):
        color = await board.set_pipeline_color(pipeline, payload.get("color", ""))
        return JSONResponse({"color": color}) if color else err("not a colour", 400)

    @app.post("/api/column")
    async def add_column(payload: dict):
        try:
            col = await board.add_column(
                payload.get("pipeline", ""),
                payload.get("title", ""),
                payload.get("index"),
                bool(payload.get("gate")),
                entry=payload.get("entry", ""),
                work=payload.get("work", ""),
                exit_=payload.get("exit", ""),
                output=payload.get("output", ""),
            )
        except ValueError as e:
            return err(str(e))
        return JSONResponse({"id": col.id, "slug": col.slug})

    @app.patch("/api/column/{column_id}")
    async def update_column(column_id: str, payload: dict):
        col = None
        if "title" in payload:
            col = await board.rename_column(column_id, payload["title"])
        if "gate" in payload:
            col = await board.set_column_gate(column_id, bool(payload["gate"]))
        if "index" in payload:
            col = await board.reorder_column(column_id, int(payload["index"]))
        return JSONResponse({"ok": bool(col)}) if col else err("not found", 404)

    @app.delete("/api/column/{column_id}")
    async def delete_column(column_id: str):
        try:
            col, moved = await board.delete_column(column_id)
        except ValueError as e:
            return err(str(e))
        return JSONResponse({"ok": True, "moved_cards": moved, "title": col.title})

    # ---- workers ------------------------------------------------------
    @app.get("/api/worker/{pipeline}/{column}")
    async def get_worker(pipeline: str, column: str):
        col = board.pipelines.resolve(pipeline, column)
        if not col:
            return err("not found", 404)
        w = board.workers.ensure(col)
        return JSONResponse(
            {
                "pipeline": col.pipeline,
                "column": col.title,
                "slug": col.slug,
                "gate": col.gate,
                "agent_name": w.agent_name,
                "path": w.path,
                "markdown": board.workers.raw(col),
                "contract": w.contract(),
            }
        )

    @app.put("/api/worker/{pipeline}/{column}")
    async def put_worker(pipeline: str, column: str, payload: dict):
        col = board.pipelines.resolve(pipeline, column)
        if not col:
            return err("not found", 404)
        md = payload.get("markdown", "")
        if not md.strip():
            return err("markdown required")
        path = board.workers.write_raw(col, md)
        await board.broadcast_board()
        return JSONResponse({"ok": True, "path": path})

    # ---- WebSocket ----------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await hub.connect(ws)
        try:
            await ws.send_json(board.snapshot())
            while True:
                frame = await ws.receive_json()
                try:
                    await handle_ws(frame, ws)
                except WebSocketDisconnect:
                    raise
                except Exception:  # noqa: BLE001 — one bad frame must not kill the socket
                    log.exception("failed handling %r frame", frame.get("type"))
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            # A crashed handler and a frame that was never sent look identical from the
            # browser: the socket just goes quiet. Log it, or the next bug hides here too.
            log.exception("websocket handler crashed — dropping the connection")
        finally:
            await hub.disconnect(ws)

    async def handle_ws(data: dict, ws: WebSocket) -> None:
        kind = data.get("type")

        if kind == "open":
            view = board.card_view(data["card_id"])
            if view:
                await ws.send_json(view)

        elif kind == "manager_open":
            view = board.manager_view(data["manager_id"])
            if view:
                await ws.send_json(view)

        elif kind == "worker_open":
            key = data.get("key") or ""
            if manager.board.pipelines.by_slug(*key.partition("/")[::2]):
                await ws.send_json(board.worker_chat_view(key))

        elif kind == "trash_open":
            await ws.send_json(board.trash_view())

        elif kind == "create":
            title = (data.get("title") or "").strip()
            if title:
                await board.add_card(
                    title,
                    (data.get("description") or "").strip(),
                    data.get("manager_id"),
                    pipeline=data.get("pipeline") or PLAN,
                )

        elif kind == "move":
            await board.move_card(data["card_id"], data["column"])

        elif kind == "trash":
            await board.trash_card(data["card_id"])

        elif kind == "restore":
            await board.restore_card(data["card_id"])

        elif kind == "purge":
            await board.purge_card(data["card_id"])

        elif kind == "assign":
            await board.assign_card(data["card_id"], data["manager_id"])

        elif kind == "set_kind":
            await board.set_card_kind(data["card_id"], data.get("kind", ""))

        elif kind == "message":
            card_id, text = data.get("card_id"), (data.get("text") or "").strip()
            if card_id and text:
                # don't block the socket while the manager works
                spawn(manager.handle_card_message(card_id, text), f"card {card_id} run")

        elif kind == "manager_message":
            mid, text = data.get("manager_id"), (data.get("text") or "").strip()
            if mid and text:
                spawn(manager.handle_board_message(mid, text), f"manager {mid} run")

        elif kind == "worker_message":
            key, text = data.get("key") or "", (data.get("text") or "").strip()
            if key and text:
                spawn(manager.handle_worker_message(key, text), f"worker {key} run")

        else:
            log.warning("ignoring unknown websocket frame: %r", kind)

    # ---- static UI ----------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    if os.path.isdir(WEB_DIR):
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app


app = create_app()
