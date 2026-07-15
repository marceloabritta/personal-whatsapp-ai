#!/usr/bin/env node
// ============================================================================
//  Self-test for TURN LATENCY — the regression test for card 9af6967a,
//  "Calendar skill taking too long to reply".
//
//  Written BEFORE the code, from PLAN.md §Tests. Offline: no network, no API key,
//  no Redis, no Google credentials, no framework, no new dependency. FREE.
//
//  THE REGRESSION THIS EXISTS TO CATCH
//  A fresh tagged calendar order went from ~6.5s to 16-23s to first reply, as unbroken
//  silence. ROOT_CAUSE.md found TWO causes, both in the SHARED request path:
//    1. Nobody sets `thinking`. claude-sonnet-5 runs adaptive thinking ON BY DEFAULT, and
//       readText() filters the thinking block straight onto the floor. We wait for it, pay
//       for it, delete it. ~4.6s of every 16s turn.
//    2. The router call and the calendar extraction call are TWO sequential blocking
//       round-trips that read the SAME transcript. Per-turn latency is linear in the number
//       of round-trips, and each is 4-8s.
//  The fix is two steps, and THE ORDER IS SAFETY-CRITICAL (PLAN.md): step 1 (thinking off)
//  MUST ship before step 2 (the merge). Step 2 with thinking still on measured a p90 of
//  41.5s — dramatically WORSE than today. This file's sections mirror that split, and
//  `TURN_SELFTEST_STEP=1` runs the STEP-1 section alone so step 1 can be verified green
//  before step 2 is started.
//
//  HOW IT MEASURES TIME. The fake Anthropic SLEEPS a fixed LLM_MS (default 1500ms) before
//  every reply. That is the stand-in for the provider's real 4-8s round-trip, and it makes
//  time-to-first-reply DETERMINISTIC: ROOT_CAUSE.md §4 measured our own overhead at 1ms, so
//  a turn takes exactly (calls x LLM_MS) plus noise. The causal claim of the whole card —
//  "latency is linear in the number of round-trips" — is therefore not an argument here, it
//  is an executed assertion. The counts (T2.1/T2.3/T2.4) are the primary evidence; the
//  wall-clock bounds (T2.2/T2.3) are the same claim stated in seconds, which is the unit the
//  human actually complained in.
//
//  HOW IT WORKS (harness copied wholesale from scripts/calendar-create-selftest.mjs)
//  The REAL server.js is booted as a child process and driven over its REAL /webhook, with
//  the reproduction's OWN orders (REPLICATION.md S1/S3/S4, verbatim). Only three things are
//  faked, all locally:
//    - Anthropic  : a local HTTP server. Sleeps LLM_MS, then returns a PINNED output per
//                   call. It also RECORDS EVERY REQUEST BODY, which is what makes `thinking`
//                   and `output_config` assertable.
//    - Evolution  : a local HTTP server. Records every message the assistant sends, and
//                   TIMESTAMPS the first one of each turn.
//    - googleapis : a RECORDING STUB injected via an ESM loader hook written to a temp dir.
//                   Google is never contacted.
//
//  ⚠ THE TRAP — READ BEFORE YOU TOUCH `kindOf`.
//  The fake model must know WHICH call it is answering, and after step 2 the merged call
//  carries NO output_config at all — so a sniffer that is handed only the schema cannot
//  see the absence of one. `kindOf` therefore takes the WHOLE REQUEST BODY, identifies the
//  merged call POSITIVELY (no output_config + the skill catalog in the system prompt), and
//  THROWS on anything it does not recognise. It never falls through to a default: a sniffer
//  that guesses does not fail loudly, it reports GREEN while asserting nothing.
//
//  WHAT IS RED TODAY, AND WHY THAT IS THE POINT
//    STEP-1  T1.0-T1.5   RED  — lib/llm.js has no withThinkingDefault(); the one
//                              `new Anthropic(` in server.js is unwrapped; no request
//                              carries `thinking`.
//    STEP-2  T2.1-T2.5   RED  — a fresh tagged create still makes 3 sequential calls (2 on
//                              a complete order), because the router and the extractor are
//                              still two separate round-trips.
//            T2.7        RED  — 1. Orchestrator/lib/inputs.js does not exist.
//            T2.10       RED  — the calendar manifest has no `inputs` declaration.
//            T2.6        GREEN today and it must STAY green — it is a guard-rail, not a
//                              regression. See its own note below.
//    CAPABILITY T3.1-T3.5 GREEN today and they must STAY GREEN. A red T3 means the fix
//                              bought speed by removing capability. STOP.
//  The T3 section going green on the SAME harness that reports the T2 reds is what proves
//  the reds are the PRODUCT's, not the harness's: the same fake model, the same webhook, the
//  same stub, driving the same skill end to end.
//
//  THE LIMIT, stated plainly. The model's outputs are PINNED, not re-derived. This suite
//  proves the ORCHESTRATION — how many round-trips a turn takes, what is on the wire, what
//  the plain-code gate decides, and that no capability was lost. It CANNOT prove a live
//  Claude still extracts the right event from a terse order under the merged prompt. That
//  half is accuracy, it is not catchable offline (CONVENTIONS §5), and it has its own
//  script: scripts/calendar-extraction-livetest.mjs (live, opt-in, human-gated).
//
//  Run:  node scripts/turn-latency-selftest.mjs                     (everything; free)
//        TURN_SELFTEST_STEP=1 node scripts/turn-latency-selftest.mjs (STEP-1 section only)
//        LLM_MS=400 node scripts/turn-latency-selftest.mjs           (faster; counts still hold)
//        TURN_SELFTEST_DEBUG=1 node scripts/turn-latency-selftest.mjs (child stdout)
// ============================================================================
import http from "node:http";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, readFile, readdir } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import os from "node:os";

const DEBUG = process.env.TURN_SELFTEST_DEBUG === "1";
const ONLY_STEP = process.env.TURN_SELFTEST_STEP || "";
const LLM_MS = Number(process.env.LLM_MS || 1500);
const REPO = fileURLToPath(new URL("..", import.meta.url));
const ORCH = fileURLToPath(new URL("../secretary/1. Orchestrator/", import.meta.url));
const SERVER = path.join(ORCH, "server.js");
const CAL_DIR = fileURLToPath(
  new URL("../secretary/2. Skills/1. Calendar Actions/", import.meta.url)
);
const [PORT_APP, PORT_LLM, PORT_EVO] = [4320, 4321, 4322];

