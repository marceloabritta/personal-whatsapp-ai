#!/usr/bin/env node
// ============================================================================
//  Self-test for the conversation language pin (card 3ec5be77,
//  "Assistant switches languages mid-conversation").
//
//  The bug this exists to prevent (2026-07-28, production, ~21:45–21:51):
//  the reply-language rail `ctx.lang` was re-derived from the router LLM's
//  per-turn `lang` every turn (`server.js:624 ctx.lang = reply.lang || ctx.lang`)
//  and reset on every fresh `@mary` turn (`:427`), with the drifted value then
//  re-persisted (`:511`). Nothing captured the language the conversation OPENED
//  in, so the router's per-turn `lang` (read from a 30-message sliding window
//  that no longer contained the opening message) flipped EN↔PT inside ONE
//  conversation. The owner saw "_Added to your list:_" and "_Adicionei à sua
//  lista:_" in the same thread.
//
//  The fix adds a pure rails module `lib/lang.js` that owns the pin policy:
//    - resolveTurnLang(pinnedLang, routerLang): an ongoing conversation HOLDS
//      its opening language; only the first turn of a NEW conversation (no pin)
//      adopts the router's detection. Default "en".
//    - shouldForceTranslateSay(sayLang, targetLang): whether the router's
//      free-form `say` prose must be force-translated (only the en↔pt residual
//      that localizeBody passes through untouched).
//
//  This test imports that module and drives its two pure functions plus a
//  faithful replay of the production flip through a tiny mock marker store.
//  It FAILS today because `lib/lang.js` does not exist yet (the import throws);
//  it PASSES once the Coding column adds the module.
//
//  No network, no keys — both functions are pure and deterministic. The QUALITY
//  of the forced say-translation rides on a live model call and is NOT tested
//  here (see PLAN.md "What cannot be caught offline"); only the gate that
//  decides WHETHER to translate is asserted.
//
//  Run:  node scripts/lang-pin-selftest.mjs
// ============================================================================
import {
  resolveTurnLang,
  shouldForceTranslateSay,
} from "../secretary/1. Orchestrator/lib/lang.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// --- Part A: the pin/hold rule (resolveTurnLang) ----------------------------
console.log("Part A — resolveTurnLang(pinnedLang, routerLang)");

check(
  "A1. new conversation opening in EN adopts EN",
  resolveTurnLang(null, "en") === "en"
);
check(
  "A2. new conversation opening in PT adopts PT",
  resolveTurnLang(null, "pt") === "pt"
);
check(
  "A3. pinned EN HOLDS even when the router says PT  <-- THE BUG",
  resolveTurnLang("en", "pt") === "en"
);
check(
  "A4. pinned PT holds even when the router says EN (symmetric)",
  resolveTurnLang("pt", "en") === "pt"
);
check(
  "A5. continuation with no fresh detection holds the pin",
  resolveTurnLang("en", null) === "en"
);
check(
  "A6. pinned value is trimmed + lowercased ('  PT ' -> 'pt')",
  resolveTurnLang(" PT ", "EN") === "pt"
);
check(
  "A7. adopted router value is normalized too ('PT' -> 'pt')",
  resolveTurnLang(null, "PT") === "pt"
);

// --- Part B: replay the production flip through a mock marker store ----------
// Mirrors exactly what the fix does per turn (PLAN.md "Tests to write / Part B"):
//   1. pinned = marker?.openingLang || null       (read BEFORE any clear)
//   2. lang = resolveTurnLang(pinned, routerLang); if (!pinned) pinned = lang;
//   3. on listen/execute -> marker = { openingLang: pinned }; on done -> null
console.log("\nPart B — replay the 21:45->21:51 flip (REPLICATION.md)");

let marker = null; // the whole "conversation" state: a persisted marker or nothing
function runTurn(routerLang, next) {
  const pinnedBefore = marker?.openingLang || null; // step 1: read before clear
  let pinned = pinnedBefore;
  const lang = resolveTurnLang(pinned, routerLang); // step 2
  if (!pinned) pinned = lang;
  if (next === "listen" || next === "execute") {
    marker = { openingLang: pinned }; // step 3: keep the conversation open
  } else if (next === "done") {
    marker = null; // conversation closed -> pin dropped
  }
  return lang;
}

// Turn 1 — brand-new conversation, router detects EN, ends listen.
const t1 = runTurn("en", "listen");
check("B1. turn 1 new EN -> reply 'en' and pins the opener", t1 === "en");
check("   B1b. marker now pinned 'en'", marker?.openingLang === "en");

// Turn 2 — fresh @mary (session cleared), router now leans PT, ends listen.
const t2 = runTurn("pt", "listen");
check(
  "B2. turn 2 fresh @mary routerLang 'pt' -> HELD 'en'  <-- the regression",
  t2 === "en"
);

// Turn 3 — another fresh @mary, router still PT, ends listen.
const t3 = runTurn("pt", "listen");
check("B3. turn 3 fresh @mary routerLang 'pt' -> still HELD 'en'", t3 === "en");

// Turn 4 — fresh @mary, router back to EN, ends done -> conversation closes.
const t4 = runTurn("en", "done");
check("B4. turn 4 ends 'done' -> reply 'en'", t4 === "en");
check("   B4b. done clears the marker (pin dropped)", marker === null);

// Turn 5 — a genuinely NEW conversation (no marker), router detects PT.
const t5 = runTurn("pt", "listen");
check(
  "B5. turn 5 NEW conversation routerLang 'pt' -> 'pt' (pin does NOT outlive close)",
  t5 === "pt"
);

// --- Part C: the say force-translate GATE (shouldForceTranslateSay) ----------
console.log("\nPart C — shouldForceTranslateSay(sayLang, targetLang)");

check(
  "C1. pt say under en target -> true (en<->pt residual)",
  shouldForceTranslateSay("pt", "en") === true
);
check(
  "C2. en say under pt target -> true (symmetric residual)",
  shouldForceTranslateSay("en", "pt") === true
);
check(
  "C3. say already in target -> false (no call)",
  shouldForceTranslateSay("en", "en") === false
);
check(
  "C4. non-maintained source (es) -> false (localizeBody handles it)",
  shouldForceTranslateSay("es", "en") === false
);
check(
  "C5. non-maintained target (es) -> false",
  shouldForceTranslateSay("en", "es") === false
);
check(
  "C6. null source -> false",
  shouldForceTranslateSay(null, "en") === false
);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
