"""Short links for the event link on her booking card.

Google's event link is about 130 characters (an `eid` that base64-encodes the event id and the
calendar's address), which takes over a WhatsApp message. Google offers no public shortener, so
this asks TinyURL's keyless endpoint for one: tinyurl.com/<code>. The event behind it still needs
a Google sign-in to open; what TinyURL learns is the long link itself.

Never in the way of a booking: any failure, a slow answer or something that does not look like a
TinyURL link gives back the long link, and the card goes out with that."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("mary.tools.shortlink")

_API = "https://tinyurl.com/api-create.php"
_PREFIX = "https://tinyurl.com/"


class ShortLinks:
    def __init__(self, timeout: float = 3.0, client: httpx.AsyncClient | None = None) -> None:
        self.timeout = timeout
        self._client = client  # test seam: an httpx.AsyncClient over a MockTransport
        # The same event's link comes back on every update; one code per link is enough.
        self._seen: dict[str, str] = {}

    async def shorten(self, url: str | None) -> str | None:
        if not url or not url.startswith("https://"):
            return url
        if url in self._seen:
            return self._seen[url]
        try:
            if self._client is not None:
                r = await self._client.get(_API, params={"url": url}, timeout=self.timeout)
            else:
                async with httpx.AsyncClient() as c:
                    r = await c.get(_API, params={"url": url}, timeout=self.timeout)
            short = r.text.strip()
            if r.status_code != 200 or not short.startswith(_PREFIX) or len(short) >= len(url):
                log.warning("shortlink: unusable answer (%s) %r", r.status_code, short[:80])
                return url
        except Exception as exc:  # a card with the long link beats no card
            log.warning("shortlink failed: %s", exc)
            return url
        if len(self._seen) > 500:
            self._seen.clear()
        self._seen[url] = short
        return short
