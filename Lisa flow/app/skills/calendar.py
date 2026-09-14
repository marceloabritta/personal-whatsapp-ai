"""The calendar skill — the local-action domain: create / list / find / update / delete.

Assembles the domain policy around the infra client: the prose (DESCRIBE/GUIDANCE) and the
per-verb input schemas stay co-located with the Google handler in tools/calendar.py and
tools/schemas.py (so prompt and behaviour never drift), and this module bundles them into the
Skill together with the confirm/render policies and the router matcher."""
from __future__ import annotations

import difflib
import re
import unicodedata

from ..tools.calendar import CONTACTS_GUIDANCE, DESCRIBE, GUIDANCE, GoogleCalendarService
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


_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def calendar_context(state: dict, ctx: dict):
    """The address-book block for this turn, plus what it surfaced.

    Pure and synchronous — it runs inside the reply path. All of the work is exact dictionary
    lookups against the in-process snapshot (measured at 0.3 ms over 2,000 contacts); the fuzzy
    matching this codebase uses elsewhere was measured at 5 s over the same book, as blocking CPU
    inside an async node, which stalls every chat rather than just this one."""
    d = ctx.get("directory")
    settings = ctx.get("settings")
    if d is None or not getattr(d, "ready", False):
        return None                      # fails open: no block, and Lisa asks as she does today
    text = state.get("turn_text") or state.get("text") or ""
    is_group = state.get("chat_kind") == "group"
    found = d.mentions(
        text,
        phone=state.get("phone"),
        limit=getattr(settings, "contacts_max_in_prompt", 5),
        group=is_group,
    )
    if not found:
        return None
    # What this conversation actually put on the table. In a group it is the only thing that may
    # be shown back — see Directory.block.
    offered = {m.group(0).lower() for m in _EMAIL_IN_TEXT.finditer(text)}
    seen = {}
    for c, _why in found:
        for e in c.get("emails") or []:
            seen[e.lower()] = {"name": c.get("name") or "",
                               "resource_name": c.get("resource_name") or "",
                               "n_emails": len(c.get("emails") or [])}
    block = d.block(found, settings.owner_name, group=is_group, offered=offered)
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
    gated_guidance=[("contacts_enabled", CONTACTS_GUIDANCE)],
    # Local, short, no live-web hops → the fast lane: Sonnet, medium effort, no thinking.
    model="claude-sonnet-5",
    effort="medium",
    think=False,
)
