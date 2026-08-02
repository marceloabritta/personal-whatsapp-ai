"""Trigger-tag matching + own-message detection + the outgoing header.

Ported from secretary/1. Orchestrator/lib/identity.js. Two traps preserved from
the original, both live because the tag list is owner-configurable:

  1. LONGEST FIRST — with "@assist" and "@assistente" both live, first-match-wins
     would slice the wrong tag. Sort by length, longest first.
  2. THE TAG MUST END — "@maryland ..." starts with "@mary" but is a different
     word. A tag only matches when the next char ends the word (space, punct, EOL).
"""
from __future__ import annotations

# The header Mary stamps on every outgoing message. It is the ONLY thing that
# tells her own replies apart from genuine owner messages: both arrive with
# fromMe=true (she sends from the owner's account). is_own_message MUST recognise
# every header she could ever emit — add retired variants here, never remove them.
OUTGOING_HEADER = "[Mary]:"
ALL_HEADERS = [OUTGOING_HEADER]

_LEADING_MARKERS = "*_~ \t\r\n"


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


def is_own_message(text: str) -> bool:
    """Is this one of Mary's OWN messages (echoed back by Evolution as fromMe)?"""
    t = (text or "").lstrip(_LEADING_MARKERS)
    return any(t.startswith(h) for h in ALL_HEADERS)


def frame(body: str, header: str = OUTGOING_HEADER) -> str:
    """Stamp the bold reply header on a message body."""
    return f"*{header}*\n\n{body}"
