"""The board-level chat's HANDS — the tools the manager uses to create and move cards.

A regression test for a real report: cards the manager created from bug reports and feature
requests were landing in Plan / Maintenance / Expedited instead of the backlog, and the
manager said it could neither see nor move a card back to the backlog. The cause was entirely
in the board-chat tool layer:

  - `create_card` defaulted to `plan` and FORCED any card into an origin pipeline — there was
    no code path that left a card in the backlog.
  - `move_card` resolved its target only across pipelines; the backlog is not a pipeline and
    has no column, so "move it to the backlog" was impossible.
  - there was no `send_to_backlog` tool at all, and `list_cards` rendered a backlog card's
    column as "?".

`backlog_test.py` proves the BOARD layer already does the right thing; this file proves the
board-chat TOOLS actually call it. It reaches the tools the way the SDK does — through the
MCP server's public `tools/call` request — so the wiring under test is the real wiring.

    python tests/boardtools_test.py        (no API key, no network)
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest  # noqa: E402

from manager.board import Board  # noqa: E402
from manager.manager import Manager, ManagerConfig  # noqa: E402
from manager.models import BACKLOG, FEATURE, MAINT, MAINTENANCE, UNSET  # noqa: E402

FAILED: list = []


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


async def call(server, name, args) -> str:
    """Invoke a board tool the way the SDK does: a tools/call request to the MCP server."""
    handler = server["instance"].request_handlers[CallToolRequest]
    req = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=name, arguments=args)
    )
    res = await handler(req)
    return res.root.content[0].text


async def tool_names(server) -> set:
    handler = server["instance"].request_handlers[ListToolsRequest]
    res = await handler(ListToolsRequest(method="tools/list"))
    return {t.name for t in res.root.tools}


def card_titled(b, title):
    return next(c for c in b.cards.values() if c.title == title)


async def main() -> int:
    b = Board(tempfile.mkdtemp(prefix="km-bt-"))
    m = Manager(b, ManagerConfig(repo_dir=".", data_dir=b.data_dir, mock=True))
    mid = next(iter(b.managers))
    server = m._board_tools(mid)

    # -----------------------------------------------------------------
    section("the manager has a hand for the backlog")
    names = await tool_names(server)
    check("send_to_backlog is a real tool", "send_to_backlog" in names)
    check("create_card is still there", "create_card" in names)

    # -----------------------------------------------------------------
    section("every card the manager creates is born in the backlog")
    await call(server, "create_card", {"title": "A feature request", "description": "x", "kind": ""})
    idea = card_titled(b, "A feature request")
    check("an untyped card lands in the backlog", idea.pipeline == BACKLOG)
    check("...with no column", idea.column == "")
    check("...untyped, awaiting classification", idea.kind == UNSET)

    await call(server, "create_card", {"title": "It replies twice", "description": "", "kind": "maintenance"})
    bug = card_titled(b, "It replies twice")
    check("a bug report ALSO lands in the backlog, not Maintenance", bug.pipeline == BACKLOG)
    check("...but is typed as maintenance while it waits", bug.kind == MAINTENANCE)

    await call(server, "create_card", {"title": "Dark mode", "description": "", "kind": "feature"})
    feat = card_titled(b, "Dark mode")
    check("a feature request lands in the backlog, not Plan", feat.pipeline == BACKLOG)
    check("...typed as a feature", feat.kind == FEATURE)

    # -----------------------------------------------------------------
    section("the manager can see backlog cards clearly")
    rows = json.loads(await call(server, "list_cards", {}))
    row = next(r for r in rows if r["id"] == idea.id)
    check("list_cards names the backlog as the pipeline", row["pipeline"] == "backlog")
    check("...and does not render a phantom '?' column", row["column"] != "?")

    # -----------------------------------------------------------------
    section("the manager can move a card back to the backlog")
    await b.route_card(bug.id, MAINT)
    check("(routed out first)", b.cards[bug.id].pipeline == MAINT)

    out = await call(server, "move_card", {"card_id": bug.id, "column": "backlog"})
    check("move_card column='backlog' returns it", b.cards[bug.id].pipeline == BACKLOG)
    check("...and reports it, not 'no such column'", "no such column" not in out.lower())
    check("...keeping its type", b.cards[bug.id].kind == MAINTENANCE)

    await b.route_card(bug.id, MAINT)
    await call(server, "send_to_backlog", {"card_id": bug.id})
    check("send_to_backlog pulls it back too", b.cards[bug.id].pipeline == BACKLOG)

    miss = await call(server, "send_to_backlog", {"card_id": "does-not-exist"})
    check("send_to_backlog on a bad id fails cleanly", "no such card" in miss.lower())

    print(f"\n{'ALL PASSED' if not FAILED else 'FAILED: ' + ', '.join(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
