"""Voice-note capture for history search — VoiceCapture (app/history/voice.py).

    cd "Lisa flow" && python tests/run_history_voice.py
    HISTORY_VOICE_TEST_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/<db> python tests/run_history_voice.py

Without a database: a fake repository that applies the query's rules (window, transcripts, final
rows, backoff) and a fake transcription service that scripts what each note returns, with an
injected clock. Covers ordering (newest first, a fresh note never behind the backfill), the
minimum age and the window, notes already transcribed or settled, expiry, retries and backoff,
long and empty records, the daily cap, the concurrency bound, auth pauses, the outage breaker,
the per-pass log line, clean shutdown, and that nothing raises into the app.

With HISTORY_VOICE_TEST_DSN: the real SQL against a throwaway Postgres. The test builds its own
public."Message" fixture table there (and its own vt_log / vt_search schemas), and refuses a
database whose "Message" table it did not create. Every id and jid is made up.

Exits non-zero when a check fails."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.history import voice as voice_mod  # noqa: E402
from app.history.voice import (  # noqa: E402
    DAY, PgVoiceRepo, VoiceCapture, VoiceNote, backoff, candidates_sql, ddl, record_sql,
)

_checks = {"pass": 0, "fail": 0}

CONTACT = "5511999990000@s.whatsapp.net"
LID = "111111111111111@lid"
GROUP = "120363000000000000@g.us"
OK = {"text": "made-up words", "duration_sec": 10.0, "language": "pt", "error": None}
EMPTY = {"text": "", "duration_sec": 3.0, "language": None, "error": "empty"}


def err(kind: str) -> dict:
    return {"text": "", "error": kind}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


# -- fakes ------------------------------------------------------------------------------------

class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeRepo:
    """The query's rules over dicts. Returns the right notes in the WRONG order (oldest first),
    so the loop is checked for sorting them itself."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.notes: dict[str, dict] = {}
        self.transcribed: set[str] = set()
        self.rows: dict[str, dict] = {}
        self.queries: list[tuple] = []
        self.opened = self.closed = False
        self.raise_candidates = 0
        self.raise_record = 0

    def add(self, msg_id: str, age: float, *, seconds=10, has_media=True, chat=CONTACT) -> None:
        self.notes[msg_id] = {"chat": chat, "ts": int(self.clock() - age), "seconds": seconds,
                              "has_media": has_media}

    def row(self, msg_id: str, status: str, *, attempts=1, next_at=None, seconds=None,
            updated_at=None, ts=None) -> None:
        n = self.notes.get(msg_id, {})
        self.rows[msg_id] = {"status": status, "attempts": attempts, "last_error": None,
                             "next_at": next_at, "seconds": seconds if seconds is not None
                             else n.get("seconds"), "ts": ts if ts is not None else n.get("ts"),
                             "updated_at": updated_at if updated_at is not None else self.clock()}

    async def open(self) -> None:
        self.opened = True

    async def aclose(self) -> None:
        self.closed = True

    async def candidates(self, *, since, until, limit):
        self.queries.append((since, until, limit))
        if self.raise_candidates:
            self.raise_candidates -= 1
            raise RuntimeError("database unreachable")
        now = self.clock()
        due = []
        for mid, n in self.notes.items():
            if not since <= n["ts"] <= until or mid in self.transcribed:
                continue
            r = self.rows.get(mid)
            if r and not (r["status"] == "pending" and (r["next_at"] is None or r["next_at"] <= now)):
                continue
            due.append(VoiceNote(mid, n["chat"], n["ts"], n["seconds"], n["has_media"],
                                 r["attempts"] if r else 0))
        due.sort(key=lambda x: x.ts, reverse=True)
        return list(reversed(due[:limit]))

    async def record(self, note, *, status, attempts, last_error, next_at, seconds):
        if self.raise_record:
            self.raise_record -= 1
            raise RuntimeError("write failed")
        self.rows[note.msg_id] = {"status": status, "attempts": attempts, "last_error": last_error,
                                  "next_at": next_at, "seconds": seconds, "ts": note.ts,
                                  "updated_at": self.clock()}

    async def minutes_since(self, since):
        return sum((r["seconds"] or 0) for r in self.rows.values()
                   if r["status"] == "done" and r["updated_at"] >= since) / 60.0

    async def age_out(self, *, before):
        n = 0
        for r in self.rows.values():
            if r["status"] == "pending" and r["ts"] < before:
                r["status"] = "expired"
                n += 1
        return n


