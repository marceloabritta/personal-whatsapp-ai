"""The update path: does a working folder survive being brought forward?

This is decision 4, and decision 3 is a constraint on it: after every migration, the cards,
the threads, the card folders, the columns and the human's own prompts must all still be
there. So that is exactly what this asserts.

    python tests/update_test.py        (no API key, no network)
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager import migrations, prompts, update, workspace  # noqa: E402
from manager.board import SCHEMA_VERSION, Board  # noqa: E402
from manager.models import PLAN  # noqa: E402
from manager.workers import WorkerStore, all_defaults  # noqa: E402
from manager.workspace import Workspace, WorkspaceError  # noqa: E402

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


def new_ws(repo=None) -> Workspace:
    repo = repo or tempfile.mkdtemp(prefix="fake-repo-")
    ws = Workspace(tempfile.mkdtemp(prefix="km-ws-"), repo)
    return ws


async def main() -> int:
    # -----------------------------------------------------------------
    section("a fresh working folder is born current")
    ws = new_ws()
    notes = ws.ensure()
    check("it stamps a schema version", ws.schema_version() == migrations.LATEST)
    check("it has nothing to migrate", migrations.pending(ws) == [])
    check("the server would let it start", update.preflight(ws) == [])
    check("it has a workers dir of its own", os.path.isdir(ws.workers_dir))
    check("it has an .env of its own", os.path.isfile(ws.env_path))
    check("it says what it did", any("created working folder" in n for n in notes))

    # -----------------------------------------------------------------
    section("the card folders live in it, outside the system folder")
    board = update.board_for(ws)
    card = await board.add_card("A card that must survive", "and its folder too")
    card_dir = board.abs_dir(card)
    with open(os.path.join(card_dir, "IDEA.md"), "w", encoding="utf-8") as fh:
        fh.write("# a real artifact\n")
    await board.append_message(card.id, "user", "remember me")

    system_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    check("the card folder is inside the working folder", card_dir.startswith(ws.path))
    check(
        "the card folder is NOT inside the system folder",
        not os.path.abspath(card_dir).startswith(system_dir + os.sep),
    )
    check("board.json records the schema version", json.load(open(ws.board_path))["schema_version"] == SCHEMA_VERSION)

    # -----------------------------------------------------------------
    section("an old folder (no version at all) migrates forward")
    old = new_ws()
    os.makedirs(old.path, exist_ok=True)
    # what a pre-versioning folder looked like: a board, some cards, worker files, no stamp
    shutil.copytree(ws.path, old.path, dirs_exist_ok=True)
    os.remove(old.version_path)
    shutil.rmtree(old.baseline_dir, ignore_errors=True)
    raw = json.load(open(old.board_path))
    raw.pop("schema_version", None)
    json.dump(raw, open(old.board_path, "w"), indent=2)

    check("it reads as schema v0", old.schema_version() == 0)
    check(
        "every migration is pending",
        [m.number for m in migrations.pending(old)] == list(range(1, migrations.LATEST + 1)),
    )
    check("the server REFUSES to serve it", bool(update.preflight(old)))

    before_cards = len(json.load(open(old.board_path))["cards"])
    notes = update.migrate(old)

    check("it is now at the latest schema", old.schema_version() == migrations.LATEST)
    check("board.json got the schema version", json.load(open(old.board_path))["schema_version"] == SCHEMA_VERSION)
    check("the server will now serve it", update.preflight(old) == [])
    check("it backed the folder up first", any("backed up" in n for n in notes))
    check("the backup is on disk", os.path.isdir(old.backups_dir) and os.listdir(old.backups_dir))

    # decision 3, as a constraint on decision 4: nothing was lost
    after = json.load(open(old.board_path))
    reloaded = Board(old.data_dir, workers_dir=old.workers_dir)
    survivor = reloaded.cards[card.id]
    check("every card survived the migration", len(after["cards"]) == before_cards)
    check("the card's thread survived", any(m.text == "remember me" for m in survivor.thread))
    check("the card's folder survived", os.path.isdir(reloaded.abs_dir(survivor)))
    check(
        "the artifact inside the card folder survived",
        os.path.isfile(os.path.join(reloaded.abs_dir(survivor), "IDEA.md")),
    )
    check("the columns survived", len(reloaded.pipelines.columns[PLAN]) == 6)

    # -----------------------------------------------------------------
    section("migrations are idempotent")
    stamp_before = old.read_version()["schema_version"]
    notes2 = update.migrate(old)
    check("running it again applies nothing", any("nothing to migrate" in n for n in notes2))
    check("the schema version did not move", old.read_version()["schema_version"] == stamp_before)
    check("the card is still there", len(json.load(open(old.board_path))["cards"]) == before_cards)

    # -----------------------------------------------------------------
    section("a failing migration fails LOUDLY and stamps nothing")
    broken = new_ws()
    broken.ensure()
    broken.stamp(0)  # pretend it is old, so there is something to run

    class Boom:
        number, name, description = 1, "boom", "a migration that explodes"
        label = "0001 a migration that explodes"

        @staticmethod
        def run(_ws):
            raise RuntimeError("disk on fire")

    real_discover = migrations.discover
    migrations.discover = lambda: [Boom]
    try:
        update.migrate(broken)
        check("it raises", False)
    except migrations.MigrationFailed as e:
        check("it raises MigrationFailed", True)
        check("the message names the migration", "explodes" in str(e))
        check("the message gives you the backup path", "backup" in str(e).lower())
        check("the backup really exists", os.path.isdir(e.backup_path))
        check("the folder was NOT stamped forward", broken.schema_version() == 0)
    finally:
        migrations.discover = real_discover

    # -----------------------------------------------------------------
    section("prompts: yours survive, upstream's improvements still reach you")
    p = new_ws()
    p.ensure()
    pboard = update.board_for(p)  # scaffolds every worker from the defaults
    store = pboard.workers

    untouched = store.path("plan", "scoping")
    customized = store.path("plan", "planning")
    mine = read(customized) + "\n- ALSO: always list the competitors. (my own rule)\n"
    with open(customized, "w", encoding="utf-8") as fh:
        fh.write(mine)

    check("a scaffolded worker has a baseline snapshot", os.path.isfile(store.baseline_path("plan", "scoping")))

    # upstream improves BOTH prompts
    defaults = all_defaults()
    defaults["plan/scoping"] += "\n- NEW UPSTREAM RULE: name the prior art.\n"
    defaults["plan/planning"] += "\n- NEW UPSTREAM RULE: record the commit SHA.\n"

    result = prompts.sync(store, defaults=defaults)
    actions = {c.key: c.action for c in result.changes}

    check("the prompt I never touched took the improvement", actions["plan/scoping"] == prompts.UPDATED)
    check("...and the new rule is really in my file now", "NEW UPSTREAM RULE" in read(untouched))
    check("the prompt I edited was KEPT", actions["plan/planning"] == prompts.KEPT)
    check("...my edit is still there", "my own rule" in read(customized))
    check("...and upstream's change did NOT overwrite it", "NEW UPSTREAM RULE" not in read(customized))
    check("...but I am told about it, with a diff", bool([c for c in result.kept if c.key == "plan/planning"][0].diff))
    check("an unchanged prompt is reported as such", actions["plan/ideas"] == prompts.UNCHANGED)

    report = prompts.write_report(os.path.join(p.path, "PROMPT_CHANGES.md"), result, "9.9.9")
    check("the diffs I have not taken are written where I can read them", os.path.isfile(report))
    check("the report names the prompt", "plan/planning" in read(report))

    # -----------------------------------------------------------------
    section("two projects cannot silently share one working folder")
    clash = Workspace(p.path, tempfile.mkdtemp(prefix="other-repo-"))
    try:
        clash.ensure()
        check("it refuses", False)
    except WorkspaceError as e:
        check("it refuses, and says how to fix it", "MANAGER_WORKSPACE" in str(e))

    # -----------------------------------------------------------------
    section("an old in-place data/ folder is adopted, not stranded")
    fake_system = tempfile.mkdtemp(prefix="km-system-")
    legacy_data = os.path.join(fake_system, "data")
    legacy_workers = os.path.join(fake_system, "workers", "plan")
    os.makedirs(os.path.join(legacy_data, "cards", "plan", "ideas", "abc-old-card"))
    os.makedirs(legacy_workers)
    with open(os.path.join(legacy_data, "board.json"), "w") as fh:
        json.dump({"cards": [], "order": [], "managers": []}, fh)
    with open(os.path.join(legacy_data, "cards", "plan", "ideas", "abc-old-card", "IDEA.md"), "w") as fh:
        fh.write("# an old card's work\n")
    with open(os.path.join(legacy_workers, "scoping.md"), "w") as fh:
        fh.write("---\ntitle: Scoping\n---\n\n## Work\nmy hand-tuned prompt\n")

    real_system = workspace.SYSTEM_DIR
    workspace.SYSTEM_DIR = fake_system
    try:
        adopted = Workspace(tempfile.mkdtemp(prefix="km-adopted-"), tempfile.mkdtemp(prefix="repo-"))
        notes = adopted.ensure()
    finally:
        workspace.SYSTEM_DIR = real_system

    check("it noticed the old folder", any("adopting" in n for n in notes))
    check(
        "the old card's folder came across",
        os.path.isfile(os.path.join(adopted.path, "cards", "plan", "ideas", "abc-old-card", "IDEA.md")),
    )
    check(
        "the hand-tuned prompt came across",
        "my hand-tuned prompt" in read(os.path.join(adopted.workers_dir, "plan", "scoping.md")),
    )
    check("the original was kept, not deleted", any(d.startswith("data.migrated-") for d in os.listdir(fake_system)))
    check("nothing is left where an update would destroy it", not os.path.isdir(os.path.join(fake_system, "data")))

    # -----------------------------------------------------------------
    # The scenario UPGRADING.md §4 exists for, and the one that used to strand a board:
    # someone hands you a NEW system folder, and the old install — with all the cards in
    # it — is a completely different directory. A `git pull` cannot save you here.
    section("adopting an old install that lives somewhere else entirely")
    old_install = _fake_old_install()
    target = Workspace(tempfile.mkdtemp(prefix="km-new-ws-"), tempfile.mkdtemp(prefix="repo-"))

    notes = target.adopt(old_install)
    notes += update.migrate(target)

    adopted_board = json.load(open(target.board_path))
    ids = [c["id"] for c in adopted_board["cards"]]
    check("the old install's cards came across", ids == ["oldcard1", "oldcard2"])
    check("their threads came across", adopted_board["cards"][0]["thread"][0]["text"] == "the human said this")
    check(
        "their card FOLDERS came across, with the work in them",
        os.path.isfile(os.path.join(target.path, "cards", "plan", "scoping", "oldcard1-legacy-card", "SCOPE.md")),
    )
    check(
        "the human's hand-tuned prompt came across, unmodified",
        "MY OWN RULE" in read(os.path.join(target.workers_dir, "plan", "scoping.md")),
    )
    check("their custom column came across", len(json.load(open(os.path.join(target.path, "pipelines.json")))["plan"]) == 7)
    check("it was migrated forward to the current schema", target.schema_version() == migrations.LATEST)
    check("the board is now servable", update.preflight(target) == [])
    check("the OLD INSTALL WAS NOT TOUCHED", os.path.isfile(os.path.join(old_install, "data", "board.json")))
    check("...and it says so", any("COPIED, not moved" in n for n in notes))
    # The adopted prompt differs from the shipped default, so we cannot prove the human
    # didn't write it — which means it must NOT get a baseline, which means no future
    # update will ever overwrite it.
    adopted_store = WorkerStore(target.workers_dir, baseline_dir=target.baseline_dir)
    check(
        "no baseline was invented for the prompt they edited",
        not os.path.isfile(adopted_store.baseline_path("plan", "scoping")),
    )
    newer = all_defaults()
    newer["plan/scoping"] += "\n- an upstream improvement, two versions later\n"
    later = prompts.sync(adopted_store, defaults=newer)
    check(
        "so a LATER upgrade still keeps their version",
        {c.key: c.action for c in later.changes}["plan/scoping"] == prompts.KEPT,
    )
    check("...their rule is still in the file", "MY OWN RULE" in read(adopted_store.path("plan", "scoping")))

    # The landmine: a `.workspace` pointer file copied in from another install aims this run
    # at a working folder that ALREADY has a board — so the old board sitting right here is
    # never adopted, and the human gets a board that comes up looking fine, and empty.
    section("an old board next to a working folder that already has one: REFUSE, don't ignore")
    stranding = _fake_old_install()
    occupied = new_ws()
    occupied.ensure()
    await update.board_for(occupied).add_card("a card that is already here")

    real_system = workspace.SYSTEM_DIR
    workspace.SYSTEM_DIR = stranding
    try:
        occupied.ensure()
        check("it refuses to strand the old board", False)
    except WorkspaceError as e:
        check("it refuses to strand the old board", True)
        check("...it names the board it would have ignored", stranding in str(e))
        check("...it tells you how to use the old one", "adopt" in str(e))
        check("...and it fingers the stale pointer file", ".workspace" in str(e))
    finally:
        workspace.SYSTEM_DIR = real_system

    section("...and it refuses to write over a board that is already there")
    try:
        target.adopt(_fake_old_install())
        check("it refuses", False)
    except WorkspaceError as e:
        check("it refuses to merge two boards", "already holds a board" in str(e))

    print(f"\n{'ALL PASSED' if not FAILED else 'FAILURES: ' + chr(10) + chr(10).join('  - ' + f for f in FAILED)}")
    return 0 if not FAILED else 1


def _fake_old_install() -> str:
    """A pre-0.2 install, as it really looked: state in data/ and workers/ INSIDE the system
    folder, a board.json with no schema version, a custom column and an edited prompt."""
    root = tempfile.mkdtemp(prefix="km-old-install-")
    data = os.path.join(root, "data")
    card_dir = os.path.join(data, "cards", "plan", "scoping", "oldcard1-legacy-card")
    os.makedirs(card_dir)
    os.makedirs(os.path.join(data, "cards", "trash"))
    os.makedirs(os.path.join(root, "workers", "plan"))
    os.makedirs(os.path.join(root, "manager"))  # it is a system folder, after all

    with open(os.path.join(card_dir, "SCOPE.md"), "w") as fh:
        fh.write("# a real scope the human paid for\n")
    with open(os.path.join(data, "board.json"), "w") as fh:
        json.dump(
            {
                "cards": [
                    {
                        "id": "oldcard1",
                        "title": "Legacy card",
                        "pipeline": "plan",
                        "column": "col-scoping",
                        "dir": os.path.join("cards", "plan", "scoping", "oldcard1-legacy-card"),
                        "thread": [{"role": "user", "text": "the human said this", "ts": 1.0}],
                    },
                    {"id": "oldcard2", "title": "Another", "pipeline": "plan", "column": "col-scoping"},
                ],
                "order": ["oldcard1", "oldcard2"],
                "managers": [{"id": "m1", "name": "Manager", "emoji": "🧭", "thread": []}],
            },
            fh,
        )
    # their columns, including one they invented — must survive exactly
    with open(os.path.join(data, "pipelines.json"), "w") as fh:
        json.dump(
            {
                "plan": [
                    {"id": "col-ideas", "pipeline": "plan", "slug": "ideas", "title": "Ideas", "gate": False},
                    {"id": "col-scoping", "pipeline": "plan", "slug": "scoping", "title": "Scoping", "gate": False},
                    {"id": "col-mine", "pipeline": "plan", "slug": "market-research", "title": "Market Research", "gate": False},
                    {"id": "col-sr", "pipeline": "plan", "slug": "scope-review", "title": "Scope Review", "gate": False},
                    {"id": "col-p", "pipeline": "plan", "slug": "planning", "title": "Planning", "gate": False},
                    {"id": "col-pr", "pipeline": "plan", "slug": "plan-review", "title": "Plan Review", "gate": False},
                    {"id": "col-ready", "pipeline": "plan", "slug": "plan-ready", "title": "Plan Ready", "gate": True},
                ],
                "build": [
                    {"id": "col-code", "pipeline": "build", "slug": "coding", "title": "Coding", "gate": False},
                ],
            },
            fh,
        )
    with open(os.path.join(root, "workers", "plan", "scoping.md"), "w") as fh:
        fh.write(
            "---\ntitle: Scoping\npipeline: plan\ndescription: mine\ntools: Read, Write\nmodel: inherit\n---\n\n"
            "## Entry criteria\nIDEA.md exists.\n\n## Work\nMY OWN RULE: always name three competitors.\n\n"
            "## Exit criteria\nSCOPE.md exists.\n\n## Output\n`SCOPE.md`\n"
        )
    return root


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
