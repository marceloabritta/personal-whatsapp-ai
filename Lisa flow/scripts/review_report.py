"""Review history and read the result.

  backfill — grade every session in scope that has no review at this judge version.
  report   — aggregate what is stored into a ranked list; terminal, or a page with --html.
  sweep    — run one reaper pass by hand (what the live service does on a timer).

Run it where the database is reachable. The simplest is inside the container, which already holds
DATABASE_URL and the API key:

  docker exec lisa_brain python -m scripts.review_report backfill --scope-version <v>
  docker exec lisa_brain python -m scripts.review_report report --html /tmp/review.html

Re-scoring is expected: edit the taxonomy or the judge prompt, bump REVIEW_JUDGE_VERSION, and run
backfill again. Old verdicts stay, new ones land beside them, and the two can be compared before
the new rubric becomes the one the sweeper uses."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_settings  # noqa: E402
from app.review import Reviewer, ReviewStore  # noqa: E402
from app.review.report import build_report, format_text  # noqa: E402
from app.review.report_html import render, standalone  # noqa: E402


async def _open(settings):
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set — nothing to review.")
    store = ReviewStore(settings.database_url, schema=settings.log_schema)
    await store.open()
    return store


async def cmd_backfill(args, settings) -> int:
    store = await _open(settings)
    try:
        scope = args.scope_version or settings.review_scope_version or None
        loop_ids = await store.pending_loops(
            judge_version=settings.review_judge_version,
            settle_seconds=0,           # history is settled by definition
            batch=args.limit,
            scope_version=scope,
        )
        if not loop_ids:
            print("nothing to do — every session in scope already has a review at "
                  f"judge_version={settings.review_judge_version}")
            return 0

        print(f"backfilling {len(loop_ids)} sessions "
              f"(judge={settings.review_model}, version={settings.review_judge_version})")
        reviewer = Reviewer(settings, store)
        t0, turns, bad = time.monotonic(), 0, 0
        for i, loop_id in enumerate(loop_ids, 1):
            res = await reviewer.review_loop(loop_id)
            turns += res.get("turns", 0)
            bad += res.get("bad", 0)
            print(f"  [{i}/{len(loop_ids)}] {loop_id[-24:]}  "
                  f"turns={res.get('turns', 0)} bad={res.get('bad', 0)}")
        print(f"\n{turns} turns graded, {bad} bad, in {time.monotonic() - t0:.0f}s")
        return 0
    finally:
        await store.aclose()


async def cmd_report(args, settings) -> int:
    store = await _open(settings)
    try:
        rep = await build_report(
            store,
            judge_version=args.judge_version or settings.review_judge_version,
            scope_version=args.scope_version or settings.review_scope_version or None,
        )
        if not rep["headline"]["turns"]:
            print("no reviews stored for this judge version — run `backfill` first")
            return 1
        print(format_text(rep))
        if args.html:
            inner = render(rep, generated_at=time.strftime("%d %b %Y %H:%M"))
            body = inner if args.fragment else standalone(inner)
            with open(args.html, "w", encoding="utf-8") as fh:
                fh.write(body)
            print(f"\nwrote {args.html} ({len(body)} bytes)"
                  + (" [artifact fragment]" if args.fragment else ""))
        return 0
    finally:
        await store.aclose()


async def cmd_sweep(args, settings) -> int:
    from app.review.reaper import Reaper

    store = await _open(settings)
    try:
        reaper = Reaper(Reviewer(settings, store), settings)
        n = await reaper.sweep_once()
        print(f"reviewed {n} session(s)")
        return 0
    finally:
        await store.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(prog="review_report", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="grade sessions with no review at this judge version")
    b.add_argument("--scope-version", default=None, help="only this prompt_version")
    b.add_argument("--limit", type=int, default=500)

    r = sub.add_parser("report", help="aggregate stored reviews")
    r.add_argument("--scope-version", default=None)
    r.add_argument("--judge-version", default=None)
    r.add_argument("--html", default=None, metavar="PATH", help="also write the page here")
    r.add_argument("--fragment", action="store_true",
                   help="emit artifact-ready content (no document wrapper)")

    sub.add_parser("sweep", help="one reaper pass, by hand")

    args = ap.parse_args()
    settings = load_settings()
    fn = {"backfill": cmd_backfill, "report": cmd_report, "sweep": cmd_sweep}[args.cmd]
    return asyncio.run(fn(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
