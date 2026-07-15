#!/usr/bin/env node
// ============================================================================
//  Self-test for the CALENDAR RECURRENCE RRULE compiler — the deterministic
//  layer of card 3696d3a5, "Calendar: recurring events (create-only)".
//
//  Written BEFORE the code, from PLAN.md §Tests. Offline: no network, no API
//  key, no Redis, no Google credentials, no framework, no new dependency. FREE.
//
//  WHAT THIS COVERS (and what it deliberately does NOT)
//  It asserts ONLY the deterministic RRULE-compile layer that the plan adds to
//  the calendar skill: `toRRule(rec, {allDay, startIso})` and its value-type
//  helper `toRRuleUntil(untilIso, allDay)`. Given a structured recurrence object
//  these produce the exact RRULE string that gets written to Google, so the
//  string is pinnable and the behaviour is a regression test, not an argument.
//
//  The model's *recognition* of a recurrence from free text ("every Monday" ->
//  {freq:"weekly",byday:["MO"]}) is model-dependent and NOT offline-testable —
//  that is the paid live end-to-end check the plan flags, not this file. Here we
//  prove the compiler is correct GIVEN a good object.
//
//  The exact cases below are enumerated in PLAN.md §Tests — built to them.
//
//  HOW IT LOADS THE COMPILER
//  Dynamic import of the skill module, the same offline dynamic-import shape
//  turn-latency-selftest.mjs uses (no network, no key; googleapis resolves from
//  secretary/node_modules). `toRRule`/`toRRuleUntil` are exported for exactly
//  this purpose.
//
//  EXPECTED STATE TODAY: this script FAILS. The compiler does not exist yet, so
//  `CAL.toRRule` / `CAL.toRRuleUntil` are `undefined`, the presence checks fail,
//  and every string assertion (which pins output only the compiler can produce)
//  fails too. That is the correct pre-implementation state — the Coding column
//  makes it green.
//
//  Run:  node scripts/calendar-recurrence-selftest.mjs
// ============================================================================
const CAL = await import(
  new URL("../secretary/2. Skills/1. Calendar Actions/skill.js", import.meta.url).href
);

let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) {
    failures++;
    if (detail !== undefined) console.log(`          got: ${JSON.stringify(detail)}`);
  }
}

// Call the (possibly-missing) exports without letting a TypeError abort the run,
// so that when the feature is absent EVERY assertion reports FAIL cleanly with a
// full count, rather than the first undefined call throwing and hiding the rest.
function rrule(rec, opts) {
  try {
    return CAL.toRRule(rec, opts);
  } catch (e) {
    return `THREW: ${e.message}`;
  }
}
function until(iso, allDay) {
  try {
    return CAL.toRRuleUntil(iso, allDay);
  } catch (e) {
    return `THREW: ${e.message}`;
  }
}

// The first occurrence — fixed so UNTIL/past-until arithmetic is deterministic.
const START = "2026-07-13T10:00:00-03:00"; // a Monday, 10:00 America/Sao_Paulo
const opts = (over = {}) => ({ startIso: START, ...over });

// ---------------------------------------------------------------------------
// 0. The exports must exist. If these two fail, the feature is simply absent —
//    that is the "fails for the right reason" signal.
// ---------------------------------------------------------------------------
check("0a. skill exports toRRule()", typeof CAL.toRRule === "function");
check("0b. skill exports toRRuleUntil()", typeof CAL.toRRuleUntil === "function");

// ---------------------------------------------------------------------------
// 1. toRRule — every v1 pattern, part order FREQ ; INTERVAL ; BYDAY ; (COUNT|UNTIL)
// ---------------------------------------------------------------------------

// Daily
{
  const r = rrule({ freq: "daily", interval: 1, byday: null, count: null, until: null }, opts());
  check("1. daily -> RRULE:FREQ=DAILY", r === "RRULE:FREQ=DAILY", r);
}

// Daily interval (INTERVAL emitted only when > 1)
{
  const r = rrule({ freq: "daily", interval: 2, byday: null, count: null, until: null }, opts());
  check("2. daily interval 2 -> RRULE:FREQ=DAILY;INTERVAL=2", r === "RRULE:FREQ=DAILY;INTERVAL=2", r);
}

// Weekly, single day
{
  const r = rrule({ freq: "weekly", interval: 1, byday: ["MO"], count: null, until: null }, opts());
  check("3. weekly on MO -> RRULE:FREQ=WEEKLY;BYDAY=MO", r === "RRULE:FREQ=WEEKLY;BYDAY=MO", r);
}