let failures = 0;
let skipped = 0;
// `section`: 1 = STEP-1, 2 = STEP-2, 3 = CAPABILITY, 0 = harness integrity (always runs).
// TURN_SELFTEST_STEP=1 evaluates the STEP-1 section alone — the tree is expected to be red
// on step 2 at that point, and drowning step 1's verdict in those reds helps nobody.
function check(name, cond, section = 0) {
  if (ONLY_STEP === "1" && (section === 2 || section === 3)) {
    skipped++;
    return;
  }
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ============================================================================
//  SECTION STEP-1 — the model's undeclared private thinking, killed at the one shared door.
//
//  lib/llm.js gains ONE new export, withThinkingDefault(client), and server.js wraps its
//  single Anthropic client in it. All 16 messages.create call sites inherit the fix, and so
//  does a skill written next month. T1.0-T1.4 are unit assertions on the wrapper; T1.5 is
//  the source lint that keeps the fix ALIVE; T1.1 is the end-to-end truth and is asserted at
//  the very bottom, over every request body the fake Anthropic saw all run.
// ============================================================================
console.log("\n=== STEP-1: thinking is disabled at the shared client ===\n");

const LLM = await import(new URL("../secretary/1. Orchestrator/lib/llm.js", import.meta.url).href);
const withThinkingDefault = LLM.withThinkingDefault;
const HAS_WRAPPER = typeof withThinkingDefault === "function";

if (!HAS_WRAPPER) {
  console.log(
    "  ..    lib/llm.js does not export withThinkingDefault() — T1.0-T1.4 below are RED\n" +
      "        for that reason, and for no other. They are unit assertions on a function\n" +
      "        that does not exist yet.\n"
  );
}

// A CLASS INSTANCE on purpose. `new Anthropic()` is one, and an object spread would drop
// its prototype methods and getters — which is why the fix must use a Proxy. T1.4 is the
// assertion that catches a spread-based implementation; this fake is what gives it teeth.
class FakeSDK {
  constructor(seen) {
    this.apiKey = "sk-fake";
    this.messages = {
      create: async (params) => {
        seen.push(params);
        return { id: "m", content: [{ type: "text", text: "{}" }] };
      },
      countTokens: async () => ({ input_tokens: 7 }),
    };
  }
  get baseURL() {
    return "https://api.anthropic.com";
  }
  async models() {
    return "reachable";
  }
}

let t1 = { def: null, esc: null, pass: null, surface: null, err: "" };
if (HAS_WRAPPER) {
  try {
    // T1.0 — the default is injected when the caller passes no `thinking`.
    const seenA = [];
    const a = withThinkingDefault(new FakeSDK(seenA));
    await a.messages.create({ model: "claude-sonnet-5", max_tokens: 200, messages: [] });
    t1.def = seenA[0]?.thinking?.type === "disabled";

    // T1.2 — the escape hatch: a caller that genuinely wants reasoning keeps it.
    const seenB = [];
    const b = withThinkingDefault(new FakeSDK(seenB));
    await b.messages.create({
      model: "claude-sonnet-5",
      max_tokens: 200,
      messages: [],
      thinking: { type: "adaptive" },
    });
    t1.esc = seenB[0]?.thinking?.type === "adaptive";

    // T1.3 — everything else passes through BYTE-IDENTICAL. A wrapper that silently
    // dropped `output_config` would break every skill's JSON, and it would do it quietly.
    const seenC = [];
    const c = withThinkingDefault(new FakeSDK(seenC));
    const sent = {
      model: "claude-sonnet-5",
      max_tokens: 4096,
      system: "you are a calendar assistant",
      output_config: { format: { type: "json_schema", schema: { type: "object" } } },
      messages: [{ role: "user", content: "hi" }],
    };
    await c.messages.create({ ...sent });
    const got = seenC[0] || {};
    const stripThinking = (o) => {
      const { thinking, ...rest } = o;
      return rest;
    };
    t1.pass = JSON.stringify(stripThinking(got)) === JSON.stringify(sent);

    // T1.4 — the rest of the SDK surface is still reachable THROUGH the proxy: a prototype
    // getter, a prototype method, an own property, and a non-`create` member of `messages`.
    const seenD = [];
    const d = withThinkingDefault(new FakeSDK(seenD));
    const tok = await d.messages.countTokens({});
    t1.surface =
      d.baseURL === "https://api.anthropic.com" &&
      (await d.models()) === "reachable" &&
      d.apiKey === "sk-fake" &&
      tok?.input_tokens === 7;
  } catch (e) {
    t1.err = e?.message || String(e);
  }
}
const why = t1.err
  ? ` [wrapper THREW: ${t1.err}]`
  : HAS_WRAPPER
  ? ""
  : " [lib/llm.js exports no withThinkingDefault()]";

check(`T1.0  the wrapper INJECTS thinking:{type:"disabled"} when the caller passes none${why}`,
  t1.def === true, 1);
check(`T1.2  an explicit \`thinking\` from a caller is NOT clobbered — the escape hatch${why}`,
  t1.esc === true, 1);
check(`T1.3  model/max_tokens/system/messages/output_config pass through BYTE-IDENTICAL${why}`,
  t1.pass === true, 1);
check(`T1.4  the non-\`messages\` SDK surface survives the wrap (Proxy, not a spread)${why}`,
  t1.surface === true, 1);

// ---- T1.5 — THE SOURCE LINT. This is the assertion that keeps the fix alive. -------------
// A skill added next month that builds its own `new Anthropic()` would reintroduce the bug
// invisibly — every behavioural test above would still pass, and the product would silently
// be slow again. Product source only: `scripts/` builds its own raw clients on purpose
// (they are fixtures, and tasks-addressed-selftest.mjs mirrors production by hand).
async function walkJs(dir) {
  const out = [];
  for (const e of await readdir(dir, { withFileTypes: true })) {
    if (e.name === "node_modules" || e.name.startsWith(".")) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...(await walkJs(p)));
    else if (/\.(js|mjs)$/.test(e.name)) out.push(p);
  }
  return out;
}
// Comments are stripped before every lint below: a `new Anthropic(` or a `start_iso` quoted in
// a comment is documentation, not code, and linting prose is how a lint gets "fixed" by
// deleting a comment.
//
// ⚠ THIS IS A SCANNER AND NOT A REGEX, AND IT HAS TO BE.
// The obvious `s.replace(/\/\*[\s\S]*?\*\//g, "")` is WRONG on this codebase and it fails
// SILENTLY, in the worst possible direction. server.js's header comment contains the line
//     //  Scans "2. Skills/*/skill.js", calls the ROUTER …
// whose `/*` opens a block comment as far as a regex is concerned. The scan then runs to the
// next `*/` and swallows ~50 lines of real code — INCLUDING server.js:59, the one
// `new Anthropic(` in the product. T1.5 reported "0 found" and would have gone GREEN the
// moment anyone wrote `withThinkingDefault(` anywhere, while the real client sat unwrapped.
// A lint that cannot see the code is worse than no lint: it certifies.
// So: walk the source, tracking strings, template literals, escapes and both comment forms.
function stripComments(src) {
  let out = "";
  let i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    const d = src[i + 1];
    if (c === "\\") { out += "  "; i += 2; continue; } // an escape (incl. `\/` in a regex)
    if (c === "/" && d === "/") { while (i < n && src[i] !== "\n") i++; continue; }
    if (c === "/" && d === "*") {
      i += 2;
      while (i < n && !(src[i] === "*" && src[i + 1] === "/")) i++;
      i += 2;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      const q = c;
      out += c;
      i++;
      while (i < n) {
        if (src[i] === "\\") { out += src[i] + (src[i + 1] ?? ""); i += 2; continue; }
        out += src[i];
        if (src[i] === q) { i++; break; }
        i++;
      }
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

const SECRETARY = fileURLToPath(new URL("../secretary/", import.meta.url));
const productFiles = (await walkJs(SECRETARY)).filter((f) => !f.includes("node_modules"));
const clientSites = [];
for (const f of productFiles) {
  const src = stripComments(await readFile(f, "utf8"));
  for (const line of src.split("\n"))
    if (/new\s+Anthropic\s*\(/.test(line))
      clientSites.push({ file: path.relative(REPO, f), line: line.trim() });
}
const wrappedSites = clientSites.filter((s) => /withThinkingDefault\s*\(/.test(s.line));
console.log(
  `        ${clientSites.length} \`new Anthropic(\` in product source; ` +
    `${wrappedSites.length} wrapped in withThinkingDefault(`
);
for (const s of clientSites) console.log(`        - ${s.file}: ${s.line}`);
check(
  "T1.5  SOURCE LINT — exactly ONE `new Anthropic(` in secretary/, and it is wrapped in " +
    "withThinkingDefault(",
  clientSites.length === 1 && wrappedSites.length === 1,
  1
);

// ============================================================================
//  SECTION STEP-2 (static) — the lints and the plain-code gate. No server, no model.
// ============================================================================
console.log("\n=== STEP-2 (static): the orchestrator stays generic; the gate is plain code ===\n");

// ---- T2.6 — THE GENERICITY LINT. The orchestrator must not know what a calendar is. ------
//
// ⚠ SCOPE, AND WHY IT IS NOT "the word calendar".
// PLAN.md words T2.6 as "no file under 1. Orchestrator/ contains CAL_SCHEMA, start_iso,
// participants or THE WORD CALENDAR outside a comment". The last clause is not writable
// against HEAD and must not be made writable: the orchestrator legitimately handles Google
// Calendar LINKS in WhatsApp messages, and always has —
//    lib/whatsapp.js   findCalendarLink() / quoted.calendarLink
//    router/router.js  hasQuotedCalendarLink: !!quoted?.calendarLink
//    router/prompt.js  the quoted-calendar-link ROUTING RULE, which PLAN.md File 4 says to
//                      keep "unchanged and in place"
// That is message-shape handling, not schema knowledge, it predates this card, and a lint on
// the bare word would be red forever — whose only available "fix" is to DELETE a load-bearing
// routing rule. So the lint is on the thing the constraint is actually about: the calendar
// skill's FIELD NAMES and its SCHEMA, plus any import reaching into 2. Skills/. That is
// exactly what a builder would add if he took the forbidden shortcut — importing CAL_SCHEMA
// into the router to build the merged prompt with output_config — and it is zero at HEAD, so
// it is GREEN today and must STAY green. It is a guard-rail, not a regression. (Reported to
// the manager as a deviation from PLAN.md's literal wording. Do not "restore" the word.)
const FORBIDDEN = [
  "CAL_SCHEMA",
  "start_iso",
  "duration_min",
  "all_day",
  "all_day_end_iso",
  "list_mode",
  "range_start_iso",
  "range_end_iso",
  "participants",
];
const orchFiles = await walkJs(ORCH);
const leaks = [];
for (const f of orchFiles) {
  const src = stripComments(await readFile(f, "utf8"));
  for (const word of FORBIDDEN)
    if (new RegExp(`\\b${word}\\b`).test(src)) leaks.push(`${path.relative(REPO, f)}: ${word}`);
  if (/from\s+["'][^"']*2\.\s*Skills/.test(src))
    leaks.push(`${path.relative(REPO, f)}: imports from "2. Skills/"`);
}
check(
  `T2.6  GENERICITY LINT — no calendar field name or schema anywhere under 1. Orchestrator/ ` +
    `(${leaks.length} leak${leaks.length === 1 ? "" : "s"}${leaks.length ? ": " + leaks.join(", ") : ""})`,
  leaks.length === 0,
  2
);

// ---- T2.7 — THE GATE, as plain-code unit assertions. No server, no model. ----------------
// checkPayload(inputs, info) -> { shapeOk, ok, problems } and it reports WHICH TIER failed:
//   VALIDITY (shapeOk) : is `info` an object, are the DECLARED fields present, right types,
//                        no unexpected fields? This tier ALONE decides handover.
//   COMPLETENESS       : for the discriminator's value, is every requiredWhen field filled?
//   CONSISTENCY        : the skill's own plain-code predicates.
// `ok` = all three. `shapeOk` = validity only. The distinction is load-bearing (File 6):
// a payload that is shape-VALID but incomplete is STILL handed to the skill, whose own
// clarification pass fills the gaps. Only a shape-INVALID payload is withheld, and then the
// skill falls back to its own interpret() call. So the worst case is "correct but slow",
// never "fast and wrong".
const INPUTS_MOD = await import(
  new URL("../secretary/1. Orchestrator/lib/inputs.js", import.meta.url).href
).catch((e) => ({ __err: e.code === "ERR_MODULE_NOT_FOUND" ? "lib/inputs.js does not exist" : e.message }));
const CAL_SKILL = await import(new URL("skill.js", pathToFileURL(CAL_DIR)).href);
const CAL_PROMPT = await import(new URL("prompt.js", pathToFileURL(CAL_DIR)).href);
const CAL_INPUTS = CAL_SKILL.manifest?.inputs;
const checkPayload = INPUTS_MOD.checkPayload;

if (typeof checkPayload !== "function")
  console.log(
    `  ..    ${INPUTS_MOD.__err || "lib/inputs.js exports no checkPayload()"} — every T2.7\n` +
      "        below is RED for that reason, and for no other.\n"
  );
if (!CAL_INPUTS)
  console.log("  ..    the calendar manifest has no `inputs` declaration yet — T2.10 is RED for that reason.\n");

// A gate that does not exist yields shapeOk/ok = null, which equals neither true nor false,
// so every assertion below goes red — and none of them goes red by ACCIDENTALLY passing.
const gate = (inputs, info) => {
  if (typeof checkPayload !== "function") return { shapeOk: null, ok: null, problems: ["no checkPayload()"] };
  try {
    return checkPayload(inputs, info);
  } catch (e) {
    return { shapeOk: null, ok: null, problems: [`checkPayload THREW: ${e.message}`] };
  }
};
// A complete, shape-valid CREATE payload: all ELEVEN declared fields present. Cases below
// override only what they are about.
const pay = (o = {}) => ({
  action: "create",
  title: "Reunião",
  participants: [{ name: "Laura", email: "laura@example.com" }],
  start_iso: "2026-07-14T15:00:00-03:00",
  duration_min: 45,
  all_day: false,
  all_day_end_iso: null,
  summary: "",
  list_mode: null,
  range_start_iso: null,
  range_end_iso: null,
  ...o,
});

const g = (o) => gate(CAL_INPUTS, pay(o));
const gRaw = (info) => gate(CAL_INPUTS, info);

check("T2.7-1  1 attendee, 0 emails                      -> STOP (ok=false)",
  g({ participants: [{ name: "Laura", email: null }] }).ok === false, 2);
check("T2.7-2  2 attendees, 1 email                      -> STOP (ok=false)",
  g({ participants: [{ name: "Laura", email: "l@x.com" }, { name: "Pedro", email: null }] }).ok === false, 2);
check("T2.7-3  create with NO date                       -> STOP (ok=false)",
  g({ start_iso: null }).ok === false, 2);
check("T2.7-4  a complete create                         -> PASS (shapeOk AND ok)",
  g({}).shapeOk === true && g({}).ok === true, 2);
// 🔴 THE ONE THAT GUARDS COMMIT 9eead61 (card 33bb6637). A zero-guest create is COMPLETE —
// "agenda amanhã 16h pegar os cachorros" is a solo reminder and must never be gated. The
// prototype in the card folder gets this WRONG (it demands >= 1 attendee); the port must not.
check("T2.7-5  a ZERO-GUEST create                       -> PASS (guards commit 9eead61)",
  g({ participants: [] }).shapeOk === true && g({ participants: [] }).ok === true, 2);
check("T2.7-6  list with no list_mode                    -> STOP (ok=false)",
  g({ action: "list", list_mode: null, participants: [], start_iso: null }).ok === false, 2);
// An undeclared / unknown skill ("other") has no contract to validate against, so nothing may
// be handed over: the skill must extract for itself.
check('T2.7-7  an UNKNOWN skill ("other", no declaration) -> STOP (shapeOk=false)',
  gate(undefined, pay({})).shapeOk === false, 2);

// ---- the all-day cases. These are what make the gate able to see commit 6c76dab. ---------
const allDay = { action: "create", participants: [], all_day: true, all_day_end_iso: null, duration_min: null,
  start_iso: "2026-07-14T00:00:00-03:00" };
check("T2.7a   ALL-DAY create (all_day:true, end null)   -> PASS — an all-day order is not gated",
  g(allDay).shapeOk === true && g(allDay).ok === true, 2);
const allDayRange = { ...allDay, start_iso: "2026-07-13T00:00:00-03:00", all_day_end_iso: "2026-07-15T00:00:00-03:00" };
// And the gate must NOT re-check end >= start: draftFromInfo() already clamps it, in the one
// normalizer every path funnels through. A duplicated clamp is how the two silently drift.
check("T2.7b   ALL-DAY RANGE (end = a later day)         -> PASS (the gate does NOT re-check end>=start)",
  g(allDayRange).shapeOk === true && g(allDayRange).ok === true, 2);
// 🔴 T2.7c — THE ONE THAT GUARDS COMMIT 6c76dab, AND THE WHOLE SAFETY NET.
// A DECLARED field that is MISSING is invalid. A declared field that is NULL is fine. If a
// missing `all_day` were silently coerced to false, a merged prompt that stopped asking for
// it would silently un-ship all-day events — fast and WRONG. Shape-invalid instead means the
// skill falls back to interpret(): correct, and merely slow.
const absent = pay({});
delete absent.all_day;
check('T2.7c   `all_day` key ABSENT                      -> shape-INVALID, problem "all_day: missing" ' +
  `(got shapeOk=${JSON.stringify(gRaw(absent).shapeOk)}, problems=${JSON.stringify(gRaw(absent).problems)})`,
  gRaw(absent).shapeOk === false &&
    (gRaw(absent).problems || []).some((p) => /all_day.*missing/i.test(String(p))), 2);
check('T2.7d   all_day:"yes" (a string, not a bool)      -> shape-INVALID -> fallback to interpret()',
  g({ all_day: "yes" }).shapeOk === false, 2);
check("T2.7e   an ordinary TIMED create (all_day:false, end null) -> PASS — a null must not gate",
  g({}).shapeOk === true && g({}).ok === true, 2);
// A shape-valid but INCOMPLETE payload is still handed over — that is the design, and it is
// the difference between the VALIDITY tier and the other two. Assert the tiers really are
// separate, or the gate could conflate them and withhold every incomplete payload (which
// would cost an extra LLM call on exactly the turns the card is trying to speed up).
const incomplete = g({ start_iso: null });
check("T2.7f   an INCOMPLETE payload is shape-VALID (shapeOk=true, ok=false) — it is still handed over",
  incomplete.shapeOk === true && incomplete.ok === false, 2);

// ---- T2.7 SCALAR ARRAY (card 55e00052) — inputs.js learned `of: { type: "string" }`. ----------
// The pilot (assistant_settings) declares tags as an array of scalars, not an array of objects.
// Both halves of that extension — describeFields's rendering AND checkType's element check — must
// ship together, or a scalar `of` renders "array of {type: undefined}" and validates as objects.
// These call checkPayload directly (no server, no model), same as the T2.7 block above.
const SCALAR_ARR = { fields: { tags: { type: "array", of: { type: "string" } } } };
check("T2.7g   scalar array — ['@assist'] (array of string) -> shapeOk:true",
  gate(SCALAR_ARR, { tags: ["@assist"] }).shapeOk === true, 2);
const notStrings = gate(SCALAR_ARR, { tags: [{ tag: "@assist" }] });
check("T2.7h   scalar array — [{tag:'@assist'}] (objects, not strings) -> shapeOk:false, 'tags[0]: not a string' " +
  `(problems=${JSON.stringify(notStrings.problems)})`,
  notStrings.shapeOk === false && (notStrings.problems || []).some((p) => /tags\[0\].*not a string/i.test(String(p))), 2);
check("T2.7i   scalar array — a bare string '@assist' (not an array) -> shapeOk:false",
  gate(SCALAR_ARR, { tags: "@assist" }).shapeOk === false, 2);
// The regression guard: an OBJECT `of` (calendar's participants) STILL validates its elements as
// objects — the scalar branch must not swallow the discriminated object case.
check("T2.7j   OBJECT array — calendar's participants still validate as OBJECTS (regression guard)",
  g({ participants: [{ name: "Laura", email: "l@x.com" }] }).shapeOk === true &&
    g({ participants: [{ name: "Laura", email: "not-an-email" }] }).ok === false, 2);
// Note (do not "fix" here): {tags:null} is shape-VALID — checkType exempts arrays from the
// not-nullable rule (checkType:127-129). Harmless: the pilot gates on `ok`, and normalizeTags(null)
// is {ok:false}, so a null tag list can never be applied.
check("T2.7k   scalar array — {tags:null} is shape-VALID (arrays are exempt from not-nullable)",
  gate(SCALAR_ARR, { tags: null }).shapeOk === true, 2);

// ---- T2.10 — THE STRUCTURAL LINT. The most valuable assertion in this file. --------------
// This card exists because a field was added to CAL_SCHEMA (6c76dab: all_day, all_day_end_iso)
// and the declaration did not follow. Step 2 binds manifest.inputs to CAL_SCHEMA's field
// names — that binding is what makes the merged payload a drop-in for interpret()'s output.
// It is also a NEW way to break the product SILENTLY: add a field to CAL_SCHEMA and forget the
// declaration, and the merged prompt simply stops asking for it, draftFromInfo reads
// `undefined`, and the feature that field implements dies without a single test going red.
// WITHOUT THIS LINT, THE NEXT ONE IS SILENT TOO. If a future card makes this red, the fix is
// to update the declaration — NEVER to loosen the lint.
// (It lives in scripts/, not under 1. Orchestrator/, so it does not violate T2.6. The SKILL is
// allowed to know CAL_SCHEMA; the orchestrator is not.)
const schemaFields = [...(CAL_PROMPT.CAL_SCHEMA?.required || [])].sort();
const declared = [...Object.keys(CAL_INPUTS?.fields || {})].sort();
const missingFromDecl = schemaFields.filter((f) => !declared.includes(f));
const extraInDecl = declared.filter((f) => !schemaFields.includes(f));
console.log(`        CAL_SCHEMA.required (${schemaFields.length}): ${schemaFields.join(", ")}`);
console.log(`        manifest.inputs.fields (${declared.length}): ${declared.join(", ") || "(none — no declaration)"}`);
check(
  "T2.10  STRUCTURAL LINT — manifest.inputs.fields == CAL_SCHEMA.required, AS A SET" +
    (missingFromDecl.length ? `  [declared in CAL_SCHEMA but NOT in manifest.inputs: ${missingFromDecl.join(", ")}]` : "") +
    (extraInDecl.length ? `  [in manifest.inputs but NOT in CAL_SCHEMA: ${extraInDecl.join(", ")}]` : ""),
  declared.length > 0 && missingFromDecl.length === 0 && extraInDecl.length === 0,
  2
);

// ============================================================================
//  THE FAKES. Copied from scripts/calendar-create-selftest.mjs, with two additions:
//  the fake Anthropic SLEEPS, and it RECORDS EVERY REQUEST BODY.
// ============================================================================
let CLOCK = 1768307000;
let history = [];
let scripted = []; // [{ kind, json }] — the pinned model outputs for this turn
let sent = []; // every message the assistant sent, this scenario
let llmCalls = []; // [{ kind }] — every Claude call, in order, this scenario
let bodies = []; // EVERY request body the fake Anthropic saw — the whole run
let unscripted = []; // calls with no fixture      -> harness fault
let unrecognised = []; // calls kindOf could not route -> harness fault
let googleCalls = [];
let llmDelay = LLM_MS; // the T3 section drops this to 0 — it asserts capability, not time

// per-turn timing, reset by say()
let turnT0 = 0;
let turnFirstReplyAt = null;
let turnCallsBase = 0;
let turnCallsAtFirstReply = null;

function kindOf(body) {
  const schema = body?.output_config?.format?.schema;

  // THE MERGED CALL, identified POSITIVELY: after step 2 it is the only call in the product
  // that asks for JSON with NO output_config at all (the format is demanded in the PROMPT —
  // that is what keeps the orchestrator generic). A sniffer handed only the schema could not
  // see the absence of one; this one takes the whole request body, which is the point.
  if (!schema) {
    const sys = String(body?.system || "");
    if (/Available tasks:/.test(sys)) return "route_extract";
    // lib/selflearning.js analyze() — a PROSE call (readText, not JSON) that captureFailure
    // fires whenever a failure is reported. It has always had no output_config, so it is the
    // one call that could be mistaken for the merged router. It is identified positively too,
    // and answered with prose. It rides the same shared client, so T1.1 covers it.
    if (/senior engineer triaging a failure/i.test(sys)) return "selflearn_analyze";
    throw new Error(
      `kindOf: a call with NO output_config that is not the merged router — system="${sys.slice(0, 60)}…"`
    );
  }

  const props = schema.properties || {};
  const has = (k) => Object.prototype.hasOwnProperty.call(props, k);
  const keys = Object.keys(props);

  if (has("tasks")) return "router"; // ROUTER_SCHEMA (today; deleted by step 2)
  if (has("what_went_wrong")) return "feedback"; // the feedback skill's extract call
  if (has("action")) return "calendar"; // CAL_SCHEMA — the fallback interpret()
  if (has("new_start_iso")) return has("decision") ? "edit_review" : "edit";
  if (has("decision") && keys.length === 1) return "confirm_classify";
  if (has("title") && has("duration_min")) return "create_review";
  if (has("start_iso") && has("participants") && !has("title")) return "resolve";

  throw new Error(`kindOf: UNRECOGNISED SCHEMA — properties=${JSON.stringify(keys)}`);
}

const evo = http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", () => {
    const p = b ? JSON.parse(b) : {};
    res.setHeader("content-type", "application/json");
    if ((req.url || "").includes("/message/sendText/")) {
      // THE MEASUREMENT. Time to FIRST reply is what the human complained about, so it is
      // stamped here — at the outbound message — and not inferred from anything.
      if (turnFirstReplyAt === null) {
        turnFirstReplyAt = Date.now();
        turnCallsAtFirstReply = llmCalls.length - turnCallsBase;
      }
      sent.push(String(p.text));
      history.push({
        key: { remoteJid: JID(), fromMe: true, id: "s" + history.length },
        message: { conversation: String(p.text) },
        messageTimestamp: CLOCK++,
        pushName: "Marcelo",
      });
      return res.end("{}");
    }
    if ((req.url || "").includes("/chat/findMessages/")) {
      const byJid = p.where?.key?.remoteJid === JID();
      return res.end(JSON.stringify(byJid ? history : []));
    }
    res.end("{}");
  });
});

const llm = http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", async () => {
    const p = b ? JSON.parse(b) : {};
    bodies.push(p); // recorded BEFORE anything can throw — T1.1 sees every call, always
    let kind;
    try {
      kind = kindOf(p);
    } catch (e) {
      unrecognised.push(e.message);
      console.log(`      !! ${e.message}`);
      res.statusCode = 500;
      res.setHeader("content-type", "application/json");
      return res.end(JSON.stringify({ type: "error", error: { message: e.message } }));
    }
    llmCalls.push({ kind });
    let text = "{}";
    if (kind === "selflearn_analyze") {
      // Background, fire-and-forget, and it wants PROSE. It needs no per-scenario fixture —
      // but it IS a real call on the shared client, so it stays in llmCalls and in `bodies`.
      text = "Likely cause: the fixture said so.\nSuspected area: scripts/turn-latency-selftest.mjs";
    } else {
      const i = scripted.findIndex((s) => s.kind === kind);
      if (i >= 0) text = scripted.splice(i, 1)[0].json;
      else {
        unscripted.push(kind);
        console.log(`      !! UNSCRIPTED LLM CALL: ${kind}`);
      }
    }

    // THE STAND-IN FOR THE PROVIDER. A real round-trip is 4-8s; this is a deterministic one.
    // Our own overhead is 1ms (ROOT_CAUSE.md §4), so a turn costs exactly calls x LLM_MS.
    if (llmDelay > 0) await new Promise((r) => setTimeout(r, llmDelay));

    res.setHeader("content-type", "application/json");
    res.end(
      JSON.stringify({
        id: "m",
        type: "message",
        role: "assistant",
        model: "claude-sonnet-5",
        content: [{ type: "text", text }],
        stop_reason: "end_turn",
        usage: { input_tokens: 1, output_tokens: 1 },
      })
    );
  });
});

