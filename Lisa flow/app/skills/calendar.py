"""The calendar skill — the local-action domain: create / list / find / update / delete.

Assembles the domain policy around the infra client: the prose (DESCRIBE/GUIDANCE) and the
per-verb input schemas stay co-located with the Google handler in tools/calendar.py and
tools/schemas.py (so prompt and behaviour never drift), and this module bundles them into the
Skill together with the confirm/render policies and the router matcher."""
from __future__ import annotations

import difflib
import unicodedata
from datetime import datetime

from ..presets import MEETING_MINUTES
from ..state import HANDOFF_MARK
from ..tools.calendar import (
    BUSY_GUIDANCE, CONTACTS_CALL_GUIDANCE, CONTACTS_GUIDANCE, DEFAULTS_GUIDANCE, DESCRIBE,
    GUIDANCE, GoogleCalendarService, apply_presets,
)
from ..tools.schemas import CALENDAR_TASK_SCHEMAS
from .base import Skill
from .calendar_format import (
    busy_line, compose_create, compose_delete, compose_update,
    fmt_create, fmt_delete, fmt_failure, fmt_list, fmt_update,
)
from .confirm import FlagConfirm
from .render import LLMReadback, Programmatic


# --- the router matcher -------------------------------------------------------------------
#
# Reuses the intent.py idea (normalise → lexicon → difflib fuzzy) to give the router a cheap,
# programmatic read of whether a turn is calendar work — WITHOUT a model call:
#
#   "yes"      strong scheduling signal          → route to calendar, no LLM
#   "no"       no calendar signal at all          → the router falls back to web (default)
#   "maybe"    a weak/time-only signal            → the router escalates to the LLM classifier
#
# Kept deliberately conservative: unusual phrasings land in "maybe" and the classifier decides,
# rather than being force-routed here. Thresholds are meant to be tuned from real traces.

# Unambiguous scheduling words (EN / PT / ES). Any hit → "yes".
_STRONG = {
    "schedule", "reschedule", "meeting", "appointment", "calendar", "agenda", "remind",
    "reminder", "invite", "invitee", "event", "attendee", "attendees", "guest", "guests",
    "agendar", "reagendar", "reagende", "reuniao", "compromisso", "marcar", "remarcar",
    "remarque", "lembrete", "convite", "convidado", "convidados", "convidar", "evento",
    "calendario",
    "agenda", "reunion", "cita", "recordatorio", "agendame", "invitado", "invitados",
    "birthday", "aniversario", "cumpleanos", "holiday", "feriado",
    "vacation", "ferias", "vacaciones", "pto",
}

# Weak / time-ish words that MIGHT be scheduling but often are not ("cancel my sub", "monday
# news"). Present without a strong word → "maybe".
_WEAK = {
    "cancel", "move", "book", "tomorrow", "today", "tonight", "morning", "afternoon", "evening",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "am", "pm",
    "cancelar", "mover", "amanha", "hoje", "manha", "tarde", "noite",
    "segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo",
    "manana", "hoy", "lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo",
    "trip", "viagem", "viaje", "folga", "block", "bloquear", "inteiro",
}