class FakeTranscription:
    """TranscriptionService.get(). A scripted list per id (the last entry repeats); success
    'writes' the transcript the way the real service does."""

    def __init__(self, repo) -> None:
        self.repo = repo
        self.script: dict[str, list] = {}
        self.calls: list[str] = []
        self.inflight = 0
        self.peak = 0
        self.delay = 0.0
        self.gate: asyncio.Event | None = None
        self.on_call = None
        self.raise_for: set[str] = set()

    def will(self, msg_id: str, *results) -> None:
        self.script[msg_id] = list(results)

    async def get(self, wa_id, *, language=None):
        self.calls.append(wa_id)
        if self.on_call:
            self.on_call(wa_id)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if wa_id in self.raise_for:
                raise RuntimeError("boom")
            seq = self.script.get(wa_id) or [OK]
            r = dict(seq.pop(0) if len(seq) > 1 else seq[0])
            if r.get("error") in (None, "empty"):
                self.repo.transcribed.add(wa_id)
            return r
        finally:
            self.inflight -= 1


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record) -> None:
        self.lines.append(record.getMessage())

    def voice(self) -> list[dict]:
        out = []
        for line in self.lines:
            if line.startswith('{"voice_capture"'):
                out.append(json.loads(line)["voice_capture"])
        return out

    def summaries(self) -> list[dict]:
        return [v for v in self.voice() if "done" in v]


CAP = Capture()
_vlog = logging.getLogger("mary.history.voice")
_vlog.addHandler(CAP)
_vlog.setLevel(logging.INFO)
_vlog.propagate = False


def settings(**kw) -> Settings:
    base = dict(
        history_voice_interval_seconds=60, history_voice_concurrency=1,
        history_voice_backfill_days=28, history_voice_max_seconds=1800,
        history_voice_min_age_seconds=300, history_voice_expire_days=21.0,
        history_voice_max_attempts=10, history_voice_daily_minutes=240,
        log_schema="lisa_log", history_schema="lisa_search", transcription_enabled=True,
    )
    base.update(kw)
    return Settings(**base)


def make(**kw):
    clock = Clock()
    repo = FakeRepo(clock)
    tr = FakeTranscription(repo)
    vc = VoiceCapture("postgresql://unused", tr, settings(**kw), repo=repo, clock=clock,
                      first_delay=0)
    return vc, repo, tr, clock


