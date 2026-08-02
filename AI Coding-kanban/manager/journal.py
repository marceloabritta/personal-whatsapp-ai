"""The in-flight journal: a durable record that a run STARTED.

Before this existed, a run lived only in RAM. Nothing on disk said that a run had begun,
what card it was for, or what message triggered it — so when the process was killed
mid-flight (a closed terminal, a `kill -9`), the system came back with no idea a run had
ever been dispatched. It could not resume it, could not retry it, could not even report it.
The only trace was a card stuck at `busy: true` forever, spinning against nothing.

So: write the run down before it starts, delete it when it finishes. **Anything still in
this file at boot was interrupted**, and that leftover entry is the recovery ticket
(see manager/recovery.py).

    <workspace>/inflight.json

Written atomically (tmp + os.replace), like board.json — a journal that can be truncated by
the very crash it exists to survive would be worse than no journal at all.

`attempts` is what keeps a crash from becoming a crash *loop*: a run that kills the process
would otherwise be resumed by the supervisor, kill it again, and be resumed again, forever.
After MAX_ATTEMPTS the entry is retired and the human is told, rather than being handed an
infinite restart cycle.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field

CARD = "card"
MANAGER = "manager"
WORKER = "worker"  # a chat ABOUT one column's worker (its contract), not a card run

MAX_ATTEMPTS = 3  # give up on resuming a run that has already taken the process down twice


@dataclass
class Run:
    kind: str  # CARD | MANAGER
    target_id: str  # card id, or manager id
    text: str  # the message that triggered the run
    session_id: str | None = None  # the SDK session, as known at dispatch
    column: str = ""  # where the card was — for the human reading a log
    started_at: float = field(default_factory=time.time)
    attempts: int = 0  # how many times we have tried to RESUME this run

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Run":
        known = {f for f in Run.__dataclass_fields__}  # type: ignore[attr-defined]
        return Run(**{k: v for k, v in d.items() if k in known})


class Journal:
    """Every run currently believed to be in flight, keyed `<kind>:<id>`."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "inflight.json")
        self.data_dir = data_dir
        self._runs: dict[str, Run] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        for d in raw.get("runs", []):
            try:
                run = Run.from_dict(d)
            except TypeError:
                continue
            self._runs[run.key] = run

    def _save(self) -> None:
        payload = {"runs": [r.to_dict() for r in self._runs.values()]}
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ---- the two calls that matter -----------------------------------
    def start(self, kind: str, target_id: str, text: str, session_id: str | None = None,
              column: str = "") -> Run:
        """Called immediately BEFORE a run is dispatched. Must reach disk first — that is
        the whole point."""
        key = f"{kind}:{target_id}"
        previous = self._runs.get(key)
        run = Run(
            kind=kind,
            target_id=target_id,
            text=text,
            session_id=session_id,
            column=column,
            # A resume re-enters start(); it must not reset the attempt counter, or a run
            # that reliably kills the process would retry forever.
            attempts=previous.attempts if previous else 0,
        )
        self._runs[key] = run
        self._save()
        return run

    def finish(self, kind: str, target_id: str) -> None:
        """Called when the run completes — successfully or with an error it survived. Either
        way it is no longer in flight, so it must not be resumed on the next boot."""
        if self._runs.pop(f"{kind}:{target_id}", None) is not None:
            self._save()

    # ---- recovery ----------------------------------------------------
    def all(self) -> list[Run]:
        """Everything still recorded as in flight. At boot, that means: interrupted."""
        return sorted(self._runs.values(), key=lambda r: r.started_at)

    def bump(self, run: Run) -> int:
        """Count a resume ATTEMPT before making it, so a run that kills the process on every
        resume runs out of road instead of looping."""
        run.attempts += 1
        self._runs[run.key] = run
        self._save()
        return run.attempts

    def get(self, kind: str, target_id: str) -> Run | None:
        return self._runs.get(f"{kind}:{target_id}")

    def is_exhausted(self, run: Run) -> bool:
        return run.attempts >= MAX_ATTEMPTS
