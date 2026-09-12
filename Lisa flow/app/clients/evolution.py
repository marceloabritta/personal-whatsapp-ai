"""The ONE internal Evolution client.

Every Evolution call goes through here — nodes never issue raw HTTP and never see
a URL, API key, or instance name. Ported from secretary/1. Orchestrator/lib/evolution.js.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..whatsapp import chat_key, contact_cards, extract_text, media_info

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

    async def _get(self, path: str, params: Optional[dict] = None) -> httpx.Response:
        url = f"{self.base}{path}"
        if self._client is not None:
            return await self._client.get(url, params=params, headers=self._headers)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.get(url, params=params, headers=self._headers)

    async def send_text(self, number: str, text: str, *, quoted: Optional[dict] = None) -> Optional[str]:
        """POST /message/sendText/{instance}. Sends RAW text — the reply header is
        stamped by the caller (act node), never here.

        `quoted` is the message key to reply to — {"remoteJid", "fromMe", "id"} — which threads
        the reply under that message in WhatsApp. Omitted, the message is sent unattached.

        Returns the sent WhatsApp message id on success so the caller can record it
        for echo-filtering; an empty string if the send was 2xx but no id came back
        (still a success); None on failure."""
        payload: dict = {"number": number, "text": text}
        if quoted:
            payload["quoted"] = {"key": quoted}
        try:
            resp = await self._post(f"/message/sendText/{self.instance}", payload)
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
        filename: str, caption: str, quoted: Optional[dict] = None,
    ) -> bool:
        """POST /message/sendMedia/{instance}. Delivers a document (e.g. a long transcript
        as a .txt). The caption is framed by the caller, never here. Returns True on 2xx."""
        payload: dict = {"number": number, "mediatype": mediatype, "mimetype": mimetype,
                         "media": media_b64, "fileName": filename, "caption": caption}
        if quoted:
            payload["quoted"] = {"key": quoted}
        try:
            resp = await self._post(f"/message/sendMedia/{self.instance}", payload)
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

    async def find_chats(self) -> list[dict]:
        """Every chat Evolution knows about, normalised to {jid, key, kind, label, last_ts}.

        This is the only source that carries LAST ACTIVITY, which is what lets group search rank
        "the group I was just in" above a better string match from March. Returns [] on any
        failure — a resolver with no chats degrades to "I could not read your chat list", never
        to a wrong guess."""
        try:
            resp = await self._post(f"/chat/findChats/{self.instance}", {})
            if resp.status_code >= 400:
                log.error("findChats failed %s: %s", resp.status_code, resp.text[:300])
                return []
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("findChats error: %s", exc)
            return []

        rows = data if isinstance(data, list) else (
            (data.get("chats") or data.get("records") or []) if isinstance(data, dict) else []
        )
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            jid = row.get("remoteJid") or row.get("id") or ""
            if not jid:
                continue
            out.append({
                "jid": jid,
                "key": chat_key(jid),
                "kind": "group" if jid.endswith("@g.us") else "contact",
                "label": (row.get("pushName") or row.get("name") or row.get("subject") or "").strip(),
                "last_ts": _as_ts(row.get("updatedAt") or row.get("lastMessageTimestamp")
                                  or row.get("messageTimestamp")),
            })
        return out

    async def fetch_groups(self) -> list[dict]:
        """Group subjects + sizes, as [{jid, key, label, size}]. Merged onto find_chats rows by
        key. Returns [] on failure."""
        try:
            resp = await self._get(f"/group/fetchAllGroups/{self.instance}",
                                   {"getParticipants": "false"})
            if resp.status_code >= 400:
                log.error("fetchAllGroups failed %s: %s", resp.status_code, resp.text[:300])
                return []
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("fetchAllGroups error: %s", exc)
            return []

        rows = data if isinstance(data, list) else (
            (data.get("groups") or []) if isinstance(data, dict) else []
        )
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            jid = row.get("id") or ""
            if not jid:
                continue
            out.append({"jid": jid, "key": chat_key(jid),
                        "label": (row.get("subject") or "").strip(),
                        "size": row.get("size") or row.get("participantsCount")})
        return out

    async def fetch_owner_jid(self) -> Optional[str]:
        """The JID of the account this instance is logged in as — used to recognise the owner's
        chat with himself when OWNER_JID is not configured. None on any failure."""
        try:
            resp = await self._get("/instance/fetchInstances", {"instanceName": self.instance})
            if resp.status_code >= 400:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("fetchInstances error: %s", exc)
            return None
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            inst = row.get("instance") if isinstance(row.get("instance"), dict) else row
            jid = inst.get("ownerJid") or inst.get("owner")
            if jid:
                return jid
        return None

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
        {id, from_me, text, push_name, ts, is_audio, media_type, media_mimetype, media_filename}.

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
            info = media_info(row.get("message"))
            by_id[rid] = {
                "id": rid,
                "from_me": bool(key.get("fromMe")),
                "text": extract_text(row.get("message")).strip(),
                "push_name": row.get("pushName"),
                "ts": int(row.get("messageTimestamp") or 0),
                # Provenance: a voice note carries no text here — context transcribes it
                # and the transcript is annotated as audio-sourced downstream.
                "is_audio": info["type"] == "audio",
                # Image/PDF provenance — context downloads the bytes and attaches a block;
                # declared mimetype/filename gate PDFs and title the document block.
                "media_type": info["type"],
                "media_mimetype": info["mimetype"],
                "media_filename": info["filename"],
                # Forwarded contact cards — the only way a contact is enrolled for
                # auto-transcription, so the key has to survive a context reseed.
                "contact_cards": contact_cards(row.get("message")),
            }
        return sorted(by_id.values(), key=lambda r: r["ts"])


def _as_ts(value) -> int:
    """A last-activity value as a unix timestamp. Evolution returns either an epoch (seconds or
    milliseconds) or an ISO string depending on the field; anything unreadable sorts oldest."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v / 1000) if v > 1e11 else int(v)  # milliseconds -> seconds
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0


def send_target(remote_jid: str) -> str:
    """What to pass as `number` when sending to this chat.

    A 1:1 takes the bare phone number, which is what every existing send does. A GROUP must take
    the full `…@g.us` JID — the group id alone is not addressable."""
    return remote_jid if (remote_jid or "").endswith("@g.us") else (remote_jid or "").split("@")[0]
