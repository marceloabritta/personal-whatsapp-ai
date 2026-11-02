"""carry — a fresh tag that finds the owner's yes under the card runs the held write.

The card went out, he answered "sim", and the yes never reached the held write: the window had
closed under it (it ran out, or the model closed it). So he tags Lisa again — "@lisa pode
seguir", "@lisa agendar" — and a fresh tag starts a new loop, which threw the held write away.
The model then read the chat, saw the card and the yes, and proposed the same write again. It may
not approve a write itself (state.APPROVED_BY), so the same card went out a second time and he
had to say yes twice.

`carried_yes` reads the chat the tag just fetched and decides, in code, whether the yes under the
card stands. It is the yes the window would have run, so it is held to the window's own rule —
the owner's clean affirmative, never a contact's — plus what makes "the chat above" unambiguous:

  * the card is the one that asked about THIS write (fingerprint), is recent, is in the fetched
    history, and nothing Lisa said came after it;
  * every message of his after the card is a clean yes, and nobody else said anything but a yes
    before his first one (a contact's "pode ser 16h?" makes his "sim" an answer to them);
  * the tag's own words are nothing, the card's own command word ("agendar" under a booking card),
    or a clean yes — and only a clean yes ("@lisa pode agendar") is a yes on its own;
  * a booking or a change has not already started.

Anything short of that returns None and the tag resets as before: the model re-proposes and the
card goes out again. The worst case is one more card, never a write he did not approve. Only the
calendar carries — its resolved-id gate needs exactly one id kept (see `kept_on_carry`); a skill
with other gate state would need its own.
"""
from __future__ import annotations

import time
from datetime import date, datetime

from .identity import is_own_message, matched_tag
from .nodes.confirm import action_fingerprint
from .prompt import today_in

CARRYABLE = {"calendar"}


def _find_card(records: list[dict], card: dict) -> int | None:
    """Index of the card in the fetched history: by WhatsApp id, else by its exact framed text."""
    cid = card.get("id")
    if cid:
        for i in range(len(records) - 1, -1, -1):
            if records[i].get("id") == cid:
                return i
    text = (card.get("text") or "").strip()
    if text:
        for i in range(len(records) - 1, -1, -1):
            r = records[i]
            if r.get("from_me") and (r.get("text") or "").strip() == text:
                return i
    return None


def _started(action: dict, now: float, tz_name: str) -> bool:
    """Has the write's event already begun? A cancellation of a past event is still fine."""
    if action.get("task", "").endswith(".delete"):
        return False
    start = action.get("start")
    if not start:
        return False
    try:
        dt = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return True  # cannot tell, so do not run it blind
    if dt.tzinfo is None:  # an all-day date
        return dt.date() < date.fromisoformat(today_in(tz_name))
    return dt.timestamp() < now


def carried_yes(state: dict, records: list[dict], *, policy, echoes, settings,
                now: float | None = None) -> tuple[dict | None, str]:
    """({yes_id, card_id, age_s}, "ok") when the yes under the card stands, else (None, reason)."""
    now = time.time() if now is None else now
    pending = state.get("pending_action")
    card = state.get("pending_card")
    if not pending:
        return None, "no_pending"
    if pending.get("task", "").partition(".")[0] not in CARRYABLE or policy is None:
        return None, "not_carryable"
    if not card:
        return None, "no_card"
    if card.get("sig") != action_fingerprint(pending):
        return None, "card_mismatch"
    age = int(now - int(card.get("ts") or 0))
    if age > settings.carry_confirm_max_age_seconds:
        return None, "stale"
    if _started(pending, now, settings.calendar_timezone):
        return None, "started"
    idx = _find_card(records, card)
    if idx is None:
        return None, "card_not_found"

    jid = state.get("remote_jid")
    owner = settings.owner_name
    trigger_id = state.get("msg_id")
    yes_id = None
    for r in records[idx + 1:]:
        if r.get("id") == trigger_id:
            continue
        text = (r.get("text") or "").strip()
        if echoes.is_ours(jid, r.get("id")) or is_own_message(text, owner):
            return None, "lisa_spoke_after"
        verdict = policy.detect(text, pending)
        if r.get("from_me"):
            if verdict != "yes":
                return None, "owner_said_other"
            yes_id = yes_id or r.get("id")
        elif yes_id is None and verdict != "yes":
            return None, "contact_spoke_first"

    tag_text = state.get("text") or ""
    tag = matched_tag(tag_text, settings.tags)
    rest = tag_text[len(tag):].strip(" ,.:;!-") if tag else tag_text.strip()
    if rest:
        if policy.detect(rest) == "yes":
            yes_id = yes_id or trigger_id  # "@lisa pode agendar" is the yes itself
        elif policy.detect(rest, pending) != "yes":
            return None, "tag_other"  # a correction, a new request — the model's to read
        # else the card's own command word: a wake-up, which needs the yes above it
    if yes_id is None:
        return None, "no_yes"
    return {"yes_id": yes_id, "card_id": card.get("id"), "age_s": age}, "ok"


def kept_on_carry(state: dict) -> dict:
    """What survives the tag's reset when the yes carries: the write, its riders and its card,
    the language the card was written in (the success card is rendered in code, and the new
    loop's language is otherwise only locked by the model), and — for a change or a cancellation —
    the one event id the resolved-id gate needs. Nothing else from the old loop. The new loop is
    that write's skill's loop, as `route` would have made it: the router never runs on this path,
    and a keyword-less follow-up ("e o endereço?") must not be re-decided by the classifier."""
    pending = state["pending_action"]
    riders = list(state.get("pending_side_effects") or [])
    keep = {
        "loop_domain": pending.get("task", "").partition(".")[0],
        "pending_action": pending,
        "pending_side_effects": riders,
        "side_effects": riders,
        "pending_card": state.get("pending_card"),
        "last_confirm_sig": state.get("last_confirm_sig"),
        "session_lang": state.get("session_lang"),
    }
    eid = pending.get("event_id")
    if eid and eid in (state.get("seen_event_ids") or []):
        keep["seen_event_ids"] = [eid]
        view = (state.get("seen_events") or {}).get(eid)
        keep["seen_events"] = {eid: view} if view else {}
    return keep
