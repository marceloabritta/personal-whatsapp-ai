"""Stable per-chat thread id for the checkpointer.

One thread per WhatsApp conversation, keyed by instance + chat JID (not the owner's
number) so it scales cleanly across every 1:1 and group."""
from __future__ import annotations

import hashlib


def make_thread_id(evolution_instance: str, chat_jid: str) -> str:
    raw = f"{evolution_instance}:{chat_jid}"
    return hashlib.sha256(raw.encode()).hexdigest()
