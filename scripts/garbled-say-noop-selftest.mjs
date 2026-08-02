#!/usr/bin/env node
// ============================================================================
//  Self-test for the "translate X into X" no-op guard (card bea6dea5,
//  "Mary produced a garbled, off-topic reply — hallucinated a translation task").
//
//  The bug this exists to prevent (2026-07-28, production):
//  localizeBody (server.js) could hand the cheap TRANSLATE_MODEL text that is
//  ALREADY in the target language, under a bare "translate into <lang>" prompt
//  with no "if it's already in that language, return it unchanged" clause. About
//  2/5 of the time the model replied ABOUT the contradiction ("this is already in
//  Portuguese…") instead of passing the text through — the garble the owner saw.
//  This was reachable because `force:true` (the forced `say` path in sendSay)
//  bypasses localizeBody's only early-return, and because the long-tail send()
//  path never guards an x→x translation for a non-maintained target.
//
//  The fix adds a pure rails predicate `translationNeeded(sourceLang, targetLang,
//  {force})` to lib/lang.js that owns the single question "do we call the translate
//  model at all?" — returning FALSE (caller returns the text unchanged, no LLM
//  call) when the KNOWN source equals the target, OR when the existing
//  maintained-language early-return applies. This is the deterministic layer around
//  the model call — exactly the case scripts/lang-pin-selftest.mjs:29-32 left
//  explicitly untested ("rides on a live model call and is NOT tested here").
//
//  This test imports that predicate and asserts its decision table. It FAILS today
//  because `translationNeeded` does not exist yet (the named import throws); it
//  PASSES once the Coding column adds the export.
//
//  No network, no keys — the predicate is pure and deterministic. Whether the
//  hardened prompt actually stops the REPRODUCED mislabel garble (a PT say labeled
//  `en`, forced into `pt`) rides on a live model call and is NOT tested here — that
//  is the human's one-off live acceptance run of scripts/garbled-translate-repro.mjs
//  (PLAN.md "What this test does NOT and CANNOT assert offline").
//
//  Run:  node scripts/garbled-say-noop-selftest.mjs
// ============================================================================
import { translationNeeded } from "../secretary/1. Orchestrator/lib/lang.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// --- Part A: the reproduced no-op — a body already in the target -------------
// force:true (the forced say path) must NOT translate X→X. This is the exact
// case lang-pin-selftest deliberately skipped; today force:true forces the call
// and the cheap model garbles.
console.log("Part A — the X→X no-op (translationNeeded returns false)");

check(
  "A1. pt say into pt target, force:true -> false  <-- THE REPRODUCED NO-OP",
  translationNeeded("pt", "pt", { force: true }) === false
);
check(
  "A2. en into en, force:true -> false (symmetric)",
  translationNeeded("en", "en", { force: true }) === false
);
check(
  "A3. es into es, force:false -> false (long-tail blast-radius x→x)",
  translationNeeded("es", "es", { force: false }) === false
);

// --- Part B: genuine translations must STILL happen (no over-suppression) -----
console.log("\nPart B — real translations still fire (translationNeeded true)");

check(
  "B1. en into pt, force:true -> true (real en→pt say force-translate)",
  translationNeeded("en", "pt", { force: true }) === true
);
check(
  "B2. es into en, force:false -> false (target en is maintained + not forced → early-return, behaviour preserved)",
  translationNeeded("es", "en", { force: false }) === false
);

// --- Part C: the existing early-return is preserved for no-source callers -----
// translationNeeded(undefined, l, {force}) must be byte-for-byte equivalent to the
// old inline guard `(!force && (MAINTAINED_LANGS.has(l) || l === "en"))`, so every
// current localizeBody caller (ctx.send etc., which pass no source) is unchanged.
console.log("\nPart C — no-source callers: old early-return preserved");

check(
  "C1. unknown source into pt, force:false -> false (maintained target, not forced)",
  translationNeeded(undefined, "pt", { force: false }) === false
);
check(
  "C2. unknown source into es, force:false -> true (non-maintained, not forced)",
  translationNeeded(undefined, "es", { force: false }) === true
);
check(
  "C3. unknown source into pt, force:true -> true (forced w/ unknown source: STILL calls)",
  translationNeeded(undefined, "pt", { force: true }) === true
);
// C3 documents the boundary: the reproduced MISLABEL case (unknown/wrong source
// into a maintained target) is NOT handled by this deterministic guard — it is
// mitigated by the hardened translate prompt (edit 2b), so here the guard must let
// the call through, not suppress it.

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
