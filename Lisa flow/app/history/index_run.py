"""Run the history indexer by hand: the first backfill, under a watchful eye, before the switch.

    docker exec lisa_brain python -m app.history.index_run              # one pass (the backfill)
    docker exec lisa_brain python -m app.history.index_run --pause 0.5  # gentler on the database
    docker exec lisa_brain python -m app.history.index_run --ddl        # print the schema's SQL

One pass is exactly what HISTORY_INDEX_ENABLED runs every minute: from an empty lisa_search it pages
through all of "Message", logging progress every 20 pages; on an index that exists it is an
incremental pass. Stopping it (Ctrl-C) is safe at any point: the cursor is saved with every page and
the next pass resumes there. It uses the app's own settings (DATABASE_URL, LOG_SCHEMA, OWNER_NAME …),
without the address book; the app's indexer adds those names on its first pass."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys


async def _main(args) -> int:
    from ..config import load_settings
    from .indexer import HistoryIndexer
    from .sql_schema import ddl, ident

    s = load_settings()
    schema = ident(s.history_schema)
    if args.ddl:
        print(f"CREATE SCHEMA IF NOT EXISTS {schema};\n"
              f"CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA {schema};"
              f"{ddl(schema, schema)}")
        return 0
    if not s.database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    idx = HistoryIndexer(s.database_url, schema=schema, settings=s, batch_size=args.batch,
                         pause_seconds=args.pause)
    await idx.open()
    try:
        stats = await idx.run_once()
    finally:
        await idx.aclose()
    print(json.dumps(stats))
    return 1 if stats.get("errors") else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ddl", action="store_true", help="print the schema's SQL and exit")
    p.add_argument("--batch", type=int, default=500, help="rows per page (500)")
    p.add_argument("--pause", type=float, default=0.25, help="seconds between pages (0.25)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
