#!/usr/bin/env node
// ============================================================================
//  Self-test for the MARY OVERHAUL — the unified per-turn call + two-phase execute
//  + stateful conversations (card 327be40b, PLAN.md Rev 3.1).
//
//  Written BEFORE the code, from PLAN.md §Tests (T1–T14). Offline: no network, no
//  API key, no Redis, no framework, no new dependency. FREE. It follows the
//  scripts/unrouted-classification-selftest.mjs + scripts/native-tools-selftest.mjs
//  idiom: DYNAMIC / NAMESPACE import of the REAL router/prompt/inputs modules, driven
//  by a FakeSDK whose messages.create returns canned messages, plus source-scans of the
//  orchestrator's own turn loop (the same guard idiom as unrouted-classification §E and
//  mary-calendar-guest-email-await §4).
//
//  THE OVERHAUL, in one paragraph. `route()` becomes ONE unified turn call carrying the
//  native toolset AND `output_config: jsonFormat(TURN_DECISION_SCHEMA)` AND adaptive
//  thinking in the SAME messages.create; it returns a THREE-decision envelope
//  {say, keepListening, execute} (no `next`, no `answer`, no `info`, no `awaitFrom`).
//  Payload extraction moves to a SECOND, per-task, schema-locked call `extract()` whose
//  schema is derived mechanically by `buildExecuteSchema(spec)` from the skill's
//  `manifest.inputs`; `checkPayload` still gates it, and a failure RE-RUNS `extract()`
//  with the `describeProblems` feedback threaded in (the Rev-3.1 fix), bounded by
//  MAX_REPAIRS → repairGiveUp. `answer()` is deleted (folded into `route()`);
//  `transcribe_audio`/`flight_search` leave the catalog; the who-gate opens to any sender;
//  a mandatory sign-off fires on a clean task-completion close.
//
//  WHY IT IS RED TODAY, AND WHY THAT IS THE POINT (the whole point of this column).
//  None of the new surface exists at HEAD:
//    - `TURN_DECISION_SCHEMA` / `extract` are not exported from router.js (T1–T5, T8).
//    - `buildExecuteSchema` is not exported from lib/inputs.js (T6, T8).
//    - `buildExtractionUser` / `renderStateBlock` are not exported from prompt.js, and
//      `buildRouterUser` ignores `audioTranscript` (T13, T14 companion).
//    - route() still emits the OLD {next, skills, info} envelope with NO output_config /
//      tools / thinking on its create call (T2, T3, T4, T5).
//    - server.js still routes repair through route() (`pendingRepair`/`turn.repair`),
//      still references awaitFrom / reply.info / reply.next / the answer branch, has no
//      `extract(` / `marker.state` / `finishedSignOff`, and the dropped skills are still
//      routable (T9, T10, T11, T12, T14).
//  Missing exports come back as `undefined` (namespace import), so each gap is a legible
//  FAILED check, never a bare module-not-found crash. The offline drives are wrapped so an
//  unexpected throw is a failing check too.
//
//  WHAT THIS SUITE DELIBERATELY CANNOT COVER (CONVENTIONS §5). It pins the DETERMINISTIC
//  layer only. It does NOT boot the real server, so the live TURN-LOOP SEQUENCE — that a
//  repair fires a SECOND extract() and not a route(), that the sign-off is actually
//  delivered on a clean close — is asserted here at the wiring layer (source-scan of the
//  loop) + the unit layer (buildExtractionUser renders the problems block). The end-to-end
//  sequence over the REAL server is exercised by the build column's updates to
//  scripts/settings-selftest.mjs + scripts/retire-assistant-selftest.mjs (which boot
//  server.js), and the model's live judgement by the paid scripts/router-selftest.mjs.
//  No fabricated offline test stands in for those.
//
//  Run:  node scripts/mary-turn-twophase-selftest.mjs
//        -> exits non-zero before the build, exits 0 after it.
// ============================================================================
import { readFile } from "node:fs/promises";
import { isOwnMessage, matchedTagNew } from "../secretary/1. Orchestrator/lib/identity.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---- the REAL modules, NAMESPACE-imported so a missing export is `undefined` -----------
// (native-tools-selftest idiom: a namespace import leaves an absent named export undefined
//  instead of throwing at module load, so T1–T14 report legible failed checks.)
const routerMod = await import("../secretary/1. Orchestrator/router/router.js");
const promptMod = await import("../secretary/1. Orchestrator/router/prompt.js");
const inputsMod = await import("../secretary/1. Orchestrator/lib/inputs.js");
const nativeMod = await import("../secretary/1. Orchestrator/lib/nativeTools.js");

