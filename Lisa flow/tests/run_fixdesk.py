"""The fix loop's desk: a judged mistake becomes a case, a plan becomes three paragraphs in his
chat with himself, and "fix it" there approves it.

Layer A needs nothing: the wording, how his answer is read, and the desk itself against a queue
kept in memory and a WhatsApp that only records what it was asked to send.

Layer B runs the real queue (`app/fixdesk/store.py`) and the worker's command line
(`app/fixdesk/cli.py`) against a real Postgres, when one is given:

    FIXDESK_TEST_DSN=postgresql://postgres:x@localhost:55432/postgres python tests/run_fixdesk.py

It works in a schema of its own (`fixdesk_test`), which it creates and drops. Without the
variable Layer B is skipped and says so.

    python tests/run_fixdesk.py
Exits non-zero on any failed check. Every chat id and name here is made up.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.fixdesk import text as T  # noqa: E402
from app.fixdesk.desk import FixDesk  # noqa: E402
from app.fixdesk.store import FixStore  # noqa: E402
from app.review import Reviewer  # noqa: E402

_checks = {"pass": 0, "fail": 0}
OWNER_JID = "5511900000001@s.whatsapp.net"
FRIEND_JID = "5511900000002@s.whatsapp.net"
TZ = ZoneInfo("America/Sao_Paulo")
PLAN = {
    "kind": "bug",
    "issue": "Tuesday 14:02, chat with a supplier: he asked for 15h to 16h30 and the card showed one hour.",
    "diagnosis": "The end time was in a voice note. The calendar step falls back to the default length "
                 "when the typed text has no end time.",
    "fix": "Read the end time from every message of the request before using the default.",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def settings(**over) -> Settings:
    base = dict(anthropic_api_key="x", owner_jid=OWNER_JID, owner_name="Marcelo",
                fix_loop_enabled=True, calendar_timezone="America/Sao_Paulo")
    base.update(over)
    return Settings(**base)


# ── Layer A: wording and reading ─────────────────────────────────────────────
def text_checks() -> None:
    print("how his answer is read")
    c = T.parse_command
    check("fix it", c("fix it") == T.Command("fix", None, ""))
    check("Fix it. (capital, full stop)", c("Fix it.") == T.Command("fix", None, ""))
    check("fix it F12", c("fix it F12") == T.Command("fix", 12, ""))
    check("fix it (F12)", c("fix it (F12)") == T.Command("fix", 12, ""))
    check("fix it with a note",
          c("fix it, but keep the 5 minute limit") == T.Command("fix", None, "but keep the 5 minute limit"))
    check("fix it F7 with a note", c("Fix it F7: only for groups") == T.Command("fix", 7, "only for groups"))
    check("a note that opens with an F-word is a note", c("fix it Friday") == T.Command("fix", None, "Friday"))
    check("skip", c("skip") == T.Command("skip", None, ""))
    check("skip F3", c("Skip F3.") == T.Command("skip", 3, ""))
    check("'skip the gym tomorrow' is a note to himself", c("skip the gym tomorrow") is None)
    check("'please fix it' is not an answer", c("please fix it") is None)
    check("'fixit' is not an answer", c("fixit") is None)
    check("nothing is not an answer", c("") is None and c(None) is None)

    print("\nthe plan message")
    body = T.proposal_body(12, PLAN)
    paras = body.split("\n\n")
    check("three paragraphs", len(paras) == 3, body)
    check("issue, diagnosis, fix, in that order",
          paras[0].startswith("*Issue.* ") and paras[1].startswith("*Diagnosis.* ")
          and paras[2].startswith("*Fix.* "))
    check("it says how to answer and ends with the case tag",
          paras[2].endswith("Reply *fix it* to go ahead. (F12)"), paras[2])
    long = T.proposal_body(1, {"issue": "word " * 200, "diagnosis": "a\n\nb\n c", "fix": "`code` **bold**"})
    lp = long.split("\n\n")
    check("a long paragraph is cut to the limit", len(lp) == 3 and len(lp[0]) <= T.PARA_MAX + 12, str(len(lp[0])))
    check("line breaks inside a paragraph are flattened", lp[1] == "*Diagnosis.* a b c", lp[1])
    check("backticks and double stars are dropped", "`" not in lp[2] and "**" not in lp[2], lp[2])
    check("a cut at a sentence end keeps the sentence",
          T.clean("One. " * 100, 100).endswith("One."), T.clean("One. " * 100, 100))

    print("\nthe other messages")
    check("ack names the run time and the case",
          T.ack_body(4, run_at="08:00") ==
          "On it. The next daily run starts at 08:00; I will tell you how it went. (F4)")
    check("ack carries the Mac note",
          "Your Mac last checked in 3 days ago. (F4)" in
          T.ack_body(4, run_at="08:00", worker_note="Your Mac last checked in 3 days ago."))
    ready = T.result_body(9, {"status": "ready", "branch": "fix/case-9", "commit": "abcdef1234",
                              "summary": "2 files changed, 1 test added.",
                              "checks": "Offline tests pass; live_dates 5/5."})
    check("ready says branch, short commit, not deployed",
          ready == "Fixed and tested, not deployed yet: branch fix/case-9, commit abcdef1. "
                   "2 files changed, 1 test added. Offline tests pass; live_dates 5/5. (F9)", ready)
    check("shipped says live", T.result_body(9, {"status": "shipped", "summary": "Done."}) == "Live. Done. (F9)")
    failed = T.result_body(9, {"status": "failed", "failed_step": "the live checks",
                               "detail": "live_dates 3/5 twice."})
    check("failed says which step and that nothing was deployed",
          failed == "Not fixed: the live checks failed. live_dates 3/5 twice. Nothing was deployed. (F9)", failed)

    print("\nquiet hours (22 to 8)")
    at = lambda h: datetime(2026, 11, 28, h, 30, tzinfo=TZ)  # noqa: E731
    check("21:30 is not quiet", not T.in_quiet_hours(at(21), 22, 8))
    check("22:30 is quiet", T.in_quiet_hours(at(22), 22, 8))
    check("03:30 is quiet", T.in_quiet_hours(at(3), 22, 8))
    check("07:30 is quiet", T.in_quiet_hours(at(7), 22, 8))
    check("08:30 is not quiet", not T.in_quiet_hours(at(8), 22, 8))
    check("equal hours mean never quiet", not T.in_quiet_hours(at(3), 0, 0))

    print("\nwhat she leaves out of her reading of his self-chat")
    hdr = "*[Marcelo's AI Assistant]:*\n\n"
    line = lambda t, me=True: T.is_fixdesk_line(t, from_me=me, owner_name="Marcelo")  # noqa: E731
    check("her plan", line(hdr + T.proposal_body(12, PLAN)))
    check("her ack", line(hdr + T.ack_body(12, run_at="08:00")))
    check("her result", line(hdr + T.result_body(12, {"status": "shipped", "summary": "Done."})))
    check("his 'fix it'", line("fix it"))
    check("his 'skip F2'", line("skip F2"))
    check("her ordinary reply stays", not line(hdr + "Marquei para quinta às 15h."))
    check("his ordinary note stays", not line("comprar leite (F12)"))
    check("someone else's 'fix it' stays", not line("fix it", me=False))


# ── Layer A: the desk, against a queue in memory ──────────────────────────────
class MemStore:
    """The queue's behaviour without Postgres: just enough of FixStore for the desk."""

    def __init__(self) -> None:
        self.cases: dict[int, dict] = {}
        self.seen = None
        self.qualifying: dict[str, list[str]] = {}
        self.today = 0

    def add(self, status: str, **kw) -> int:
        cid = len(self.cases) + 1
        self.cases[cid] = {"id": cid, "status": status, "plan": dict(PLAN), "result": None,
                           "proposal_wa_id": None, "result_wa_id": None, "approved_at": None,
                           "proposed_at": None, "result_sent_at": None, **kw}
        return cid

    async def qualifying_codes(self, loop_id, jv):
        return self.qualifying.get(loop_id, [])

    async def open_case(self, loop_id, jv, codes):
        if any(c.get("loop_id") == loop_id for c in self.cases.values()):
            return None
        return self.add("new", loop_id=loop_id, codes=codes, plan=None)

    async def due_proposals(self, limit):
        rows = [c for c in self.cases.values() if c["status"] == "planned"]
        return rows[:max(0, limit)]

    async def proposed_today(self, tz):
        return self.today

    async def mark_proposed(self, cid, wa_id):
        self.cases[cid].update(status="proposed", proposal_wa_id=wa_id, proposed_at=datetime.now(timezone.utc))
        self.today += 1

    async def due_results(self):
        return [c for c in self.cases.values()
                if c["status"] in ("ready", "shipped", "failed") and not c["result_sent_at"] and c["approved_at"]]

    async def mark_result_sent(self, cid, wa_id):
        self.cases[cid].update(result_sent_at=datetime.now(timezone.utc), result_wa_id=wa_id)

    async def expire(self, days):
        return 0

    async def find_for_reply(self, quoted_id, case_id):
        if quoted_id:
            for c in self.cases.values():
                if quoted_id in (c["proposal_wa_id"], c["result_wa_id"]):
                    return c
        if case_id is not None:
            return self.cases.get(case_id)
        waiting = [c for c in self.cases.values() if c["status"] == "proposed"]
        return max(waiting, key=lambda c: c["proposed_at"]) if waiting else None

    async def approve(self, cid, note, wa_id):
        c = self.cases[cid]
        if c["status"] != "proposed":
            return False
        c.update(status="approved", approval_note=note or None, approved_at=datetime.now(timezone.utc))
        return True

    async def dismiss(self, cid):
        c = self.cases[cid]
        if c["status"] != "proposed":
            return False
        c["status"] = "dismissed"
        return True

    async def worker_seen(self):
        return self.seen


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def send_text(self, number, text, *, quoted=None):
        if self.fail:
            return None
        self.sent.append((number, text))
        return f"WA{len(self.sent)}"


