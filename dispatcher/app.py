"""Dispatcher — the ONE webhook Evolution posts to, fanning out to the flows by tag.

One WhatsApp number → one Evolution instance → one webhook URL. Evolution cannot route
by message content, so this thin service reads the summon @tag (and per-chat window
ownership) and forwards the *raw, unmodified* payload to the flow that owns the turn:

    @lisa                              ─▶  Lisa flow   (LangGraph brain, :8000)
    (untagged)                         ─▶  the chat's current owner-flow, if its window is live
    (his own "fix it" / "skip")        ─▶  FIX_REPLY_URL, if configured and no window is live
    (any voice note)                   ─▶  AUTO_TRANSCRIBE_URL, additionally, if configured

Adding a new experimental flow is one line: drop another (tags, url) route below (or wire it
via env) and point a fresh @tag at it — the core @lisa flow keeps running, untouched, so a
new feature can never break it.

Routing invariants (mirror each flow's own gate, which stays as the final arbiter):
  - a tag only *summons* when it comes from the OWNER (fromMe) — a contact typing "@lisa"
    does not open a window; it can only continue one already open;
  - the tag always wins and (re)assigns ownership of the chat;
  - an untagged message follows the last owner while that chat's window is live;
  - a flow's own echoed reply (recognised by its header, not a tag) never re-summons and
    never self-extends the window.

A forward that cannot reach its flow is tried again for a short while, in order per chat
(`Forwarder`): a flow being redeployed is gone for some seconds, and the message sent in
those seconds is as real as any other.

A message that reaches the dispatcher long after it was sent (MAX_MESSAGE_AGE_SECONDS) is not
acted on at all: no forward, no window, no transcription. Only a backlog arrives that late,
and by then he has moved on. While such a backlog is pouring in (`DrainBreaker`), nothing is
acted on, not even messages that look fresh: in a replay some carry their delivery time.

The helpers below are copied verbatim from `Lisa flow/app/{whatsapp,identity}.py` so the
dispatcher parses payloads and detects tags/own-messages exactly as the flows do — and
stays a standalone service with no cross-folder imports."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from typing import Any

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


def is_audio_message(msg: dict | None) -> bool:
    """True for a WhatsApp voice note (copied from Lisa flow/app/whatsapp.py)."""
    if not msg:
        return False
    return "audioMessage" in msg or "pttMessage" in msg


def message_age(data: dict, now: float | None = None) -> float | None:
    """Seconds since WhatsApp says this message was sent, or None when the payload has no time."""
    raw = data.get("messageTimestamp")
    if isinstance(raw, dict):  # a protobuf Long, should Evolution ever pass one through
        raw = raw.get("low")
    try:
        sent = float(raw)
    except (TypeError, ValueError):
        return None
    if sent <= 0:
        return None
    if sent > 1e12:  # milliseconds, not seconds
        sent /= 1000
    return (time.time() if now is None else now) - sent


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


# His answer to a fix plan (Lisa flow/app/fixdesk/): "fix it", "Fix it F12", "skip". Only the
# opening words are read here; the desk reads the rest and decides whether it is an answer.
_FIX_ANSWER = re.compile(r"^\s*(fix\s+it|skip)\b", re.IGNORECASE)


def is_fix_answer(text: str) -> bool:
    return bool(_FIX_ANSWER.match(text or ""))


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
        # Automatic transcription needs to see voice notes that carry NO tag and open no window,
        # which every rule below would otherwise drop. When this is wired, audio is additionally
        # forwarded to that flow — which decides for itself whether the chat is enrolled. Leave
        # it unset and the dispatcher behaves exactly as it did before.
        self.auto_transcribe_url = os.getenv("AUTO_TRANSCRIBE_URL") or None
        # The fix desk. Lisa sends him fix plans in his chat with himself, and he answers with a
        # plain "fix it" or "skip": no tag, and her own message opens no window, so the rules
        # below would drop it. When this is set, a message of HIS that opens with one of those
        # words, and falls under no tag and no live window, is forwarded here. It claims no
        # window. The desk acts only in his chat with himself; from any other chat the forward
        # costs one POST and stops there. Unset, the dispatcher behaves exactly as before.
        self.fix_reply_url = os.getenv("FIX_REPLY_URL") or None
        # How long a forward is tried again while its flow cannot be reached (see Forwarder).
        # It has to outlast a flow's whole restart: up to 120 s finishing the turns it had in
        # flight (stop_grace_period on the lisa service) and about 15 s to boot. "0" tries
        # once and gives up, which is what the dispatcher did before it retried.
        self.forward_retry = float(os.getenv("FORWARD_RETRY_SECONDS", "150"))
        # A message older than this when it reaches the dispatcher is not acted on: no forward,
        # no window, no transcription. Live messages arrive within seconds; only a backlog
        # comes later. On 2026-11-13/14 WhatsApp held this number's incoming messages for up
        # to 25 h (a Baileys bug, evolution-api #2778), each reconnect replayed a slice, and
        # Lisa answered day-old requests as if he had just asked. Ten minutes leaves room for
        # a slow phone; "0" turns the guard off.
        self.max_message_age = int(os.getenv("MAX_MESSAGE_AGE_SECONDS", "600"))
        # Drain breaker (see DrainBreaker). One stale message is a late phone; many in a row is
        # a backlog being replayed, and in a replay his own re-sent messages can carry the time
        # they were delivered instead of the time he sent them. Once DRAIN_TRIGGER stale
        # messages arrive within DRAIN_WINDOW_SECONDS, nothing is acted on, fresh-looking or
        # not, until DRAIN_QUIET_SECONDS pass with no stale arrival. "0" turns it off.
        self.drain_trigger = int(os.getenv("DRAIN_TRIGGER", "5"))
        self.drain_window = float(os.getenv("DRAIN_WINDOW_SECONDS", "60"))
        self.drain_quiet = float(os.getenv("DRAIN_QUIET_SECONDS", "180"))

    def route_for_tag(self, text: str) -> str | None:
        """The forward URL whose tag-set this text is summoned with, or None."""
        for tags, url in self.routes:
            if matched_tag(text, tags):
                return url
        return None


# ── ownership store: which flow owns a chat's open window (memory, plus Redis) ─
class OwnerStore:
    """chat jid → forward URL, with a TTL window.

    Memory always holds the window. With REDIS_URL set, Redis holds it too, so a window
    survives a dispatcher restart. Redis is asked first; when it fails, or has nothing for the
    chat, memory answers. A Redis failure is logged and never raised: the webhook has to queue
    its forward whatever state Redis is in. After a failure Redis is left alone for
    REDIS_RETRY_AFTER seconds, so the messages of an outage do not each wait on a timeout.

    Memory is this process's own copy, so falling back to it assumes ONE dispatcher process,
    which is what the compose file runs."""

    REDIS_TIMEOUT = 1.0  # seconds to connect, and to wait for each reply
    REDIS_RETRY_AFTER = 5.0  # seconds Redis is left alone after it fails
    MEM_SWEEP_AT = 2048  # windows held in memory before the lapsed ones are swept out

    def __init__(self, redis_url: str | None, ttl: int) -> None:
        self.ttl = ttl
        self._redis = None
        self._mem: dict[str, tuple[str, float]] = {}
        self._redis_down = False  # the last Redis call failed
        self._skip_until = 0.0  # time.monotonic() before which Redis is not tried
        if redis_url:
            try:
                import redis.asyncio as aioredis

                # from_url opens no connection: a Redis that is down shows up at the first call.
                self._redis = aioredis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=self.REDIS_TIMEOUT,
                    socket_timeout=self.REDIS_TIMEOUT,
                )
            except Exception:  # no redis pkg / bad url → memory only
                self._redis = None

    @staticmethod
    def _key(jid: str) -> str:
        return f"disp:owner:{jid}"

    def state(self) -> str:
        """Where windows are being kept: "redis", "memory" (none configured) or
        "memory_redis_down"."""
        if self._redis is None:
            return "memory"
        return "memory_redis_down" if self._redis_down else "redis"

    async def _redis_call(self, command: str, *args: Any, **kw: Any) -> tuple[bool, Any]:
        """One Redis command. (True, reply) when Redis answered; (False, None) when there is
        no Redis, it is being left alone after a failure, or it failed just now."""
        if self._redis is None or time.monotonic() < self._skip_until:
            return False, None
        try:
            reply = await getattr(self._redis, command)(*args, **kw)
        except Exception as exc:  # down, restarting, loading, timing out: memory answers
            self._skip_until = time.monotonic() + self.REDIS_RETRY_AFTER
            if not self._redis_down:  # one line per outage, not one per message
                self._redis_down = True
                error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning(json.dumps({"owners": "redis_down", "error": error}))
            return False, None
        if self._redis_down:
            self._redis_down = False
            log.info('{"owners":"redis_back"}')
        return True, reply

    async def ping(self) -> str:
        """Try Redis once, then say where windows are being kept (see `state`)."""
        await self._redis_call("ping")
        return self.state()

    async def set(self, jid: str, url: str) -> None:
        if len(self._mem) >= self.MEM_SWEEP_AT:
            now = time.time()
            self._mem = {j: item for j, item in self._mem.items() if item[1] > now}
        # Memory first, so its copy lapses no later than Redis's: when Redis says a window is
        # over, memory must not bring it back.
        self._mem[jid] = (url, time.time() + self.ttl)
        await self._redis_call("set", self._key(jid), url, ex=self.ttl)

    async def get(self, jid: str) -> str | None:
        answered, url = await self._redis_call("get", self._key(jid))
        if answered and url:
            return url
        # Redis failed, or has nothing: a window opened while it was down exists only here.
        item = self._mem.get(jid)
        if not item:
            return None
        url, expiry = item
        if time.time() >= expiry:
            self._mem.pop(jid, None)
            return None
        return url


# ── drain breaker: a replayed backlog is not acted on, even where it looks fresh ─
class DrainBreaker:
    """Notices a backlog being replayed, and holds every message until the replay is over.

    The age guard (Config.max_message_age) reads the time WhatsApp put on each message. In a
    replay that time can lie. On 2026-11-15 a patched Evolution released 25 h of held messages
    in about 15 minutes; the guard dropped some 1,000 of them, but 133 of his own phone
    messages from that day came in stamped with their delivery time, all within two minutes,
    and Lisa answered or transcribed eight of them into chats with other people. What the
    replay does show is a flood of messages that are honestly old. So once `trigger` stale
    messages arrive within `window` seconds, every message is held (no forward, no window, no
    transcription) until `quiet` seconds pass with no stale one. A message he really sends in
    those minutes is held too; he tags her again once it is over. Lisa still reads all of
    them from Evolution's store as context the next time she is called.
    """

    def __init__(self, trigger: int, window: float, quiet: float, clock: Any = time.monotonic) -> None:
        self.trigger = trigger
        self.window = window
        self.quiet = quiet
        self.clock = clock
        self._recent: deque[float] = deque()  # arrival times of the latest stale messages
        self._last_stale: float | None = None
        self._since: float | None = None  # when the drain under way began
        self._held_now = 0
        self.drains = 0
        self.held = 0

    def saw_stale(self) -> None:
        now = self.clock()
        self._last_stale = now
        self._recent.append(now)
        while self._recent and now - self._recent[0] > self.window:
            self._recent.popleft()
        if self.trigger and self._since is None and len(self._recent) >= self.trigger:
            self._since = now
            self._held_now = 0
            self.drains += 1
            log.warning(json.dumps({"drain": "started", "stale_in_window": len(self._recent),
                                    "window_s": self.window}))

    def draining(self) -> bool:
        """Is a replay under way? Ends it (and says so) once it has been quiet long enough."""
        if self._since is None:
            return False
        now = self.clock()
        if self._last_stale is not None and now - self._last_stale >= self.quiet:
            log.warning(json.dumps({"drain": "ended", "held": self._held_now,
                                    "lasted_s": int(now - self._since)}))
            self._since = None
            self._recent.clear()
            return False
        return True

    def hold(self) -> None:
        self._held_now += 1
        self.held += 1

    def state(self) -> dict:
        draining = self.draining()
        return {"draining": draining, "held_this_drain": self._held_now if draining else 0,
                "drains": self.drains, "held": self.held, "trigger": self.trigger,
                "window_s": self.window, "quiet_s": self.quiet}


# ── forwarding: every message reaches its flow, in order, even across a restart ─
class Forwarder:
    """Delivers each forward to its flow, and keeps trying while the flow cannot be reached.

    The webhook used to POST once and drop the message on any failure. A flow that is being
    redeployed has no listener for about twelve seconds, and for up to two minutes more when
    it first finishes a turn it had in flight: on 2026-11-11 four forwards were refused in two
    such gaps (00:33 and 00:41 UTC). All four happened to be messages the flow
    would have ignored, but a tagged order or his "sim" to a card would have gone the same way,
    with nothing to tell him.

    So a forward that fails because the flow could not be reached, or answers 502/503/504, is
    tried again with a short pause, for FORWARD_RETRY_SECONDS (150 unless set) counted from
    when the message arrived. Any other answer means the flow has it, and it is never sent
    again. A retry cannot
    double a message that did arrive: the flow drops a message id it has already seen
    (`_already_seen` in Lisa flow/app/main.py), and a flow that restarted in between never saw it.

    Order is kept per chat and per flow. Each (flow, chat) has its own line and one message of
    it in flight at a time, so a "sim" never reaches the flow before the order it answers, and
    one chat waiting on a retry holds no other chat back.

    The lines live in this process's memory. A dispatcher restart forgets them, and they are
    bounded: past MAX_PER_CHAT waiting for one chat, or MAX_PENDING in all, a new message is
    dropped and logged rather than queued. Nothing here logs what a message says."""

    # Seconds before each new try; the last one repeats. Two at most, so a flow that is back
    # has its messages within two seconds, at a cost of one try every two seconds meanwhile.
    BACKOFF = (0.5, 1.0, 2.0)
    RETRY_STATUS = frozenset({502, 503, 504})  # the flow, or something in front of it, is not up
    MAX_PER_CHAT = 200  # a lively chat with an open window sends some twenty a minute
    MAX_PENDING = 1000

    def __init__(self, http: Any, retry_for: float = 150.0) -> None:
        self._http = http
        self.retry_for = retry_for
        self._lines: dict[tuple[str, str], deque] = {}
        self._workers: dict[tuple[str, str], asyncio.Task] = {}
        self._pending = 0  # accepted and not yet delivered or given up
        self.counts = {"delivered_late": 0, "gave_up": 0, "dropped": 0}

    def state(self) -> dict:
        """For the health route: what is waiting now, and what the retries have done so far."""
        return {"pending": self._pending, "retry_seconds": self.retry_for, **self.counts}

    def submit(self, url: str, body: dict) -> bool:
        """Take one message for `url`. False when there was no room and it was dropped."""
        jid = ((body.get("data") or body).get("key") or {}).get("remoteJid") or ""
        key = (url, jid)
        line = self._lines.get(key)
        if self._pending >= self.MAX_PENDING or (line is not None and len(line) >= self.MAX_PER_CHAT):
            self.counts["dropped"] += 1
            log.error(json.dumps({"forward": "dropped", "reason": "queue_full",
                                  "url": url, "jid": jid, "pending": self._pending}))
            return False
        if line is None:
            line = self._lines[key] = deque()
        line.append((body, time.monotonic()))
        self._pending += 1
        if key not in self._workers:
            self._workers[key] = asyncio.get_running_loop().create_task(self._drain(key))
        return True

    async def _drain(self, key: tuple[str, str]) -> None:
        """Send one chat's line, oldest first, one message in flight at a time."""
        url, jid = key
        line = self._lines[key]
        held_until = 0.0  # a message that arrived before this stood in line behind a retry
        try:
            while line:
                body, arrived = line[0]
                tries = await self._deliver(url, jid, body, arrived, behind=arrived < held_until)
                if tries > 1:
                    held_until = time.monotonic()
                line.popleft()
                self._pending -= 1
        finally:
            # Nothing awaits between the loop ending and these two lines, so a message that
            # arrives next finds no line and no worker, and starts both again.
            self._pending -= len(line)  # only when cancelled at shutdown: the rest is abandoned
            self._lines.pop(key, None)
            self._workers.pop(key, None)

    async def _deliver(self, url: str, jid: str, body: dict, arrived: float,
                       behind: bool = False) -> int:
        """Send one message, trying again while the flow is away. Returns how many tries it
        took. `behind` says it waited in line for an earlier message's retry, so it is late
        even when its own first try goes through."""
        attempt = 0
        while True:
            attempt += 1
            try:
                status = (await self._http.post(url, json=body)).status_code
            except httpx.TransportError as exc:  # refused, reset, timed out: nobody answered
                error = type(exc).__name__
            except Exception:  # not something waiting can fix; must never crash the dispatcher
                log.exception('{"forward":"failed","url":"%s"}', url)
                return attempt
            else:
                if status not in self.RETRY_STATUS:
                    if attempt > 1 or behind:
                        self.counts["delivered_late"] += 1
                        log.info(json.dumps({
                            "forward": "delivered", "url": url, "jid": jid, "attempts": attempt,
                            "waited": round(time.monotonic() - arrived, 1)}))
                    if status >= 400:  # the flow answered, and said no: its call, not retried
                        log.warning(json.dumps({"forward": "rejected", "url": url, "jid": jid,
                                                "status": status}))
                    return attempt
                error = f"HTTP {status}"
            waited = time.monotonic() - arrived
            pause = self.BACKOFF[min(attempt, len(self.BACKOFF)) - 1]
            if waited + pause > self.retry_for:
                self.counts["gave_up"] += 1
                log.error(json.dumps({"forward": "gave_up", "url": url, "jid": jid,
                                      "attempts": attempt, "waited": round(waited, 1),
                                      "error": error}))
                return attempt
            if attempt == 1:  # one line per message, not one per try
                log.warning(json.dumps({"forward": "retrying", "url": url, "jid": jid,
                                        "error": error}))
            await asyncio.sleep(pause)

    async def aclose(self) -> None:
        """Stop trying. What is still waiting is abandoned, as it would be by a restart."""
        workers = list(self._workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        # A worker cancelled before it ever ran never reaches its own cleanup.
        self._lines.clear()
        self._workers.clear()
        self._pending = 0


cfg = Config()
breaker = DrainBreaker(cfg.drain_trigger, cfg.drain_window, cfg.drain_quiet)
app = FastAPI(title="Flow dispatcher", version="1.0.0")


@app.on_event("startup")
async def _startup() -> None:
    app.state.owners = OwnerStore(cfg.redis_url, cfg.window_ttl)
    app.state.http = httpx.AsyncClient(timeout=10.0)
    app.state.forwards = Forwarder(app.state.http, cfg.forward_retry)
    log.info(
        '{"boot":"dispatcher","redis":%s,"store":"%s","ttl":%d,"routes":%d,"max_message_age":%d,'
        '"drain":{"trigger":%d,"window_s":%d,"quiet_s":%d}}',
        "true" if cfg.redis_url else "false",
        await app.state.owners.ping(),
        cfg.window_ttl,
        len(cfg.routes),
        cfg.max_message_age,
        cfg.drain_trigger,
        cfg.drain_window,
        cfg.drain_quiet,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state.forwards.aclose()
    await app.state.http.aclose()


@app.get("/")
async def health() -> dict:
    forwards = getattr(app.state, "forwards", None)  # made at startup
    return {
        "ok": True,
        "service": "dispatcher",
        "routes": [{"tags": t, "url": u} for t, u in cfg.routes],
        "auto_transcribe_url": cfg.auto_transcribe_url,
        "fix_reply_url": cfg.fix_reply_url,
        "window_ttl": cfg.window_ttl,
        "max_message_age": cfg.max_message_age,
        "drain": breaker.state(),
        "owner_store": app.state.owners.state(),
        "forwards": forwards.state() if forwards else None,
    }


async def _forward(url: str, body: dict) -> None:
    """Hand one message to the Forwarder, which sends it and retries if the flow is away.
    Returns at once: the sending happens on the chat's own line (Forwarder._drain)."""
    app.state.forwards.submit(url, body)


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    body = await request.json()
    data = body.get("data") or body
    key = data.get("key") or {}
    jid = key.get("remoteJid") or ""
    from_me = bool(key.get("fromMe"))
    text = extract_text(data.get("message")).strip()

    # Late arrivals are not acted on (Config.max_message_age). A payload with no send time is
    # treated as live: dropping it would be the worse mistake.
    age = message_age(data)
    if cfg.max_message_age and age is not None and age > cfg.max_message_age:
        breaker.saw_stale()
        log.info('{"route":"stale","jid":"%s","from_me":%s,"age_s":%d}', jid, str(from_me).lower(), int(age))
        return Response(status_code=200)
    # While a backlog is being replayed, a fresh-looking message is not acted on either
    # (DrainBreaker): in a replay its timestamp may be when it was delivered.
    if breaker.draining():
        breaker.hold()
        log.info('{"route":"draining","jid":"%s","from_me":%s}', jid, str(from_me).lower())
        return Response(status_code=200)

    owners: OwnerStore = app.state.owners
    # Forwarded already this request, so a message matching two rules is POSTed once.
    forwarded: set[str] = set()

    # 0) AUDIO always reaches the auto-transcription flow — tagged or not, window or not. It
    #    deliberately does NOT claim ownership of the chat: transcribing a voice note is not a
    #    conversation, and it must not steer where the chat's next untagged message goes. The
    #    flow itself decides whether that chat is enrolled; a voice note from anywhere else
    #    costs one POST and stops at its gate.
    if cfg.auto_transcribe_url and is_audio_message(data.get("message")):
        background.add_task(_forward, cfg.auto_transcribe_url, body)
        forwarded.add(cfg.auto_transcribe_url)
        log.info('{"route":"audio","jid":"%s","url":"%s"}', jid, cfg.auto_transcribe_url)

    # 1) OWNER summon by tag → (re)assign ownership and forward.
    if from_me:
        url = cfg.route_for_tag(text)
        if url:
            await owners.set(jid, url)
            if url not in forwarded:
                background.add_task(_forward, url, body)
            log.info('{"route":"tag","jid":"%s","url":"%s"}', jid, url)
            return Response(status_code=200)

    # 2) Continuation → follow the chat's current owner-flow, if any.
    url = await owners.get(jid)
    if url:
        # Refresh the window on genuine conversation, but never on a flow's own echo.
        if not is_own_message(text, cfg.owner_name):
            await owners.set(jid, url)
        if url not in forwarded:
            background.add_task(_forward, url, body)
        log.info('{"route":"window","jid":"%s","url":"%s"}', jid, url)
        return Response(status_code=200)

    # 3) His answer to a fix plan → the fix desk. After the window rule on purpose: while he is
    #    talking with a flow in this chat, "fix it" is part of that conversation.
    if (from_me and cfg.fix_reply_url and is_fix_answer(text)
            and not is_own_message(text, cfg.owner_name)):
        if cfg.fix_reply_url not in forwarded:
            background.add_task(_forward, cfg.fix_reply_url, body)
        log.info('{"route":"fix","jid":"%s","url":"%s"}', jid, cfg.fix_reply_url)
        return Response(status_code=200)

    # 4) No tag, no open window → nothing to do (the audio rule above may still have fired).
    if not forwarded:
        log.info('{"route":"drop","jid":"%s","from_me":%s}', jid, str(from_me).lower())
    return Response(status_code=200)
