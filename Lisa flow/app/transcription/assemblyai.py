"""AssemblyAI behind the seam — the first transcription backend.

Ported from secretary/2. Skills/2. Audio transcriptions/skill.js (aaiUpload + aaiTranscribe):
upload the decrypted bytes, create a transcript, poll until it completes. httpx instead of
fetch. Never raises — every failure returns a classified TranscriptResult.error."""
from __future__ import annotations

import asyncio
import logging

import httpx

from .base import TranscriptResult

log = logging.getLogger("mary.transcription.assemblyai")

_BASE = "https://api.assemblyai.com/v2"
_POLL_INTERVAL = 3.0  # seconds between status polls (WhatsApp voice notes are short)


class AssemblyAITranscriber:
    def __init__(self, settings) -> None:
        self.s = settings

    @property
    def _key(self) -> str:
        return self.s.assemblyai_api_key or ""

    async def transcribe(
        self, audio: bytes, *, mimetype: str, language: str | None
    ) -> TranscriptResult:
        if not self._key:
            return {"text": "", "error": "auth"}

        headers = {"authorization": self._key}
        timeout = httpx.Timeout(self.s.transcription_request_timeout)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                upload_url = await self._upload(client, headers, audio)
                return await self._transcribe(client, headers, upload_url, language)
        except _AAIError as exc:
            log.error("assemblyai %s: %s", exc.kind, exc)
            return {"text": "", "error": exc.kind}
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            log.error("assemblyai transport error: %s", exc)
            return {"text": "", "error": "provider"}

    async def _upload(self, client, headers, audio: bytes) -> str:
        resp = await client.post(f"{_BASE}/upload", headers=headers, content=audio)
        if resp.status_code in (401, 403):
            raise _AAIError("auth", f"upload {resp.status_code}")
        if resp.status_code >= 400:
            raise _AAIError("provider", f"upload {resp.status_code}: {resp.text[:200]}")
        return resp.json()["upload_url"]

    async def _transcribe(self, client, headers, upload_url: str,
                          language: str | None) -> TranscriptResult:
        body: dict = {"audio_url": upload_url}
        # No language (or "auto") → let AssemblyAI detect it; otherwise pin the code.
        if language and language.lower() != "auto":
            body["language_code"] = language
        else:
            body["language_detection"] = True

        create = await client.post(f"{_BASE}/transcript", headers=headers, json=body)
        if create.status_code in (401, 403):
            raise _AAIError("auth", f"transcript {create.status_code}")
        if create.status_code >= 400:
            raise _AAIError("provider", f"transcript {create.status_code}: {create.text[:200]}")
        tid = create.json()["id"]

        # Poll until completed/error, bounded by the configured ceiling.
        deadline = self.s.transcription_max_poll_seconds
        waited = 0.0
        while waited < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            waited += _POLL_INTERVAL
            poll = await client.get(f"{_BASE}/transcript/{tid}", headers=headers)
            if poll.status_code >= 400:
                continue  # transient — keep polling until the deadline
            data = poll.json()
            status = data.get("status")
            if status == "completed":
                text = (data.get("text") or "").strip()
                return {
                    "text": text,
                    "duration_sec": _as_float(data.get("audio_duration")),
                    "language": data.get("language_code"),
                    "error": None if text else "empty",
                }
            if status == "error":
                raise _AAIError("provider", f"status=error: {data.get('error')}")
        raise _AAIError("timeout", f"no result after {deadline}s")


class _AAIError(Exception):
    def __init__(self, kind: str, msg: str) -> None:
        super().__init__(msg)
        self.kind = kind


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
