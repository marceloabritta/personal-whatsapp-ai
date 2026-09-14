"""Per-verb input schemas for the calendar tool.

These compose into the calendar skill's enforced-JSON output as the `actions.items` anyOf — one
flat object per verb (see skills.output_schema_for). Each entry is (required, properties)
WITHOUT the `task` discriminator; the fan-out injects `task: {const: "calendar.<verb>"}`.

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


# NO `confirmed` FIELD, DELIBERATELY. Approval is `_approved_by`, stamped by resolve_pending on
# the owner's own message and absent from every schema the model sees (skills/confirm.py). A
# model-writable approval flag was only ever advisory — the gate stopped reading it when a
# repeated tool failure had the model set it itself — so it was three optional slots and three
# lines of prompt buying nothing. Those slots are what `all_day` is paid for. Do not reintroduce it.

# create — needs only title + start; everything else optional.
# NB: Anthropic caps the TOTAL optional-parameter count of an enforced schema at 24 (a
# separate limit from the 16 union/array cap; see count_optionals + run_step3) — verified
# against the live API, which rejects 25 with "too many optional parameters". duration_min
# was dropped (use `end`) to keep margin under it; the handler still honours it if present.
CREATE = _verb(
    ["title", "start"],
    {
        "title": _STR,
        "start": _STR,            # ISO 8601 with offset, resolved by the model
        "end": _STR,              # ISO 8601; omitted -> start + default_meeting_minutes
        "virtual": _BOOL,         # true -> Google Meet link; nulls location (video wins)
        "location": _STR,
        "attendees": _STRARR,     # emails — ALWAYS the complete final list, never a delta
        "send_invites": _BOOL,    # default true; false -> don't email guests (sendUpdates=none)
        "all_day": _BOOL,         # true -> a whole-day event; start/end are read as dates
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
        "virtual": _BOOL,
        "location": _STR,
        "attendees": _STRARR,     # ALWAYS the complete final list — patch REPLACES the array
        "send_invites": _BOOL,
        "all_day": _BOOL,
    },
)

# delete — needs a resolved event_id.
DELETE = _verb(
    ["event_id"],
    {
        "event_id": _STR,
        "send_invites": _BOOL,   # default true; false -> cancel without emailing the guests
    },
)

CALENDAR_TASK_SCHEMAS: dict[str, dict] = {
    "create": CREATE,
    "list": LIST,
    "find": FIND,
    "update": UPDATE,
    "delete": DELETE,
}
