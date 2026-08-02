"""Pause the board. It must stop — and STAY stopped, even across a kill.

The pause is the one wind-down: the human's Pause button and a shipping update call the very
same step (manager.pause). So this asserts what a pause is worth, and shipping inherits it.

Three things, and the second is the one that is easy to get wrong:

  1. It stops. Running work is told to wind down, nothing new starts, and the board says so.
  2. It STAYS stopped. The pause is on disk, so a `kill -9` and a fresh boot come back paused
     — recovery does not get to quietly resume everything the human deliberately stopped.
  3. Resume picks the work back up — the same card, in the column it actually reached, rather
     than starting over or sitting there abandoned looking perfectly healthy.

    python tests/pause_test.py      (mock mode; no API key, no network)
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


def post(port: int, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
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
        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        self.proc.wait(timeout=10)


async def send_ws(port: int, frame: dict) -> None:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
        await ws.recv()  # the board snapshot pushed on connect
        await ws.send(json.dumps(frame))
        await asyncio.sleep(0.25)


def settle(port: int, timeout: float = 15.0) -> dict:
    """Wait for the board to go quiet — a wind-down finishes the column it is in first."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = get(port, "/api/inflight")
        if st["count"] == 0:
            return st
        time.sleep(0.2)
    return get(port, "/api/inflight")


def main() -> int:
    if not os.path.exists(PYBIN):
        print("no .venv — run ./run.sh once first")
        return 1

    ws_dir = tempfile.mkdtemp(prefix="km-pause-ws-")
    repo = tempfile.mkdtemp(prefix="km-pause-repo-")
    port = free_port()

    section("start the board and put a card to work")
    srv = Server(ws_dir, repo, port)
    srv.start()
    data_dir = get(port, "/api/config")["data_dir"]
    marker = os.path.join(data_dir, ".paused")
    check("it starts unpaused", get(port, "/api/inflight")["paused"] is False)
    check("with no pause marker on disk", not os.path.exists(marker))

    cid = post(port, "/api/card", {"title": "Pause me", "description": "mid-flight",
                                   "pipeline": "plan", "kind": "feature"})["id"]
    asyncio.run(send_ws(port, {"type": "message", "card_id": cid, "text": "start"}))
    time.sleep(1.4)  # the mock walks the columns with a pause at each: this lands mid-run
    check("the card is being worked", get(port, f"/api/card/{cid}")["busy"] is True)

    section("pause it")
    st = post(port, "/api/pause")
    check("the board reports itself paused", st["paused"] is True)
    check("...and draining: no new work is dispatched", st["draining"] is True)
    check("the pause is written to disk", os.path.exists(marker))

    st = settle(port)
    check("the work in flight wound down", st["count"] == 0)
    paused_card = get(port, f"/api/card/{cid}")
    paused_col = paused_card["column"]
    check("the card is no longer busy", paused_card["busy"] is False)
    check("it stopped short of the gate", paused_card["gate"] is False)
    check("and it is owed a carry-on when work resumes", st["pending"] >= 1)

    # The point of a pause: it does not creep on. Give it long enough to walk several more
    # columns if it were going to, and assert it did not move.
    time.sleep(2.5)
    still = get(port, f"/api/card/{cid}")
    check("it does not start anything while paused", still["busy"] is False)
    check("and it has not moved a column", still["column"] == paused_col)
    check("the board is still paused", get(port, "/api/inflight")["paused"] is True)

    section("kill -9 the paused board, then start it again")
    srv.kill_hard()
    srv2 = Server(ws_dir, repo, port)
    srv2.start()

    # THE ONE THAT MATTERS. A pause the process forgets is not a pause: recovery would resume
    # every interrupted run on the way up and hand the board back its work, and the human who
    # stopped it would find it running.
    check("it comes back PAUSED", get(port, "/api/inflight")["paused"] is True)
    time.sleep(2.5)
    after = get(port, f"/api/card/{cid}")
    check("the boot resumed nothing", after["busy"] is False)
    check("the card is where it was left", after["column"] == paused_col)
    check("the carry-on is still held", get(port, "/api/inflight")["pending"] >= 1)

    section("resume")
    st = post(port, "/api/resume")
    check("the board is working again", st["paused"] is False)
    check("...and not draining", st["draining"] is False)
    check("the pause marker is gone from disk", not os.path.exists(marker))

    # It carries ON — from the column it actually reached, in its own conversation. It does not
    # start the card over, and it does not sit there abandoned.
    deadline = time.time() + 30
    while time.time() < deadline:
        now = get(port, f"/api/card/{cid}")
        if not now["busy"] and now["gate"]:
            break
        time.sleep(0.3)
    check("the card picked itself back up and walked on to the gate", now["gate"] is True)
    check("its work accumulated rather than starting over",
          os.path.isfile(os.path.join(now["abs_dir"], "IDEA.md")))
    check("nothing is left queued", get(port, "/api/inflight")["pending"] == 0)

    # ---- the upgrade path has to survive its own introduction -----------
    # `./ship.sh` runs the NEW shipping client against the OLD board that is still up. The
    # release that renames an endpoint is therefore the one release that cannot use it: a 0.15
    # board answers /api/pause with a 404, and the very ship that installs the pause would die
    # on it. So the client falls back to the endpoint the old board does have.
    section("shipping falls back when the running board is too old to know the endpoint")
    from manager import shipping

    st = shipping._api_or_older(
        f"http://127.0.0.1:{port}", "/api/no-such-endpoint", older="/api/pause"
    )
    check("a 404 on the new endpoint falls back to the old one", st["draining"] is True)
    post(port, "/api/resume")
    check("...and the board is left working", get(port, "/api/inflight")["paused"] is False)

    srv2.kill_hard()
    print(f"\n{'ALL PASSED' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
