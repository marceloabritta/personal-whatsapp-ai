"""The Plan-Ready HTML doc.

When a card reaches the PLAN pipeline's final (gate) column, the board renders its plan
artifacts (SCOPE.md / PLAN.md / …) into a Notion-style `plan.html` in the card folder and
exposes it as an artifact (Board._plan_doc_if_ready → plandoc.write_plan_doc). This proves the
dependency-free markdown renderer and the move_card trigger.

    python tests/plandoc_test.py        (no API key, no network)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager import plandoc  # noqa: E402
from manager.board import Board  # noqa: E402
from manager.models import PLAN  # noqa: E402

FAILED: list = []


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


async def main() -> int:
    print("\nthe markdown renderer (dependency-free)")
    h = plandoc.render_markdown(
        "# Title\n\n"
        "Para with **bold**, `code`, and [a link](https://x.com).\n\n"
        "- one\n- two\n\n"
        "| Op | Input |\n|----|-------|\n| create | `start_iso` |\n\n"
        "```js\nconst x = 1;\n```\n\n"
        "> a note\n\n---\n"
    )
    check("heading", "<h1>Title</h1>" in h)
    check("bold / inline code / link", "<strong>bold</strong>" in h and "<code>code</code>" in h and '<a href="https://x.com">' in h)
    check("unordered list", "<ul><li>one</li><li>two</li></ul>" in h)
    check("table", "<table>" in h and "<th>Op</th>" in h and "<td>create</td>" in h)
    check("fenced code (escaped, not parsed)", "<pre><code>const x = 1;</code></pre>" in h)
    check("blockquote + hr", "<blockquote>a note</blockquote>" in h and "<hr />" in h)
    check("html is escaped", "&lt;" not in "Title" and "<script>" not in plandoc.render_markdown("<script>x</script>"))

    print("\nthe steps render as numbered blocks")
    hs = plandoc.render_markdown(
        "## Steps\n\n1. **Add the toggle** — a button in `header.js`.\n2. **Wire it up** — swap `--bg`.\n\n## Files\n- `header.js`\n"
    )
    check("each step is its own block", hs.count('class="step"') == 2)
    check("a numbered badge on each", 'class="step-n"' in hs)
    check("the Files list is NOT a step block", "<ul>" in hs)

    print("\nthe Plan-gate trigger")
    b = Board(tempfile.mkdtemp(prefix="km-pd-"))
    card = await b.add_card("Calendar recurring events", pipeline=PLAN, kind="feature")
    with open(os.path.join(b.abs_dir(card), "IDEA.md"), "w", encoding="utf-8") as fh:
        fh.write("# Idea\n\n**What:** Repeat events weekly.\n")
    check("no plan.html before the gate", "plan.html" not in card.artifacts)

    last = b.pipelines.columns[PLAN][-1]  # the Plan gate
    await b.move_card(card.id, last.id)
    card = b.cards[card.id]
    doc = os.path.join(b.abs_dir(card), "plan.html")
    check("plan.html generated on reaching the Plan gate", os.path.isfile(doc))
    check("plan.html tracked as a card artifact", "plan.html" in card.artifacts)
    text = open(doc, encoding="utf-8").read()
    check("doc is a full page titled with the card", "<!doctype html>" in text and "Calendar recurring events" in text)
    check("doc carries the intake content", "Repeat events weekly" in text)

    # The real flow: the plan worker writes PLAN.md WHILE the card sits at the gate — the doc
    # must (re)generate off that, not just off what existed when the card arrived.
    with open(os.path.join(b.abs_dir(card), "PLAN.md"), "w", encoding="utf-8") as fh:
        fh.write("# Plan\n\n## Steps\n\n1. **Build the RRULE** — in `recurrence.js`.\n")
    await b.set_artifact(card.id, "PLAN.md", os.path.join(card.dir, "PLAN.md"))
    text = open(doc, encoding="utf-8").read()
    check("doc regenerates when PLAN.md lands at the gate", "recurrence.js" in text)
    check("...and the plan's step is a block", 'class="step"' in text)

    print("\nedge cases")
    empty = await b.add_card("Nothing planned", pipeline=PLAN, kind="feature")
    await b.move_card(empty.id, last.id)
    check("no doc when the card has no plan artifacts", "plan.html" not in b.cards[empty.id].artifacts)

    b2 = Board(tempfile.mkdtemp(prefix="km-pd2-"))
    mid = await b2.add_card("Mid pipeline", pipeline=PLAN, kind="feature")
    with open(os.path.join(b2.abs_dir(mid), "PLAN.md"), "w", encoding="utf-8") as fh:
        fh.write("# Plan\n\nx\n")
    await b2.move_card(mid.id, b2.pipelines.columns[PLAN][0].id)  # the non-gate intake column
    check("no doc on a non-gate plan column", "plan.html" not in b2.cards[mid.id].artifacts)

    print("\n" + ("ALL PASSED" if not FAILED else "FAILED: " + ", ".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