const { route, extract, TURN_DECISION_SCHEMA } = routerMod;
const { checkPayload, describeProblems, buildExecuteSchema } = inputsMod;
const { buildRouterUser, buildExtractionUser, renderStateBlock } = promptMod;
const { buildNativeTools } = nativeMod;

const ORCH = "../secretary/1. Orchestrator";
const SKILLS = "../secretary/3. Mary Skills";
const readSrc = (p) => readFile(new URL(p, import.meta.url), "utf8");

// The LOCKED build defaults the plan pins (T4). A build that drifts from them goes red.
const NATIVE_MAX_TOOL_HOPS = 4; // pause_turn resume cap (default)
const MAX_REPAIRS = 3; // consecutive extraction-repair failures before repairGiveUp

// ---------------------------------------------------------------------------
//  A FakeSDK whose messages.create returns a canned message, capturing every
//  create() params/opts (native-tools-selftest.makeAnthropic idiom). `handler`
//  receives (callIndex, params, opts) and returns a message OR throws.
// ---------------------------------------------------------------------------
function makeAnthropic(handler) {
  const calls = [];
  return {
    calls,
    messages: {
      create: async (params, opts) => {
        const idx = calls.length;
        calls.push({ params, opts });
        return handler(idx, params, opts);
      },
    },
  };
}
const textMsg = (text, stop = "end_turn") => ({ stop_reason: stop, content: [{ type: "text", text }] });
// A paused server-tool turn: a trailing server_tool_use block, stop_reason pause_turn.
const SRV_USE = { type: "server_tool_use", id: "srv_1", name: "web_search", input: { query: "x" } };
const SRV_OK = {
  type: "web_search_tool_result",
  tool_use_id: "srv_1",
  content: [{ type: "web_search_result", title: "t", url: "https://x", encrypted_content: "…" }],
};
const pauseMsg = () => ({ stop_reason: "pause_turn", content: [SRV_USE, SRV_OK] });

// A catalog spec mirroring calendar_action's real manifest.inputs (skill.js:47-171), the
// shape T6/T7/T8 assert against. Faithful subset: enum discriminator, nullable scalars, an
// array-of-object (participants), and requiredWhen.
const CAL_SPEC = {
  discriminator: "action",
  fields: {
    action: { type: "enum", enum: ["find", "list", "create", "edit", "delete", "other"] },
    query: { type: "string", nullable: true },
    event_id: { type: "string", nullable: true },
    start_iso: { type: "iso", nullable: true },
    participants: {
      type: "array",
      nullable: true,
      of: { name: { type: "string", nullable: true }, email: { type: "email", nullable: true } },
    },
    list_mode: { type: "enum", enum: ["window", "next"], nullable: true },
  },
  requiredWhen: { find: [], list: ["list_mode"], create: ["start_iso"], edit: [], delete: [], other: [] },
  consistency: [{ name: "create_has_a_date", test: (i) => i.action !== "create" || !!i.start_iso }],
};
// Every declared field present (checkPayload treats an ABSENT declared field as invalid).
const calPayload = (over = {}) => ({
  action: "find",
  query: null,
  event_id: null,
  start_iso: null,
  participants: [],
  list_mode: null,
  ...over,
});