let _jid = "5511994224000@s.whatsapp.net";
const JID = () => _jid;

await new Promise((r) => evo.listen(PORT_EVO, r));
await new Promise((r) => llm.listen(PORT_LLM, r));

// ---- the googleapis stub, written OUTSIDE the repo (temp dir) ----------------
// Same trick as calendar-create-selftest.mjs. `events.get` returns ONE confirmed event so
// the edit/delete flows (T3.3/T3.4) can be driven from a quoted calendar link: the link's
// decoded event id scores 100 in matchEventTargets, which is a confident match on its own.
const EVENT_ID = "evt_1";
const tmp = await mkdtemp(path.join(os.tmpdir(), "turn-latency-selftest-"));
await writeFile(
  path.join(tmp, "gstub.mjs"),
  `const rec = (name, args) => console.log("GOOGLE_CALL " + JSON.stringify({ name, args }));
const EV = {
  id: ${JSON.stringify(EVENT_ID)}, status: "confirmed", summary: "Reunião com a Laura",
  htmlLink: "https://calendar.google.com/event?eid=STUB",
  start: { dateTime: "2026-07-14T15:00:00-03:00", timeZone: "America/Sao_Paulo" },
  end:   { dateTime: "2026-07-14T16:00:00-03:00", timeZone: "America/Sao_Paulo" },
  attendees: [{ email: "laura@example.com" }],
};
const calendar = () => ({
  events: {
    insert: async (a) => {
      rec("events.insert", a);
      return { data: {
        id: "evt_stub_1", status: "confirmed",
        summary: a?.requestBody?.summary,
        htmlLink: "https://calendar.google.com/event?eid=STUB",
        start: a?.requestBody?.start, end: a?.requestBody?.end,
        attendees: a?.requestBody?.attendees || [],
      } };
    },
    // Empty calendar: create finds no duplicate to reuse, so every create really inserts.
    list: async (a) => { rec("events.list", a); return { data: { items: [] } }; },
    get: async (a) => {
      rec("events.get", a);
      if (a?.eventId === ${JSON.stringify(EVENT_ID)}) return { data: EV };
      const e = new Error("Not Found"); e.code = 404; throw e;
    },
    patch: async (a) => { rec("events.patch", a); return { data: { id: a.eventId } }; },
    update: async (a) => { rec("events.update", a); return { data: { id: a.eventId } }; },
    delete: async (a) => { rec("events.delete", a); return { data: {} }; },
  },
});
class OAuth2 { constructor(...a) { this._a = a; } setCredentials(c) { this._c = c; } }
export const google = { calendar, tasks: () => ({ tasks: {}, tasklists: {} }), auth: { OAuth2 } };
export default { google };
`
);
await writeFile(
  path.join(tmp, "hooks.mjs"),
  `const STUB = ${JSON.stringify(pathToFileURL(path.join(tmp, "gstub.mjs")).href)};
export async function resolve(spec, ctx, next) {
  if (spec === "googleapis") return { url: STUB, format: "module", shortCircuit: true };
  return next(spec, ctx);
}
`
);
await writeFile(
  path.join(tmp, "register.mjs"),
  `import { register } from "node:module";
register("./hooks.mjs", import.meta.url);
`
);

