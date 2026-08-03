"""The skills registry — one dict, fanned out into every seam.

Register a skill here and everything downstream follows: the per-domain enforced-JSON schema,
the per-domain system prompt, the local handlers, the confirm/render policies, and the native
server tools. The graph never imports a concrete skill — only these fan-out functions and the
router (skills.router). This replaces the old tools/registry.py; each domain now gets its OWN
schema and prompt instead of one merged contract."""
from __future__ import annotations

from typing import Any

from .base import Skill, count_optionals, count_unions
from .calendar import CALENDAR
from .confirm import ConfirmPolicy
from .render import LLMReadback, Programmatic
from .web import WEB

SKILLS: dict[str, Skill] = {
    "calendar": CALENDAR,
    "web": WEB,
}

# Persistent gather memory (only meaningful for local-action skills). Cleared on every tag-reset
# so it can never leak across loops. Same shape the old registry used.
_WORKFLOW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task"],
    "properties": {
        "task": {"type": "string"},
        "known_inputs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "value"],
                "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "reason"],
                "properties": {"field": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
    },
}


# --- enforced-JSON output schema, per domain --------------------------------------------

def output_schema_for(domain: str, skills: dict = SKILLS) -> dict:
    """The enforced-JSON contract for ONE skill. Local skills (with verbs) get `actions` +
    `workflow`, where `actions.items` is an anyOf over just this domain's verbs; native skills
    (web) get the lean base — reasoning/state/message/lang, no actions."""
    skill = skills[domain]
    props: dict[str, Any] = {
        "reasoning": {"type": "string"},
        "state": {"type": "string", "enum": ["keep_listening", "close"]},
        "message": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "lang": {"type": "string"},
    }
    required = ["reasoning", "state", "message", "lang"]

    if skill.verbs:
        branches: list[dict] = []
        for verb in skill.verbs:
            vs = skill.schemas[verb]
            p = {"task": {"const": f"{skill.name}.{verb}"}}
            p.update(vs["properties"])
            branches.append({
                "type": "object",
                "additionalProperties": False,
                "required": ["task", *vs["required"]],
                "properties": p,
            })
        props["actions"] = {"type": "array", "items": {"anyOf": branches}}
        props["workflow"] = {"anyOf": [_WORKFLOW_SCHEMA, {"type": "null"}]}
        required += ["actions", "workflow"]

    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": props,
    }


def has_actions(domain: str, skills: dict = SKILLS) -> bool:
    return bool(skills[domain].verbs)


# --- per-domain system prompt -----------------------------------------------------------

def system_prompt_for(domain: str, settings, session_lang: str | None = None,
                      skills: dict = SKILLS) -> str:
    """Render the domain-scoped system prompt: the shared base + this skill's block. The
    actions/workflow JSON-contract section is included only for local skills."""
    from ..prompt import build_system_prompt

    skill = skills[domain]
    owner = settings.owner_name
    return build_system_prompt(
        owner, settings.primary_tag,
        guidance=skill.guidance.format(owner_name=owner),
        describe=skill.describe.format(owner_name=owner),
        has_actions=bool(skill.verbs),
        session_lang=session_lang,
    )


# --- runtime fan-out --------------------------------------------------------------------

def handlers(settings, skills: dict = SKILLS) -> dict[str, Any]:
    """{domain: handler_instance} for every local skill that has a handler (for execute)."""
    out: dict[str, Any] = {}
    for name, skill in skills.items():
        if skill.kind == "local" and skill.handler_cls is not None:
            out[name] = skill.handler_cls(settings)
    return out


def confirm_policies(skills: dict = SKILLS) -> dict[str, Any]:
    """{domain: ConfirmPolicy|None} — consulted by the confirm node."""
    return {name: skill.confirm for name, skill in skills.items()}


def render_policies(skills: dict = SKILLS) -> dict[str, Any]:
    """{domain: RenderPolicy|None} — consulted by the respond node."""
    return {name: skill.render for name, skill in skills.items()}


def server_tools_for(domain: str, settings, skills: dict = SKILLS):
    """The native Anthropic tool defs for a domain, or None. Resolves a builder(settings)."""
    st = skills[domain].server_tools
    if st is None:
        return None
    return st(settings) if callable(st) else st


__all__ = [
    "SKILLS", "Skill", "ConfirmPolicy", "LLMReadback", "Programmatic",
    "count_unions", "count_optionals",
    "output_schema_for", "has_actions", "system_prompt_for",
    "handlers", "confirm_policies", "render_policies", "server_tools_for",
]
