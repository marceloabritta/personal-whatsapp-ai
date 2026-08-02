"""Per-task input sub-schemas for the calendar domain.

These compose into the enforced-JSON `actions` array (registry.build_output_schema):
each becomes a branch {task:"calendar.<verb>", inputs:<schema>}. Kept strict for
Anthropic structured outputs: every property is in `required`, optional fields are
made nullable, and additionalProperties is false everywhere."""
from __future__ import annotations

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}
_STRARR = {"type": "array", "items": {"type": "string"}}


def _nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


def _obj(required: list[str], properties: dict) -> dict:
    # strict: every property required (optional ones are nullable), no extras
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties.keys()),
        "properties": properties,
    }


CREATE_INPUTS = _obj(
    ["title", "start"],
    {
        "title": _STR,
        "start": _STR,  # ISO 8601 with -03:00
        "end": _nullable(_STR),
        "duration_min": _nullable(_INT),
        "virtual": _nullable(_BOOL),
        "location": _nullable(_STR),
        "attendees": _nullable(_STRARR),  # emails
        "send_invites": _nullable(_BOOL),
    },
)

LIST_INPUTS = _obj(
    [],
    {
        "query": _nullable(_STR),
        "time_min": _nullable(_STR),
        "time_max": _nullable(_STR),
    },
)

UPDATE_INPUTS = _obj(
    ["event_id"],
    {
        "event_id": _STR,
        "title": _nullable(_STR),
        "start": _nullable(_STR),
        "end": _nullable(_STR),
        "duration_min": _nullable(_INT),
        "virtual": _nullable(_BOOL),
        "location": _nullable(_STR),
        "attendees": _nullable(_STRARR),
        "send_invites": _nullable(_BOOL),
    },
)

DELETE_INPUTS = _obj(["event_id"], {"event_id": _STR})

CALENDAR_TASK_SCHEMAS = {
    "create": CREATE_INPUTS,
    "list": LIST_INPUTS,
    "update": UPDATE_INPUTS,
    "delete": DELETE_INPUTS,
}
