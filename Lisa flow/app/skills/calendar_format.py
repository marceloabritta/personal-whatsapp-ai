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

from ..tools.calendar import changes, is_date_only, span_days

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
        "failed_transient": "Não consegui falar com o Google agora. Quer que eu tente de novo?",
        "u_title": "Novo título", "u_time": "Novo horário", "u_where": "Novo local",
        "days": "dias", "u_date": "Nova data", "u_all_day": "Passar para dia inteiro",
        "u_timed": "Passar para horário",
        "guests_silent": "Os convidados não serão avisados.",
        "guests_full": "Participantes (lista final)", "guests_dropped": "Removidos",
        "u_video_off": "Remover chamada de vídeo", "u_where_off": "Remover local",
        "u_guests_off": "Remover todos os convidados", "u_title_off": "Remover título",
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
        "failed_transient": "I couldn't reach Google just now. Want me to try again?",
        "u_title": "New title", "u_time": "New time", "u_where": "New location",
        "days": "days", "u_date": "New date", "u_all_day": "Make it all-day",
        "u_timed": "Give it a time",
        "guests_silent": "The guests won't be notified.",
        "guests_full": "Guests (final list)", "guests_dropped": "Removed",
        "u_video_off": "Remove video call", "u_where_off": "Remove location",
        "u_guests_off": "Remove all guests", "u_title_off": "Remove title",
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
        "failed_transient": "No pude conectar con Google ahora. ¿Lo intento de nuevo?",
        "u_title": "Nuevo título", "u_time": "Nueva hora", "u_where": "Nueva ubicación",
        "days": "días", "u_date": "Nueva fecha", "u_all_day": "Pasar a todo el día",
        "u_timed": "Poner hora",
        "guests_silent": "No se avisará a los invitados.",
        "guests_full": "Invitados (lista final)", "guests_dropped": "Quitados",
        "u_video_off": "Quitar videollamada", "u_where_off": "Quitar ubicación",
        "u_guests_off": "Quitar todos los invitados", "u_title_off": "Quitar título",
    },
}


def _guest_names(state: dict) -> dict:
    """{address: name} for this turn — the address book's contribution to a confirmation.

    Two sources, merged: `seen_contacts`, written by the reason node from the injected block and
    merged across the loop; and the {name, email} pairs the confirm node stripped THIS turn and
    handed in on a patched state, so a person Lisa has only just learned about is still named on
    the very card that proposes the meeting with them."""
    out: dict = {}
    for email, info in (state.get("seen_contacts") or {}).items():
        name = (info or {}).get("name")
        if name:
            out[email.lower()] = name
    for a in state.get("side_effects") or []:
        email = (a or {}).get("email")
        name = (a or {}).get("name")
        if email and name:
            out[email.lower()] = name
    return out


def _guest_line(email: str, names: dict) -> str:
    """'Ana Silva — ana@acme.com' when we know who that is, else the bare address."""
    name = names.get((email or "").lower())
    return f"{name} — {email}" if name else email


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


def _when(ev: dict, lang: str) -> list:
    """The date/time line(s) for any event, either kind — THE one renderer.

    Returns a list because an all-day event reads as two lines (the day, then "Dia inteiro")
    while a timed one reads as one. Never route a date-only ISO through _dt: fmt_time would
    turn it into midnight and print "12:00 AM" on a whole-day event."""
    L = _L[lang]
    iso = ev.get("start")
    if not iso:
        return []
    all_day = ev.get("all_day", is_date_only(iso))
    if not all_day:
        return [_dt(iso, lang)]
    iso = iso[:10]                      # coerce, exactly as the handler does
    last = (ev.get("end") or iso)[:10]
    try:
        n = span_days(iso, last)
        if n <= 1:
            return [fmt_date(iso, lang), L["all_day"]]
        return [f"{fmt_date(iso, lang)} \u2192 {fmt_date(last, lang)}",
                f"{L['all_day']} \u00b7 {n} {L['days']}"]
    except ValueError:
        # A date we cannot parse is a model typo, not a reason to drop the whole card —
        # show it raw so the owner can see what it is about to approve and correct it.
        return [iso, L["all_day"]]