def _normalize(s: str) -> str:
    """Casefold + strip accents/punctuation → space-separated tokens (same as intent._normalize)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s.lower())
    return " ".join(s.split())


def _hits(tokens: list[str], lexicon: set[str], threshold: float) -> bool:
    for tok in tokens:
        if tok in lexicon:
            return True
        if len(tok) >= 6 and any(
            difflib.SequenceMatcher(None, tok, w).ratio() >= threshold
            for w in lexicon if len(w) >= 6
        ):
            return True
    return False


# Multi-word signals the token lexicon structurally cannot see: _normalize splits on
# whitespace, so "all day" and "dia inteiro" are two tokens each and no single-token entry can
# ever match them — while "dia" alone is far too common to put in _WEAK.
_STRONG_PHRASES = ("all day", "dia inteiro", "todo el dia", "day off", "dia de folga")


def calendar_matcher(text: str, *, threshold: float = 0.86) -> str:
    """"yes" | "no" | "maybe" — is this turn calendar work? (programmatic, no model call)."""
    norm = _normalize(text)
    if any(p in norm for p in _STRONG_PHRASES):
        return "yes"
    tokens = norm.split()
    if not tokens:
        return "no"
    if _hits(tokens, _STRONG, threshold):
        return "yes"
    if _hits(tokens, _WEAK, threshold):
        return "maybe"
    return "no"


def presets_block(presets, owner: str) -> str | None:
    """The owner's presets, as the model reads them — only what differs from the calendar
    instructions' own defaults, plus the saved places. None when nothing does, so with nothing
    set the prompt is byte-for-byte the pre-presets one.

    The system applies the length, the video call and the guest emails itself (apply_presets);
    the model is told them so that what it says and the explicit flags it sends agree with them.
    The places are the part it must USE: "at my house" has to become an address, and only the
    model can tell that "minha casa" means the place he saved as "casa"."""
    if presets is None:
        return None
    lines: list[str] = []
    if presets.is_set(MEETING_MINUTES):
        lines.append(f"- Standard meeting length: {presets.meeting_minutes()} minutes — this "
                     f"replaces the default length above. Leave \"end\" out unless he gives one.")
    if not presets.video_call():
        lines.append("- Video call: OFF by default. A timed event with no place gets NO Meet "
                     "link. Send \"virtual\": true only when he asks for a call, a Meet or a "
                     "video meeting.")
    if not presets.notify_guests():
        lines.append("- Guest emails: OFF by default. Guests are added to the event without "
                     "being emailed. Send \"send_invites\": true only when he asks for the guests "
                     "to be invited by email or notified.")
    places = presets.places()
    if places:
        lines.append("- Saved places. When he says one of these is where the meeting happens — "
                     "by its name or in his own words for it (\"minha casa\" for casa) — set "
                     "\"location\" to its address, copied exactly. A place not listed here is "
                     "written the way he said it, as before:")
        lines.extend(f"  - {p['name']}: {p['address']}" for p in places)
    if not lines:
        return None
    head = (f"\n\n{owner}'s presets — he set these himself in setup, and they replace the "
            f"matching defaults in the calendar instructions above. The system applies them to "
            f"every booking; never ask him about them.")
    return head + "\n" + "\n".join(lines)


def calendar_context(state: dict, ctx: dict):
    """The prompt blocks for this turn — the owner's presets and the address book — plus what the
    address book surfaced.

    Pure and synchronous — it runs inside the reply path. All of the work is exact dictionary
    lookups against the in-process snapshots (measured at 0.3 ms over 2,000 contacts); the fuzzy
    matching this codebase uses elsewhere was measured at 5 s over the same book, as blocking CPU
    inside an async node, which stalls every chat rather than just this one."""
    settings = ctx.get("settings")
    pblock = presets_block(ctx.get("presets"), getattr(settings, "owner_name", "the owner"))
    book = _address_book_context(state, ctx)
    if book is None:
        return pblock
    return {"block": (pblock or "") + book["block"], "state": book["state"]}


def _address_book_context(state: dict, ctx: dict):
    """The address-book block and the contacts it surfaced, or None."""
    d = ctx.get("directory")
    settings = ctx.get("settings")
    if d is None or not getattr(d, "ready", False):
        return None                      # fails open: no block, and Lisa asks as she does today
    text = state.get("turn_text") or state.get("text") or ""
    found = d.mentions(
        text,
        phone=state.get("phone"),
        # The room. In a group `phone` is None, so this is the only identity evidence there is.
        phones=state.get("participant_phones"),
        limit=getattr(settings, "contacts_max_in_prompt", 5),
    )
    if not found:
        return None
    seen = {}
    for c, why in found:
        if why == "name?":
            continue  # ambiguous: no address is attributable to it, so none is remembered
        for e in c.get("emails") or []:
            seen[e.lower()] = {"name": c.get("name") or "",
                               "resource_name": c.get("resource_name") or "",
                               "n_emails": len(c.get("emails") or [])}
    block = d.block(found, settings.owner_name)
    if not block.strip():
        return None
    return {"block": block, "state": {"seen_contacts": seen}}


def _contacts_heard(state: dict) -> bool:
    """The contacts skill already answered somewhere in this loop."""
    for m in state.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, str) and content.startswith(HANDOFF_MARK) \
                and content[len(HANDOFF_MARK):].startswith("The contacts skill"):
            return True
    return False


def _guest_in_title(title: str, owner_name: str) -> str | None:
    """The other side of a "Marcelo & Felipe" title, the form the calendar prompt gives a meeting
    whose topic it could not tell. None for any other title."""
    left, sep, right = (title or "").partition("&")
    owner = _normalize(owner_name).split()[:1]
    if not sep or not right.strip() or _normalize(left).split() != owner:
        return None
    return right.strip()


# --- when he is not free ------------------------------------------------------------------
#
# On 2026-11-10 he tagged "@lisa agendar aqui em casa, 11 as 12:30" in a chat with a partner and
# got a card for a time he already had a meeting in. Nothing had looked. Now the system looks
# before every card, and what a held-back booking leaves behind is a note for the model, not a
# message for the chat: she says he is not free, as a secretary would, and settles another time
# with the people in the conversation.
#
# What she can let slip is bounded by what she is given, so she is given times and nothing else.
# The handler does not even fetch the other event's title (tools/calendar.py, _BUSY_FIELDS).

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# How the confirm node opens the note: "[calendar.create not proposed] …".
NOT_PROPOSED = " not proposed] "


def _role_text(m) -> tuple[str, str]:
    """(role, text) of a checkpointed message, whichever shape it came back in."""
    if isinstance(m, dict):
        role, content = m.get("role", "user"), m.get("content")
    else:
        role = "assistant" if getattr(m, "type", "") == "ai" else "user"
        content = getattr(m, "content", "")
    return role, content if isinstance(content, str) else ""


def _is_busy_note(text: str) -> bool:
    return text.startswith("[calendar.") and NOT_PROPOSED in text[:48]


def _hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


def busy_note(owner: str, data: dict) -> str:
    """What the model is told when a booking is held back: that he is not free then, and when
    he is. Built from the handler's times-only answer, so there is no title here to repeat."""
    start, end = data["start"], data["end"]
    d = datetime.fromisoformat(start)
    minutes = data.get("minutes")
    free = data.get("free") or []
    if free:
        windows = ", ".join(f"{_hhmm(w['start'])} to {_hhmm(w['end'])}" for w in free)
        rest = (f"The times he is free that day, each long enough for the {minutes} minutes "
                f"this takes: {windows}. They are for you to choose from, never to read out.")
    else:
        rest = (f"He has no other free time that day long enough for the {minutes} minutes "
                f"this takes.")
    return (f"{owner} is not free then: something else is already on his calendar during "
            f"{_WEEKDAYS[d.weekday()]}, {d:%Y-%m-%d}, {_hhmm(start)} to {_hhmm(end)} "
            f"[slot {start}/{end}]. No card was shown, and you are not told what the other "
            f"thing is. {rest} Now follow WHEN HE IS NOT FREE.")


