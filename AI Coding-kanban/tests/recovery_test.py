"""The acceptance test from docs/INCIDENT-process-death-and-resume.md §5.

    1. Start the server; send a card a message that triggers a worker delegation.
    2. While the worker is running, kill -9 the server.
    3. DO NOTHING.
    4. On restart, the card thread shows the interruption note, the manager re-enters its
       session, inspects the card folder, and picks up from what actually reached disk.
    5. The card reaches its column's exit criteria without a human touching anything.

Step 3 is the whole test. The old behaviour was: the card spins "working" forever, the work
is gone, and no restart can recover it because nothing on disk ever recorded that a run
began. (launchd does step 4's restart in real life; here the test process does it, which
tests the part that is ours.)

    python tests/recovery_test.py       (mock mode; no API key, no network)
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from manager.journal import MAX_ATTEMPTS, Journal  # noqa: E402
from tests.restart_test import Server, check, free_port, get, post, section, send_ws_sync  # noqa: E402


def wait_for(fn, timeout=25.0, every=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(every)
    return None


def main() -> int:
    from tests.restart_test import FAILED

    ws_dir = tempfile.mkdtemp(prefix="km-recover-ws-")
    repo = tempfile.mkdtemp(prefix="km-recover-repo-")
    port = free_port()

    # -----------------------------------------------------------------
    section("1-2. a run is dispatched, and the process is killed mid-flight")
    srv = Server(ws_dir, repo, port)
    srv.start()
    cid = post(port, "/api/card", {"title": "Resume me", "description": "without being asked",
                                   "pipeline": "plan", "kind": "feature"})["id"]
    send_ws_sync(port, {"type": "message", "card_id": cid, "text": "start"})
    time.sleep(1.4)  # the mock manager pauses at each column, so this lands mid-run

    mid = get(port, f"/api/card/{cid}")
    check("the manager is mid-run", mid["busy"] is True)

    inflight = json.load(open(os.path.join(ws_dir, "inflight.json")))["runs"]
    check("the run was written down BEFORE it started", len(inflight) == 1)
    check("...and it knows which card", inflight[0]["target_id"] == cid)
    check("...and what the human actually asked for", inflight[0]["text"] == "start")
    check("...and which session to re-enter", "session_id" in inflight[0])

    srv.kill_hard()
    check("the process is gone", srv.proc.poll() is not None)
    check(
        "the journal entry SURVIVED the kill — this is the recovery ticket",
        len(json.load(open(os.path.join(ws_dir, "inflight.json")))["runs"]) == 1,
    )
    board_on_disk = json.load(open(os.path.join(ws_dir, "board.json")))
    check(
        "busy=true is still on disk (the evidence was NOT erased)",
        board_on_disk["cards"][0]["busy"] is True,
    )

    # -----------------------------------------------------------------
    section("3-5. restart, and DO NOTHING")
    srv2 = Server(ws_dir, repo, port)
    srv2.start()

    card = get(port, f"/api/card/{cid}")
    texts = [m["text"] for m in card["thread"]]
    check(
        "the card says it was interrupted",
        any("killed while working" in t or "cut off" in t for t in texts),
    )
    check(
        "the resume prompt was NOT put in the thread as if the human typed it",
        not any(m["role"] == "user" and "AUTOMATIC RESUME" in m["text"] for m in card["thread"]),
    )

    # Nobody sends anything. The board must finish the work on its own.
    done = wait_for(lambda: (lambda c: c if (not c["busy"] and c["gate"]) else None)(get(port, f"/api/card/{cid}")))
    check("the manager resumed on its own and worked the card to the gate", done is not None)
    if done:
        check(
            "the column's artifacts are on disk",
            os.path.isfile(os.path.join(done["abs_dir"], "SCOPE.md"))
            and os.path.isfile(os.path.join(done["abs_dir"], "PLAN.md")),
        )
        check("no human message was needed", not any(
            m["role"] == "user" and m["text"] != "start" for m in done["thread"]
        ))

    check(
        "the journal is empty again once the run completed",
        json.load(open(os.path.join(ws_dir, "inflight.json")))["runs"] == [],
    )
    check("the card is no longer busy", get(port, f"/api/card/{cid}")["busy"] is False)

    section("a log file exists, so the next incident is not transcript archaeology")
    logfile = os.path.join(ws_dir, "logs", "manager.log")
    check("there is a log file", os.path.isfile(logfile))
    check("and the recovery is IN it", "recovery:" in open(logfile).read())

    srv2.kill_hard()

    # -----------------------------------------------------------------
    section("a run that keeps killing the process is NOT retried forever")
    # Forge a journal entry that has already burned through its attempts, as a crash-loop
    # would produce: without a cap, launchd + a poisoned run = an infinite restart cycle.
    j = Journal(ws_dir)
    run = j.start("card", cid, "a message that kills the process", session_id="s1")
    for _ in range(MAX_ATTEMPTS):
        j.bump(run)
    check("it is marked exhausted", j.is_exhausted(run))

    srv3 = Server(ws_dir, repo, port)
    srv3.start()
    thread = get(port, f"/api/card/{cid}")["thread"]
    check(
        "the board gives up and says so, instead of looping",
        any("not resuming it again" in m["text"] for m in thread),
    )
    check("it stops spinning", get(port, f"/api/card/{cid}")["busy"] is False)
    check(
        "and the poisoned entry is retired",
        json.load(open(os.path.join(ws_dir, "inflight.json")))["runs"] == [],
    )
    srv3.kill_hard()

    print(f"\n{'ALL PASSED' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
