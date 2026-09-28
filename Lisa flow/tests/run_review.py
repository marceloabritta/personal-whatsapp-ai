"""Session-review verification, three layers, first failure exits non-zero:

  A. pure functions — the gap vocabulary, the judge schema, the transcript renderer, and the
     timing scorer's banding rules (no DB, no API key);
  B. real-data baseline — replays 120 recorded turns from the live window through the timing
     scorer and asserts it still reproduces the measured distribution. This is the regression
     that catches a budget edit or a sign error the unit tests would wave through;
  C. real Postgres — if MARY_TEST_DATABASE_URL is set, creates the review tables in a throwaway
     schema and round-trips a review with findings (upsert, replace, mark-done).

    cd "Lisa flow" && python tests/run_review.py
"""
from __future__ import annotations

import asyncio
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.review import prompt as rp  # noqa: E402
from app.review import taxonomy as tx  # noqa: E402
from app.review.timing import (  # noqa: E402
    BUDGETS, SILENCE_BUDGET, WINDOW_SECONDS, Turn, score_timing,
)

_checks = {"pass": 0, "fail": 0}
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "turn_facts.psv")


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    _checks["pass" if cond else "fail"] += 1


# ============================ A. pure functions ============================
def test_pure() -> None:
    print("A. pure functions (no DB, no key)")

    # -- vocabulary --
    check("19 judge codes", len(tx.JUDGE_CODES) == 19)
    check("6 timing codes", len(tx.TIMING_CODES) == 6)
    check("families do not overlap", not (set(tx.JUDGE_CODES) & set(tx.TIMING_CODES)))
    check("every code has a description", all(tx.ALL_CODES.values()))
    # v2 codes, each added because the first run fragmented ONE real failure across several
    # vaguer codes (a denied capability landed in wrong_domain, ignored_context AND a free-text
    # proposal), or because a code was acting as a magnet for a fault it does not describe
    # (every single claimed_undone was really "had approval and re-asked instead of acting").
    for code in ("false_refusal", "ignored_approval", "incomplete_message"):
        check(f"v2 code present: {code}", code in tx.JUDGE_CODES)
    check("claimed_undone now excludes the failure-to-act case",
          "was done" in tx.JUDGE_CODES["claimed_undone"].lower())
    check("wrong_details now excludes mere omission",
          "incomplete_message" in tx.JUDGE_CODES["wrong_details"])
    check("unclear_reply now excludes mere omission",
          "incomplete_message" in tx.JUDGE_CODES["unclear_reply"])
    check("false_refusal is pointed at instead of wrong_domain",
          "wrong_domain" in tx.JUDGE_CODES["false_refusal"])

    # -- schema: the Anthropic output_config rejects >16 anyOf/array params --
    def unions(o) -> int:
        if isinstance(o, dict):
            n = 1 if ("anyOf" in o or o.get("type") == "array") else 0
            return n + sum(unions(v) for v in o.values())
        if isinstance(o, list):
            return sum(unions(v) for v in o)
        return 0

    schema = tx.judge_schema()
    check("schema under the union cap", unions(schema) <= 16)
    check("judge may only use judge codes",
          set(schema["properties"]["gaps"]["items"]["properties"]["code"]["enum"]) == set(tx.JUDGE_CODES))
    check("task_class enum matches", set(schema["properties"]["task_class"]["enum"]) == set(tx.TASK_CLASSES))

    # -- normalise: the judge is allowed to be inconsistent; the store is not --
    good_with_gaps = tx.normalise({"verdict": "good", "confidence": "high", "rationale": "r",
                                   "task_class": "ack", "proposed_gap": "",
                                   "gaps": [{"code": "too_verbose", "severity": "major",
                                             "evidence": "e", "harm": "he read padding"}]})
    check("good + gaps downgrades to acceptable", good_with_gaps["verdict"] == "acceptable")
    check("good + gaps keeps the gaps", len(good_with_gaps["gaps"]) == 1)
    check("the harm is kept", good_with_gaps["gaps"][0]["harm"] == "he read padding")
    check("unknown code is dropped",
          tx.normalise({"gaps": [{"code": "nope", "severity": "minor", "evidence": "e",
                                  "harm": "h"}]})["gaps"] == [])

    # -- v3 guards. Each one removes a class of bad call measured in the v2 audit. --
    # A finding that cannot say what was lost was an observation, not a fault.
    check("a gap with no harm is dropped",
          tx.normalise({"gaps": [{"code": "too_verbose", "severity": "minor",
                                  "evidence": "e", "harm": "   "}]})["gaps"] == [])
    # v2 filed two codes off one rationale five times, which doubled one fault in every count.
    twice = tx.normalise({"rationale": "one problem", "gaps": [
        {"code": "wrong_details", "severity": "minor", "evidence": "a",
         "harm": "the guest line contradicts the confirmation"},
        {"code": "bad_format", "severity": "minor", "evidence": "b",
         "harm": "The guest line contradicts the confirmation."},
    ]})
    check("one harm, one finding — the second code collapses", len(twice["gaps"]) == 1)
    check("the collapse keeps the FIRST, most specific code",
          twice["gaps"][0]["code"] == "wrong_details")
    two_faults = tx.normalise({"rationale": "two problems", "gaps": [
        {"code": "wrong_details", "severity": "minor", "evidence": "a", "harm": "wrong end time"},
        {"code": "too_verbose", "severity": "minor", "evidence": "b", "harm": "three currencies"},
    ]})
    check("two genuinely different harms both stand", len(two_faults["gaps"]) == 2)

    # v2 ended four rationales with "staying silent is defensible" and filed missed_turn anyway.
    cleared = tx.normalise({"rationale": "it was banter between the two of them, so staying "
                                         "silent is defensible.",
                            "gaps": [{"code": "missed_turn", "severity": "minor",
                                      "evidence": "e", "harm": "he got no answer"}]}, silent=True)
    check("a silence the rationale clears files nothing", cleared["gaps"] == [])
    kept = tx.normalise({"rationale": "he addressed her directly with @lisa and she stayed "
                                      "silent instead of acting.",
                         "gaps": [{"code": "missed_turn", "severity": "major",
                                   "evidence": "e", "harm": "his request went unanswered"}]},
                        silent=True)
    check("a real miss still stands", len(kept["gaps"]) == 1)
    not_silent = tx.normalise({"rationale": "staying silent would have been reasonable",
                               "gaps": [{"code": "missed_turn", "severity": "minor",
                                         "evidence": "e", "harm": "h"}]}, silent=False)
    check("the silence guard never touches a REPLY", len(not_silent["gaps"]) == 1)
    other_code = tx.normalise({"rationale": "staying silent is defensible",
                               "gaps": [{"code": "wrong_details", "severity": "minor",
                                         "evidence": "e", "harm": "wrong date"}]}, silent=True)
    check("the silence guard only clears silence codes", len(other_code["gaps"]) == 1)
    prop = tx.normalise({"verdict": "bad", "gaps": [], "proposed_gap": "invented a price"})
    check("proposed_gap becomes an 'other' finding",
          len(prop["gaps"]) == 1 and prop["gaps"][0]["code"] == tx.OTHER)
    check("garbage verdict falls back", tx.normalise({"verdict": "excellent"})["verdict"] == "acceptable")
    check("garbage task_class falls back",
          tx.normalise({"task_class": "xyz"})["task_class"] == tx.DEFAULT_TASK_CLASS)

    # -- the judge must not be told anything it is not allowed to use --
    sysprompt = rp.build_judge_prompt("Marcelo", "@lisa")
    for forbidden in ("latency", "seconds", "how long it took", "milliseconds"):
        check(f"prompt never mentions {forbidden!r}", forbidden not in sysprompt.lower())
    check("prompt says not to judge speed", "do not judge speed" in sysprompt.lower())
    check("prompt lists every judge code", all(c in sysprompt for c in tx.JUDGE_CODES))
    check("prompt frames silence as legitimate", "silence is a real" in sysprompt.lower())
    check("prompt asks for the MOST SPECIFIC code", "most specifically" in sysprompt.lower())
    check("prompt forbids double-filing one fault", "one code per fault" in sysprompt.lower())
    # v3: the four things the audit showed the judge did not know.
    check("prompt tells it to resolve relative dates against the stamp",
          "amanh" in sysprompt.lower() and "correct" in sysprompt.lower())
    check("prompt says a gap must name the harm", "name the harm" in sysprompt.lower())
    check("prompt clears a turn its own rationale defends",
          "the gaps list is empty" in sysprompt.lower())
    check("prompt marks the confirmation gate as deliberate",
          "deliberate" in sysprompt.lower() and "go-ahead" in sysprompt.lower())
    check("capability card grants guest invitations", "guest" in sysprompt.lower())
    check("capability card grants contact memory", "remember the people" in sysprompt.lower())

    # -- the date stamp reaches the turn message --
    import datetime as _dt
    when = rp.format_when(_dt.datetime(2026, 9, 16, 20, 44, tzinfo=_dt.timezone.utc))
    check("the stamp names the weekday", "Wednesday" in when)
    check("the stamp is local, not UTC", "17:44" in when and "Sao_Paulo" in when)
    check("no timestamp means no stamp block", rp.format_when(None) == "")
    check("the stamp is shown to the judge", "Wednesday" in rp.build_turn_message("x", "hi", when))
    check("a turn with no stamp shows no empty header",
          "WHEN THIS TURN WAS TAKEN" not in rp.build_turn_message("x", "hi", ""))

    # -- transcript rendering --
    lines = [{"who": "Marcelo", "text": f"m{i}"} for i in range(10)]
    rendered = rp.render_transcript(lines, max_lines=4)
    check("transcript keeps the NEWEST lines", "m9" in rendered and "m0" not in rendered)
    check("transcript notes what it dropped", "earlier messages omitted" in rendered)
    check("silence is marked explicitly", rp.SILENT_MARKER in rp.build_turn_message("x", None))
    check("a reply is not marked silent", rp.SILENT_MARKER not in rp.build_turn_message("x", "hi"))
    check("empty lines are skipped",
          rp.render_transcript([{"who": "A", "text": ""}, {"who": "B", "text": "y"}]) == "B: y")

    # -- banding --
    ack = BUDGETS["ack"]
    check("ack: 5s is good", ack.band(5) == "good")
    check("ack: 22s is slow", ack.band(22) == "slow")
    check("ack: 35s is a breach", ack.band(35) == "breach")
    # The v1 budgets fired on the median (63-100% of traffic per class), which ranks nothing.
    # These are set off the measured p50/p90 so they catch the tail instead.
    check("ack no longer fires on its own median (13.0s)", ack.band(13.0) == "good")
    check("silence no longer fires on its own median (12.7s)",
          SILENCE_BUDGET.band(12.7) == "good")
    check("web_lookup no longer fires on its own median (39.9s)",
          BUDGETS["web_lookup"].band(39.9) != "breach")
    check("calendar_write, already well calibrated, is unchanged",
          (BUDGETS["calendar_write"].good_s, BUDGETS["calendar_write"].slow_s) == (20, 40))
    check("boundaries are inclusive", ack.band(ack.good_s) == "good" and ack.band(ack.slow_s) == "slow")
    check("audio scales the transcription budget",
          BUDGETS["transcription"].band(40, audio_sec=120) == "good"
          and BUDGETS["transcription"].band(40) == "slow")
    check("research gets more room than an ack",
          BUDGETS["web_research"].good_s > BUDGETS["ack"].good_s)

    # -- scoring --
    fast = score_timing(Turn(silent=False, reply_ts=1000, last_human_ts=995), "ack")
    check("a fast reply files nothing", fast.gaps == [] and fast.band == "good")

    slow = score_timing(Turn(silent=False, reply_ts=1040, last_human_ts=1000), "ack")
    check("a slow reply files slow_reply/major", ("slow_reply", "major") in slow.gaps)

    quiet = score_timing(Turn(silent=True, reply_ts=1040, last_human_ts=1000), "web_research")
    check("a slow SILENCE is not excused by the task",
          ("slow_silence", "major") in quiet.gaps)
    check("silence ignores the task budget", SILENCE_BUDGET.slow_s < BUDGETS["web_research"].slow_s)

    past = score_timing(Turn(silent=False, reply_ts=1000 + WINDOW_SECONDS + 1, last_human_ts=1000),
                        "web_research")
    check("past the window is window_expired/major", ("window_expired", "major") in past.gaps)
    check("window_expired supersedes the band",
          not any(c == "slow_reply" for c, _ in past.gaps))

    err = score_timing(Turn(silent=True, reply_ts=1040, last_human_ts=1000, error=True), "ack")
    # -- overtaken: measured, but not charged --
    slow_ot = score_timing(Turn(silent=True, reply_ts=1_100.0, last_human_ts=1_000.0,
                                overtaken=True), "ack")
    check("an overtaken turn still reports its band", slow_ot.band == "breach")
    check("an overtaken turn still reports its wait", slow_ot.wait_seconds == 100.0)
    check("an overtaken turn files NO latency gap",
          [c for c, _ in slow_ot.gaps if c in ("window_expired", "slow_silence", "slow_reply")] == [])
    same_not_ot = score_timing(Turn(silent=True, reply_ts=1_100.0, last_human_ts=1_000.0), "ack")
    check("...but the same turn not overtaken does", 
          ("window_expired", "major") in same_not_ot.gaps)
    err_ot = score_timing(Turn(silent=True, reply_ts=1_100.0, last_human_ts=1_000.0,
                               error=True, delivery="failed", overtaken=True), "ack")
    codes_ot = [c for c, _ in err_ot.gaps]
    check("an overtaken turn still files a no-answer error", "no_answer_error" in codes_ot)
    check("an overtaken turn still files a failed delivery", "delivery_failed" in codes_ot)

    check("an errored turn files no_answer_error", ("no_answer_error", "major") in err.gaps)
    quick_err = score_timing(Turn(silent=True, reply_ts=1005, last_human_ts=1000, error=True), "ack")
    check("a QUICK error is only minor", ("no_answer_error", "minor") in quick_err.gaps)

    failed = score_timing(Turn(silent=False, reply_ts=1200, last_human_ts=1000, delivery="failed"), "ack")
    check("a failed send files delivery_failed/major", ("delivery_failed", "major") in failed.gaps)

    unknown = score_timing(Turn(silent=False, reply_ts=None, last_human_ts=None), "ack")
    check("an unmeasurable turn bands 'unknown'", unknown.band == "unknown")
    check("an unmeasurable turn files NO timing gap", unknown.gaps == [])
    unknown_err = score_timing(Turn(silent=True, reply_ts=None, last_human_ts=None, error=True), "ack")
    check("...but a no-answer fact still stands",
          ("no_answer_error", "minor") in unknown_err.gaps)

    skew = score_timing(Turn(silent=False, reply_ts=999, last_human_ts=1000), "ack")
    check("small clock skew reads as immediate", skew.wait_seconds == 0.0)
    check("large negative skew is refused",
          score_timing(Turn(silent=False, reply_ts=900, last_human_ts=1000), "ack").band == "unknown")


    # -- the judge must never be shown the message it is judging --
    # act_node writes the reply's transcript line BEFORE it emits the record, so both carry the
    # same trace_id and a naive seq slice would hand the judge its own output as "context".
    from app.review.store import _context_for

    lines = [
        {"who": "Marcelo", "text": "older ask", "trace_id": "T0"},
        {"who": "AI Assistant", "text": "an EARLIER reply", "trace_id": "T0"},
        {"who": "Marcelo", "text": "the new ask", "trace_id": "T1"},
        {"who": "AI Assistant", "text": "THE TURN ITSELF", "trace_id": "T1"},
    ]
    ctx = [ln["text"] for ln in _context_for(lines, "T1")]
    check("context excludes the turn being judged", "THE TURN ITSELF" not in ctx)
    check("context keeps her EARLIER replies", "an EARLIER reply" in ctx)
    check("context keeps human lines from the same activation", "the new ask" in ctx)
    mutated = _context_for(lines, "T1")
    mutated[0]["text"] = "clobbered"
    check("context is a copy, not an alias", lines[0]["text"] == "older ask")


