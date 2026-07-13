"""Give the manager a policy file the human owns, and heal the worker prompts that still
point into the old system folder.

Two things, both consequences of the same 0.2 change — state moved OUT of the system folder:

1. **The manager had no editable prompt.** Worker prompts were state; the manager's was
   hardcoded in `manager/agents.py`, which an upgrade replaces. Anything the human taught
   him there was lost on the next update. He gets `<workspace>/MANAGER.md` now.

2. **The worker prompts had a dead path in them.** They opened with "first, read
   `<system-folder>/workers/CONVENTIONS.md`" — written when `workers/` lived inside the
   system folder. After 0.2 that file is in the working folder and the old path resolves to
   nothing, so every worker silently ran WITHOUT the house rules. We repoint those
   references at the `{workspace}` token, which the system resolves at delegation time (see
   manager/workers.py).

(2) edits files in `<workspace>/workers/` — normally forbidden. It is allowed here, and
only here, because it is a mechanical repair of a path THIS SYSTEM broke: the reference the
human wrote is preserved exactly, only its location is corrected. Nothing else in the file
is touched, and the folder is backed up before any migration runs.
"""
from __future__ import annotations

import os
import re

from .. import policy

NUMBER = 2
DESCRIPTION = "give the manager an editable policy file; repoint worker prompts at the working folder"

# Any path ending in `workers/CONVENTIONS.md` — whatever folder it was rooted in — that is
# not already the token. Deliberately narrow: it matches a path, not prose.
_STALE_CONVENTIONS = re.compile(r"(?:[^\s`*()\[\]]+/)?workers/CONVENTIONS\.md")
_FIXED = "{workspace}/workers/CONVENTIONS.md"


def migrate(ws) -> list[str]:
    notes: list[str] = []

    if policy.ensure(ws.path):
        notes.append(
            f"created {policy.FILENAME} — the manager's standing orders, yours to edit. "
            f"It is appended to his prompt and overrides the system's defaults."
        )

    healed = _repoint_conventions(ws)
    if healed:
        notes.append(
            f"repointed a dead CONVENTIONS.md path in {healed} worker prompt(s) — they were "
            f"reading a file that moved out of the system folder in 0.2, so the house rules "
            f"were reaching no worker at all"
        )

    return notes


def _repoint_conventions(ws) -> int:
    """Rewrite stale `.../workers/CONVENTIONS.md` references to `{workspace}/workers/…`.

    Idempotent: a file already using the token contains no stale path, so it is not written.
    """
    count = 0
    for path in _worker_files(ws.workers_dir):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue

        fixed = _STALE_CONVENTIONS.sub(_replace, text)
        if fixed == text:
            continue

        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(fixed)
        except OSError:
            continue
        count += 1
    return count


def _replace(m: re.Match) -> str:
    """Leave an already-correct reference exactly as it is — this must be safe to run twice."""
    return m.group(0) if m.group(0).startswith(_FIXED) else _FIXED


def _worker_files(workers_dir: str) -> list[str]:
    """The worker prompts only — `<workers_dir>/<pipeline>/<slug>.md`.

    Nothing at the root of `workers/`, and CONVENTIONS.md is the reason. Only a worker's
    *instructions* pass through `resolve_tokens` on their way into an AgentDefinition;
    CONVENTIONS.md is opened raw by the worker with the Read tool. Writing a `{workspace}`
    token into it would put an unresolvable placeholder in front of the very file this
    migration exists to make readable again.
    """
    out: list[str] = []
    for pipeline in sorted(os.listdir(workers_dir)) if os.path.isdir(workers_dir) else []:
        pdir = os.path.join(workers_dir, pipeline)
        if not os.path.isdir(pdir) or pipeline == "_deleted":
            continue  # archived prompts: not in use, not ours to rewrite
        out.extend(
            os.path.join(pdir, n) for n in sorted(os.listdir(pdir)) if n.endswith(".md")
        )
    return out
