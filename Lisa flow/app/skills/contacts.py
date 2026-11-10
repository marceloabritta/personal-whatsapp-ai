"""The contacts skill — the owner's Google Contacts: find / create / update / delete.

Assembles the domain policy around tools/contacts.py: the prose and schemas live with the handler, and
this module bundles them into the Skill with the confirm policy, the render policies, the router
matcher and the gate.

Three things are structural here, not prompt-deep:

  owner_only   — a contact's message can never route a turn here, and on a turn only someone else
                 asked for there is no address-book block, the gate refuses a lookup, and no card can
                 be proposed. Otherwise anyone could ask Lisa for a number, or have her put a delete in
                 front of the owner for his next "ok", while the window is open.
  resolve_gate — a write only runs against a card surfaced THIS loop, and the gate is also where a
                 masked "old" value becomes the real one, so the model never needs to hold it.
  ContactsConfirm — refuses in code, before any card goes out: a target nobody surfaced, an email or
                 phone the conversation never contained, a person who is already on file. And a card
                 takes one answer (`one_answer`), so a refusal in his own words cannot be left
                 standing for the next "ok" to approve.
"""
from __future__ import annotations

import re
import unicodedata

from ..intent import _normalize as _intent_normalize, classify_confirmation
from ..nodes.confirm import _EMAIL_RE, _loop_text
from ..tools.contacts import (
    DESCRIBE, GUIDANCE, ContactsService, describe_person, person_view, unmask_changes, values_of,
)
from ..tools.contacts_schemas import CONTACTS_TASK_SCHEMAS
from .base import Skill
from .calendar import _STRONG as _CALENDAR_WORDS, _normalize
from .confirm import FlagConfirm, _verb
from .contacts_format import (
    compose_create, compose_delete, compose_update, fmt_create, fmt_delete, fmt_failure, fmt_update,
)
from .render import LLMReadback, Programmatic

# --- the router matcher -------------------------------------------------------------------
#
# Exact tokens, never the fuzzy match the calendar matcher uses. At its 0.86 threshold "contrato"
# scores 0.93 against "contato", so "manda o contrato pro Felipe" would land here.
_CONTACT_WORDS = {"contato", "contatos", "contact", "contacts", "contacto", "contactos"}
# In Brazil "agenda" is the address book as often as the calendar. With one of these it is the book.
_CARD_WORDS = {"telefone", "celular", "numero", "email", "whatsapp", "zap", "endereco"}
# A calendar word beside them makes it a booking that happens to mention a contact. Calendar takes
# that one, and can call this skill for the part it needs.
_CALENDAR_ONLY = _CALENDAR_WORDS - {"agenda"}


def contacts_matcher(text: str) -> str:
    """"yes" | "no" — is this turn plainly about the address book? (programmatic, no model call)."""
    norm = _normalize(text)
    tokens = set(norm.split())
    if tokens & _CALENDAR_ONLY:
        return "no"
    if tokens & _CONTACT_WORDS:
        return "yes"
    if "agenda" in tokens and (tokens & _CARD_WORDS or "e mail" in norm):
        return "yes"
    return "no"


def _from_owner(state: dict) -> bool:
    """Did he say anything this activation? His own message may have been carried in by someone
    else's, so the trigger alone does not settle it."""
    return bool(state.get("from_me") or state.get("owner_replies"))


# --- the per-turn block -------------------------------------------------------------------

def contacts_context(state: dict, ctx: dict):
    """The cards this conversation points at, plus what it surfaced. Pure and synchronous, like the
    calendar block: `mentions` and `member_ids` are dictionary lookups, and the deliberate, slower
    search is `contacts.find`, which runs off the reply path."""
    d = ctx.get("directory")
    settings = ctx.get("settings")
    if d is None or not getattr(d, "ready", False):
        return None
    if not _from_owner(state):
        return None  # someone else is talking: there is nothing here they should be answered from
    phone, phones = state.get("phone"), state.get("participant_phones")
    members = d.member_ids(phone, phones)
    self_chat = bool(state.get("is_self_chat"))
    found = d.mentions(state.get("turn_text") or state.get("text") or "", phone=phone, phones=phones,
                       limit=getattr(settings, "contacts_max_in_prompt", 5))
    seen: dict = {}
    lines: list = []
    ask: list = []
    for c, why in found:
        if why == "name?":
            ask.append(c.get("name") or "")  # ambiguous: no id, or she would pick one
            continue
        view = person_view(c, visible=self_chat or c["resource_name"] in members,
                           self_chat=self_chat)
        seen[c["resource_name"]] = view
        lines.append(describe_person(view))
    if ask:
        lines.append(f"- A name used here fits several cards: {', '.join(ask)}. Use contacts.find "
                     f"to see them and ask which.")
    if phone and not self_chat and not members:
        lines.append(f"- The other person in this chat writes from {phone}, and no card has that "
                     f"number. They may be saved under another one.")
    if not lines:
        return None
    head = (f"\n\nAddress book — the cards this conversation points at, from "
            f"{settings.owner_name}'s Google Contacts. Anyone else: contacts.find.")
    return {"block": "\n".join([head, *lines]), "state": {"seen_people": seen}}


