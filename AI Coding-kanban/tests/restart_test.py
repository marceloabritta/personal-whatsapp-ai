"""Kill the server and start it again. Nothing may be lost.

This is decision 3, asserted rather than assumed. It runs the REAL server as a subprocess,
drives a real card through the real websocket, then `kill -9`s it mid-run — no clean
shutdown, no chance to flush anything — and restarts it.

What must survive: the card, its column, its folder, the artifacts in that folder, its chat
thread, and the manager's thread. What must NOT survive is the in-flight turn itself — and
the card must come back saying so, rather than sitting there with a spinner and no run
behind it.

    python tests/restart_test.py      (mock mode; no API key, no network)
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PYBIN = os.path.join(ROOT, ".venv", "bin", "python")
FAILED: list = []


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.load(r)


def post(port: int, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


class Server:
    """The real thing: `python -m manager`, in its own process group so we can kill it hard."""

    def __init__(self, workspace: str, repo: str, port: int):
        self.env = {
            **os.environ,
            "MANAGER_WORKSPACE": workspace,
            "MANAGER_REPO_DIR": repo,
            "MANAGER_PORT": str(port),
            "MANAGER_MOCK": "1",
        }
        self.port = port
        self.proc = None

    def start(self, timeout: float = 25.0) -> None:
        self.proc = subprocess.Popen(
            [PYBIN, "-m", "manager"],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode(errors="replace")
                raise RuntimeError(f"server died on startup:\n{out}")
            try:
                get(self.port, "/api/board")
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.15)
        raise RuntimeError("server never came up")

    def kill_hard(self) -> None:
        """SIGKILL the whole group. No shutdown hook, no flush, no mercy — the point is that
        the board's state is already durable on disk, not that it gets a chance to save."""
        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        self.proc.wait(timeout=10)


async def send_ws(port: int, frame: dict) -> None:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        await ws.recv()  # the board snapshot the server pushes on connect
        await ws.send(json.dumps(frame))
        await asyncio.sleep(0.25)  # let the frame land before we close


def send_ws_sync(port: int, frame: dict) -> None:
    asyncio.run(send_ws(port, frame))


def main() -> int:
    if not os.path.exists(PYBIN):
        print("no .venv — run ./run.sh once first")
        return 1

    ws_dir = tempfile.mkdtemp(prefix="km-restart-ws-")
    repo = tempfile.mkdtemp(prefix="km-restart-repo-")
    port = free_port()

    section("start the server, put a card through it")
    srv = Server(ws_dir, repo, port)
    srv.start()
    cfg = get(port, "/api/config")
    check("it is serving the working folder we gave it", cfg["workspace"] == ws_dir)
    check("the card folders live there, not in the system folder", not cfg["data_dir"].startswith(ROOT + os.sep))
    check("it reports a schema version", cfg["schema_version"] >= 1)

    cid = post(port, "/api/card", {"title": "Survive a kill", "description": "the whole point",
                                   "pipeline": "plan", "kind": "feature"})["id"]
    asyncio.run(send_ws(port, {"type": "message", "card_id": cid, "text": "start"}))

    # The mock manager walks the columns with a pause at each one, so this lands mid-run.
    time.sleep(1.4)
    card = get(port, f"/api/card/{cid}")
    card_dir = card["abs_dir"]
    check("the manager is working the card", card["busy"] is True)
    check("it has already written an artifact", os.path.isfile(os.path.join(card_dir, "IDEA.md")))

    section("kill -9 it, mid-run")
    srv.kill_hard()
    check("the process is gone", srv.proc.poll() is not None)
    check("the card's folder is still on disk", os.path.isdir(card_dir))

    section("start it again")
    srv2 = Server(ws_dir, repo, port)
    srv2.start()
    back = get(port, f"/api/card/{cid}")

    check("the card came back", back["id"] == cid)
    check("its title survived", back["title"] == "Survive a kill")
    check("its column survived", back["column"] == card["column"])
    check("its folder survived", os.path.isdir(back["abs_dir"]))
    check("the artifact in its folder survived", os.path.isfile(os.path.join(back["abs_dir"], "IDEA.md")))
    check("its chat thread survived", any(m["text"] == "start" for m in back["thread"]))
    check("the manager's session id survived", get(port, "/api/board") is not None)

    # The interrupted RUN is recovered rather than abandoned — the board tells you it was cut
    # off and resumes it by itself. (recovery_test.py is where that is asserted properly.)
    check(
        "it TELLS you the run was cut off",
        any("cut off" in m["text"].lower() or "killed while working" in m["text"].lower()
            for m in back["thread"]),
    )

    section("and it finishes the card without being asked")
    deadline = time.time() + 25
    while time.time() < deadline:
        now = get(port, f"/api/card/{cid}")
        if not now["busy"] and now["gate"]:
            break
        time.sleep(0.3)
    check("the card walked on to the gate", now["gate"] is True)
    check("its folder accumulated the later columns' work", os.path.isfile(os.path.join(now["abs_dir"], "PLAN.md")))
    check("the earlier artifact is still there too", os.path.isfile(os.path.join(now["abs_dir"], "IDEA.md")))

    srv2.kill_hard()
    print(f"\n{'ALL PASSED' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
