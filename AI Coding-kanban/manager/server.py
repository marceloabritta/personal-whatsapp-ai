"""FastAPI app: localhost board UI + REST + WebSocket live chat.

Run with:  python -m manager  (see __main__.py)

Binds to 127.0.0.1 only, so the board is visible solely to this machine.
"""
from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .board import Board
from .manager import Manager, ManagerConfig

FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(FEATURE_DIR, "web")
DATA_DIR = os.environ.get("MANAGER_DATA_DIR") or os.path.join(FEATURE_DIR, "data")
REPO_DIR = os.environ.get("MANAGER_REPO_DIR") or os.path.dirname(FEATURE_DIR)


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
    board = Board(DATA_DIR, broadcaster=hub.broadcast)
    config = ManagerConfig(repo_dir=REPO_DIR, data_dir=DATA_DIR)
    manager = Manager(board, config)

    app.state.board = board
    app.state.manager = manager
    app.state.config = config

    # ---- REST --------------------------------------------------------
    @app.get("/api/board")
    async def get_board():
        return JSONResponse(board.snapshot())

    @app.get("/api/config")
    async def get_config():
        return {
            "repo_dir": config.repo_dir,
            "mock": config.mock,
            "model": config.model or "(sdk default)",
            "permission_mode": config.permission_mode,
        }

    @app.get("/api/card/{card_id}")
    async def get_card(card_id: str):
        view = board.card_view(card_id)
        if not view:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(view)

    @app.post("/api/card")
    async def create_card(payload: dict):
        title = (payload.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title required"}, status_code=400)
        card = await board.add_card(title, (payload.get("description") or "").strip())
        return JSONResponse({"id": card.id})

    @app.post("/api/card/{card_id}/move")
    async def move_card(card_id: str, payload: dict):
        c = await board.move_card(card_id, payload["column"])
        return JSONResponse({"ok": bool(c)})

    # ---- WebSocket ---------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await hub.connect(ws)
        try:
            await ws.send_json(board.snapshot())
            while True:
                data = await ws.receive_json()
                await handle_ws(data, ws)
        except WebSocketDisconnect:
            await hub.disconnect(ws)
        except Exception:  # noqa: BLE001
            await hub.disconnect(ws)

    async def handle_ws(data: dict, ws: WebSocket) -> None:
        kind = data.get("type")
        if kind == "open":
            view = board.card_view(data["card_id"])
            if view:
                await ws.send_json(view)
        elif kind == "create":
            title = (data.get("title") or "").strip()
            if title:
                await board.add_card(title, (data.get("description") or "").strip())
        elif kind == "move":
            await board.move_card(data["card_id"], data["column"])
        elif kind == "message":
            card_id = data["card_id"]
            text = (data.get("text") or "").strip()
            if card_id and text:
                # run the manager without blocking the socket
                asyncio.create_task(manager.handle_user_message(card_id, text))

    # ---- static UI ---------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    if os.path.isdir(WEB_DIR):
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app


app = create_app()