def _told_and_answered(state: dict, slot: str) -> bool:
    """She has already said he is not free at this very time, and he is the one speaking now.

    That is the only way the same time comes back to be proposed for a reason: he heard it and
    said to book it anyway. Read off the thread: the note for this slot, then a message of hers
    after it (the note alone means nobody was told yet, and the model is merely insisting).
    Someone else speaking is not him deciding to double-book himself."""
    if not (state.get("from_me") or state.get("owner_replies")):
        return False
    noted = False
    for m in state.get("messages") or []:
        role, text = _role_text(m)
        if role == "assistant":
            if noted:
                return True
        elif _is_busy_note(text) and f"[slot {slot}]" in text:
            noted = True
    return False


async def calendar_busy_check(action: dict, state: dict, ctx: dict) -> dict | None:
    """Before a booking's card: is he free then? (FlagConfirm.precheck)

    None when there is nothing to look at. Otherwise {"hold", "trace"}, and when `hold` is true
    also "note" (for the model) and "say" (the line the code sends if the model cannot be asked).

    A read that fails holds nothing back. The card going out for a time he is busy is yesterday's
    behaviour; no card at all because Google hiccuped would be a booking lost."""
    settings = ctx.get("settings")
    if not getattr(settings, "busy_check_enabled", False):
        return None
    verb = (action.get("task") or "").partition(".")[2]
    handler = ctx.get("handler")
    if verb not in ("create", "update") or handler is None:
        return None
    write = {k: v for k, v in action.items() if k != "task" and not k.startswith("_")}
    prev = (state.get("seen_events") or {}).get(action.get("event_id")) if verb == "update" else None
    res = await handler.run("busy", {"verb": verb, "write": write, "prev": prev}) or {}
    data = res.get("data") or {}
    if not res.get("ok"):
        return {"hold": False, "trace": {"read": "failed", "error": res.get("error")}}
    if not data.get("checked"):
        return {"hold": False, "trace": {"read": "skipped"}}
    trace = {"read": "ok", "start": data.get("start"), "end": data.get("end"),
             "conflict": bool(data.get("conflict"))}
    if not data.get("conflict"):
        return {"hold": False, "trace": trace}
    slot = f"{data['start']}/{data['end']}"
    if _told_and_answered(state, slot):
        return {"hold": False, "trace": {**trace, "anyway": True}}
    owner = getattr(settings, "owner_name", "the owner")
    lang = state.get("session_lang") or state.get("lang")
    return {"hold": True, "note": busy_note(owner, data), "say": busy_line(owner, lang),
            "trace": {**trace, "free": len(data.get("free") or [])}}


