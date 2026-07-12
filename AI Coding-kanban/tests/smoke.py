"""Headless end-to-end test of the board + mock manager pipeline.

Drives a card through every column and both human gates without an API key.
Run from the feature-folder root:  python tests/smoke.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager.board import Board          # noqa: E402
from manager.manager import Manager, ManagerConfig  # noqa: E402
from manager.models import Column         # noqa: E402


async def main() -> int:
    repo = tempfile.mkdtemp(prefix="fake-repo-")
    data = tempfile.mkdtemp(prefix="km-data-")
    events: list[dict] = []

    async def broadcaster(msg):
        events.append(msg)

    board = Board(data, broadcaster=broadcaster)
    cfg = ManagerConfig(repo_dir=repo, data_dir=data, mock=True)
    mgr = Manager(board, cfg)

    ok = True

    def check(label, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

    card = await board.add_card("Add password reset flow", "users forget passwords")
    cid = card.id
    check("card starts in ideas", board.cards[cid].column == Column.IDEAS.value)

    await mgr.handle_user_message(cid, "start")
    c = board.cards[cid]
    check("after 'start' -> plans_ready", c.column == Column.PLANS_READY.value)
    check("plan-approval gate active", c.gate == "plan_approval")
    check("SCOPE.md artifact recorded", "SCOPE.md" in c.artifacts)
    check("PLAN.md artifact recorded", "PLAN.md" in c.artifacts)
    check("SCOPE.md written to disk",
          os.path.exists(os.path.join(data, "cards", cid, "SCOPE.md")))

    # a message that is NOT approval must not advance past the gate
    await mgr.handle_user_message(cid, "what does the plan cover?")
    check("non-approval keeps card at gate", board.cards[cid].column == Column.PLANS_READY.value)

    await mgr.handle_user_message(cid, "approve")
    c = board.cards[cid]
    check("after 'approve' -> build_review", c.column == Column.BUILD_REVIEW.value)
    check("ship gate active", c.gate == "ship_approval")

    await mgr.handle_user_message(cid, "ship it")
    c = board.cards[cid]
    check("after 'ship it' -> shipped", c.column == Column.SHIPPED.value)
    check("no gate after shipped", c.gate is None)

    # persistence: reload board from disk, state survives
    board2 = Board(data)
    check("state persisted across reload", board2.cards[cid].column == Column.SHIPPED.value)
    check("thread persisted", len(board2.cards[cid].thread) > 0)

    check("board events were broadcast", len(events) > 5)

    print(f"\n{'ALL PASSED' if ok else 'FAILURES PRESENT'} — {len(events)} events emitted")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
