"""EvolutionHistoryMedia — the HistoryMedia seam (types.py) over Evolution and the transcription
service.

WhatsApp keeps media downloadable for about 16-21 days; Evolution re-downloads it by message id
(getBase64FromMediaMessage). What a message IS (its type, declared mimetype, file name, date) is
read from Evolution's own record of it, by the same id, because the download itself says only
the mimetype. That record also tells an expired file (old) from a failed download (recent).

send_to_owner has ONE destination: the owner's chat with himself (settings.owner_jid). It is not a
parameter, so nothing upstream can point a file anywhere else."""
from __future__ import annotations

import logging
import mimetypes
import time
from typing import Any, Optional

from ..clients.evolution import send_target
from ..whatsapp import media_info
from .types import FileResult

log = logging.getLogger("mary.history.media")

SEND_MAX_BYTES = 32_000_000          # what is re-sent at most (base64 in memory, then JSON)
EXPIRED_AFTER_SECONDS = 16 * 86400   # WhatsApp serves media ~16-21 days; a failed download past 16 is "expired"
_MEDIA_TYPES = ("audio", "image", "video", "document")
_SEND_AS_IMAGE = ("image/jpeg", "image/png")
_SEND_AS_VIDEO = ("video/mp4", "video/3gpp")


# The index's message kinds (types.KINDS) as whatsapp.media_info names them.
_INDEX_KIND_TO_TYPE = {"voice": "audio"}