def _busy_unanswered(state: dict) -> bool:
    """A "not proposed" note is the last word in the thread: she has not spoken since."""
    for m in reversed(state.get("messages") or []):
        role, text = _role_text(m)
        if role == "assistant":
            return False
        if _is_busy_note(text):
            return True
    return False


def _say_he_is_busy(result: dict, state: dict, settings) -> dict:
    """Told he is not free, the model said nothing and did nothing → the line is said for it.

    Whether to offer another time is hers to judge. That he hears his booking did not go ahead is
    not: silence here leaves him waiting for a card that will never come."""
    if not getattr(settings, "busy_check_enabled", False):
        return result
    if result.get("message") or result.get("actions") or result.get("call"):
        return result
    if not _busy_unanswered(state):
        return result
    lang = state.get("session_lang") or result.get("lang")
    return {**result, "state": "awaiting_answer",
            "message": busy_line(getattr(settings, "owner_name", "the owner"), lang)}


def calendar_revise(result: dict, state: dict, settings) -> dict:
    """Code's second look at a calendar pass (Skill.revise): the two steps the prompt asks for
    and the model now and then skips."""
    said = _say_he_is_busy(result, state, settings)
    if said is not result:
        return said
    return _contacts_first(result, state, settings)


def _contacts_first(result: dict, state: dict, settings) -> dict:
    """A booking for someone with no address, made before asking the contacts skill → ask it first.

    The prompt already says to (CONTACTS_CALL_GUIDANCE), and most passes do. Live, some still put up
    "Marcelo & Testolini" with no guests, reasoning that he had not asked for an invite, so the card
    went out naming a person who would never hear of the meeting while his address sat in the book.
    The sign is in the output itself: a create titled with him and a guest, and no guest on it.

    Only once per loop, and only when nothing else explains the empty guest list: the person is not
    in the address block (that line is the model's to weigh), he did not ask for a quiet event, and
    contacts has not answered yet. Contacts that finds nobody hands back, and the booking then goes
    out as the model wrote it."""
    if not getattr(settings, "contacts_crud_on", False):
        return result
    if result.get("call") or state.get("skill_stack") or int(state.get("skill_calls") or 0):
        return result
    if not (state.get("from_me") or state.get("owner_replies")):
        return result                      # contacts is his alone; the call would only be refused
    actions = result.get("actions") or []
    if len(actions) != 1 or actions[0].get("task") != "calendar.create":
        return result
    create = actions[0]
    if create.get("attendees") or create.get("send_invites") is False:
        return result
    guest = _guest_in_title(create.get("title") or "", getattr(settings, "owner_name", ""))
    if not guest:
        return result
    guest_toks = set(_normalize(guest).split())
    for seen in (state.get("seen_contacts") or {}).values():
        if guest_toks & set(_normalize(seen.get("name") or "").split()):
            return result                  # listed above: the model saw what is on file
    if _contacts_heard(state):
        return result

    known = [{"field": k, "value": str(v).lower() if isinstance(v, bool) else str(v)}
             for k, v in create.items() if k != "task" and v not in (None, "", [])]
    request = f"Find {guest}'s email, for an invite"
    return {**result, "actions": [], "message": None, "call": {"skill": "contacts", "request": request},
            "workflow": {"task": "calendar.create", "known_inputs": known,
                         "open_questions": [{"field": "attendees",
                                             "reason": f"need {guest}'s email"}]}}