const selfLearnDir = await mkdtemp(path.join(os.tmpdir(), "turn-latency-selflearn-"));

const child = spawn(
  process.execPath,
  ["--import", pathToFileURL(path.join(tmp, "register.mjs")).href, SERVER],
  {
    cwd: REPO,
    env: {
      ...process.env,
      PORT: String(PORT_APP),
      ANTHROPIC_BASE_URL: `http://127.0.0.1:${PORT_LLM}`,
      ANTHROPIC_API_KEY: "sk-ant-selftest",
      EVOLUTION_URL: `http://127.0.0.1:${PORT_EVO}`,
      EVOLUTION_APIKEY: "x",
      EVOLUTION_INSTANCE: "secretary",
      REDIS_URL: "",
      OWNER_NAME: "Marcelo",
      // The reproduction (REPLICATION.md) used "@secretaria", and the orders below are its
      // orders VERBATIM. This suite exercises the NEW orchestrator flow, so "@secretaria" is
      // wired as the NEW-flow tag (SECRETARY_TAG_NEW); the legacy tag is parked on a disjoint
      // "@legacy" so it can never intercept these orders. (Dual-tag parallel run.)
      SECRETARY_TAG: "@legacy",
      SECRETARY_TAG_NEW: "@secretaria",
      SELF_LEARNING_DIR: selfLearnDir,
      GOOGLE_CLIENT_ID: "",
      GOOGLE_CLIENT_SECRET: "",
      GOOGLE_REFRESH_TOKEN: "",
    },
    stdio: ["ignore", "pipe", "pipe"],
  }
);

