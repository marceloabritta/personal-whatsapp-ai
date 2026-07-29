#!/usr/bin/env node
// ============================================================================
//  Self-test for NATIVE SERVER-SIDE TOOLS in stateful @mary conversations
//  (card 6c09b8ab, "Full Frontier-Model Capability in Stateful Conversations").
//
//  The feature adds a second, TOOL-CARRYING "answer pass" alongside the untouched
//  JSON classification turn. Two internal model calls, one WhatsApp reply — the
//  model can search the web, fetch a URL already in the thread, and run real
//  computation inline, then answer in prose. See PLAN.md.
//
//  This test pins the DETERMINISTIC LAYER only — the parts that do NOT depend on a
//  live model judgement. What is explicitly NOT tested here (stated up front, per
//  CONVENTIONS §5): the model's DECISION to emit `next:"answer"` and its DECISION
//  to FIRE a tool are live judgements. An offline test cannot cover them without
//  faking the model; the live `scripts/router-selftest.mjs` and a real end-to-end
//  tool-firing turn are the human's real-spend calls (flagged in PLAN.md, not run
//  here). What we CAN and DO assert:
//
//    1. buildNativeTools(env) builds the tools array correctly (web_search /
//       web_fetch versions + names, max_uses, code_execution IFF NATIVE_CODE_EXEC,
//       [] when the feature is off).
//    2. A tool-carrying reply ([server_tool_use, web_search_tool_result, text])
//       is extracted as PROSE (readText) by the answer pass — it never falls to
//       the classification JSON parser / the "didn't understand" menu.
//    3. pause_turn resume: one pause then end_turn -> loops exactly once and
//       returns the final text; a forever-pause stops at NATIVE_MAX_TOOL_HOPS (=4)
//       with outcome "timeout".
//    4. Ceiling / timeout arithmetic: a create() that times out -> "timeout";
//       a web_search_tool_result error-object block -> "tool_error"; empty text
//       with no error -> "tool_error"; a model refusal -> "refusal" (kept SILENT
//       by the server branch — a refusal is NOT mapped to a user notice).
//    5. route() accepts the new `{"next":"answer"}` classification -> next:"answer",
//       while an unknown `{"next":"banana"}` still degrades to "done".
//
//  It FAILS TODAY, on purpose, because the feature is not built yet:
//    - `secretary/1. Orchestrator/lib/nativeTools.js` does not exist, so
//      buildNativeTools is unavailable (assertion 1).
//    - `answer()` is not exported from router.js yet (assertions 2-4).
//    - route() does not allow `"answer"` yet — it degrades to "done" (assertion 5).
//  It PASSES once the Coding column ships nativeTools.js, answer(), and the route()
//  allow-list entry. The missing pieces are reported as legible FAILED checks (the
//  imports are guarded), not as a bare module-not-found crash.
//
//  No network, no key, no framework — `ctx.anthropic.messages.create` is a
//  hand-built stub, modeled on scripts/history-selftest.mjs / lang-pin-selftest.mjs.
//
//  Run:  node scripts/native-tools-selftest.mjs
// ============================================================================
import { APIConnectionTimeoutError } from "@anthropic-ai/sdk";
import { readText } from "../secretary/1. Orchestrator/lib/llm.js";
// Namespace import: route exists today, answer() does not yet — a namespace import
// leaves a missing export as `undefined` instead of failing the whole module load,
// so assertions 2-5 can report it as a legible failed check rather than crashing.
import * as router from "../secretary/1. Orchestrator/router/router.js";

// The new lib may not exist yet — dynamic import so its absence is a failed check.
let nativeMod = null;
try {
  nativeMod = await import("../secretary/1. Orchestrator/lib/nativeTools.js");
} catch {
  /* not built yet — assertion 1 reports it */
}
const buildNativeTools = nativeMod?.buildNativeTools;
const { route, answer } = router;

// The LOCKED build decisions (PLAN.md / PREFLIGHT fold-in #6). The test encodes
// the SHIPPED defaults, so a build that drifts from them goes red.
const NATIVE_MAX_TOOL_HOPS = 4; // pause_turn resume cap
const WEB_SEARCH_MAX_USES = 5; // default max_uses on the search/fetch tool defs

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ---------------------------------------------------------------------------
//  Stub harness for the answer pass: a fake anthropic whose messages.create is
//  driven by a handler(callIndex) that returns a message or throws.
// ---------------------------------------------------------------------------
function makeAnthropic(handler) {
  const calls = [];
  return {
    calls,
    messages: {
      create: async (params, opts) => {
        const idx = calls.length;
        calls.push({ params, opts });
        return handler(idx, params, opts); // may return a msg OR throw
      },
    },
  };
}

function makeCtx(anthropic, env = { NATIVE_TOOLS: "on" }, extra = {}) {
  return {
    owner: "Marcelo",
    anthropic,
    model: "claude-sonnet-5",
    env,
    order: "what's the weather in Lisbon right now?",
    nowStr: "Wednesday, 29 July 2026, 10:00",
    contact: "Marcelo",
    lang: "en",
    ...extra,
  };
}
const TURN = { labeledTranscript: "OWNER: what's the weather in Lisbon right now?" };

