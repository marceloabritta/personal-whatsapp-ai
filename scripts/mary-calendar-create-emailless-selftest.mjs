#!/usr/bin/env node
// ============================================================================
//  Self-test / regression guard — card cf6734bf "Mary flow making mistakes on
//  calendar booking".
//
//  The bug this exists to prevent (2026-07-28, "emailless-guest give-up"):
//  the @mary Calendar Actions `create` contract REQUIRED an email for every named
//  guest (`requiredWhen.create` listing `participants[].email` + the
//  `attendee_count_matches_email_count` consistency rule). That is unsatisfiable
//  when the guest's email was never in the chat, so the truthful
//  `participants:[{name:"Fernando",email:null}]` payload was rejected by
//  `checkPayload`, the orchestrator repair loop hit MAX_REPAIRS=2, and the owner
//  got the give-up string ("Não consegui acertar isso…"). This is the INVERSE of
//  the throwaway scripts/repro-mary-fernando-giveup.mjs: that proved the give-up;
//  this proves the gate now ACCEPTS the deliberate "book-without-email" payload.
//
//  It drives the REAL orchestrator gate (checkPayload / describeProblems,
//  lib/inputs.js) with the REAL Mary Calendar Actions input contract
//  (manifest.inputs) and the REAL draftFromInfo, using Fernando's two exact
//  captured payloads (REPLICATION.md). No network, no keys, no model call.
//
//  ⚠ SCOPE — WHAT THIS TEST DELIBERATELY CANNOT COVER (CONVENTIONS §5):
//  This asserts PART 1 (the gate) ONLY. PART 2 — Mary's ask-vs-book JUDGEMENT
//  (whether, on a create with a named-but-emailless guest, she ASKS the owner for
//  the email or BOOKS WITHOUT inviting) — is MODEL behaviour driven by the skill
//  rulebook fed to the router/extractor. It depends on a live model call and is
//  NOT, and cannot honestly be, asserted offline. The deterministic layer we CAN
//  and DO assert is the gate's post-conditions (below). PART 2 is confirmed only
//  by the live check named in PLAN.md's router note.
//
//  Assertions:
//    1. PART-1 ANCHOR: Fernando's exact payload PASSES checkPayload (ok, no problems)
//    2. the MAX_REPAIRS=2 loop over both captured payloads no longer gives up
//    3. book-without wiring: draftFromInfo carries Fernando by name, email:null
//    4. guard rails still hold (relaxation opened no hole):
//         - create with start_iso:null still fails
//         - create with a malformed email still fails, with an email problem
//         - edit with no event_id now PASSES the gate (card 1600b424 dropped event_id
//           from requiredWhen.{edit,delete}; the skill self-resolves the target), while
//           create still REQUIRES its date — proving only the ACT-gate for edit/delete moved
//
//  EXPECTED STATE TODAY (before the PART-1 fix): assertions 1 & 2 FAIL — that
//  failure is the point of this column. Assertions 3 & 4 PASS now and must keep
//  passing after the fix.
//
//  Run:  node scripts/mary-calendar-create-emailless-selftest.mjs
//        -> exits non-zero before PART 1, exits 0 after PART 1.
// ============================================================================
import { checkPayload } from "../secretary/1. Orchestrator/lib/inputs.js";
import { manifest, draftFromInfo } from "../secretary/3. Mary Skills/1. Calendar Actions/skill.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

const MAX_REPAIRS = 2; // server.js:321

// Fernando's two consecutive router payloads, VERBATIM from REPLICATION.md
// (14:56:44.595Z and 14:56:48.885Z). "@mary agendar" then "isso" to confirm;
// his email was never in the conversation, so the router sent email:null both times.
// Identical except duration_min (null, then 45).
const payload1 = {
  action: "create", query: null, event_id: null, title: "Almoço com Fernando",
  participants: [{ name: "Fernando", email: null }],
  start_iso: "2026-07-28T13:00:00-03:00", duration_min: null, all_day: false,
  all_day_end_iso: null, summary: "", list_mode: null, range_start_iso: null,
  range_end_iso: null, recurrence: null, location: "casa do Marcelo", virtual: false,
};
const payload2 = { ...payload1, duration_min: 45 };

