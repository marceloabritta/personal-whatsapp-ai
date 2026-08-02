"""Session markers — is a chat mid-conversation with Mary?

Step 1 only OPENS a marker when @mary fires and READS it for continuations; nothing
yet extends it, so it simply expires after SESSION_TTL. Redis-backed when REDIS_URL
is set, in-memory otherwise (fine for a single-process dev run)."""
from __future__ import annotations

import time
from typing import Any


class InMemorySessions:
    def __init__(self, ttl: int = 1800) -> None:
        self.ttl = ttl
        self._exp: dict[str, float] = {}

    def is_open(self, jid: str) -> bool:
        exp = self._exp.get(jid)
        if not exp:
            return False
        if exp < time.time():
            self._exp.pop(jid, None)
            return False
        return True

    def open(self, jid: str) -> None:
        self._exp[jid] = time.time() + self.ttl

    def close(self, jid: str) -> None:
        self._exp.pop(jid, None)


class RedisSessions:
    def __init__(self, client: Any, ttl: int = 1800) -> None:
        self.r = client
        self.ttl = ttl

    def is_open(self, jid: str) -> bool:
        try:
            return bool(self.r.exists(f"session:{jid}"))
        except Exception:
            return False

    def open(self, jid: str) -> None:
        try:
            self.r.set(f"session:{jid}", "1", ex=self.ttl)
        except Exception:
            pass

    def close(self, jid: str) -> None:
        try:
            self.r.delete(f"session:{jid}")
        except Exception:
            pass
