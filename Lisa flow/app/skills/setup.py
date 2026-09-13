"""The setup skill — self-chat configuration of the auto-transcription roster.

Assembles the domain policy around `tools/setup.py`: the prose and per-verb schemas live with
the handler, and this module bundles them into the Skill with the confirm policy, the per-verb
render policies, the router matcher, and the resolve gate.

Two things are structural here, not prompt-deep:

  only_self_chat  — the router will not route a turn to this skill outside the owner's chat with
                    himself, and the resolve gate refuses every setup action there regardless of
                    how it arrived. Configuration is not something a contact can talk Lisa into.
  resolve_gate    — a write only runs against a chat surfaced THIS loop: a forwarded contact
                    card, a group the resolver returned, or a row of the last list. The gate is
                    also where "edit 5" becomes a chat key, so the model never handles an id.
"""
from __future__ import annotations

import difflib
import unicodedata

from ..roster import normalize_scope
from ..tools.setup import DESCRIBE, GUIDANCE, RosterService
from ..tools.setup_schemas import SETUP_TASK_SCHEMAS
from .base import Skill
from .confirm import FlagConfirm
from .render import LLMReadback, Programmatic
from .setup_format import (
    compose_enroll, compose_remove, compose_update,
    fmt_enroll, fmt_failure, fmt_list, fmt_menu, fmt_remove, fmt_update,
)

# Explicit configuration words only. Deliberately narrow: "transcreve esse áudio" is a
# transcription REQUEST, not a request to configure transcription, and must not land here.
_SETUP_WORDS = {
    "setup", "settings", "configure", "configuration", "preferences",
    "configurar", "configuracao", "configuracoes", "config", "ajustes", "ajuste",
    "preferencias", "configura",
    "configuracion", "ajustar",
}


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s.lower())
    return " ".join(s.split())


def setup_matcher(text: str, *, threshold: float = 0.86) -> str:
    """"yes" | "no" — is this turn asking to configure the assistant? (no model call).

    Never returns "maybe": either an explicit setup word is present or the classifier can decide.
    The router additionally refuses this skill outside the self-chat, so a "yes" here is not
    enough on its own to route."""
    tokens = _normalize(text).split()
    for tok in tokens:
        if tok in _SETUP_WORDS:
            return "yes"
        if len(tok) >= 6 and any(
            difflib.SequenceMatcher(None, tok, w).ratio() >= threshold
            for w in _SETUP_WORDS if len(w) >= 6
        ):
            return "yes"
    return "no"


_WRITE_VERBS = {"enroll", "update", "remove"}


def setup_resolve_gate(verb: str, inputs: dict, state: dict):
    """(patched_inputs, error) — may this setup action run, and against which chat?

    Refuses outside the self-chat; resolves an `ordinal` from the last list into a chat key;
    refuses any key that was not surfaced this loop; and enriches the inputs with the jid, label
    and kind already known for that chat, so the handler never has to guess them."""
    if not state.get("is_self_chat"):
        return None, {"error": "not_self_chat",
                      "summary": "Setup only runs in your own chat with yourself."}
    if verb not in _WRITE_VERBS:
        return inputs, None

    listed = state.get("listed_chats") or {}
    seen_keys = state.get("seen_chat_keys") or []
    seen_chats = state.get("seen_chats") or {}

    key = (inputs.get("chat_key") or "").strip()
    ordinal = inputs.get("ordinal")

    # A blanket rule names no chat, so there is nothing to have surfaced: "all contacts" is
    # always a valid target. Normalised here so the handler sees the canonical key.
    scope = normalize_scope(key)
    if scope and ordinal is None:
        return {**{k: v for k, v in inputs.items() if k != "ordinal"}, "chat_key": scope}, None
    if ordinal is not None:  # the ordinal wins — it is what the owner actually typed
        key = listed.get(str(ordinal)) or ""
        if not key:
            return None, {"error": "unresolved_id",
                          "summary": f"There is no {ordinal} on the current list — "
                                     f"show the list again, then use the number it prints."}
    if not key:
        return None, {"error": "unresolved_id",
                      "summary": "No chat named. Forward a contact card, name a group, or use "
                                 "the number from the list."}
    if key not in seen_keys:
        return None, {"error": "unresolved_id",
                      "summary": "That chat was not found in this conversation — forward the "
                                 "contact card, or list/name the group first."}

    view = seen_chats.get(key) or {}
    patched = {k: v for k, v in inputs.items() if k != "ordinal"}
    patched["chat_key"] = key
    if view.get("chat_jid"):
        patched.setdefault("chat_jid", view["chat_jid"])
    if view.get("kind"):
        patched.setdefault("kind", view["kind"])
    if view.get("label") and not patched.get("label"):
        patched["label"] = view["label"]
    return patched, None


SETUP = Skill(
    name="setup",
    kind="local",
    describe=DESCRIBE,
    guidance=GUIDANCE,
    verbs=["menu", "list", "resolve", "enroll", "update", "remove"],
    schemas=SETUP_TASK_SCHEMAS,
    handler_cls=RosterService,
    # Every write waits for the owner's go-ahead, and the question he answers is composed in
    # code — so the sentence approved and the row written are the same thing.
    confirm=FlagConfirm(
        _WRITE_VERBS,
        compose_map={"enroll": compose_enroll, "update": compose_update,
                     "remove": compose_remove},
    ),
    # Everything renders programmatically except `resolve`: "which of these two groups?" is
    # judgment, so that one keeps the model.
    render={
        "menu": Programmatic(fmt_menu, on_failure=fmt_failure),
        "list": Programmatic(fmt_list, on_failure=fmt_failure),
        "enroll": Programmatic(fmt_enroll, on_failure=fmt_failure),
        "update": Programmatic(fmt_update, on_failure=fmt_failure),
        "remove": Programmatic(fmt_remove, on_failure=fmt_failure),
        "resolve": LLMReadback(),
    },
    matcher=setup_matcher,
    only_self_chat=True,
    resolve_gate=setup_resolve_gate,
    # Local, short, no live-web hops — the fast lane, same as calendar.
    model="claude-sonnet-5",
    effort="medium",
    think=False,
)
