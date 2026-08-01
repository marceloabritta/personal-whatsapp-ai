#!/usr/bin/env node
// ============================================================================
//  Self-test for the ROUTER DEGRADED-vs-LEGIT-CLOSE classification (card 77cd6542):
//  "Router logs ordinary chit-chat as failures".
//
//  Written BEFORE the code, from PLAN.md §"Tests to write" (A–E). Offline: no
//  network, no API key, no Redis, no framework, no new dependency. FREE.
//
//  THE FIX, in one line: give route() an explicit `degraded` flag so the
//  orchestrator can tell "the router refused / produced garbage" (a real alarm)
//  from "the model deliberately closed a no-op" (ordinary chit-chat — silence it),
//  then gate both server.js `unrouted` capture sites on that flag.
//
//  WHAT THIS SUITE ASSERTS (the DETERMINISTIC layer only, per CONVENTIONS §5):
//    A. A legit chit-chat close (a parseable {next:"done", skills:[]}) is NOT
//       degraded — the shape server.js must close SILENTLY. (green before & after.)
//    B. An UNPARSEABLE reply (plain prose, no JSON) IS degraded. (RED before.)
//    C. A model REFUSAL (stop_reason:"refusal") IS degraded. (RED before.)
//    D. An execute naming only an unknown skill degrades to skills:["other"] and is
//       NOT degraded — proving a genuine router failure can never reach the
//       execute-empty capture site. (green before & after.)
//    E. server.js wiring guard (source scan, same idiom as selflearning-selftest §9):
//       `phase: "unrouted"` occurs EXACTLY ONCE and the source references
//       `reply.degraded`. (RED before: two occurrences, no `reply.degraded`.)
//
//  WHAT IT CANNOT CATCH (stated, CONVENTIONS §5): whether the LIVE model actually
//  returns the empty-close shape for the strings "obg." / "🤣🤣🤣" / "deixa pra la" /
//  "conte uma piada". That needs a real API call and is model-non-deterministic; the
//  four captured production reports in Bugs and Malfunctions/inbox/ are that evidence
//  (REPLICATION.md). No fabricated offline test stands in for it.
//
//  ⚠ WHY IT IS RED TODAY, AND WHY THAT IS THE POINT.
//  route() throws away the refused/unparseable-vs-deliberate-close signal: both the
//  degrade fallback (router.js:169-170) and a legit close return an object with NO
//  `degraded` field, and server.js branches on shape alone (no `reply.degraded`). So:
//    - B/C are RED because `reply.degraded` is `undefined` on the degrade path today
//      (the fix sets it to `true`). Read defensively via `=== true`, so a missing
//      field is a FAILING check, never a thrown script and never a false green.
//    - E is RED because server.js still has TWO `phase:"unrouted"` sites and never
//      mentions `reply.degraded` (the gate is not wired yet).
//    - A/D are GREEN today and MUST STAY green — a legit close / a degrade-to-"other"
//      must never be flagged; `!reply.degraded` holds whether the field is `undefined`
//      (today) or `false` (after the fix).
//
//  Run:  node scripts/unrouted-classification-selftest.mjs
// ============================================================================

import { readFile } from "node:fs/promises";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ---- real module, loaded by DYNAMIC import (a missing field -> undefined) ----
const RT = await import(
  new URL("../secretary/1. Orchestrator/router/router.js", import.meta.url).href
);
const { route } = RT;

// A FakeSDK whose messages.create returns a CANNED message object — exactly the
// file-relay-selftest idiom. Each case injects its own canned reply so the real
// route() runs its real parse / refusal / degrade logic over it.
class FakeSDK {
  constructor(reply) {
    this.seen = [];
    this.messages = {
      create: async (params) => {
        this.seen.push(params);
        return reply;
      },
    };
  }
}

// A ctx shaped like the real orchestrator turn ctx (mirrors file-relay-selftest's
// baseCtx). catalog:[] so the only valid id is the built-in "other" sink (router.js:93).
function baseCtx(reply, over = {}) {
  const sdk = new FakeSDK(reply);
  const ctx = {
    owner: "Marcelo",
    anthropic: sdk,
    model: "ctx-model",
    order: "obg.",
    transcript: "ME: obg.",
    nowStr: "2026-07-28 10:00",
    contact: "Laura",
    hasQuotedAudio: false,
    quoted: null,
    catalog: [],
    tags: ["@mary"],
    media: undefined,
    ...over,
  };
  return { ctx, sdk };
}

