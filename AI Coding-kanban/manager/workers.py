"""Workers: one editable markdown file per kanban column.

    <workspace>/workers/<pipeline>/<column-slug>.md

A column is a CONTRACT, and the worker file is where that contract is written:

    ## Entry criteria   what a card must already have to be worked in this column
    ## Work             what this worker actually does
    ## Exit criteria    what must be true for the card to leave this column
    ## Output           the files this worker writes into the card folder

The worker checks the entry criteria before it starts and the exit criteria
before it finishes, and reports both to the manager. It does not move the card —
only the manager does that. Worker and manager scopes are strictly separate:

    WORKER   does the work. Gets the card folder (which already holds every
             previous column's output — the folder travels with the card) plus
             the codebase. Reports back. Cannot move cards, cannot delegate.
    MANAGER  supervises. Reads the report, decides: accept and advance, send it
             back, fix it himself, or stop at a gate for the human.

Edit these files — in the UI, in your editor, or by telling the manager to change
one in chat — and the next delegation picks the change up. The file is the source
of truth; nothing about a worker is hardcoded.

These files are **state, not system**. They live in your working folder, they are yours,
and an update never overwrites them. The system ships only the *defaults* they are
scaffolded from (`workers.default/`); when a column is first created its default is copied
into your workers dir and a pristine copy is kept in `.defaults/workers/` — that copy is
the baseline the updater three-way-merges against, so upstream can improve a prompt you
never touched without ever clobbering one you did. See manager/prompts.py.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .models import PIPELINE_TITLES, Column
from .version import DEFAULTS_DIR

DEFAULT_TOOLS = ["Read", "Grep", "Glob", "Write", "Edit"]

# Tokens a worker prompt may use to point at a file WITHOUT hardcoding where the working
# folder happens to sit on this machine. Resolved at delegation time.
#
# This exists because of a real bug: worker prompts said "first, read
# `<system-folder>/workers/CONVENTIONS.md`", and when 0.2 moved state OUT of the system
# folder, that path died. Every worker went on opening a file that was no longer there and
# silently worked without the house rules. A prompt must be able to name the working folder
# without knowing its path.
WORKSPACE_TOKEN = "{workspace}"


def resolve_tokens(text: str, root: str) -> str:
    """Substitute the path tokens in a worker's instructions.

    A plain `str.replace`, deliberately — worker prompts are markdown full of braces
    (JSON, code, f-strings), and `str.format` would choke on the first one.
    """
    return text.replace(WORKSPACE_TOKEN, root) if root else text

# The canonical section headings. The UI reads these; the manager checks against them.
ENTRY_H = "Entry criteria"
WORK_H = "Work"
EXIT_H = "Exit criteria"
OUTPUT_H = "Output"


# ---------------------------------------------------------------------------
# Wrapped around every worker's instructions at delegation time. This is the part
# the human does NOT edit — it is what makes a worker a worker.
# ---------------------------------------------------------------------------
WORKER_PREAMBLE = """\
You are a WORKER on one column of a kanban board. You do the work of exactly one
column, then hand back to the manager. You are not the manager.

## Your inputs
- **The card folder.** The manager gives you its path. It travels with the card, so it
  already contains the output of every column the card has passed through — that is
  your input material. READ IT FIRST, before touching the codebase.
- **The codebase.** You have the repository. Verify claims against the real code; never
  assert how the product works without reading it.

## Your boundaries
- You do the work of THIS column only. Not the next one, not the previous one.
- You do NOT move the card, change its column, or decide what happens next. That is the
  manager's job. You report; he decides.
- You do NOT delegate. You have no workers.
- If the entry criteria are not met, STOP. Do not paper over a gap by doing the previous
  column's work yourself — report it as BLOCKED and hand back. A blocked report is a
  successful outcome, not a failure.

## Your contract for this column
"""

REPORT_PROTOCOL = """

---

## How you must report back (mandatory)
Your final message IS your report to the manager. It is not a chat message to a human.
End it with exactly this block, and be honest — the manager acts on it verbatim:

    ENTRY: PASS | BLOCKED
      — if BLOCKED: precisely what was missing, and which column should supply it.
    WORK: what you actually did (2-4 lines).
    OUTPUT: every file you wrote or changed, as a path.
    EXIT: MET | NOT MET
      — if NOT MET: which exit criterion failed, and what it would take to meet it.
    FLAGS: anything the manager must decide on — a wrong plan, a risk, a judgement call
      that is not yours to make. "none" if there are none.

