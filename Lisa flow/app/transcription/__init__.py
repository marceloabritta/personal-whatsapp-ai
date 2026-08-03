"""Provider-neutral transcription. The graph imports only `Transcriber` / `build_transcriber`;
concrete providers (AssemblyAI today) live behind the seam and are chosen by env — the same
shape as app/reasoning/. A future Whisper/Groq backend drops in with one factory branch and no
graph change."""
from __future__ import annotations

from .base import Transcriber, TranscriptResult


def build_transcriber(settings) -> Transcriber:
    """Build the provider's transcriber, selected by settings.transcription_provider."""
    provider = (settings.transcription_provider or "assemblyai").lower()
    if provider == "assemblyai":
        from .assemblyai import AssemblyAITranscriber

        return AssemblyAITranscriber(settings)
    raise ValueError(f"unknown TRANSCRIPTION_PROVIDER: {provider!r}")


__all__ = ["Transcriber", "TranscriptResult", "build_transcriber"]
