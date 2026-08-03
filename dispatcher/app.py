"""Dispatcher — the ONE webhook Evolution posts to, fanning out to the flows by tag.

One WhatsApp number → one Evolution instance → one webhook URL. Evolution cannot route
by message content, so this thin service reads the summon @tag (and per-chat window
ownership) and forwards the *raw, unmodified* payload to the flow that owns the turn:

    @lisa                              ─▶  Lisa flow   (LangGraph brain, :8000)
    (untagged)                         ─▶  the chat's current owner-flow, if its window is live

Adding a new experimental flow is one line: drop another (tags, url) route below (or wire it
via env) and point a fresh @tag at it — the core @lisa flow keeps running, untouched, so a
new feature can never break it.

Routing invariants (mirror each flow's own gate, which stays as the final arbiter):
  - a tag only *summons* when it comes from the OWNER (fromMe) — a contact typing "@mary"
    does not open a window; it can only continue one already open;
  - the tag always wins and (re)assigns ownership of the chat;
  - an untagged message follows the last owner while that chat's window is live;
  - a flow's own echoed reply (recognised by its header, not a tag) never re-summons and
    never self-extends the window.

The helpers below are copied verbatim from `Lisa flow/app/{whatsapp,identity}.py` so the
dispatcher parses payloads and detects tags/own-messages exactly as the flows do — and
stays a standalone service with no cross-folder imports."""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("dispatcher")


# ── payload / tag helpers (copied from the flows) ───────────────────────────
def extract_text(msg: dict | None) -> str:
    """Text from an Evolution `message` object (several possible shapes)."""
    if not msg:
        return ""
    return (
        msg.get("conversation")
        or (msg.get("extendedTextMessage") or {}).get("text")
        or (msg.get("imageMessage") or {}).get("caption")
        or (msg.get("videoMessage") or {}).get("caption")
        or ""
    )


_LEADING_MARKERS = "*_~ \t\r\n"


def _header_for(owner_name: str, lang: str = "en") -> str:
    if (lang or "en").lower().startswith("pt"):
        return f"[Assistente IA do {owner_name}]:"
    return f"[{owner_name}'s AI Assistant]:"


def _all_headers(owner_name: str) -> list[str]:
    return [_header_for(owner_name, "en"), _header_for(owner_name, "pt")]


def _ends_tag(ch: str) -> bool:
    return ch == "" or not (ch.isalnum() or ch == "_")


def matched_tag(text: str, tags: list[str]) -> str | None:
    """The trigger tag this text starts with, or None. Longest-first; must end the word."""
    low = (text or "").lower()
    for tag in sorted(tags, key=len, reverse=True):
        if low.startswith(tag) and _ends_tag(low[len(tag) : len(tag) + 1]):
            return tag
    return None


def is_own_message(text: str, owner_name: str) -> bool:
    """Is this one of a flow's OWN replies (echoed back by Evolution as fromMe)?"""
    t = (text or "").lstrip(_LEADING_MARKERS)
    return any(t.startswith(h) for h in _all_headers(owner_name))


# ── config ──────────────────────────────────────────────────────────────────
def _tags(raw: str) -> list[str]:
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


class Config:
    def __init__(self) -> None:
        self.owner_name = os.getenv("OWNER_NAME", "Marcelo")
        self.window_ttl = int(os.getenv("WINDOW_TTL", "120"))
        self.redis_url = os.getenv("REDIS_URL") or None
        # Ordered routes: first tag-set that matches wins. Env-overridable so the
        # tag→URL wiring lives with the deployment, not in code. To add a new flow,
        # append another (tags, url) entry here and point a fresh @tag at it — the
        # core @lisa route stays untouched, so a new feature can't break it.
        self.routes = [
            (
                _tags(os.getenv("LISA_TAGS", "@lisa")),
                os.getenv("LISA_URL", "http://lisa:8000/webhook"),
            ),
        ]

    def route_for_tag(self, text: str) -> str | None:
        """The forward URL whose tag-set this text is summoned with, or None."""
        for tags, url in self.routes:
            if matched_tag(text, tags):
                return url
        return None


# ── ownership store: which flow owns a chat's open window (Redis, mem fallback) ─
class OwnerStore:
    """chat jid → forward URL, with a TTL window. Redis when reachable (survives
    restarts, shared if the dispatcher is ever scaled), in-memory otherwise."""

    def __init__(self, redis_url: str | None, ttl: int) -> None:
        self.ttl = ttl
        self._redis = None
        self._mem: dict[str, tuple[str, float]] = {}
        if redis_url:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(redis_url, decode_responses=True)
            except Exception:  # no redis pkg / bad url → memory fallback
                self._redis = None

    @staticmethod
    def _key(jid: str) -> str:
        return f"disp:owner:{jid}"

    async def set(self, jid: str, url: str) -> None:
        if self._redis is not None:
            await self._redis.set(self._key(jid), url, ex=self.ttl)
        else:
            self._mem[jid] = (url, time.time() + self.ttl)

    async def get(self, jid: str) -> str | None:
        if self._redis is not None:
            return await self._redis.get(self._key(jid))
        item = self._mem.get(jid)
        if not item:
            return None
        url, expiry = item
        if time.time() >= expiry:
            self._mem.pop(jid, None)
            return None
        return url


cfg = Config()
app = FastAPI(title="Flow dispatcher", version="1.0.0")


@app.on_event("startup")
async def _startup() -> None:
    app.state.owners = OwnerStore(cfg.redis_url, cfg.window_ttl)
    app.state.http = httpx.AsyncClient(timeout=10.0)
    log.info(
        '{"boot":"dispatcher","redis":%s,"ttl":%d,"routes":%d}',
        "true" if cfg.redis_url else "false",
        cfg.window_ttl,
        len(cfg.routes),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state.http.aclose()


@app.get("/")
async def health() -> dict:
    return {
        "ok": True,
        "service": "dispatcher",
        "routes": [{"tags": t, "url": u} for t, u in cfg.routes],
        "window_ttl": cfg.window_ttl,
    }


async def _forward(url: str, body: dict) -> None:
    try:
        await app.state.http.post(url, json=body)
    except Exception:  # a downstream hiccup must never crash the dispatcher
        log.exception('{"forward":"failed","url":"%s"}', url)


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    body = await request.json()
    data = body.get("data") or body
    key = data.get("key") or {}
    jid = key.get("remoteJid") or ""
    from_me = bool(key.get("fromMe"))
    text = extract_text(data.get("message")).strip()

    owners: OwnerStore = app.state.owners

    # 1) OWNER summon by tag → (re)assign ownership and forward.
    if from_me:
        url = cfg.route_for_tag(text)
        if url:
            await owners.set(jid, url)
            background.add_task(_forward, url, body)
            log.info('{"route":"tag","jid":"%s","url":"%s"}', jid, url)
            return Response(status_code=200)

    # 2) Continuation → follow the chat's current owner-flow, if any.
    url = await owners.get(jid)
    if url:
        # Refresh the window on genuine conversation, but never on a flow's own echo.
        if not is_own_message(text, cfg.owner_name):
            await owners.set(jid, url)
        background.add_task(_forward, url, body)
        log.info('{"route":"window","jid":"%s","url":"%s"}', jid, url)
        return Response(status_code=200)

    # 3) No tag, no open window → nothing to do.
    log.info('{"route":"drop","jid":"%s","from_me":%s}', jid, str(from_me).lower())
    return Response(status_code=200)
