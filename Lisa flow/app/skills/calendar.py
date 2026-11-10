"""The calendar skill — the local-action domain: create / list / find / update / delete.

Assembles the domain policy around the infra client: the prose (DESCRIBE/GUIDANCE) and the
per-verb input schemas stay co-located with the Google handler in tools/calendar.py and
tools/schemas.py (so prompt and behaviour never drift), and this module bundles them into the
Skill together with the confirm/render policies and the router matcher."""
from __future__ import annotations

import difflib
import unicodedata

from ..presets import MEETING_MINUTES
from ..tools.calendar import (
    CONTACTS_CALL_GUIDANCE, CONTACTS_GUIDANCE, DEFAULTS_GUIDANCE, DESCRIBE, GUIDANCE,
    GoogleCalendarService, apply_presets,
)
from ..tools.schemas import CALENDAR_TASK_SCHEMAS
from .base import Skill
from .calendar_format import (
    compose_create, compose_delete, compose_update,
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
                    ("presets_enabled", DEFAULTS_GUIDANCE)],
    calls={"contacts"},
    # Local, short, no live-web hops → the fast lane: Sonnet, medium effort, no thinking.
    model="claude-sonnet-5",
    effort="medium",
    think=False,
)