def _guest_note(action: dict, L: dict) -> str:
    """Whether the guests are about to be emailed. `send_invites` is a query parameter, not an
    event field, so it never appears in the change-set — the owner would otherwise approve a
    write with no idea that eight invitations ride on it."""
    return L["guests_silent"] if action.get("send_invites") is False else L["guests_will"]


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
    lines = [f"{L['confirm_create']}:", "", action.get("title") or L["event"],
             *_when(action, lang)]
    if action.get("virtual"):
        lines.append(L["video"])
    elif action.get("location"):
        lines.append(action["location"])
    ask = L["ask_create"]
    if action.get("attendees"):
        names = _guest_names(state)
        lines.append(L["participants"])
        lines.extend(_guest_line(e, names) for e in action["attendees"])
        ask = f"{ask} {_guest_note(action, L)}"
    lines += ["", ask]
    return _joined(lines)


def compose_update(action: dict, state: dict) -> str | None:
    """Identify the event (its CURRENT title + time) and list WHAT is changing, so the owner can
    tell exactly what he's approving.

    The change-set is NOT recomputed here. It comes from tools.calendar.changes — the same
    function the body builder's presence rule is built on — because this file having its own
    idea of "what is changing" is precisely what broke: it tested truthiness, so `virtual: false`,
    `location: ""` and `attendees: []` all read as "not sent" and were silently left out. The
    worst of those still patched Google, so the owner approved an unnamed change that removed
    every guest from the meeting."""
    lang = _lang(state); L = _L[lang]
    ev = (state.get("seen_events") or {}).get(action.get("event_id")) or {}
    header = ev.get("title") or action.get("title")
    if not (header or ev.get("start")):
        return None  # nothing to identify the event by → let the model phrase it

    lines = [f"{L['confirm_update']}:", "", header or L["event"]]
    if ev.get("start"):
        lines.extend(_when(ev, lang))

    described: list[str] = []
    chs = changes(action, ev)
    by = {c["field"]: c for c in chs}
    # start / end / all_day describe ONE thing — when the event happens. Emitted per field they
    # produced two contradictory "Novo horário" lines on any multi-day or converting update.
    if {"start", "end", "all_day"} & set(by):
        after = {
            "start": by["start"]["new"] if "start" in by else ev.get("start"),
            "end": by["end"]["new"] if "end" in by else ev.get("end"),
            "all_day": by["all_day"]["new"] if "all_day" in by else ev.get("all_day"),
        }
        if "all_day" in by:
            described.append(L["u_all_day"] if after["all_day"] else L["u_timed"])
        when = _when(after, lang)
        if when:
            described.append(f"{(L['u_date'] if after['all_day'] else L['u_time'])}: {when[0]}")
            described.extend(when[1:])
    names = _guest_names(state)
    for ch in chs:
        if ch["field"] in ("start", "end", "all_day"):
            continue
        described.extend(_describe_change(ch, L, lang, names))

    if not described:
        # Nothing identifiable is changing. Saying "Posso alterar?" over an empty list asks the
        # owner to approve a blank; let the model explain instead.
        return None

    ask = L["ask_update"]
    if action.get("attendees") or ev.get("attendees"):
        ask = f"{ask} {_guest_note(action, L)}"
    lines += [""] + described + ["", ask]
    return _joined(lines)


