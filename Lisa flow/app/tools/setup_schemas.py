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

# help — the one-message how-to: the three ways to add something, and the three directions.
# No inputs: it is the same answer every time, which is the point of it being a verb at all.
HELP = _verb([], {})

# list — what is configured right now. `item` scopes it to one setup item ("transcription", or
# "meetings" when presets are on) and is optional because the bare list — everything — is the
# useful default.
LIST = _verb([], {"item": _STR})

# resolve — find one of the owner's GROUPS by name. A contact is never resolved here: it is
# added from a forwarded card. An empty query is valid and means "show me my recent groups".
RESOLVE = _verb([], {"query": _STR})

# enroll — add a chat. Target it the same three ways `update` and `remove` allow: an `ordinal`
# from the last list or resolve, a `chat_key` surfaced this loop, or the `label` it was shown
# under. Only `direction` is required.
#
# `chat_key` USED to be required, and that was the bug behind two live failures: the enforced
# schema left the model no way to enrol by ordinal without inventing a value for chat_key, so it
# sent "__resolve_ordinal_1__" once and "" the next time. A required field the caller cannot
# legitimately fill is a trap, not a contract.
ENROLL = _verb(
    ["direction"],
    {"ordinal": _INT, "chat_key": _STR, "direction": _DIRECTION, "label": _STR,
     "confirmed": _CONFIRMED},
)

# update — change a rule's direction. Target by ordinal from the last list, or by chat_key.
UPDATE = _verb(
    [],
    {"ordinal": _INT, "chat_key": _STR, "direction": _DIRECTION, "confirmed": _CONFIRMED},
)

# remove — drop a rule. It stops being transcribed and disappears from the list.
REMOVE = _verb([], {"ordinal": _INT, "chat_key": _STR, "confirmed": _CONFIRMED})

# --- meeting presets (gated on PRESETS_ENABLED; see skills/setup.py gated_verbs) -------------
#
# Shown with `list` and `item: "meetings"` — no read verb of their own.

# set_presets — the three settings, any subset in ONE action ("30 minutes, no video" is one
# card, not two). None is required: a contract that demanded all three would make the model
# invent the two he did not mention. The handler refuses an action that carries none.
SET_PRESETS = _verb(
    [],
    {"meeting_minutes": _INT, "video_call": _BOOL, "notify_guests": _BOOL,
     "confirmed": _CONFIRMED},
)

# save_place — a named address. Saving a name that exists replaces its address. Places are
# addressed BY NAME, never by number: the numbers on the list belong to the chats, and a place
# verb that took an ordinal would let "remove 2" mean two different things.
SAVE_PLACE = _verb(["name", "address"],
                   {"name": _STR, "address": _STR, "confirmed": _CONFIRMED})

# remove_place — forget a named address.
REMOVE_PLACE = _verb(["name"], {"name": _STR, "confirmed": _CONFIRMED})

SETUP_TASK_SCHEMAS = {
    "menu": MENU, "help": HELP, "list": LIST, "resolve": RESOLVE,
    "enroll": ENROLL, "update": UPDATE, "remove": REMOVE,
    "set_presets": SET_PRESETS, "save_place": SAVE_PLACE, "remove_place": REMOVE_PLACE,
}