def answer(text: str, *, jid: str = OWNER_JID, from_me: bool = True, msg_id: str = "M1",
           quoted: str | None = None, alt: str | None = None) -> dict:
    data: dict = {"key": {"remoteJid": jid, "fromMe": from_me, "id": msg_id},
                  "message": {"conversation": text}}
    if alt:
        data["key"]["remoteJidAlt"] = alt
    if quoted:
        data["contextInfo"] = {"stanzaId": quoted, "quotedMessage": {"conversation": "…"}}
    return {"event": "messages.upsert", "data": data}


async def desk_checks() -> None:
    noon = lambda: datetime(2026, 11, 28, 12, 0, tzinfo=TZ)  # noqa: E731
    night = lambda: datetime(2026, 11, 28, 23, 0, tzinfo=TZ)  # noqa: E731

    print("\na graded session opens a case")
    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(), clock=noon)
    store.qualifying = {"loop-a": ["wrong_details"], "loop-b": []}
    await desk.note_graded("loop-a", "v5")
    await desk.note_graded("loop-b", "v5")
    await desk.note_graded("loop-a", "v5")
    check("one case for the session with a finding, none for the clean one, none twice",
          [c["loop_id"] for c in store.cases.values()] == ["loop-a"], str(store.cases))

    class Boom(MemStore):
        async def qualifying_codes(self, loop_id, jv):
            raise RuntimeError("db down")
    await FixDesk(Boom(), wa, settings(), clock=noon).note_graded("loop-x", "v5")
    check("a failing queue does not raise into the reviewer", True)

    print("\nthe reviewer calls the desk once a session is graded, live only")

    class OneTurnStore:
        async def load_turns(self, loop_id):
            return [{"loop_id": loop_id, "seq": 3, "chat_id": "c", "prompt_version": "p", "domain": None,
                     "ts": datetime.now(timezone.utc), "reply_text": "ok", "silent": False,
                     "reply_ts": 10.0, "last_human_ts": 5, "last_human_id": "h", "overtaken": False,
                     "audio_id": None, "model_ms": 1, "delivery": "ok", "turn_error": "none",
                     "error": False, "lines": []}]

        async def audio_seconds(self, ids):
            return {}

        async def write_review(self, row, gaps):
            pass

        async def mark_done(self, loop_id, **kw):
            pass

    class StubJudge:
        async def judge(self, **kw):
            return {"verdict": "bad", "confidence": "high", "rationale": "", "task_class": "ack",
                    "gaps": [], "proposed_gap": "", "error": None, "judge_model": "stub"}

    called: list[tuple] = []

    async def on_graded(loop_id, jv):
        called.append((loop_id, jv))

    rv = Reviewer(settings(), OneTurnStore(), judge=StubJudge())
    await rv.review_loop("loop-z")
    check("with nobody listening (the backfill), nothing is called", called == [])
    rv.on_graded = on_graded
    await rv.review_loop("loop-z")
    check("the listener gets the loop and the rubric version", called == [("loop-z", rv.s.review_judge_version)],
          str(called))

    async def bad_listener(loop_id, jv):
        raise RuntimeError("boom")
    rv.on_graded = bad_listener
    out = await rv.review_loop("loop-z")
    check("a listener that raises does not break the review", out.get("turns") == 1, str(out))

    print("\nsending plans")
    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(), clock=noon)
    a = store.add("planned")
    done = await desk.tick()
    check("a planned case is sent to his own number", done["proposed"] == 1 and wa.sent[0][0] == "5511900000001",
          str(wa.sent))
    check("under her header, with the three paragraphs",
          wa.sent[0][1] == "*[Marcelo's AI Assistant]:*\n\n" + T.proposal_body(a, PLAN), wa.sent[0][1])
    check("the case is now proposed and remembers the message", store.cases[a]["status"] == "proposed"
          and store.cases[a]["proposal_wa_id"] == "WA1")
    await desk.tick()
    check("it is not sent twice", len(wa.sent) == 1)

    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(), clock=night)
    store.add("planned")
    await desk.tick()
    check("nothing is sent at 23:00", wa.sent == [])
    desk._clock = noon
    await desk.tick()
    check("... and it goes out once the quiet hours are over", len(wa.sent) == 1)

    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(fix_daily_cap=2), clock=noon)
    for _ in range(4):
        store.add("planned")
    await desk.tick()
    await desk.tick()
    check("the daily cap holds across passes", len(wa.sent) == 2, str(len(wa.sent)))

    store, wa = MemStore(), FakeWhatsApp()
    wa.fail = True
    desk = FixDesk(store, wa, settings(), clock=noon)
    b = store.add("planned")
    await desk.tick()
    check("a failed send leaves the case planned", store.cases[b]["status"] == "planned")

    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(owner_jid=""), clock=noon)
    store.add("planned")
    await desk.tick()
    check("with his own number unknown, nothing is sent", wa.sent == [])

    print("\nhis answer")
    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(), clock=noon)
    store.seen = {"host": "mac", "seen_at": datetime.now(timezone.utc), "info": {}}
    c1 = store.add("planned")
    await desk.tick()
    check("'fix it' from someone else is ignored",
          await desk.handle_reply(answer("fix it", from_me=False, jid=FRIEND_JID)) == "not_owner")
    check("his 'fix it' in another chat is ignored",
          await desk.handle_reply(answer("fix it", jid=FRIEND_JID)) == "not_self_chat")
    check("a message that is not an answer is ignored",
          await desk.handle_reply(answer("fixing the car today")) == "not_an_answer")
    check("her own message is ignored",
          await desk.handle_reply(answer("*[Marcelo's AI Assistant]:*\n\nfix it")) == "own_message")
    n = len(wa.sent)
    check("none of those sent anything or moved the case",
          len(wa.sent) == n and store.cases[c1]["status"] == "proposed")
    check("his 'fix it' approves the newest plan", await desk.handle_reply(answer("fix it", msg_id="M2")) == "approved")
    check("the case is approved", store.cases[c1]["status"] == "approved")
    check("she answers 'On it' with the run time and the tag",
          wa.sent[-1][1].endswith("On it. The next daily run starts at 08:00; I will tell you how it went. (F1)"),
          wa.sent[-1][1])
    check("the same message delivered twice is handled once",
          await desk.handle_reply(answer("fix it", msg_id="M2")) == "seen")
    check("a second 'fix it' says it is already queued",
          await desk.handle_reply(answer("fix it F1", msg_id="M3")) == "already"
          and "Already queued" in wa.sent[-1][1])

    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(), clock=noon)
    old, new = store.add("planned"), store.add("planned")
    await desk.tick()
    store.cases[old]["proposed_at"] -= timedelta(hours=3)
    await desk.handle_reply(answer("fix it, only in groups", msg_id="A", quoted=store.cases[old]["proposal_wa_id"]))
    check("a reply to an older plan approves that plan, not the newest",
          store.cases[old]["status"] == "approved" and store.cases[new]["status"] == "proposed")
    check("... and keeps his note for the coding agent", store.cases[old]["approval_note"] == "only in groups")
    check("no Mac heard from yet: she says so",
          "Your Mac has not checked in yet" in wa.sent[-1][1], wa.sent[-1][1])
    check("'skip' closes the plan that is waiting",
          await desk.handle_reply(answer("skip", msg_id="B")) == "dismissed"
          and store.cases[new]["status"] == "dismissed" and wa.sent[-1][1].endswith("Skipped. (F2)"))
    check("'fix it' with nothing waiting says nothing",
          await desk.handle_reply(answer("fix it", msg_id="C")) == "no_case")
    check("his answer arriving under the @lid twin of his chat still counts",
          await desk.handle_reply(answer("skip F1", msg_id="D", jid="99887766554433@lid", alt=OWNER_JID))
          == "skip_ignored")

    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(), clock=noon)
    store.seen = {"host": "mac", "seen_at": datetime.now(timezone.utc) - timedelta(days=3), "info": {}}
    store.add("planned")
    await desk.tick()
    await desk.handle_reply(answer("fix it", msg_id="E"))
    check("a Mac silent for three days is mentioned", "Your Mac last checked in 3 days ago." in wa.sent[-1][1],
          wa.sent[-1][1])

    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(), clock=noon)
    store.seen = {"host": "mac", "seen_at": datetime.now(timezone.utc),
                  "info": {"ok": False, "why": "Claude Code is not signed in on this Mac"}}
    store.add("planned")
    await desk.tick()
    await desk.handle_reply(answer("fix it", msg_id="F"))
    check("a Mac that checked in but cannot run Claude Code is mentioned, with the reason",
          "It waits, though: Claude Code is not signed in on this Mac. (F1)" in wa.sent[-1][1],
          wa.sent[-1][1])

    print("\nsending results")
    store, wa = MemStore(), FakeWhatsApp()
    desk = FixDesk(store, wa, settings(), clock=noon)
    r = store.add("ready", approved_at=datetime.now(timezone.utc),
                  result={"status": "ready", "branch": "fix/case-1", "commit": "abcdef123",
                          "summary": "1 file changed.", "checks": "Offline tests pass."})
    store.add("failed", approved_at=None, result={"status": "failed"})
    await desk.tick()
    check("a finished case is reported once, the never-approved one is not",
          len(wa.sent) == 1 and "branch fix/case-1, commit abcdef1" in wa.sent[0][1], str(wa.sent))
    await desk.tick()
    check("... and only once", len(wa.sent) == 1 and store.cases[r]["result_sent_at"] is not None)