def calendar_resolve_gate(verb: str, inputs: dict, state: dict):
    """(patched_inputs, error) — update/delete must target an event surfaced by a prior search
    in THIS loop. Moved verbatim out of the execute node, which used to hardcode it; behaviour
    is unchanged, the rule simply belongs to the skill that owns it."""
    if verb in ("update", "delete") and inputs.get("event_id") not in (state.get("seen_event_ids") or []):
        return None, {"error": "unresolved_id",
                      "summary": f"Cannot {verb}: that event was not found via a prior search — "
                                 f"run find first, then {verb} the id it returns."}
    return inputs, None


CALENDAR = Skill(
    name="calendar",
    kind="local",
    describe=DESCRIBE,
    guidance=GUIDANCE,
    verbs=["create", "list", "find", "update", "delete", "remember"],
    schemas=CALENDAR_TASK_SCHEMAS,
    handler_cls=GoogleCalendarService,
    # The confirm policy composes the confirmation prompt per verb (no LLM writes it) and detects
    # the owner's "yes" in code (skills.confirm.FlagConfirm.detect).
    confirm=FlagConfirm(
        {"create", "update", "delete"},
        compose_map={"create": compose_create, "update": compose_update, "delete": compose_delete},
        # His presets (length, video call, guest emails, places) are written into the held write
        # before its card is composed — one value for the card and the event both.
        prepare=apply_presets,
        # And his calendar is read for that time before the card is shown at all.
        precheck=calendar_busy_check,
    ),
    # Render is PER VERB: writes + list render programmatically from the result; find keeps the
    # model (judgment / "which one?"). respond falls back to the model on failure or an
    # unsupported language.
    # A transient failure is reported in code too (fmt_failure) — never handed back to the
    # model, which would answer by silently re-proposing the action.
    render={
        "create": Programmatic(fmt_create, on_failure=fmt_failure),
        "update": Programmatic(fmt_update, on_failure=fmt_failure),
        "delete": Programmatic(fmt_delete, on_failure=fmt_failure),
        "list": Programmatic(fmt_list, on_failure=fmt_failure),
        "find": LLMReadback(),
    },
    matcher=calendar_matcher,
    resolve_gate=calendar_resolve_gate,
    # `remember` never enters the action loop: stripped at the confirm node, gated there against
    # the turn's text, dispatched detached, never rendered.
    side_effects={"remember"},
    context_provider=calendar_context,
    # With contacts off, the verb leaves the schema and the address-book rules leave the prompt,
    # so the build is the pre-feature one rather than one that merely never injects the block.
    gated_verbs={"remember": "contacts_enabled"},
    gated_guidance=[("contacts_enabled", CONTACTS_GUIDANCE),
                    ("contacts_crud_on", CONTACTS_CALL_GUIDANCE),
                    ("presets_enabled", DEFAULTS_GUIDANCE),
                    ("busy_check_enabled", BUSY_GUIDANCE)],
    calls={"contacts"},
    revise=calendar_revise,
    # Local, short, no live-web hops → the fast lane: Sonnet, medium effort, no thinking.
    model="claude-sonnet-5",
    effort="medium",
    think=False,
)
