"""Two-level trace — every @mary run leaves both:

  code  : structured events for the builder (node I/O, API calls with args+replies,
          decision variables, timings). JSON to stdout, mirrored to LangSmith when
          langsmith is configured via env.
  user  : the human transcript — what you sent vs. what Mary sent back on WhatsApp.

Both share one trace id, so a run can be replayed from either angle. A gated-out
message writes only a one-line "ignored" note (see gate node), keeping this clean.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Any, Optional

# The trace id of the activation currently running, set once per webhook in main._run
# BEFORE the graph is invoked (so every node task inherits it via copy_context). It lets
# the shared IO clients — evolution, calendar — emit control events through Trace.io
# without threading a trace id through every call signature.
current_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_trace_id", default=None
)

try:  # structured JSON logging when available; plain logging otherwise.
    import structlog

    _slog = structlog.get_logger("mary")
    _HAS_STRUCTLOG = True
except Exception:  # pragma: no cover - fallback path
    _slog = None
    _HAS_STRUCTLOG = False

_logger = logging.getLogger("mary")


class Trace:
    """Emits both trace levels. Keeps the last events/transcript in memory too, so
    tests and a future `/trace/{id}` endpoint can read a run without parsing logs."""

    def __init__(self, store: Any = None, keep: int = 2000) -> None:
        self.store = store  # optional redis client for durable transcript
        self.keep = keep
        self.events: list[dict] = []  # code-level
        self.transcript: list[dict] = []  # user-level
        self._sink: Any = None  # optional durable LogStore (Postgres)

    def attach_sink(self, sink: Any) -> None:
        """Attach the durable LogStore. Until attached, Trace is stdout + ring only,
        exactly as before — so dev/tests without a DB behave unchanged."""
        self._sink = sink

    def start(self, chat_id: str) -> str:
        tid = f"mary-{chat_id or 'unknown'}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"
        return tid

    def code(self, tid: str, node: str, **fields: Any) -> None:
        rec = {"trace_id": tid, "level": "code", "node": node, **fields}
        self._remember(self.events, rec)
        self._emit(rec)
        self._to_sink(rec)

    def user(self, tid: str, who: str, text: str, **fields: Any) -> None:
        rec = {"trace_id": tid, "level": "user", "who": who, "text": text, **fields}
        self._remember(self.transcript, rec)
        self._persist(tid, rec)
        self._emit(rec)
        self._to_sink(rec)

    def io(self, api: str, **fields: Any) -> None:
        """A control-stream event from a shared IO client (evolution, calendar). Reads
        the current activation's trace id from the contextvar, so no tid plumbing is
        needed. A no-op outside an activation (e.g. a bare unit test)."""
        tid = current_trace_id.get()
        if tid:
            self.code(tid, node="io", api=api, **fields)

    # -- internals -----------------------------------------------------------
    def _to_sink(self, rec: dict) -> None:
        if self._sink is None:
            return
        try:
            self._sink.enqueue(dict(rec))  # copy: the ring keeps the original untouched
        except Exception:  # never let the durable sink break the reply path
            pass

    def _remember(self, buf: list[dict], rec: dict) -> None:
        buf.append(rec)
        if len(buf) > self.keep:
            del buf[: len(buf) - self.keep]

    def _persist(self, tid: str, rec: dict) -> None:
        if not self.store:
            return
        try:
            self.store.rpush(f"transcript:{tid}", json.dumps(rec, default=str))
            self.store.expire(f"transcript:{tid}", 60 * 60 * 24 * 30)
        except Exception:  # never let logging break the reply path
            pass

    def _emit(self, rec: dict) -> None:
        if _HAS_STRUCTLOG:
            _slog.info(rec.get("level", "trace"), **rec)
        else:
            _logger.info("%s", json.dumps(rec, default=str))


def build_trace(store: Optional[Any] = None) -> Trace:
    return Trace(store=store)