let googleTouched = false;
let outBuf = "";
child.stdout.on("data", (b) => {
  const s = b.toString();
  if (DEBUG) process.stdout.write(s);
  outBuf += s;
  let i;
  while ((i = outBuf.indexOf("\n")) >= 0) {
    const line = outBuf.slice(0, i);
    outBuf = outBuf.slice(i + 1);
    if (line.startsWith("GOOGLE_CALL ")) {
      try {
        googleCalls.push(JSON.parse(line.slice("GOOGLE_CALL ".length)));
      } catch {
        /* ignore a torn line */
      }
    }
  }
});
const sniffGoogle = (buf) => {
  if (/googleapis\.com|invalid_grant|No refresh token/i.test(buf.toString())) googleTouched = true;
};
child.stdout.on("data", sniffGoogle);
child.stderr.on("data", (b) => {
  if (DEBUG) process.stderr.write(b.toString());
  sniffGoogle(b);
});

await new Promise((resolve, reject) => {
  const t = setTimeout(() => reject(new Error("boot timeout")), 20000);
  child.stdout.on("data", (b) => {
    if (b.toString().includes("listening on port")) {
      clearTimeout(t);
      resolve();
    }
  });
});

// ---- driving the real webhook ------------------------------------------------
let mid = 0;
async function say(text, { fromMe = true, pushName = "Marcelo", quote = null } = {}) {
  const before = sent.length;
  turnFirstReplyAt = null;
  turnCallsAtFirstReply = null;
  turnCallsBase = llmCalls.length;

  // A quoted (replied-to) message. Evolution delivers the reply context as a sibling of
  // `message` OR nested under it; lib/whatsapp.js getQuoted() checks the sibling first.
  const message = quote
    ? { extendedTextMessage: { text, contextInfo: { stanzaId: "q1", quotedMessage: { conversation: quote } } } }
    : { conversation: text };

  turnT0 = Date.now();
  await fetch(`http://127.0.0.1:${PORT_APP}/webhook`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      data: { key: { fromMe, remoteJid: JID(), id: "m" + ++mid }, message, messageTimestamp: CLOCK++, pushName },
    }),
  });

  // Settle: wait until neither a reply nor an LLM call has happened for longer than one
  // model round-trip. The idle window MUST exceed llmDelay — at 100ms ticks the original
  // 1.2s window would declare "settled" in the middle of a 1500ms model call, and the suite
  // would silently measure a turn that had not finished.
  const idleTicks = Math.ceil((llmDelay + 1500) / 100);
  let idle = 0;
  let mark = sent.length + llmCalls.length;
  while (idle < idleTicks) {
    await new Promise((r) => setTimeout(r, 100));
    const now = sent.length + llmCalls.length;
    if (now !== mark) {
      mark = now;
      idle = 0;
    } else idle++;
  }
  return {
    out: sent.slice(before),
    ms: turnFirstReplyAt === null ? null : turnFirstReplyAt - turnT0,
    calls: turnCallsAtFirstReply,
  };
}