function routeCtx(anthropic, over = {}) {
  return {
    owner: "Marcelo",
    anthropic,
    model: "claude-sonnet-5",
    env: { NATIVE_TOOLS: "on" },
    order: "what is on tomorrow?",
    transcript: "OWNER: what is on tomorrow?",
    labeledTranscript: "OWNER: what is on tomorrow?",
    nowStr: "Wednesday, 29 July 2026, 10:00",
    contact: "Marcelo",
    hasQuotedAudio: false,
    quoted: null,
    catalog: [{ id: "calendar_action", description: "calendar things", inputs: CAL_SPEC, conversation: "orchestrator" }],
    tags: ["@mary"],
    media: null,
    ...over,
  };
}
// Drive route() defensively — a thrown route() is a failing check, never a crash.
async function driveRoute(anthropic, over, turn = { labeledTranscript: "OWNER: hi" }) {
  try {
    if (typeof route !== "function") return { __missing: true };
    return await route(routeCtx(anthropic, over), turn);
  } catch (e) {
    return { __threw: e?.message || String(e) };
  }
}

// A schema-valid three-decision envelope (the shape the unified call returns).
const DECISION = { say: "On tomorrow: a 10am standup.", keepListening: true, execute: [], lang: "en", pendingNeed: null };

// ============================================================================
//  T1 — schema shape. TURN_DECISION_SCHEMA is the 3-field decision, nothing legacy.
// ============================================================================
console.log("\n=== T1  TURN_DECISION_SCHEMA is the three-decision envelope ===\n");
{
  const S = TURN_DECISION_SCHEMA;
  check("T1.1  TURN_DECISION_SCHEMA is exported as an object  <-- NOT BUILT YET", !!S && typeof S === "object");
  check("T1.2  additionalProperties === false (the model cannot leak fields)", !!S && S.additionalProperties === false);
  const props = (S && S.properties) || {};
  check("T1.3  has keepListening : boolean", props.keepListening && props.keepListening.type === "boolean");
  check("T1.4  has execute : array|null", props.execute && eq(props.execute.type, ["array", "null"]));
  check("T1.5  has say / lang / pendingNeed", !!props.say && !!props.lang && !!props.pendingNeed);
  check("T1.6  NO next, NO info, NO awaitFrom in properties", !("next" in props) && !("info" in props) && !("awaitFrom" in props));
  const blob = JSON.stringify(S || {});
  check('T1.7  no "answer" enum / mode anywhere in the schema', !/"answer"/.test(blob) && !/\bnext\b/.test(blob));
}

// ============================================================================
//  T2 — a canned schema-valid envelope drives route() -> {say,keepListening,execute}.
// ============================================================================
console.log("\n=== T2  a schema-valid decision parses via route() (no info/next, not degraded) ===\n");
{
  const sdk = makeAnthropic(() => textMsg(JSON.stringify(DECISION)));
  const r = await driveRoute(sdk, {});
  check("T2.1  route() returns keepListening as a boolean  <-- returns legacy {next} today", typeof r.keepListening === "boolean");
  check("T2.2  route() returns execute as an array", Array.isArray(r.execute));
  check("T2.3  say + lang + pendingNeed carried through", r.say === DECISION.say && r.lang === "en" && r.pendingNeed === null);
  check("T2.4  degraded === false on a clean parse", r.degraded === false);
  check("T2.5  the NEW envelope carries NO `info` and NO `next`", !("info" in r) && !("next" in r));
}

