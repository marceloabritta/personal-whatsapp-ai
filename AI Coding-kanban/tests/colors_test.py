"""Pipeline colours, and the two rules that make the board readable at a glance.

  1. **A card is painted from the pipeline it BELONGS to, not the one it sits in.** This is
     the rule that survives promotion: a fix is yellow in the red build pipeline, so the
     build columns still tell you what is new work and what is a repair. If this ever
     regresses to "colour by current pipeline", every card in build turns red and the
     distinction the human asked for is gone.

  2. **The colour is state.** It lives in the working folder, it is editable per pipeline,
     and an update never repaints a board someone has recoloured.

    python tests/colors_test.py        (no API key, no network)
"""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager.board import Board  # noqa: E402
from manager.migrations import m0004_pipeline_colors as m0004  # noqa: E402
from manager.models import (  # noqa: E402
    BUILD,
    DEFAULT_PIPELINE_COLORS,
    EXPED,
    MAINT,
    PIPELINE_TITLES,
    PLAN,
)
from manager.pipelines import valid_color  # noqa: E402
from manager.workspace import Workspace  # noqa: E402

FAILED: list = []
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


async def main() -> int:
    page = open(WEB, encoding="utf-8").read()

    # -----------------------------------------------------------------
    section("the pipelines are named and painted as asked")
    check("plan is the NEW FEATURE plan pipeline", PIPELINE_TITLES[PLAN] == "New Feature Plan")
    check("maintenance keeps its name", PIPELINE_TITLES[MAINT] == "Maintenance")

    def hue(p):
        c = DEFAULT_PIPELINE_COLORS[p].lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    pr, pg, pb = hue(PLAN)
    mr, mg, mb = hue(MAINT)
    br, bg, bb = hue(BUILD)
    check("new-feature plan is GREEN (green channel dominates)", pg > pr and pg > pb)
    check("maintenance is YELLOW (red+green over blue)", mr > mb and mg > mb)
    check("build is RED (red channel dominates)", br > bg and br > bb)

    b = Board(tempfile.mkdtemp(prefix="km-color-"))
    snap = {p["id"]: p for p in b.snapshot()["pipelines"]}
    check("every pipeline ships its colour to the UI", all(snap[p]["color"] for p in snap))
    check("...and its title", snap[PLAN]["title"] == "New Feature Plan")

    # -----------------------------------------------------------------
    section("a colour is validated before it reaches the page's CSS")
    check("#rrggbb is a colour", valid_color("#1d3b2a") == "#1d3b2a")
    check("#rgb is a colour", valid_color("#abc") == "#abc")
    for junk in ["red", "", "javascript:alert(1)", "#12345", "url(x)", "#1d3b2a;}body{"]:
        check(f"{junk!r} is refused", valid_color(junk) is None)

    check("the board refuses to store junk", await b.set_pipeline_color(PLAN, "nonsense") is None)
    check("...and keeps the old colour", b.pipelines.colors[PLAN] == DEFAULT_PIPELINE_COLORS[PLAN])

    # -----------------------------------------------------------------
    section("the human's colour is state: it persists and is not overwritten")
    await b.set_pipeline_color(MAINT, "#ff00aa")
    check("it took", b.pipelines.colors[MAINT] == "#ff00aa")
    b2 = Board(b.data_dir)
    check("it survives a reload", b2.pipelines.colors[MAINT] == "#ff00aa")
    check("the others are untouched", b2.pipelines.colors[BUILD] == DEFAULT_PIPELINE_COLORS[BUILD])
    check(
        "the columns were not disturbed by writing a colour",
        len(b2.pipelines.columns[MAINT]) == 5,
    )

    # -----------------------------------------------------------------
    section("the migration paints an old folder, and never repaints a custom one")
    ws = Workspace(tempfile.mkdtemp(prefix="km-ws-"), tempfile.mkdtemp(prefix="repo-"))
    ws.ensure()
    Board(ws.path)
    cfg = os.path.join(ws.path, "pipelines.json")

    raw = json.load(open(cfg))
    raw.pop("colors", None)  # rewind to a pre-colour folder
    json.dump(raw, open(cfg, "w"), indent=2)

    notes = m0004.migrate(ws)
    after = json.load(open(cfg))
    check("it reports what it did", bool(notes))
    check("all four are painted", set(after["colors"]) == {PLAN, MAINT, EXPED, BUILD})
    check("with the defaults", after["colors"][BUILD] == DEFAULT_PIPELINE_COLORS[BUILD])
    check("the columns survived", len(after[MAINT]) == 5)
    check("running it twice does nothing", m0004.migrate(ws) == [])

    after["colors"][PLAN] = "#123456"  # the human recolours
    json.dump(after, open(cfg, "w"), indent=2)
    m0004.migrate(ws)
    check(
        "an update never repaints a colour the human chose",
        json.load(open(cfg))["colors"][PLAN] == "#123456",
    )

    # -----------------------------------------------------------------
    section("the board fits the screen: no horizontal scroll, equal column widths")
    check("the board never scrolls sideways", "overflow-x:hidden" in page)
    check("columns are a grid, not a scrolling flex row", "grid-template-columns:repeat(var(--cols" in page)
    check("...sized off the LONGEST pipeline", "Math.max(1,...BOARD.pipelines.map(p=>p.columns.length))" in page)
    check("...and every row uses that same track count", "row.style.setProperty('--cols',cols)" in page)
    check("the '+ column' tile that caused the scroll is gone", "add-col" not in page)
    check("each pipeline has an edit button instead", "openPipe(p.id)" in page)
    check("which edits columns, workers and colour", all(
        s in page for s in ["p-color", "p-cols", "openWorker(p.id,col", "/api/pipeline/"]
    ))
    check(
        "and closeAll asks the DOM for drawers — a hardcoded list left d-pipe unclosable",
        "document.querySelectorAll('.drawer').forEach(d=>d.classList.remove('open'))" in page,
    )

    # -----------------------------------------------------------------
    section("THE POINT: a card is coloured by the pipeline it BELONGS to")
    check(
        "the card's colour comes from its home pipeline, not its current one",
        "function cardColor" in page and "c.kind==='maintenance' ? 'maint' : 'plan'" in page,
    )
    check(
        "...and an UNTYPED card is grey, belonging to no pipeline yet",
        "if(c.kind!=='feature' && c.kind!=='maintenance') return getVar('--untyped')" in page,
    )
    check("the card is a DARKER shade of it", re.search(r"--c-bg',shade\(home,\.\d+\)", page) is not None)
    check(
        "and the highlight of an open card still wins over it",
        ".card.open-chat{background:var(--card-open)" in page and "var(--c-bg" in page,
    )
    check(
        "which only works because the card colour is a CSS var, not an inline style",
        "d.style.setProperty('--c-bg'" in page,
    )

    section("the card's TYPE is on the card, and one click changes it")
    check("the drawer header shows the type, not the column", "function paintKind" in page)
    check("...painted the same colour as the card", "el.style.background=shade(home,.62)" in page)
    check("...and clicking it flips it", "type:'set_kind'" in page)
    check("the old 'Build → Build Review' location chip is gone", "$('c-where').textContent=colTitle" not in page)

    print(f"\n{'=' * 70}")
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("colors: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