function reset(jid) {
  _jid = jid; // a fresh chat => a fresh (empty) session; no cross-talk between scenarios
  history = [];
  sent = [];
  llmCalls = [];
  scripted = [];
  googleCalls = [];
}

const body_ = (s) => String(s).replace(/^\*\[[^\]]+\]:\*\s*/, "").trim();
const shown = (o) => (o.length ? JSON.stringify(body_(o[0])) : "(NOTHING AT ALL)");
const inserts = () => googleCalls.filter((c) => c.name === "events.insert");
// The edit WRITE, whichever verb carries it: patch and update are both a write to Google, and
// which one the skill uses is an implementation detail. T3.3 is about the confirm-first promise
// — nothing written before "sim", the edit written after — so it watches the write, not the verb.
const editWrites = () => googleCalls.filter((c) => c.name === "events.patch" || c.name === "events.update");
const deletes = () => googleCalls.filter((c) => c.name === "events.delete");
const kinds = () => llmCalls.map((c) => c.kind);

// ---- the pinned model outputs ------------------------------------------------
// Every fresh-order scenario scripts BOTH shapes, so ONE fixture set drives the product
// before AND after step 2:
//   today  : "router" (ROUTER_SCHEMA)  then  "calendar" (CAL_SCHEMA)   -> 2 calls
//   after  : "route_extract" (no output_config, info folded in)        -> 1 call
// A red assertion below is therefore the PRODUCT's behaviour, never the fixture's.
const cal = (o = {}) => ({
  action: "create",
  title: null,
  participants: [],
  start_iso: null,
  duration_min: null,
  all_day: null,
  all_day_end_iso: null,
  summary: "",
  list_mode: null,
  range_start_iso: null,
  range_end_iso: null,
  ...o,
});
const ROUTER = (lang = "pt") => ({
  kind: "router",
  json: JSON.stringify({ tasks: ["calendar_action"], lang, reason: "agendar" }),
});
const CALENDAR = (o = {}) => ({ kind: "calendar", json: JSON.stringify(cal(o)) });
// The merged reply, RE-PINNED to the conversational three-state contract (card 55e00052):
// route(ctx, turn) now returns { say, next, skills, info, lang }, not { tasks, lang, info }. A
// fresh calendar order is the model saying "execute this skill now" — say:null, next:"execute",
// skills:[…]. The `info` field identity is unchanged, so the merged payload is still a drop-in
// for interpret()'s output. (One shape, one fixture — the router is NOT taught to accept both;
// the stale ROUTER entries below simply go unclaimed, which is harmless.)
const MERGED = (o = {}, { tasks = ["calendar_action"], lang = "pt", info } = {}) => ({
  kind: "route_extract",
  json: JSON.stringify({
    say: null,
    next: "execute",
    skills: tasks,
    lang,
    info: info === undefined ? cal(o) : info,
  }),
});
const resolve_ = (o = {}) =>
  ({ kind: "resolve", json: JSON.stringify({ decision: "modify", start_iso: null, participants: null, no_email_for: [], ...o }) });
const review = (o = {}) =>
  ({ kind: "create_review", json: JSON.stringify({ decision: "confirm", title: null, participants: [], start_iso: null, duration_min: null, summary: "", all_day: null, all_day_end_iso: null, ...o }) });

const SEC = (ms) => (ms === null ? "no reply" : `${(ms / 1000).toFixed(2)}s`);

// ============================================================================
//  SECTION STEP-2 (driven) — the reproduction's OWN orders, through the REAL server.
//  REPLICATION.md S1 / S3 / S4, verbatim. Their measured production latencies were 19s,
//  18s and 10s. Here the clock is LLM_MS per round-trip, so the assertion is on the number
//  of round-trips and the wall-clock that follows from it.
// ============================================================================
console.log(`\n=== STEP-2 (driven): the reproduction's orders. LLM_MS=${LLM_MS} per model call ===\n`);

// ---- T2.1 / T2.2 — REPLICATION S1: an INCOMPLETE create (no date, no email) --------------
// Today: router -> calendar -> inspectMissing = THREE calls before the assistant says a word.
// After: merged -> inspectMissing = TWO. The clarification call is not removed by this card
// (it is the "only if the check fails" call, and the payload really is incomplete) — the
// ROUTER round-trip is.
reset("5511111111111@s.whatsapp.net");
scripted = [
  ROUTER(),
  CALENDAR({ title: "Reunião com a Laura", participants: [{ name: "Laura", email: null }] }),
  MERGED({ title: "Reunião com a Laura", participants: [{ name: "Laura", email: null }] }),
  resolve_({}),
];
let r = await say("@secretaria agendar uma reuniao com a Laura");
console.log(`   owner    : @secretaria agendar uma reuniao com a Laura        [REPLICATION S1, measured 19s live]`);
console.log(`   assistant: ${shown(r.out)}`);
console.log(`   -> calls before first reply: ${r.calls}   time to first reply: ${SEC(r.ms)}   (${kinds().join(" -> ")})`);
check(`T2.1  S1 (incomplete create) — calls before the FIRST reply <= 2  (got ${r.calls})`,
  r.calls !== null && r.calls <= 2, 2);
check(`T2.2  S1 — time to first reply < 2.6 x LLM_MS = ${(2.6 * LLM_MS) / 1000}s  (got ${SEC(r.ms)})`,
  r.ms !== null && r.ms < 2.6 * LLM_MS, 2);
// T3.2 rides on the same drive: the clarification pass must STILL fire and STILL ask.
check("T3.2  CAPABILITY — the clarification pass still fires on an incomplete draft, and still ASKS",
  r.out.length === 1 && /(e-mail|email|quando|data)/i.test(body_(r.out[0])), 3);

