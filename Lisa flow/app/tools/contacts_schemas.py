"""Per-verb input schemas for the contacts tool.

These compose into the contacts skill's enforced-JSON output as the `actions.items` anyOf (see
skills.output_schema_for). Each entry is (required, properties) WITHOUT the `task` discriminator;
the fan-out injects `task: {const: "contacts.<verb>"}`.

Every field is required, so this schema adds nothing to the 24-optional cap, and its only arrays are
the two `changes` lists, well inside the 16-union cap. An unused `old` or `new` is sent as "", which
the handler reads the same as absent."""
from __future__ import annotations

from .people import FIELDS

_STR = {"type": "string"}

_CHANGE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["field", "op", "old", "new"],
    "properties": {
        "field": {"type": "string", "enum": list(FIELDS)},
        "op": {"type": "string", "enum": ["add", "replace", "remove"]},
        "old": _STR,
        "new": _STR,
    },
}
_CHANGES = {"type": "array", "items": _CHANGE}


def _verb(required: list[str], properties: dict) -> dict:
    return {"required": list(required), "properties": dict(properties)}


# find — the lookup. A name, nickname, company, email or phone number.
FIND = _verb(["query"], {"query": _STR})

# create — a new card. `changes` carries everything but the name, each with op "add".
CREATE = _verb(["name", "changes"], {"name": _STR, "changes": _CHANGES})

# update — change a card that was surfaced this loop.
UPDATE = _verb(["contact_id", "changes"], {"contact_id": _STR, "changes": _CHANGES})

# delete — remove a card that was surfaced this loop.
DELETE = _verb(["contact_id"], {"contact_id": _STR})

CONTACTS_TASK_SCHEMAS: dict[str, dict] = {
    "find": FIND,
    "create": CREATE,
    "update": UPDATE,
    "delete": DELETE,
}
