"""The calendar skill — the local-action domain: create / list / find / update / delete.

Assembles the domain policy around the infra client: the prose (DESCRIBE/GUIDANCE) and the
per-verb input schemas stay co-located with the Google handler in tools/calendar.py and
tools/schemas.py (so prompt and behaviour never drift), and this module bundles them into the
Skill together with the confirm/render policies and the router matcher."""
from __future__ import annotations

import difflib
import unicodedata

from ..tools.calendar import DESCRIBE, GUIDANCE, GoogleCalendarService
from ..tools.schemas import CALENDAR_TASK_SCHEMAS
from .base import Skill
from .calendar_format import (
    compose_create, compose_delete, compose_update,
    fmt_create, fmt_delete, fmt_list, fmt_update,
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
}

# Weak / time-ish words that MIGHT be scheduling but often are not ("cancel my sub", "monday
# news"). Present without a strong word → "maybe".
_WEAK = {
    "cancel", "move", "book", "tomorrow", "today", "tonight", "morning", "afternoon", "evening",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "am", "pm",
    "cancelar", "mover", "amanha", "hoje", "manha", "tarde", "noite",
    "segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo",
    "manana", "hoy", "lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo",
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


def calendar_matcher(text: str, *, threshold: float = 0.86) -> str:
    """"yes" | "no" | "maybe" — is this turn calendar work? (programmatic, no model call)."""
    tokens = _normalize(text).split()
    if not tokens:
        return "no"
    if _hits(tokens, _STRONG, threshold):
        return "yes"
    if _hits(tokens, _WEAK, threshold):
        return "maybe"
    return "no"


CALENDAR = Skill(
    name="calendar",
    kind="local",
    describe=DESCRIBE,
    guidance=GUIDANCE,
    verbs=["create", "list", "find", "update", "delete"],
    schemas=CALENDAR_TASK_SCHEMAS,
    handler_cls=GoogleCalendarService,
    # The confirm policy composes the confirmation prompt per verb (no LLM writes it) and detects
    # the owner's "yes" in code (skills.confirm.FlagConfirm.detect).
    confirm=FlagConfirm(
        {"create", "update", "delete"},
        compose_map={"create": compose_create, "update": compose_update, "delete": compose_delete},
    ),
    # Render is PER VERB: writes + list render programmatically from the result; find keeps the
    # model (judgment / "which one?"). respond falls back to the model on failure or an
    # unsupported language.
    render={
        "create": Programmatic(fmt_create),
        "update": Programmatic(fmt_update),
        "delete": Programmatic(fmt_delete),
        "list": Programmatic(fmt_list),
        "find": LLMReadback(),
    },
    matcher=calendar_matcher,
    # Local, short, no live-web hops → the fast lane: Sonnet, medium effort, no thinking.
    model="claude-sonnet-5",
    effort="medium",
    think=False,
)