// ---- T2.3 / T2.5 — REPLICATION S3: a COMPLETE create ------------------------------------
// The shape REPLICATION measured at 18s with only TWO LLM calls — the one that proves the
// clarification call is not the whole bug. Today: router -> calendar. After: ONE merged call.
reset("5522222222222@s.whatsapp.net");
scripted = [
  ROUTER(),
  CALENDAR({ title: "Call com o Pedro Teste", participants: [{ name: "Pedro Teste", email: "pedro.teste@example.com" }],
    start_iso: "2026-07-14T16:00:00-03:00", duration_min: 30 }),
  MERGED({ title: "Call com o Pedro Teste", participants: [{ name: "Pedro Teste", email: "pedro.teste@example.com" }],
    start_iso: "2026-07-14T16:00:00-03:00", duration_min: 30 }),
];
r = await say(
  "@secretaria agendar amanha as 16h uma call de 30 minutos com o Pedro Teste, email pedro.teste@example.com"
);
console.log(`   owner    : @secretaria agendar amanha as 16h uma call ... pedro.teste@example.com   [S3, measured 18s live]`);
console.log(`   assistant: ${shown(r.out)}`);
console.log(`   -> calls before first reply: ${r.calls}   time to first reply: ${SEC(r.ms)}   (${kinds().join(" -> ")})`);
check(`T2.3a S3 (complete create) — calls before the FIRST reply == 1  (got ${r.calls})`,
  r.calls === 1, 2);
check(`T2.3b S3 — time to first reply < 1.9 x LLM_MS = ${(1.9 * LLM_MS) / 1000}s  (got ${SEC(r.ms)})`,
  r.ms !== null && r.ms < 1.9 * LLM_MS, 2);
// The FIRST call of a fresh turn is the merged one, and it carries NO output_config: the
// reply format is declared in the PROMPT. If a builder "fixes" a red suite by putting
// output_config back on the router call, THIS is what refuses to go green.
const firstOfTurn = bodies.slice(-llmCalls.length)[0];
check("T2.5  the FIRST call of a fresh tagged order carries NO `output_config` — the format is " +
  "declared in the PROMPT, so the orchestrator never imports a skill's schema",
  !!firstOfTurn && firstOfTurn.output_config === undefined, 2);
// T3.1 rides on the same drive: confirm-first must survive.
check("T3.1a CAPABILITY — a complete create still reaches the CONFIRM CARD (it does not book anything yet)",
  r.out.length === 1 && /Confirme este evento/i.test(body_(r.out[0])), 3);
check("T3.1b CAPABILITY — and NOTHING has reached Google before the owner says 'sim'",
  inserts().length === 0, 3);

scripted = [review({ decision: "confirm" })];
r = await say("sim");
console.log(`   owner    : sim                                        <- UNTAGGED`);
console.log(`   assistant: ${shown(r.out)}`);
check("T3.1c CAPABILITY — 'sim' WRITES the event: Google receives exactly one insert",
  inserts().length === 1, 3);

// ---- T2.4 — REPLICATION S4: a LIST ------------------------------------------------------
reset("5533333333333@s.whatsapp.net");
scripted = [
  ROUTER(),
  CALENDAR({ action: "list", list_mode: "window", range_start_iso: "2026-07-14T00:00:00-03:00",
    range_end_iso: "2026-07-14T23:59:59-03:00" }),
  MERGED({ action: "list", list_mode: "window", range_start_iso: "2026-07-14T00:00:00-03:00",
    range_end_iso: "2026-07-14T23:59:59-03:00" }),
];
r = await say("@secretaria como esta minha agenda amanha?");
console.log(`   owner    : @secretaria como esta minha agenda amanha?            [REPLICATION S4, measured 10s live]`);
console.log(`   assistant: ${shown(r.out)}`);
console.log(`   -> calls before first reply: ${r.calls}   time to first reply: ${SEC(r.ms)}   (${kinds().join(" -> ")})`);
check(`T2.4  S4 (list) — calls before the FIRST reply == 1  (got ${r.calls})`, r.calls === 1, 2);

// ---- From here on the clock does not matter — only behaviour. Drop the delay. ------------
llmDelay = 0;

// ---- T2.8 — a SHAPE-INVALID payload: capability is never lost, only speed ----------------
// The merged call returns garbage in `info`. The plain-code gate says the SHAPE is bad, so
// the payload is WITHHELD and the skill falls back to its OWN interpret() call — today's
// path, unchanged. The worst case of this whole card is "correct but slow".
reset("5544444444444@s.whatsapp.net");
scripted = [
  MERGED({}, { info: "not an object at all" }),
  ROUTER(), // today's path still needs its router fixture
  CALENDAR({ title: "Reunião com a Laura", participants: [{ name: "Laura", email: "laura@example.com" }],
    start_iso: "2026-07-14T15:00:00-03:00", duration_min: 45 }),
];
r = await say("@secretaria agendar reuniao com a laura amanha 15h, laura@example.com");
console.log(`   owner    : @secretaria agendar reuniao com a laura amanha 15h  <- the merged call returns GARBAGE info`);
console.log(`   assistant: ${shown(r.out)}`);
console.log(`   -> calls: ${kinds().join(" -> ")}`);
check("T2.8  a shape-INVALID payload makes the skill FALL BACK to its own interpret() call " +
  `(saw: ${kinds().join(" -> ") || "nothing"})`,
  kinds().includes("route_extract") && kinds().includes("calendar"), 2);
check("T2.8b …and no capability is lost: the confirm card still appears",
  r.out.length === 1 && /Confirme este evento/i.test(body_(r.out[0])), 2);

// ---- T2.9 — DUAL INTENT: the payload belongs to tasks[0], and to nobody else -------------
// On ["feedback","calendar_action"] the extracted payload is FEEDBACK's. Calendar must get
// ctx.info === null and re-extract for itself. Handing calendar someone else's payload is
// how you book the wrong meeting — so the merged `info` here carries a deliberately WRONG
// title, and the assertion is that it never reaches the confirm card.
reset("5555555555555@s.whatsapp.net");
scripted = [
  MERGED({}, {
    tasks: ["feedback", "calendar_action"],
    info: { title: "PAYLOAD-QUE-NAO-E-DO-CALENDARIO", what_went_wrong: "errou a hora", expected: null,
      suspected_skill: "calendar_action", enough_context: true },
  }),
  // today's path: the router returns the SAME dual-intent routing, so the drive is
  // meaningful before and after — only the number of calls, and who gets the payload, move.
  { kind: "router", json: JSON.stringify({ tasks: ["feedback", "calendar_action"], lang: "pt", reason: "ambos" }) },
  { kind: "feedback", json: JSON.stringify({ title: "hora errada", what_went_wrong: "errou a hora",
      expected: "17h", suspected_skill: "calendar_action", enough_context: true }) },
  CALENDAR({ title: "Reunião CORRETA com a Laura", participants: [{ name: "Laura", email: "laura@example.com" }],
    start_iso: "2026-07-14T17:00:00-03:00", duration_min: 45 }),
];
r = await say("@secretaria voce errou a hora, muda pra 17h com a laura, laura@example.com");
console.log(`   owner    : @secretaria voce errou a hora, muda pra 17h com a laura   <- DUAL INTENT`);
console.log(`   assistant: ${r.out.map((s) => JSON.stringify(body_(s))).join("  |  ")}`);
console.log(`   -> calls: ${kinds().join(" -> ")}`);
const dualText = r.out.map(body_).join("\n");
check("T2.9a DUAL INTENT — calendar re-extracts for itself (a 'calendar' interpret call is made): " +
  "the payload belonged to feedback, and calendar must be handed ctx.info === null " +
  `(saw: ${kinds().join(" -> ") || "nothing"})`,
  kinds().includes("route_extract") && kinds().includes("calendar"), 2);
check("T2.9b DUAL INTENT — feedback's payload NEVER reaches the calendar draft",
  !/PAYLOAD-QUE-NAO-E-DO-CALENDARIO/.test(dualText), 2);

// ============================================================================
//  CAPABILITY SECTION — no capability may be bought back with speed.
//  These are GREEN today and they MUST STAY GREEN. A red one here means the fix traded a
//  feature for milliseconds: STOP. (T3.3/T3.4 also close a real gap — edit and delete went
//  untested through three experiments, because a realistic fixture needs a quoted invite and
//  creating one risked emailing a real person. Against the stub they cost nothing and touch
//  nobody.)
// ============================================================================
console.log("\n=== CAPABILITY: nothing was traded away for speed ===\n");