class EvolutionHistoryMedia:
    def __init__(self, evolution, transcription, settings) -> None:
        self.evolution = evolution
        self.transcription = transcription
        self.settings = settings
        self._meta_cache: dict[str, Optional[dict]] = {}
        # The history index, when runtime attaches it: names a file and dates it without asking
        # Evolution. None (tests, the CLI's fake) falls back to Evolution's own record.
        self.store: Any = None

    # --- the seam -------------------------------------------------------------------------

    async def transcribe(self, msg_id: str) -> dict:
        """{"ok", "text", "error"}. Cached (and the cache shared) by the transcription service,
        so a note transcribed once is never paid for again."""
        if self.transcription is None:
            return {"ok": False, "text": "", "error": "failed"}
        try:
            res = await self.transcription.get(msg_id) or {}
        except Exception as exc:
            log.warning("history transcribe failed: %s", type(exc).__name__)
            return {"ok": False, "text": "", "error": "failed"}
        text = (res.get("text") or "").strip()
        if text:
            return {"ok": True, "text": text, "error": None}
        err = res.get("error")
        if err == "empty":
            return {"ok": False, "text": "", "error": "empty"}
        if err == "download":
            return {"ok": False, "text": "", "error": await self._gone_or_failed(msg_id)}
        return {"ok": False, "text": "", "error": "failed"}

    async def fetch(self, msg_id: str, *, max_bytes: int = 15_000_000) -> FileResult:
        meta = await self._meta(msg_id)
        if meta is not None and meta.get("type") not in _MEDIA_TYPES:
            return FileResult(ok=False, error="not_media")
        try:
            res = await self.evolution.get_media_base64(msg_id)
        except Exception as exc:
            log.warning("history fetch failed: %s", type(exc).__name__)
            res = None
        b64 = (res or {}).get("base64")
        if not b64:
            return FileResult(ok=False, error=await self._gone_or_failed(msg_id))
        if len(b64) * 3 // 4 > max_bytes:
            return FileResult(ok=False, error="too_large", mimetype=self._mimetype(meta, res),
                              file_name=self._file_name(msg_id, meta, self._mimetype(meta, res)))
        mimetype = self._mimetype(meta, res)
        return FileResult(ok=True, error=None, mimetype=mimetype,
                          file_name=self._file_name(msg_id, meta, mimetype), base64=b64)

    async def send_to_owner(self, msg_id: str, *, caption: str = "") -> FileResult:
        owner = getattr(self.settings, "owner_jid", "") or ""
        if not owner:
            log.error("history send: no owner_jid configured")
            return FileResult(ok=False, error="failed")
        f = await self.fetch(msg_id, max_bytes=SEND_MAX_BYTES)
        if not f.get("ok"):
            return FileResult(ok=False, error=f.get("error") or "failed",
                              mimetype=f.get("mimetype"), file_name=f.get("file_name"))
        mimetype = f.get("mimetype") or "application/octet-stream"
        try:
            sent = await self.evolution.send_media_id(
                send_target(owner), mediatype=mediatype_for(mimetype), mimetype=mimetype,
                media_b64=f["base64"], filename=f.get("file_name") or "file", caption=caption or "")
        except Exception as exc:
            log.warning("history send failed: %s", type(exc).__name__)
            sent = None
        if sent is None:
            return FileResult(ok=False, error="failed", mimetype=mimetype,
                              file_name=f.get("file_name"))
        return FileResult(ok=True, error=None, mimetype=mimetype, file_name=f.get("file_name"),
                          sent_id=sent)

    # --- what a message is ----------------------------------------------------------------

    async def _meta(self, msg_id: str) -> Optional[dict]:
        """{type, mimetype, filename, ts} from Evolution's record of the message; None when it
        cannot be read (the download is then tried anyway)."""
        if msg_id in self._meta_cache:
            return self._meta_cache[msg_id]
        meta = None
        if self.store is not None:
            try:
                hit = await self.store.get_message(msg_id)
            except Exception as exc:
                log.warning("history media index lookup failed: %s", type(exc).__name__)
                hit = None
            if hit:
                kind = hit.get("kind")
                meta = {"type": _INDEX_KIND_TO_TYPE.get(kind, kind), "mimetype": hit.get("mimetype"),
                        "filename": hit.get("file_name"), "ts": int(hit.get("ts") or 0)}
        finder = getattr(self.evolution, "_find_messages", None)
        if meta is None and finder is not None:
            try:
                rows = await finder({"key": {"id": msg_id}})
                row = next((r for r in rows or []
                            if isinstance(r, dict) and (r.get("key") or {}).get("id") == msg_id),
                           None)
                if row is not None:
                    info = media_info(row.get("message"))
                    meta = {**info, "ts": int(row.get("messageTimestamp") or 0)}
            except Exception as exc:
                log.warning("history media lookup failed: %s", type(exc).__name__)
        if len(self._meta_cache) > 512:
            self._meta_cache.clear()
        self._meta_cache[msg_id] = meta
        return meta

    async def _gone_or_failed(self, msg_id: str) -> str:
        meta = await self._meta(msg_id)
        ts = (meta or {}).get("ts") or 0
        return "expired" if ts and time.time() - ts > EXPIRED_AFTER_SECONDS else "failed"

    @staticmethod
    def _mimetype(meta: Optional[dict], res: Optional[dict]) -> str:
        """The download's mimetype, unless it is the client's audio default for a message that
        is not audio; then the one the message declared."""
        got = (res or {}).get("mimetype") or ""
        declared = (meta or {}).get("mimetype") or ""
        if got and not (got.startswith("audio/ogg") and meta and meta.get("type") != "audio"):
            return got
        return declared or got or "application/octet-stream"

    @staticmethod
    def _file_name(msg_id: str, meta: Optional[dict], mimetype: str) -> str:
        name = (meta or {}).get("filename")
        if name:
            return name
        ext = mimetypes.guess_extension((mimetype or "").split(";")[0].strip()) or ""
        if ext == ".oga":
            ext = ".ogg"
        return f"whatsapp-{msg_id[:12]}{ext}"


def mediatype_for(mimetype: str) -> str:
    """How Evolution should send it: photos and videos as themselves, audio as audio, the rest as
    a document (which keeps its file name)."""
    base = (mimetype or "").split(";")[0].strip().lower()
    if base in _SEND_AS_IMAGE:
        return "image"
    if base in _SEND_AS_VIDEO:
        return "video"
    if base.startswith("audio/"):
        return "audio"
    return "document"