# ── Layer B: the real queue, against a real Postgres ────────────────────────────
SCHEMA = "fixdesk_test"


async def store_checks(dsn: str) -> None:
    import psycopg

    from app.review.store import ReviewStore

    print("\nthe real queue (Postgres)")
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {SCHEMA}")
        await conn.execute(f"""
            CREATE TABLE {SCHEMA}.loops (loop_id text PRIMARY KEY, chat_id text, started_at timestamptz,
                updated_at timestamptz, prompt_version text, model text, lang text, trigger text);
            CREATE TABLE {SCHEMA}.events (id bigserial PRIMARY KEY, loop_id text, trace_id text, seq int,
                ts timestamptz DEFAULT now(), stream text, label text, who text, wa_message_id text,
                text text, payload jsonb);""")
    rstore = ReviewStore(dsn, schema=SCHEMA)
    await rstore.open()
    store = FixStore(dsn, schema=SCHEMA)
    await store.open()
    await store.open()  # the DDL runs at every boot
    try:
        now = datetime.now(timezone.utc)

        async def session(loop_id: str, gaps: list[dict]) -> None:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                await conn.execute(f"INSERT INTO {SCHEMA}.loops (loop_id, chat_id, started_at, prompt_version) "
                                   "VALUES (%s, %s, now(), 'p1')", (loop_id, "chat-1"))
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.events (loop_id, seq, stream, label, who, text) VALUES "
                    "(%s, 1, 'transcript', 'Marcelo', 'Marcelo', '@lisa marca amanhã 15h'), "
                    "(%s, 2, 'transcript', 'AI Assistant', 'AI Assistant', 'Marquei.')", (loop_id, loop_id))
            await rstore.write_review(
                {"loop_id": loop_id, "seq": 3, "chat_id": "chat-1", "ts": now, "judge_version": "v5",
                 "judge_model": "m", "turn_kind": "reply", "domain": "calendar", "prompt_version": "p1",
                 "verdict": "bad", "confidence": "medium", "rationale": "r", "proposed_gap": "",
                 "reply_text": "Marquei.", "error": None, "task_class": "calendar_write",
                 "wait_seconds": 4, "last_human_id": "h", "overtaken": False, "model_ms": 1,
                 "timing_band": "good", "delivery": "ok", "turn_error": "none"}, gaps)

        judge_gap = {"code": "wrong_details", "severity": "major", "evidence": "e", "harm": "h", "source": "judge"}
        await session("L1", [judge_gap])
        await session("L2", [{"code": "slow_reply", "severity": "minor", "evidence": "9s", "harm": "h", "source": "timing"}])
        await session("L3", [{"code": "delivery_failed", "severity": "major", "evidence": "x", "harm": "h", "source": "timing"}])
        await session("L4", [judge_gap])
        await session("L5", [])
        check("a judge finding qualifies", await store.qualifying_codes("L1", "v5") == ["wrong_details"])
        check("a slow reply does not", await store.qualifying_codes("L2", "v5") == [])
        check("a failed delivery does", await store.qualifying_codes("L3", "v5") == ["delivery_failed"])
        check("a clean session does not", await store.qualifying_codes("L5", "v5") == [])
        check("another rubric version sees nothing", await store.qualifying_codes("L1", "v9") == [])

        c1 = await store.open_case("L1", "v5", ["wrong_details"])
        check("a case is opened", isinstance(c1, int))
        check("the same session never opens a second, even under a new rubric",
              await store.open_case("L1", "v6", ["wrong_details"]) is None)
        c4 = await store.open_case("L4", "v5", ["wrong_details"])

        pulled = await store.pull()
        check("pull lists the new cases", [c["id"] for c in pulled["new"]] == [c1, c4] and pulled["approved"] == [])
        ev = pulled["new"][0]["evidence"]
        check("... each with its session, reviews, findings and transcript",
              ev["loop"]["prompt_version"] == "p1" and len(ev["reviews"]) == 1
              and ev["findings"][0]["code"] == "wrong_details" and [t["seq"] for t in ev["transcript"]] == [1, 2],
              json.dumps(ev, default=str)[:300])
        check("pull's answer is JSON once dates are strings", bool(json.dumps(pulled, default=str)))

        check("a case cannot be fixed before it is approved", await store.claim(c1, "fixing") is None)
        check("the worker claims it to investigate", (await store.claim(c1, "investigating"))["status"] == "investigating")
        check("... and only once", await store.claim(c1, "investigating") is None)
        check("a plan of no known kind is refused", await store.save_plan(c1, {"kind": "poem"}, "s") is None)
        check("a bug plan makes it planned", await store.save_plan(c1, dict(PLAN), "sess-1") == "planned")
        due = await store.due_proposals(5)
        check("the desk finds it due, with the plan", [d["id"] for d in due] == [c1] and due[0]["plan"]["kind"] == "bug")
        check("no room left today means nothing due", await store.due_proposals(0) == [])
        check("nothing proposed yet today", await store.proposed_today("America/Sao_Paulo") == 0)
        await store.mark_proposed(c1, "WA-PLAN-1")
        check("one proposed today", await store.proposed_today("America/Sao_Paulo") == 1)

        await store.claim(c4, "investigating")
        check("a repeat closes as duplicate",
              await store.save_plan(c4, {"kind": "duplicate", "duplicate_of": c1}, "sess-4") == "duplicate")
        row = await store.find_for_reply(None, c1)
        check("... and the case it repeats counts two sessions", row["seen"] == 2, str(row["seen"]))
        check("the repeat is not due to be sent", await store.due_proposals(5) == [])

        check("his reply to the plan finds the case", (await store.find_for_reply("WA-PLAN-1", None))["id"] == c1)
        check("with no reply-to and no tag, the newest waiting plan", (await store.find_for_reply(None, None))["id"] == c1)
        check("an unknown reply-to falls back the same way", (await store.find_for_reply("NOPE", None))["id"] == c1)
        check("approve", await store.approve(c1, "only in groups", "WA-HIS-1"))
        check("... not twice", not await store.approve(c1, "", "WA-HIS-2"))
        check("nothing is waiting any more", await store.find_for_reply(None, None) is None)
        pulled = await store.pull()
        check("pull now lists it as approved, with the note and the session to resume",
              [(c["id"], c["approval_note"], c["session_id"]) for c in pulled["approved"]] == [(c1, "only in groups", "sess-1")])
        check("... and among the recent cases to compare against",
              [r["id"] for r in pulled["recent"]] == [c1] and pulled["recent"][0]["issue"] == PLAN["issue"])

        await store.claim(c1, "fixing")
        check("a run that dies puts it back as approved", await store.release(c1, "killed") == "approved")
        await store.claim(c1, "fixing")
        await store.release(c1, "killed")
        await store.claim(c1, "fixing")
        check("the third death fails it out loud", await store.release(c1, "killed again") == "failed")
        due = await store.due_results()
        check("... as a result to tell him about",
              [d["id"] for d in due] == [c1] and due[0]["result"]["failed_step"] == "the run itself")
        await store.mark_result_sent(c1, "WA-RES-1")
        check("a result is sent once", await store.due_results() == [])

        c3 = await store.open_case("L3", "v5", ["delivery_failed"])
        await store.claim(c3, "investigating")
        await store.save_plan(c3, dict(PLAN), "sess-3")
        await store.mark_proposed(c3, "WA-PLAN-3")
        await store.approve(c3, "", None)
        await store.claim(c3, "fixing")
        check("an unknown result state is refused", await store.save_result(c3, {"status": "maybe"}) is None)
        check("a ready result is saved",
              await store.save_result(c3, {"status": "ready", "branch": "fix/case-3", "commit": "abc"}) == "ready")
        check("... and is due to be reported", [d["id"] for d in await store.due_results()] == [c3])

        c2 = await store.open_case("L2", "v5", ["x"])
        await store.claim(c2, "investigating")
        await store.save_plan(c2, dict(PLAN), "s")
        await store.mark_proposed(c2, "WA-PLAN-2")
        check("a fresh plan does not expire", await store.expire(14) == 0)
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(f"UPDATE {SCHEMA}.fix_cases SET proposed_at = now() - interval '15 days' WHERE id = %s", (c2,))
        check("one unanswered for 15 days does", await store.expire(14) == 1)
        check("dismiss needs a waiting plan", not await store.dismiss(c2))

        check("no worker seen yet", await store.worker_seen() is None)
        await store.heartbeat("mac-test", {"cli": "2.1.330"})
        await store.heartbeat("mac-test", {"cli": "2.1.331"})
        seen = await store.worker_seen()
        check("a heartbeat is kept, one row per host", seen["host"] == "mac-test" and seen["info"]["cli"] == "2.1.331")

        print("\nthe worker's command line (Postgres)")
        env = {**os.environ, "DATABASE_URL": dsn, "LOG_SCHEMA": SCHEMA, "ANTHROPIC_API_KEY": "x",
               "FIX_LOOP_ENABLED": "true"}
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        def cli(*args: str, stdin: dict | None = None) -> tuple[int, dict]:
            p = subprocess.run([sys.executable, "-m", "app.fixdesk.cli", *args], cwd=root, env=env,
                               input=json.dumps(stdin) if stdin is not None else "",
                               capture_output=True, text=True, timeout=60)
            last = (p.stdout.strip().splitlines() or ["{}"])[-1]
            return p.returncode, json.loads(last)

        c5 = await store.open_case("L5", "v5", ["x"])
        code, out = cli("pull", "mac-cli", stdin={"cli": "test"})
        check("pull answers with the queue and says the loop is on",
              code == 0 and out["enabled"] is True and [c["id"] for c in out["new"]] == [c5], str(out)[:200])
        check("claim", cli("claim", str(c5), "investigating") == (0, {"ok": True}))
        check("a second claim fails with exit 1", cli("claim", str(c5), "investigating") == (1, {"ok": False}))
        code, out = cli("plan", str(c5), stdin={"plan": dict(PLAN), "session_id": "sess-5"})
        check("plan", code == 0 and out["status"] == "planned", str(out))
        await store.mark_proposed(c5, "WA-PLAN-5")
        await store.approve(c5, "", None)
        check("claim to fix", cli("claim", str(c5), "fixing")[0] == 0)
        code, out = cli("release", str(c5), stdin={"error": "laptop closed"})
        check("release puts it back", code == 0 and out["status"] == "approved", str(out))
        cli("claim", str(c5), "fixing")
        code, out = cli("result", str(c5), stdin={"status": "ready", "branch": "fix/case-5", "commit": "abc1234"})
        check("result", code == 0 and out["status"] == "ready", str(out))
        check("an unknown command fails", cli("dance", "1")[0] == 1)
    finally:
        await store.aclose()
        await rstore.aclose()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


async def main() -> int:
    text_checks()
    await desk_checks()
    dsn = os.environ.get("FIXDESK_TEST_DSN")
    if dsn:
        await store_checks(dsn)
    else:
        print("\nthe real queue (Postgres): SKIPPED, set FIXDESK_TEST_DSN to run it")
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
