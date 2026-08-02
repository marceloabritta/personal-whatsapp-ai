"""The one tool registry — declared at startup, fanned out into the enforced-JSON
schema (local tools), the MCP connector config (mcp tools), and the prompt (both).

Add a tool here and everything downstream updates."""
from __future__ import annotations

from .calendar import GUIDANCE as CALENDAR_GUIDANCE
from .calendar import GoogleCalendarService
from .schemas import CALENDAR_TASK_SCHEMAS

# type: "local"  → handler class (built with settings in deps); contributes to actions schema
# type: "anthropic_mcp" → server_url/token env names; adapted onto the MCP connector
TOOLS: dict[str, dict] = {
    "calendar": {
        "type": "local",
        "handler": GoogleCalendarService,  # class — instantiated in deps with settings
        "verbs": ["create", "list", "update", "delete"],
        "schemas": CALENDAR_TASK_SCHEMAS,
        "confirm_first": {"create", "update", "delete"},
        "describe": "Create, find, reschedule or cancel events on the owner's Google Calendar.",
        "guidance": CALENDAR_GUIDANCE,  # per-task prompt, appended by build_task_prompts
    },
    # --- examples for later; not registered yet ---
    # "contacts": {"type": "local", "handler": GoogleContactsService, ...},
    # "tasks":    {"type": "local", "handler": GoogleTasksService, ...},
    # "booking":  {"type": "anthropic_mcp", "server_url_env": "BOOKING_MCP_URL", "token_env": "BOOKING_TOKEN",
    #              "describe": "Search and book restaurants."},
}

_WORKFLOW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task", "known_inputs", "open_questions"],
    "properties": {
        "task": {"type": "string"},
        "known_inputs": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["field", "value"],
                "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["field", "reason"],
                "properties": {"field": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
    },
}


def build_output_schema(tools: dict = TOOLS) -> dict:
    """The full enforced-JSON schema handed to the reasoner each turn."""
    branches = []
    for name, spec in tools.items():
        if spec.get("type") != "local":
            continue
        for verb, inp in spec["schemas"].items():
            branches.append({
                "type": "object", "additionalProperties": False,
                "required": ["task", "inputs"],
                "properties": {"task": {"const": f"{name}.{verb}"}, "inputs": inp},
            })
    actions_items = {"anyOf": branches} if branches else {"type": "object"}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["lang", "next_message", "loop_state", "actions", "workflow"],
        "properties": {
            "lang": {"type": "string"},
            "next_message": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "loop_state": {"type": "string", "enum": ["keep_listening", "close_loop"]},
            "actions": {"type": "array", "items": actions_items},
            "workflow": {"anyOf": [_WORKFLOW_SCHEMA, {"type": "null"}]},
        },
    }


def build_mcp_servers(tools: dict, settings) -> list[dict]:
    """anthropic_mcp tools → Anthropic connector `mcp_servers` entries."""
    import os

    servers = []
    for name, spec in tools.items():
        if spec.get("type") != "anthropic_mcp":
            continue
        url = os.getenv(spec.get("server_url_env", ""))
        token = os.getenv(spec.get("token_env", ""))
        if not url:
            continue
        entry = {"type": "url", "name": name, "url": url}
        if token:
            entry["authorization_token"] = token
        servers.append(entry)
    return servers


def build_tools_prompt(tools: dict = TOOLS) -> str:
    """Human-readable tool list for the system prompt — marks how each is invoked."""
    lines = []
    for name, spec in tools.items():
        if spec.get("type") == "local":
            verbs = ", ".join(f"{name}.{v}" for v in spec["verbs"])
            lines.append(f"- {name} (run via actions): {spec['describe']} Tasks: {verbs}.")
        else:
            lines.append(f"- {name} (call directly): {spec.get('describe','')}")
    return "\n".join(lines) if lines else "(no tools)"


def build_task_prompts(tools: dict = TOOLS, owner_name: str = "the owner") -> str:
    """Per-task guidance blocks — each tool's `guidance`, rendered and stacked.

    Every registered tool may carry a `guidance` template (co-located in its own module)
    telling the assistant how to think when acting in that domain. We render `{owner_name}`
    and join the blocks; the result is appended to the system prompt at build time."""
    blocks = [
        spec["guidance"].format(owner_name=owner_name)
        for spec in tools.values()
        if spec.get("guidance")
    ]
    return "\n\n".join(blocks)


def local_handlers(tools: dict, settings) -> dict:
    """{domain: handler instance} for every local tool."""
    return {
        name: spec["handler"](settings)
        for name, spec in tools.items()
        if spec.get("type") == "local"
    }
