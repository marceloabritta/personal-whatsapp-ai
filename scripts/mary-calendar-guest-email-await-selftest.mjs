#!/usr/bin/env node
// ============================================================================
//  Self-test / regression guard — card 89b81cd9 "Calendar: assistant ignores a
//  guest's inline email reply", REWRITTEN for the Mary overhaul (card 327be40b).
//
//  The bug this exists to prevent (2026-07-28, "guest email reply ignored"):
//  when @mary asked for a guest's email, the guest's inline reply (fromMe:false,
//  untagged) was DROPPED at the inbound gate — it "just sat there" until the owner
//  nudged. The ORIGINAL fix added an awaitFrom="contact" branch so the gate would
//  honour a contact reply.
//
//  The Mary overhaul REMOVES awaitFrom entirely and opens the continuation gate to
//  ANY sender while the orchestrator marker is OPEN (server.js: `session?.open`). So
//  the who-lock that caused this bug is gone at the root: a guest's inline reply
//  ALWAYS continues an open conversation, no per-sender branch required. This test is
//  rewritten to that contract — it preserves the SCENARIO (a guest's inline email
//  reply is honoured, not dropped) but drops every awaitFrom assertion.
//
//  It brackets the invariant at the two DETERMINISTIC layers, importing the REAL
//  product surfaces (no network, no keys, no model call):
//    - the calendar rulebook still ASKS the guest for a missing email (it did not
//      lose the guest-in-chat branch), and no longer hard-locks a sender (no awaitFrom);
//    - the inbound gate HONOURS an untagged guest reply while the marker is open (the
//      gate boolean copied verbatim from server.js).
//
//  ⚠ SCOPE — WHAT THIS TEST DELIBERATELY CANNOT COVER (CONVENTIONS §5):
//  Whether the router LLM actually asks-then-books on a live calendar turn is MODEL
//  behaviour driven by the rulebook + the certainty rule; it depends on a live model
//  call and is NOT asserted offline. This test asserts only the two deterministic
//  layers AROUND that call. The live behaviour is confirmed by the human live check.
//
//  Assertions:
//    1. GUEST-ASK BRANCH PRESERVED: the rulebook still asks the guest for a missing
//       email inline (keepListening + pendingNeed), so the scenario survives.
//    2. WHO-LOCK GONE: the rulebook no longer contains ANY awaitFrom directive — the
//       sender lock that caused the drop is removed at the root.
//    3. BOOK-WITHOUT-INVITE PRESERVED: the rulebook still authorizes email=null.
//    4. GATE CONTRACT LOCK: a guest reply (fromMe:false, untagged) PASSES the gate
//       while the marker is OPEN, and is DROPPED with no open session. (Locks the
//       open-gate contract the fix now relies on — any sender continues an open marker.)
//
//  Run:  node scripts/mary-calendar-guest-email-await-selftest.mjs
// ============================================================================
import { buildExtractionRules } from "../secretary/3. Mary Skills/1. Calendar Actions/prompt.js";
import { matchedTagNew, isOwnMessage } from "../secretary/1. Orchestrator/lib/identity.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// The real rulebook text, built with a sample owner name (same string carried verbatim into the
// router system prompt via manifest.inputs.rulebook).
const OWNER = "Marcelo";
const rulebook = buildExtractionRules(OWNER);

// ---- 1. GUEST-ASK BRANCH PRESERVED -----------------------------------------
// The scenario survives only if the rulebook still tells the model to ASK the guest for a missing
// email inline instead of guessing — now expressed as keepListening=true + a pendingNeed.
check(
  "1. rulebook still asks the guest for a missing email inline (keepListening + pendingNeed)",
  rulebook.includes("keepListening=true") && rulebook.includes("pendingNeed")
);

// ---- 2. WHO-LOCK GONE — the root of the ignored-reply bug -------------------
// The overhaul removes awaitFrom; the rulebook must carry no awaitFrom directive any more (the
// gate is opened to any sender, so no per-sender lock exists to get wrong).
check(
  "2. the who-lock is gone (rulebook contains no awaitFrom directive)",
  !rulebook.includes("awaitFrom")
);

// ---- 3. BOOK-WITHOUT-INVITE PRESERVED --------------------------------------
// Passes before and after — guards the emailless-guest fix (e7c3863) is not regressed.
check(
  "3. book-without-invite preserved (rulebook still authorizes email=null)",
  rulebook.includes("email=null")
);

// ---- 4. GATE CONTRACT LOCK — the open-gate invariant the fix relies on ------
// The gate boolean copied VERBATIM from server.js (only the input names are fed in). While the
// orchestrator marker is OPEN, an untagged non-owner message continues the conversation; with no
// open session it is dropped. This is the contract that makes a guest's inline reply reach @mary.
function gatePasses({ fromMe, text, session }) {
  const tag = fromMe ? matchedTagNew(text) : null;
  const isTagged = !!tag;
  const isOwnMsg = isOwnMessage(text);
  let isContinuation = false;
  if (session?.open && !isTagged && !isOwnMsg) isContinuation = true;
  // server.js — `if (!isTagged && !isContinuation) return;`  (the DROP)
  return !(!isTagged && !isContinuation);
}

// The guest's inline reply: an untagged, non-fromMe message carrying his email (the exact message
// shape the owner said was ignored).
const guestReply = { fromMe: false, text: "rafael@medflowfin.com" };

const passesWhenOpen = gatePasses({ ...guestReply, session: { open: true } });
const droppedWhenNoSession = !gatePasses({ ...guestReply, session: null });

console.log("\n[4] gate contract:");
console.log(`    guest reply, marker OPEN     -> ${passesWhenOpen ? "PASSES (reaches @mary)" : "DROPPED"}`);
console.log(`    guest reply, no open session -> ${droppedWhenNoSession ? "DROPPED (never reaches @mary)" : "PASSES"}`);
check(
  "4. gate contract lock: guest reply PASSES while the marker is open and is DROPPED with no open session",
  passesWhenOpen === true && droppedWhenNoSession === true
);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
