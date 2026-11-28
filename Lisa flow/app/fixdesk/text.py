"""What the fix desk says, and how it reads his answer. Pure functions, no I/O.

Every message the desk sends ends with the case's tag, "(F12)". The tag is how he names a case
("fix it F12"), and how the desk's own messages are told apart from the rest of his chat with
himself (`is_fixdesk_line`), so that a later "@lisa ..." there does not read a fix plan as part of
the conversation."""
from __future__ import annotations

import re
from datetime import datetime
from typing import NamedTuple, Optional

from ..identity import is_own_message

PARA_MAX = 320      # one paragraph of the plan, in characters
SUMMARY_MAX = 240   # the one-sentence summary in a result message

_CASE_TAG = re.compile(r"\(F(\d+)\)\s*$")
_FIX = re.compile(r"^\s*fix\s+it\b[\s,.;:!\-–—]*(.*)$", re.IGNORECASE | re.DOTALL)
_SKIP = re.compile(r"^\s*skip\b[\s,.;:!]*(.*)$", re.IGNORECASE | re.DOTALL)
_UNDO = re.compile(r"^\s*undo\b[\s,.;:!]*(.*)$", re.IGNORECASE | re.DOTALL)
_ID = re.compile(r"^\(?F(\d+)\)?(?![\w])[\s,.;:]*", re.IGNORECASE)


class Command(NamedTuple):
    kind: str                 # "fix" | "skip" | "undo"
    case_id: Optional[int]    # the case he named, if he named one
    note: str                 # whatever followed "fix it": passed to the coding agent


def tag(case_id: int) -> str:
    return f"(F{case_id})"


def parse_command(text: str | None) -> Optional[Command]:
    """His answer to a plan, or None when the message is not one.

    "fix it", "Fix it F12", "fix it, but keep the 5 minute limit" → fix.
    "skip", "skip F12" → skip. "skip the gym tomorrow" is a note to himself, not an answer: a
    skip carries a case tag or nothing. "undo", "undo F12" → take a shipped fix back, read the
    same way."""
    t = (text or "").strip()
    m = _FIX.match(t)
    if m:
        rest = m.group(1).strip()
        case_id = None
        mid = _ID.match(rest)
        if mid:
            case_id = int(mid.group(1))
            rest = rest[mid.end():].strip()
        return Command("fix", case_id, rest[:500])
    for kind, pattern in (("skip", _SKIP), ("undo", _UNDO)):
        m = pattern.match(t)
        if m:
            rest = m.group(1).strip()
            if not rest:
                return Command(kind, None, "")
            mid = _ID.match(rest)
            if mid and not rest[mid.end():].strip():
                return Command(kind, int(mid.group(1)), "")
    return None


def is_fixdesk_line(text: str | None, *, from_me: bool, owner_name: str) -> bool:
    """Is this line of his self-chat part of the fix desk's exchange?

    Her side: one of her own messages that ends with a case tag. His side: an answer to a plan."""
    t = text or ""
    if is_own_message(t, owner_name):
        return bool(_CASE_TAG.search(t))
    return bool(from_me and parse_command(t))


def clean(text: str | None, limit: int = PARA_MAX) -> str:
    """One paragraph: a single line, no markdown the phone would show as symbols, capped."""
    t = " ".join((text or "").split())
    t = t.replace("`", "").replace("**", "")
    if len(t) <= limit:
        return t
    cut = t[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    if stop >= limit * 0.6:
        return cut[: stop + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:") + "…"


def proposal_body(case_id: int, plan: dict) -> str:
    """The three paragraphs. `plan` carries issue / diagnosis / fix as written by the investigator."""
    return (
        f"*Issue.* {clean(plan.get('issue'))}\n\n"
        f"*Diagnosis.* {clean(plan.get('diagnosis'))}\n\n"
        f"*Fix.* {clean(plan.get('fix'))} Reply *fix it* to go ahead. {tag(case_id)}"
    )


def ack_body(case_id: int, *, run_at: str, worker_note: str = "") -> str:
    note = f" {worker_note}" if worker_note else ""
    return (f"On it. The next daily run starts at {run_at}; I will tell you how it went."
            f"{note} {tag(case_id)}")


def already_body(case_id: int, status: str) -> str:
    if status in ("ready", "shipped"):
        return f"That one is already done. {tag(case_id)}"
    return f"Already queued for the next daily run. {tag(case_id)}"


def skipped_body(case_id: int) -> str:
    return f"Skipped. {tag(case_id)}"


def undo_ack_body(case_id: int) -> str:
    return ("I will take it back on your Mac's next run. To do it sooner, press Run now on the "
            f"task in the desktop app. {tag(case_id)}")


def undo_already_body(case_id: int, status: str) -> str:
    if status == "undone":
        return f"That one has already been taken back. {tag(case_id)}"
    return f"Already queued to be taken back. {tag(case_id)}"


def result_body(case_id: int, result: dict) -> str:
    """What happened to an approved case. `result` is what the worker on the Mac reported. Once
    he has asked for a fix to be taken back, `result["undo"]` says how that went, and that is
    the news."""
    undo = result.get("undo")
    if isinstance(undo, dict):
        if undo.get("ok"):
            return f"Taken back: that change is out of production. {tag(case_id)}"
        detail = clean(undo.get("detail"), SUMMARY_MAX)
        return f"Could not take it back: {detail} It is still live. {tag(case_id)}".replace("  ", " ")
    status = result.get("status")
    summary = clean(result.get("summary"), SUMMARY_MAX)
    checks = clean(result.get("checks"), SUMMARY_MAX)
    tail = " ".join(p for p in (summary, checks) if p)
    if status == "shipped":
        return f"Live. {tail} {tag(case_id)}".replace("  ", " ")
    if status == "ready":
        branch = result.get("branch") or "?"
        commit = (result.get("commit") or "")[:7]
        return (f"Fixed and tested, not deployed yet: branch {branch}, commit {commit}. {tail} "
                f"{tag(case_id)}").replace("  ", " ")
    step = clean(result.get("failed_step"), 80) or "the run"
    detail = clean(result.get("detail"), SUMMARY_MAX)
    return (f"Not fixed: {step} failed. {detail} Nothing was deployed. "
            f"{tag(case_id)}").replace("  ", " ")


def in_quiet_hours(now: datetime, quiet_from: int, quiet_until: int) -> bool:
    """Is `now` (local time) inside the hours when nothing is sent? 22 and 8 mean 22:00–07:59."""
    if quiet_from == quiet_until:
        return False
    h = now.hour
    if quiet_from > quiet_until:
        return h >= quiet_from or h < quiet_until
    return quiet_from <= h < quiet_until
