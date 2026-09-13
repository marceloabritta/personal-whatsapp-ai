"""Setup's programmatic prose — the confirmations it asks and the results it reports.

Every sentence here is built in CODE, which is the point: the words the owner approves and the
row that gets written come from the same place, so they cannot disagree. The model chooses a
verb; it never writes a confirmation and never reports a change.

Only two languages render programmatically (en/pt), matching the calendar formatter; anything
else falls back to the model (see nodes/respond.py)."""
from __future__ import annotations

import unicodedata

from ..roster import (
    ALL_CONTACTS, ALL_GROUPS, BOTH, DIRECTION_CHOICES, INBOUND, OUTBOUND, SCOPE_KIND,
)

LANGS = ("en", "pt")

# The direction labels. Fixed words, never translated: they are labels, so the list column reads
# the same every time. "both" is the one value whose label differs from its stored key.
LABEL = {INBOUND: "inbound", OUTBOUND: "outbound", BOTH: "in & out"}


def label_of(direction: str | None) -> str:
    return LABEL.get(direction or "", direction or "?")


# Blanket rules read as plain English rows, not as keys.
SCOPE_LABEL = {
    "en": {ALL_CONTACTS: "All contacts", ALL_GROUPS: "All groups"},
    "pt": {ALL_CONTACTS: "Todos os contatos", ALL_GROUPS: "Todos os grupos"},
}


def scope_label(key: str, lang: str = "en") -> str:
    table = SCOPE_LABEL["pt" if (lang or "en").startswith("pt") else "en"]
    return table.get(key, key)


def row_label(rule: dict, lang: str = "en") -> str:
    """What a row is called in the list and in a confirmation."""
    if rule.get("kind") == SCOPE_KIND:
        return scope_label(rule["chat_key"], lang)
    return rule.get("label") or rule.get("chat_key") or "?"


def _lang(state: dict) -> str:
    return (state.get("session_lang") or state.get("lang") or "en")[:2].lower()


def _name(view: dict | None, key: str) -> str:
    return (view or {}).get("label") or key


def _view(state: dict, key: str) -> dict:
    return (state.get("seen_chats") or {}).get(key) or {}


