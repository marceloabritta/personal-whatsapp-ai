"""The ONE internal Evolution client.

Every Evolution call goes through here — nodes never issue raw HTTP and never see
a URL, API key, or instance name. Ported from secretary/1. Orchestrator/lib/evolution.js.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from ..whatsapp import extract_text

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
        trace: Any = None,
    ) -> None:
        self.base = url.rstrip("/")
        self.instance = instance
        self.timeout = timeout
        self._headers = {"Content-Type": "application/json", "apikey": apikey}
        self._client = client  # inject in tests; otherwise a client is made per call
        self._trace = trace  # optional Trace, for control-stream IO events

    def _io(self, api: str, **fields: Any) -> None:
        if self._trace is not None:
            self._trace.io(api, **fields)  # reads the current trace id from the contextvar

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        url = f"{self.base}{path}"
        if self._client is not None:
            return await self._client.post(url, json=payload, headers=self._headers)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.post(url, json=payload, headers=self._headers)

    async def send_text(self, number: str, text: str) -> bool:
        """POST /message/sendText/{instance}. Sends RAW text — the reply header is
        stamped by the caller (act node), never here. Returns True on 2xx."""
        t0 = time.monotonic()
        try:
            resp = await self._post(
                f"/message/sendText/{self.instance}", {"number": number, "text": text}
            )
        except httpx.HTTPError as exc:
            log.error("sendText transport error: %s", exc)
            self._io("evolution.send_text", ok=False, status=None,
                     ms=int((time.monotonic() - t0) * 1000), error=str(exc))
            return False
        ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            log.error("sendText failed %s: %s", resp.status_code, resp.text[:500])
            self._io("evolution.send_text", ok=False, status=resp.status_code, ms=ms)
            return False
        self._io("evolution.send_text", ok=True, status=resp.status_code, ms=ms)
        return True

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

        t0 = time.monotonic()
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
            }
        out = sorted(by_id.values(), key=lambda r: r["ts"])
        self._io("evolution.fetch_history", count=len(out),
                 ms=int((time.monotonic() - t0) * 1000))
        return out