// ============================================================================
//  T3 — the unified turn call attaches tools + output_config + thinking.
// ============================================================================
console.log("\n=== T3  route()'s create carries output_config + tools + adaptive thinking ===\n");
{
  const sdk = makeAnthropic(() => textMsg(JSON.stringify(DECISION)));
  await driveRoute(sdk, { env: { NATIVE_TOOLS: "on" } });
  const p = sdk.calls[0]?.params || {};
  check("T3.1  output_config.format.schema deep-equals TURN_DECISION_SCHEMA  <-- no output_config today",
    !!TURN_DECISION_SCHEMA && eq(p.output_config?.format?.schema, TURN_DECISION_SCHEMA));
  check("T3.2  params.tools is buildNativeTools(env) (web_search/web_fetch) with NATIVE_TOOLS on",
    eq(p.tools, buildNativeTools({ NATIVE_TOOLS: "on" })) && Array.isArray(p.tools) && p.tools.length >= 2);
  check("T3.3  params.thinking is { type: 'adaptive' }", eq(p.thinking, { type: "adaptive" }));

  // NATIVE_TOOLS off -> tools is [] and the call still returns a schema-valid decision.
  const sdk2 = makeAnthropic(() => textMsg(JSON.stringify(DECISION)));
  const r2 = await driveRoute(sdk2, { env: {} });
  const p2 = sdk2.calls[0]?.params || {};
  check("T3.4  NATIVE_TOOLS off -> params.tools === [] (degrades to a tool-less schema call)", eq(p2.tools, []));
  check("T3.5  …and it still returns a schema-valid decision", typeof r2.keepListening === "boolean");
}

// ============================================================================
//  T4 — a tool-using / pause_turn turn resolves to a schema-valid decision.
// ============================================================================
console.log("\n=== T4  pause_turn resume loop, bounded by NATIVE_MAX_TOOL_HOPS ===\n");
{
  // Pause once (a web-search hop), then the final schema JSON.
  const sdk = makeAnthropic((i) => (i === 0 ? pauseMsg() : textMsg(JSON.stringify(DECISION))));
  const r = await driveRoute(sdk, {});
  check("T4.1  one pause_turn then end_turn -> loops exactly once (2 create calls)  <-- no resume loop today", sdk.calls.length === 2);
  check("T4.2  the resume resends [user, assistant(content)] (2 messages on the 2nd call)",
    (sdk.calls[1]?.params?.messages || []).length === 2 && sdk.calls[1]?.params?.messages?.[1]?.role === "assistant");
  check("T4.3  returns the parsed decision, degraded === false", r.keepListening === true && r.degraded === false);

  // A forever-pause must stop at the hop cap and degrade, never run away.
  const forever = makeAnthropic(() => pauseMsg());
  const rf = await driveRoute(forever, {});
  check(`T4.4  a forever-pause is BOUNDED (<= ${NATIVE_MAX_TOOL_HOPS + 1} create calls, not infinite)`,
    forever.calls.length >= 1 && forever.calls.length <= NATIVE_MAX_TOOL_HOPS + 1);
  check("T4.5  a forever-pause degrades to {keepListening:false, execute:[], degraded:true}",
    rf.degraded === true && rf.keepListening === false && Array.isArray(rf.execute) && rf.execute.length === 0);
}

// ============================================================================
//  T5 — leaked-prose reply recovers (parseJsonReply); an unparseable reply degrades.
// ============================================================================
console.log("\n=== T5  leaked prose still recovers; refusal/unparseable degrades (fallback retained) ===\n");
{
  // Prose then the JSON object: readReply's parseJsonReply fallback recovers it.
  const leaked = makeAnthropic(() => textMsg("Sure, here you go:\n" + JSON.stringify(DECISION)));
  const rl = await driveRoute(leaked, {});
  check("T5.1  a prose-prefixed reply is RECOVERED (degraded === false)", rl.degraded === false);
  check("T5.2  …and it maps to the three-decision envelope (keepListening is a boolean)", typeof rl.keepListening === "boolean");

  // Plain prose, no JSON -> unparseable -> the degrade envelope.
  const junk = makeAnthropic(() => textMsg("sorry, I can't do that"));
  const rj = await driveRoute(junk, {});
  check("T5.3  an unparseable reply degrades to keepListening:false, execute:[], degraded:true",
    rj.degraded === true && rj.keepListening === false && Array.isArray(rj.execute) && rj.execute.length === 0);
}

