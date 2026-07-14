"""Shipping an update without destroying work.

The old way: `kill` the server, restart it, let `manager/recovery.py` sort it out. Recovery is
good — it re-enters the SDK session and tells the manager to trust the disk over its own
memory — but it is a **seatbelt, not a shipping strategy**. A run killed mid-flight loses the
turn it was in: the worker that was halfway through its task dies with it, and everything it
had not yet written to disk is gone. Resuming re-does that work from the last artifact that
landed. Every single update cost a turn.

So we stop killing runs.

    1. DRAIN     the server accepts no new runs. Anything the human sends is written to
                 <workspace>/pending.json and acknowledged as saved.
    2. WAIT      until every in-flight run has finished on its own. However long that takes.
    3. STOP      cleanly. The server refuses to go down while anything is running, so this
                 step cannot be the thing that breaks the promise.
    4. MIGRATE   bring the working folder up to the new code (backed up first, as always).
    5. START     and the queued messages are dispatched on the way up.

Nothing is killed. Nothing is lost. It is slower than `kill -9`, and that is the entire point:
the time is spent waiting for work to finish rather than paying to redo it.

`--force` exists for the case where a run is genuinely stuck. It kills, and it says so. It is
the old behaviour, and it is now something you have to ask for by name.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

# FOUR HOURS, and that is deliberate.
#
# The first real ship timed out at 30 minutes and refused — on a perfectly healthy run. A
# supervision run does not drive one column; it drives the card as far down the pipeline as it
# will go, delegating a worker at each step. Preflight → tests → coding is easily an hour of
# honest work. A timeout shorter than the work it is waiting for turns "waits however long it
# takes" into "refuses to ship", which is safe but useless.
#
# The timeout is a backstop against a run that is genuinely wedged, not a patience budget.
DEFAULT_TIMEOUT = 14400
POLL = 3.0


class ShipError(RuntimeError):
    pass


def _api(base: str, path: str, method: str = "GET", payload: dict | None = None, timeout: float = 10.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body else {}


def is_running(base: str) -> bool:
    try:
        _api(base, "/api/config", timeout=3.0)
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def drain_and_stop(
    base: str,
    timeout: float = DEFAULT_TIMEOUT,
    force: bool = False,
    say=print,
) -> bool:
    """Drain the running board and stop it cleanly. True if it stopped; False if nothing was
    running to begin with. Raises ShipError if it will not go quiet and we were not told to
    force it — refusing to ship is the correct outcome there, not shipping anyway."""
    if not is_running(base):
        say("  nothing running — starting clean")
        return False

    state = _api(base, "/api/drain", method="POST")
    n = state["count"]
    say(f"  draining: no new runs will start ({n} still in flight)")
    for r in state["runs"]:
        say(f"    · {r['label']} — running {r['seconds']}s")
    if n:
        say("  waiting for them to FINISH. Nothing is being killed.")

    deadline = time.time() + timeout
    last = -1
    while True:
        state = _api(base, "/api/inflight")
        n = state["count"]
        if n == 0:
            break
        if n != last:
            longest = max((r["seconds"] for r in state["runs"]), default=0)
            say(f"  {n} run(s) still working… (longest: {longest // 60}m)")
            last = n
        if time.time() > deadline:
            names = ", ".join(r["label"] for r in state["runs"])
            if not force:
                # Do NOT ship. An update that kills a live run is exactly the thing we are
                # here to stop doing, and a timeout is not permission to do it anyway.
                _api(base, "/api/undrain", method="POST")
                raise ShipError(
                    f"still running after {int(timeout)}s: {names}\n"
                    "  The board is accepting work again; nothing was shipped and nothing was lost.\n"
                    "  Wait for it to finish and ship again, or re-run with --force to kill it "
                    "(recovery will resume it, but the turn it is in will be lost)."
                )
            say(f"  ! FORCED: killing {n} live run(s): {names}")
            say("  ! recovery will resume them on the way up, but the turn they are in is lost.")
            break
        time.sleep(POLL)

    if state.get("pending"):
        say(f"  {state['pending']} message(s) you sent during the drain are saved for after the restart")

    say("  stopping the board")
    _api(base, "/api/shutdown", method="POST", payload={"force": force})

    for _ in range(60):
        if not is_running(base):
            say("  stopped cleanly")
            return True
        time.sleep(0.5)
    raise ShipError("the board did not stop. Nothing was migrated; it is still serving.")
