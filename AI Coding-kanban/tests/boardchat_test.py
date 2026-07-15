"""The manager's OWN chat — the board-level conversation — must actually run in LIVE mode.

This is a regression test for a real incident: sending a message to the manager's exclusive
chat came back "⚠️ manager error: 'Manager' object has no attribute '_real_board'". The live
dispatch in `handle_board_message` called `self._real_board(...)`, and that method did not
exist — the whole board-chat path was dead in LIVE mode. Every other suite runs in MOCK mode,
so nothing exercised it and nothing caught it.

So this runs the manager with `mock=False` and stubs ONLY the SDK stream. Everything the bug
lived in — the dispatch, the board prompt, the tool server, the option wiring — runs for real.

    python tests/boardchat_test.py        (no API key, no network)
"""
import asyncio
import inspect
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager.board import Board  # noqa: E402
from manager.manager import Manager, ManagerConfig  # noqa: E402

FAILED: list = []


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


async def main() -> int:
    b = Board(tempfile.mkdtemp(prefix="km-bc-"))
    # mock=False: we want the REAL board-chat path, which is where the bug was. Only the SDK
    # turn itself is stubbed — the method, its prompt and its tools are all built for real.
    m = Manager(b, ManagerConfig(repo_dir=".", data_dir=b.data_dir, mock=False))
    mgr_id = next(iter(b.managers))

    async def fake_pump(stream, on_session, on_text, on_system, on_worker,
                        on_activity=None, on_working=None):
        # Stand in for the model: hand back a session id and one line of prose, exactly as the
        # real _pump would when the SDK streams a reply. The stream itself is never iterated,
        # so nothing connects to the network.
        await on_session("sess-board-xyz")
        await on_text("Here is your board status: nothing is stuck.")

    m._pump = fake_pump

    section("a message to the manager's own chat is answered in LIVE mode")
    await m.handle_board_message(mgr_id, "give me a status of the board")

    thread = [(msg.role, msg.text) for msg in b.managers[mgr_id].thread]
    check("my message is on the thread", any(r == "user" and "status" in t for r, t in thread))
    check("the manager REPLIED — the real board path ran",
          any(r == "manager" and "board status" in t for r, t in thread))
    check("no '_real_board' AttributeError was posted",
          not any("_real_board" in t for _, t in thread))
    check("...and no 'manager error' of any kind",
          not any(r == "system" and "manager error" in t.lower() for r, t in thread))
    check("his session id was persisted", b.managers[mgr_id].session_id == "sess-board-xyz")
    check("he is not left busy afterwards", b.managers[mgr_id].busy is False)

    section("every mock/real dispatch pair is fully wired")
    # The bug was a dispatch to a method that did not exist. Assert that can't happen again:
    # for every `self._real_X(` / `self._mock_X(` call in the class, the method exists.
    src = inspect.getsource(Manager)
    called = set(re.findall(r"self\.(_real_\w+|_mock_\w+)\(", src))
    for name in sorted(called):
        check(f"{name} exists (called at least once)", hasattr(Manager, name))

    print(f"\n{'ALL PASSED' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
