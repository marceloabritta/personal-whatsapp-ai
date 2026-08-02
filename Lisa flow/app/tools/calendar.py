"""Google Calendar tool — the owner's own calendar over the Calendar API v3.

P1 (this commit) ships the declarative surface only: the DESCRIBE line, the per-task GUIDANCE
block (appended to the system prompt via the registry), and the handler shell. The CRUD +
robust `find` bodies land in P3; `run()` raises until then, and nothing calls it before the
execute node exists.

Auth (P3): OAuth2 refresh-token client on the owner's account (google.oauth2.credentials) —
no service account. The googleapis client is synchronous, so every call runs in a thread via
asyncio.to_thread. Imports of the google libs are lazy so this module loads without them."""
from __future__ import annotations

from typing import Any

from .base import ActionResult

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

DESCRIBE = (
    "Create, find, reschedule or cancel events on {owner_name}'s Google Calendar."
)

# Per-task guidance, appended to the system prompt (registry.build_task_prompts). Templated
# with {owner_name}. Co-located with the handler so prompt and behaviour never drift.
GUIDANCE = """Calendar actions — you manage {owner_name}'s Google Calendar with create, list, \
find, update and delete.

- Times: treat everything in the configured timezone and write times as full ISO 8601 with \
the offset. Resolve relative dates yourself ("tomorrow 3pm", "next Friday") into an explicit \
ISO datetime — never pass vague words to the tool.
- Creating: you only need a title and a start. Do not interrogate {owner_name} for details he \
did not give; add a reasonable end (or a duration) and go. Clarify only when acting without \
the answer would be wrong. Use `virtual: true` for a video call (a Meet link is attached and \
location is dropped — video wins over a place); otherwise set `location`. Add `attendees` \
emails when he names people; set `send_invites: false` if he does not want them emailed.
- Editing or deleting: you never know an event's id. ALWAYS run `find` first to resolve the \
exact event from what {owner_name} said — search by title words, by the person on it, or by \
its time window. Read the candidates back, confirm the specific event if there is any doubt, \
and only then update or delete using the id you found. Never invent an id.
- Confirm before you change anything: for create, update and delete, restate the plan in one \
short line and act on the go-ahead. `list` and `find` are free — no confirmation needed.
- Report what actually happened, from the tool's result — never say something is done before \
you have seen the result come back."""


class GoogleCalendarService:
    """Local tool handler for the `calendar` domain. Instantiated once in deps with settings."""

    def __init__(self, settings) -> None:
        self.s = settings
        self._svc = None  # lazy Calendar service (built in P3)

    async def run(self, verb: str, inputs: dict[str, Any]) -> ActionResult:
        # CRUD + find implemented in P3. Until then this is never reached (no execute node yet).
        raise NotImplementedError("calendar handler lands in P3")