def fold(text: str) -> str:
    """Casefold + strip accents, for comparing a label the model echoed back against the one it
    was given ("Família Marciana" vs "familia marciana")."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


def resolve_target(action: dict, state: dict) -> tuple[str, str | None]:
    """(chat_key, ambiguous_label) — which chat this action targets.

    Three ways in, in priority order, and NONE of them require the model to carry an opaque id:
      1. `ordinal`   — the number printed on the last list or resolve;
      2. `chat_key`  — a key already surfaced this loop (a parsed card, a resolved group);
      3. a LABEL where a key was expected — resolved against the chats surfaced this loop.

    (3) exists because asking a model to copy "5511941261921-1363718480" across turns is the one
    place this design still did that, and it sent the group's NAME instead. The name is
    resolvable, so it is resolved — but only when it matches exactly one surfaced chat. Two
    chats with the same name come back as ambiguous rather than a coin toss.

    Used by BOTH the execute gate and the confirmation composer, so the chat named in the
    question is always the chat that gets written."""
    ordinal = (action or {}).get("ordinal")
    if ordinal is not None:
        return (state.get("listed_chats") or {}).get(str(ordinal)) or "", None

    seen_keys = state.get("seen_chat_keys") or []
    seen_chats = state.get("seen_chats") or {}

    key = ((action or {}).get("chat_key") or "").strip()
    if key and key in seen_keys:
        return key, None

    # Fall back to the NAME — from chat_key when a label was put there, or from the `label`
    # field, which the model often fills correctly even when it fumbles the key.
    for candidate in (key, ((action or {}).get("label") or "").strip()):
        wanted = fold(candidate)
        if not wanted:
            continue
        hits = [k for k, v in seen_chats.items()
                if fold((v or {}).get("label") or "") == wanted]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return "", candidate

    return key, None  # nothing resolved; the gate reports it


def target_key(action: dict, state: dict) -> str:
    """The chat this action targets, resolved the same way the execute gate will.

    The gate runs at EXECUTE, after the confirmation is composed — so without resolving here too,
    the question would be written before the number had been resolved and would name no one."""
    key, _ = resolve_target(action, state)
    return key


# --- confirmations (composed by the confirm node, before anything is written) ---------------

def compose_enroll(action: dict, state: dict) -> str:
    key = target_key(action, state)
    lang = _lang(state)
    if key in (ALL_CONTACTS, ALL_GROUPS):
        name = scope_label(key, lang)
        d = label_of(action.get("direction"))
        if lang == "pt":
            return (f"Confirmar — *{name}*: transcrever *{d}* em todas essas conversas, "
                    f"inclusive as que não estão na lista. Aplico?")
        return (f"Confirm — *{name}*: auto-transcribe *{d}* across all of them, including chats "
                f"not on the list. Apply it?")
    view = _view(state, key)
    name = action.get("label") or _name(view, key)
    kind = view.get("kind") or "contact"
    d = label_of(action.get("direction"))
    existing = (state.get("seen_chats") or {}).get(key, {}).get("direction")
    tail = " (group)" if kind == "group" else ""
    if _lang(state) == "pt":
        if existing:
            return f"Confirmar — *{name}*{tail}: mudar {label_of(existing)} → *{d}*?"
        return f"Confirmar — *{name}*{tail}: transcrever automaticamente os áudios *{d}*. Registro?"
    if existing:
        return f"Confirm — *{name}*{tail}: change {label_of(existing)} → *{d}*?"
    return f"Confirm — *{name}*{tail}: auto-transcribe *{d}* audio. Register it?"


def compose_update(action: dict, state: dict) -> str:
    key = target_key(action, state)
    view = _view(state, key)
    name = _name(view, key)
    n = _ordinal_of(state, key, action)
    head = f"{n}. {name}" if n else name
    kind = view.get("kind") or "contact"
    tail = " (group)" if kind == "group" else ""
    before = label_of(view.get("direction")) if view.get("direction") else None
    d = label_of(action.get("direction"))
    if _lang(state) == "pt":
        return (f"Confirmar — *{head}*{tail}: mudar {before} → *{d}*?" if before
                else f"Confirmar — *{head}*{tail}: mudar para *{d}*?")
    return (f"Confirm — *{head}*{tail}: change {before} → *{d}*?" if before
            else f"Confirm — *{head}*{tail}: change to *{d}*?")


def compose_remove(action: dict, state: dict) -> str:
    key = target_key(action, state)
    if key in (ALL_CONTACTS, ALL_GROUPS):
        name = scope_label(key, _lang(state))
        return (f"Limpar *{name}*? Só as conversas listadas continuam."
                if _lang(state) == "pt" else
                f"Clear *{name}*? Only the chats on the list keep being transcribed.")
    view = _view(state, key)
    name = _name(view, key)
    n = _ordinal_of(state, key, action)
    head = f"{n}. {name}" if n else name
    tail = " (group)" if (view.get("kind") == "group") else ""
    if _lang(state) == "pt":
        return (f"Remover *{head}*{tail}? Para de ser transcrito e sai da lista.")
    return f"Remove *{head}*{tail}? It stops being transcribed and drops off the list."


def _ordinal_of(state: dict, key: str, action: dict | None = None) -> str:
    """The number to echo back. What the owner typed when he used one; otherwise the number this
    chat carried on the last list."""
    if action is not None and action.get("ordinal") is not None:
        return str(action["ordinal"])
    return _ordinal_lookup(state, key)


def _ordinal_lookup(state: dict, key: str) -> str:
    """The number this chat was printed against in the last list, so the confirmation can echo
    it back — the cheapest way to catch a stale or misremembered number."""
    for n, k in (state.get("listed_chats") or {}).items():
        if k == key:
            return str(n)
    return ""


# --- result rendering -----------------------------------------------------------------------

def fmt_list(results: list, state: dict) -> str:
    """Everything configured: blanket rules, then contacts, then groups — each titled, all
    numbered in ONE sequence so any row can be edited or removed by its number.

    The blanket section is listed FIRST because it explains the rest: with "All contacts:
    outbound" set, a contact that appears nowhere below is still having your audio written out."""
    data = (results[0].get("data") or {}) if results else {}
    scopes = data.get("scopes") or []
    contacts = data.get("contacts") or []
    groups = data.get("groups") or []
    pt = _lang(state) == "pt"
    lang = "pt" if pt else "en"
    if not scopes and not contacts and not groups:
        return ("Transcrição — nada configurado ainda.\n\n"
                "Para adicionar um contato, me encaminhe o cartão dele. "
                "Para um grupo, é só dizer o nome.\n"
                'Ou ligue para todos de uma vez: "todos os contatos, outbound".' if pt else
                "Transcription — nothing configured yet.\n\n"
                "To add a contact, forward me their contact card. "
                "To add a group, just tell me its name.\n"
                'Or set them all at once: "all contacts, outbound".')

    rows = scopes + contacts + groups
    total = len(contacts) + len(groups)
    head = (f"Transcrição — {total} conversa(s)" if pt else
            f"Transcription — {total} chat{'s' if total != 1 else ''}")
    if scopes:
        head += (" + regras gerais" if pt else " + blanket rules")
    width = max((len(row_label(r, lang)) for r in rows), default=0)
    lines = [head]
    sections = (
        (("Todos" if pt else "Everyone"), scopes),
        (("Contatos" if pt else "Contacts"), contacts),
        (("Grupos" if pt else "Groups"), groups),
    )
    for title, bucket in sections:
        if not bucket:
            continue  # an empty section is omitted, not printed empty
        lines.append("")
        lines.append(f"*{title}*")
        for r in bucket:
            name = row_label(r, lang)
            lines.append(f"{r['n']:>2}. {name.ljust(width)}   {label_of(r.get('direction'))}")
    lines.append("")
    lines.append('Diga "edita 1 pra in & out" ou "remove 2".' if pt else
                 'Say "edit 1 to in & out" or "remove 2".')
    return "\n".join(lines)