def _describe_change(ch: dict, L: dict, lang: str, names: dict | None = None) -> list[str]:
    """One change-set entry → the line(s) the owner reads. Removals get their own words."""
    f, kind, new = ch["field"], ch["kind"], ch["new"]
    if f == "virtual":
        return [L["video"] if new else L["u_video_off"]]
    if f == "title":
        return [L["u_title_off"]] if kind == "clear" else [f"{L['u_title']}: {new}"]
    if f == "location":
        return [L["u_where_off"]] if kind == "clear" else [f"{L['u_where']}: {new}"]
    if f in ("start", "end"):
        return [f"{L['u_time']}: {_dt(new, lang)}"]
    if f == "attendees":
        if kind == "clear":
            return [L["u_guests_off"]]
        # Google's patch REPLACES the attendee array, so this list is the final roster, not an
        # addition. Naming it "Participantes" read as "these are being added" — and anyone the
        # model left out was silently uninvited AND mailed a cancellation.
        out = [L["guests_full"], *(_guest_line(e, names or {}) for e in new)]
        dropped = sorted(set(ch.get("old") or []) - set(new))
        if dropped:
            out.append(f"{L['guests_dropped']}: {', '.join(dropped)}")
        return out
    return []


def compose_delete(action: dict, state: dict) -> str | None:
    lang = _lang(state); L = _L[lang]
    ev = (state.get("seen_events") or {}).get(action.get("event_id")) or {}
    if not ev:  # need the event's details to confirm a cancellation
        return None
    lines = [f"{L['confirm_delete']}:", "", ev.get("title") or L["event"]]
    if ev.get("start"):
        lines.extend(_when(ev, lang))
    if ev.get("attendees"):
        names = _guest_names(state)
        lines.append(L["participants"])
        lines.extend(_guest_line(e, names) for e in ev["attendees"])
    ask = L["ask_delete"] + (f" {_guest_note(action, L)}" if ev.get("attendees") else "")
    lines += ["", ask]
    return _joined(lines)


# --- success cards (after a write) --------------------------------------------------------

def fmt_create(results: list, state: dict) -> str:
    # Kept deliberately minimal — same shape whether virtual or in-person: heading, blank, title,
    # date/time, blank, the event link. The details were already in the confirmation.
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['created']}:", "", d.get("title") or L["event"]]
    lines.extend(_when(d, lang))
    if d.get("attendees"):
        lines.append(L["guests_did"] if d.get("notified") else L["guests_silent"])
    if d.get("html_link"):
        lines += ["", f"{L['event_link']}: {d['html_link']}"]
    return _joined(lines)


def fmt_update(results: list, state: dict) -> str:
    # The changes were listed in the confirmation just above, so the card points back to them
    # ("now with the details above") instead of re-listing.
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['updated']}:", "", d.get("title") or L["event"]]
    lines.extend(_when(d, lang))
    if d.get("attendees"):
        lines.append(L["guests_did"] if d.get("notified") else L["guests_silent"])
    lines += ["", L["u_done"]]
    if d.get("html_link"):
        lines += ["", f"{L['event_link']}: {d['html_link']}"]
    return _joined(lines)


def fmt_delete(results: list, state: dict) -> str:
    lang = _lang(state); L = _L[lang]
    d = (results[0].get("data") or {})
    lines = [f"{L['cancelled']}:", "", d.get("title") or L["event"]]
    lines.extend(_when(d, lang))
    if d.get("had_attendees"):
        lines.append(L["guests_did"] if d.get("notified", True) else L["guests_silent"])
    return _joined(lines)


# --- the agenda (a read) ------------------------------------------------------------------

def fmt_failure(results: list, state: dict) -> str:
    """What we say when the tool call failed.

    Success has always been composed in code; failure was the one case left to the model, and
    the model's calendar guidance tells it to keep `message` null and re-emit the action — so a
    failed write came back as the SAME confirmation question, with no hint anything had gone
    wrong. This is the missing half: short, honest, in the session language."""
    lang = _lang(state)
    return _L[lang]["failed_transient"]


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
        if e.get("all_day", "T" not in iso):
            n = e.get("days") or 1
            t = L["all_day"] if n <= 1 else f"{L['all_day']} \u00b7 {n} {L['days']}"
        else:
            t = fmt_time(iso, lang)
        by_day[day].append(f"{t} - {e.get('title') or L['untitled']}")
    # The day header is bold (WhatsApp *…* — it has no underline) so each day stands out.
    return "\n\n".join(f"*{fmt_date(iso, lang)}*\n" + "\n".join(by_day[iso[:10]]) for iso in order)