// ============================================================================
//  T6 — buildExecuteSchema derives the right shape from a manifest.inputs.
// ============================================================================
console.log("\n=== T6  buildExecuteSchema(spec) — shape-only, from the declaration ===\n");
{
  if (typeof buildExecuteSchema !== "function") {
    check("T6.1  buildExecuteSchema is exported from lib/inputs.js  <-- NOT BUILT YET", false);
  } else {
    const s = buildExecuteSchema(CAL_SPEC);
    const props = s?.properties || {};
    check("T6.1  object schema, additionalProperties === false", s?.type === "object" && s.additionalProperties === false);
    check("T6.2  required lists EVERY declared field (an absent field is invalid)",
      eq([...(s.required || [])].sort(), Object.keys(CAL_SPEC.fields).sort()));
    check("T6.3  the enum discriminator is preserved", eq(props.action?.enum, CAL_SPEC.fields.action.enum));
    check("T6.4  a nullable scalar maps to a [...,'null'] union", Array.isArray(props.query?.type) && props.query.type.includes("null"));
    check("T6.5  array-of-object: participants.items is an object schema with its subfields",
      props.participants?.items?.type === "object" &&
        !!props.participants.items.properties?.name && !!props.participants.items.properties?.email);

    // A scalar-`of` array maps items to the scalar (not an object).
    const scalarSpec = { fields: { tags: { type: "array", of: { type: "string" } } }, requiredWhen: {} };
    const ss = buildExecuteSchema(scalarSpec);
    const items = ss?.properties?.tags?.items;
    check("T6.6  a scalar-`of` array maps items to the scalar (items.type === 'string')",
      items && (items.type === "string" || (Array.isArray(items.type) && items.type.includes("string"))));
  }
}

// ============================================================================
//  T7 — checkPayload gates the extraction payload (REAL checkPayload).
// ============================================================================
console.log("\n=== T7  checkPayload gates the derived payload ===\n");
{
  // A complete LIST payload (discriminator=list needs list_mode; all fields present) -> ok.
  const good = checkPayload(CAL_SPEC, calPayload({ action: "list", list_mode: "next" }));
  check("T7.1  a complete derived payload -> ok:true", good.ok === true);
  // A create missing its requiredWhen field (start_iso null) -> ok:false with the problem.
  const bad = checkPayload(CAL_SPEC, calPayload({ action: "create", start_iso: null }));
  check("T7.2  a payload missing a requiredWhen field -> ok:false", bad.ok === false);
  check("T7.3  …and the problem names the missing field (start_iso)",
    Array.isArray(bad.problems) && bad.problems.some((p) => /start_iso/.test(p)));
}

// ============================================================================
//  T8 — extract() assembles the derived schema, carries NO tools, one create call.
// ============================================================================
console.log("\n=== T8  extract() = second, per-task, schema-locked call — no tools ===\n");
{
  if (typeof extract !== "function") {
    check("T8.1  extract() is exported from router.js  <-- NOT BUILT YET", false);
    check("T8.2  extract() carries the derived schema, no tools", false);
  } else {
    const sdk = makeAnthropic(() => textMsg(JSON.stringify(calPayload({ action: "list", list_mode: "next" }))));
    let payload = null;
    try {
      payload = await extract(routeCtx(sdk), {
        labeledTranscript: "OWNER: what is on tomorrow?",
        primary: "calendar_action",
        spec: CAL_SPEC,
        stateBlock: "",
      });
    } catch (e) {
      check(`T8  extract() must not throw (${e?.message || e})`, false);
    }
    const p = sdk.calls[0]?.params || {};
    check("T8.1  exactly ONE create call for an extraction", sdk.calls.length === 1);
    check("T8.2  output_config.format.schema deep-equals buildExecuteSchema(spec)",
      typeof buildExecuteSchema === "function" && eq(p.output_config?.format?.schema, buildExecuteSchema(CAL_SPEC)));
    check("T8.3  the extraction call carries NO tools", p.tools == null || (Array.isArray(p.tools) && p.tools.length === 0));
    check("T8.4  the user prompt names the primary task", JSON.stringify(p.messages || "").includes("calendar_action"));
    check("T8.5  the parsed payload is returned", payload && payload.action === "list");
  }
}

