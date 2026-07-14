"""Headless end-to-end test of the board, the folder tree, and the mock pipeline.

Drives a card through both pipelines and both human gates, then exercises the trash
and the column editor — all without an API key.

Run from the feature-folder root:  python tests/smoke.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager.board import Board                        # noqa: E402
from manager.manager import Manager, ManagerConfig     # noqa: E402
from manager.models import BACKLOG, BUILD, EXPED, MAINT, PLAN, FEATURE  # noqa: E402

FAILED: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name: str) -> None:
    print(f"\n{name}")


async def main() -> int:
    repo = tempfile.mkdtemp(prefix="fake-repo-")
    data = tempfile.mkdtemp(prefix="km-data-")
    workers = tempfile.mkdtemp(prefix="km-workers-")
    events: list[dict] = []

    async def broadcaster(msg):
        events.append(msg)

    board = Board(data, workers_dir=workers, broadcaster=broadcaster)
    mgr = Manager(board, ManagerConfig(repo_dir=repo, data_dir=data, mock=True))
    pl = board.pipelines

    # ---------------------------------------------------------------
    section("setup")
    check("four pipelines exist", [p["id"] for p in pl.snapshot()] == [PLAN, MAINT, EXPED, BUILD])
    check("plan pipeline has columns", len(pl.columns[PLAN]) == 6)
    check("maintenance pipeline has columns", len(pl.columns[MAINT]) == 5)
    check("expedited pipeline has columns", len(pl.columns[EXPED]) == 4)
    check("build pipeline has columns", len(pl.columns[BUILD]) == 5)
    check("a default manager exists", len(board.managers) == 1)
    check(
        "every column has a worker file on disk",
        all(os.path.exists(board.workers.path(c.pipeline, c.slug)) for c in pl.all_columns()),
    )
    plan_ready = pl.by_slug(PLAN, "plan-ready")
    build_review = pl.by_slug(BUILD, "build-review")
    check("plan-ready is a gate", plan_ready.gate)
    check("build-review is a gate", build_review.gate)

    w = board.workers.ensure(pl.by_slug(PLAN, "scoping"))
    c = w.contract()
    check("a worker's contract has entry criteria", bool(c["entry"]))
    check("a worker's contract has exit criteria", bool(c["exit"]))
    check("worker agent name is namespaced", w.agent_name == "plan__scoping")

    # ---------------------------------------------------------------
    section("a card, and its folder")
    # Every card is born in the BACKLOG now — unrouted, and (unless told) untyped.
    card = await board.add_card("Add password reset flow", "users forget passwords", kind=FEATURE)
    cid = card.id
    check("card starts in the BACKLOG, not a pipeline", board.cards[cid].pipeline == BACKLOG)
    check("...with no column", board.cards[cid].column == "")
    check("card is assigned to a manager", bool(board.cards[cid].manager_id))
    check(
        "folder is under the backlog",
        board.cards[cid].dir == os.path.join("cards", "backlog", f"{cid}-add-password-reset-flow"),
    )
    await board.route_card(cid, PLAN)
    check("routing puts it in the plan inbox", board.cards[cid].column == pl.first(PLAN).id)
    check(
        "...and the folder follows it",
        board.cards[cid].dir == os.path.join("cards", "plan", "ideas", f"{cid}-add-password-reset-flow"),
    )
    check("folder exists on disk", os.path.isdir(board.abs_dir(board.cards[cid])))

    # ---------------------------------------------------------------
    section("plan pipeline -> gate")
    await mgr.handle_card_message(cid, "start")
    c = board.cards[cid]
    check("card walked to the plan-ready gate", c.column == plan_ready.id)
    check("card is still in the plan pipeline", c.pipeline == PLAN)
    check("folder moved with the card", c.dir.startswith(os.path.join("cards", "plan", "plan-ready")))
    files = sorted(os.listdir(board.abs_dir(c)))
    check("folder accumulated every column's output", files == ["IDEA.md", "PLAN.md", "PLAN_REVIEW.md", "SCOPE.md", "SCOPE_REVIEW.md"])
    check("artifacts recorded on the card", "PLAN.md" in c.artifacts)
    check("the worker's report reached the thread", any(m.role == "worker" for m in c.thread))

    await mgr.handle_card_message(cid, "what does the plan cover?")
    check("a non-approval does not cross the gate", board.cards[cid].column == plan_ready.id)

    # ---------------------------------------------------------------
    section("gate -> build pipeline")
    await mgr.handle_card_message(cid, "approve")
    c = board.cards[cid]
    check("approval promoted the card into build", c.pipeline == BUILD)
    check("card walked to the build-review gate", c.column == build_review.id)
    check("folder followed into the build tree", c.dir.startswith(os.path.join("cards", "build", "build-review")))
    files = sorted(os.listdir(board.abs_dir(c)))
    check("plan artifacts travelled with the card", "PLAN.md" in files and "SCOPE.md" in files)
    check("build artifacts were added", "PREFLIGHT.md" in files and "BUILD.md" in files)

    await mgr.handle_card_message(cid, "ship it")
    c = board.cards[cid]
    check("shipping reached the last column", c.column == pl.last(BUILD).id)
    check("SHIPPED.md was written", os.path.exists(os.path.join(board.abs_dir(c), "SHIPPED.md")))

    # ---------------------------------------------------------------
    section("trash")
    trashed_dir = board.abs_dir(board.cards[cid])
    await board.trash_card(cid)
    c = board.cards[cid]
    check("card is trashed", c.trashed)
    check("card left the board snapshot", cid not in [x["id"] for x in board.snapshot()["cards"]])
    check("folder moved into the trash", c.dir.startswith(os.path.join("cards", "trash")))
    check("folder still exists (nothing destroyed)", os.path.isdir(board.abs_dir(c)))
    check("old location is gone", not os.path.isdir(trashed_dir))
    check("SHIPPED.md survived the trip", os.path.exists(os.path.join(board.abs_dir(c), "SHIPPED.md")))

    await board.restore_card(cid)
    c = board.cards[cid]
    check("restore puts the card back on the board", not c.trashed)
    check("restore returns it to the column it left", c.column == pl.last(BUILD).id)
    check("restore returns the folder too", os.path.isdir(board.abs_dir(c)))

    # ---------------------------------------------------------------
    section("editing the columns")
    col = await board.add_column(
        PLAN, "Research", index=1, entry="IDEA.md exists.", exit_="RESEARCH.md names 3 competitors."
    )
    check("column was inserted at the right position", pl.columns[PLAN][1].id == col.id)
    check("column folder was created", os.path.isdir(os.path.join(data, "cards", "plan", "research")))
    wpath = board.workers.path(PLAN, "research")
    check("a worker file was scaffolded for it", os.path.exists(wpath))
    rw = board.workers.ensure(col)
    check("the entry criteria I typed are in the worker", "IDEA.md exists." in rw.contract()["entry"])
    check("the exit criteria I typed are in the worker", "3 competitors" in rw.contract()["exit"])

    card2 = await board.add_card("A second idea")
    await board.move_card(card2.id, col.id)
    check("a card can sit in the new column", board.cards[card2.id].column == col.id)

    await board.rename_column(col.id, "Market Research")
    c2 = board.cards[card2.id]
    check("rename moved the card's folder", c2.dir.startswith(os.path.join("cards", "plan", "market-research")))
    check("rename moved the card's folder on disk", os.path.isdir(board.abs_dir(c2)))
    check("rename moved the worker file", os.path.exists(board.workers.path(PLAN, "market-research")))
    check("the old worker file is gone", not os.path.exists(wpath))

    _, moved = await board.delete_column(col.id)
    check("deleting a column relocates its cards", moved == 1)
    check("the stranded card fell back a column", board.cards[card2.id].column == pl.first(PLAN).id)
    check("the stranded card's folder followed", os.path.isdir(board.abs_dir(board.cards[card2.id])))
    check("the worker file was archived, not destroyed",
          os.path.exists(os.path.join(workers, "_deleted", "plan-market-research.md")))

    # ---------------------------------------------------------------
    section("managers")
    m2 = await board.add_manager("Ops", "🛠")
    await board.assign_card(card2.id, m2.id)
    check("a second manager can be added", len(board.managers) == 2)
    check("a card can be reassigned", board.cards[card2.id].manager_id == m2.id)
    await mgr.handle_board_message(m2.id, "what's on the board?")
    check("the board-level chat has its own thread", len(board.managers[m2.id].thread) >= 2)
    check("the board chat did not touch the card threads",
          all(m.role != "user" or m.text != "what's on the board?" for m in board.cards[card2.id].thread))

    # ---------------------------------------------------------------
    section("persistence")
    board2 = Board(data, workers_dir=workers)
    check("cards survive a reload", board2.cards[cid].column == pl.last(BUILD).id)
    check("threads survive a reload", len(board2.cards[cid].thread) > 0)
    check("managers survive a reload", len(board2.managers) == 2)
    check("the edited pipeline survives a reload", len(board2.pipelines.columns[PLAN]) == 6)
    check("board events were broadcast", len(events) > 10)

    print(f"\n{'ALL PASSED' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}")
    print(f"{len(events)} events emitted")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