// route() must never THROW here — a thrown script would be red for the wrong reason.
// Wrap each drive so an unexpected throw surfaces as a failing check, not a crash.
async function drive(label, reply, over = {}) {
  try {
    const { ctx } = baseCtx(reply, over);
    return await route(ctx, {});
  } catch (e) {
    check(`${label} — route() must not throw (${e?.message || e})`, false);
    return {}; // an empty object -> the field checks below fail cleanly, never crash
  }
}

// ============================================================================
//  A. A legit chit-chat close is NOT degraded. (green before & after.)
//  A parseable {next:"done", say:null, skills:[], lang:"pt"} — the model
//  deliberately closing a no-op. This is the shape server.js must close SILENTLY;
//  it must NEVER carry degraded.
// ============================================================================
console.log("\n=== A. legit chit-chat close is NOT degraded (parseable done) ===\n");

const aReply = {
  stop_reason: "end_turn",
  content: [{ type: "text", text: '{"keepListening":false,"say":null,"execute":[],"lang":"pt"}' }],
};
const A = await drive("A", aReply);
check("A.1  parseable close -> reply.keepListening === false", A.keepListening === false);
check("A.2  parseable close -> reply.execute is empty", Array.isArray(A.execute) && A.execute.length === 0);
check('A.3  language preserved from the parse -> reply.lang === "pt"', A.lang === "pt");
check("A.4  a LEGIT close is NOT flagged -> !reply.degraded (the silent-close guard)", !A.degraded);

// ============================================================================
//  B. An UNPARSEABLE reply IS degraded. (RED before — degraded is undefined today.)
//  Plain prose, no JSON: parseJsonReply returns null -> route.js:169 fallback.
// ============================================================================
console.log("\n=== B. unparseable reply IS degraded (plain prose, no JSON) ===\n");

const bReply = {
  stop_reason: "end_turn",
  content: [{ type: "text", text: "sorry, I can't help with that" }],
};
const B = await drive("B", bReply);
check("B.1  unparseable -> degrades to a CLOSE (keepListening:false, no loop)", B.keepListening === false);
check("B.2  unparseable -> reply.execute is empty", Array.isArray(B.execute) && B.execute.length === 0);
check("B.3  unparseable IS the schema-drift alarm -> reply.degraded === true", B.degraded === true);

// ============================================================================
//  C. A model REFUSAL IS degraded. (RED before.)
//  stop_reason:"refusal", empty content: parsed stays null -> route.js:169 fallback.
// ============================================================================
console.log("\n=== C. model refusal IS degraded (stop_reason:refusal) ===\n");

const cReply = { stop_reason: "refusal", content: [] };
const C = await drive("C", cReply);
check("C.1  refusal -> degrades to a CLOSE (keepListening:false)", C.keepListening === false);
check("C.2  refusal IS the schema-drift alarm -> reply.degraded === true", C.degraded === true);

// ============================================================================
//  D. An execute naming only an unknown skill is NOT degraded. (green before & after.)
//  A PARSED reply: {execute:["nope_skill"]} -> "nope_skill" is not a valid catalog id, so
//  route() FILTERS it out and returns execute:[] (a parsed reply, degraded:false). server.js
//  then closes cleanly with no menu/capture. This proves a genuine degrade can never arrive at
//  the execute-empty close as an alarm.
// ============================================================================
console.log("\n=== D. execute-with-only-unknown-skill filters to [], NOT degraded ===\n");

const dReply = {
  stop_reason: "end_turn",
  content: [{ type: "text", text: '{"keepListening":true,"execute":["nope_skill"],"lang":"pt"}' }],
};
const D = await drive("D", dReply);
check("D.1  a parsed execute reply is NOT degraded -> !reply.degraded", !D.degraded);
check(
  "D.2  an unknown skill is filtered out -> reply.execute is empty",
  Array.isArray(D.execute) && D.execute.length === 0
);
check("D.3  the reply parsed cleanly -> keepListening carried through (true)", D.keepListening === true);

// ============================================================================
//  E. server.js wiring guard (source scan — same idiom as selflearning-selftest §9).
//  The classification decision lives in server.js; assert it is actually rewired.
// ============================================================================
console.log("\n=== E. server.js wiring guard (source scan) ===\n");

const serverSrc = await readFile(
  new URL("../secretary/1. Orchestrator/server.js", import.meta.url),
  "utf8"
);
const unroutedCount = (serverSrc.match(/phase:\s*"unrouted"/g) || []).length;
check(
  `E.1  phase:"unrouted" occurs EXACTLY ONCE (only the degraded done branch remains; two today) — found ${unroutedCount}`,
  unroutedCount === 1
);
check(
  "E.2  server.js references `reply.degraded` (the capture gate is actually wired)",
  serverSrc.includes("reply.degraded")
);

// ---- verdict ----------------------------------------------------------------
console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
