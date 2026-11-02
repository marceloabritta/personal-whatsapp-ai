"""Session markers — is a chat mid-conversation with Mary?

`open()` takes an optional TTL so one kind of conversation can hold the window longer than
another: a setup loop needs minutes, because forwarding a contact card means leaving the chat
entirely, and so does a question waiting on an answer, while a normal exchange only needs the
usual seconds. `ttl_left()` lets a silent turn keep a longer window instead of shortening it.

Step 1 only OPENS a marker when @mary fires and READS it for continuations; nothing
yet extends it, so it simply expires after SESSION_TTL. Redis-backed when REDIS_URL
is set, in-memory otherwise (fine for a single-process dev run)."""
from __future__ import annotations

import math
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

    def open(self, jid: str, ttl: int | None = None) -> None:
        self._exp[jid] = time.time() + (ttl or self.ttl)

    def ttl_left(self, jid: str) -> int:
        """Whole seconds before the window lapses; 0 when it is closed."""
        exp = self._exp.get(jid)
        return max(0, math.ceil(exp - time.time())) if exp else 0

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

    def open(self, jid: str, ttl: int | None = None) -> None:
        try:
            self.r.set(f"session:{jid}", "1", ex=(ttl or self.ttl))
        except Exception:
            pass

    def ttl_left(self, jid: str) -> int:
        try:
            return max(0, int(self.r.ttl(f"session:{jid}")))  # -2 missing, -1 no expiry
        except Exception:
            return 0

    def close(self, jid: str) -> None:
        try:
            self.r.delete(f"session:{jid}")
        except Exception:
            pass