// Weekly, interval + multi-day
{
  const r = rrule(
    { freq: "weekly", interval: 2, byday: ["MO", "WE"], count: null, until: null },
    opts()
  );
  check(
    "4. weekly interval 2 on MO,WE -> ...INTERVAL=2;BYDAY=MO,WE",
    r === "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE",
    r
  );
}

// BYDAY canonicalization + dedup: input order/dupes collapse to canonical MO..SU order
{
  const r = rrule(
    { freq: "weekly", interval: 1, byday: ["WE", "MO", "WE"], count: null, until: null },
    opts()
  );
  check(
    "5. byday [WE,MO,WE] canonicalized+deduped -> BYDAY=MO,WE",
    r === "RRULE:FREQ=WEEKLY;BYDAY=MO,WE",
    r
  );
}

// Monthly day-of-month: BYDAY is ignored on monthly (no BYDAY, no ordinal)
{
  const r = rrule({ freq: "monthly", interval: 1, byday: ["MO"], count: null, until: null }, opts());
  check("6. monthly ignores byday -> RRULE:FREQ=MONTHLY", r === "RRULE:FREQ=MONTHLY", r);
}

// COUNT
{
  const r = rrule({ freq: "weekly", interval: 1, byday: ["MO"], count: 5, until: null }, opts());
  check(
    "7. weekly on MO count 5 -> ...BYDAY=MO;COUNT=5",
    r === "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=5",
    r
  );
}

// COUNT-XOR-UNTIL: when both are present COUNT wins and UNTIL is dropped (RRULE forbids both)
{
  const r = rrule(
    {
      freq: "weekly",
      interval: 1,
      byday: ["MO"],
      count: 5,
      until: "2026-09-01T00:00:00-03:00",
    },
    opts()
  );
  check(
    "8. count XOR until: COUNT kept, UNTIL dropped",
    typeof r === "string" && r.endsWith(";COUNT=5") && !r.includes("UNTIL"),
    r
  );
}

// Timed UNTIL -> UTC basic form, inclusive end of local until-day
{
  const r = rrule(
    { freq: "daily", interval: 1, byday: null, count: null, until: "2026-08-30T00:00:00-03:00" },
    opts({ allDay: false })
  );
  check(
    "9. timed until -> RRULE:FREQ=DAILY;UNTIL=20260831T025959Z",
    r === "RRULE:FREQ=DAILY;UNTIL=20260831T025959Z",
    r
  );
}

// All-day UNTIL -> DATE form (YYYYMMDD), no clock time
{
  const r = rrule(
    { freq: "daily", interval: 1, byday: null, count: null, until: "2026-08-30T00:00:00-03:00" },
    opts({ allDay: true })
  );
  check(
    "10. all-day until -> RRULE:FREQ=DAILY;UNTIL=20260830",
    r === "RRULE:FREQ=DAILY;UNTIL=20260830",
    r
  );
}

// Past-until fallback: an until at/before the first occurrence -> one-off (null)
{
  const r = rrule(
    { freq: "daily", interval: 1, byday: null, count: null, until: "2026-07-01T00:00:00-03:00" },
    opts()
  );
  check("11. past-until (before start) -> null", r === null, r);
}

// Uncompilable / degenerate -> null
{
  check("12a. toRRule(null) -> null", rrule(null, opts()) === null, rrule(null, opts()));
  check(
    "12b. unknown freq (yearly) -> null",
    rrule({ freq: "yearly", interval: 1, byday: null, count: null, until: null }, opts()) === null,
    rrule({ freq: "yearly" }, opts())
  );
  check("12c. empty object (no freq) -> null", rrule({}, opts()) === null, rrule({}, opts()));
}

// Degenerate numbers
{
  const r0 = rrule(
    { freq: "daily", interval: 0, byday: null, count: null, until: null },
    opts()
  );
  check("13a. interval 0 -> no INTERVAL= (defaults to 1)", r0 === "RRULE:FREQ=DAILY", r0);

  const rc = rrule(
    { freq: "daily", interval: 1, byday: null, count: 0, until: null },
    opts()
  );
  check("13b. count 0 (no until) -> RRULE:FREQ=DAILY only (count 0 is no count)", rc === "RRULE:FREQ=DAILY", rc);
}

// ---------------------------------------------------------------------------
// 2. toRRuleUntil — value-type-correct UNTIL token
// ---------------------------------------------------------------------------
{
  const t = until("2026-08-30T00:00:00-03:00", false);
  check("14. toRRuleUntil timed -> 20260831T025959Z", t === "20260831T025959Z", t);
}
{
  const d = until("2026-08-30T00:00:00-03:00", true);
  check("15. toRRuleUntil all-day -> 20260830 (DATE)", d === "20260830", d);
}
{
  const n = until("not-a-date", false);
  check("16. toRRuleUntil unparseable -> null", n === null, n);
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
