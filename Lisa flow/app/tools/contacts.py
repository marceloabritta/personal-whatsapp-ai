"""The contacts tool — find, create, update and delete cards in the owner's Google Contacts.

One handler behind the `execute` node, with the same contract as tools/calendar.py and tools/setup.py:
it never raises into the graph, and every failure comes back as an ActionResult with `ok=False` and a
classified `error`, so the reply path can speak truthfully about what happened.

What this handler does NOT decide: whether the owner approved (the confirm policy), who may look
things up and which card an id means (the resolve gate, in skills/contacts.py), or how a result reads
as a message (the render policy).

Every Google mutation runs ONCE. People has no request key, so a blind retry of a create duplicates a
person in the owner's real address book (see tools/people.py). An update is made safe differently: it
reads the card fresh, so a replay finds its work already done. This is NOT calendar's retry ladder.

WHO SEES WHAT. Lisa's replies are posted into the chat, so everyone in it reads them. Outside the
owner's chat with himself, the details of anyone who is not in that chat are masked here, before the
model reads them, so a model that slips still has nothing to leak: it cannot read out what it was
never given. The one exception is an email fetched for an invite (see `_reveal_emails`), because the
invitation carries the guest list anyway — the same reasoning as Directory.block.

The prose below is the model's whole briefing for this domain."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..directory import LEARNED
from .base import ActionResult
from .people import ChangeError, card_view, is_transient

log = logging.getLogger("mary.contacts")

DESCRIBE = ("Keep {owner_name}'s Google Contacts: look someone up, save a new person, change "
            "what is on a card, or delete one.")

GUIDANCE = """Contacts — you keep {owner_name}'s real Google Contacts: find a card, create one, change one (update), or delete one.

HOW YOU WORK HERE — you do NOT write the confirmation or the result. `create`, `update` and `delete` show {owner_name} a card and run on his yes, and the SYSTEM composes and sends both. So emit the action with "message": null, never write a "Confirming:" or "Saved:" text yourself, never re-send an action you already sent, and never say something is done before its result comes back. `find` runs straight away and comes back to you, so you can answer or carry on.
- A card takes one answer. Anything but a clean yes withdraws it, and you are told so: if he corrected it, propose it again with the correction; if he refused or moved on, just answer him.

FINDING THE CARD
- The cards this conversation points at are listed above under "Address book", each with its [id]. For anyone else, emit contacts.find with what he called them: a name, nickname, company, email or phone number.
- `update` and `delete` need the [id] of a card you were shown. A name is not an id, and you never invent one.
- One card fits -> use it. Several fit -> ask which, naming them, and emit no write that turn. Too many to read out -> ask for one detail that narrows it (a surname, the company) and search again.
- None fits -> say so and offer to create the card. The other person in a chat may be saved under another name or number, so search before you conclude he is not on file.

CHANGES — a list of {{"field", "op", "old", "new"}}. Send "" for whichever of "old" and "new" does not apply.
- "field" is one of: name, nickname, email, phone, company, title, address, birthday, notes.
- "update", "change", "new email", "now it is" all mean "op": "replace", with "old" the value being replaced and "new" the value replacing it. Use "add" only when he says "also" or "another": a second email on a card means every future booking has to ask which one to use.
- "remove" takes the value to remove in "old".
- name, company, title, birthday and notes hold one value each: leave "old" as "". "add" on notes appends to what is there, so use it unless he asks to rewrite them.
- A birthday is "YYYY-MM-DD", or "MM-DD" with no year.
- `create` takes the person's "name" and the rest as "changes" with "op": "add". Example: {{"task": "contacts.create", "name": "Ana Silva", "changes": [{{"field": "email", "op": "add", "old": "", "new": "ana@acme.com"}}]}}
- Example: {{"task": "contacts.update", "contact_id": "people/c123", "changes": [{{"field": "email", "op": "replace", "old": "ana@old.com", "new": "ana@acme.com"}}]}}
- NEVER invent an email or a phone number. Write only what was said in this chat; the system refuses a value that is not in the conversation. A value the other person wrote counts: he may be the one telling {owner_name} his new address.