// A complete create payload with every declared field at its null/default value, so
// guard cases isolate exactly ONE failing field (an absent declared field is itself
// invalid — lib/inputs.js header — which would muddy the assertion).
const createBase = {
  action: "create", query: null, event_id: null, title: null, participants: null,
  start_iso: "2026-07-28T13:00:00-03:00", duration_min: null, all_day: false,
  all_day_end_iso: null, summary: "", list_mode: null, range_start_iso: null,
  range_end_iso: null, recurrence: null, location: null, virtual: false,
};

// ---- 1. PART-1 ANCHOR: Fernando's exact payload passes the gate ------------
// FAILS at HEAD (the create gate rejects it with the two captured problems).
const g1 = checkPayload(manifest.inputs, payload1);
console.log("\n[1] checkPayload(payload1).ok       =", g1.ok);
console.log("    checkPayload(payload1).problems =", JSON.stringify(g1.problems));
check(
  "1. Fernando's emailless create PASSES the gate (ok, no problems)  <-- the give-up regression",
  g1.ok === true && Array.isArray(g1.problems) && g1.problems.length === 0
);

// ---- 2. the MAX_REPAIRS=2 loop no longer gives up --------------------------
// Same loop the repro script uses (server.js:827-834). FAILS at HEAD.
let repairs = 0;
let gaveUp = false;
for (const info of [payload1, payload2]) {
  const g = checkPayload(manifest.inputs, info);
  if (!g.ok) {
    repairs++;
    if (repairs >= MAX_REPAIRS) {
      gaveUp = true;
      break;
    }
  }
}
console.log(`\n[2] repair loop: repairs=${repairs}/${MAX_REPAIRS}, gaveUp=${gaveUp}`);
check("2. the MAX_REPAIRS=2 repair loop no longer fires the give-up", gaveUp === false);

// ---- 3. book-without wiring: Fernando carried by name, email null ----------
// draftEmails / draftUninvited are module-private (skill.js:753-760); replicate their
// one-line logic here rather than exporting them, to keep the product change minimal.
const draftEmails = (d) => (d.participants || []).map((p) => p?.email).filter(Boolean);
const draftUninvited = (d) => (d.participants || []).filter((p) => !p.email && p.name).map((p) => p.name);

const stubCtx = { owner: "Marcelo", contact: "Fernando" };
const draft = draftFromInfo(stubCtx, payload1);
const fernando = (draft.participants || []).find((p) => p.name === "Fernando");
console.log("\n[3] draftEmails    =", JSON.stringify(draftEmails(draft)));
console.log("    draftUninvited =", JSON.stringify(draftUninvited(draft)));
check(
  "3. book-without wiring: Fernando kept by name with email:null (no invite, but named)",
  !!fernando &&
    fernando.email == null &&
    draftEmails(draft).length === 0 &&
    draftUninvited(draft).join(",") === "Fernando"
);

// ---- 4. guard rails: the relaxation must not loosen these ------------------
// 4a. create with a missing date still fails (start_iso required + create_always_has_a_date).
const gNoDate = checkPayload(manifest.inputs, {
  ...createBase, start_iso: null, participants: [{ name: "Fernando", email: null }],
});
check("4a. create with start_iso:null still FAILS the gate", gNoDate.ok === false);

// 4b. create with a MALFORMED email still fails — the VALIDITY tier is untouched,
//     so garbage emails are rejected even though ABSENT ones are now allowed.
const gBadEmail = checkPayload(manifest.inputs, {
  ...createBase, participants: [{ name: "X", email: "notanemail" }],
});
check(
  "4b. create with a malformed email still FAILS, with an email problem",
  gBadEmail.ok === false && gBadEmail.problems.some((p) => /email/i.test(p))
);

// 4c. edit with no event_id now PASSES the gate (card 1600b424): the skill self-resolves the
//     target from the quoted invite link or start_iso+attendee email, so event_id is no longer
//     required-to-act. The CREATE guards (4a/4b) still hold — only the edit/delete ACT-gate moved.
const gEditNoId = checkPayload(manifest.inputs, {
  ...createBase, action: "edit", event_id: null, title: "New title",
});
check("4c. edit with no event_id now PASSES the gate (self-resolution restored)", gEditNoId.ok === true);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
