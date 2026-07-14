"""Talking to the manager about ONE column's worker.

Three things are being defended here.

  1. **It is its own conversation.** Its own thread, its own SDK session, keyed
     `<pipeline>/<slug>`. Folding it into the board chat would have been less code and
     would have buried "make this reviewer stop inventing work" under "what's in flight?".

  2. **It is journalled, so it is recoverable.** It is a new kind of long-running SDK run,
     and an un-journalled run is one that vanishes when the process dies — leaving a thread
     spinning against nothing. That was a real incident once; see docs/INCIDENT-*.

  3. **A rename does not orphan it.** The key contains the column's slug, so renaming the
     column must carry the conversation with it, or the human's discussion of the contract
     is silently abandoned on the floor.

    python tests/workerchat_test.py        (no API key, no network)
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager.board import Board  # noqa: E402
from manager.journal import CARD, WORKER, Journal  # noqa: E402
from manager.manager import POLICY_KEY, Manager, ManagerConfig  # noqa: E402
from manager.migrations import m0005_worker_chats as m0005  # noqa: E402
from manager import policy
from manager.models import MAINT, PLAN  # noqa: E402
from manager.recovery import Recovery  # noqa: E402
from manager.workspace import Workspace  # noqa: E402

FAILED: list = []
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


def new_board(d=None) -> Board:
    return Board(d or tempfile.mkdtemp(prefix="km-wc-"))


def mgr(b: Board, journal=None) -> Manager:
    return Manager(
        b, ManagerConfig(repo_dir=".", data_dir=b.data_dir, mock=True), journal=journal
    )


async def main() -> int:
    page = open(WEB, encoding="utf-8").read()

    # -----------------------------------------------------------------
    section("a worker has its own conversation, and it persists")
    b = new_board()
    m = mgr(b)
    key = b.worker_key(MAINT, "exploring")

    check("it starts empty", b.worker_chat(key).thread == [])
    await m.handle_prompt_message(key, "this one is too vague")
    chat = b.worker_chat(key)
    check("what I said is in the thread", chat.thread[0].role == "user")
    check("the manager answered", any(x.role == "manager" for x in chat.thread))
    check("it is not left spinning", chat.busy is False)
    check(
        "the reply is about THIS worker",
        "Exploring" in chat.thread[-1].text,
    )

    check("it survives a reload", len(new_board(b.data_dir).worker_chat(key).thread) == 2)

    other = b.worker_key(PLAN, "scoping")
    check("a different worker is a different conversation", b.worker_chat(other).thread == [])
    check("...and the board chat is untouched by it", all(
        not mm.thread for mm in b.managers.values()
    ))

    # -----------------------------------------------------------------
    section("a nonexistent worker is not a conversation")
    await m.handle_prompt_message("plan/does-not-exist", "hello?")
    check("nothing was created for it", "plan/does-not-exist" not in b.worker_chats)

    # -----------------------------------------------------------------
    section("renaming the column carries the conversation with it")
    col = b.pipelines.by_slug(MAINT, "exploring")
    await b.rename_column(col.id, "Root Cause")
    new_key = b.worker_key(MAINT, "root-cause")
    check("the conversation moved to the new key", new_key in b.worker_chats)
    check("the old key is gone", key not in b.worker_chats)
    check("and every message came with it", len(b.worker_chat(new_key).thread) == 2)
    check("...even across a reload", len(new_board(b.data_dir).worker_chat(new_key).thread) == 2)

    # -----------------------------------------------------------------
    section("the run is JOURNALLED — so a killed process can resume it")
    d = tempfile.mkdtemp(prefix="km-wc2-")
    b2 = new_board(d)
    j = Journal(d)
    key2 = b2.worker_key(PLAN, "scoping")

    # Simulate the process dying mid-run: a journal entry left behind, thread marked busy.
    j.start(WORKER, key2, "make the exit criteria falsifiable", session_id=None)
    await b2.set_worker_busy(key2, True)
    check("the run is on disk before it finishes", j.get(WORKER, key2) is not None)
    check("...and inflight.json is the file that says so", os.path.exists(os.path.join(d, "inflight.json")))

    resumed = []
    def dispatch(coro, label):
        resumed.append(label)
        coro.close()  # don't actually run it; we are asserting that it WOULD be

    notes = await Recovery(b2, mgr(b2, journal=j), j).run(dispatch)
    check("recovery resumes the worker run", any("resume worker" in r for r in resumed))
    check("...and says so", any("worker" in n for n in notes))

    # a worker chat left busy with NO journal entry behind it is a lie: clear it
    b3 = new_board(tempfile.mkdtemp(prefix="km-wc3-"))
    j3 = Journal(b3.data_dir)
    k3 = b3.worker_key(PLAN, "planning")
    await b3.set_worker_busy(k3, True)
    await Recovery(b3, mgr(b3, journal=j3), j3).run(lambda c, l: c.close())
    check("an orphaned 'thinking' spinner is cleared", b3.worker_chat(k3).busy is False)

    # -----------------------------------------------------------------
    section("the manager's OWN brain is a prompt chat too")
    bd = new_board()
    policy.ensure(bd.data_dir)
    bm = mgr(bd)
    check("the reserved key cannot collide with a worker's", "/" not in POLICY_KEY)
    check(
        "no column could ever produce it",
        all(bd.worker_key(c.pipeline, c.slug) != POLICY_KEY for c in bd.pipelines.all_columns()),
    )

    before = policy.read(bd.data_dir)
    check("he has standing orders to begin with", "Decide. Do not ask." in before)

    await bm.handle_prompt_message(POLICY_KEY, "you ask me too much")
    pchat = bd.worker_chat(POLICY_KEY)
    check("it is its own conversation", len(pchat.thread) == 2)
    check("...and it answered about the ORDERS", "standing orders" in pchat.thread[-1].text)
    check("it is not left spinning", pchat.busy is False)
    check("it persists", len(new_board(bd.data_dir).worker_chat(POLICY_KEY).thread) == 2)

    # the file itself is editable, and cannot be silently erased
    policy.write(bd.data_dir, "# Mine\nAlways ask me first.")
    check("writing the orders sticks", "Always ask me first." in policy.read(bd.data_dir))
    check("...and reaches his prompt", "Always ask me first." in policy.block(bd.data_dir))
    check("recovery knows this key always exists",
          Recovery(bd, bm, Journal(bd.data_dir))._column_exists(POLICY_KEY))

    # -----------------------------------------------------------------
    section("the migration opens the drawer, and touches nothing else")
    ws = Workspace(tempfile.mkdtemp(prefix="km-ws-"), tempfile.mkdtemp(prefix="repo-"))
    ws.ensure()
    bb = Board(ws.path)
    card = await bb.add_card("Keep me")
    del bb

    raw = json.load(open(ws.board_path))
    raw.pop("worker_chats", None)  # rewind to a pre-worker-chat folder
    json.dump(raw, open(ws.board_path, "w"), indent=2)

    notes = m0005.migrate(ws)
    after = json.load(open(ws.board_path))
    check("it reports what it did", bool(notes))
    check("the key is there", after.get("worker_chats") == {})
    check("the card is untouched", len(after["cards"]) == 1 and after["cards"][0]["id"] == card.id)
    check("running it twice does nothing", m0005.migrate(ws) == [])

    # -----------------------------------------------------------------
    section("the UI: a chat under the file, and a ✕ that goes back where you came from")
    check("the chat box is titled as asked", "Talk to the manager about this worker" in page)
    check("it has its own thread and composer", "w-thread" in page and "w-chat-send" in page)
    check(
        "starting the chat collapses the file (both prompt drawers)",
        "#d-worker.chatting textarea.code,#d-brain.chatting textarea.code{min-height:0" in page,
    )
    check("...and it collapses on the first message", "setChatting(true)" in page)
    check("...and stays collapsed when you reopen a worker you've talked to",
          "setChatting((m.thread||[]).length>0)" in page)
    check("the ✕ returns to the pipeline panel if you came from there",
          "wFrom ? openPipe(wFrom) : closeAll()" in page)
    check("the manager has a brain button", 'id="m-brain"' in page)
    check("...which opens his standing orders", "function openBrain" in page)
    check("...with a chat to rewrite them", "Ask the manager to rewrite his own orders" in page)
    check("...and a ✕ back to his chat", "bFrom ? openManager(bFrom) : closeAll()" in page)
    check("the manager's edits are pulled back into the file", "refreshWorkerFile()" in page)
    check("...but never over your unsaved edits", "if(!wCtx || wDirty) return;" in page)

    print(f"\n{'=' * 70}")
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("worker chat: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
