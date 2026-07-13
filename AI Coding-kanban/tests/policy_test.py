"""The manager's standing orders, and the dead path they were written to fix.

Two guarantees, and both are about the same line: the system is disposable, the working
folder is not.

  1. `<workspace>/MANAGER.md` reaches the manager's prompt, OVERRIDES the system's built-in
     guidance, and an update never overwrites it. If it did, every instruction the human
     ever gave the manager would evaporate at the next upgrade — which is precisely the bug
     this file exists to prevent regressing.

  2. A worker prompt can name the working folder with `{workspace}` and have it resolved at
     delegation time. Before this, the prompts hardcoded a path inside the system folder;
     when state moved out in 0.2 the path died, and every worker went on silently running
     without the house rules it told them to read.

    python tests/policy_test.py        (no API key, no network)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager import migrations, policy  # noqa: E402
from manager.agents import board_prompt_for, manager_prompt_for  # noqa: E402
from manager.migrations import m0002_manager_policy as m0002  # noqa: E402
from manager.models import BUILD, MAINT, PLAN  # noqa: E402
from manager.workers import WorkerStore, resolve_tokens  # noqa: E402
from manager.workspace import Workspace  # noqa: E402

FAILED: list = []


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def new_ws() -> Workspace:
    ws = Workspace(tempfile.mkdtemp(prefix="km-ws-"), tempfile.mkdtemp(prefix="fake-repo-"))
    ws.ensure()
    return ws


class FakeCol:
    def __init__(self, pipeline="plan", slug="scoping", title="Scoping"):
        self.pipeline, self.slug, self.title, self.gate = pipeline, slug, title, False


class FakeCard:
    id, title, description = "abc123", "A card", "does a thing"
    pipeline, column = "plan", "scoping"


class FakePipelines:
    """Just enough of PipelineConfig for the prompt builders: a `columns` dict and `get`."""

    def __init__(self):
        self.columns = {
            PLAN: [FakeCol(PLAN, "scoping", "Scoping")],
            MAINT: [FakeCol(MAINT, "replication", "Replication")],
            BUILD: [FakeCol(BUILD, "coding", "Coding")],
        }

    def get(self, _column):
        return self.columns[PLAN][0]


class FakeManager:
    name, emoji = "Ada", "🧭"


def main() -> int:
    pipelines = FakePipelines()

    # -----------------------------------------------------------------
    section("a fresh working folder is born with standing orders")
    ws = new_ws()
    check("MANAGER.md exists after migration", os.path.isfile(policy.path_for(ws.path)))
    check("nothing left to migrate", migrations.pending(ws) == [])
    check("it is the system default, verbatim", read(policy.path_for(ws.path)).strip() == policy.read_default())

    # -----------------------------------------------------------------
    section("the orders reach the manager's prompt, and outrank the system's")
    workers = WorkerStore(ws.workers_dir, root=ws.path)
    card_prompt = manager_prompt_for(FakeCard(), pipelines, workers, "/tmp/card", ws.path)
    board_prompt = board_prompt_for(FakeManager(), pipelines, workers, ws.path)

    for name, prompt in (("card", card_prompt), ("board", board_prompt)):
        check(f"the {name} prompt carries the orders", "STANDING ORDERS" in prompt)
        check(f"the {name} prompt says they win", "THESE WIN" in prompt)

    check("the manager is told to decide, not ask", "Decide. Do not ask." in card_prompt)
    check("...to document the call", "Document the call" in card_prompt)
    check("...to talk product, not code", "Talk product, not code" in card_prompt)
    check("...to keep the chat small", "small and clean" in card_prompt)
    check("...to defend the happy path", "Defend the happy path" in card_prompt)
    check(
        "the orders come LAST — after the built-in guidance they override",
        card_prompt.index("STANDING ORDERS") > card_prompt.index("supervision cycle"),
    )

    # -----------------------------------------------------------------
    section("they are the human's: an update never overwrites them")
    write(policy.path_for(ws.path), "# Mine\nAlways ask me first.\n")
    check("policy.ensure() will not clobber an existing file", policy.ensure(ws.path) is False)
    check("the human's text survives", "Always ask me first." in read(policy.path_for(ws.path)))
    check(
        "and it is what the manager is told",
        "Always ask me first." in manager_prompt_for(FakeCard(), pipelines, workers, "/tmp/c", ws.path),
    )

    # -----------------------------------------------------------------
    section("no policy file at all is survivable, not fatal")
    os.remove(policy.path_for(ws.path))
    check("read() returns empty", policy.read(ws.path) == "")
    check("block() returns empty", policy.block(ws.path) == "")
    bare = manager_prompt_for(FakeCard(), pipelines, workers, "/tmp/c", ws.path)
    check("the prompt still builds", "SUPERVISOR" in bare and "STANDING ORDERS" not in bare)
    check("and a manager with no workspace at all still builds", "SUPERVISOR" in manager_prompt_for(FakeCard(), pipelines, workers, "/tmp/c"))

    # -----------------------------------------------------------------
    section("{workspace} in a worker prompt resolves to the working folder")
    check(
        "the token is substituted",
        resolve_tokens("read {workspace}/workers/CONVENTIONS.md", "/ws") == "read /ws/workers/CONVENTIONS.md",
    )
    check(
        "a prompt full of braces is not mangled (str.replace, not str.format)",
        resolve_tokens('{"json": true} and {x} stay put — {workspace} does not', "/ws")
        == '{"json": true} and {x} stay put — /ws does not',
    )
    check("no root: the token is left alone rather than blanked", resolve_tokens("{workspace}/x", "") == "{workspace}/x")

    # -----------------------------------------------------------------
    section("the migration heals a worker prompt pointing into the old system folder")
    ws2 = new_ws()
    stale = os.path.join(ws2.workers_dir, "plan", "scoping.md")
    write(
        stale,
        "---\ntitle: Scoping\n---\n\n## Work\n"
        "- **First, read `AI Coding-kanban/workers/CONVENTIONS.md`.** It is the house rules.\n"
        "- Also see `Some Other Folder/workers/CONVENTIONS.md` §5.\n",
    )
    # CONVENTIONS.md itself is read RAW by the worker — a token in it would never resolve.
    conventions = os.path.join(ws2.workers_dir, "CONVENTIONS.md")
    write(conventions, "# House rules\nSee AI Coding-kanban/workers/CONVENTIONS.md for more.\n")

    healed = m0002._repoint_conventions(ws2)
    body = read(stale)
    check("it rewrote the prompt", healed == 1)
    check("the dead path is gone", "AI Coding-kanban/workers/CONVENTIONS.md" not in body)
    check("it points at the working folder now", "{workspace}/workers/CONVENTIONS.md" in body)
    check("every stale reference in the file, not just the first", body.count("{workspace}/workers/CONVENTIONS.md") == 2)
    check("the human's own words are untouched", "It is the house rules." in body)
    check(
        "CONVENTIONS.md itself is NOT rewritten (nothing resolves tokens in it)",
        "{workspace}" not in read(conventions),
    )

    check("running it twice changes nothing", m0002._repoint_conventions(ws2) == 0)

    # and end to end: the worker the manager actually delegates to sees a real path
    store = WorkerStore(ws2.workers_dir, root=ws2.path)
    resolved = resolve_tokens(read(stale), store.root)
    check(
        "the delegated worker gets an absolute, existing path",
        os.path.join(ws2.path, "workers", "CONVENTIONS.md") in resolved,
    )

    print(f"\n{'=' * 70}")
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("policy: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
