"""Calendar message composition — every user-facing calendar message, built in code.

Three families, all localized to the locked session language (pt / en / es):

  compose_*   the CONFIRMATION prompt before a write (create/update/delete)   → confirm policy
  fmt_*       the SUCCESS card after a write (create/update/delete)           → render policy
  fmt_list    the AGENDA for a read (list)                                     → render policy

The model composes none of these — it only emits the structured action; on the calendar domain
its `message` is null. Confirmations read from the action (create) or the cached found event
(update/delete, via state["seen_events"]); success cards + the agenda read from the handler's
`ActionResult.data`. Unsupported languages fall back to the model (see LANGS + the respond node)."""
from __future__ import annotations

from datetime import datetime

LANGS = {"pt", "en", "es"}

_MONTHS = {
    "pt": ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
}
_WEEKDAYS = {
    "pt": ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira",
           "Sábado", "Domingo"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "es": ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"],
}
_L = {
    "pt": {
        "confirm_create": "Confirmando", "confirm_update": "Confirmando alteração",
        "confirm_delete": "Confirmando cancelamento",
        "ask_create": "Posso agendar?", "ask_update": "Posso alterar?", "ask_delete": "Cancelar?",
        "created": "Agendado", "updated": "Alterado", "cancelled": "Cancelado",
        "video": "Google Meet (chamada de vídeo)", "with": "com", "guests": "convidados",
        "guests_will": "Os convidados serão avisados.", "guests_did": "Os convidados foram avisados.",
        "empty": "Nada na agenda.", "all_day": "Dia inteiro", "untitled": "(sem título)",
        "event": "(evento)",
    },
    "en": {
        "confirm_create": "Confirming", "confirm_update": "Confirming this change",
        "confirm_delete": "Confirming this cancellation",
        "ask_create": "Shall I schedule it?", "ask_update": "Shall I change it?",
        "ask_delete": "Cancel this one?",
        "created": "Scheduled", "updated": "Updated", "cancelled": "Cancelled",
        "video": "Google Meet (video call)", "with": "with", "guests": "guest(s)",
        "guests_will": "The guests will be notified.", "guests_did": "The guests were notified.",
        "empty": "Nothing on your calendar.", "all_day": "All day", "untitled": "(no title)",
        "event": "(event)",
    },
    "es": {
        "confirm_create": "Confirmando", "confirm_update": "Confirmando el cambio",
        "confirm_delete": "Confirmando la cancelación",
        "ask_create": "¿Lo agendo?", "ask_update": "¿Lo cambio?", "ask_delete": "¿Lo cancelo?",
        "created": "Agendado", "updated": "Actualizado", "cancelled": "Cancelado",
        "video": "Google Meet (videollamada)", "with": "con", "guests": "invitados",
        "guests_will": "Se avisará a los invitados.", "guests_did": "Se avisó a los invitados.",
        "empty": "Nada en la agenda.", "all_day": "Todo el día", "untitled": "(sin título)",
        "event": "(evento)",
    },
}


def _lang(state: dict) -> str:
    code = (state.get("session_lang") or state.get("lang") or "en")[:2].lower()
    return code if code in _L else "en"


def fmt_date(iso: str, lang: str) -> str:
    """'04/ago - Terça-feira'."""
    d = datetime.fromisoformat(iso)
    return f"{d.day:02d}/{_MONTHS[lang][d.month - 1]} - {_WEEKDAYS[lang][d.weekday()]}"


def fmt_time(iso: str, lang: str) -> str:
    """Morning → 12-hour with AM ('09:00 AM'); afternoon/evening → 24-hour ('16:00')."""
    d = datetime.fromisoformat(iso)
    return d.strftime("%I:%M %p") if d.hour < 12 else d.strftime("%H:%M")


def _dt(iso: str, lang: str) -> str:
    return f"{fmt_date(iso, lang)}, {fmt_time(iso, lang)}"


