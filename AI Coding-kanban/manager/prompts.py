"""Three-way merge for worker prompts, so "my prompts survive" and "my system improves"
stop being in conflict.

Your prompts are state: an update must never overwrite them. But most of this system's
quality lives in those prompts, so if upstream improves one, a folder that simply refuses
to be touched would never get better either. Git solved this long ago, and so do we:

    BASELINE   the pristine default your worker was scaffolded from  (<ws>/.defaults/workers/)
    YOURS      the file in your working folder                       (<ws>/workers/)
    NEW        the default this version of the system ships          (workers.default/)

    yours == baseline, new differs  ->  you never touched it. Take the new default silently.
    yours != baseline, new differs  ->  KEEP YOURS. Report that upstream changed it, with
                                        the diff, so you can merge it deliberately.
    new == baseline                 ->  upstream changed nothing. Nothing to do.
    no baseline yet                 ->  fall back to comparing yours against the new default:
                                        identical means untouched; different means yours wins
                                        and we report. We never guess in the clobbering
                                        direction.

Nothing here writes over a file you have edited. The worst it can do is tell you something.
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field

from .workers import all_defaults, parse_markdown, retitle

UPDATED = "updated"  # yours was untouched; took the new default
KEPT = "kept"  # you customized it and upstream changed it: yours stands, here's the diff
UNCHANGED = "unchanged"  # nothing to do


@dataclass
class PromptChange:
    key: str  # "<pipeline>/<slug>"
    action: str
    diff: str = ""


@dataclass
class PromptSync:
    changes: list[PromptChange] = field(default_factory=list)

    @property
    def updated(self) -> list[PromptChange]:
        return [c for c in self.changes if c.action == UPDATED]

    @property
    def kept(self) -> list[PromptChange]:
        return [c for c in self.changes if c.action == KEPT]

    def describe(self) -> list[str]:
        out = []
        for c in self.updated:
            out.append(f"  ✓ {c.key}: you never edited this one — took the improved default.")
        for c in self.kept:
            out.append(
                f"  ! {c.key}: upstream improved this prompt, but you have edited it — "
                f"YOURS IS UNTOUCHED. Review the diff and merge what you want."
            )
        return out


def _norm(text: str | None) -> str:
    """Compare on content, not on trailing whitespace."""
    if text is None:
        return ""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _read(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def sync(store, defaults: dict[str, str] | None = None, apply: bool = True) -> PromptSync:
    """Merge the system's default prompts into a working folder. `store` is a WorkerStore.

    Only files that already exist in your workers dir are considered — a default for a
    column you don't have is not your business, and will be scaffolded if you ever add it.
    """
    if defaults is None:
        defaults = all_defaults(store.defaults_dir)

    result = PromptSync()
    for key, new_raw in sorted(defaults.items()):
        pipeline, slug = key.split("/", 1)
        yours_path = store.path(pipeline, slug)
        yours = _read(yours_path)
        if yours is None:
            continue  # you don't have this column

        # Keep the title you're actually using out of the comparison — a renamed column
        # must not read as an upstream change.
        meta, _ = parse_markdown(yours)
        new = retitle(new_raw, meta.get("title", "")) if meta.get("title") else new_raw

        baseline = _read(store.baseline_path(pipeline, slug))
        n_yours, n_new, n_base = _norm(yours), _norm(new), _norm(baseline)

        if n_yours == n_new:
            # Identical to the new default either way: record it as the baseline and move on.
            if apply and n_base != n_new:
                store.write_baseline(pipeline, slug, new)
            result.changes.append(PromptChange(key, UNCHANGED))
            continue

        untouched = baseline is not None and n_yours == n_base
        if untouched:
            if apply:
                store.write_raw_path(yours_path, new)
                store.write_baseline(pipeline, slug, new)
            result.changes.append(PromptChange(key, UPDATED, _diff(yours, new, key)))
        else:
            # You edited it (or we have no baseline and cannot prove you didn't). Yours wins.
            against = baseline if baseline is not None else yours
            result.changes.append(PromptChange(key, KEPT, _diff(against, new, key)))
    return result


def _diff(before: str, after: str, key: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{key} (yours/baseline)",
            tofile=f"{key} (new default)",
        )
    )


def write_report(path: str, sync_result: PromptSync, version: str) -> str | None:
    """Dump the kept-but-changed diffs somewhere you can actually read them."""
    kept = sync_result.kept
    if not kept:
        return None
    body = [
        f"# Upstream prompt changes you have NOT taken (system {version})",
        "",
        "These default worker prompts improved upstream, but you have edited your copies,",
        "so nothing was overwritten. Below is what changed upstream. Merge by hand what you",
        "want; ignore the rest.",
        "",
    ]
    for c in kept:
        body += [f"## {c.key}", "", "```diff", c.diff.rstrip(), "```", ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(body))
    return path
