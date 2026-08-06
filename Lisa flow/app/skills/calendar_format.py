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
    # PT weekdays without the "-feira" suffix — shorter and cleaner ("segunda", not "segunda-feira").
    "pt": ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "es": ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"],
}
_L = {
    "pt": {
        "confirm_create": "Confirmando", "confirm_update": "Confirmando alteração",
        "confirm_delete": "Confirmando cancelamento",
        "ask_create": "Posso agendar?", "ask_update": "Posso alterar?", "ask_delete": "Cancelar?",
        "created": "Agendado", "updated": "Alterado", "cancelled": "Cancelado",
        "video": "Chamada de vídeo", "participants": "Participantes", "event_link": "Link do evento",
        "u_done": "Agora com os detalhes acima.",
        "guests_will": "Os convidados serão avisados.", "guests_did": "Os convidados foram avisados.",
        "empty": "Nada na agenda.", "all_day": "Dia inteiro", "untitled": "(sem título)",
        "event": "(evento)",
        "u_title": "Novo título", "u_time": "Novo horário", "u_where": "Novo local", "u_guests": "Adicionar",
    },
    "en": {
        "confirm_create": "Confirming", "confirm_update": "Confirming this change",
        "confirm_delete": "Confirming this cancellation",
        "ask_create": "Shall I schedule it?", "ask_update": "Shall I change it?",
        "ask_delete": "Cancel this one?",
        "created": "Scheduled", "updated": "Updated", "cancelled": "Cancelled",
        "video": "Video call", "participants": "Guests", "event_link": "Event link",
        "u_done": "Now with the details above.",
        "guests_will": "The guests will be notified.", "guests_did": "The guests were notified.",
        "empty": "Nothing on your calendar.", "all_day": "All day", "untitled": "(no title)",
        "event": "(event)",
        "u_title": "New title", "u_time": "New time", "u_where": "New location", "u_guests": "Add",
    },
    "es": {
        "confirm_create": "Confirmando", "confirm_update": "Confirmando el cambio",
        "confirm_delete": "Confirmando la cancelación",
        "ask_create": "¿Lo agendo?", "ask_update": "¿Lo cambio?", "ask_delete": "¿Lo cancelo?",
        "created": "Agendado", "updated": "Actualizado", "cancelled": "Cancelado",
        "video": "Videollamada", "participants": "Invitados", "event_link": "Enlace del evento",
        "u_done": "Ahora con los detalles de arriba.",
        "guests_will": "Se avisará a los invitados.", "guests_did": "Se avisó a los invitados.",
        "empty": "Nada en la agenda.", "all_day": "Todo el día", "untitled": "(sin título)",
        "event": "(evento)",
        "u_title": "Nuevo título", "u_time": "Nueva hora", "u_where": "Nueva ubicación", "u_guests": "Añadir",
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


def _cap(line: str) -> str:
    # Capitalize the first character of a line when it's a letter (so a name-email "ana@x.com" →
    # "Ana@x.com"); lines starting with a digit or symbol (dates "05/ago", the "*bold*" header) are
    # left untouched.
    return line[0].upper() + line[1:] if line and line[0].islower() else line


def _joined(lines: list) -> str:
    # Keep intentional blank lines ("" separators between sections); drop only absent fields (None).
    # The formatters never append "" for a missing field — they skip it — so nothing collapses.
    return "\n".join(_cap(x) for x in lines if x is not None)


# --- confirmation prompts (before a write) ------------------------------------------------

def compose_create(action: dict, state: dict) -> str | None:
    lang = _lang(state); L = _L[lang]
    if not action.get("start"):
        return None
    lines = [f"{L['confirm_create']}:", "", action.get("title") or L["event"], _dt(action["start"], lang)]
    if action.get("virtual"):
        lines.append(L["video"])
    elif action.get("location"):
        lines.append(action["location"])
    if action.get("attendees"):
        lines.append(L["participants"])
        lines.extend(action["attendees"])
    lines += ["", L["ask_create"]]
    return _joined(lines)


def compose_update(action: dict, state: dict) -> str | None:
    """Identify the event (its CURRENT title + time) and list WHAT is changing, so the owner can
    tell exactly what he's approving. New values come from the action, old from the cached event."""
    lang = _lang(state); L = _L[lang]
    ev = (state.get("seen_events") or {}).get(action.get("event_id")) or {}
    header = ev.get("title") or action.get("title")
    if not (header or ev.get("start")):
        return None  # nothing to identify the event by → let the model phrase it

    lines = [f"{L['confirm_update']}:", "", header or L["event"]]
    if ev.get("start"):
        lines.append(_dt(ev["start"], lang))

    changes: list[str] = []
    if action.get("title") and action["title"] != ev.get("title"):
        changes.append(f"{L['u_title']}: {action['title']}")
    if action.get("start") and action["start"] != ev.get("start"):
        changes.append(f"{L['u_time']}: {_dt(action['start'], lang)}")
    if action.get("virtual"):
        changes.append(L["video"])
    elif action.get("location") and action["location"] != ev.get("location"):
        changes.append(f"{L['u_where']}: {action['location']}")
    if action.get("attendees"):
        added = [a for a in action["attendees"] if a not in (ev.get("attendees") or [])]
        if added:  # same shape as create/delete: a Participantes label + one email per line
            changes.append(L["participants"])
            changes.extend(added)

    if changes:
        lines += [""] + changes
    lines += ["", L["ask_update"]]
    return _joined(lines)


def compose_delete(action: dict, state: dict) -> str | None:
    lang = _lang(state); L = _L[lang]
    ev = (state.get("seen_events") or {}).get(action.get("event_id")) or {}
    if not ev:  # need the event's details to confirm a cancellation
        return None
    lines = [f"{L['confirm_delete']}:", "", ev.get("title") or L["event"]]
    if ev.get("start"):
        lines.append(_dt(ev["start"], lang))
    if ev.get("attendees"):
        lines.append(L["participants"])
        lines.extend(ev["attendees"])
    ask = L["ask_delete"] + (f" {L['guests_will']}" if ev.get("attendees") else "")
    lines += ["", ask]
    return _joined(lines)


# --- success cards (after a write) --------------------------------------------------------

def fmt_create(results: list, state: dict) -> str:
    # Kept deliberately minimal — same shape whether virtual or in-person: heading, blank, title,
    # date/time, blank, the event link. The details were already in the confirmation.
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['created']}:", "", d.get("title") or L["event"]]
    if d.get("start"):
        lines.append(_dt(d["start"], lang))
    if d.get("html_link"):
        lines += ["", f"{L['event_link']}: {d['html_link']}"]
    return _joined(lines)


def fmt_update(results: list, state: dict) -> str:
    # The changes were listed in the confirmation just above, so the card points back to them
    # ("now with the details above") instead of re-listing.
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['updated']}:", "", d.get("title") or L["event"]]
    if d.get("start"):
        lines.append(_dt(d["start"], lang))
    lines += ["", L["u_done"]]
    if d.get("html_link"):
        lines += ["", f"{L['event_link']}: {d['html_link']}"]
    return _joined(lines)


def fmt_delete(results: list, state: dict) -> str:
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['cancelled']}:", "", d.get("title") or L["event"]]
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
    # The day header is bold (WhatsApp *…* — it has no underline) so each day stands out.
    return "\n\n".join(f"*{fmt_date(iso, lang)}*\n" + "\n".join(by_day[iso[:10]]) for iso in order)