def _joined(lines: list) -> str:
    return "\n".join(x for x in lines if x is not None and x != "")


# --- confirmation prompts (before a write) ------------------------------------------------

def compose_create(action: dict, state: dict) -> str | None:
    lang = _lang(state); L = _L[lang]
    if not action.get("start"):
        return None
    lines = [f"{L['confirm_create']}:", action.get("title") or L["event"], _dt(action["start"], lang)]
    if action.get("virtual"):
        lines.append(L["video"])
    elif action.get("location"):
        lines.append(action["location"])
    if action.get("attendees"):
        lines.append(f"{L['with']} {', '.join(action['attendees'])}")
    lines += ["", L["ask_create"]]
    return _joined(lines)


def compose_update(action: dict, state: dict) -> str | None:
    lang = _lang(state); L = _L[lang]
    ev = (state.get("seen_events") or {}).get(action.get("event_id")) or {}
    start = action.get("start") or ev.get("start")
    title = ev.get("title") or action.get("title")
    if not (title or start):
        return None
    lines = [f"{L['confirm_update']}:", title or L["event"]]
    if start:
        lines.append(_dt(start, lang))
    if action.get("virtual"):
        lines.append(L["video"])
    elif action.get("location"):
        lines.append(action["location"])
    lines += ["", L["ask_update"]]
    return _joined(lines)


def compose_delete(action: dict, state: dict) -> str | None:
    lang = _lang(state); L = _L[lang]
    ev = (state.get("seen_events") or {}).get(action.get("event_id")) or {}
    if not ev:  # need the event's details to confirm a cancellation
        return None
    lines = [f"{L['confirm_delete']}:", ev.get("title") or L["event"]]
    if ev.get("start"):
        lines.append(_dt(ev["start"], lang))
    ask = L["ask_delete"] + (f" {L['guests_will']}" if ev.get("attendees") else "")
    lines += ["", ask]
    return _joined(lines)


# --- success cards (after a write) --------------------------------------------------------

def fmt_create(results: list, state: dict) -> str:
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['created']}:", d.get("title") or L["event"]]
    if d.get("start"):
        lines.append(_dt(d["start"], lang))
    if d.get("meet_link"):
        lines.append(L["video"])
    elif d.get("location"):
        lines.append(d["location"])
    if d.get("html_link"):
        lines.append(d["html_link"])
    n = len(d.get("attendees") or [])
    if n:
        lines.append(f"{n} {L['guests']}")
    return _joined(lines)


def fmt_update(results: list, state: dict) -> str:
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['updated']}:", d.get("title") or L["event"]]
    if d.get("start"):
        lines.append(_dt(d["start"], lang))
    if d.get("meet_link"):
        lines.append(L["video"])
    elif d.get("location"):
        lines.append(d["location"])
    return _joined(lines)


def fmt_delete(results: list, state: dict) -> str:
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['cancelled']}:", d.get("title") or L["event"]]
    if d.get("start"):
        lines.append(_dt(d["start"], lang))
    if d.get("had_attendees"):
        lines.append(L["guests_did"])
    return _joined(lines)


# --- the agenda (a read) ------------------------------------------------------------------

def fmt_list(results: list, state: dict) -> str:
    lang = _lang(state); L = _L[lang]
    items = (results[0].get("data") or {}).get("items") or []
    if not items:
        return L["empty"]
    order: list[str] = []
    by_day: dict[str, list] = {}
    for e in sorted(items, key=lambda e: e.get("start") or ""):
        iso = e.get("start")
        if not iso:
            continue
        day = iso[:10]
        if day not in by_day:
            by_day[day] = []
            order.append(iso)
        t = L["all_day"] if "T" not in iso else fmt_time(iso, lang)
        by_day[day].append(f"{t} - {e.get('title') or L['untitled']}")
    return "\n\n".join(fmt_date(iso, lang) + "\n" + "\n".join(by_day[iso[:10]]) for iso in order)
