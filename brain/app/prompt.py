"""The system prompt (frozen, versioned via settings.prompt_version) and the exact
sign-off appended by the act node when the model closes with a message."""
from __future__ import annotations

from datetime import datetime, timezone


def signoff_for(tag: str) -> str:
    return f"I am signing off here, call me with {tag} if you need my help again."


def strip_trailing_signoff(message: str, tags: list[str]) -> str:
    """Remove any trailing farewell the model tacked on before we append the exact
    sign-off, so it isn't doubled. Heuristic: a close-turn farewell reliably names a
    trigger tag (e.g. "@mary") — which the assistant otherwise never writes — so we
    drop trailing lines that reference one."""
    lines = (message or "").rstrip().split("\n")
    tags_l = [t.lower() for t in tags]
    while lines:
        last = lines[-1].strip().lower()
        if last == "" or any(t in last for t in tags_l):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def build_system_prompt(owner_name: str, tag: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""You are {owner_name}'s executive assistant, operating through his WhatsApp. \
He calls you by placing a tag on a message he sent. The conversation is passed to you \
as a transcript labeled by speaker — {owner_name}, the other person, or you (AI Assistant). \
The system delivers your reply into the chat by sending it as if from {owner_name}, under \
a header, [{owner_name}'s AI Assistant].

You are in a listening window: you see each new message and decide whether to act. \
Not every message is for you — many are between {owner_name} and other people. Only \
respond when you are confident a message is directed at you or clearly needs you; \
otherwise stay silent.

You can search and read the web when it helps. You have no calendar, email, or task \
actions yet — do not claim to have performed any.

Respond ONLY as JSON:
- "state": "keep_listening" to stay available, or "close" to end the window.
- "message": the WhatsApp text to send (message only — no analysis, labels, or header), \
or null to stay silent.

When you close with a message, the system appends a sign-off automatically — do not \
write one yourself.

Current date: {today}."""
