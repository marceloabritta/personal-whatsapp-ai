"""The fix desk: the part of the fix loop that lives in the Lisa process.

It does three things, none of them with a model:

  opens a case     when the reviewer finishes a session that has a mistake in it;
  sends            each finished plan to his chat with himself, and later what came of it;
  reads his answer "fix it" or "skip", forwarded here by the dispatcher.

The investigating and the fixing happen on his Mac, in Claude Code, once a day (`fixer/` at the
repo root). That worker and this desk meet only in the queue (`store.py`).

Rails, the same as the review layer's: its own task and its own pool, nothing in the reply path
awaits it, and every exception is swallowed. The desk must never delay, alter or break a reply."""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from ..identity import frame, is_own_message
from ..whatsapp import chat_key, extract_text, get_quoted
from . import text as T
from .store import FixStore

log = logging.getLogger("mary.fixdesk")

WORKER_STALE_HOURS = 36   # past this, her "On it" says the Mac has not checked in


class FixDesk:
    def __init__(self, store: FixStore, evolution: Any, settings: Any,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.store = store
        self.evolution = evolution
        self.s = settings
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._seen: "OrderedDict[str, None]" = OrderedDict()   # answers already handled, by id

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.s.fix_desk_seconds)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a broken pass must never end the task
                log.warning("fix desk pass failed: %s", exc)

    # -- helpers ---------------------------------------------------------------
    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(ZoneInfo(self.s.calendar_timezone))

    def _number(self) -> str:
        """Where the desk writes: his chat with himself. "" until his own JID is known."""
        return (self.s.owner_jid or "").split("@")[0].split(":")[0]

    async def _send(self, body: str) -> Optional[str]:
        number = self._number()
        if not number:
            return None
        return await self.evolution.send_text(
            number, frame(body, self.s.owner_name, self.s.fix_lang))

    # -- the trigger (called by the reviewer; must never raise) -----------------
    async def note_graded(self, loop_id: str, judge_version: str) -> None:
        try:
            codes = await self.store.qualifying_codes(loop_id, judge_version)
            if not codes:
                return
            case_id = await self.store.open_case(loop_id, judge_version, codes)
            if case_id:
                log.info('{"fix":"case","id":%d,"loop_id":"%s","codes":%d}',
                         case_id, loop_id, len(codes))
        except Exception as exc:
            log.warning("fix case not opened for %s: %s", loop_id, exc)

    # -- sending ------------------------------------------------------------------
    async def tick(self) -> dict:
        """One pass: plans that are ready, then results, then plans nobody answered."""
        done = {"proposed": 0, "results": 0, "expired": 0}
        if not self._number():
            return done
        if not T.in_quiet_hours(self._now(), self.s.fix_quiet_from, self.s.fix_quiet_until):
            room = self.s.fix_daily_cap - await self.store.proposed_today(self.s.calendar_timezone)
            for case in await self.store.due_proposals(room):
                wa_id = await self._send(T.proposal_body(case["id"], case["plan"] or {}))
                if wa_id is None:   # the send failed: leave it planned, the next pass tries again
                    break
                await self.store.mark_proposed(case["id"], wa_id)
                done["proposed"] += 1
                log.info('{"fix":"proposed","id":%d}', case["id"])
            for case in await self.store.due_results():
                wa_id = await self._send(T.result_body(case["id"], case["result"] or {}))
                if wa_id is None:
                    break
                await self.store.mark_result_sent(case["id"], wa_id)
                done["results"] += 1
                log.info('{"fix":"result_sent","id":%d,"status":"%s"}', case["id"], case["status"])
        done["expired"] = await self.store.expire(self.s.fix_expire_days)
        return done

    # -- his answer -----------------------------------------------------------------
    def _first_time(self, msg_id: Optional[str]) -> bool:
        if not msg_id:
            return True
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = None
        if len(self._seen) > 500:
            self._seen.popitem(last=False)
        return True

    async def handle_reply(self, body: dict) -> str:
        """One message the dispatcher took for an answer. Returns what was done with it, for the
        log and the tests. Anything that is not his own answer, in his own chat, about a case
        that exists, is dropped without a word."""
        try:
            return await self._handle(body)
        except Exception as exc:
            log.warning("fix reply failed: %s", exc)
            return "error"

    async def _handle(self, body: dict) -> str:
        data = body.get("data") or body
        key = data.get("key") or {}
        if not key.get("fromMe"):
            return "not_owner"
        owner = self.s.owner_key
        chats = {chat_key(key.get("remoteJid")), chat_key(key.get("remoteJidAlt"))}
        if not owner or owner not in chats:
            return "not_self_chat"
        text = extract_text(data.get("message")).strip()
        if is_own_message(text, self.s.owner_name):
            return "own_message"
        cmd = T.parse_command(text)
        if cmd is None:
            return "not_an_answer"
        if not self._first_time(key.get("id")):
            return "seen"

        quoted = get_quoted(data)
        case = await self.store.find_for_reply((quoted or {}).get("id"), cmd.case_id)
        if case is None:
            return "no_case"
        cid, status = case["id"], case["status"]

        if cmd.kind == "skip":
            if status == "proposed" and await self.store.dismiss(cid):
                await self._send(T.skipped_body(cid))
                return "dismissed"
            return "skip_ignored"

        if status == "proposed":
            if not await self.store.approve(cid, cmd.note, key.get("id")):
                return "lost_race"
            await self._send(T.ack_body(cid, run_at=self.s.fix_run_at,
                                        worker_note=await self._worker_note()))
            log.info('{"fix":"approved","id":%d,"note":%s}', cid, "true" if cmd.note else "false")
            return "approved"
        if status in ("approved", "fixing", "ready", "shipped"):
            await self._send(T.already_body(cid, status))
            return "already"
        return "closed"

    async def _worker_note(self) -> str:
        """A line for the "On it" message when the Mac has not been heard from."""
        try:
            seen = await self.store.worker_seen()
        except Exception:
            return ""
        if not seen:
            return "Your Mac has not checked in yet, so it waits until it does."
        info = seen.get("info") or {}
        if info.get("ok") is False:
            why = str(info.get("why") or "it cannot run Claude Code").rstrip(".")
            return f"It waits, though: {why}."
        age = datetime.now(timezone.utc) - seen["seen_at"]
        if age.total_seconds() > WORKER_STALE_HOURS * 3600:
            days = max(1, round(age.total_seconds() / 86400))
            return f"Your Mac last checked in {days} day{'s' if days != 1 else ''} ago."
        return ""