Never claim EXIT: MET to look competent. The manager verifies, and an inflated report
costs more than an honest NOT MET.
"""


@dataclass
class Worker:
    pipeline: str
    slug: str
    title: str
    description: str
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    model: str | None = None
    instructions: str = ""
    path: str = ""

    @property
    def agent_name(self) -> str:
        """The name the manager delegates to via the Agent tool."""
        return f"{self.pipeline}__{self.slug.replace('-', '_')}"

    def section(self, heading: str) -> str:
        """Pull one `## Heading` section out of the instructions (for the UI)."""
        m = re.search(
            rf"(?im)^\#\#\s+{re.escape(heading)}\s*$\n(.*?)(?=^\#\#\s|\Z)",
            self.instructions,
            re.S | re.M,
        )
        return m.group(1).strip() if m else ""

    def contract(self) -> dict[str, str]:
        return {
            "entry": self.section(ENTRY_H),
            "work": self.section(WORK_H),
            "exit": self.section(EXIT_H),
            "output": self.section(OUTPUT_H),
        }


# ---------------------------------------------------------------------------
# frontmatter (deliberately tiny — no yaml dependency)
# ---------------------------------------------------------------------------
def parse_markdown(text: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip()
    return meta, m.group(2).strip()


def render_markdown(w: Worker) -> str:
    return (
        "---\n"
        f"title: {w.title}\n"
        f"pipeline: {w.pipeline}\n"
        f"description: {w.description}\n"
        f"tools: {', '.join(w.tools)}\n"
        f"model: {w.model or 'inherit'}\n"
        "---\n\n"
        f"{w.instructions.strip()}\n"
    )


def build_instructions(entry: str, work: str, exit_: str, output: str) -> str:
    """Assemble the four-section contract body. Used when a column is created from the UI."""
    return (
        f"## {ENTRY_H}\n{entry.strip()}\n\n"
        f"## {WORK_H}\n{work.strip()}\n\n"
        f"## {EXIT_H}\n{exit_.strip()}\n\n"
        f"## {OUTPUT_H}\n{output.strip()}\n"
    )


# ---------------------------------------------------------------------------
# The default worker for a column, if the system ships one.
#
# These live as real markdown files in `workers.default/` — the same format as the ones in
# your working folder, so "the default" and "yours" are directly comparable. That is what
# makes the three-way merge on update possible.
# ---------------------------------------------------------------------------
def default_path(pipeline: str, slug: str, defaults_dir: str = DEFAULTS_DIR) -> str:
    return os.path.join(defaults_dir, pipeline, f"{slug}.md")


def read_default(pipeline: str, slug: str, defaults_dir: str = DEFAULTS_DIR) -> str | None:
    """The system's default worker file for this column, verbatim. None if it ships none."""
    try:
        with open(default_path(pipeline, slug, defaults_dir), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def all_defaults(defaults_dir: str = DEFAULTS_DIR) -> dict[str, str]:
    """Every default the system ships, keyed `<pipeline>/<slug>`."""
    out: dict[str, str] = {}
    for pipeline in sorted(os.listdir(defaults_dir)) if os.path.isdir(defaults_dir) else []:
        pdir = os.path.join(defaults_dir, pipeline)
        if not os.path.isdir(pdir):
            continue
        for name in sorted(os.listdir(pdir)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(pdir, name), "r", encoding="utf-8") as fh:
                out[f"{pipeline}/{name[:-3]}"] = fh.read()
    return out


def retitle(markdown: str, title: str) -> str:
    """Point a default's `title:` frontmatter at the column that is actually using it."""
    return re.sub(r"(?m)^title:.*$", f"title: {title}", markdown, count=1)


def _scaffold(col: Column, entry: str = "", work: str = "", exit_: str = "", output: str = "") -> Worker:
    """The worker file for a column the human invented. If the human filled in the entry/exit
    contract when creating the column, it goes straight in; otherwise this is a scaffold the
    manager can flesh out on request."""
    pipeline_title = PIPELINE_TITLES.get(col.pipeline, col.pipeline)
    upper = col.slug.replace("-", "_").upper()
    todo = f'_Not specified yet. Ask the manager: "write the worker for the {col.title} column."_'
    return Worker(
        pipeline=col.pipeline,
        slug=col.slug,
        title=col.title,
        description=(
            f'Worker for the "{col.title}" column of the {pipeline_title.lower()} pipeline.'
        ),
        tools=list(DEFAULT_TOOLS),
        model=None,
        instructions=build_instructions(
            entry
            or f"""What a card must already have to be worked in **{col.title}**.

{todo}""",
            work
            or f"""What this worker actually does in **{col.title}**.

{todo}""",
            exit_
            or f"""What must be true for a card to LEAVE **{col.title}**.

{todo}""",
            output or f"`{upper}.md` in the card folder. Describe here exactly what it must contain.",
        ),
    )


class WorkerStore:
    """Reads and writes the worker markdown files. The files are the source of truth.

    `dir` is yours (the working folder). `defaults_dir` is the system's templates, read-only.
    `baseline_dir` — when set — keeps a pristine copy of whatever default a worker was
    scaffolded from, so the updater can tell "you never touched this" from "you rewrote it".
    """

    def __init__(
        self,
        workers_dir: str,
        baseline_dir: str | None = None,
        defaults_dir: str = DEFAULTS_DIR,
        root: str | None = None,
    ):
        self.dir = workers_dir
        self.baseline_dir = baseline_dir
        self.defaults_dir = defaults_dir
        # The working folder itself — what `{workspace}` in a prompt resolves to.
        self.root = root or os.path.dirname(os.path.abspath(workers_dir))
        os.makedirs(workers_dir, exist_ok=True)

    def path(self, pipeline: str, slug: str) -> str:
        return os.path.join(self.dir, pipeline, f"{slug}.md")

    def baseline_path(self, pipeline: str, slug: str) -> str | None:
        if not self.baseline_dir:
            return None
        return os.path.join(self.baseline_dir, pipeline, f"{slug}.md")

    def write_baseline(self, pipeline: str, slug: str, markdown: str) -> None:
        """Remember exactly what the system gave you, so an update can three-way-merge."""
        p = self.baseline_path(pipeline, slug)
        if not p:
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(markdown if markdown.endswith("\n") else markdown + "\n")

    # ---- read --------------------------------------------------------
    def load(self, col: Column) -> Worker | None:
        p = self.path(col.pipeline, col.slug)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as fh:
                meta, body = parse_markdown(fh.read())
        except OSError:
            return None
        tools = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
        model = meta.get("model", "").strip()
        return Worker(
            pipeline=col.pipeline,
            slug=col.slug,
            title=meta.get("title") or col.title,
            description=meta.get("description", ""),
            tools=tools or list(DEFAULT_TOOLS),
            model=None if model in ("", "inherit", "default") else model,
            instructions=body,
            path=p,
        )

    def raw(self, col: Column) -> str:
        """The file exactly as it sits on disk (for the UI editor)."""
        w = self.ensure(col)
        try:
            with open(w.path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return render_markdown(w)

    def ensure(
        self,
        col: Column,
        entry: str = "",
        work: str = "",
        exit_: str = "",
        output: str = "",
    ) -> Worker:
        """Load the column's worker, creating it from the system default (or a scaffold, for
        a column you invented) if it doesn't exist yet. The moment a default is materialized
        into your folder it stops being system and becomes yours."""
        w = self.load(col)
        if w:
            return w

        default = None
        if not (entry or work or exit_ or output):
            default = read_default(col.pipeline, col.slug, self.defaults_dir)

        if default:
            markdown = retitle(default, col.title)
            self.write_baseline(col.pipeline, col.slug, markdown)
        else:
            markdown = render_markdown(_scaffold(col, entry, work, exit_, output))

        self.write_raw(col, markdown)
        return self.load(col) or _scaffold(col, entry, work, exit_, output)

    # ---- write -------------------------------------------------------
    def write_raw(self, col: Column, markdown: str) -> str:
        return self.write_raw_path(self.path(col.pipeline, col.slug), markdown)

    def write_raw_path(self, path: str, markdown: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(markdown if markdown.endswith("\n") else markdown + "\n")
        return path

    def rename(self, pipeline: str, old_slug: str, new_slug: str, new_title: str) -> None:
        """Follow a column rename: move the file, update its `title:` frontmatter. The merge
        baseline moves with it, or the updater would lose track of what you started from."""
        if old_slug == new_slug:
            return
        old, new = self.path(pipeline, old_slug), self.path(pipeline, new_slug)
        if not os.path.exists(old):
            return
        os.makedirs(os.path.dirname(new), exist_ok=True)
        try:
            with open(old, "r", encoding="utf-8") as fh:
                text = fh.read()
            text = retitle(text, new_title)
            with open(new, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.remove(old)
        except OSError:
            pass

        old_base, new_base = self.baseline_path(pipeline, old_slug), self.baseline_path(pipeline, new_slug)
        if old_base and new_base and os.path.exists(old_base):
            os.makedirs(os.path.dirname(new_base), exist_ok=True)
            try:
                os.replace(old_base, new_base)
            except OSError:
                pass

    def delete(self, pipeline: str, slug: str) -> None:
        """Archive rather than destroy — a worker file may hold a lot of human thought."""
        p = self.path(pipeline, slug)
        if not os.path.exists(p):
            return
        trash = os.path.join(self.dir, "_deleted")
        os.makedirs(trash, exist_ok=True)
        dest = os.path.join(trash, f"{pipeline}-{slug}.md")
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(trash, f"{pipeline}-{slug}-{n}.md")
            n += 1
        try:
            os.replace(p, dest)
        except OSError:
            pass
        base = self.baseline_path(pipeline, slug)
        if base and os.path.exists(base):
            os.remove(base)  # the column is gone; there is nothing left to merge against

    # ---- running a worker as its OWN conversation ---------------------
    def runtime(self, col: Column) -> dict:
        """Everything needed to run this column's worker as a STANDALONE query.

        This is the difference between the old design and the new one. A worker used to be an
        `AgentDefinition` handed to the manager's own query — which meant the worker ran
        INSIDE the manager's session, and the manager sat there occupied for the whole of it.
        A twenty-minute coder meant a twenty-minute busy manager, and the human asking him a
        question got queued behind a job he was not actually doing.

        Now the worker is its own conversation, with its own tools, and the manager is idle
        while it runs. Same prompt either way — see definitions(), which still exists for the
        contract the UI shows.
        """
        w = self.ensure(col)
        return {
            "name": w.agent_name,
            "title": w.title,
            "prompt": f"{WORKER_PREAMBLE}\n{resolve_tokens(w.instructions.strip(), self.root)}\n{REPORT_PROTOCOL}",
            "tools": list(w.tools),
            "model": w.model,
        }

    def by_agent_name(self, columns: list[Column], name: str) -> Column | None:
        """Resolve what the manager asked to delegate to — by agent name, slug or title."""
        want = (name or "").strip().lower()
        if not want:
            return None
        for col in columns:
            w = self.ensure(col)
            if want in (w.agent_name.lower(), col.slug.lower(), col.title.lower()):
                return col
        return None

    # ---- agent definitions -------------------------------------------
    def definitions(self, columns: list[Column]) -> dict:
        """Build the AgentDefinition map the manager delegates to, from the files on disk.

        Rebuilt on every manager run, so an edit to a worker file — by you in the UI, in
        your editor, or by the manager in chat — takes effect on the very next delegation.
        The worker's own contract is sandwiched between the preamble (what a worker is)
        and the report protocol (how it must hand back), and its `{workspace}` tokens are
        resolved to the real working folder on the way through.
        """
        from claude_agent_sdk import AgentDefinition  # lazy: keeps mock mode SDK-free

        out: dict = {}
        for col in columns:
            w = self.ensure(col)
            instructions = resolve_tokens(w.instructions.strip(), self.root)
            kwargs = dict(
                description=w.description or f"Worker for the {w.title} column.",
                tools=w.tools,
                prompt=f"{WORKER_PREAMBLE}\n{instructions}\n{REPORT_PROTOCOL}",
            )
            if w.model:
                kwargs["model"] = w.model
            out[w.agent_name] = AgentDefinition(**kwargs)
        return out