# --- the target and the values ------------------------------------------------------------

def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


def resolve_target(action: dict, state: dict) -> str | None:
    """Which surfaced card this action means. Used by BOTH the composer and the gate, so the card
    named in the question is the card that gets written. Accepts a name where an id was expected,
    the slip setup also tolerates, but only when exactly one surfaced card has it."""
    seen = state.get("seen_people") or {}
    key = ((action or {}).get("contact_id") or "").strip()
    if key in seen:
        return key
    wanted = _fold(key)
    hits = [rn for rn, v in seen.items() if wanted and _fold(v.get("name")) == wanted]
    return hits[0] if len(hits) == 1 else None


# Separators people write inside a number. A colon stays, so "18:40" cannot run into what follows.
_NUMBER_GAPS = re.compile(r"[\s\-().]+")


def _in_conversation(field: str, value: str, haystack: str) -> bool:
    """Is this email or phone actually in the conversation? The same rule `remember` is held to."""
    value = (value or "").strip()
    if field == "email":
        # Whole addresses only: ana@acme.com is not "in" a chat that says joana@acme.com. The shape
        # check is what stops a mask, which is in the conversation as a find result, being written
        # to Google as somebody's address.
        return bool(_EMAIL_RE.fullmatch(value)) and bool(re.search(
            rf"(?<![\w.+\-]){re.escape(value)}(?![\w\-])", haystack, re.IGNORECASE))
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 8:
        return False
    flat = _NUMBER_GAPS.sub("", haystack)
    national = digits[2:] if digits.startswith("55") and len(digits) >= 12 else digits
    return digits in flat or national in flat


def _ungrounded(changes: list, state: dict) -> list:
    haystack = _loop_text(state)
    return [(ch.get("new") or "").strip() for ch in changes or []
            if ch.get("field") in ("email", "phone") and ch.get("op") != "remove"
            and (ch.get("new") or "").strip()
            and not _in_conversation(ch["field"], ch["new"], haystack)]


def problem_with(action: dict, state: dict, directory=None) -> str | None:
    """Why this write cannot even be proposed, in words the model can act on. None means it can."""
    verb = _verb(action)
    changes = (action or {}).get("changes") or []
    if verb == "create":
        if not (action.get("name") or "").strip():
            return "A new card needs a name. Ask for it."
        bad = _ungrounded(changes, state)
        if bad:
            return (f"{', '.join(bad)} is not in this conversation, so it was not proposed. Use only "
                    f"what was said here, or ask for it.")
        if directory is not None:
            for ch in changes:
                new = (ch.get("new") or "").strip()
                held = (directory.by_email(new) if ch.get("field") == "email"
                        else (directory.by_phone(new) or [None])[0]
                        if ch.get("field") == "phone" else None)
                if held:
                    return (f"{new} is already on the card for {held.get('name')}, so a new card "
                            f"would duplicate it. Run contacts.find and update that card instead, "
                            f"or ask him which he wants.")
        return None

    target = resolve_target(action, state)
    if target is None:
        return (f"That card was not found in this conversation, so nothing was proposed. Run "
                f"contacts.find first, then {verb} the id it returns.")
    if verb == "update":
        if not changes:
            return "No change was given, so nothing was proposed."
        view = (state.get("seen_people") or {})[target]
        for ch in changes:
            if ch.get("op") == "replace" and not (ch.get("old") or "").strip():
                held = values_of(view, ch.get("field"))
                if len(held) > 1 and ch.get("field") in ("email", "phone", "nickname", "address"):
                    return (f"This card has {len(held)} of field {ch.get('field')}. Ask him which "
                            f"one to replace, and send it in \"old\".")
        bad = _ungrounded(changes, state)
        if bad:
            return (f"{', '.join(bad)} is not in this conversation, so it was not proposed. Use only "
                    f"what was said here, or ask for it.")
    return None


# --- the gate -----------------------------------------------------------------------------

