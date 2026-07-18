#!/usr/bin/env node
// ============================================================================
//  Self-test for the CALENDAR TIME FORMAT — card 4a91ee70, "Change time format
//  across chats". Written BEFORE the code, from PLAN.md §The test.
//
//  THE DECISION IT PINS
//  Every user-facing calendar clock time renders as BARE, zero-padded 24-hour
//  (`09:30`, `12:00`, `15:00`, `00:00`) — uniform, no AM/PM, no morning/afternoon
//  branching, in America/Sao_Paulo (already REPLY_TZ). This is display-only; the
//  two formatters are duplicated across the assistant (`2. Skills`) and Mary
//  (`3. Mary Skills`) calendar stacks, so BOTH copies are asserted.
//
//  Offline: no network, no API key, no Redis, no Google credentials, no framework,
//  no new dependency. FREE. Dynamic import() of the two prompt.js modules — the
//  same offline shape calendar-recurrence-selftest.mjs uses.
//
//  EXPECTED STATE TODAY: this script FAILS. `localizeTime` is not exported yet
//  (import is `undefined` → presence checks fail), and today's output is
//  `12:00 PM` / `12:00 AM` / `9:30 AM` / `3:00 PM` — AM/PM present and the morning
//  hour unpadded, so every string assertion fails too. That is the correct
//  pre-implementation state; the Coding column makes it green.
//
//  Run:  node scripts/timeformat-selftest.mjs
// ============================================================================
const ASSISTANT = await import(
  new URL("../secretary/2. Skills/1. Calendar Actions/prompt.js", import.meta.url).href
);
const MARY = await import(
  new URL("../secretary/3. Mary Skills/1. Calendar Actions/prompt.js", import.meta.url).href
);

let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) {
    failures++;
    if (detail !== undefined) console.log(`          got: ${JSON.stringify(detail)}`);
  }
}

// Call a (possibly-missing) formatter without letting a TypeError abort the run,
// so that when the export is absent EVERY assertion reports FAIL cleanly rather
// than the first undefined call throwing and hiding the rest.
function fmtTime(mod, lang, iso) {
  try {
    return mod.localizeTime(lang, iso);
  } catch (e) {
    return `THREW: ${e.message}`;
  }
}
function fmtDate(mod, lang, iso) {
  try {
    return mod.localizeDate(lang, iso);
  } catch (e) {
    return `THREW: ${e.message}`;
  }
}

// ISO instants in -03:00 (São Paulo), one per boundary the plan enumerates.
const CASES = [
  { label: "noon", iso: "2026-07-05T12:00:00-03:00", want: "12:00" },
  { label: "midnight", iso: "2026-07-05T00:00:00-03:00", want: "00:00" },
  { label: "morning", iso: "2026-07-05T09:30:00-03:00", want: "09:30" },
  { label: "afternoon", iso: "2026-07-05T15:00:00-03:00", want: "15:00" },
];

const AMPM = /\b[AP]M\b/;
const STACKS = [
  { name: "assistant (2. Skills)", mod: ASSISTANT },
  { name: "mary (3. Mary Skills)", mod: MARY },
];

// 0. The export must exist in both stacks — the "fails for the right reason" signal.
for (const { name, mod } of STACKS) {
  check(`0. ${name} exports localizeTime()`, typeof mod.localizeTime === "function");
}

for (const { name, mod } of STACKS) {
  for (const { label, iso, want } of CASES) {
    // localizeTime — bare 24-h, both locales, exactly the expected string.
    const en = fmtTime(mod, "en", iso);
    check(`${name}: localizeTime en ${label} -> ${want}`, en === want, en);
    const pt = fmtTime(mod, "pt", iso);
    check(`${name}: localizeTime pt ${label} -> ${want}`, pt === want, pt);

    // localizeDate — must END with the same bare time and contain NO AM/PM.
    const den = fmtDate(mod, "en", iso);
    check(
      `${name}: localizeDate en ${label} ends ${want}, no AM/PM`,
      typeof den === "string" && den.endsWith(want) && !AMPM.test(den),
      den
    );
    const dpt = fmtDate(mod, "pt", iso);
    check(
      `${name}: localizeDate pt ${label} ends ${want}, no AM/PM`,
      typeof dpt === "string" && dpt.endsWith(want) && !AMPM.test(dpt),
      dpt
    );
  }
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