// A server_tool_use block — the trailing block on a paused/answered tool turn.
const SRV_USE = { type: "server_tool_use", id: "srv_1", name: "web_search", input: { query: "weather Lisbon" } };
// A successful web_search_tool_result (array content).
const SRV_OK = {
  type: "web_search_tool_result",
  tool_use_id: "srv_1",
  content: [{ type: "web_search_result", title: "Lisbon weather", url: "https://x", encrypted_content: "…" }],
};
// A FAILED web_search_tool_result: content is an ERROR OBJECT, not results.
const SRV_ERR = {
  type: "web_search_tool_result",
  tool_use_id: "srv_1",
  content: { type: "web_search_tool_result_error", error_code: "max_uses_exceeded" },
};

const proseMsg = (text) => ({ stop_reason: "end_turn", content: [SRV_USE, SRV_OK, { type: "text", text }] });
const pauseMsg = () => ({ stop_reason: "pause_turn", content: [SRV_USE] });
const errorMsg = () => ({ stop_reason: "end_turn", content: [SRV_USE, SRV_ERR] });
const emptyMsg = () => ({ stop_reason: "end_turn", content: [{ type: "text", text: "" }] });
const refusalMsg = () => ({ stop_reason: "refusal", content: [] });

function makeTimeout() {
  // What production actually throws when the create() `{ timeout }` elapses. The
  // plan calls it "APITimeoutError" colloquially; the real SDK class is
  // APIConnectionTimeoutError. Its `.name` is left as "Error" by the SDK, so we set
  // it here — the answer pass must recognise the production error (instanceof /
  // constructor.name / name), not a fictional class.
  const e = new APIConnectionTimeoutError({ message: "Request timed out." });
  e.name = "APIConnectionTimeoutError";
  return e;
}

// ===========================================================================
//  1. buildNativeTools builds the tools array correctly.
// ===========================================================================
console.log("1 — buildNativeTools(env): the tools array shape");

if (typeof buildNativeTools !== "function") {
  check("1. buildNativeTools is exported from lib/nativeTools.js  <-- NOT BUILT YET", false);
} else {
  const on = buildNativeTools({ NATIVE_TOOLS: "on" });
  const ws = Array.isArray(on) ? on.find((t) => t.type === "web_search_20260209") : null;
  const wf = Array.isArray(on) ? on.find((t) => t.type === "web_fetch_20260209") : null;

  check("1a. NATIVE_TOOLS=on -> web_search_20260209 present, name 'web_search'", !!ws && ws.name === "web_search");
  check("1b. NATIVE_TOOLS=on -> web_fetch_20260209 present, name 'web_fetch'", !!wf && wf.name === "web_fetch");
  check(
    `1c. default max_uses honours WEB_SEARCH_MAX_USES (=${WEB_SEARCH_MAX_USES})`,
    !!ws && Number(ws.max_uses) === WEB_SEARCH_MAX_USES
  );

  const on3 = buildNativeTools({ NATIVE_TOOLS: "on", WEB_SEARCH_MAX_USES: "3" });
  const ws3 = Array.isArray(on3) ? on3.find((t) => t.type === "web_search_20260209") : null;
  check("1d. WEB_SEARCH_MAX_USES=3 flows through to max_uses", !!ws3 && Number(ws3.max_uses) === 3);

  check(
    "1e. code_execution ABSENT when NATIVE_CODE_EXEC unset (default off)",
    Array.isArray(on) && !on.some((t) => t.type === "code_execution_20260521")
  );

  const onCe = buildNativeTools({ NATIVE_TOOLS: "on", NATIVE_CODE_EXEC: "on" });
  const ce = Array.isArray(onCe) ? onCe.find((t) => t.type === "code_execution_20260521") : null;
  check("1f. code_execution_20260521 present IFF NATIVE_CODE_EXEC set, name 'code_execution'", !!ce && ce.name === "code_execution");

  check("1g. feature off (NATIVE_TOOLS absent) -> []", Array.isArray(buildNativeTools({})) && buildNativeTools({}).length === 0);
  check("1h. feature off (NATIVE_TOOLS empty) -> []", Array.isArray(buildNativeTools({ NATIVE_TOOLS: "" })) && buildNativeTools({ NATIVE_TOOLS: "" }).length === 0);
  check(
    "1i. master off overrides NATIVE_CODE_EXEC -> []",
    Array.isArray(buildNativeTools({ NATIVE_CODE_EXEC: "on" })) && buildNativeTools({ NATIVE_CODE_EXEC: "on" }).length === 0
  );
}

// ===========================================================================
//  2. A tool-carrying reply is extracted as PROSE, never degraded to the menu.
// ===========================================================================
console.log("\n2 — tool-carrying reply -> prose (never the JSON parser / menu)");

// The deterministic extractor contract the answer pass relies on: readText skips
// the server_tool_use / web_search_tool_result blocks and returns only the text.
// (This sub-check passes today; it documents the contract answer() builds on.)
check(
  "2a. readText([server_tool_use, web_search_tool_result, text]) -> the prose only",
  readText(proseMsg("It's 24°C and clear in Lisbon.")) === "It's 24°C and clear in Lisbon."
);