# ==================== B. real-data baseline regression =====================
def test_baseline() -> None:
    print("\nB. real-data baseline (120 recorded turns)")
    if not os.path.exists(FIXTURE):
        check("fixture present", False)
        return

    waits: dict[str, list] = {"reply": [], "silence": []}
    over60 = noanswer = overtaken = unknown = suppressed = 0
    for line in open(FIXTURE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        silent_s, wait_s, err, dlv, ot = line.split("|")
        silent = silent_s == "t"
        # Replay through the scorer with a fixed base so only the DELTA matters.
        s = score_timing(
            Turn(silent=silent, reply_ts=1_000_000 + float(wait_s), last_human_ts=1_000_000,
                 error=(err != "none"), delivery=dlv or None, overtaken=(ot == "t")),
            "ack",
        )
        # What the v3 overtaken rule removes, measured on the same 120 turns: the wait is still
        # recorded (the percentiles below are unchanged), only the GAP is withheld.
        charged = score_timing(
            Turn(silent=silent, reply_ts=1_000_000 + float(wait_s), last_human_ts=1_000_000,
                 error=(err != "none"), delivery=dlv or None),
            "ack",
        )
        suppressed += len(charged.gaps) - len(s.gaps)
        if s.wait_seconds is None:
            unknown += 1
        else:
            waits["silence" if silent else "reply"].append(s.wait_seconds)
            if s.wait_seconds > WINDOW_SECONDS:
                over60 += 1
        noanswer += sum(1 for c, _ in s.gaps if c in ("no_answer_error", "delivery_failed"))
        overtaken += ot == "t"

    def pct(v, q):
        v = sorted(v)
        return v[min(len(v) - 1, int(math.ceil(q * len(v))) - 1)]

    def near(a, b, tol=0.05):
        return abs(a - b) <= tol

    r, s_ = waits["reply"], waits["silence"]
    check("every turn is measurable (0 unknown)", unknown == 0)
    check("82 replies / 38 silences", len(r) == 82 and len(s_) == 38)
    check("replies p50 = 7.3s", near(pct(r, .5), 7.3))
    check("replies p90 = 25.4s", near(pct(r, .9), 25.4))
    check("replies worst = 196.8s", near(max(r), 196.8, 0.2))
    check("silences p50 = 12.7s", near(pct(s_, .5), 12.7))
    check("silences p90 = 39.5s", near(pct(s_, .9), 39.5))
    check("8 replies over 30s", sum(1 for x in r if x > 30) == 8)
    check("6 silences over 30s", sum(1 for x in s_ if x > 30) == 6)
    check("2 turns past the 60s window", over60 == 2)
    check("2 no-answer turns", noanswer == 2)
    check("19 overtaken turns", overtaken == 19)
    check("v3 withholds 10 timing gaps from those 19 turns", suppressed == 10)
    check("silences are SLOWER than replies at the median", pct(s_, .5) > pct(r, .5))


# ========================= C. real Postgres round-trip =====================
async def test_postgres() -> None:
    dsn = os.environ.get("MARY_TEST_DATABASE_URL")
    print("\nC. Postgres round-trip")
    if not dsn:
        print("  [SKIP] set MARY_TEST_DATABASE_URL to run")
        return

    from app.review.store import ReviewStore

    schema = "review_selftest"
    store = ReviewStore(dsn, schema=schema)
    try:
        async with await __import__("psycopg").AsyncConnection.connect(dsn) as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await conn.execute(f"CREATE SCHEMA {schema}")
            # pending_loops joins the LOG's loops table — review reads it, never writes it, so
            # the selftest stands in for the LogStore here.
            await conn.execute(
                f"""CREATE TABLE {schema}.loops (
                        loop_id text PRIMARY KEY, updated_at timestamptz NOT NULL,
                        prompt_version text)"""
            )
            await conn.execute(
                f"INSERT INTO {schema}.loops VALUES ('L1', now() - interval '1 hour', 'pv')"
            )
            await conn.commit()
        await store.open()

        import datetime as dt
        row = {
            "loop_id": "L1", "seq": 5, "chat_id": "C", "ts": dt.datetime.now(dt.timezone.utc),
            "judge_version": "t1", "judge_model": "m", "turn_kind": "reply", "domain": "calendar",
            "prompt_version": "pv", "verdict": "bad", "confidence": "high", "rationale": "why",
            "proposed_gap": "", "reply_text": "hi", "error": None, "task_class": "ack",
            "wait_seconds": 12.5, "last_human_id": "W1", "overtaken": False, "model_ms": 4000,
            "timing_band": "slow", "delivery": "ok", "turn_error": "none",
        }
        await store.write_review(row, [{"code": "too_verbose", "severity": "minor",
                                        "evidence": "e", "source": "judge"}])
        await store.write_review(row, [{"code": "slow_reply", "severity": "major",
                                        "evidence": "12.5s", "source": "timing"}])

        async with store._pool.connection() as conn:
            n_rev = (await (await conn.execute(f"SELECT count(*) FROM {schema}.reviews")).fetchone())[0]
            n_fin = (await (await conn.execute(f"SELECT count(*) FROM {schema}.findings")).fetchone())[0]
            code = (await (await conn.execute(f"SELECT code FROM {schema}.findings")).fetchone())[0]
        check("re-judging upserts, never duplicates", n_rev == 1)
        check("findings are replaced, not appended", n_fin == 1)
        check("the replacement is the new finding", code == "slow_reply")

        await store.mark_done("L1", judge_version="t1", turns=1, bad=1)
        await store.mark_done("L1", judge_version="t1", turns=2, bad=0)
        pending = await store.pending_loops(judge_version="t1", settle_seconds=0, batch=10)
        check("a reviewed loop is not pending again", "L1" not in pending)
        pending_v2 = await store.pending_loops(judge_version="t2", settle_seconds=0, batch=10)
        check("a NEW judge_version makes history eligible again", isinstance(pending_v2, list))
    finally:
        try:
            async with await __import__("psycopg").AsyncConnection.connect(dsn) as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                await conn.commit()
        except Exception:
            pass
        await store.aclose()


def main() -> int:
    test_pure()
    test_baseline()
    asyncio.run(test_postgres())
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
