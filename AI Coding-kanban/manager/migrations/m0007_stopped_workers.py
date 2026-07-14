"""Let a card remember a worker that was STOPPED, so it can be resumed instead of restarted.

A restart tells every running worker to stop. That is only acceptable because nothing it knew
is thrown away:

  * before it goes, it writes `WIP.md` into the card folder — done / mid-flight / next;
  * the SDK session it was thinking in is remembered on the card;
  * after the restart it is RESUMED in that same conversation, reads its own WIP note, and
    carries on. It does not start the column again from a blank sheet.

This migration only opens the two fields that hold that. It is additive and empty: no card,
thread, column or prompt is touched, and a card that was never stopped simply has them unset.
"""
from __future__ import annotations

from ._helpers import read_json, write_json

NUMBER = 7
DESCRIPTION = "a card can remember a worker it stopped, so a restart resumes it rather than redoing it"


def migrate(ws) -> list[str]:
    data = read_json(ws.board_path)
    if data is None:
        return []

    changed = 0
    for card in data.get("cards", []):
        if "worker_session" in card and "worker_name" in card:
            continue
        card.setdefault("worker_session", None)
        card.setdefault("worker_name", "")
        changed += 1

    if not changed:
        return []
    write_json(ws.board_path, data)
    return [
        "cards can now remember a worker that was stopped for a restart — it saves a WIP note, "
        "and is RESUMED in the same conversation afterwards rather than starting over"
    ]
