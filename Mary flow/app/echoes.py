"""Echo store — the ids of WhatsApp messages Mary herself sent.

We send from the owner's own account, so Evolution echoes our replies back with
fromMe=true and they reappear in fetch_history. The header stamp (is_own_message)
is a fallback filter; the robust one is by message id: record every id we send and
drop it on ingestion. Per-key TTL so the store self-expires — it only needs to
outlive the reseed window (a fresh @mary re-reads the last N WhatsApp messages,
some of which may be our own recent replies). Redis-backed when REDIS_URL is set,
in-memory otherwise (fine for a single-process dev run)."""
from __future__ import annotations

import time
from typing import Any


class InMemoryEchoes:
    def __init__(self, ttl: int = 604800) -> None:
        self.ttl = ttl
        self._exp: dict[str, float] = {}

    @staticmethod
    def _k(jid: str, msg_id: str) -> str:
        return f"{jid}:{msg_id}"

    def record(self, jid: str, msg_id: str | None) -> None:
        if not msg_id:
            return
        self._exp[self._k(jid, msg_id)] = time.time() + self.ttl

    def is_ours(self, jid: str, msg_id: str | None) -> bool:
        if not msg_id:
            return False
        k = self._k(jid, msg_id)
        exp = self._exp.get(k)
        if not exp:
            return False
        if exp < time.time():
            self._exp.pop(k, None)
            return False
        return True


class RedisEchoes:
    def __init__(self, client: Any, ttl: int = 604800) -> None:
        self.r = client
        self.ttl = ttl

    def record(self, jid: str, msg_id: str | None) -> None:
        if not msg_id:
            return
        try:
            self.r.set(f"echo:{jid}:{msg_id}", "1", ex=self.ttl)
        except Exception:
            pass

    def is_ours(self, jid: str, msg_id: str | None) -> bool:
        if not msg_id:
            return False
        try:
            return bool(self.r.exists(f"echo:{jid}:{msg_id}"))
        except Exception:
            return False
