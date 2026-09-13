"""The setup tool — CRUD over the auto-transcription roster.

One handler behind the `execute` node. It never raises into the graph: every failure comes back
as an ActionResult with `ok=False` and a classified `error`, so the reply path can speak
truthfully about what happened (the same contract tools/calendar.py honours).

What this handler does NOT decide: whether the chat is the owner's own (the resolve gate, in
skills/setup.py, refuses everything else), whether the owner approved (the confirm policy), or
how the result reads as a message (the render policy). It reads and writes rows.

The prose below is the model's whole briefing for this domain."""
from __future__ import annotations

import logging
from typing import Any, Optional

from .. import chatfind
from ..roster import (
    SCOPE_KIND, make_rule, make_scope, normalize_direction, normalize_scope,
)
from ..whatsapp import chat_kind
from .base import ActionResult

log = logging.getLogger("mary.setup")

DESCRIBE = ("Configure which chats have their voice notes transcribed automatically — one at a "
            "time, or all contacts / all groups at once. {owner_name}'s setup, available only in "
            "his chat with himself.")

GUIDANCE = """You are running SETUP: {owner_name} is configuring how his assistant behaves. This only ever happens in his chat with himself, so there is no one else in the conversation and no one else to consider.

One thing is configurable today: **Transcription** — which chats get their voice notes transcribed automatically, and in which direction. A chat is set to one of exactly three directions. **Always offer them as this numbered list, in this order**, so he can answer with a single digit:

1. **inbound** — audio the other person sends
2. **outbound** — audio {owner_name} sends
3. **in & out** — both

Write the direction in exactly those words. In an action, send `direction` as `"inbound"`, `"outbound"` or `"both"`. When he replies with a bare digit, it means that line: 1 = inbound, 2 = outbound, 3 = in & out.

How a chat is named, and this is not negotiable:

- **A contact is added by forwarding their contact card.** When a card arrives you will see a line like `[contact card: Mãe · 5511976004417]` in the transcript, and the number in it is the `chat_key`. If {owner_name} asks to add a person WITHOUT sending a card, ask him to forward the card — never look a person up by name, and never type a phone number he did not send you.
- **A card may carry several numbers.** When it does, the line reads `several numbers, ask which:` followed by a numbered list. Show him that list and ask which one — he answers with the digit. Never pick for him, and never ask him to forward the card again: every number on it is already available to you, so re-reading the line you were given is always better than asking for it twice. A number marked "not on WhatsApp" cannot receive anything; say so rather than offering it.
- **A group is added by name.** Call `setup.resolve` with what he called it. It returns real groups from his chat list, most recently active first, each with a number. Show them numbered and ask which; never pick for him. **Then enrol it by that number**: send `ordinal: 1` and LEAVE `chat_key` OUT entirely. `chat_key` is optional — never invent a value for it, never put the group's name or a placeholder there. The same goes for `setup.update` and `setup.remove`.

**Blanket rules.** Besides individual chats, two defaults cover whole categories: `all_contacts` (every 1:1) and `all_groups` (every group). Set one by using it as the `chat_key`: `setup.enroll` with `chat_key: "all_contacts"`, `direction: "outbound"`. Clear it with `setup.remove` and the same key.

A chat's own rule and the blanket rule for its kind ADD UP — either one covering a direction is enough. So "all_contacts: outbound" plus "Mãe: in & out" means everyone gets {owner_name}'s audio written out, and Mãe's own audio comes back as well. Setting a chat does NOT switch the blanket rule off for it. If {owner_name} asks for something that would need a chat EXCLUDED from a blanket rule, say plainly that it is not possible yet — clearing the blanket rule is the only way — rather than pretending it worked.

**When he asks to add something without saying what** — "add", "how do I add a group", "how does this work" — call `setup.help` and NOTHING else. It answers with the whole how-to in one message: the contact card, the group name, the blanket rules, and the three directions. Do not write that explanation yourself and do not give him half of it; the verb exists so the answer is the same every time.

Working with the list:

- `setup.list` shows everything configured — blanket rules, then contacts, then groups, under separate titles but numbered in one continuous sequence. Those numbers are handles: after a list, "edit 5 to outbound" or "remove 2" targets a row by `ordinal`. Pass the number he said as `ordinal` — do not try to reconstruct a chat_key from it.
- Opening setup with nothing else to do: show the menu and the current list together, so he sees the state immediately.

Confirmation. `enroll`, `update` and `remove` all change his configuration, so they need his go-ahead. Emit the action with `confirmed: false` and say nothing about it — the system composes the confirmation question, shows it to him, and runs the action itself when he agrees. Never claim something is registered before it has run; a result line tells you it happened.

Keep every message short. State what changed, or ask the one question you need."""

# The setup items. A second item (quiet hours, reply language) drops in here.
ITEMS = [
    {"key": "transcription", "title": "Transcription",
     "summary": "Auto-transcribe voice notes — per chat, or across all contacts / all groups."},
]

