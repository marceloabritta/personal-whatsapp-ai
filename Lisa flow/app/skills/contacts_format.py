"""Contacts' programmatic prose — the confirmations it asks and the results it reports.

Every sentence is built in CODE, so the words the owner approves and the write that runs come from
the same action and cannot disagree. Localized like the calendar cards (pt / en / es); anything else
falls back to the model (see nodes/respond.py).

Each composer shows old values only as far as this chat may read them: a card is posted in the chat,
so changing Ana's address from a chat with Felipe must not show Felipe what Ana's address was. A new
value is shown as it is, because it is in this chat already (the grounding check saw to that)."""
from __future__ import annotations

from ..tools.contacts import mask, values_of

LANGS = {"pt", "en", "es"}

_L = {
    "pt": {
        "confirm_create": "Confirmando novo contato", "confirm_update": "Confirmando alteração no contato",
        "confirm_delete": "Confirmando exclusão do contato",
        "ask_create": "Posso salvar?", "ask_update": "Posso alterar?",
        "ask_delete": "Apagar este contato?",
        "created": "Contato salvo", "updated": "Contato atualizado", "deleted": "Contato apagado",
        "noop": "Esse contato já está assim", "add": "Adicionar", "remove": "Remover",
        "added": "Adicionado", "removed": "Removido",
        "overwrites": "substitui o que estiver lá",
        "stuck": "Continua aparecendo, porque vem do perfil Google da pessoa e não do seu contato",
        "promote": "Ainda não está no Google: vou criar o contato.",
        "failed_transient": "Não consegui falar com o Google agora. Quer que eu tente de novo?",
        "failed_create": "O Google não confirmou se o contato foi criado. Confira antes de pedir "
                         "de novo, para não duplicar.",
        "email": "E-mail", "phone": "Telefone", "name": "Nome", "nickname": "Apelido",
        "company": "Empresa", "title": "Cargo", "address": "Endereço", "birthday": "Aniversário",
        "notes": "Notas",
    },
    "en": {
        "confirm_create": "Confirming new contact", "confirm_update": "Confirming this contact change",
        "confirm_delete": "Confirming contact deletion",
        "ask_create": "Shall I save it?", "ask_update": "Shall I change it?",
        "ask_delete": "Delete this contact?",
        "created": "Contact saved", "updated": "Contact updated", "deleted": "Contact deleted",
        "noop": "This contact already says that", "add": "Add", "remove": "Remove",
        "added": "Added", "removed": "Removed",
        "overwrites": "replaces whatever is there",
        "stuck": "Still showing, because it comes from their Google profile and not your contact",
        "promote": "Not in Google yet: I'll create the contact.",
        "failed_transient": "I couldn't reach Google just now. Want me to try again?",
        "failed_create": "Google didn't confirm whether the contact was created. Check before "
                         "asking again, so it isn't duplicated.",
        "email": "Email", "phone": "Phone", "name": "Name", "nickname": "Nickname",
        "company": "Company", "title": "Title", "address": "Address", "birthday": "Birthday",
        "notes": "Notes",
    },
    "es": {
        "confirm_create": "Confirmando nuevo contacto", "confirm_update": "Confirmando el cambio del contacto",
        "confirm_delete": "Confirmando la eliminación del contacto",
        "ask_create": "¿Lo guardo?", "ask_update": "¿Lo cambio?",
        "ask_delete": "¿Borro este contacto?",
        "created": "Contacto guardado", "updated": "Contacto actualizado", "deleted": "Contacto borrado",
        "noop": "Este contacto ya está así", "add": "Agregar", "remove": "Quitar",
        "added": "Agregado", "removed": "Quitado",
        "overwrites": "reemplaza lo que haya",
        "stuck": "Sigue apareciendo, porque viene de su perfil de Google y no de tu contacto",
        "promote": "Aún no está en Google: voy a crear el contacto.",
        "failed_transient": "No pude conectar con Google ahora. ¿Lo intento de nuevo?",
        "failed_create": "Google no confirmó si el contacto se creó. Revísalo antes de pedirlo de "
                         "nuevo, para no duplicarlo.",
        "email": "Correo", "phone": "Teléfono", "name": "Nombre", "nickname": "Apodo",
        "company": "Empresa", "title": "Cargo", "address": "Dirección", "birthday": "Cumpleaños",
        "notes": "Notas",
    },
}


def _lang(state: dict) -> str:
    code = (state.get("session_lang") or state.get("lang") or "en")[:2].lower()
    return code if code in LANGS else "en"


def view_of(action: dict, state: dict) -> dict:
    """The card this action targets, as this loop saw it."""
    return (state.get("seen_people") or {}).get((action or {}).get("contact_id")) or {}


def _shown(field: str, value: str, view: dict) -> str:
    if field == "notes" and not view.get("self_chat"):
        return mask(field, value)  # what he wrote about someone is not for their own chat
    return value if view.get("visible") else mask(field, value)


