"""Messages that arrived while the system was being updated.

Shipping an update means restarting the process. The old way of doing that was to kill it and
let `manager/recovery.py` pick up the pieces — and recovery *works*, but it is a seatbelt, not
a shipping strategy. A run killed mid-flight loses the turn it was in: the worker that was
halfway through a task is gone, and its unwritten output with it. Resuming re-does that work
from the last thing that reached disk, and everything after it is lost.

So we do not kill runs any more. We DRAIN: stop accepting new work, let the in-flight runs
finish, and only then restart.

That leaves one hole, and this file is it. During the drain — which can take minutes if a
build is running — the human can still type. Their message must not be dropped on the floor
and must not be silently swallowed by a process that is about to exit. So it is written HERE,
on disk, before we acknowledge it, and dispatched on the other side of the restart.

Same shape as the journal, and for the same reason: if it is not on disk before the process
can die, it does not exist.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Message:
    kind: str  # CARD | MANAGER | WORKER — which handler this is for
    target_id: str  # card id / manager id / prompt key
    text: str
    # Did a PERSON say this, or is it machinery (a card told to pick itself back up after the
    # restart)? A person is owed an answer; machinery is not. See Manager.handle_card_message.
    from_human: bool = True
    queued_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Message":
        known = {f for f in Message.__dataclass_fields__}  # type: ignore[attr-defined]
        return Message(**{k: v for k, v in d.items() if k in known})


class PendingQueue:
    """Messages received while draining, in the order they were sent."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "pending.json")
        self._msgs: list[Message] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        self._msgs = [Message.from_dict(m) for m in (raw.get("messages") or [])]

    def _save(self) -> None:
        """Atomic, like the journal and board.json. A queue that can be truncated by a crash
        is a queue that can eat the very message it exists to protect."""
        payload = {"messages": [m.to_dict() for m in self._msgs]}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def add(self, kind: str, target_id: str, text: str, from_human: bool = True) -> Message:
        m = Message(kind=kind, target_id=target_id, text=text, from_human=from_human)
        self._msgs.append(m)
        self._save()
        return m

    def all(self) -> list[Message]:
        return list(self._msgs)

    def __len__(self) -> int:
        return len(self._msgs)

    def clear(self) -> None:
        self._msgs = []
        self._save()
