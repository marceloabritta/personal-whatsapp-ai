"""The ONE internal Evolution client.

Every Evolution call goes through here — nodes never issue raw HTTP and never see
a URL, API key, or instance name. Ported from secretary/1. Orchestrator/lib/evolution.js.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..whatsapp import extract_text, is_audio_message

log = logging.getLogger("mary.evolution")


class Evolution:
    def __init__(
        self,
        url: str,
        apikey: str,
        instance: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 20.0,
    ) -> None:
        self.base = url.rstrip("/")
        self.instance = instance
        self.timeout = timeout
        self._headers = {"Content-Type": "application/json", "apikey": apikey}
        self._client = client  # inject in tests; otherwise a client is made per call

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        url = f"{self.base}{path}"
        if self._client is not None:
            return await self._client.post(url, json=payload, headers=self._headers)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.post(url, json=payload, headers=self._headers)

    async def send_text(self, number: str, text: str) -> Optional[str]:
        """POST /message/sendText/{instance}. Sends RAW text — the reply header is
        stamped by the caller (act node), never here.

        Returns the sent WhatsApp message id on success so the caller can record it
        for echo-filtering; an empty string if the send was 2xx but no id came back
        (still a success); None on failure."""
        try:
            resp = await self._post(
                f"/message/sendText/{self.instance}", {"number": number, "text": text}
            )
        except httpx.HTTPError as exc:
            log.error("sendText transport error: %s", exc)
            return None
        if resp.status_code >= 400:
            log.error("sendText failed %s: %s", resp.status_code, resp.text[:500])
            return None
        try:
            data = resp.json()
        except ValueError:
            return ""
        key = (data.get("key") if isinstance(data, dict) else None) or {}
        return key.get("id") or ""

    async def send_media(
        self, number: str, *, mediatype: str, mimetype: str, media_b64: str,
        filename: str, caption: str,
    ) -> bool:
        """POST /message/sendMedia/{instance}. Delivers a document (e.g. a long transcript
        as a .txt). The caption is framed by the caller, never here. Returns True on 2xx."""
        try:
            resp = await self._post(
                f"/message/sendMedia/{self.instance}",
                {"number": number, "mediatype": mediatype, "mimetype": mimetype,
                 "media": media_b64, "fileName": filename, "caption": caption},
            )
        except httpx.HTTPError as exc:
            log.error("sendMedia transport error: %s", exc)
            return False
        if resp.status_code >= 400:
            log.error("sendMedia failed %s: %s", resp.status_code, resp.text[:500])
            return False
        return True

    async def get_media_base64(
        self, message_id: str, *, convert_to_mp4: bool = False
    ) -> Optional[dict]:
        """POST /chat/getBase64FromMediaMessage/{instance}. Downloads and decrypts a media
        message's bytes. Returns {"base64": str, "mimetype": str} on success, None on failure
        (never raises into the graph — consistent with send_text)."""
        try:
            resp = await self._post(
                f"/chat/getBase64FromMediaMessage/{self.instance}",
                {"message": {"key": {"id": message_id}}, "convertToMp4": convert_to_mp4},
            )
        except httpx.HTTPError as exc:
            log.error("getBase64FromMediaMessage transport error: %s", exc)
            return None
        if resp.status_code >= 400:
            log.error("getBase64FromMediaMessage failed %s: %s",
                      resp.status_code, resp.text[:500])
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        b64 = data.get("base64") if isinstance(data, dict) else None
        if not b64:
            log.error("getBase64FromMediaMessage: no base64 in response")
            return None
        return {"base64": b64, "mimetype": data.get("mimetype") or "audio/ogg"}

    async def _find_messages(self, where: dict) -> list[dict]:
        """One findMessages page. Returns [] on any failure so one bad query can't
        take down the other in fetch_history."""
        try:
            resp = await self._post(
                f"/chat/findMessages/{self.instance}", {"where": where}
            )
            if resp.status_code >= 400:
                return []
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("findMessages error: %s", exc)
            return []
        if isinstance(data, list):
            return data
        msgs = data.get("messages") if isinstance(data, dict) else None
        if isinstance(msgs, dict):
            return msgs.get("records") or []
        return (data.get("records") if isinstance(data, dict) else None) or []

    async def fetch_history(self, remote_jid: str) -> list[dict]:
        """Conversation history, oldest→newest, normalised to
        {id, from_me, text, push_name, ts}.

        WhatsApp LID addressing: a 1:1 chat's inbound messages persist under the
        contact's `…@lid` JID while we send to the phone `…@s.whatsapp.net`. Querying
        `remoteJid` alone returns only our own outbound; Evolution records the phone
        JID as `key.remoteJidAlt` on the LID rows, so we ask both ways and merge."""
        import asyncio

        pages = await asyncio.gather(
            self._find_messages({"key": {"remoteJid": remote_jid}}),
            self._find_messages({"key": {"remoteJidAlt": remote_jid}}),
        )
        by_id: dict[str, dict] = {}
        for row in [r for page in pages for r in page]:
            key = row.get("key") or {}
            rid = key.get("id")
            if not rid:
                continue
            by_id[rid] = {
                "id": rid,
                "from_me": bool(key.get("fromMe")),
                "text": extract_text(row.get("message")).strip(),
                "push_name": row.get("pushName"),
                "ts": int(row.get("messageTimestamp") or 0),
                # Provenance: a voice note carries no text here — context transcribes it
                # and the transcript is annotated as audio-sourced downstream.
                "is_audio": is_audio_message(row.get("message")),
            }
        return sorted(by_id.values(), key=lambda r: r["ts"])
