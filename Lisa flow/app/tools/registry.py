"""The tool registry — one dict, fanned out into every seam.

Add a tool here and everything downstream updates: the enforced-JSON output schema, the
system-prompt tool list, the per-tool task prompts, the MCP connectors, and the local
handler instances. The graph never imports a concrete tool — only these fan-out functions."""
from __future__ import annotations

import os
from typing import Any

from .calendar import DESCRIBE as CALENDAR_DESCRIBE
from .calendar import GUIDANCE as CALENDAR_GUIDANCE
from .calendar import GoogleCalendarService
from .schemas import CALENDAR_TASK_SCHEMAS

# --- the registry -----------------------------------------------------------------------
#
# Each entry:
#   type          "local" | "anthropic_mcp"
#   describe      one line for the system-prompt tool list ({owner_name} templated)
#   guidance      per-task prompt block, appended to the system prompt ({owner_name} templated)
#   verbs         the domain's actions (local)
#   schemas       {verb: {"required":[...], "properties":{...}}} (local; -> output schema)
#   confirm_first set of verbs that need a go-ahead before executing (local)
#   handler       handler class, instantiated with settings (local)
#   server_url_env / token_env   env var names for the MCP connector (anthropic_mcp)
#
TOOLS: dict[str, dict[str, Any]] = {
    "calendar": {
        "type": "local",
        "describe": CALENDAR_DESCRIBE,
        "guidance": CALENDAR_GUIDANCE,
        "verbs": ["create", "list", "find", "update", "delete"],
        "schemas": CALENDAR_TASK_SCHEMAS,
        "confirm_first": {"create", "update", "delete"},
        "handler": GoogleCalendarService,
    },
    # Example MCP tool for later (no local handler):
    # "booking": {
    #     "type": "anthropic_mcp",
    #     "describe": "Book a table via the restaurant's MCP server.",
    #     "server_url_env": "BOOKING_MCP_URL",
    #     "token_env": "BOOKING_MCP_TOKEN",
    # },
}

# --- the output schema (enforced JSON) --------------------------------------------------

# Persistent gather memory: what the model has learned toward a goal in progress, so it can
# collect inputs across turns without re-asking. Nullable (null is meaningful). Cleared on
# every tag-reset so it can never leak across loops.
_WORKFLOW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task"],
    "properties": {
        "task": {"type": "string"},                 # e.g. "calendar.create"
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


def build_output_schema(tools: dict = TOOLS) -> dict:
    """The enforced-JSON contract handed to the reasoner each turn.

    Keeps Lisa's original fields (reasoning/state/message/lang) and adds `actions` +
    `workflow`. Every local tool x verb becomes ONE flat branch of the single `actions.items`
    anyOf — the only union in the actions area. Optional fields stay plain-typed (union-cap)."""
    branches: list[dict] = []
    for name, spec in tools.items():
        if spec.get("type") != "local":
            continue
        for verb, vs in spec["schemas"].items():
            props = {"task": {"const": f"{name}.{verb}"}}
            props.update(vs["properties"])
            branches.append({
                "type": "object",
                "additionalProperties": False,
                "required": ["task", *vs["required"]],
                "properties": props,
            })
    actions_items = {"anyOf": branches} if branches else {"type": "object"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reasoning", "state", "message", "lang", "actions", "workflow"],
        "properties": {
            "reasoning": {"type": "string"},
            "state": {"type": "string", "enum": ["keep_listening", "close"]},
            "message": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "lang": {"type": "string"},
            "actions": {"type": "array", "items": actions_items},
            "workflow": {"anyOf": [_WORKFLOW_SCHEMA, {"type": "null"}]},
        },
    }


# --- prompt fan-out ---------------------------------------------------------------------

def build_tools_prompt(tools: dict = TOOLS, owner_name: str = "the owner") -> str:
    """Human-readable tool list for the system prompt, marking how each is invoked."""
    lines: list[str] = []
    for name, spec in tools.items():
        desc = spec.get("describe", "").format(owner_name=owner_name)
        if spec.get("type") == "local":
            verbs = ", ".join(spec.get("verbs", []))
            lines.append(f"- {name} (run via actions): {desc} Tasks: {verbs}.")
        else:
            lines.append(f"- {name} (call directly): {desc}")
    return "\n".join(lines)


def build_task_prompts(tools: dict = TOOLS, owner_name: str = "the owner") -> str:
    """Stack every tool's own guidance block, templated with the owner name."""
    blocks = [
        spec["guidance"].format(owner_name=owner_name)
        for spec in tools.values()
        if spec.get("guidance")
    ]
    return "\n\n".join(blocks)


# --- runtime fan-out --------------------------------------------------------------------

def build_mcp_servers(tools: dict = TOOLS, settings=None) -> list[dict]:
    """URL connectors for each anthropic_mcp tool whose env vars are populated."""
    servers: list[dict] = []
    for name, spec in tools.items():
        if spec.get("type") != "anthropic_mcp":
            continue
        url = os.getenv(spec.get("server_url_env", ""))
        if not url:
            continue
        entry = {"type": "url", "name": name, "url": url}
        token = os.getenv(spec.get("token_env", "")) if spec.get("token_env") else None
        if token:
            entry["authorization_token"] = token
        servers.append(entry)
    return servers


def local_handlers(tools: dict = TOOLS, settings=None) -> dict[str, Any]:
    """{domain: handler_instance} for every local tool that has a handler."""
    out: dict[str, Any] = {}
    for name, spec in tools.items():
        if spec.get("type") != "local":
            continue
        handler = spec.get("handler")
        if handler is not None:
            out[name] = handler(settings)
    return out


def confirm_first(tools: dict = TOOLS) -> dict[str, set]:
    """{domain: {verbs needing a go-ahead}} — used by the execute node's confirm gate."""
    return {
        name: set(spec.get("confirm_first", set()))
        for name, spec in tools.items()
        if spec.get("type") == "local"
    }


# --- the union-cap guard ----------------------------------------------------------------

def count_unions(schema: Any) -> int:
    """Count every anyOf + every type:array param — Anthropic caps this at 16."""
    n = 0
    if isinstance(schema, dict):
        if "anyOf" in schema:
            n += 1
        if schema.get("type") == "array":
            n += 1
        for v in schema.values():
            n += count_unions(v)
    elif isinstance(schema, list):
        for v in schema:
            n += count_unions(v)
    return n


def count_optionals(schema: Any) -> int:
    """Count optional (not-required) parameters across the whole schema — Anthropic caps this
    at 24 (a SEPARATE limit from the 16 union cap; grammar compilation). For each object, that
    is len(properties) - len(required); summed recursively through anyOf branches / array items."""
    n = 0
    if isinstance(schema, dict):
        if isinstance(schema.get("properties"), dict):
            req = set(schema.get("required", []))
            n += sum(1 for k in schema["properties"] if k not in req)
        for v in schema.values():
            n += count_optionals(v)
    elif isinstance(schema, list):
        for v in schema:
            n += count_optionals(v)
    return n