async def wait_for(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


# -- 1. order ---------------------------------------------------------------------------------

async def t1_order():
    print("1. newest first; a note that arrives mid-backfill goes next")
    vc, repo, tr, clock = make()
    repo.add("VN_5D", 5 * DAY)
    repo.add("VN_10M", 600)
    repo.add("VN_1D", DAY)
    repo.add("VN_3D", 3 * DAY, chat=GROUP)
    await vc.run_once()
    check("transcribed newest first, though the repo answered oldest first",
          tr.calls == ["VN_10M", "VN_1D", "VN_3D", "VN_5D"], str(tr.calls))

    vc, repo, tr, clock = make(history_voice_concurrency=1)  # batches of 4
    for i in range(9):
        repo.add(f"VN_OLD{i}", (10 + i) * DAY, chat=LID)

    def arrive(wa_id):
        if wa_id == "VN_OLD0":
            repo.add("VN_FRESH", 400)

    tr.on_call = arrive
    counts = await vc.run_once()
    check("the batch is small (4 at concurrency 1)", repo.queries[0][2] == 4, str(repo.queries))
    check("the fresh note went right after the first batch, ahead of the rest of the backfill",
          tr.calls.index("VN_FRESH") == 4, str(tr.calls))
    check("the pass carried on to the end of the backfill", counts["done"] == 10, str(counts))


# -- 2. window --------------------------------------------------------------------------------

async def t2_window():
    print("2. the window: at least the minimum age, at most the backfill")
    vc, repo, tr, clock = make()
    now = clock()
    repo.add("VN_60S", 60)
    repo.add("VN_301S", 301)
    repo.add("VN_27D", 28 * DAY - 60)
    repo.add("VN_29D", 29 * DAY)
    await vc.run_once()
    check("too fresh and too old are left alone", sorted(tr.calls) == ["VN_27D", "VN_301S"],
          str(tr.calls))
    since, until, _ = repo.queries[0]
    check("the query asks for [now - 28 d, now - 300 s]",
          since == int(now - 28 * DAY) and until == int(now - 300), f"{since} {until}")
    clock.advance(300)
    await vc.run_once()
    check("five minutes later the fresh one is due", tr.calls[-1] == "VN_60S", str(tr.calls))


# -- 3. already there -------------------------------------------------------------------------

async def t3_already():
    print("3. notes already transcribed, settled or waiting are not tried")
    vc, repo, tr, clock = make()
    for mid in ("VN_T", "VN_DONE", "VN_EXP", "VN_FAIL", "VN_SKIP", "VN_WAIT", "VN_DUE"):
        repo.add(mid, DAY)
    repo.transcribed.add("VN_T")
    repo.row("VN_DONE", "done")
    repo.row("VN_EXP", "expired")
    repo.row("VN_FAIL", "failed")
    repo.row("VN_SKIP", "skipped")
    repo.row("VN_WAIT", "pending", attempts=2, next_at=clock() + 60)
    repo.row("VN_DUE", "pending", attempts=2, next_at=clock() - 1)
    await vc.run_once()
    check("only the pending note whose backoff is over is tried", tr.calls == ["VN_DUE"],
          str(tr.calls))
    check("its attempts carry on from the row (2 -> 3)", repo.rows["VN_DUE"]["attempts"] == 3,
          str(repo.rows["VN_DUE"]))
    check("and it is done", repo.rows["VN_DUE"]["status"] == "done")


# -- 4. expiry --------------------------------------------------------------------------------

async def t4_expiry():
    print("4. expiry: old notes at once, young ones after their retries")
    vc, repo, tr, clock = make(history_voice_max_attempts=3)
    repo.add("VN_GONE", 22 * DAY)
    repo.add("VN_YOUNG", 2 * DAY)
    tr.will("VN_GONE", err("download"))
    tr.will("VN_YOUNG", err("download"))
    t0 = clock()
    await vc.run_once()
    g, y = repo.rows["VN_GONE"], repo.rows["VN_YOUNG"]
    check("a 22-day-old note whose download fails is expired on the first try",
          g["status"] == "expired" and g["attempts"] == 1 and g["next_at"] is None, str(g))
    check("a 2-day-old one is pending, retried in 10 min",
          y["status"] == "pending" and y["attempts"] == 1 and y["next_at"] == t0 + 600
          and y["last_error"] == "download", str(y))
    clock.advance(601)
    await vc.run_once()
    clock.advance(1201)
    await vc.run_once()
    y = repo.rows["VN_YOUNG"]
    check("after max attempts (3) it is expired too", y["status"] == "expired" and y["attempts"] == 3,
          str(y))
    check("three tries in all", tr.calls.count("VN_YOUNG") == 3)

    vc, repo, tr, clock = make()
    repo.add("VN_EDGE", 21 * DAY - 300)
    tr.will("VN_EDGE", err("download"))
    await vc.run_once()
    check("just under 21 days: pending", repo.rows["VN_EDGE"]["status"] == "pending")
    clock.advance(601)
    await vc.run_once()
    check("past 21 days, the next failure is final", repo.rows["VN_EDGE"]["status"] == "expired",
          str(repo.rows["VN_EDGE"]))

    vc, repo, tr, clock = make()
    for i in range(6):
        repo.add(f"VN_TAIL{i}", (22 + i) * DAY)
        tr.will(f"VN_TAIL{i}", err("download"))
    CAP.lines.clear()
    counts = await vc.run_once()
    check("a tail of old notes all failing is expected: every one tried and expired, no halt",
          counts["expired"] == 6 and len(tr.calls) == 6, str(counts))
    check("and no outage is reported", not any("halted" in v for v in CAP.voice()), str(CAP.voice()))

    vc, repo, tr, clock = make()
    repo.add("VN_LIMBO", 28 * DAY - 3600)
    repo.row("VN_LIMBO", "pending", attempts=4, next_at=clock() + 6 * 3600)
    clock.advance(2 * 3600)
    counts = await vc.run_once()
    check("a pending note that ages out of the window is expired, not left pending forever",
          repo.rows["VN_LIMBO"]["status"] == "expired" and counts["expired"] == 1
          and not tr.calls, str(repo.rows["VN_LIMBO"]))


# -- 5. retries -------------------------------------------------------------------------------

async def t5_retries():
    print("5. transient failures: backoff, then failed")
    check("backoff doubles from 10 min and stops at 12 h",
          [backoff(i) for i in (1, 2, 3, 7, 8, 20)] == [600, 1200, 2400, 38400, 43200, 43200])
    vc, repo, tr, clock = make(history_voice_max_attempts=5)
    repo.add("VN_FLAKY", 3600, seconds=42)
    tr.will("VN_FLAKY", err("provider"), err("timeout"), OK)
    t0 = clock()
    await vc.run_once()
    r = repo.rows["VN_FLAKY"]
    check("provider error: pending, retry at +10 min",
          r["status"] == "pending" and r["next_at"] == t0 + 600 and r["last_error"] == "provider",
          str(r))
    clock.advance(300)
    await vc.run_once()
    check("not retried before its time", tr.calls == ["VN_FLAKY"], str(tr.calls))
    clock.advance(301)
    t1 = clock()
    await vc.run_once()
    r = repo.rows["VN_FLAKY"]
    check("timeout: pending again, the wait doubled (+20 min)",
          r["status"] == "pending" and r["attempts"] == 2 and r["next_at"] == t1 + 1200, str(r))
    clock.advance(1200)
    counts = await vc.run_once()
    r = repo.rows["VN_FLAKY"]
    check("third try succeeds: done, attempts 3, its minutes counted",
          r["status"] == "done" and r["attempts"] == 3 and counts["minutes"] == 0.7, f"{r} {counts}")

    vc, repo, tr, clock = make(history_voice_max_attempts=3)
    repo.add("VN_BROKEN", 3600)
    tr.will("VN_BROKEN", err("timeout"))
    for _ in range(3):
        await vc.run_once()
        clock.advance(13 * 3600)
    r = repo.rows["VN_BROKEN"]
    check("after max attempts a provider failure is final: failed",
          r["status"] == "failed" and r["attempts"] == 3 and r["last_error"] == "timeout", str(r))

    vc, repo, tr, clock = make()
    repo.add("VN_SILENT", 3600, seconds=4)
    tr.will("VN_SILENT", EMPTY)
    await vc.run_once()
    r = repo.rows["VN_SILENT"]
    check("a silent note is done (the service stored the empty transcript)",
          r["status"] == "done" and r["last_error"] == "empty", str(r))


# -- 6. skips ---------------------------------------------------------------------------------

async def t6_skips():
    print("6. long clips and records with no audio are skipped without a call")
    vc, repo, tr, clock = make()
    repo.add("VN_LONG", 3600, seconds=1801)
    repo.add("VN_EXACT", 3600, seconds=1800)
    repo.add("VN_NOAUDIO", 3600, seconds=None, has_media=False)
    counts = await vc.run_once()
    check("over the limit: skipped as too_long",
          repo.rows["VN_LONG"]["status"] == "skipped" and repo.rows["VN_LONG"]["last_error"] == "too_long")
    check("at the limit: transcribed", repo.rows["VN_EXACT"]["status"] == "done")
    check("no audio in the record: skipped as no_media",
          repo.rows["VN_NOAUDIO"]["status"] == "skipped"
          and repo.rows["VN_NOAUDIO"]["last_error"] == "no_media")
    check("only the one in range reached the provider", tr.calls == ["VN_EXACT"], str(tr.calls))
    check("counted", counts["skipped"] == 2 and counts["done"] == 1, str(counts))

    vc, repo, tr, clock = make(history_voice_max_seconds=0)
    repo.add("VN_HUGE", 3600, seconds=5000)
    await vc.run_once()
    check("max_seconds 0 means no limit", tr.calls == ["VN_HUGE"])


# -- 7. daily cap -----------------------------------------------------------------------------

async def t7_cap():
    print("7. the daily cap on transcribed minutes")
    vc, repo, tr, clock = make(history_voice_daily_minutes=10)
    repo.add("VN_EARLIER", 2 * DAY, seconds=480)
    repo.row("VN_EARLIER", "done", seconds=480, updated_at=clock() - 3600)  # 8 min today
    for i in range(4):
        repo.add(f"VN_C{i}", 3600 + i, seconds=60)
    CAP.lines.clear()
    counts = await vc.run_once()
    check("8 min used + 1 + 1 fits 10; the third would not", len(tr.calls) == 2, str(tr.calls))
    caps = [v for v in CAP.voice() if v.get("cap")]
    check("the cap is logged when it bites",
          len(caps) == 1 and caps[0]["limit_minutes"] == 10 and caps[0]["minutes_24h"] == 10.0,
          str(CAP.voice()))
    check("the pass still reports what it did", counts["done"] == 2)
    await vc.run_once()
    check("still capped: nothing more, and no second cap line",
          len(tr.calls) == 2 and len([v for v in CAP.voice() if v.get("cap")]) == 1)
    clock.advance(DAY + 1)
    await vc.run_once()
    check("24 hours on, the rest goes through", len(tr.calls) == 4, str(tr.calls))

    vc, repo, tr, clock = make(history_voice_daily_minutes=1)
    repo.add("VN_U1", 3600, seconds=None)
    repo.add("VN_U2", 3601, seconds=None)
    tr.will("VN_U1", {"text": "x", "duration_sec": None, "error": None})
    await vc.run_once()
    check("a note with no declared length counts as a minute", tr.calls == ["VN_U1"], str(tr.calls))

    vc, repo, tr, clock = make(history_voice_daily_minutes=0)
    for i in range(5):
        repo.add(f"VN_N{i}", 3600 + i, seconds=600)
    await vc.run_once()
    check("cap 0 means no cap", len(tr.calls) == 5)


# -- 8. concurrency ---------------------------------------------------------------------------

async def t8_concurrency():
    print("8. at most HISTORY_VOICE_CONCURRENCY transcriptions at once")
    for width in (1, 3):
        vc, repo, tr, clock = make(history_voice_concurrency=width)
        tr.delay = 0.02
        for i in range(14):
            repo.add(f"VN_P{width}_{i}", 3600 + i)
        counts = await vc.run_once()
        check(f"concurrency {width}: peak {tr.peak}, all 14 done",
              tr.peak == width and counts["done"] == 14, f"peak={tr.peak} {counts}")


# -- 9. shutdown ------------------------------------------------------------------------------

async def t9_shutdown():
    print("9. aclose() stops the loop at once, even mid-transcription")
    vc, repo, tr, clock = make()
    tr.gate = asyncio.Event()  # the provider never answers
    repo.add("VN_HANG", 3600)
    vc.start()
    started = await wait_for(lambda: tr.inflight == 1)
    check("a transcription is in flight", started)
    t0 = time.monotonic()
    await vc.aclose()
    took = time.monotonic() - t0
    check("aclose returned promptly", took < 1.0, f"{took:.2f}s")
    check("the repository was closed", repo.closed)
    check("the cut-off note has no row, so it is tried again after the restart",
          "VN_HANG" not in repo.rows)
    check("no transcription is left running", tr.inflight == 0)

    vc, repo, tr, clock = make()
    vc.start()
    await wait_for(lambda: len(repo.queries) >= 1)
    t0 = time.monotonic()
    await vc.aclose()
    check("an idle loop (sleeping between passes) stops at once too",
          time.monotonic() - t0 < 1.0 and repo.closed)
    await vc.aclose()
    check("a second aclose is harmless", True)

    vc, repo, tr, clock = make()
    await vc.aclose()
    check("aclose without start just closes the repository", repo.closed)


# -- 10. never raises -------------------------------------------------------------------------

async def t10_never_raises():
    print("10. a failure is logged and retried next pass, never raised into the app")
    vc, repo, tr, clock = make(history_voice_interval_seconds=1)
    repo.add("VN_LATER", 3600)
    repo.raise_candidates = 1
    CAP.lines.clear()
    vc.start()
    ok = await wait_for(lambda: tr.calls == ["VN_LATER"], timeout=3.0)
    await vc.aclose()
    check("the first pass failed, the loop survived, the next pass did the work", ok,
          f"queries={len(repo.queries)} calls={tr.calls}")
    check("the failure was logged", any("voice capture pass failed" in line for line in CAP.lines))

    vc, repo, tr, clock = make()
    repo.add("VN_RAISE", 3600)
    tr.raise_for.add("VN_RAISE")
    await vc.run_once()
    check("a transcription that raises counts as a provider failure: pending",
          repo.rows["VN_RAISE"]["status"] == "pending"
          and repo.rows["VN_RAISE"]["last_error"] == "provider", str(repo.rows.get("VN_RAISE")))

    vc, repo, tr, clock = make()
    for i in range(3):
        repo.add(f"VN_W{i}", 3600 + i)
    repo.raise_record = 1
    try:
        await vc.run_once()
        raised = False
    except Exception:
        raised = True
    check("a bookkeeping write that fails does not raise", not raised)
    check("and the pass stops there instead of hammering the database", len(tr.calls) == 1,
          str(tr.calls))


# -- 11. auth ---------------------------------------------------------------------------------

async def t11_auth():
    print("11. a missing or rejected provider key pauses capture, costing no note an attempt")
    vc, repo, tr, clock = make()
    repo.add("VN_K1", 3600)
    repo.add("VN_K2", 7200)
    tr.will("VN_K1", err("auth"), OK)
    CAP.lines.clear()
    await vc.run_once()
    check("one call, then the pass stops", tr.calls == ["VN_K1"], str(tr.calls))
    check("nothing recorded against the note", "VN_K1" not in repo.rows)
    check("the pause is logged", any(v.get("paused") == "auth" for v in CAP.voice()), str(CAP.voice()))
    await vc.run_once()
    check("during the pause nothing is tried", tr.calls == ["VN_K1"])
    clock.advance(901)
    await vc.run_once()
    check("after 15 minutes capture resumes", repo.rows.get("VN_K1", {}).get("status") == "done"
          and repo.rows.get("VN_K2", {}).get("status") == "done", str(repo.rows))


# -- 12. outage breaker -----------------------------------------------------------------------

async def t12_breaker():
    print("12. three failures in a row end the pass (an outage costs a few attempts, not all)")
    vc, repo, tr, clock = make()
    for i in range(6):
        repo.add(f"VN_O{i}", 3600 + i)
        tr.will(f"VN_O{i}", err("provider"))
    CAP.lines.clear()
    counts = await vc.run_once()
    check("three tried, three left untouched", len(tr.calls) == 3 and counts["pending"] == 3,
          f"{tr.calls} {counts}")
    check("the halt is logged", any(v.get("halted") == "unhealthy" for v in CAP.voice()))
    check("the untouched ones have no attempt counted",
          all(f"VN_O{i}" not in repo.rows for i in range(3, 6)))
    await vc.run_once()
    check("the next pass reaches the others (the first three are in backoff)",
          len(tr.calls) == 6 and set(tr.calls[3:]) == {"VN_O3", "VN_O4", "VN_O5"}, str(tr.calls))

    vc, repo, tr, clock = make()
    for i in range(6):
        repo.add(f"VN_M{i}", 3600 + i)
        tr.will(f"VN_M{i}", err("provider") if i % 3 != 2 else OK)
    await vc.run_once()
    check("a success in between resets the streak: all six tried", len(tr.calls) == 6)


# -- 13. the log line -------------------------------------------------------------------------

async def t13_logline():
    print("13. one JSON line per pass that did something")
    vc, repo, tr, clock = make()
    repo.add("VN_A", 3600, seconds=90)
    repo.add("VN_B", 3700, seconds=2000)
    CAP.lines.clear()
    await vc.run_once()
    lines = CAP.summaries()
    check("one line", len(lines) == 1, str(CAP.lines))
    want = {"done", "expired", "failed", "skipped", "pending", "minutes"}
    check("with the agreed keys", lines and set(lines[0]) == want, str(lines))
    check("and the right numbers", lines and lines[0]["done"] == 1 and lines[0]["skipped"] == 1
          and lines[0]["minutes"] == 1.5, str(lines))
    await vc.run_once()
    check("an idle pass logs nothing", len(CAP.summaries()) == 1)


# -- 14. open ---------------------------------------------------------------------------------

async def t14_open():
    print("14. open() and the constructor")
    vc, repo, tr, clock = make(transcription_enabled=False)
    try:
        await vc.open()
        refused = False
    except RuntimeError:
        refused = True
    check("transcription switched off: open() refuses (runtime then leaves it off)",
          refused and not repo.opened)
    try:
        bad = VoiceCapture("postgresql://nowhere.invalid/x", tr,
                           settings(history_schema="bad; drop"))
        built = True
    except Exception:
        built = False
    check("a bad schema setting does not break the constructor (the boot goes on)", built)
    try:
        await bad.repo.open()
        stopped = False
    except ValueError:
        stopped = True
    check("open() refuses it before any SQL is built", stopped)
    vc, repo, tr, clock = make()
    await vc.open()
    check("open() opens the repository", repo.opened)


# -- 15. the SQL ------------------------------------------------------------------------------

def _params(sql: str) -> set:
    return set(re.findall(r"%\((\w+)\)s", sql))


async def t15_sql():
    print("15. the SQL the real repository sends")
    q = candidates_sql(log_schema="lisa_log", history_schema="lisa_search")
    check("parameters are exactly since/until/limit", _params(q) == {"since", "until", "limit"})
    check("no stray % for psycopg to trip on", not re.findall(r"%(?!\(\w+\)s)", q))
    check("reads Evolution's table", 'FROM public."Message" m' in q)
    check("one row per key.id", "DISTINCT ON (msg_id)" in q)
    check("newest first, bounded", "ORDER BY a.ts DESC\nLIMIT %(limit)s" in q)
    check("leaves out notes with a transcript",
          "NOT EXISTS (SELECT 1 FROM lisa_log.transcripts t WHERE t.wa_id = a.msg_id)" in q)
    check("leaves out settled notes and ones waiting out a backoff",
          "LEFT JOIN lisa_search.voice_attempts v" in q and "v.status = 'pending'" in q
          and "v.next_at <= now()" in q)
    check("looks inside every wrapper",
          all(f"'{w}'" in q for w in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
                                      "viewOnceMessageV2Extension"))
          and "m.message->'ephemeralMessage'->'message'->'audioMessage'" in q)
    check("the time filter is inclusive at both ends",
          'm."messageTimestamp" BETWEEN %(since)s AND %(until)s' in q)
    check("parentheses balance", q.count("(") == q.count(")"))
    q2 = candidates_sql(log_schema="lisa_log", history_schema="lisa_search", with_transcripts=False)
    check("without the transcripts table it is not referenced", "transcripts" not in q2)
    r = record_sql("lisa_search")
    check("the upsert names every column",
          _params(r) == {"msg_id", "chat", "ts", "seconds", "attempts", "status", "last_error",
                         "next_at"} and "ON CONFLICT (msg_id) DO UPDATE" in r)
    d = ddl("lisa_search")
    check("the table is created idempotently, statuses checked",
          "CREATE TABLE IF NOT EXISTS lisa_search.voice_attempts" in d
          and "CREATE SCHEMA IF NOT EXISTS lisa_search" in d
          and all(f"'{x}'" in d for x in voice_mod.STATUSES) and "msg_id     text PRIMARY KEY" in d)


