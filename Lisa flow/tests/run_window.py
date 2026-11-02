"""The listening window's length — 60s for conversation, 5 minutes while Lisa waits on an answer.

In the log, answers to her confirmation cards and questions ("Qual o e-mail do Zen?") landed
70-210s after she asked: past the flat 60s window, so they were dropped, and the fresh tag that
followed threw the held booking away. Drives the real compiled graph with the calendar stubs from
run_step3. No network, no Postgres, no Anthropic key.

    python tests/run_window.py
Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import Settings  # noqa: E402
from app.nodes.act import window_ttl  # noqa: E402
from app.prompt import build_system_prompt  # noqa: E402
from app.sessions import RedisSessions  # noqa: E402
from app.skills import output_schema_for, system_prompt_for  # noqa: E402
from run_step3 import OWNER_JID, _invoke, _upsert, make_toolenv  # noqa: E402

_checks = {"pass": 0, "fail": 0}

CREATE = {"task": "calendar.create", "title": "Call", "start": "2026-11-05T15:00:00-03:00",
          "confirmed": False}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def unit_checks() -> None:
    print("\nWindow length — units")
    s = Settings(evolution_apikey="x")
    check("defaults: 60s conversation, 5 min waiting",
          s.loop_ttl_seconds == 60 and s.await_window_seconds == 300)
    check("a plain turn gets the conversational window",
          window_ttl({"llm_state": "keep_listening"}, s) == 60)
    check("the model's awaiting_answer gets the waiting window",
          window_ttl({"llm_state": "awaiting_answer"}, s) == 300)
    check("a held write gets the waiting window",
          window_ttl({"llm_state": "keep_listening", "pending_action": CREATE}, s) == 300)
    check("a gather goal with open questions gets the waiting window",
          window_ttl({"workflow": {"task": "calendar.create",
                                   "open_questions": ["Zen's email"]}}, s) == 300)
    check("a gather goal with nothing left to ask does not",
          window_ttl({"workflow": {"task": "calendar.create", "open_questions": []}}, s) == 60)
    check("a setup loop keeps its own window",
          window_ttl({"loop_domain": "setup"}, Settings(evolution_apikey="x",
                                                        setup_window_seconds=420)) == 420)
    check("the longer of setup and waiting wins",
          window_ttl({"loop_domain": "setup", "llm_state": "awaiting_answer"},
                     Settings(evolution_apikey="x", setup_window_seconds=120)) == 300)

    check("the reviewer waits out the LONGEST window, not the 60s one",
          s.review_settle_window == 300 + s.review_settle_seconds)

    enum = output_schema_for("calendar")["properties"]["state"]["enum"]
    check("the enforced JSON offers awaiting_answer", "awaiting_answer" in enum)
    check("the web skill's lean schema offers it too",
          "awaiting_answer" in output_schema_for("web")["properties"]["state"]["enum"])
    check("the prompt tells her what awaiting_answer is for",
          '"awaiting_answer" when your message asks something' in system_prompt_for("calendar", s))

    class _R:
        def __init__(self, ttl):
            self._ttl = ttl

        def ttl(self, key):
            if isinstance(self._ttl, Exception):
                raise self._ttl
            return self._ttl

    check("redis ttl_left: a live key reports its seconds", RedisSessions(_R(212)).ttl_left("j") == 212)
    check("redis ttl_left: a missing key (-2) is 0", RedisSessions(_R(-2)).ttl_left("j") == 0)
    check("redis ttl_left: a redis error is 0", RedisSessions(_R(OSError("down"))).ttl_left("j") == 0)


async def graph_checks() -> None:
    print("\nWindow length — through the graph")

    # 1. A plain answer keeps the conversational 60s.
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [{"message": "You have nothing on Thursday."}]
    await _invoke(graph, _upsert("@mary what's on my calendar thursday", mid="p1"))
    left = deps.sessions.ttl_left(OWNER_JID)
    check("[plain] a statement holds the window 60s", 55 <= left <= 60, f"{left}s")

    # 2. A question she needs answered holds it 5 minutes...
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [{"state": "awaiting_answer", "message": "I can only book the next class. Shall I?"}]
    await _invoke(graph, _upsert("@mary schedule my weekly class", mid="q1"))
    left = deps.sessions.ttl_left(OWNER_JID)
    check("[question] awaiting_answer holds the window 5 minutes", 295 <= left <= 300, f"{left}s")
    #    ...a contact's chatter she stays silent on does not cut it back to 60s...
    deps.sessions._exp[OWNER_JID] -= 100  # 100s of looking things up later
    stub.script = [{"state": "keep_listening", "message": None}]
    await _invoke(graph, _upsert("haha ok", from_me=False, mid="q2"))
    left = deps.sessions.ttl_left(OWNER_JID)
    check("[question] a silent turn never shortens the window", left >= 195, f"{left}s")
    #    ...and once he answers and she moves on, it is the 60s window again.
    stub.script = [{"state": "keep_listening", "message": "Done — just Thursday's."}]
    await _invoke(graph, _upsert("yes, just the next one", mid="q3"))
    left = deps.sessions.ttl_left(OWNER_JID)
    check("[question] her next statement brings back the 60s window", 55 <= left <= 60, f"{left}s")

    # 3. A confirmation card is a wait on his yes, whatever state the model wrote.
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [{"state": "awaiting_answer", "message": None, "actions": [dict(CREATE)]}]
    st = await _invoke(graph, _upsert("@mary schedule a call at 3pm", mid="c1"))
    left = deps.sessions.ttl_left(OWNER_JID)
    check("[card] the card went out and the write is held",
          "Confirming" in evo.sent[-1][1] and bool(st.get("pending_action")))
    check("[card] a confirmation card holds the window 5 minutes", 295 <= left <= 300, f"{left}s")
    #    His yes 3 minutes later still lands — the old 60s window had closed by then.
    deps.sessions._exp[OWNER_JID] -= 180
    check("[card] the window is still open 3 minutes on", deps.sessions.is_open(OWNER_JID))
    cal.responses["create"] = {"ok": True, "summary": "created", "data": {
        "title": "Call", "start": "2026-11-05T15:00:00-03:00", "html_link": "http://cal/1"}}
    st = await _invoke(graph, _upsert("sim", mid="c2"))
    left = deps.sessions.ttl_left(OWNER_JID)
    check("[card] the yes ran the write", cal.n("create") == 1 and st.get("pending_action") is None)
    # The yes path never reaches `reason`, so without the per-turn reset it would act on the
    # card turn's "awaiting_answer" and hold 5 minutes after the booking is done.
    check("[card] once booked, the stale awaiting_answer does not linger: back to 60s",
          55 <= left <= 60, f"{left}s")

    # 4. Gathering details: an open question in the workflow is a wait, read off the state.
    deps, evo, stub, cal, graph = make_toolenv()
    stub.script = [{"message": "What's Zen's email?", "workflow": {
        "task": "calendar.create", "known_inputs": [{"field": "who", "value": "Zen"}],
        "open_questions": ["Zen's email"]}}]
    await _invoke(graph, _upsert("@mary schedule lunch with Zen friday", mid="w1"))
    left = deps.sessions.ttl_left(OWNER_JID)
    check("[gather] an open question holds the window 5 minutes", 295 <= left <= 300, f"{left}s")

    # 5. Close still closes, waiting or not.
    stub.script = [{"state": "close", "message": "Ok, leaving it."}]
    await _invoke(graph, _upsert("never mind", mid="w2"))
    check("[close] close drops the window", not deps.sessions.is_open(OWNER_JID))


def _finish() -> None:
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    unit_checks()
    asyncio.run(graph_checks())
    _finish()
