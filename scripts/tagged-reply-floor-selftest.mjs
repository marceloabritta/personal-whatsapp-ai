#!/usr/bin/env node
// ============================================================================
//  Self-test for the "directly-tagged @mary order must never exit silently"
//  floor (card b133fd86, "New Mary flow failed at answering a question").
//
//  The bug this exists to prevent (2026-08-01, production):
//  the deployed unified per-turn @mary call may legally return a
//  deliberate-silence envelope (say:null, execute:[], keepListening:true|false).
//  That is correct for chatter NOT addressed to Mary, but a MISFIRE when the
//  owner DIRECTLY tagged @mary on the opening turn — server.js's LISTEN and CLOSE
//  branches honoured the silence and sent nothing. The reproduction was
//  `@mary … quanto custa o denza b5 no brasil?` → total silence, with the droplet
//  log showing `TURN -> {"keepListening":true,"execute":[],"hasSay":false}`.
//
//  The fix adds a pure, additive rails predicate `needsTaggedReplyFloor(
//  { isTagged, turnIndex, hasSay, executeCount })` to router.js that owns the
//  single question "must the caller substitute a deterministic notice instead of
//  nothing?" — returning TRUE only for a directly-tagged, turnIndex-0 turn that
//  produced neither `say` nor `execute`. It is scoped to turnIndex 0 and isTagged
//  so it never disturbs a group-chat/continuation silence (isTagged=false) or a
//  post-dispatch read-back turn (turnIndex >= 1).
//
//  This test imports that predicate and asserts its full decision table. It FAILS
//  today because `needsTaggedReplyFloor` does not exist yet (the named import is
//  undefined and the first assertion throws / fails); it PASSES once the Coding
//  column adds the export.
//
//  No network, no keys — the predicate is pure and deterministic.
//
//  What this test does NOT and CANNOT assert offline: the WIRING — that
//  server.js's two silent-exit branches (LISTEN ~787, CLOSE ~807) actually CALL
//  this predicate and then `send(number, orch(ctx.lang,"noReply"), ctx.lang)`.
//  server.js self-boots (top-level `await newSettings.ready` + `app.listen`) and
//  `route()` is a live model call, so it cannot be imported and stubbed in an
//  offline, dependency-free selftest without a framework. The predicate is the
//  deterministic core of the fix and is what this test locks down; the wiring is
//  verified by code review of the two call-sites plus the human's optional live
//  acceptance check. This is the same offline boundary the garbled-say-noop card
//  accepted for a server.js-hosted behaviour.
//
//  Run:  node scripts/tagged-reply-floor-selftest.mjs
// ============================================================================
import { needsTaggedReplyFloor } from "../secretary/1. Orchestrator/router/router.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// --- Part A: the misfire — a tagged, turnIndex-0, silent turn IS floored ------
// The exact failing envelope from the reproduction. The predicate ignores
// keepListening, so both silent exits (LISTEN keepListening:true and CLOSE
// keepListening:false) resolve to the same tagged/turn-0/no-say/no-execute shape.
console.log("Part A — the misfire is floored (needsTaggedReplyFloor returns true)");

check(
  "A1. tagged, turn 0, no say, no execute (LISTEN) -> true  <-- THE REPRODUCED MISFIRE",
  needsTaggedReplyFloor({ isTagged: true, turnIndex: 0, hasSay: false, executeCount: 0 }) === true
);
check(
  "A2. tagged, turn 0, no say, no execute (CLOSE, keepListening ignored) -> true",
  needsTaggedReplyFloor({ isTagged: true, turnIndex: 0, hasSay: false, executeCount: 0 }) === true
);

// --- Part B: the guards — everything else stays UNfloored ---------------------
// These are the blast-radius cases that must NOT change behaviour.
console.log("\nPart B — everything else is not floored (needsTaggedReplyFloor returns false)");

check(
  "B1. NOT tagged (group-chat/continuation silence) -> false  <-- THE KEY REGRESSION GUARD",
  needsTaggedReplyFloor({ isTagged: false, turnIndex: 0, hasSay: false, executeCount: 0 }) === false
);
check(
  "B2. tagged but DID say something -> false (no double-send)",
  needsTaggedReplyFloor({ isTagged: true, turnIndex: 0, hasSay: true, executeCount: 0 }) === false
);
check(
  "B3. tagged but dispatched a skill -> false (skill outcome is the reply)",
  needsTaggedReplyFloor({ isTagged: true, turnIndex: 0, hasSay: false, executeCount: 1 }) === false
);
check(
  "B4. tagged post-dispatch read-back turn (turnIndex 1) -> false",
  needsTaggedReplyFloor({ isTagged: true, turnIndex: 1, hasSay: false, executeCount: 0 }) === false
);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
