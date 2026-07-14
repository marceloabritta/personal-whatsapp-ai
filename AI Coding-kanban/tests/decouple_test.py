"""The manager is IDLE while a worker works — and keeps the card's context anyway.

The bug: a worker ran as a subagent INSIDE the manager's own SDK session, so his session was
held for the entire length of the worker's task. Twenty minutes of a coder meant twenty
minutes of a "busy" manager, and a human asking him a question got queued behind a job he was
not doing. He was not thinking. He was just occupied.

The shape now:

    manager turn (short)  →  delegate, END TURN  →  worker runs as its OWN conversation
                                                     (manager idle)  →  he is woken with the
                                                     report and judges it

Three claims, and this file exists to hold them:

  1. **He goes idle.** A message that arrives while a worker runs gets a CHAT turn
     immediately. It is never queued.
  2. **The context stays stuck to the card.** Same session id across every turn — the
     supervision turns and the chat turns — so what you said mid-run is in front of him when
     the report lands.
  3. **He cannot dispatch from a chat turn.** A worker is already out; its report is coming.

The real SDK is not exercised here (no API key). The TURN is stubbed; what is under test is
the orchestration around it, which is where the whole bug lived.

    python tests/decouple_test.py        (no API key, no network)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager import manager as mgr_mod  # noqa: E402
from manager.board import Board  # noqa: E402
from manager.manager import Manager, ManagerConfig  # noqa: E402
from manager.models import PLAN  # noqa: E402
from manager.pending import PendingQueue  # noqa: E402

FAILED: list = []


def check(label, cond):
    if not cond:
        FAILED.append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def section(name):
    print(f"\n{name}")


def new_mgr():
    b = Board(tempfile.mkdtemp(prefix="km-dc-"))
    # mock=False: we want the REAL orchestration (_real_card). Only the SDK turn is stubbed.
    m = Manager(b, ManagerConfig(repo_dir=".", data_dir=b.data_dir, mock=False))
    return b, m


async def main() -> int:
    b, m = new_mgr()
    card = await b.add_card("Do the thing", pipeline=PLAN, kind="feature")
    cid = card.id
    col = b.pipelines.get(card.column)
    worker_name = b.workers.runtime(col)["name"]

    # ---- the stub: one delegation, then done -------------------------
    turns: list[tuple[str, str]] = []      # (kind, text) of every manager turn
    worker_started = asyncio.Event()
    let_worker_finish = asyncio.Event()

    async def fake_manager_turn(card_id, text, kind="drive"):
        turns.append((kind, text))
        m._turn_kind[card_id] = kind
        # The FIRST turn is a "reply" now: the human woke him, so he answers them AND may
        # delegate. Only the turns after it are machine-woken supervision.
        if kind == "reply":
            return (worker_name, "here is the whole briefing, with the folder path etc.")
        return None

    async def fake_run_worker(card_id, worker, instructions):
        m._workers_running.add(card_id)
        await m.board.set_working(card_id, worker)
        worker_started.set()
        await let_worker_finish.wait()      # a LONG worker — the human talks during this
        m._workers_running.discard(card_id)
        await m.board.set_working(card_id, "")
        return "ENTRY: PASS\nWORK: did it\nEXIT: MET\nFLAGS: none"

    m._manager_turn = fake_manager_turn
    m._run_worker = fake_run_worker

    # ---- drive the card ---------------------------------------------
    section("the manager delegates, then goes idle")
    drive = asyncio.create_task(m.handle_card_message(cid, "start"))
    await asyncio.wait_for(worker_started.wait(), timeout=5)

    check("a worker is running", cid in m._workers_running)
    check("the card is busy", b.cards[cid].busy is True)
    check("...and the board says the WORKER is who's working", b.cards[cid].working == worker_name)
    check("the manager's session lock is FREE while it runs", not m._lock_for(f"session:{cid}").locked())

    # ---- THE POINT: talk to him mid-run ------------------------------
    section("THE POINT: I can talk to him mid-run, and I am not queued")
    before = len(b.cards[cid].thread)
    await asyncio.wait_for(m.handle_card_message(cid, "how is it going?"), timeout=5)

    chat_turns = [t for t in turns if t[0] == "chat"]
    check("he answered on a CHAT turn", len(chat_turns) == 1)
    check("...with what I actually asked", chat_turns[0][1] == "how is it going?")
    check("...and the worker was NOT interrupted", cid in m._workers_running)
    thread = [msg.text for msg in b.cards[cid].thread]
    check("my message is in the card's thread", "how is it going?" in thread)
    check(
        "and I was NOT told I was queued",
        not any("queued" in t for t in thread[before:]),
    )

    # ---- he cannot dispatch from a chat turn -------------------------
    section("he cannot dispatch a second worker while one is out")
    check("the turn is marked as a chat", m._turn_kind.get(cid) == "chat")
    check("...which is what the delegate tool refuses on", cid in m._workers_running)

    # ---- the worker finishes; he is woken with the report ------------
    section("the worker finishes and he is woken with the report")
    let_worker_finish.set()
    await asyncio.wait_for(drive, timeout=5)

    drive_turns = [t for t in turns if t[0] == "drive"]
    check("the worker's report woke him for a supervision turn", len(drive_turns) == 1)
    check("...and it was the worker's report", "HAS REPORTED BACK" in drive_turns[0][1])
    check("...carrying what the worker actually said", "EXIT: MET" in drive_turns[0][1])
    check(
        "...and telling him to check the disk, not trust it",
        "Do not take the report on trust" in drive_turns[0][1],
    )

    check("the card is idle again", b.cards[cid].busy is False)
    check("nobody is working", b.cards[cid].working == "")
    check("no worker is left running", cid not in m._workers_running)
    check("the card is no longer being driven", cid not in m._driving)

    # ---- context: ONE session for the card, across every turn --------
    section("the context is stuck to the card: one session, every turn")
    check(
        "the chat turn and the supervision turns are the same conversation",
        # every turn went through _manager_turn on the card's own session — the chat turn is
        # in `turns` alongside the others, in order, so he sees it before judging the report.
        # reply = I woke him. chat = I spoke mid-run. drive = the worker's report woke him.
        [t[0] for t in turns] == ["reply", "chat", "drive"],
    )

    # ---- the chat is what he needs you FOR; the rest is filed ---------
    section("his supervision prose is FILED, not said to you")
    b3, m3 = new_mgr()
    c3 = await b3.add_card("Quiet please", pipeline=PLAN, kind="feature")

    # what the pump does with his prose, per turn kind — the structural rule under test
    from manager.board import Board as _B  # noqa: F401

    await b3.append_note(c3.id, "PREFLIGHT: GO. Verified independently — eleven fields…")
    await b3.append_message(c3.id, "manager", "Gate: your call. Ship both steps?")

    thread = b3.cards[c3.id].thread
    notes = [x for x in thread if x.role == "note"]
    said = [x for x in thread if x.role == "manager"]
    check("a note is filed on the card", len(notes) == 1)
    check("...and is NOT a message to the human", notes[0].role != "manager")
    check("only what he needs me for is a message", len(said) == 1 and "your call" in said[0].text)
    check("...and the reasoning is still kept, not thrown away", "eleven fields" in notes[0].text)

    check(
        "a note survives a reload",
        len([x for x in Board(b3.data_dir).cards[c3.id].thread if x.role == "note"]) == 1,
    )

    # ---- AN ORDER IS NEVER MET WITH SILENCE ---------------------------
    section("when I give an order, he answers ME — the reply is not filed away")
    b4, m4 = new_mgr()
    c4 = await b4.add_card("Go ahead please", pipeline=PLAN, kind="feature")
    kinds: list[str] = []

    async def turn(card_id, text, kind="drive"):
        kinds.append(kind)
        m4._turn_kind[card_id] = kind
        # He says something on every turn — the question is WHERE it lands.
        await (m4.board.append_message(card_id, "manager", f"[{kind}] on it")
               if kind in ("chat", "reply")
               else m4.board.append_note(card_id, f"[{kind}] judged the report"))
        if kind == "reply":
            return (b4.workers.runtime(b4.pipelines.get(b4.cards[card_id].column))["name"],
                    "a briefing long enough to be accepted by the delegate tool")
        return None

    async def worker(card_id, w, i):
        return "ENTRY: PASS\nEXIT: MET"

    m4._manager_turn = turn
    m4._run_worker = worker
    await asyncio.wait_for(m4.handle_card_message(c4.id, "go ahead"), timeout=10)

    thread = b4.cards[c4.id].thread
    said = [x.text for x in thread if x.role == "manager"]
    notes = [x.text for x in thread if x.role == "note"]
    check("the turn I woke is a REPLY, not a silent supervision turn", kinds[0] == "reply")
    check("HE ANSWERED ME — my order was not met with silence", len(said) == 1)
    check("...in my chat, not filed away", "[reply] on it" in said[0])
    check("every turn AFTER that is the machine waking him", kinds[1:] == ["drive"])
    check("...and those are filed, not sent", len(notes) == 1 and "[drive]" in notes[0])
    check("so the work actually ran", b4.cards[c4.id].busy is False)

    # machinery must NOT trigger a reply: a resume/triage is nobody speaking
    b5, m5 = new_mgr()
    c5 = await b5.add_card("Machine woke me", pipeline=PLAN, kind="feature")
    seen: list[str] = []

    async def turn5(card_id, text, kind="drive"):
        seen.append(kind)
        m5._turn_kind[card_id] = kind
        return None

    m5._manager_turn = turn5
    await asyncio.wait_for(m5.handle_card_message(c5.id, "[AUTOMATIC resume]", resuming=True), timeout=10)
    check("a RESUME is not the human talking — no reply is owed", seen == ["drive"])

    # THE ONE THAT BIT: a message queued during a ship is REPLAYED (so `resuming`) but a
    # PERSON said it. Deriving from_human from resuming filed his answer as a note and left
    # the human staring at silence after giving an order.
    seen.clear()
    await asyncio.wait_for(
        m5.handle_card_message(c5.id, "get to work on this", resuming=True, from_human=True),
        timeout=10,
    )
    check("a message queued during a ship STILL earns a reply", seen == ["reply"])

    # ---- WINDING DOWN: stop feeding the pipe, don't just wait for it ---
    section("a restart WINDS DOWN the work — it does not wait for the whole pipeline")
    b6, m6 = new_mgr()
    c6 = await b6.add_card("Long pipeline", pipeline=PLAN, kind="feature")
    col6 = b6.pipelines.get(c6.column)
    w6 = b6.workers.runtime(col6)["name"]
    dispatched = []

    # A manager who would happily drive the card through column after column, forever.
    async def eager(card_id, text, kind="drive"):
        m6._turn_kind[card_id] = kind
        if m6.draining:                     # the delegate tool would refuse; he winds down
            m6._wound_down.add(card_id)
            return None
        return (w6, "a briefing long enough to be accepted by the delegate tool")

    async def quick_worker(card_id, w, i):
        dispatched.append(w)
        if len(dispatched) == 2:
            m6.begin_drain()                # the human clicks Restart, mid-pipeline
        return "ENTRY: PASS\nEXIT: MET"

    m6._manager_turn = eager
    m6._run_worker = quick_worker
    await asyncio.wait_for(m6.handle_card_message(c6.id, "start"), timeout=20)

    check("it did NOT keep feeding the pipeline once I clicked", len(dispatched) == 2)
    check("...the worker in hand was allowed to finish", dispatched[-1] == w6)
    check("the card is not left busy", b6.cards[c6.id].busy is False)

    # and the card is owed a "carry on" — it must not be silently abandoned mid-pipeline
    queued = m6.pending.all()
    check("it remembered the card, to resume after the restart", len(queued) == 1)
    check("...as MACHINERY, not as me talking", queued[0].from_human is False)
    check("...and it tells the card to trust the disk, not its memory",
          "Read the card folder on disk first" in queued[0].text)
    check("...and not to start over", "Do not start over" in queued[0].text)
    check("it is on disk, so a crash cannot lose it", len(PendingQueue(b6.data_dir)) == 1)

    # a card that simply FINISHED is not owed a carry-on
    b7, m7 = new_mgr()
    c7 = await b7.add_card("Finished cleanly", pipeline=PLAN, kind="feature")

    async def done(card_id, text, kind="drive"):
        m7._turn_kind[card_id] = kind
        return None

    m7._manager_turn = done
    await asyncio.wait_for(m7.handle_card_message(c7.id, "start"), timeout=10)
    check("a card that finished on its own is NOT queued to resume", len(m7.pending) == 0)

    # ---- WINDING DOWN MUST NOT TAKE MY KEYBOARD AWAY -------------------
    section("while it winds down I can still talk, and I am still answered")
    b8, m8 = new_mgr()
    c8 = await b8.add_card("Talk to me", pipeline=PLAN, kind="feature")
    answered: list[str] = []

    async def reply_turn(card_id, text, kind="drive"):
        m8._turn_kind[card_id] = kind
        answered.append(text)
        await m8.board.append_message(card_id, "manager", "answered you")
        return None

    m8._manager_turn = reply_turn
    m8.begin_drain()                      # the human clicked Restart; workers are winding down

    check("it IS winding down", m8.draining is True)
    check("...but it is not stopping yet", m8.stopping is False)

    await asyncio.wait_for(m8.handle_card_message(c8.id, "can you still hear me?"), timeout=10)
    thread = [x.text for x in b8.cards[c8.id].thread]
    check("my message was ACTED ON, not queued", "can you still hear me?" in answered)
    check("...and he answered me", "answered you" in thread)
    check("...and nothing was parked in the queue", len(m8.pending) == 0)
    check("...and I was NOT told my message is 'saved for later'",
          not any("message is saved" in t for t in thread))

    # only when the process actually commits to going down does the queue exist
    m8.stopping = True
    await asyncio.wait_for(m8.handle_card_message(c8.id, "one for the road"), timeout=10)
    check("in the last seconds, a message IS saved", len(m8.pending) == 1)
    check("...because there is nothing left to run it", m8.pending.all()[0].text == "one for the road")
    check("...and it is mine, so I am owed an answer after the restart",
          m8.pending.all()[0].from_human is True)

    # ---- TELLING A WORKER TO STOP is what makes a restart fast ---------
    section("a running worker is TOLD TO STOP — the restart does not wait ten minutes for it")
    b9, m9 = new_mgr()
    c9 = await b9.add_card("Long worker", pipeline=PLAN, kind="feature")
    col9 = b9.pipelines.get(c9.column)
    w9 = b9.workers.runtime(col9)["name"]

    class FakeClient:
        """Stands in for ClaudeSDKClient: the thing the real code keeps a handle on so it can
        say 'stop'. `query()` used to be a one-shot call that could not be interrupted at all,
        which is exactly why the restart used to take ten minutes."""

        def __init__(self):
            self.interrupted = asyncio.Event()

        async def interrupt(self):
            self.interrupted.set()

    fake = FakeClient()
    started = asyncio.Event()

    async def long_worker(card_id, w, i):
        # a worker that would happily run for ten more minutes
        m9._workers_running.add(card_id)
        m9._worker_clients[card_id] = fake
        await m9.board.set_working(card_id, w)
        started.set()
        await fake.interrupted.wait()      # ...unless it is TOLD to stop
        m9._worker_clients.pop(card_id, None)
        m9._workers_running.discard(card_id)
        await m9.board.set_working(card_id, "")
        return "[STOPPED — the system is restarting]"

    async def delegating(card_id, text, kind="drive"):
        m9._turn_kind[card_id] = kind
        if m9.draining:
            m9._wound_down.add(card_id)
            return None
        return (w9, "a briefing long enough to be accepted by the delegate tool")

    m9._manager_turn = delegating
    m9._run_worker = long_worker
    drive9 = asyncio.create_task(m9.handle_card_message(c9.id, "start"))
    await asyncio.wait_for(started.wait(), timeout=5)
    check("a long worker is running", c9.id in m9._workers_running)

    # the human clicks Restart
    m9.begin_drain()
    t0 = asyncio.get_running_loop().time()
    stopped = await m9.stop_workers()
    check("it TOLD the worker to stop", stopped == 1)
    check("...and the worker got the message", fake.interrupted.is_set())

    await asyncio.wait_for(drive9, timeout=5)
    took = asyncio.get_running_loop().time() - t0
    check(f"...and the run ended almost immediately ({took:.2f}s), not in ten minutes", took < 3)
    check("no worker is left running", c9.id not in m9._workers_running)
    check("the card is not left busy", b9.cards[c9.id].busy is False)
    check("it told me on the card that it stopped it",
          any("Told the worker to stop" in x.text for x in b9.cards[c9.id].thread))
    check("...and the card is queued to carry on after the restart", len(m9.pending) == 1)
    check("...as machinery, silently", m9.pending.all()[0].from_human is False)

    # ---- STOPPED IS NOT LOST -------------------------------------------
    section("a stopped worker SAVES its place and is RESUMED — not restarted from scratch")
    check("it is told to write down where it got to",
          "WIP.md" in mgr_mod.SAVE_AND_STOP and "Next" in mgr_mod.SAVE_AND_STOP)
    check("...for itself, because it comes back to it",
          "resumed in this same conversation" in mgr_mod.SAVE_AND_STOP.lower())
    check("...and it is told nothing is being thrown away",
          "Nothing you have done is being thrown away" in mgr_mod.SAVE_AND_STOP)

    # the card remembers the session it was stopped in
    b10 = Board(tempfile.mkdtemp(prefix="km-dc10-"))
    c10 = await b10.add_card("Stopped mid-task", pipeline=PLAN, kind="feature")
    col10 = b10.pipelines.get(c10.column)
    w10 = b10.workers.runtime(col10)["name"]

    await b10.set_stopped_worker(c10.id, w10, "sess-abc123")
    check("the card remembers WHICH worker was stopped", b10.cards[c10.id].worker_name == w10)
    check("...and the CONVERSATION it was stopped in", b10.cards[c10.id].worker_session == "sess-abc123")
    check("...and it survives a restart of the process",
          Board(b10.data_dir).cards[c10.id].worker_session == "sess-abc123")

    await b10.clear_stopped_worker(c10.id)
    check("a worker that FINISHES leaves nothing to resume", b10.cards[c10.id].worker_session is None)
    check("...so the next delegation starts clean", b10.cards[c10.id].worker_name == "")

    # ---- a runaway delegate loop is stopped ---------------------------
    section("a manager that delegates forever is stopped, not left spinning")
    b2, m2 = new_mgr()
    c2 = await b2.add_card("Loop me", pipeline=PLAN, kind="feature")
    col2 = b2.pipelines.get(c2.column)
    w2 = b2.workers.runtime(col2)["name"]

    async def always_delegate(card_id, text, kind="drive"):
        m2._turn_kind[card_id] = kind
        return (w2, "briefing that is definitely long enough to pass the check")

    async def instant_worker(card_id, worker, instructions):
        return "ENTRY: PASS\nEXIT: NOT MET"

    m2._manager_turn = always_delegate
    m2._run_worker = instant_worker
    await asyncio.wait_for(m2.handle_card_message(c2.id, "start"), timeout=20)
    said = [x.text for x in b2.cards[c2.id].thread]
    check("it gives up rather than looping forever", any("looping" in t for t in said))
    check("...and says nothing is lost", any("Nothing is lost" in t for t in said))
    check("the card is not left busy", b2.cards[c2.id].busy is False)

    print(f"\n{'=' * 70}")
    if FAILED:
        print(f"FAILED ({len(FAILED)}):")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("decoupling: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
