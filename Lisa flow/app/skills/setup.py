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
from ..tools.setup import DESCRIBE, GUIDANCE, PRESETS_GUIDANCE, RosterService
from ..tools.setup_schemas import SETUP_TASK_SCHEMAS
from .base import Skill
from .confirm import FlagConfirm
from .render import LLMReadback, Programmatic
from .setup_format import (
    compose_enroll, compose_remove, compose_remove_place, compose_save_place,
    compose_set_presets, compose_update, resolve_target,
    fmt_enroll, fmt_failure, fmt_help, fmt_list, fmt_menu, fmt_remove, fmt_remove_place,
    fmt_save_place, fmt_set_presets, fmt_update,
)

# Explicit configuration words only. Deliberately narrow: "transcreve esse áudio" is a
# transcription REQUEST, not a request to configure transcription, and must not land here.
# Equally, "reunião de 30 minutos" is a booking, not a preset: the presets are reached through
# setup (or by the word "presets"), never by meeting vocabulary, which belongs to the calendar.
_SETUP_WORDS = {
    "setup", "settings", "configure", "configuration", "preferences",
    "configurar", "configuracao", "configuracoes", "config", "ajustes", "ajuste",
    "preferencias", "configura",
    "configuracion", "ajustar",
    "presets", "preset", "defaults",
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
# The presets' writes name no chat, so the chat-resolution half of the gate does not apply to
# them — only the self-chat half does, and that one runs for every verb.
_PRESET_WRITES = {"set_presets", "save_place", "remove_place"}


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

    # A blanket rule names no chat, so there is nothing to have surfaced: "all contacts" is
    # always a valid target. Normalised here so the handler sees the canonical key.
    scope = normalize_scope(inputs.get("chat_key"))
    if scope and inputs.get("ordinal") is None:
        return {**{k: v for k, v in inputs.items() if k != "ordinal"}, "chat_key": scope}, None

    seen_keys = state.get("seen_chat_keys") or []
    seen_chats = state.get("seen_chats") or {}

    key, ambiguous = resolve_target(inputs, state)
    if ambiguous:
        return None, {"error": "unresolved_id",
                      "summary": f"More than one chat here is called {ambiguous!r} — say its "
                                 f"number from the list instead."}
    if inputs.get("ordinal") is not None and not key:
        return None, {"error": "unresolved_id",
                      "summary": f"There is no {inputs['ordinal']} on the current list — "
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
    verbs=["menu", "help", "list", "resolve", "enroll", "update", "remove",
           "set_presets", "save_place", "remove_place"],
    schemas=SETUP_TASK_SCHEMAS,
    handler_cls=RosterService,
    # Every write waits for the owner's go-ahead, and the question he answers is composed in
    # code — so the sentence approved and the row written are the same thing.
    confirm=FlagConfirm(
        _WRITE_VERBS | _PRESET_WRITES,
        compose_map={"enroll": compose_enroll, "update": compose_update,
                     "remove": compose_remove, "set_presets": compose_set_presets,
                     "save_place": compose_save_place, "remove_place": compose_remove_place},
    ),
    # Everything renders programmatically except `resolve`: "which of these two groups?" is
    # judgment, so that one keeps the model.
    render={
        "menu": Programmatic(fmt_menu, on_failure=fmt_failure),
        "help": Programmatic(fmt_help, on_failure=fmt_failure),
        "list": Programmatic(fmt_list, on_failure=fmt_failure),
        "enroll": Programmatic(fmt_enroll, on_failure=fmt_failure),
        "update": Programmatic(fmt_update, on_failure=fmt_failure),
        "remove": Programmatic(fmt_remove, on_failure=fmt_failure),
        "set_presets": Programmatic(fmt_set_presets, on_failure=fmt_failure),
        "save_place": Programmatic(fmt_save_place, on_failure=fmt_failure),
        "remove_place": Programmatic(fmt_remove_place, on_failure=fmt_failure),
        "resolve": LLMReadback(),
    },
    matcher=setup_matcher,
    only_self_chat=True,
    resolve_gate=setup_resolve_gate,
    # A setup loop keeps its untagged follow-ups. Its subject now overlaps the calendar's
    # vocabulary — "meetings of 30 minutes", "don't send the invite" — and the calendar matcher
    # would otherwise pull every such answer out of setup into a booking. A fresh tag still
    # re-decides, so "@lisa marca reunião amanhã" from inside setup is a booking.
    holds_loop=True,
    # The presets exist only when they are switched on: off, their verbs leave the schema and
    # their section leaves the prompt, so the build is the pre-feature one.
    gated_verbs={v: "presets_enabled" for v in _PRESET_WRITES},
    gated_guidance=[("presets_enabled", PRESETS_GUIDANCE)],
    # Local, short, no live-web hops — the fast lane, same as calendar.
    model="claude-sonnet-5",
    effort="medium",
    think=False,
)