// ============================================================================
//  T9 — catalog trim: transcribe_audio + flight_search are dropped, the FIVE stay.
//  Discovery guard via the non-routable manifest flag (the plan's chosen mechanism).
// ============================================================================
console.log("\n=== T9  catalog trim — the two dropped skills are marked non-routable ===\n");
{
  const audioSrc = await readSrc(`${SKILLS}/2. Audio transcriptions/skill.js`);
  const flightSrc = await readSrc(`${SKILLS}/6. Flight Search/skill.js`);
  const nonRoutable = (src) => /routable\s*:\s*false/.test(src);
  check("T9.1  transcribe_audio manifest is non-routable (routable:false)  <-- NOT FLAGGED YET", nonRoutable(audioSrc));
  check("T9.2  flight_search manifest is non-routable (routable:false)  <-- NOT FLAGGED YET", nonRoutable(flightSrc));

  // The five KEPT tasks must NOT be flagged non-routable.
  const kept = [
    "1. Calendar Actions", "3. Tasks", "4. Feature Requests", "5. Feedback", "7. Assistant Settings",
  ];
  let keptOk = true;
  for (const dir of kept) if (nonRoutable(await readSrc(`${SKILLS}/${dir}/skill.js`))) keptOk = false;
  check("T9.3  the FIVE kept tasks are NOT marked non-routable", keptOk);

  // loadSkills honours the flag (skips a manifest.routable === false skill).
  const serverSrc = await readSrc(`${ORCH}/server.js`);
  check("T9.4  loadSkills() skips a non-routable manifest (references routable)", /routable/.test(serverSrc));
}

// ============================================================================
//  T10 — the opened continuation gate reads a NON-OWNER message (the who-lock is gone).
//  The gate boolean, NEW logic, driven with the REAL matchers (guest-email §4 idiom) +
//  a source-scan proving server.js actually carries the new gate (the fails-before anchor).
// ============================================================================
console.log("\n=== T10  continuation gate opens to any sender while a marker is open ===\n");
{
  // The NEW gate, PLAN.md §Unit-2: `if (session?.open && !isTagged && !isOwnMsg) isContinuation = true;`
  function gatePasses({ fromMe, text, session }) {
    const tag = fromMe ? matchedTagNew(text) : null;
    const isTagged = !!tag;
    const isOwnMsg = isOwnMessage(text);
    let isContinuation = false;
    if (session?.open && !isTagged && !isOwnMsg) isContinuation = true;
    return !(!isTagged && !isContinuation); // server.js drop: `if (!isTagged && !isContinuation) return;`
  }
  const guestReply = { fromMe: false, text: "rafael@medflowfin.com" };
  check("T10.1  a non-owner (fromMe:false) untagged reply PASSES while the marker is open",
    gatePasses({ ...guestReply, session: { open: true } }) === true);
  check("T10.2  the SAME reply with NO open session is DROPPED", gatePasses({ ...guestReply, session: null }) === false);
  check("T10.3  the gate no longer keys on WHO — an owner untagged msg also continues an open marker",
    gatePasses({ fromMe: true, text: "yes go ahead", session: { open: true } }) === true);

  // Fails-before anchor: server.js actually carries the open-gate and dropped the who-lock.
  const serverSrc = await readSrc(`${ORCH}/server.js`);
  check("T10.4  server.js gates continuation on `session?.open` (not awaitFrom)  <-- NOT REWIRED YET",
    /session\?\.open\s*&&\s*!isTagged/.test(serverSrc));
  check("T10.5  server.js no longer derives `session?.awaitFrom || \"owner\"`",
    !/session\?\.awaitFrom\s*\|\|\s*"owner"/.test(serverSrc));
}