_DIRECTION_HELP = 'direction must be one of: inbound, outbound, both (shown as "in & out")'


class RosterService:
    """Local handler for the six setup verbs. `roster` and `evolution` are attached after
    construction by deps (the skills fan-out builds handlers from settings alone)."""

    _VERBS = ("menu", "help", "list", "resolve", "enroll", "update", "remove")

    def __init__(self, settings, *, roster: Any = None, evolution: Any = None) -> None:
        self.s = settings
        self.roster = roster
        self.evolution = evolution
        self._groups_cache: Optional[list[dict]] = None

    async def run(self, verb: str, inputs: dict) -> ActionResult:
        if verb not in self._VERBS:
            return {"ok": False, "error": "unknown_verb", "summary": f"no setup verb {verb!r}"}
        if self.roster is None:
            return {"ok": False, "error": "store_unavailable",
                    "summary": "Setup is not available — the roster is not configured."}
        try:
            return await getattr(self, f"_{verb}")(inputs or {})
        except Exception as exc:  # never raise into the graph
            log.exception("setup.%s failed: %s", verb, exc)
            return {"ok": False, "error": "store_unavailable",
                    "summary": f"Could not complete setup.{verb} — the change was not saved."}

    # --- reads ---------------------------------------------------------------------------
    async def _menu(self, inputs: dict) -> ActionResult:
        return {"ok": True, "data": {"items": list(ITEMS)},
                "summary": "Setup items: " + ", ".join(i["title"] for i in ITEMS)}

    async def _help(self, inputs: dict) -> ActionResult:
        return {"ok": True, "data": {"help": True},
                "summary": "Explained the three ways to add: a contact card, a group name, "
                           "or all contacts / all groups."}

    async def _list(self, inputs: dict) -> ActionResult:
        """Everything configured, numbered continuously across all three sections — blanket
        rules first, then contacts, then groups. The ordinals are published in `data` so the
        execute node can remember them; that is what makes "edit 5" resolvable."""
        await self._sync()
        scopes = self.roster.scopes()
        rules = self.roster.snapshot()
        contacts = [r for r in rules if r.get("kind") != "group"]
        groups = [r for r in rules if r.get("kind") == "group"]

        ordinals: dict[str, str] = {}
        numbered_s, numbered_c, numbered_g, n = [], [], [], 0
        for bucket, out in ((scopes, numbered_s), (contacts, numbered_c), (groups, numbered_g)):
            for rule in bucket:
                n += 1
                ordinals[str(n)] = rule["chat_key"]
                out.append({**rule, "n": n})

        every = numbered_s + numbered_c + numbered_g
        return {
            "ok": True,
            "data": {"scopes": numbered_s, "contacts": numbered_c, "groups": numbered_g,
                     "ordinals": ordinals, "seen_keys": [r["chat_key"] for r in scopes + rules],
                     "total": len(rules), "scope_total": len(scopes)},
            "summary": ("; ".join(f"{r['n']}. {r.get('label') or r['chat_key']} "
                                  f"({r['direction']})" for r in every)
                        if every else "Nothing is configured for auto-transcription yet."),
        }

    async def _resolve(self, inputs: dict) -> ActionResult:
        """Find GROUPS by name. Contacts are never resolved here — they arrive as a card."""
        if self.evolution is None:
            return {"ok": False, "error": "evolution_unavailable",
                    "summary": "Could not read the chat list."}
        groups = await self._groups()
        if groups is None:
            return {"ok": False, "error": "evolution_unavailable",
                    "summary": "Could not read your group list from WhatsApp just now."}
        if not groups:
            return {"ok": False, "error": "no_groups",
                    "summary": "No groups found in your chat list."}

        query = (inputs.get("query") or "").strip()
        found = chatfind.rank(query, groups, limit=self.s.setup_group_candidates)
        cands = [{"n": i + 1, "chat_key": g["chat_key"], "chat_jid": g.get("chat_jid") or "",
                  "label": g.get("label") or g["chat_key"], "kind": "group",
                  "last_ts": g.get("last_ts") or 0, "size": g.get("size")}
                 for i, g in enumerate(found["candidates"])]

        head = (f'{len(cands)} group(s) match "{query}", most recently active first: '
                if found["matched"] else
                "No group matched that name. Your most recently active groups, in order: ")
        return {
            "ok": True,
            # `ordinals` makes the candidates addressable BY NUMBER, the same way the list is.
            # Without it the model had to copy an opaque group id into the next action, which is
            # exactly the thing it gets wrong — it sent the group's name instead and the write
            # gate (correctly) refused it.
            "data": {"candidates": cands, "matched": found["matched"],
                     "ordinals": {str(c["n"]): c["chat_key"] for c in cands},
                     "seen_keys": [c["chat_key"] for c in cands]},
            "summary": head + "; ".join(
                f"{c['n']}. {c['label']}"
                + (f" ({c['size']} people)" if c.get("size") else "") for c in cands),
        }

    # --- writes --------------------------------------------------------------------------
    async def _enroll(self, inputs: dict) -> ActionResult:
        key = (inputs.get("chat_key") or "").strip()
        direction = normalize_direction(inputs.get("direction"))
        scope = normalize_scope(key)
        if scope:
            # A blanket rule: the default for a whole KIND of chat. It names no chat, so none of
            # the resolution below applies.
            if direction is None:
                return {"ok": False, "error": "invalid_direction", "summary": _DIRECTION_HELP}
            await self._sync()
            existed = self.roster.get(scope)
            rule = make_scope(scope, direction)
            await self.roster.upsert(rule)
            return {"ok": True,
                    "data": {**rule, "existed": bool(existed), "seen_keys": [scope]},
                    "summary": f"{scope} set to {direction}."}
        if not key:
            return {"ok": False, "error": "unresolved_id",
                    "summary": "No chat to enrol — forward a contact card, or name a group first."}
        if direction is None:
            return {"ok": False, "error": "invalid_direction", "summary": _DIRECTION_HELP}

        await self._sync()
        existing = self.roster.get(key)
        jid = inputs.get("chat_jid") or (existing or {}).get("chat_jid") or self._jid_for(key)
        rule = make_rule(
            chat_key=key, chat_jid=jid, direction=direction,
            kind=(existing or {}).get("kind") or chat_kind(jid),
            label=inputs.get("label") or (existing or {}).get("label"),
            alt_key=(existing or {}).get("alt_key") or inputs.get("alt_key"),
        )
        await self.roster.upsert(rule)
        label = rule.get("label") or key
        # Re-adding an existing chat is an edit, not an error: "add Mãe as in & out" when she is
        # already inbound is plainly a direction change, and saying so is more useful than a
        # refusal.
        changed = bool(existing) and existing.get("direction") != direction
        return {"ok": True,
                "data": {**rule, "existed": bool(existing), "changed": changed,
                         "seen_keys": [key]},
                "summary": (f"{label} updated to {direction}." if existing
                            else f"{label} enrolled ({direction}).")}

    async def _update(self, inputs: dict) -> ActionResult:
        key = normalize_scope(inputs.get("chat_key")) or (inputs.get("chat_key") or "").strip()
        direction = normalize_direction(inputs.get("direction"))
        if direction is None:
            return {"ok": False, "error": "invalid_direction", "summary": _DIRECTION_HELP}
        await self._sync()
        before = self.roster.get(key)
        if before is None:
            return {"ok": False, "error": "not_found",
                    "summary": "That chat is not on the list."}
        after = await self.roster.set_direction(key, direction)
        label = after.get("label") or key
        return {"ok": True,
                "data": {**after, "from": before["direction"], "to": direction,
                         "seen_keys": [key]},
                "summary": f"{label}: {before['direction']} -> {direction}."}

    async def _remove(self, inputs: dict) -> ActionResult:
        key = normalize_scope(inputs.get("chat_key")) or (inputs.get("chat_key") or "").strip()
        await self._sync()
        gone = await self.roster.remove(key)
        if gone is None:
            return {"ok": False, "error": "not_found",
                    "summary": "That chat is not on the list."}
        remaining = self.roster.snapshot()
        label = gone.get("label") or key
        if gone.get("kind") == SCOPE_KIND:
            return {"ok": True,
                    "data": {**gone, "remaining": remaining, "scopes": self.roster.scopes(),
                             "removed": True},
                    "summary": f"{key} cleared."}
        return {"ok": True,
                "data": {**gone, "remaining": remaining, "removed": True},
                "summary": f"{label} removed. {len(remaining)} chat(s) still enrolled."}

    # --- helpers -------------------------------------------------------------------------
    async def _sync(self) -> None:
        """Make sure the snapshot is current before a read or a write decides anything."""
        try:
            await self.roster.refresh(force=True)
        except Exception as exc:  # a stale snapshot beats a failed turn
            log.warning("roster refresh before setup op failed: %s", exc)

    async def _groups(self) -> Optional[list[dict]]:
        """The owner's groups with subjects and last activity, cached for this handler's life
        within a loop. None when Evolution could not be read at all."""
        if self._groups_cache is not None:
            return self._groups_cache
        chats = await self.evolution.find_chats()
        groups = await self.evolution.fetch_groups()
        if not chats and not groups:
            return None
        self._groups_cache = chatfind.merge_groups(chats, groups)
        return self._groups_cache

    def _jid_for(self, key: str) -> str:
        """Best-known JID for a key we are enrolling. A group id is only ever a group JID; a
        contact key is a phone number."""
        for row in (self._groups_cache or []):
            if row.get("chat_key") == key and row.get("chat_jid"):
                return row["chat_jid"]
        return f"{key}@g.us" if len(key) > 15 and "-" in key else f"{key}@s.whatsapp.net"

    def forget_groups(self) -> None:
        self._groups_cache = None
