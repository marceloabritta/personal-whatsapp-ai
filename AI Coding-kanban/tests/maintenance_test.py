"""The maintenance pipeline, and the card `kind` that survives the trip into build.

The load-bearing claim, and the only reason `kind` is a stored field rather than something
derived from `card.pipeline`: **once a fix is in the build pipeline, the pipeline no longer
tells you it is a fix.** If kind were derived, a maintenance card would turn into a feature
the moment it was promoted — which is exactly the distinction the human asked to keep.

    python tests/maintenance_test.py        (no API key, no network)
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager import migrations  # noqa: E402
from manager.board import Board  # noqa: E402
from manager.migrations import m0003_maintenance_pipeline as m0003  # noqa: E402
from manager.models import BUILD, FEATURE, MAINT, MAINTENANCE, PIPELINES, PLAN  # noqa: E402
from manager.pipelines import DEFAULT_COLUMNS  # noqa: E402
from manager.workspace import Workspace  # noqa: E402

FAILED: list = []


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


def new_board() -> Board:
    return Board(tempfile.mkdtemp(prefix="km-maint-"))


async def main() -> int:
    # -----------------------------------------------------------------
    section("the board has three pipelines, maintenance between plan and build")
    check("order is plan → maint → build", PIPELINES == (PLAN, MAINT, BUILD))

    b = new_board()
    slugs = [c.slug for c in b.pipelines.columns[MAINT]]
    check(
        "the columns are report → replication → exploring → plan-fix → plan-ready-to-build",
        slugs == ["report", "replication", "exploring", "plan-fix", "plan-ready-to-build"],
    )
    check("the last one is a GATE", b.pipelines.columns[MAINT][-1].gate is True)
    check(
        "every maintenance column got a worker file",
        all(os.path.isfile(b.workers.path(MAINT, s)) for s in slugs),
    )
    check(
        "the replication worker demands a reproduction",
        "Reproduced?" in b.workers.ensure(b.pipelines.by_slug(MAINT, "replication")).instructions,
    )
    check(
        "plan-fix writes PLAN.md — the artifact the BUILD pipeline already demands",
        "PLAN.md" in b.workers.ensure(b.pipelines.by_slug(MAINT, "plan-fix")).instructions,
    )

    # -----------------------------------------------------------------
    section("a card is born with the kind of the pipeline it is filed in")
    feature = await b.add_card("Add flight search")
    fix = await b.add_card("Calendar replies twice", pipeline=MAINT)

    check("a plan card is a feature", feature.kind == FEATURE)
    check("a maint card is maintenance", fix.kind == MAINTENANCE)
    check("and it starts in Report", b.pipelines.get(fix.column).slug == "report")
    check("a card cannot be born in build", (await b.add_card("x", pipeline=BUILD)).pipeline == PLAN)

    # -----------------------------------------------------------------
    section("THE POINT: the kind survives promotion into build")
    for col in b.pipelines.columns[MAINT][1:]:
        await b.move_card(fix.id, col.id)
    check("the fix reached the maintenance gate", b.cards[fix.id].column == b.pipelines.last(MAINT).id)

    promoted = await b.promote_to_build(fix.id)
    check("a maintenance card CAN be promoted to build", promoted is not None)
    check("it is in the build pipeline", b.cards[fix.id].pipeline == BUILD)
    check("it is in build's FIRST column", b.cards[fix.id].column == b.pipelines.first(BUILD).id)
    check("...and it is STILL a maintenance card", b.cards[fix.id].kind == MAINTENANCE)

    await b.move_card(feature.id, b.pipelines.last(PLAN).id)
    await b.promote_to_build(feature.id)
    check("a plan card still promotes too", b.cards[feature.id].pipeline == BUILD)
    check("...and is still a feature", b.cards[feature.id].kind == FEATURE)

    check(
        "so the UI can tell them apart in the SAME build column",
        b.cards[fix.id].column == b.cards[feature.id].column
        and b.cards[fix.id].kind != b.cards[feature.id].kind,
    )
    check(
        "and the kind is on the wire for the UI to paint",
        {c["id"]: c["kind"] for c in b.snapshot()["cards"]}[fix.id] == MAINTENANCE,
    )

    # -----------------------------------------------------------------
    section("dragging a card between origins re-labels it; build never does")
    await b.move_card(feature.id, b.pipelines.first(MAINT).id)
    check("dragged into maintenance → becomes a fix", b.cards[feature.id].kind == MAINTENANCE)
    await b.move_card(feature.id, b.pipelines.first(PLAN).id)
    check("dragged back into plan → becomes a feature", b.cards[feature.id].kind == FEATURE)

    # -----------------------------------------------------------------
    section("the kind is the HUMAN's field: settable, and it stays set")
    idea = await b.add_card("Actually this is a bug")
    check("it starts as a feature", idea.kind == FEATURE)

    await b.set_card_kind(idea.id, MAINTENANCE)
    check("the human can flip it", b.cards[idea.id].kind == MAINTENANCE)
    check("flipping it does NOT move the card", b.cards[idea.id].pipeline == PLAN)

    # The regression this guards: kind used to be re-derived on every move, so the very next
    # column advance silently undid the human. It must not.
    plan_cols = b.pipelines.columns[PLAN]
    await b.move_card(idea.id, plan_cols[1].id)
    check("it SURVIVES the next move within the pipeline", b.cards[idea.id].kind == MAINTENANCE)
    await b.move_card(idea.id, plan_cols[2].id)
    check("...and the one after that", b.cards[idea.id].kind == MAINTENANCE)

    check("junk is refused", await b.set_card_kind(idea.id, "banana") is None)
    check("...and the kind is unharmed", b.cards[idea.id].kind == MAINTENANCE)
    await b.set_card_kind(idea.id, FEATURE)
    check("it flips back", b.cards[idea.id].kind == FEATURE)

    # -----------------------------------------------------------------
    section("it survives a reload — kind is persisted, not in-memory")
    b2 = Board(b.data_dir)
    check("the fix is still maintenance after reload", b2.cards[fix.id].kind == MAINTENANCE)
    check("and still in build", b2.cards[fix.id].pipeline == BUILD)

    # -----------------------------------------------------------------
    section("the migration brings an OLD folder forward without losing a card")
    ws = Workspace(tempfile.mkdtemp(prefix="km-ws-"), tempfile.mkdtemp(prefix="repo-"))
    ws.ensure()
    old = Board(ws.path)
    a = await old.add_card("An old feature")
    del old

    # Rewind the folder to how it looked before this migration: no maint, no kinds.
    cfg = os.path.join(ws.path, "pipelines.json")
    raw = json.load(open(cfg))
    raw.pop(MAINT, None)
    json.dump(raw, open(cfg, "w"), indent=2)
    board_raw = json.load(open(ws.board_path))
    for c in board_raw["cards"]:
        c.pop("kind", None)
    json.dump(board_raw, open(ws.board_path, "w"), indent=2)

    notes = m0003.migrate(ws)
    check("it says it seeded the pipeline", any("maintenance pipeline" in n for n in notes))
    check("it says it stamped the cards", any("feature" in n for n in notes))

    cfg_after = json.load(open(cfg))
    check("pipelines.json now has all three", [p for p in PIPELINES if cfg_after.get(p)] == list(PIPELINES))
    check(
        "with the right maintenance columns",
        [c["slug"] for c in cfg_after[MAINT]] == [s for _t, s, _g in DEFAULT_COLUMNS[MAINT]],
    )
    cards_after = json.load(open(ws.board_path))["cards"]
    check("the old card survived", len(cards_after) == 1 and cards_after[0]["id"] == a.id)
    check("and is now explicitly a feature", cards_after[0]["kind"] == FEATURE)

    check("running it again is a no-op", m0003.migrate(ws) == [])

    # a human who reordered/renamed the maintenance columns must not have them reset
    cfg_after[MAINT] = [cfg_after[MAINT][0]]
    cfg_after[MAINT][0]["title"] = "Inbox"
    json.dump(cfg_after, open(cfg, "w"), indent=2)
    m0003.migrate(ws)
    check(
        "it never overwrites columns the human has changed",
        [c["title"] for c in json.load(open(cfg))[MAINT]] == ["Inbox"],
    )

    # -----------------------------------------------------------------
    section("a fresh folder is born current, with all three")
    ws2 = Workspace(tempfile.mkdtemp(prefix="km-ws2-"), tempfile.mkdtemp(prefix="repo2-"))
    ws2.ensure()
    check("nothing pending", migrations.pending(ws2) == [])
    b3 = Board(ws2.path)
    check("maintenance is there from birth", len(b3.pipelines.columns[MAINT]) == 5)

    print(f"\n{'=' * 70}")
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("maintenance: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