// ============================================================================
//  T11 — server wiring guard (source scan). The turn loop is rewired to the new envelope.
// ============================================================================
console.log("\n=== T11  server.js / prompt.js / router.js wiring guard ===\n");
{
  const serverSrc = await readSrc(`${ORCH}/server.js`);
  const promptSrc = await readSrc(`${ORCH}/router/prompt.js`);
  const routerSrc = await readSrc(`${ORCH}/router/router.js`);

  // GONE after the card (all present at HEAD -> RED):
  const gone = [
    ["session.awaitFrom", /session\?\.awaitFrom|marker\.awaitFrom|reply\.awaitFrom/],
    ["reply.info", /reply\.info/],
    ["reply.next", /reply\.next/],
    ["NATIVE_MAX_ANSWERS", /NATIVE_MAX_ANSWERS/],
    ['reply.next === "answer"', /next\s*===\s*"answer"/],
    ["import { answer } from router", /import\s*\{[^}]*\banswer\b[^}]*\}\s*from\s*["'][^"']*router\/router/],
    ["pendingRepair", /pendingRepair/],
    ["turnArg.repair / turn.repair", /turnArg\.repair|turn\.repair/],
  ];
  for (const [label, re] of gone)
    check(`T11.1  server.js no longer references ${label}  <-- present at HEAD`, !re.test(serverSrc));

  // PRESENT after the card (absent at HEAD -> RED, except session?.open which already exists):
  const present = [
    ["reply.keepListening", /reply\.keepListening/],
    ["reply.execute", /reply\.execute/],
    ["reply.pendingNeed", /reply\.pendingNeed/],
    ["extract(", /\bextract\s*\(/],
    ["marker.state", /marker\.state/],
    ["finishedSignOff", /finishedSignOff/],
    ["session?.open (gate)", /session\?\.open/],
  ];
  for (const [label, re] of present)
    check(`T11.2  server.js references ${label}  <-- NOT BUILT YET`, re.test(serverSrc));

  // The retired repair-prompt path: prompt.js no longer exports buildRepairUser; router.js
  // no longer builds a turn.repair user (repair is a fresh extract(), never a route() turn).
  check("T11.3  prompt.js no longer EXPORTS buildRepairUser  <-- exported at HEAD",
    !/export\s+function\s+buildRepairUser/.test(promptSrc));
  check("T11.4  router.js no longer builds a `turn.repair` user  <-- built at HEAD", !/turn\.repair/.test(routerSrc));
  check("T11.5  router.js no longer exports answer()  <-- exported at HEAD", !/export\s+async\s+function\s+answer/.test(routerSrc));
  check("T11.6  router.js exports extract()  <-- NOT BUILT YET", /export\s+async\s+function\s+extract/.test(routerSrc));
}

// ============================================================================
//  T12 — mandatory sign-off on a clean task-completion close (wiring layer).
//  The delivered-on-a-real-close behaviour is exercised end-to-end by the build column's
//  server-booting settings/retire-assistant selftests; here we lock the deterministic wiring.
// ============================================================================
console.log("\n=== T12  mandatory sign-off exists and is gated to a task-completion close ===\n");
{
  const serverSrc = await readSrc(`${ORCH}/server.js`);
  check("T12.1  ORCH_MSG.finishedSignOff exists  <-- NOT BUILT YET", /finishedSignOff\s*:/.test(serverSrc));
  check("T12.2  it is bilingual {en, pt} and interpolates the trigger tag",
    /finishedSignOff[\s\S]{0,240}\ben\s*:[\s\S]{0,200}\bpt\s*:/.test(serverSrc) &&
      /finishedSignOff[\s\S]{0,320}\(tag\)/.test(serverSrc));
  check("T12.3  the sign-off is gated on a task having run this conversation (state.didWork)",
    /state\.didWork/.test(serverSrc) && /didWork/.test(serverSrc));
  // A deliberate-silence turn is keepListening:true and so structurally CANNOT reach a close —
  // the envelope shape guarantees the distinction the sign-off relies on (asserted at T2).
  const silent = { say: null, keepListening: true, execute: [], lang: "en", pendingNeed: null };
  check("T12.4  a chatter-ignore turn is keepListening:true (never a close -> never a sign-off)",
    silent.keepListening === true && silent.execute.length === 0);
}

// ============================================================================
//  T13 — an audio transcript is rendered as TEXT (not a media block) in the turn prompt.
// ============================================================================
console.log("\n=== T13  ctx.audioTranscript is rendered inline as text (Blocker 1) ===\n");
{
  const MARK = "meet me at noon tomorrow";
  let user = "";
  try {
    user = buildRouterUser("Marcelo", {
      order: "transcribe this",
      transcript: "OWNER: transcribe this",
      hasQuotedAudio: true,
      hasQuotedCalendarLink: false,
      nowStr: "Wednesday, 29 July 2026, 10:00",
      contact: "Marcelo",
      quotedText: null,
      hasMedia: false,
      stateBlock: "",
      audioTranscript: MARK,
    });
  } catch (e) {
    check(`T13  buildRouterUser must not throw (${e?.message || e})`, false);
  }
  check("T13.1  buildRouterUser renders the audio transcript text inline  <-- ignored at HEAD",
    typeof user === "string" && user.includes(MARK));
  check("T13.2  it is labelled as an AUDIO transcription (not attached as a media block)",
    typeof user === "string" && /audio/i.test(user) && user.includes(MARK));
}

// ============================================================================
//  T14 — a checkPayload failure RE-RUNS EXTRACTION with the problems (the Rev-3.1 fix).
//  Companion unit: buildExtractionUser renders the problems block. Wiring: the repair loop
//  re-runs extract() (NOT route()), bounded by MAX_REPAIRS -> repairGiveUp.
// ============================================================================
console.log("\n=== T14  repair re-runs extract() with describeProblems threaded in ===\n");
{
  // --- companion unit: the correction feedback reaches the payload-producing call ----------
  if (typeof buildExtractionUser !== "function") {
    check("T14.1  buildExtractionUser is exported from prompt.js  <-- NOT BUILT YET", false);
    check("T14.2  …renders a non-empty problems block when problems is set", false);
    check("T14.3  …renders NO problems block when problems is null", false);
  } else {
    const problemText = describeProblems(["start_iso: required, missing"]);
    let withP = "", without = "";
    try {
      withP = buildExtractionUser("Marcelo", {
        primary: "calendar_action", transcript: "OWNER: book it", nowStr: "now", contact: "Marcelo",
        stateBlock: "", hasMedia: false, audioTranscript: null, problems: problemText,
      });
      without = buildExtractionUser("Marcelo", {
        primary: "calendar_action", transcript: "OWNER: book it", nowStr: "now", contact: "Marcelo",
        stateBlock: "", hasMedia: false, audioTranscript: null, problems: null,
      });
    } catch (e) {
      check(`T14  buildExtractionUser must not throw (${e?.message || e})`, false);
    }
    check("T14.1  buildExtractionUser is a function", true);
    check("T14.2  with problems set -> the extraction prompt carries the describeProblems text",
      typeof withP === "string" && withP.includes("start_iso"));
    check("T14.3  with problems null -> no problems block leaks into the prompt",
      typeof without === "string" && !without.includes("start_iso"));
  }

  // --- wiring: the repair loop re-runs extract(), not route(), bounded by MAX_REPAIRS -------
  const serverSrc = await readSrc(`${ORCH}/server.js`);
  // The repair path threads describeProblems INTO an extract() call (the Rev-3.1 fix).
  const repairReExtracts = /problems\s*=\s*describeProblems/.test(serverSrc) && /extract\s*\(\s*ctx/.test(serverSrc);
  check("T14.4  a failed checkPayload re-runs extract() with problems (NOT a route() repair turn)  <-- routes to route() today",
    repairReExtracts && !/pendingRepair\s*=\s*describeProblems/.test(serverSrc));
  check("T14.5  the repair loop is bounded by MAX_REPAIRS -> repairGiveUp", /MAX_REPAIRS/.test(serverSrc) && /repairGiveUp/.test(serverSrc));
  check("T14.6  MAX_REPAIRS cap constant is present (bounded, never loops forever)", /MAX_REPAIRS/.test(serverSrc) && MAX_REPAIRS === 3);
}

// ---- verdict ----------------------------------------------------------------
console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
