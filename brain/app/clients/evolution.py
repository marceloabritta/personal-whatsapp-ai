"""The ONE internal Evolution client.

Every Evolution call goes through here — nodes never issue raw HTTP and never see
a URL, API key, or instance name. Base URL, auth, and error handling live in this
single place. Ported from secretary/1. Orchestrator/lib/evolution.js.

Step 1 needs exactly one call: send_text. fetch_history / send_media / get_media
come back as later steps need them.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

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

    async def send_text(self, number: str, text: str) -> bool:
        """POST /message/sendText/{instance}. Sends RAW text — the reply header is
        stamped by the caller (ack node), never here. Returns True on 2xx."""
        url = f"{self.base}/message/sendText/{self.instance}"
        payload = {"number": number, "text": text}
        try:
            if self._client is not None:
                resp = await self._client.post(url, json=payload, headers=self._headers)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            log.error("sendText transport error: %s", exc)
            return False

        if resp.status_code >= 400:
            log.error("sendText failed %s: %s", resp.status_code, resp.text[:500])
            return False
        return True