WHO READS THIS — your messages are posted in the chat, and everyone in it reads them.
- Outside {owner_name}'s chat with himself you only see the details of people who are in this chat. Everyone else's are masked (f***@a***.com) or left out. You do not have them, so never guess at them, and do not list what you cannot see.
- If he asks you to read out a masked detail here, tell him in one line to ask in his own chat with you.
- A masked value still works as "old": copy it exactly as you were shown it. It never works as "new".
- His notes on a person are only shown in his chat with himself.
- Only {owner_name} can have you look things up or change a card. If someone else asks, say nothing about it.

Keep every message short: the answer, or the one question you need."""

# How many cards a search shows, and below which it reads each whole card from Google.
_SHOW = 8
_READ_WHOLE = 5


# --- what the room may see -----------------------------------------------------------------

def mask_email(addr: str) -> str:
    local, _, domain = (addr or "").partition("@")
    head, _, tail = domain.partition(".")
    return f"{local[:1]}***@{head[:1]}***.{tail}" if tail else f"{local[:1]}***"


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"••••{digits[-4:]}"


def mask(field: str, value: str) -> str:
    if field == "email":
        return mask_email(value)
    if field == "phone":
        return mask_phone(value)
    return "•••"


def person_view(contact: dict, *, visible: bool, card: dict | None = None,
                self_chat: bool = False) -> dict:
    """A card as this loop remembers it. Holds the real values, for the code that composes a
    confirmation; `visible` says whether they may be shown in this chat. Decided once, when the card
    surfaces, so the composer two nodes later needs nothing but the state.

    `visible` means the person is in this chat, so their own email and phone are no secret here.
    `self_chat` is stricter, and is the only way to see the notes: those are what he wrote ABOUT
    someone, and the last person he wants them read out to is that person, in their own chat."""
    src = card or contact
    view = {
        "resource_name": contact.get("resource_name") or "",
        "name": src.get("name") or contact.get("name") or "",
        "nicknames": list(src.get("nicknames") or []),
        "emails": list(src.get("emails") or []),
        "phones": list(src.get("phones") or []),
        "company": src.get("company") or "",
        "source": contact.get("source") or "google",
        "visible": bool(visible),
        "self_chat": bool(self_chat),
        # Whether this holds the whole card or just the snapshot's fields, so a card about to
        # overwrite a field it never read can say so.
        "whole": bool(card),
    }
    if card:
        for key in ("title", "addresses", "birthday", "notes"):
            view[key] = card.get(key)
    return view


def describe_person(view: dict, *, reveal_emails: bool = False) -> str:
    """One line for the model. The ONLY place a card turns into text it reads, so this is the rail."""
    show = bool(view.get("visible"))
    parts = [f"- {view.get('name') or '(unnamed)'} [{view.get('resource_name')}]"]
    if view.get("company"):
        parts.append(f"company: {view['company']}")
    emails = view.get("emails") or []
    if emails:
        parts.append("email: " + " | ".join(
            emails if (show or reveal_emails) else [mask_email(e) for e in emails]))
    phones = view.get("phones") or []
    if phones:
        parts.append("phone: " + " | ".join(phones if show else [mask_phone(p) for p in phones]))
    if show:
        if view.get("nicknames"):
            parts.append("nickname: " + " | ".join(view["nicknames"]))
        if view.get("title"):
            parts.append(f"title: {view['title']}")
        if view.get("addresses"):
            parts.append("address: " + " | ".join(view["addresses"]))
        if view.get("birthday"):
            parts.append(f"birthday: {view['birthday']}")
        if view.get("notes") and view.get("self_chat"):
            parts.append(f"notes: {view['notes']}")
    if view.get("source") == LEARNED:
        parts.append("known to you only, not yet in Google")
    if not show and not reveal_emails:
        parts.append("details hidden in this chat")
    return " · ".join(parts)


def values_of(view: dict, field: str) -> list:
    """What this card holds for `field`, as far as this loop has read it."""
    key = {"email": "emails", "phone": "phones", "nickname": "nicknames",
           "address": "addresses"}.get(field)
    if key:
        return list(view.get(key) or [])
    value = view.get({"name": "name", "company": "company", "title": "title",
                      "birthday": "birthday", "notes": "notes"}.get(field, ""))
    return [value] if value else []


def unmask_changes(changes: list, view: dict) -> list:
    """Swap a masked `old` for the real value it stands for. The model was only ever shown the mask,
    and Google needs the value. A mask two entries share stays as it is, and the write refuses it."""
    out = []
    for ch in changes or []:
        old = (ch.get("old") or "").strip()
        if old:
            hits = [v for v in values_of(view, ch.get("field"))
                    if mask(ch.get("field"), v) == old]
            if len(hits) == 1:
                ch = {**ch, "old": hits[0]}
        out.append(ch)
    return out


class ContactsService:
    """Local handler for the four contacts verbs. `directory` is attached after construction by deps
    (the skills fan-out builds handlers from settings alone); None means contacts are off."""

    _VERBS = {"find": "_find", "create": "_create", "update": "_update", "delete": "_delete"}

    def __init__(self, settings) -> None:
        self.s = settings
        self.directory: Any = None

    async def run(self, verb: str, inputs: dict) -> ActionResult:
        method = self._VERBS.get(verb)
        if not method:
            return {"ok": False, "error": "unknown_verb",
                    "summary": f"no contacts verb {verb!r}"}
        d = self.directory
        if d is None or not d.ready:
            # Fails closed while cold: "not loaded yet" must not read as "nobody by that name",
            # which is what would have her re-create someone already in the book.
            return {"ok": False, "error": "unavailable",
                    "summary": "The address book is not available right now. Nothing was changed."}
        try:
            return await getattr(self, method)(inputs or {})
        except ChangeError as exc:
            return {"ok": False, "error": "validation", "summary": f"Not changed: {exc}"}
        except Exception as exc:  # never raise into the graph
            return await self._on_error(exc, verb, inputs or {})

    # --- find ----------------------------------------------------------------------------

    async def _find(self, inputs: dict) -> ActionResult:
        d = self.directory
        query = (inputs.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "validation",
                    "summary": "contacts.find needs something to search for."}
        found = await d.search(query)
        shown = found[:_SHOW]
        cards = await self._whole_cards(shown) if len(found) <= _READ_WHOLE else {}
        members = d.member_ids(inputs.get("_phone"), inputs.get("_phones"))
        self_chat = bool(inputs.get("_self_chat"))
        views = {c["resource_name"]: person_view(
                     c, visible=self_chat or c["resource_name"] in members,
                     card=cards.get(c["resource_name"]), self_chat=self_chat)
                 for c in shown}
        # An invite only needs the address of the person it is for. Several candidates are a
        # question, not a shortlist of addresses to hold (Directory.block refuses them the same way).
        reveal = bool(inputs.get("_reveal_emails")) and len(found) == 1
        if not found:
            summary = f'No card matches "{query}".'
        else:
            more = f", showing the best {len(shown)}" if len(found) > len(shown) else ""
            head = f'{len(found)} card(s) match "{query}"{more}:'
            lines = [describe_person(v, reveal_emails=reveal) for v in views.values()]
            summary = "\n".join([head, *lines])
        return {"ok": True, "private": True, "summary": summary,
                "data": {"seen_people": views, "total": len(found)}}

    async def _whole_cards(self, contacts: list) -> dict:
        """{resource_name: card}. Best-effort: the snapshot still answers if Google does not."""
        names = [c["resource_name"] for c in contacts if c.get("source") != LEARNED]
        if not names:
            return {}
        try:
            people = await asyncio.to_thread(self.directory.people.read_cards, names)
        except Exception as exc:
            log.warning("contacts: whole-card read failed: %s", exc)
            return {}
        return {p["resourceName"]: card_view(p) for p in people if p.get("resourceName")}

    # --- writes --------------------------------------------------------------------------

    async def _create(self, inputs: dict) -> ActionResult:
        name = (inputs.get("name") or "").strip()
        changes = inputs.get("changes") or []
        if not name:
            return {"ok": False, "error": "validation", "summary": "A new card needs a name."}
        d = self.directory
        person = await asyncio.to_thread(d.people.create_card, name, changes)
        c = await d.apply_person(person)
        view = person_view(c, visible=True)
        return {"ok": True, "summary": f"Created the card for {c.get('name') or name}.",
                "data": {"name": c.get("name") or name, "contact_id": c["resource_name"],
                         "changes": changes, "seen_people": {c["resource_name"]: view}}}

    async def _update(self, inputs: dict) -> ActionResult:
        d = self.directory
        rn = inputs.get("contact_id") or ""
        changes = inputs.get("changes") or []
        if not changes:
            return {"ok": False, "error": "validation", "summary": "No change was given."}
        c = d.get(rn)
        if c is None:
            return {"ok": False, "error": "not_found",
                    "summary": "That card is no longer in the address book. Nothing was changed."}
        if c.get("source") == LEARNED:
            return await self._promote(c, changes, inputs)
        person, report = await asyncio.to_thread(d.people.apply_changes, rn, changes)
        # A no-op answers with the contact-only read, which is short of what the snapshot holds.
        fresh = c if report["noop"] else await d.apply_person(person)
        visible = bool(inputs.get("_visible"))
        name = fresh.get("name") or c.get("name") or ""
        # What was meant to go away may be a value this chat was only ever shown the mask of.
        stuck = [g["value"] if visible else mask(g["field"], g["value"]) for g in report["stuck"]]
        if stuck:
            summary = (f"The card for {name} no longer holds it, but it is still showing, because it "
                       f"comes from their Google profile: {', '.join(stuck)}.")
        elif report["noop"]:
            summary = f"Nothing to change: the card for {name} already says this."
        else:
            summary = f"Updated the card for {name}."
        data = {"name": name, "contact_id": rn, "changes": changes, "noop": report["noop"],
                "stuck": stuck, "visible": visible}
        if not report["noop"]:
            data["seen_people"] = {rn: person_view(
                fresh, visible=visible, card=card_view(person),
                self_chat=bool(inputs.get("_self_chat")))}
        return {"ok": True, "summary": summary, "data": data}

    async def _promote(self, learned: dict, changes: list, inputs: dict) -> ActionResult:
        """A card she only knows herself. He is editing it as a contact, so it becomes a real one:
        what she had learned, with his changes on top, in one create."""
        d = self.directory
        name = learned.get("name") or ""
        merged = [{"field": "email", "op": "add", "old": "", "new": e}
                  for e in learned.get("emails") or []]
        for ch in changes:
            if ch.get("field") == "name" and ch.get("op") != "remove":
                name = (ch.get("new") or "").strip() or name
            else:
                merged.append(ch)
        person = await asyncio.to_thread(d.people.create_card, name, merged)
        fresh = await d.apply_person(person)
        await d.drop(learned["resource_name"])
        rn = fresh["resource_name"]
        visible = bool(inputs.get("_visible"))
        return {"ok": True, "summary": f"Saved {name} to Google Contacts, with the change.",
                "data": {"name": fresh.get("name") or name, "contact_id": rn, "changes": changes,
                         "noop": False, "stuck": [], "visible": visible,
                         "seen_people": {rn: person_view(fresh, visible=visible)}}}

    async def _delete(self, inputs: dict) -> ActionResult:
        d = self.directory
        rn = inputs.get("contact_id") or ""
        c = d.get(rn)
        if c is None:
            return {"ok": False, "error": "not_found",
                    "summary": "That card is no longer in the address book."}
        name = c.get("name") or ""
        if c.get("source") == LEARNED:
            await d.drop(rn)
            return {"ok": True, "summary": f"Deleted {name}.", "data": {"name": name}}
        if d.store is None:
            # The copy is the only undo he can be sure of, so no copy means no delete.
            return {"ok": False, "error": "no_store",
                    "summary": "Cannot delete right now: there is nowhere to keep a copy of the "
                               "card first. Nothing was changed."}
        person = await asyncio.to_thread(d.people.read_card, rn)
        await d.store.tombstone(rn, person)
        await asyncio.to_thread(d.people.delete_contact, rn)
        await d.drop(rn)
        return {"ok": True, "summary": f"Deleted the card for {name}.", "data": {"name": name}}

    # --- failures ------------------------------------------------------------------------

    async def _on_error(self, exc: Exception, verb: str, inputs: dict) -> ActionResult:
        status = getattr(getattr(exc, "resp", None), "status", None)
        d = self.directory
        if status in (401, 403):
            err = "auth"
        elif status == 404:
            err = "not_found"
            rn = inputs.get("contact_id")
            if rn:  # Google says the card is gone, so stop offering it
                await d.drop(rn)
        elif is_transient(exc):
            # Named, so the render policy can tell "the network hiccuped" from "Google said no".
            err = "transient"
            if verb == "create":
                # Google may have committed before the socket died. Sync now, so the next try's
                # duplicate check sees the person if it did.
                d.spawn(d.refresh())
        else:
            err = str(exc)
        log.warning("contacts.%s failed: %s", verb, err)
        return {"ok": False, "error": err, "summary": f"contacts.{verb} failed: {err}",
                "data": {"verb": verb}}