def _old_value(ch: dict, view: dict) -> str:
    """The value being replaced: the one named, or the card's only one."""
    old = (ch.get("old") or "").strip()
    if old:
        return old
    held = values_of(view, ch.get("field"))
    return held[0] if len(held) == 1 else ""


# A card holds one of each, so "adding" to one of these replaces it, and the card must say so. Notes
# hold one too, but an add appends to them (tools/people.py).
_ONE_VALUE = {"name", "company", "title", "birthday"}
# Read only with the whole card, so without it the card cannot name what it is about to overwrite.
_NEEDS_WHOLE_CARD = {"title", "birthday", "notes", "address"}


def _change_line(ch: dict, view: dict, L: dict) -> str:
    field, op = ch.get("field"), ch.get("op")
    new = (ch.get("new") or "").strip()
    label = L.get(field, field)
    if op == "add" and field not in _ONE_VALUE:
        return f"{L['add']} {label.lower()}: {new}"
    if op == "remove":
        old = _old_value(ch, view)
        return (f"{L['remove']} {label.lower()}: {_shown(field, old, view)}"
                if old else f"{L['remove']} {label.lower()}")
    old = _old_value(ch, view)
    if old:
        return f"{label}: {_shown(field, old, view)} → {new}"
    if field in _NEEDS_WHOLE_CARD and not view.get("whole"):
        return f"{label} ({L['overwrites']}): {new}"
    return f"{label}: {new}"


# --- confirmation prompts (before a write) ------------------------------------------------

def compose_create(action: dict, state: dict) -> str | None:
    L = _L[_lang(state)]
    name = (action.get("name") or "").strip()
    if not name:
        return None
    lines = [f"{L['confirm_create']}:", "", name]
    for ch in action.get("changes") or []:
        lines.append(f"{L.get(ch.get('field'), ch.get('field'))}: {(ch.get('new') or '').strip()}")
    lines += ["", L["ask_create"]]
    return "\n".join(lines)


def compose_update(action: dict, state: dict) -> str | None:
    L = _L[_lang(state)]
    view = view_of(action, state)
    changes = action.get("changes") or []
    if not view or not changes:
        return None
    lines = [f"{L['confirm_update']}:", "", view.get("name") or "(?)"]
    lines.extend(_change_line(ch, view, L) for ch in changes)
    if view.get("source") == "learned":
        lines += ["", L["promote"]]
    lines += ["", L["ask_update"]]
    return "\n".join(lines)


def compose_delete(action: dict, state: dict) -> str | None:
    L = _L[_lang(state)]
    view = view_of(action, state)
    if not view:
        return None
    lines = [f"{L['confirm_delete']}:", "", view.get("name") or "(?)"]
    if view.get("company"):
        lines.append(view["company"])
    if view.get("visible"):
        lines.extend(view.get("emails") or [])
        lines.extend(view.get("phones") or [])
    lines += ["", L["ask_delete"]]
    return "\n".join(lines)


# --- success cards (after a write) --------------------------------------------------------

def fmt_create(results: list, state: dict) -> str:
    L = _L[_lang(state)]
    d = results[0].get("data") or {}
    return "\n".join([f"{L['created']}:", "", d.get("name") or ""])


def fmt_update(results: list, state: dict) -> str:
    L = _L[_lang(state)]
    d = results[0].get("data") or {}
    name = d.get("name") or ""
    stuck = d.get("stuck") or []
    if d.get("noop"):
        # Nothing moved. If that was because the value lives on his profile, "already says that"
        # would be the opposite of true, so the reason is the whole message.
        if stuck:
            return "\n".join([name, "", f"{L['stuck']}: " + ", ".join(stuck)])
        return "\n".join([f"{L['noop']}:", "", name])
    view = {"visible": d.get("visible")}
    lines = [f"{L['updated']}:", "", name]
    for ch in d.get("changes") or []:
        field, op = ch.get("field"), ch.get("op")
        label = L.get(field, field)
        if op == "remove":
            old = (ch.get("old") or "").strip()
            lines.append(f"{L['removed']}: {_shown(field, old, view)}" if old
                         else f"{L['removed']}: {label.lower()}")
        else:
            lines.append(f"{label}: {(ch.get('new') or '').strip()}")
    if stuck:
        lines += ["", f"{L['stuck']}: " + ", ".join(stuck)]
    return "\n".join(lines)


def fmt_delete(results: list, state: dict) -> str:
    L = _L[_lang(state)]
    d = results[0].get("data") or {}
    return "\n".join([f"{L['deleted']}:", "", d.get("name") or ""])


def fmt_failure(results: list, state: dict) -> str:
    """A transient failure, reported in code. `create` says more: Google may have saved it before
    the connection died, and "try again" on a create is how a real address book gets duplicates."""
    L = _L[_lang(state)]
    verb = ((results[0].get("data") or {}).get("verb")) if results else None
    return L["failed_create"] if verb == "create" else L["failed_transient"]
