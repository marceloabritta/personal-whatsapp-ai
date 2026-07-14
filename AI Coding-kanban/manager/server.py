"""FastAPI app: localhost board UI + REST + WebSocket live chat.

Run with:  python -m manager  (see __main__.py)

Binds to 127.0.0.1 only, so the board is visible solely to this machine.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import policy
from . import update as updater
from .journal import Journal
from .logs import setup_logging
from .models import BACKLOG, KINDS, PLAN
from .manager import POLICY_KEY, Manager, ManagerConfig
from .recovery import Recovery
from .version import SYSTEM_DIR, system_version
from .workspace import load_env, resolve

log = logging.getLogger("manager")

# The SYSTEM side. Code and UI only — replaced wholesale on every update.
WEB_DIR = os.path.join(SYSTEM_DIR, "web")

# The version of the code THIS PROCESS IS RUNNING — read once, at import.
#
# `system_version()` re-reads the VERSION file on every call, so a server that has been up
# since 0.11.0 would cheerfully report 0.11.1 the moment the file changed on disk. During a
# ship — the one moment you are actually asking "did the new code land?" — it answers about
# the disk instead of about itself. It fooled me while shipping this very change.
RUNNING_VERSION = system_version()


# The REPO the manager works on, and the WORKING FOLDER that holds this project's state.
# Neither is derived from where this file happens to sit: see manager/workspace.py.
REPO_DIR = os.path.abspath(os.environ.get("MANAGER_REPO_DIR") or os.path.dirname(SYSTEM_DIR))
WORKSPACE = resolve(REPO_DIR)
WORKSPACE.repo_dir = REPO_DIR
load_env(WORKSPACE)  # so `uvicorn manager.server:app` sees the same config as ./run.sh

# Kept as module-level names because tests and tools import them.
DATA_DIR = WORKSPACE.data_dir
WORKERS_DIR = WORKSPACE.workers_dir

# Dropped on disk when the server is going down IN ORDER TO COME BACK. `python -m manager`
# sees it after uvicorn stops and re-execs itself into the new code. Without it a SIGTERM is
# just a SIGTERM — which is what you want when someone actually means "stop".
RESTART_SENTINEL = os.path.join(WORKSPACE.path, ".restart")

# How the board asks the process to stop.
#
# NOT a signal. `uvicorn.run()` does not return after SIGTERM — it re-raises the signal once
# it has shut down gracefully, so the process dies at exit code 143 and anything you wrote
# after `uvicorn.run(...)` never executes. That is exactly where the "restart into the new
# code" step lives, so a SIGTERM restart is a restart that never comes back.
#
# `__main__` sets this to uvicorn's own `should_exit` flag, which makes `run()` return
# normally and lets the process go on to re-exec itself.
EXIT_HOOK = None


def request_exit() -> None:
    if EXIT_HOOK is not None:
        EXIT_HOOK()
    else:  # no hook (e.g. `uvicorn manager.server:app` by hand) — a signal is all we have
        os.kill(os.getpid(), signal.SIGTERM)


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

        # Anything the human sent WHILE we were being updated was written to disk rather
        # than started. Now that we are back, send it.
        for n in await manager.dispatch_pending(spawn):
            log.warning("pending: %s", n)

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

    # ---- shipping: drain, then restart. Never kill work. ---------------
    # The old way to ship an update was to kill the process and let recovery pick up the
    # pieces. Recovery works, but it is a seatbelt: a run killed mid-flight loses the turn it
    # was in — the worker that was halfway through a task, and everything it had not yet
    # written. That was happening on EVERY update. So: stop taking new work, wait for the
    # in-flight runs to finish, and only then go down. See manager/shipping.py.
    def _inflight_json() -> dict:
        runs = manager.inflight()
        now = time.time()
        return {
            "draining": manager.draining,
            "count": len(runs),
            "pending": len(manager.pending),
            "runs": [
                {
                    "kind": r.kind,
                    "target": r.target_id,
                    "label": _run_label(r),
                    "seconds": max(0, int(now - r.started_at)),
                }
                for r in runs
            ],
        }

    def _run_label(r) -> str:
        if r.kind == "card":
            c = board.cards.get(r.target_id)
            if not c:
                return r.target_id
            # Where the card is NOW — not `r.column`, which is where it was when the run was
            # dispatched. A run drives a card through several columns, so the journal's copy
            # goes stale within minutes and makes a healthy run look parked.
            col = board.pipelines.get(c.column)
            return f"{c.title} ({col.title if col else 'backlog'})"
        if r.kind == "worker":
            return f"worker chat: {r.target_id}"
        m = board.managers.get(r.target_id)
        return f"board chat with {m.name}" if m else r.target_id

    @app.get("/api/inflight")
    async def get_inflight():
        return JSONResponse(_inflight_json())

    # ---- "an update is available. restart when you like." ---------------
    # The board does NOT stop taking work because new code exists. It carries on, and offers
    # a button. Draining pre-emptively — the old shape — meant the human sat behind a queue
    # for work they had not asked to pause, which is a worse tax than the update itself.
    #
    # When they click: finish what is running, restart, resume. Nothing is killed, and the
    # only window in which anything queues is the one they opened on purpose.
    @app.get("/api/update")
    async def update_available():
        on_disk = system_version()
        return JSONResponse(
            {
                "running": RUNNING_VERSION,
                "on_disk": on_disk,
                "available": on_disk != RUNNING_VERSION,
                "restarting": os.path.exists(RESTART_SENTINEL),
            }
        )

    @app.post("/api/restart")
    async def restart_for_update():
        """TELL THE WORKERS TO STOP, then restart, then pick every card back up.

        Not "wait for them". A worker runs for many minutes, and waiting politely for jobs
        nobody is waiting on turns a ten-second restart into a ten-minute one. They are
        interrupted: what they wrote to the card folder is on disk, what they had not written
        is gone, and the card resumes from the disk afterwards.

        Returns immediately — the board stays up and usable while this happens, and the
        manager keeps answering you.
        """
        runs = manager.begin_drain()
        stopped = await manager.stop_workers()
        await hub.broadcast({"type": "draining", "draining": True, "restarting": True})
        log.warning(
            "restart requested: told %d worker(s) to stop; %d run(s) winding down",
            stopped, len(runs),
        )

        async def _wait_then_restart():
            while True:
                while manager.inflight():
                    await asyncio.sleep(2)     # they FINISH. We do not cut them off.
                # Commit to going down — from HERE, and only here, a message has to be queued,
                # because there will be nothing left to act on it. Then look once more: a run
                # may have started in the gap, and cutting it off is the one thing we promised
                # never to do.
                manager.stopping = True
                await asyncio.sleep(0.5)
                if not manager.inflight():
                    break
                manager.stopping = False
            log.warning("restart: everything finished; going down to come back up")
            with open(RESTART_SENTINEL, "w", encoding="utf-8") as fh:
                fh.write(system_version())
            await hub.broadcast({"type": "shutdown"})
            await asyncio.sleep(0.3)       # let the frame reach the browser
            request_exit()                 # graceful — and `run()` RETURNS, so we can re-exec

        asyncio.create_task(_wait_then_restart())
        return JSONResponse(_inflight_json())

    @app.post("/api/drain")
    async def start_drain():
        manager.begin_drain()
        await hub.broadcast({"type": "draining", "draining": True})
        return JSONResponse(_inflight_json())

    @app.post("/api/undrain")
    async def stop_drain():
        manager.end_drain()
        await hub.broadcast({"type": "draining", "draining": False})
        return JSONResponse(_inflight_json())

    @app.post("/api/shutdown")
    async def shutdown(payload: dict | None = None):
        """Go down cleanly. REFUSES while anything is still running, unless forced — the
        whole point of this path is that it cannot be the thing that destroys work."""
        force = bool((payload or {}).get("force"))
        runs = manager.inflight()
        if runs and not force:
            return err(f"{len(runs)} run(s) still in flight — drain first", 409)
        log.warning("shutdown requested (in flight: %d, forced: %s)", len(runs), force)
        manager.stopping = True   # from here a message must be saved: nothing is left to run it
        await hub.broadcast({"type": "shutdown"})

        async def _bye():
            await asyncio.sleep(0.25)  # let the response and the frame actually go out
            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_bye())
        return JSONResponse({"ok": True, "inflight": len(runs)})

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
            "system_version": RUNNING_VERSION,   # what is RUNNING, not what is on disk
            "version_on_disk": system_version(),  # what a restart would give you
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
            pipeline=payload.get("pipeline") or BACKLOG,
            kind=payload.get("kind") or "",
        )
        # Nobody said what it is → the manager decides, now. This is what makes "no card is
        # left without a type" true, rather than merely intended.
        if card.kind not in KINDS:
            spawn(manager.triage_card(card.id), f"triage {card.id}")
        return JSONResponse({"id": card.id, "kind": card.kind})

    @app.post("/api/card/{card_id}/route")
    async def route_card(card_id: str, payload: dict):
        c = await board.route_card(card_id, (payload.get("pipeline") or "").strip().lower())
        return JSONResponse({"pipeline": c.pipeline}) if c else err(
            "cannot route: unknown pipeline, or the card still has no type", 400
        )

    @app.post("/api/card/{card_id}/backlog")
    async def unroute_card(card_id: str):
        c = await board.send_to_backlog(card_id)
        return JSONResponse({"ok": bool(c)})

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
    # ---- the manager's own brain: <workspace>/MANAGER.md ---------------
    @app.get("/api/policy")
    async def get_policy():
        return JSONResponse(
            {
                "markdown": policy.read(WORKSPACE.path),
                "path": policy.path_for(WORKSPACE.path),
            }
        )

    @app.put("/api/policy")
    async def put_policy(payload: dict):
        md = payload.get("markdown", "")
        if not (md or "").strip():
            return err("refusing to erase your standing orders entirely")
        path = policy.write(WORKSPACE.path, md)
        return JSONResponse({"ok": True, "path": path})

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
            if key == POLICY_KEY or board.pipelines.by_slug(*key.partition("/")[::2]):
                await ws.send_json(board.worker_chat_view(key))

        elif kind == "trash_open":
            await ws.send_json(board.trash_view())

        elif kind == "create":
            title = (data.get("title") or "").strip()
            if title:
                card = await board.add_card(
                    title,
                    (data.get("description") or "").strip(),
                    data.get("manager_id"),
                    pipeline=data.get("pipeline") or BACKLOG,
                    kind=data.get("kind") or "",
                )
                if card.kind not in KINDS:
                    spawn(manager.triage_card(card.id), f"triage {card.id}")

        elif kind == "backlog":
            await board.send_to_backlog(data["card_id"])

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
                spawn(manager.handle_prompt_message(key, text), f"prompt {key} run")

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