// The invite the owner replies to. eid = base64("<eventId> <calendarId>"), exactly as Google
// builds it and exactly as resolveEventId() decodes it.
const EID = Buffer.from(`${EVENT_ID} marcelo@example.com`).toString("base64");
const INVITE = `Convite criado: Reunião com a Laura — https://calendar.google.com/event?eid=${EID}`;

// ---- T3.3 — EDIT: a quoted invite + "move it to 5pm" -> confirm -> the edit is written -----
reset("5566666666666@s.whatsapp.net");
scripted = [
  ROUTER(),
  MERGED({ action: "edit", participants: [{ name: "Laura", email: "laura@example.com" }],
    start_iso: "2026-07-14T15:00:00-03:00" }),
  CALENDAR({ action: "edit", participants: [{ name: "Laura", email: "laura@example.com" }],
    start_iso: "2026-07-14T15:00:00-03:00" }),
  { kind: "edit", json: JSON.stringify({ new_start_iso: "2026-07-14T17:00:00-03:00", new_duration_min: null,
      new_title: null, new_summary: null, add_emails: [], remove_emails: [], clarify: null }) },
];
r = await say("@secretaria muda essa reuniao pras 17h", { quote: INVITE });
console.log(`   owner    : @secretaria muda essa reuniao pras 17h   <- REPLYING to the invite`);
console.log(`   assistant: ${shown(r.out)}`);
// The card must show the REQUESTED change (17h -> "5:00 PM") and still be waiting for "sim".
check("T3.3a CAPABILITY — edit reaches the EDIT CONFIRM card, showing the NEW time and awaiting 'sim'",
  r.out.length === 1 &&
    /evento atualizado/i.test(body_(r.out[0])) &&
    /5:00 PM/.test(body_(r.out[0])) &&
    /"sim"/i.test(body_(r.out[0])), 3);
check("T3.3b CAPABILITY — and NOTHING is written to Google before the owner says 'sim'",
  editWrites().length === 0, 3);

scripted = [
  { kind: "edit_review", json: JSON.stringify({ decision: "confirm", new_start_iso: null, new_duration_min: null,
      new_title: null, new_summary: null, add_emails: [], remove_emails: [], clarify: null }) },
];
r = await say("sim");
console.log(`   owner    : sim                                        <- UNTAGGED`);
console.log(`   assistant: ${shown(r.out)}`);
check(`T3.3c CAPABILITY — 'sim' WRITES the edit to Google (${editWrites().length === 1 ? editWrites()[0].name : "no write"}, ${editWrites().length} call${editWrites().length === 1 ? "" : "s"})`,
  editWrites().length === 1, 3);

// ---- T3.4 — DELETE: a quoted invite + "cancel it" -> confirm -> events.delete ------------
reset("5577777777777@s.whatsapp.net");
scripted = [
  ROUTER(),
  MERGED({ action: "delete", participants: [{ name: "Laura", email: "laura@example.com" }],
    start_iso: "2026-07-14T15:00:00-03:00" }),
  CALENDAR({ action: "delete", participants: [{ name: "Laura", email: "laura@example.com" }],
    start_iso: "2026-07-14T15:00:00-03:00" }),
];
r = await say("@secretaria cancela essa reuniao", { quote: INVITE });
console.log(`   owner    : @secretaria cancela essa reuniao          <- REPLYING to the invite`);
console.log(`   assistant: ${shown(r.out)}`);
check("T3.4a CAPABILITY — delete reaches the DELETE CONFIRM card", r.out.length === 1 && body_(r.out[0]).length > 0, 3);
check("T3.4b CAPABILITY — and NOTHING is deleted before the owner says 'sim'", deletes().length === 0, 3);

scripted = [{ kind: "confirm_classify", json: JSON.stringify({ decision: "confirm" }) }];
r = await say("sim");
console.log(`   owner    : sim                                        <- UNTAGGED`);
console.log(`   assistant: ${shown(r.out)}`);
check(`T3.4c CAPABILITY — 'sim' DELETES from Google (events.delete, ${deletes().length} call${deletes().length === 1 ? "" : "s"})`,
  deletes().length === 1, 3);

// ---- T3.5 — BOTH LANGUAGES ---------------------------------------------------------------
// The rails DETECT and CARRY the language (CONVENTIONS §1). Step 2 moves that detection into
// the merged call, so it is exactly the kind of thing a merge quietly drops.
reset("5588888888888@s.whatsapp.net");
scripted = [
  ROUTER("en"),
  CALENDAR({ title: "Call with Pedro", participants: [{ name: "Pedro", email: "pedro.teste@example.com" }],
    start_iso: "2026-07-14T16:00:00-03:00", duration_min: 30 }),
  MERGED({ title: "Call with Pedro", participants: [{ name: "Pedro", email: "pedro.teste@example.com" }],
    start_iso: "2026-07-14T16:00:00-03:00", duration_min: 30 }, { lang: "en" }),
];
r = await say("@secretaria schedule a 30 minute call with Pedro tomorrow at 4pm, pedro.teste@example.com");
console.log(`   owner    : @secretaria schedule a 30 minute call with Pedro tomorrow at 4pm   <- ENGLISH`);
console.log(`   assistant: ${shown(r.out)}`);
check("T3.5a CAPABILITY — an EN order gets an EN reply ('Confirm this event')",
  r.out.length === 1 && /Confirm this event/i.test(body_(r.out[0])), 3);
// (the pt half is already proven by every scenario above — "Confirme este evento")
check("T3.5b CAPABILITY — the PT replies above were in PT — the language is still detected and carried",
  true, 3);

// ============================================================================
//  T1.1 — THE END-TO-END TRUTH, over EVERY request the fake Anthropic saw all run.
//  Asserted here, at the bottom, because by now the suite has driven a create, a list, an
//  edit, a delete, a clarification, two confirmations and two languages through the real
//  server — which is every kind of call the product makes on this path.
// ============================================================================
console.log("\n=== STEP-1 (end to end): every request on the wire ===\n");
const noThinking = bodies.filter((b) => b?.thinking?.type !== "disabled");
console.log(
  `        ${bodies.length} requests reached Anthropic; ` +
    `${bodies.length - noThinking.length} carried thinking:{type:"disabled"}`
);
check(
  `T1.1  EVERY request reaching Anthropic carries thinking:{type:"disabled"} ` +
    `(${noThinking.length} of ${bodies.length} did NOT)`,
  bodies.length > 0 && noThinking.length === 0,
  1
);

// ============================================================================
//  HARNESS INTEGRITY. If any of these is red, NOTHING above can be trusted in EITHER
//  direction — a mis-routed or unanswered model call makes both a pass and a fail
//  meaningless. These always run, in every mode.
// ============================================================================
console.log("\n=== harness integrity ===\n");
check(`H1  every Claude call was RECOGNISED by the sniffer (${unrecognised.length} unrecognised)`,
  unrecognised.length === 0);
check(`H2  every Claude call had a pinned fixture (${unscripted.length} unscripted: ${unscripted.join(", ") || "none"})`,
  unscripted.length === 0);
check("H3  Google was NEVER contacted — every calendar call hit the local stub", !googleTouched);
// The suite must not be able to go green by driving NOTHING. If the webhook silently stopped
// starting flows (a renamed trigger tag, a boot failure), every count above would be 0 and
// several assertions would read as "few calls = fast". This is the floor under that.
check(`H4  the server was actually DRIVEN — ${bodies.length} model calls reached the fake Anthropic`,
  bodies.length >= 12);

// ---- done --------------------------------------------------------------------
console.log(
  `\n${failures === 0 ? "PASS" : `FAIL (${failures})`}` +
    (skipped ? `  [${skipped} assertions skipped — TURN_SELFTEST_STEP=1, STEP-1 section only]` : "") +
    "\n"
);

child.kill("SIGKILL");
evo.close();
llm.close();
process.exit(failures === 0 ? 0 : 1);
