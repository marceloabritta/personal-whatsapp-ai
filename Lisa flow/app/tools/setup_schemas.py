"""Per-verb input schemas for the setup tool.

These compose into the setup skill's enforced-JSON output as the `actions.items` anyOf (see
skills.output_schema_for). Each entry is (required, properties) WITHOUT the `task`
discriminator; the fan-out injects `task: {const: "setup.<verb>"}`.

UNION-CAP RULE (hard): Anthropic structured outputs reject a schema with more than 16
union/array-typed params and more than 24 optionals. Nothing here is an array and optionals are
left out of `required` rather than wrapped as anyOf:[T, null] — the same discipline as
tools/schemas.py. Setup contributes 0 arrays and ~11 optionals, well inside both.

NOTE on targets: `update` and `remove` take EITHER an `ordinal` from the last list ("edit 5") or
a `chat_key` from a forwarded card / group pick. Neither is required in the schema, because the
model should not have to choose one to satisfy a contract; the resolve gate refuses the action
when neither resolves to a chat surfaced this loop."""
from __future__ import annotations

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

# The three directions, exactly as they are stored and shown. "both" renders as "in & out".
_DIRECTION = {"type": "string", "enum": ["inbound", "outbound", "both"]}

# A mutating verb carries `confirmed`, which the model sets only after a go-ahead. It is NOT the
# gate — approval is stamped as `_approved_by` by resolve_pending, on the owner's own message,
# and appears in no schema the model sees. This field exists so the contract matches calendar's.
_CONFIRMED = {"type": "boolean"}


def _verb(required: list[str], properties: dict) -> dict:
    return {"required": list(required), "properties": dict(properties)}


# menu — what can be configured. No inputs.
MENU = _verb([], {})

# list — what is enrolled right now. `item` scopes it to one setup item (only "transcription"
# exists today) and is optional because the bare list is the useful default.
LIST = _verb([], {"item": _STR})

# resolve — find one of the owner's GROUPS by name. A contact is never resolved here: it is
# added from a forwarded card. An empty query is valid and means "show me my recent groups".
RESOLVE = _verb([], {"query": _STR})

# enroll — add a chat. `chat_key` must have been surfaced this loop (card, resolve, or list).
ENROLL = _verb(
    ["chat_key", "direction"],
    {"chat_key": _STR, "direction": _DIRECTION, "label": _STR, "confirmed": _CONFIRMED},
)

# update — change a rule's direction. Target by ordinal from the last list, or by chat_key.
UPDATE = _verb(
    [],
    {"ordinal": _INT, "chat_key": _STR, "direction": _DIRECTION, "confirmed": _CONFIRMED},
)

# remove — drop a rule. It stops being transcribed and disappears from the list.
REMOVE = _verb([], {"ordinal": _INT, "chat_key": _STR, "confirmed": _CONFIRMED})

SETUP_TASK_SCHEMAS = {
    "menu": MENU, "list": LIST, "resolve": RESOLVE,
    "enroll": ENROLL, "update": UPDATE, "remove": REMOVE,
}
