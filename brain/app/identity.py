"""Trigger-tag matching + own-message detection + the outgoing header.

Ported from secretary/1. Orchestrator/lib/identity.js. Two traps preserved from
the original, both live because the tag list is owner-configurable:

  1. LONGEST FIRST — with "@assist" and "@assistente" both live, first-match-wins
     would slice the wrong tag. Sort by length, longest first.
  2. THE TAG MUST END — "@maryland ..." starts with "@mary" but is a different
     word. A tag only matches when the next char ends the word (space, punct, EOL).
"""
from __future__ import annotations

# The header stamped on every outgoing message. It is the ONLY thing that tells
# the assistant's own replies apart from genuine owner messages: both arrive with
# fromMe=true (it sends from the owner's account). is_own_message MUST recognise
# every header it could ever have emitted, so retired ones live in LEGACY_HEADERS
# forever — never remove them, or old own-messages get re-consumed as owner input.
LEGACY_HEADERS = ["[Mary]:"]

_LEADING_MARKERS = "*_~ \t\r\n"


def header_for(owner_name: str) -> str:
    """The reply header for this owner, e.g. '[Marcelo's AI Assistant]:'."""
    return f"[{owner_name}'s AI Assistant]:"


def all_headers(owner_name: str) -> list[str]:
    """Every header we could have emitted — the current one plus retired ones."""
    return [header_for(owner_name), *LEGACY_HEADERS]


def _ends_tag(ch: str) -> bool:
    """A tag ends at end-of-text or any non-word char. A letter/digit/_ means the
    message opened with a different word that merely begins with the tag."""
    return ch == "" or not (ch.isalnum() or ch == "_")


def matched_tag(text: str, tags: list[str]) -> str | None:
    """The trigger tag this text starts with (for slicing it off), or None."""
    low = (text or "").lower()
    for tag in sorted(tags, key=len, reverse=True):
        if low.startswith(tag) and _ends_tag(low[len(tag) : len(tag) + 1]):
            return tag
    return None


def is_own_message(text: str, owner_name: str) -> bool:
    """Is this one of our OWN messages (echoed back by Evolution as fromMe)?"""
    t = (text or "").lstrip(_LEADING_MARKERS)
    return any(t.startswith(h) for h in all_headers(owner_name))


def frame(body: str, owner_name: str) -> str:
    """Stamp the bold reply header on a message body."""
    return f"*{header_for(owner_name)}*\n\n{body}"