# -- 16. against Postgres ---------------------------------------------------------------------

FIXTURE_MARK = "history-voice test fixture"


async def t16_postgres():
    print("16. the real repository against a throwaway Postgres")
    dsn = os.environ.get("HISTORY_VOICE_TEST_DSN")
    if not dsn:
        print("  [SKIP] set HISTORY_VOICE_TEST_DSN to run these")
        return
    import psycopg
    from psycopg.types.json import Jsonb

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        cur = await conn.execute(
            "SELECT to_regclass('public.\"Message\"') IS NOT NULL, "
            "obj_description(to_regclass('public.\"Message\"'), 'pg_class')")
        exists, mark = await cur.fetchone()
        if exists and mark != FIXTURE_MARK:
            check('refusing: this database has a "Message" table this test did not create', False)
            return
        await conn.execute('DROP TABLE IF EXISTS public."Message"')
        await conn.execute("DROP SCHEMA IF EXISTS vt_log CASCADE")
        await conn.execute("DROP SCHEMA IF EXISTS vt_search CASCADE")
        await conn.execute(
            'CREATE TABLE public."Message" (id text PRIMARY KEY, key jsonb, "messageType" varchar, '
            'message jsonb, "messageTimestamp" int)')
        await conn.execute(f"COMMENT ON TABLE public.\"Message\" IS '{FIXTURE_MARK}'")
        await conn.execute("CREATE SCHEMA vt_log")
        await conn.execute(
            "CREATE TABLE vt_log.transcripts (wa_id text PRIMARY KEY, text text NOT NULL, "
            "duration_sec real, language text, created_at timestamptz NOT NULL DEFAULT now())")

        now = int(time.time())
        n = [0]

        async def put(msg_id, age, mtype, message, chat=CONTACT):
            n[0] += 1
            await conn.execute(
                'INSERT INTO public."Message" (id, key, "messageType", message, "messageTimestamp") '
                "VALUES (%s, %s, %s, %s, %s)",
                (f"row{n[0]}", Jsonb({"id": msg_id, "remoteJid": chat, "fromMe": False}), mtype,
                 Jsonb(message), now - age))

        def audio(seconds=10, **extra):
            return {"audioMessage": {"seconds": seconds, "ptt": True, "mimetype": "audio/ogg",
                                     "mediaKey": "made-up", **extra}}

        await put("PG_PLAIN", 600, "audioMessage", audio(12))
        await put("PG_GROUP", 2 * DAY, "audioMessage", audio(30), chat=GROUP)
        await put("PG_EPH", DAY, "ephemeralMessage", {"ephemeralMessage": {"message": audio(7)}})
        await put("PG_VO", 3 * DAY, "viewOnceMessageV2", {"viewOnceMessageV2": {"message": audio(9)}})
        await put("PG_VOFLAG", 3 * DAY + 5, "audioMessage", audio(8, viewOnce=True))
        await put("PG_IMG", DAY, "ephemeralMessage",
                  {"ephemeralMessage": {"message": {"imageMessage": {"caption": "x"}}}})
        await put("PG_TEXT", DAY, "conversation", {"conversation": "made-up"})
        await put("PG_QUOTED", DAY, "documentMessage",
                  {"documentMessage": {"contextInfo": {"quotedMessage": audio(5)}}})
        await put("PG_NULL", 4 * DAY, "audioMessage", None)
        await put("PG_DUP", 5 * DAY, "audioMessage", audio(20))
        await put("PG_DUP", 5 * DAY - 10, "audioMessage", None)
        await put("PG_STRSEC", 6 * DAY, "audioMessage", audio("45"))
        await put("PG_FRESH", 60, "audioMessage", audio(5))
        await put("PG_OLD", 29 * DAY, "audioMessage", audio(5))
        await put("PG_HAS_T", DAY, "audioMessage", audio(5))
        await conn.execute("INSERT INTO vt_log.transcripts (wa_id, text) VALUES ('PG_HAS_T', 'x')")
        for mid, age in (("PG_R_DONE", DAY), ("PG_R_SKIP", DAY), ("PG_R_WAIT", DAY), ("PG_R_DUE", 7 * DAY)):
            await put(mid, age, "audioMessage", audio(5))

        repo = PgVoiceRepo(dsn, log_schema="vt_log", history_schema="vt_search")
        await repo.open()
        check("open() created vt_search.voice_attempts and saw the transcripts table",
              repo._has_transcripts)
        await conn.execute(
            "INSERT INTO vt_search.voice_attempts (msg_id, chat, ts, attempts, status, next_at) VALUES "
            f"('PG_R_DONE', 'c', {now - DAY}, 1, 'done', NULL), "
            f"('PG_R_SKIP', 'c', {now - DAY}, 0, 'skipped', NULL), "
            f"('PG_R_WAIT', 'c', {now - DAY}, 1, 'pending', now() + interval '1 hour'), "
            f"('PG_R_DUE', 'c', {now - 7 * DAY}, 2, 'pending', now() - interval '1 minute')")

        since, until = now - 28 * 86400, now - 300
        notes = await repo.candidates(since=since, until=until, limit=50)
        ids = [x.msg_id for x in notes]
        want = {"PG_PLAIN", "PG_GROUP", "PG_EPH", "PG_VO", "PG_VOFLAG", "PG_NULL", "PG_DUP",
                "PG_STRSEC", "PG_R_DUE"}
        check("exactly the due voice notes", set(ids) == want,
              f"extra={set(ids) - want} missing={want - set(ids)}")
        check("newest first", [x.ts for x in notes] == sorted((x.ts for x in notes), reverse=True))
        by = {x.msg_id: x for x in notes}
        check("wrapped notes carry their length",
              by.get("PG_EPH") and by["PG_EPH"].seconds == 7 and by["PG_VO"].seconds == 9)
        check("a record with no audio comes back marked as such",
              by.get("PG_NULL") and not by["PG_NULL"].has_media and by["PG_NULL"].seconds is None)
        check("a message stored twice is one note, the copy with audio",
              ids.count("PG_DUP") == 1 and by["PG_DUP"].has_media and by["PG_DUP"].seconds == 20)
        check("a length stored as a string is read", by.get("PG_STRSEC") and by["PG_STRSEC"].seconds == 45)
        check("a pending row carries its attempts", by.get("PG_R_DUE") and by["PG_R_DUE"].attempts == 2)
        check("the chat is the stored jid", by.get("PG_GROUP") and by["PG_GROUP"].chat == GROUP)
        top = await repo.candidates(since=since, until=until, limit=2)
        check("the limit keeps the newest", [x.msg_id for x in top] == ["PG_PLAIN", "PG_EPH"],
              str([x.msg_id for x in top]))

        note = by["PG_GROUP"]
        await repo.record(note, status="pending", attempts=1, last_error="provider",
                          next_at=time.time() + 600, seconds=30)
        again = {x.msg_id for x in await repo.candidates(since=since, until=until, limit=50)}
        check("a recorded backoff takes the note out of the next read", "PG_GROUP" not in again)
        await repo.record(note, status="done", attempts=2, last_error=None, next_at=None,
                          seconds=None)
        cur = await conn.execute(
            "SELECT status, attempts, seconds, next_at, last_error FROM vt_search.voice_attempts "
            "WHERE msg_id = 'PG_GROUP'")
        row = await cur.fetchone()
        check("the upsert updates in place and keeps the length it had",
              row == ("done", 2, 30, None, None), str(row))
        mins = await repo.minutes_since(time.time() - 3600)
        check("minutes_since adds up the done rows", abs(mins - 0.5) < 1e-6, str(mins))
        await conn.execute(
            "INSERT INTO vt_search.voice_attempts (msg_id, chat, ts, attempts, status, last_error) "
            f"VALUES ('PG_AGED', 'c', {now - 40 * DAY}, 3, 'pending', 'download')")
        aged = await repo.age_out(before=since)
        cur = await conn.execute(
            "SELECT status, last_error FROM vt_search.voice_attempts WHERE msg_id = 'PG_AGED'")
        check("age_out expires pending rows past the window", aged == 1
              and await cur.fetchone() == ("expired", "download; aged out"))
        try:
            await conn.execute("INSERT INTO vt_search.voice_attempts (msg_id, status) VALUES ('x', 'bogus')")
            checked = False
        except psycopg.errors.CheckViolation:
            checked = True
        check("the status column refuses anything else", checked)

        # The whole pass, real repository, fake provider.
        tr = FakeTranscription(type("R", (), {"transcribed": set()})())
        tr.will("PG_R_DUE", err("download"))
        vc = VoiceCapture(dsn, tr, settings(log_schema="vt_log", history_schema="vt_search",
                                            history_voice_concurrency=2), repo=repo)
        counts = await vc.run_once()
        cur = await conn.execute("SELECT msg_id, status FROM vt_search.voice_attempts")
        st = dict(await cur.fetchall())
        check("a full pass settles every due note",
              st.get("PG_PLAIN") == "done" and st.get("PG_NULL") == "skipped"
              and st.get("PG_R_DUE") == "pending" and st.get("PG_DUP") == "done", str(st))
        check("and reports it", counts["done"] == 6 and counts["skipped"] == 1
              and counts["pending"] == 1, str(counts))
        check("the next pass finds nothing to do",
              (await vc.run_once())["done"] == 0 and len(tr.calls) == 7, str(tr.calls))

        await conn.execute("DROP TABLE vt_log.transcripts")
        repo2 = PgVoiceRepo(dsn, log_schema="vt_log", history_schema="vt_search")
        await repo2.open()
        check("with the transcripts table missing, open() still works",
              repo2._has_transcripts is False)
        left = {x.msg_id for x in await repo2.candidates(since=since, until=until, limit=50)}
        check("and voice_attempts alone decides", "PG_HAS_T" in left and "PG_PLAIN" not in left,
              str(left))
        await repo2.aclose()
        await vc.aclose()
    finally:
        await conn.execute('DROP TABLE IF EXISTS public."Message"')
        await conn.execute("DROP SCHEMA IF EXISTS vt_log CASCADE")
        await conn.execute("DROP SCHEMA IF EXISTS vt_search CASCADE")
        await conn.close()


async def main() -> None:
    for fn in (t1_order, t2_window, t3_already, t4_expiry, t5_retries, t6_skips, t7_cap,
               t8_concurrency, t9_shutdown, t10_never_raises, t11_auth, t12_breaker,
               t13_logline, t14_open, t15_sql, t16_postgres):
        await fn()
        print()
    print(f"{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
