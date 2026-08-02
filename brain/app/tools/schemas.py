"""Per-task input sub-schemas for the calendar domain.

These compose into the enforced-JSON `actions` array (registry.build_output_schema):
each becomes a branch {task:"calendar.<verb>", inputs:<schema>}.

Anthropic structured outputs cap a schema at 16 union/array-typed parameters (`anyOf`
or `type:array`) — exceeding it is a 400. So optional fields are simply LEFT OUT of
`required` (plain-typed), NOT wrapped in `anyOf:[T, null]`; every object still sets
`additionalProperties:false`. The handler reads inputs with `.get()`, so an omitted
optional is None either way."""
from __future__ import annotations

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}
_STRARR = {"type": "array", "items": {"type": "string"}}


def _obj(required: list[str], properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),  # mandatory only; the rest are optional
        "properties": properties,
    }


CREATE_INPUTS = _obj(
    ["title", "start"],
    {
        "title": _STR,
        "start": _STR,  # ISO 8601 with -03:00
        "end": _STR,
        "duration_min": _INT,
        "virtual": _BOOL,
        "location": _STR,
        "attendees": _STRARR,  # emails
        "send_invites": _BOOL,
    },
)

LIST_INPUTS = _obj(
    [],
    {
        "query": _STR,
        "time_min": _STR,
        "time_max": _STR,
    },
)

UPDATE_INPUTS = _obj(
    ["event_id"],
    {
        "event_id": _STR,
        "title": _STR,
        "start": _STR,
        "end": _STR,
        "duration_min": _INT,
        "virtual": _BOOL,
        "location": _STR,
        "attendees": _STRARR,
        "send_invites": _BOOL,
    },
)

DELETE_INPUTS = _obj(["event_id"], {"event_id": _STR})

CALENDAR_TASK_SCHEMAS = {
    "create": CREATE_INPUTS,
    "list": LIST_INPUTS,
    "update": UPDATE_INPUTS,
    "delete": DELETE_INPUTS,
}
