"""Per-verb input schemas for the calendar tool.

These compose into the enforced-JSON output as the single `actions.items` anyOf — one flat
object per verb (see registry.build_output_schema). Each entry is (required, properties)
WITHOUT the `task` discriminator; the registry injects `task: {const: "calendar.<verb>"}`.

UNION-CAP RULE (hard): Anthropic structured outputs reject a schema with more than 16
union/array-typed params (`anyOf` or `type:array`). So optional fields are simply LEFT OUT
of `required` (plain-typed) — never wrapped as `anyOf:[T, null]`. The handler reads inputs
with `.get()`, so an absent optional is None either way. The only arrays here are the
`attendees` lists. See tests/run_step3.py for the <= 16 guard."""
from __future__ import annotations

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_STRARR = {"type": "array", "items": {"type": "string"}}


def _verb(required: list[str], properties: dict) -> dict:
    """A verb's input contract: mandatory fields in `required`, the rest optional by omission."""
    return {"required": list(required), "properties": dict(properties)}


# create — needs only title + start; everything else optional.
CREATE = _verb(
    ["title", "start"],
    {
        "title": _STR,
        "start": _STR,            # ISO 8601 with offset, resolved by the model
        "end": _STR,              # ISO 8601; omitted -> start + default_meeting_minutes
        "duration_min": _INT,
        "virtual": _BOOL,         # true -> Google Meet link; nulls location (video wins)
        "location": _STR,
        "attendees": _STRARR,     # emails
        "send_invites": _BOOL,    # default true; false -> sendUpdates="none"
    },
)

# list — the plain agenda read; no required fields.
LIST = _verb(
    [],
    {"time_min": _STR, "time_max": _STR},
)

# find — the robust prose->event_id resolver; no required fields (the model fills what it inferred).
FIND = _verb(
    [],
    {
        "query": _STR,            # full-text over summary/description/location/attendees
        "attendee": _STR,         # name or email to anchor/rank on
        "title_contains": _STR,   # ranking hint
        "time_min": _STR,
        "time_max": _STR,
    },
)

# update — needs a resolved event_id; any create-like field may be patched.
UPDATE = _verb(
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

# delete — needs a resolved event_id.
DELETE = _verb(
    ["event_id"],
    {"event_id": _STR},
)

CALENDAR_TASK_SCHEMAS: dict[str, dict] = {
    "create": CREATE,
    "list": LIST,
    "find": FIND,
    "update": UPDATE,
    "delete": DELETE,
}
