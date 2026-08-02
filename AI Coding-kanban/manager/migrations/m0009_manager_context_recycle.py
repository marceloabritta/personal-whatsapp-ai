"""Surface the manager's context-recycling knob in the working folder's .env.

The manager is a long-lived Claude session. Left alone it resumes the same conversation turn
after turn, and the context window — and the token bill — only grows. Recycling drops the
session every N turns and starts a fresh one; nothing is lost, because every turn rebuilds its
prompt from the board on disk.

The behaviour is already the default in code (25 turns, ON). This migration only writes the
key into `.env` so it is visible and tunable — set it to 0 to switch recycling off.
"""
from __future__ import annotations

from ._helpers import add_env_key

NUMBER = 9
DESCRIPTION = "manager recycles its context every N turns to save tokens (default 25; 0 = off)"


def migrate(ws) -> list[str]:
    added = add_env_key(
        ws,
        "MANAGER_CONTEXT_RECYCLE_TURNS",
        "25",
        comment="Manager drops its SDK session after this many turns to clear context and save tokens (0 = never).",
    )
    if not added:
        return []
    return [
        "the manager now clears its context every 25 turns to save tokens — it rebuilds from "
        "the board each turn, so nothing is lost. Tune or disable with MANAGER_CONTEXT_RECYCLE_TURNS in .env."
    ]
