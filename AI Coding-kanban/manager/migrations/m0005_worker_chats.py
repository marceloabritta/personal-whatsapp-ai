"""Give `board.json` a place to keep the conversations about each column's worker.

Talking to the manager about a worker is its own subject — "make this reviewer stop
inventing work" is not the same conversation as "what's in flight?" — so it gets its own
thread and its own SDK session, keyed `<pipeline>/<slug>`, exactly like a card gets one.

This migration only opens the drawer it lives in. It is additive and empty: no existing
card, thread, column or prompt is touched, and a folder that already has the key is left
exactly as it is.
"""
from __future__ import annotations

from ._helpers import read_json, write_json

NUMBER = 5
DESCRIPTION = "board.json: keep a conversation per column worker"


def migrate(ws) -> list[str]:
    data = read_json(ws.board_path)
    if data is None:
        return []  # no board yet — Board writes the key itself the first time it saves
    if isinstance(data.get("worker_chats"), dict):
        return []

    data["worker_chats"] = {}
    write_json(ws.board_path, data)
    return [
        "board.json can now hold one conversation per column worker — open a column's 🧠 "
        "and talk to the manager about its contract"
    ]
