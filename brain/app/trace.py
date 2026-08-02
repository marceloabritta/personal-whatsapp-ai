"""Two-level trace — every @mary run leaves both:

  code  : structured events for the builder (node I/O, API calls with args+replies,
          decision variables, timings). JSON to stdout, mirrored to LangSmith when
          langsmith is configured via env.
  user  : the human transcript — what was said in the chat (owner, contact, and Mary).

Both share one trace id (the activation) AND a loop id (the listening window that spans
activations). A durable LogStore can be attached; when it is, every LOOP-SCOPED record
is enqueued to Postgres, grouped by loop id into three streams — transcript, the AI
reasoning, and the control flow. Records with no loop id (an ignored message, a parse
before the gate decides) stay stdout-only. Logging never blocks or breaks the reply.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

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
    tests and a `/trace` endpoint can read a run without parsing logs. When a durable
    sink is attached, loop-scoped records are also enqueued to it (best-effort)."""

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

    def new_loop_id(self, chat_id: str) -> str:
        return f"loop-{chat_id or 'unknown'}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"

    def code(self, tid: str, node: str, *, loop_id: Optional[str] = None, **fields: Any) -> None:
        rec = {"trace_id": tid, "level": "code", "node": node, "loop_id": loop_id, **fields}
        self._remember(self.events, rec)
        self._emit(rec)
        self._to_sink(rec)

    def user(
        self, tid: str, who: str, text: str, *,
        loop_id: Optional[str] = None, wa_id: Optional[str] = None,
        ts: Optional[int] = None, **fields: Any,
    ) -> None:
        rec = {
            "trace_id": tid, "level": "user", "who": who, "text": text,
            "loop_id": loop_id, "wa_id": wa_id, "ts": ts, **fields,
        }
        self._remember(self.transcript, rec)
        self._persist(tid, rec)
        self._emit(rec)
        self._to_sink(rec)

    # -- internals -----------------------------------------------------------
    def _to_sink(self, rec: dict) -> None:
        # Only loop-scoped records are durable — an ignored message (no loop) is noise.
        if self._sink is None or not rec.get("loop_id"):
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
