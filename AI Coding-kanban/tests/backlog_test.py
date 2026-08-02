"""The backlog, the card type, and the fast lane.

The invariants this file exists to hold:

  1. **Every card is born in the backlog, unrouted.** Which pipeline work goes down is a
     DECISION. It must not be a side effect of which "+" button someone clicked.

  2. **No card leaves the backlog without a type.** Enforced in the Board, not merely asked
     of the manager — an invariant that depends on an LLM remembering it is not an invariant.
     That is why `unset` exists at all: without it, an unclassified card would already be
     silently a `feature` and the classification would be unauditable.

  3. **You cannot un-decide a card.** `unset` is settable by nobody. A type only ever moves
     forward.

  4. **Expedited is fast because it has fewer STEPS, never fewer humans.** Both of its gates
     are real: the plan is approved before code is written, the build before anything ships.

    python tests/backlog_test.py        (no API key, no network)
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager.board import Board  # noqa: E402
from manager.manager import Manager, ManagerConfig  # noqa: E402
from manager.migrations import m0006_backlog_and_expedited as m0006  # noqa: E402
from manager.models import (  # noqa: E402
    BACKLOG,
    BUILD,
    EXPED,
    FEATURE,
    MAINT,
    MAINTENANCE,
    PIPELINES,
    PLAN,
    ROUTABLE,
    UNSET,
    valid_kind,
)
from manager.workspace import Workspace  # noqa: E402

FAILED: list = []
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


def new_board(d=None) -> Board:
    return Board(d or tempfile.mkdtemp(prefix="km-bl-"))


def mgr(b: Board) -> Manager:
    return Manager(b, ManagerConfig(repo_dir=".", data_dir=b.data_dir, mock=True))


def col_of(b, c):
    return b.pipelines.get(c.column)


async def main() -> int:
    page = open(WEB, encoding="utf-8").read()

    # -----------------------------------------------------------------
    section("four pipelines, and the backlog is not one of them")
    check("order is plan → maint → exped → build", PIPELINES == (PLAN, MAINT, EXPED, BUILD))
    check("the backlog is NOT a pipeline", BACKLOG not in PIPELINES)
    check("you may route to plan, maint, exped — never straight to build", ROUTABLE == (PLAN, MAINT, EXPED))

    b = new_board()
    slugs = [c.slug for c in b.pipelines.columns[EXPED]]
    check("expedited is scope → plan → build → shipped", slugs == ["scope", "plan", "build", "shipped"])
    gated = [c.slug for c in b.pipelines.columns[EXPED] if c.gate]
    check("...gated at plan (before a line is written) and build (before it ships)", gated == ["plan", "build"])
    check("every expedited column has a worker", all(os.path.isfile(b.workers.path(EXPED, s)) for s in slugs))

    build_w = b.workers.ensure(b.pipelines.by_slug(EXPED, "build")).instructions
    check("the fast builder must still watch the test FAIL first", "fail" in build_w.lower())
    check("...must run the whole suite", "suite" in build_w.lower())
    check("...must stay inside the plan's file list", "file list" in build_w.lower())
    check("...and must NOT ship anything itself", "do NOT commit" in build_w or "not commit" in build_w.lower())
    ship_w = b.workers.ensure(b.pipelines.by_slug(EXPED, "shipped")).instructions
    check("only the last column commits/pushes/deploys", all(w in ship_w.lower() for w in ("commit", "push", "deploy")))

    # -----------------------------------------------------------------
    section("every card is born in the backlog, unrouted")
    c = await b.add_card("Something someone asked for")
    check("it is in the backlog", c.pipeline == BACKLOG)
    check("it has no column", c.column == "")
    check("it has NO TYPE yet", c.kind == UNSET)
    check("it still gets a folder", os.path.isdir(b.abs_dir(c)) and "backlog" in c.dir)

    typed = await b.add_card("Add flight search", kind=FEATURE)
    check("a type can be given at creation", typed.kind == FEATURE)
    check("...and it still waits in the backlog", typed.pipeline == BACKLOG)

    # -----------------------------------------------------------------
    section("NO CARD LEAVES THE BACKLOG UNTYPED — enforced, not merely asked")
    check("routing an untyped card is refused", await b.route_card(c.id, EXPED) is None)
    check("...it did not move", b.cards[c.id].pipeline == BACKLOG)
    check("the board can list what it is owed", c.id in b.untyped_cards())

    await mgr(b).triage_card(c.id)
    check("triage types it", b.cards[c.id].kind in (FEATURE, MAINTENANCE))
    check("...and does NOT route it — the human says when to start", b.cards[c.id].pipeline == BACKLOG)
    check("nothing is owed any more", b.untyped_cards() == [])

    # -----------------------------------------------------------------
    section("a type is a one-way door")
    check("'unset' is not a settable type", valid_kind(UNSET) is None)
    check("...so nothing can un-decide a card", await b.set_card_kind(typed.id, UNSET) is None)
    check("...and it is still a feature", b.cards[typed.id].kind == FEATURE)

    # -----------------------------------------------------------------
    section("routing: the human's word is final, otherwise the manager chooses")
    m = mgr(b)
    await m.handle_card_message(typed.id, "build this expedited")
    t = b.cards[typed.id]
    check("'build this expedited' → the fast lane, no argument", t.pipeline == EXPED)
    check("...into its FIRST column", col_of(b, t).slug == "scope")
    check("...and it keeps its type", t.kind == FEATURE)
    check("...and its folder followed it", "exped" in t.dir and os.path.isdir(b.abs_dir(t)))

    bug = await b.add_card("Event creation is broken", kind=MAINTENANCE)
    await m.handle_card_message(bug.id, "start working on this")
    check("'start working on this' → the manager routed it himself", b.cards[bug.id].pipeline in ROUTABLE)
    check("...and it is still a maintenance card", b.cards[bug.id].kind == MAINTENANCE)

    check("a card sitting in the backlog does nothing until told", (await b.add_card("Later")).pipeline == BACKLOG)

    # -----------------------------------------------------------------
    section("a card can be pulled back out of a pipeline")
    back = await b.send_to_backlog(typed.id)
    check("it returns to the backlog", back.pipeline == BACKLOG and back.column == "")
    check("...keeping its type", back.kind == FEATURE)
    check("...and its folder moved back", "backlog" in back.dir and os.path.isdir(b.abs_dir(back)))
    check("it survives a reload there", new_board(b.data_dir).cards[typed.id].pipeline == BACKLOG)

    # -----------------------------------------------------------------
    section("a card whose column vanishes falls back to the BACKLOG, not a guessed column")
    b2 = new_board()
    orphan = await b2.add_card("Orphan me", kind=FEATURE)
    await b2.route_card(orphan.id, PLAN)
    check("it is in plan", b2.cards[orphan.id].pipeline == PLAN)

    raw = json.load(open(b2.path))
    for card in raw["cards"]:
        if card["id"] == orphan.id:
            card["column"] = "a-column-that-no-longer-exists"
    json.dump(raw, open(b2.path, "w"), indent=2)

    reloaded = new_board(b2.data_dir)
    check("it lands in the backlog, not somebody's inbox", reloaded.cards[orphan.id].pipeline == BACKLOG)
    check("...still a feature", reloaded.cards[orphan.id].kind == FEATURE)

    # -----------------------------------------------------------------
    section("the migration adds the fast lane and touches nothing else")
    ws = Workspace(tempfile.mkdtemp(prefix="km-ws-"), tempfile.mkdtemp(prefix="repo-"))
    ws.ensure()
    bb = Board(ws.path)
    keep = await bb.add_card("Do not lose me", kind=FEATURE)
    await bb.route_card(keep.id, PLAN)
    del bb

    cfg = os.path.join(ws.path, "pipelines.json")
    raw = json.load(open(cfg))
    raw.pop(EXPED, None)
    (raw.get("colors") or {}).pop(EXPED, None)
    json.dump(raw, open(cfg, "w"), indent=2)

    notes = m0006.migrate(ws)
    after = json.load(open(cfg))
    check("it reports what it did", any("Expedited" in n for n in notes))
    check("expedited is there", [x["slug"] for x in after[EXPED]] == ["scope", "plan", "build", "shipped"])
    check("...with both gates", [x["slug"] for x in after[EXPED] if x["gate"]] == ["plan", "build"])
    check("...and a colour", after["colors"].get(EXPED))
    check("the other pipelines survived", len(after[PLAN]) == 6 and len(after[MAINT]) == 5)
    cards = json.load(open(ws.board_path))["cards"]
    check("the card survived, where it was", len(cards) == 1 and cards[0]["pipeline"] == PLAN)
    check("running it twice does nothing", m0006.migrate(ws) == [])

    # -----------------------------------------------------------------
    section("the UI creates only in the backlog")
    check("the backlog renders above the pipelines", "function backlogEl" in page and "root.appendChild(backlogEl())" in page)
    check("cards are created there, with an optional type", "function newCard" in page and "new feature" in page)
    check("the '+ card' buttons inside pipelines are gone", "'+ New idea'" not in page and "'+ New report'" not in page)
    check("an untyped card is grey and says so", "needs a type" in page and "--untyped" in page)
    check("you can drag a card back out of a pipeline", "type:'backlog',card_id:id" in page)

    print(f"\n{'=' * 70}")
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("backlog + expedited: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