if (typeof answer !== "function") {
  check("2b. answer() is exported from router.js  <-- NOT BUILT YET", false);
} else {
  const anthropic = makeAnthropic(() => proseMsg("It's 24°C and clear in Lisbon."));
  const a = await answer(makeCtx(anthropic), TURN);
  // The answer pass returns the model's PROSE with outcome "ok" — it never parsed
  // JSON and never produced a "didn't understand" menu. (parseJsonReply is internal
  // to router.js and not exported, so we assert the behavioural consequence.)
  check("2b. answer() returns the tool-turn prose verbatim", a && a.text === "It's 24°C and clear in Lisbon.");
  check("2c. answer() classifies a clean tool turn as outcome 'ok'", a && a.outcome === "ok");
}

// ===========================================================================
//  3. pause_turn resume loop.
// ===========================================================================
console.log("\n3 — pause_turn resume (bounded by NATIVE_MAX_TOOL_HOPS)");

if (typeof answer !== "function") {
  check("3. answer() is exported from router.js  <-- NOT BUILT YET", false);
} else {
  // Pause once, then finish.
  const once = makeAnthropic((i) => (i === 0 ? pauseMsg() : proseMsg("Final answer after one hop.")));
  const a1 = await answer(makeCtx(once), TURN);
  check("3a. one pause_turn then end_turn -> loops exactly once (2 create calls)", once.calls.length === 2);
  check("3b. returns the FINAL text after the resume", a1 && a1.text === "Final answer after one hop.");
  check("3c. outcome 'ok' after a successful resume", a1 && a1.outcome === "ok");

  // Pause forever -> must stop at the hop cap with outcome "timeout", not run away.
  const forever = makeAnthropic(() => pauseMsg());
  const a2 = await answer(makeCtx(forever), TURN);
  check("3d. a forever-pause returns outcome 'timeout' (hop cap hit)", a2 && a2.outcome === "timeout");
  check(
    `3e. the forever-pause loop is BOUNDED (2..${NATIVE_MAX_TOOL_HOPS + 2} create calls, not infinite)`,
    forever.calls.length >= 2 && forever.calls.length <= NATIVE_MAX_TOOL_HOPS + 2
  );
}

// ===========================================================================
//  4. Ceiling / timeout / error arithmetic.
// ===========================================================================
console.log("\n4 — ceiling / timeout / error arithmetic");

if (typeof answer !== "function") {
  check("4. answer() is exported from router.js  <-- NOT BUILT YET", false);
} else {
  const timedOut = makeAnthropic(() => {
    throw makeTimeout();
  });
  const at = await answer(makeCtx(timedOut), TURN);
  check("4a. create() times out -> outcome 'timeout' (never throws out of answer)", at && at.outcome === "timeout");

  const toolErr = makeAnthropic(() => errorMsg());
  const ae = await answer(makeCtx(toolErr), TURN);
  check("4b. a web_search_tool_result ERROR-OBJECT block -> outcome 'tool_error'", ae && ae.outcome === "tool_error");

  const empty = makeAnthropic(() => emptyMsg());
  const az = await answer(makeCtx(empty), TURN);
  check("4c. empty text with no error -> outcome 'tool_error'", az && az.outcome === "tool_error");

  const refused = makeAnthropic(() => refusalMsg());
  const ar = await answer(makeCtx(refused), TURN);
  // A refusal is its OWN outcome — the server branch keeps it SILENT (does NOT map
  // it to the toolError user notice), matching the classification path (router.js:158).
  check("4d. a model refusal -> outcome 'refusal' (server keeps it silent)", ar && ar.outcome === "refusal");
}

// ===========================================================================
//  5. route() classification guard — "answer" allowed, unknown still degrades.
// ===========================================================================
console.log("\n5 — route() accepts next:'answer'; unknown still degrades to 'done'");

function routeCtx(replyJson) {
  const anthropic = makeAnthropic(() => ({ stop_reason: "end_turn", content: [{ type: "text", text: replyJson }] }));
  return {
    owner: "Marcelo",
    anthropic,
    model: "claude-sonnet-5",
    order: "who won the 2022 World Cup?",
    transcript: "OWNER: who won the 2022 World Cup?",
    nowStr: "Wednesday, 29 July 2026, 10:00",
    contact: "Marcelo",
    hasQuotedAudio: false,
    quoted: null,
    catalog: [{ id: "calendar_action", description: "schedule things", inputs: null }],
    tags: ["@mary"],
    media: null,
  };
}

if (typeof route !== "function") {
  check("5. route is exported from router.js", false);
} else {
  const rAnswer = await route(routeCtx('{"next":"answer","lang":"en"}'), TURN);
  check('5a. {"next":"answer"} classification -> next:"answer"  <-- NOT ALLOWED YET', rAnswer.next === "answer");

  const rBanana = await route(routeCtx('{"next":"banana","lang":"en"}'), TURN);
  check('5b. an UNKNOWN {"next":"banana"} still degrades to "done" (guard intact)', rBanana.next === "done");
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
