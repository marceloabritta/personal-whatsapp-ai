"""The manager's standing orders — the one prompt the HUMAN owns.

Worker prompts have always been state. The manager's prompt was not: it was baked into
`manager/agents.py`, which is system code an upgrade replaces wholesale. So anything a
human taught the manager there — how to talk to them, when to interrupt them, what to
refuse — quietly died at the next update. That is the same class of bug as the pre-0.2
`data/` directory, one level up.

So the manager's policy lives in the working folder now, at `<workspace>/MANAGER.md`,
exactly like a worker prompt: yours, editable, never overwritten. The system ships only the
default it is scaffolded from (`MANAGER.default.md` at the system root).

It is appended LAST to the manager's system prompt, and the block says so in its own words:
where the built-in guidance and the human's standing orders disagree, **the human wins**.
That precedence is the point of the file. Without it the built-in text — which tells the
manager to escalate judgement calls — would quietly contradict a human who has just told
him to stop escalating.
"""
from __future__ import annotations

import os

from .version import MANAGER_DEFAULT_PATH

FILENAME = "MANAGER.md"


def path_for(data_dir: str) -> str:
    return os.path.join(data_dir, FILENAME)


def read_default() -> str:
    """The system's default standing orders. A TEMPLATE — never written over the human's."""
    try:
        with open(MANAGER_DEFAULT_PATH, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def read(data_dir: str) -> str:
    """The human's standing orders, or "" if they have none.

    Never raises. A missing, empty or unreadable policy file means "no standing orders" —
    it must never be able to take the board down.
    """
    if not data_dir:
        return ""
    try:
        with open(path_for(data_dir), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def ensure(data_dir: str) -> bool:
    """Scaffold the policy file from the system default. True if it created one.

    Never overwrites: the moment the file is in the working folder it stops being the
    system's and becomes the human's, exactly like a worker prompt.
    """
    if not data_dir or os.path.exists(path_for(data_dir)):
        return False
    default = read_default()
    if not default:
        return False
    os.makedirs(data_dir, exist_ok=True)
    with open(path_for(data_dir), "w", encoding="utf-8") as fh:
        fh.write(default + "\n")
    return True


def block(data_dir: str) -> str:
    """The section appended to the manager's system prompt. "" when there are no orders."""
    text = read(data_dir)
    if not text:
        return ""
    return (
        "\n---\n\n"
        "# YOUR BOSS'S STANDING ORDERS\n\n"
        "These come from the human you report to. They are not general advice, and they are\n"
        "not optional: **where these and anything above disagree, THESE WIN.**\n\n"
        f"{text}\n"
    )
