"""Every test, in order of how much they cost to run. No API key needed by any of them.

    python tests/run_all.py

    smoke        the board, the folder tree, the mock pipeline, the trash, the columns
    update       migrations, backups, the three-way prompt merge
    policy       the human's standing orders reach the manager and outrank the system's
    maintenance  the third pipeline, and the card kind that survives promotion into build
    colors       pipeline colours, and the card that keeps its own into build
    workerchat   the per-worker conversation: its own session, journalled, rename-safe
    backlog      the backlog, the card type, and the expedited fast lane
    decouple     the manager goes IDLE while a worker runs — and keeps the card's context
    boardchat    the manager's own chat runs in LIVE mode (regression: missing _real_board)
    pause        stop everything safely — and stay stopped, across a kill. Shipping calls this
    ship         pause → wait → stop → migrate → start, WITHOUT killing live work
    restart      kill -9 the real server mid-run and start it again
    recovery     the incident's acceptance test: killed mid-run, it resumes ITSELF
    ui           drive the real page in headless Chrome  (skipped if Chrome isn't there)
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYBIN = os.path.join(os.path.dirname(HERE), ".venv", "bin", "python")

SUITES = [
    "smoke.py",
    "update_test.py",
    "policy_test.py",
    "maintenance_test.py",
    "colors_test.py",
    "workerchat_test.py",
    "backlog_test.py",
    "decouple_test.py",
    "boardchat_test.py",
    "pause_test.py",
    "ship_test.py",
    "restart_test.py",
    "recovery_test.py",
    "ui_test.py",
]


def main() -> int:
    py = PYBIN if os.path.exists(PYBIN) else sys.executable
    results = {}
    for suite in SUITES:
        print(f"\n{'=' * 70}\n{suite}\n{'=' * 70}")
        code = subprocess.run([py, os.path.join(HERE, suite)], cwd=os.path.dirname(HERE)).returncode
        results[suite] = code

    print(f"\n{'=' * 70}")
    for suite, code in results.items():
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {suite}")
    failed = [s for s, c in results.items() if c != 0]
    print(f"\n{'ALL SUITES PASSED' if not failed else 'FAILED: ' + ', '.join(failed)}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