def contacts_resolve_gate(verb: str, inputs: dict, state: dict):
    """(patched_inputs, error) — may this action run, and against which card?

    A write already passed the owner's yes, so this only decides the target. `find` has no card in
    front of it, so this is the one place that stands between someone else in the chat and the book."""
    stack = state.get("skill_stack") or []
    room = {
        "_self_chat": bool(state.get("is_self_chat")),
        "_phones": list(state.get("participant_phones") or []),
    }
    if verb == "find":
        if not _from_owner(state):
            return None, {"error": "not_owner",
                          "summary": "Only the owner can look contacts up. Say nothing about it."}
        # Fetching an address for an invite: the invitation carries it to the room regardless.
        if stack and stack[-1].get("domain") == "calendar":
            room["_reveal_emails"] = True
        return {**inputs, **room}, None
    if verb == "create":
        return {**inputs, **room}, None

    target = resolve_target(inputs, state)
    if target is None:
        return None, {"error": "unresolved_id",
                      "summary": f"Cannot {verb}: that card was not found via a prior search — "
                                 f"run contacts.find first, then {verb} the id it returns."}
    view = (state.get("seen_people") or {}).get(target) or {}
    patched = {**inputs, **room, "contact_id": target, "_visible": bool(view.get("visible"))}
    if verb == "update":
        patched["changes"] = unmask_changes(inputs.get("changes") or [], view)
    return patched, None


# --- the confirm policy -------------------------------------------------------------------

_CANCEL_WORDS = {"cancela", "cancelar", "cancele", "cancel", "cancelo"}


class ContactsConfirm(FlagConfirm):
    """FlagConfirm, plus what only an address book needs."""

    # A card takes one answer: anything from him but a clean yes withdraws it (nodes/resolve.py).
    # Calendar keeps a proposal through a correction, and can afford to: a wrong event can be moved,
    # and nobody's only copy of anything is on it. A deleted contact has no undo at Google.
    one_answer = True

    def detect(self, text: str, action: dict | None = None) -> str:
        """Its own word lists. The shared one is keyed by verb alone, and under a calendar delete
        ("Cancelar?") "cancela" means go ahead. Under "Apagar este contato?" it means stop, and
        reading it as a yes would delete the card he was calling off."""
        verb = _verb(action)
        tokens = set(_intent_normalize(text).split())
        if verb == "delete" and tokens and tokens <= _CANCEL_WORDS:
            return "no"
        return classify_confirmation(text, f"contact_{verb}" if verb else "")

    async def confirm(self, *, action: dict, state: dict, deps) -> dict:
        decision = await super().confirm(action=action, state=state, deps=deps)
        if decision["ok"]:
            return decision
        if not _from_owner(state):
            return {"ok": False, "reason": "not_owner",
                    "message": "Only he can have a contact changed, and this came from someone else, "
                               "so nothing was proposed. Say nothing about it."}
        problem = problem_with(action, state, (deps or {}).get("directory"))
        if problem:
            return {"ok": False, "message": problem, "reason": "invalid"}
        return decision

    def compose(self, action: dict, state: dict):
        # The composers read `contact_id` as the key into seen_people, and may have been handed a
        # name: resolve it first, so the card names who the gate will write.
        target = resolve_target(action, state)
        return super().compose({**action, "contact_id": target} if target else action, state)


CONTACTS = Skill(
    name="contacts",
    kind="local",
    describe=DESCRIBE,
    guidance=GUIDANCE,
    verbs=["find", "create", "update", "delete"],
    schemas=CONTACTS_TASK_SCHEMAS,
    handler_cls=ContactsService,
    confirm=ContactsConfirm(
        {"create", "update", "delete"},
        compose_map={"create": compose_create, "update": compose_update, "delete": compose_delete},
    ),
    # Writes render in code. `find` keeps the model: "which of these?" is judgment, and the answer may
    # be another action rather than a message.
    render={
        "create": Programmatic(fmt_create, on_failure=fmt_failure),
        "update": Programmatic(fmt_update, on_failure=fmt_failure),
        "delete": Programmatic(fmt_delete, on_failure=fmt_failure),
        "find": LLMReadback(),
    },
    matcher=contacts_matcher,
    resolve_gate=contacts_resolve_gate,
    context_provider=contacts_context,
    enabled_by="contacts_crud_on",
    owner_only=True,
    calls={"calendar"},
    # Local, short, no live-web hops — the fast lane, same as calendar and setup.
    model="claude-sonnet-5",
    effort="medium",
    think=False,
)
