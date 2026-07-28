#!/usr/bin/env node
// ============================================================================
//  Self-test / regression guard — card 89b81cd9 "Calendar: assistant ignores a
//  guest's inline email reply".
//
//  The bug this exists to prevent (2026-07-28, "guest email reply ignored"):
//  @mary's Calendar Actions missing-email rulebook (buildExtractionRules in
//  3. Mary Skills/1. Calendar Actions/prompt.js) hardcoded awaitFrom="owner" when
//  asking for a guest's email, and offered NO branch for the common case where the
//  emailless guest is the very person in THIS chat and can answer inline. So when
//  @mary asked for the email the router emitted awaitFrom:"owner", the session was
//  persisted awaiting the owner, and the guest's inline reply (fromMe:false,
//  untagged) was DROPPED at the inbound gate (server.js:346-356) — it "just sat
//  there" until the owner nudged. The fix adds a third rulebook branch: when the
//  emailless guest is the person in this chat, ASK THEM and reply awaitFrom="contact".
//
//  This test brackets that invariant at the two DETERMINISTIC layers, importing the
//  REAL product surfaces (no network, no keys, no model call):
//    - the rulebook now AUTHORIZES the contact branch (buildExtractionRules text);
//    - the inbound gate HONOURS awaitFrom="contact" for a guest reply (lib/identity.js
//      + the gate boolean copied verbatim from server.js:346-356).
//
//  ⚠ SCOPE — WHAT THIS TEST DELIBERATELY CANNOT COVER (CONVENTIONS §5):
//  Whether the router LLM actually EMITS awaitFrom:"contact" on a live calendar
//  ask-turn where the guest is in the chat is MODEL behaviour driven by the rulebook.
//  It depends on a live model call and is NOT, and cannot honestly be, asserted
//  offline. This test asserts only the two deterministic layers AROUND that call:
//  the rulebook authorizes the branch (assertion 1), and the gate honours it
//  (assertion 4). The live behaviour is confirmed only by the human live check named
//  in PLAN.md.
//
//  Assertions:
//    1. RULEBOOK AUTHORIZATION (the fails-before / passes-after anchor): the rulebook
//       CONTAINS an awaitFrom="contact" branch. At HEAD this substring appears 0 times
//       -> this assertion FAILS today and PASSES after the fix.
//    2. OWNER BRANCH PRESERVED: the rulebook still contains awaitFrom="owner".
//    3. BOOK-WITHOUT-INVITE PRESERVED: the rulebook still authorizes email=null.
//    4. GATE CONTRACT LOCK: a guest reply (fromMe:false, untagged) with
//       awaitFrom="contact" PASSES the gate; the same reply with awaitFrom="owner" is
//       DROPPED. (Locks the contract the fix relies on.)
//
//  EXPECTED STATE TODAY (before the fix): assertion 1 FAILS — that failure is the
//  point of this column. Assertions 2, 3 & 4 PASS now and must keep passing after.
//
//  Run:  node scripts/mary-calendar-guest-email-await-selftest.mjs
//        -> exits non-zero before the fix, exits 0 after the fix.
// ============================================================================
import { buildExtractionRules } from "../secretary/3. Mary Skills/1. Calendar Actions/prompt.js";
import { matchedTagNew, isOwnMessage } from "../secretary/1. Orchestrator/lib/identity.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// The real rulebook text, built with a sample owner name (same string carried
// verbatim into the router system prompt via manifest.inputs.rulebook).
const OWNER = "Marcelo";
const rulebook = buildExtractionRules(OWNER);

// ---- 1. RULEBOOK AUTHORIZATION — the fails-before / passes-after anchor ------
// FAILS at HEAD: the string awaitFrom="contact" appears 0 times (verified by grep).
// The fix adds the guest-in-chat branch that emits it.
check(
  '1. rulebook AUTHORIZES the contact branch (contains awaitFrom="contact")  <-- the ignored-reply regression',
  rulebook.includes('awaitFrom="contact"')
);

// ---- 2. OWNER BRANCH PRESERVED ---------------------------------------------
// Passes before and after — guards the "ask the owner" path is not deleted.
check(
  '2. owner branch preserved (rulebook still contains awaitFrom="owner")',
  rulebook.includes('awaitFrom="owner"')
);

// ---- 3. BOOK-WITHOUT-INVITE PRESERVED --------------------------------------
// Passes before and after — guards the emailless-guest fix (e7c3863) is not regressed.
check(
  "3. book-without-invite preserved (rulebook still authorizes email=null)",
  rulebook.includes("email=null")
);

// ---- 4. GATE CONTRACT LOCK — the invariant the fix relies on ----------------
// The gate boolean copied VERBATIM from server.js:346-356 (only the input names are
// fed in). Returns whether the message reaches the @mary flow (`passed`) or is
// dropped (`return`). Reuses repro-gate.mjs's exact approach with the REAL matchers.
function gatePasses({ fromMe, text, session }) {
  const gateText = text; // no attachment in these cases -> gateText === text
  const tag = fromMe ? matchedTagNew(gateText) : null;
  const isTagged = !!tag;
  const isOwnMsg = isOwnMessage(text);

  const awaitFrom = session?.awaitFrom || "owner";
  let isContinuation = false;
  if (session && !isTagged && !isOwnMsg) {
    if (fromMe && (awaitFrom === "owner" || awaitFrom === "any"))
      isContinuation = true;
    else if (!fromMe && (awaitFrom === "contact" || awaitFrom === "any"))
      isContinuation = true;
  }

  // server.js:356 — `if (!isTagged && !isContinuation) return;`  (the DROP)
  return !(!isTagged && !isContinuation);
}

// The guest's inline reply: an untagged, non-fromMe message carrying his email
// (the exact message shape the owner said was ignored — repro-gate.mjs case A/C).
const guestReply = { fromMe: false, text: "rafael@medflowfin.com" };

const passesWhenAwaitingContact = gatePasses({
  ...guestReply,
  session: { awaitFrom: "contact" },
});
const droppedWhenAwaitingOwner = !gatePasses({
  ...guestReply,
  session: { awaitFrom: "owner" },
});

console.log("\n[4] gate contract:");
console.log(
  `    guest reply, awaitFrom="contact" -> ${passesWhenAwaitingContact ? "PASSES (reaches @mary)" : "DROPPED"}`
);
console.log(
  `    guest reply, awaitFrom="owner"   -> ${droppedWhenAwaitingOwner ? "DROPPED (never reaches @mary)" : "PASSES"}`
);
check(
  '4. gate contract lock: guest reply PASSES when awaitFrom="contact" and is DROPPED when awaitFrom="owner"',
  passesWhenAwaitingContact === true && droppedWhenAwaitingOwner === true
);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
