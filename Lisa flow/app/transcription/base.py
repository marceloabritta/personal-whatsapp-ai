"""The transcription seam. The graph depends only on these shapes — never on a concrete
speech-to-text SDK, mirroring the reasoning seam (app/reasoning/base.py).

A Transcriber turns audio bytes into text. Like the tool ActionResult, it must NEVER raise
into the graph: every failure comes back as a TranscriptResult with `error` classified, so
the caller (a fast-path node or the context node) can degrade honestly."""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict


class TranscriptResult(TypedDict, total=False):
    text: str                    # the transcript ("" when empty/silent, or on error)
    duration_sec: Optional[float]  # audio length as the provider measured it, or None
    language: Optional[str]      # ISO 639-1 the provider detected/used, or None
    error: Optional[str]         # classification when it failed: "auth" | "download" |
                                 #   "provider" | "timeout" | "empty" | None on success


class Transcriber(Protocol):
    async def transcribe(
        self, audio: bytes, *, mimetype: str, language: str | None
    ) -> TranscriptResult:
        """Bytes → text. `language` may be an ISO code, or None/"auto" to let the provider
        detect it. NEVER raises — a failure is a TranscriptResult with a classified `error`."""
        ...