def fmt_menu(results: list, state: dict) -> str:
    """The menu. Every word localised — an English "Setup." bolted onto a Portuguese sentence is
    what made the opening message change language halfway through."""
    data = (results[0].get("data") or {}) if results else {}
    items = data.get("items") or []
    pt = _lang(state) == "pt"
    lines = ["Configuração. Um item disponível hoje:" if pt
             else "Setup. One thing is configurable today:", ""]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. *{item['title']}* — {item['summary']}")
    return "\n".join(lines)


def direction_choices(lang: str = "en") -> str:
    """The three directions as a numbered list, always in the same order, so a reply of "1" is
    unambiguous. The direction WORDS never translate — they are labels (see LABEL)."""
    pt = (lang or "en").lower().startswith("pt")
    gloss = {
        INBOUND: "áudio que a outra pessoa envia" if pt else "audio the other person sends",
        OUTBOUND: "áudio que você envia" if pt else "audio you send",
        BOTH: "os dois" if pt else "both",
    }
    return "\n".join(f"{n}. *{label_of(d)}* — {gloss[d]}" for n, d in DIRECTION_CHOICES)


def fmt_enroll(results: list, state: dict) -> str:
    data = (results[0].get("data") or {}) if results else {}
    name = row_label(data, _lang(state)) if data.get("chat_key") else "?"
    d = label_of(data.get("direction"))
    pt = _lang(state) == "pt"
    return f"Pronto. {name} → {d}." if pt else f"Done. {name} → {d}."


def fmt_update(results: list, state: dict) -> str:
    data = (results[0].get("data") or {}) if results else {}
    name = row_label(data, _lang(state)) if data.get("chat_key") else "?"
    d = label_of(data.get("to") or data.get("direction"))
    pt = _lang(state) == "pt"
    return f"Pronto. {name} → {d}." if pt else f"Done. {name} → {d}."


def fmt_remove(results: list, state: dict) -> str:
    """A delete reports what went, then re-lists — the receipt, and the new numbering."""
    data = (results[0].get("data") or {}) if results else {}
    name = row_label(data, _lang(state)) if data.get("chat_key") else "?"
    pt = _lang(state) == "pt"
    head = (f"Removido. {name} saiu da lista." if pt else
            f"Removed. {name} is off the list.")
    remaining = data.get("remaining") or []
    scopes = data.get("scopes") or []
    if not remaining and not scopes:
        return head
    numbered_s, contacts, groups, n = [], [], [], 0
    for rule in scopes:
        n += 1
        numbered_s.append({**rule, "n": n})
    for rule in remaining:
        n += 1
        (groups if rule.get("kind") == "group" else contacts).append({**rule, "n": n})
    relist = fmt_list([{"data": {"scopes": numbered_s, "contacts": contacts,
                                 "groups": groups}}], state)
    return f"{head}\n\n{relist}"


def fmt_failure(results: list, state: dict) -> str:
    """An honest line when a setup action failed, in code — never handed back to the model,
    which would answer by silently re-proposing the same change."""
    first = next((r for r in results if not r.get("ok")), {})
    detail = first.get("summary") or ""
    pt = _lang(state) == "pt"
    head = ("Não consegui salvar essa mudança agora." if pt else
            "I couldn't save that change just now.")
    return f"{head}\n\n{detail}".strip()
