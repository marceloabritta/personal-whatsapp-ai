#!/usr/bin/env node
// ============================================================================
//  Self-test for card a45d87a5 — "restore the @assistant flow beside @mary (A5)".
//
//  Written BEFORE the code, from the Tests column. Offline: no network, no real API key,
//  no Redis, no framework, no new dependency. FREE.
//
//  WHAT THIS PINS — the ONE observable behaviour change of the whole card, plus the A5
//  isolation property the plan (SCOPE edge case 5) makes a hard correctness requirement:
//
//    - TODAY (HEAD): the legacy flow is RETIRED. A fromMe message tagged `@assistant …`
//      (or `@assistente …`) matches nothing (only `@mary` survives, via matchedTagNew), so
//      it is buffered as ordinary chatter and SILENTLY IGNORED — zero outbound sends, zero
//      LLM calls. And `lib/identity.js` exports NO `useNewFlowFor`.
//    - AFTER this card ships: `@assistant …`/`@assistente …` reach the restored frozen
//      legacy dispatch (runLegacyFlow) and reply, WHILE `@mary …` keeps working unchanged,
//      AND no @mary continuation — tagged or untagged, `.skill`-bearing or not — can ever be
//      routed into the LEGACY flow (the A5 discriminator, `useNewFlowFor`).
//
//  TWO PARTS.
//
//  PART A — a pure unit test of the discriminator `useNewFlowFor(session, isTagged, taggedNew)`,
//  imported from `lib/identity.js`. Deterministic, no server. This is the CONVENTIONS §5
//  "assert on the deterministic layer" proof of the A5 property, INCLUDING the case that
//  cannot be driven end-to-end today (no live @mary skill writes `.skill` on the main key):
//    A1  fresh @mary tag                                  -> NEW
//    A2  fresh legacy tag                                 -> LEGACY
//    A3  untagged @mary marker continuation               -> NEW
//    A4  untagged @mary `.skill` session, NO legacy stamp -> NEW  <-- THE A5 CASE / the pin.
//        Under the pre-retirement `useNewFlow = !session?.skill` this computes LEGACY — an
//        A5 isolation break. This assertion FAILS both on the old discriminator AND on the
//        absence of the feature (export missing), and passes ONLY with `useNewFlowFor`.
//    A5  untagged legacy-stamped (`flow:"legacy"`) session -> LEGACY
//
//  PART B — full-server drive (routing / isolation, end-to-end). The REAL server.js is booted
//  as a child process and driven over its REAL /webhook. Only the two outside services are
//  faked, both locally:
//    - Anthropic : a local HTTP server. kindOf() identifies WHICH call it is answering from
//                  the request body shape and returns a PINNED reply per turn:
//                    "turn"              — the merged @mary turn call. The LEGACY router SHARES
//                                          that "Available tasks:" system prompt, so its route
//                                          call is also a "turn" to the fake; it is told apart
//                                          by the SHAPE of the pinned reply, not the kind.
//                    "legacy_propose"    — the frozen assistant_settings propose (tags+reasoning).
//                    "legacy_classify"   — classifyConfirmation's decision call (lib/confirm.js).
//                    "selflearn_analyze" — lib/selflearning.js analyze() (prose).
//    - Evolution : a local HTTP server. RECORDS every message the assistant sends and serves
//                  the chat history back to fetchHistory.
//  Booted ONCE with BOTH tag lists live (SECRETARY_TAG=@assistente,@assistant and
//  SECRETARY_TAG_NEW=@mary), so the two flows coexist in one process — the exact split the
//  card restores. The B-assertions:
//    B1  @assistant …  -> reaches the LEGACY flow (a legacy_propose fired) + a send.  RED@HEAD.
//    B2  @assistente … -> same, the PT alias.                                          RED@HEAD.
//    B3  @mary …       -> exactly one turn call + one send, not a degrade.  Green today & after.
//    B4  (A5) untagged `yes` continuing B1's legacy session -> a legacy_classify + a send AND
//        ZERO turn calls: the `flow:"legacy"` stamp round-trips the session store and an
//        untagged legacy continuation is NOT hijacked by @mary.                         RED@HEAD.
//    B5  (A5) untagged follow-up continuing B3's @mary marker -> a turn call + ZERO legacy
//        calls: an untagged @mary continuation never selects LEGACY (the drivable half of A4).
//        Green today & after.
//    B6  (A5) @mary … again AFTER the legacy setTags of B1/B4 changed the legacy list -> a
//        turn call + a send: the legacy write did NOT mutate NEW_TAGS (distinct arrays,
//        distinct settings stores).                                          Green today & after.
//
//  WHY IT IS RED TODAY, AND WHY THAT IS THE POINT (the whole point of the column).
//  At HEAD the feature is ABSENT. Part A: `useNewFlowFor` is not exported -> A0..A5 all red
//  (legibly — the import does not crash the run; the sentinel makes each line fail). Part B:
//  the legacy flow does not exist, so `@assistant`/`@assistente`/an untagged legacy `yes`
//  match nothing and produce zero sends / zero legacy calls -> B1/B2/B4 red. Crucially the
//  reds are "feature missing", NOT "harness broken": B3/B5/B6 (the @mary controls) and the
//  harness-integrity block stay GREEN on the very same run, proving the server is alive and
//  reachable and that kindOf recognises every call. When the card restores the legacy flow
//  and adds `useNewFlowFor`, every assertion goes green with no change to this file.
//
//  THE LIMIT, stated plainly. The model's outputs are PINNED, not re-derived. This suite
//  proves the ROUTING and the ISOLATION — which tag reaches which flow, whether a reply is
//  sent, and that no @mary state leaks to legacy — not the model's judgement about content.
//  That is not catchable offline (CONVENTIONS §5). This card touches no manifest.description
//  and no router/prompt.js, so NO live router check is implicated (PLAN.md §Tests).
//
//  Run:  node scripts/assistant-restore-selftest.mjs                          (everything; free)
//        RESTORE_SELFTEST_DEBUG=1 node scripts/assistant-restore-selftest.mjs   (child stdout/stderr)
// ============================================================================
import http from "node:http";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import os from "node:os";

const DEBUG = process.env.RESTORE_SELFTEST_DEBUG === "1";
const REPO = fileURLToPath(new URL("..", import.meta.url));
const ORCH = fileURLToPath(new URL("../secretary/1. Orchestrator/", import.meta.url));
const SERVER = path.join(ORCH, "server.js");
const [PORT_APP, PORT_LLM, PORT_EVO] = [4350, 4351, 4352];

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}
const has = (msgs, s) => msgs.join("\n~~~\n").includes(s);

// ============================================================================
//  PART A — pure unit test of the A5 discriminator (no server).
//  Imported dynamically so that at HEAD, where the export is absent, the run does NOT crash
//  with a link error — instead every assertion fails legibly against a sentinel, and Part B
//  still runs so its @mary controls prove the harness is alive.
// ============================================================================
console.log("\n=== Part A — useNewFlowFor discriminator (pure unit, no server) ===\n");
let useNewFlowFor;
try {
  ({ useNewFlowFor } = await import("../secretary/1. Orchestrator/lib/identity.js"));
} catch (e) {
  console.log(`      !! importing lib/identity.js threw: ${e.message}`);
}
const present = typeof useNewFlowFor === "function";
// Sentinel caller: when the export is missing (HEAD), every === comparison below is false, so
// A1..A5 fail as "feature missing" rather than throwing a TypeError.
const F = (s, t, n) => (present ? useNewFlowFor(s, t, n) : "ABSENT");

check("A0  lib/identity.js exports useNewFlowFor (absent at HEAD -> Part A red)", present);
check("A1  fresh @mary tag -> NEW", F(null, true, true) === true);
check("A2  fresh legacy tag -> LEGACY", F(null, true, false) === false);
check("A3  untagged @mary marker continuation -> NEW",
  F({ open: true, turns: 1 }, false, false) === true);
check(
  "A4  THE A5 CASE: untagged @mary `.skill` session, no legacy stamp -> NEW, not LEGACY",
  F({ skill: "calendar_action" }, false, false) === true
);
check("A5  untagged legacy-stamped (flow:'legacy') continuation -> LEGACY",
  F({ flow: "legacy", skill: "assistant_settings" }, false, false) === false);

// ============================================================================
//  PART B — the fakes.
// ============================================================================
let history = [];      // the chat as the fake Evolution serves it to fetchHistory
let scripted = [];     // [{ kind, json }] — the pinned model replies for the CURRENT webhook
let sent = [];         // every message the assistant sent, this run
let llmCalls = [];     // [{ kind }] — every Claude call, in order
let bodies = [];       // every request body the fake Anthropic saw
let unscripted = [];   // a recognised call with no fixture -> informational
let unrecognised = []; // a call kindOf could NOT place -> harness fault
let CLOCK = 1768307000;

// Identify WHICH call the fake is answering, from the WHOLE request body.
function kindOf(body) {
  const schema = body?.output_config?.format?.schema;
  if (!schema) {
    const sys = String(body?.system || "");
    if (/Available tasks:/.test(sys)) return "turn";
    if (/senior engineer triaging a failure/i.test(sys)) return "selflearn_analyze";
    throw new Error(
      `kindOf: a no-output_config call that is neither the turn call nor the ` +
        `self-learning analyze — system="${sys.slice(0, 70)}…"`
    );
  }
  const keys = Object.keys(schema.properties || {});
  if (keys.includes("tags") && keys.includes("reasoning")) return "legacy_propose";
  if (keys.includes("decision")) return "legacy_classify";
  throw new Error(`kindOf: an unexpected output_config call — properties=${JSON.stringify(keys)}`);
}

const JID_OWNER = "5511994224000@s.whatsapp.net";

const evo = http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", () => {
    const p = b ? JSON.parse(b) : {};
    res.setHeader("content-type", "application/json");
    if ((req.url || "").includes("/message/sendText/")) {
      sent.push(String(p.text));
      // The secretary's own message re-enters history (fromMe:true) exactly as Evolution
      // would deliver it — so the next turn's transcript sees it, as in production.
      history.push({
        key: { remoteJid: JID_OWNER, fromMe: true, id: "s" + history.length },
        message: { conversation: String(p.text) },
        messageTimestamp: CLOCK++,
        pushName: "Marcelo",
      });
      return res.end("{}");
    }
    if ((req.url || "").includes("/chat/findMessages/")) {
      const byJid = p.where?.key?.remoteJid === JID_OWNER;
      return res.end(JSON.stringify(byJid ? history : []));
    }
    res.end("{}");
  });
});

const llm = http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", () => {
    const p = b ? JSON.parse(b) : {};
    bodies.push(p);
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
      text = "Likely cause: the fixture said so.\nSuspected area: scripts/assistant-restore-selftest.mjs";
    } else {
      const i = scripted.findIndex((s) => s.kind === kind);
      if (i >= 0) text = scripted.splice(i, 1)[0].json;
      else {
        unscripted.push(kind);
        console.log(`      !! UNSCRIPTED ${kind} CALL (answered with {} )`);
      }
    }
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

await new Promise((r) => evo.listen(PORT_EVO, r));
await new Promise((r) => llm.listen(PORT_LLM, r));

// ---- googleapis stub, written OUTSIDE the repo (temp dir) --------------------
// The tags never touch Google here, but loadSkills() imports EVERY skill at boot, incl. the
// calendar skills which import googleapis. Stub it so boot is deterministic and no turn can
// ever reach the network — the same ESM-loader trick the sibling selftests use.
const tmp = await mkdtemp(path.join(os.tmpdir(), "assistant-restore-selftest-"));
await writeFile(
  path.join(tmp, "gstub.mjs"),
  `const calendar = () => ({ events: { insert: async () => ({ data: {} }), list: async () => ({ data: { items: [] } }), get: async () => ({ data: {} }), patch: async () => ({ data: {} }), update: async () => ({ data: {} }), delete: async () => ({ data: {} }) } });
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

// ---- boot / teardown the server child ---------------------------------------
async function startServer() {
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
        // BOTH tag lists live in ONE server: the legacy flow answers @assistente/@assistant
        // and the @mary flow answers @mary — the exact two-flow split this card restores.
        SECRETARY_TAG: "@assistente,@assistant",
        SECRETARY_TAG_NEW: "@mary",
        SELF_LEARNING_DIR: path.join(tmp, "sl"),
        GOOGLE_CLIENT_ID: "",
        GOOGLE_CLIENT_SECRET: "",
        GOOGLE_REFRESH_TOKEN: "",
      },
      stdio: ["ignore", "pipe", "pipe"],
    }
  );
  if (DEBUG) {
    child.stdout.on("data", (b) => process.stdout.write(b));
    child.stderr.on("data", (b) => process.stderr.write(b));
  }
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("boot timeout")), 20000);
    child.stdout.on("data", (b) => {
      if (b.toString().includes("listening on port")) {
        clearTimeout(t);
        resolve();
      }
    });
  });
  return child;
}

async function stopServer(child) {
  child.kill("SIGKILL");
  await new Promise((r) => setTimeout(r, 150));
}

// ---- driving the real webhook -----------------------------------------------
let mid = 0;
async function say(text, { fromMe = true, pushName = "Marcelo" } = {}) {
  const before = sent.length;
  const callsBefore = llmCalls.length;
  await fetch(`http://127.0.0.1:${PORT_APP}/webhook`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      data: {
        key: { fromMe, remoteJid: JID_OWNER, id: "m" + ++mid },
        message: { conversation: text },
        messageTimestamp: CLOCK++,
        pushName,
      },
    }),
  });
  // Settle: wait until neither a reply nor an LLM call has happened for ~1.2s. The whole
  // webhook — gate, dispatch, read-back — runs async after the fast 200, so this waits for
  // the whole cycle. A silently-ignored message makes NEITHER, so this drains its full grace
  // window and then returns with empty slices — the deterministic proof of "no send".
  let idle = 0;
  let mark = sent.length + llmCalls.length;
  while (idle < 12) {
    await new Promise((r) => setTimeout(r, 100));
    const now = sent.length + llmCalls.length;
    if (now !== mark) { mark = now; idle = 0; } else idle++;
  }
  const calls = llmCalls.slice(callsBefore);
  const cnt = (k) => calls.filter((c) => c.kind === k).length;
  return {
    out: sent.slice(before),
    turn: cnt("turn"),
    propose: cnt("legacy_propose"),
    classify: cnt("legacy_classify"),
  };
}

// ---- fixtures ---------------------------------------------------------------
// @mary flow: a single pinned three-state TURN reply. `listen` sends the model's `say` and
// holds the @mary marker — it dispatches no skill (skills:[]), so the @mary controls need no
// converted skill and no flow stamp (raw sessions store).
const turnReply = (o) => ({ kind: "turn", json: JSON.stringify({ lang: "en", ...o }) });
const listen = (say, awaitFrom = "owner") =>
  turnReply({ say, next: "listen", awaitFrom, skills: [], info: null });

// LEGACY flow fixtures — consumed only once the flow exists. The frozen router shares the
// "Available tasks:" system prompt (kind "turn") but returns the OLD { tasks, lang } shape;
// the frozen assistant_settings then makes its propose (tags+reasoning) call and, on the next
// message, classifyConfirmation's decision call (lib/confirm.js -> "confirm"|"decline"|"unrelated").
const legacyRoute = (tasks, info = null) =>
  ({ kind: "turn", json: JSON.stringify({ tasks, lang: "en", info }) });
const legacyPropose = (tags, reasoning = "Collapsing to the short form.") =>
  ({ kind: "legacy_propose", json: JSON.stringify({ tags, reasoning }) });
const legacyClassify = (decision = "confirm") =>
  ({ kind: "legacy_classify", json: JSON.stringify({ decision }) });

// ============================================================================
//  DRIVE — one server. Order chosen so the future PASS state is coherent AND the HEAD RED
//  state is clean:
//    - B2 (@assistente) runs BEFORE B4's confirmation changes the legacy list to @assist,
//      so the @assistente alias still matches when B2 needs it.
//    - B4 (untagged yes) immediately follows B1, confirming B1's legacy session.
//    - B6 (@mary isolation) runs AFTER B4's legacy setTags, to prove NEW_TAGS was untouched.
//    - The @mary marker pair (B3 open, B5 continue) runs LAST, so no open @mary marker ever
//      precedes a legacy message (which at HEAD would otherwise turn a legacy tag into a stray
//      @mary continuation and muddy the "no send" reds).
// ============================================================================
console.log("\n=== Part B — full-server drive (routing / isolation) ===\n");
const child = await startServer();

// ---- B2  @assistente reaches the legacy flow (PT alias) ---------------------
console.log("--- B2  @assistente -> legacy flow (PT alias) ---");
scripted = [legacyRoute(["assistant_settings"]), legacyPropose(["@assist"])];
const b2 = await say("@assistente muda teu marcador para @assist");
console.log(`   -> sends: ${b2.out.length}, propose: ${b2.propose}, turn: ${b2.turn}`);
check("B2  @assistente reached the LEGACY flow — a legacy_propose call fired", b2.propose >= 1);
check("B2  @assistente got an outbound reply (≥1 send)", b2.out.length >= 1);

// ---- B1  @assistant reaches the legacy flow + opens the confirm session -----
console.log("--- B1  @assistant -> legacy flow (opens confirm session) ---");
scripted = [legacyRoute(["assistant_settings"]), legacyPropose(["@assist"])];
const b1 = await say("@assistant change your tag to @assist");
console.log(`   -> sends: ${b1.out.length}, propose: ${b1.propose}, turn: ${b1.turn}`);
check("B1  @assistant reached the LEGACY flow — a legacy_propose call fired", b1.propose >= 1);
check("B1  @assistant got an outbound reply (≥1 send)", b1.out.length >= 1);

// ---- B4 (A5)  untagged `yes` continues in LEGACY, not @mary ------------------
// The legacySessions wrapper stamped B1's session flow:"legacy"; the untagged continuation
// must select LEGACY (classifyConfirmation fires) and NEVER the @mary turn loop (zero turn).
console.log("--- B4 (A5)  untagged `yes` stays in the LEGACY flow ---");
scripted = [legacyClassify("confirm")];
const b4 = await say("yes");
console.log(`   -> sends: ${b4.out.length}, classify: ${b4.classify}, turn: ${b4.turn}`);
check("B4  untagged legacy continuation fired a legacy_classify (flow:'legacy' round-tripped)",
  b4.classify >= 1);
check("B4  …and produced a send", b4.out.length >= 1);
check("B4  …and ZERO @mary turn calls — the legacy session was NOT hijacked by @mary",
  b4.turn === 0);

// ---- B6 (A5)  @mary still matches after the legacy setTags of B1/B4 ----------
// If the legacy setTags had leaked into NEW_TAGS, @mary would stop matching and this goes red.
console.log("--- B6 (A5)  @mary still works after the legacy tag change (arrays isolated) ---");
scripted = [listen("Sure — what's the meeting?", "owner")];
const b6 = await say("@mary marque uma reunião amanhã 15h");
console.log(`   -> sends: ${b6.out.length}, turn: ${b6.turn}`);
check("B6  @mary still ROUTES after a legacy tag write — a turn call fired (NEW_TAGS intact)",
  b6.turn >= 1);
check("B6  @mary still REPLIED (≥1 send)", b6.out.length >= 1);

// ---- B3  @mary routes and replies (acceptance floor; opens a @mary marker) ---
console.log("--- B3  @mary routes + replies (opens a marker) ---");
scripted = [listen("On it — tell me what you need.", "owner")];
const b3 = await say("@mary agenda uma call na sexta");
console.log(`   -> sends: ${b3.out.length}, turn: ${b3.turn}`);
check("B3  @mary ROUTES — the surviving turn loop fired exactly one turn", b3.turn === 1);
check("B3  @mary REPLIED — exactly one outbound send (the model's `say`)", b3.out.length === 1);
check("B3  …and it is the model's reply, not an 'I didn't understand' degrade",
  has(b3.out, "tell me what you need") && !has(b3.out, "didn't understand"));

// ---- B5 (A5)  untagged @mary follow-up stays in @mary, never legacy ----------
console.log("--- B5 (A5)  untagged follow-up continues in @mary (zero legacy) ---");
scripted = [listen("Got it — Friday it is.", "owner")];
const b5 = await say("sim, pode ser sexta às 15h");
console.log(`   -> sends: ${b5.out.length}, turn: ${b5.turn}, propose: ${b5.propose}, classify: ${b5.classify}`);
check("B5  untagged @mary continuation fired a turn call (continued in @mary)", b5.turn >= 1);
check("B5  …and ZERO legacy calls — an untagged @mary session never selects LEGACY",
  b5.propose === 0 && b5.classify === 0);

await stopServer(child);

// ============================================================================
//  HARNESS INTEGRITY. If any of these is red, NOTHING above can be trusted in EITHER
//  direction — the reds could be the harness, not the product.
// ============================================================================
console.log("\n=== harness integrity ===\n");
check(`H1  every Claude call was RECOGNISED by kindOf (${unrecognised.length} unrecognised)`,
  unrecognised.length === 0);
check("H2  the server was actually DRIVEN — the @mary control produced a turn call and a send",
  b3.turn >= 1 && b3.out.length >= 1);
if (unscripted.length)
  console.log(`  ..    ${unscripted.length} recognised-but-unscripted call(s) answered with {} : ${unscripted.join(", ")}`);

// ---- done --------------------------------------------------------------------
console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);
evo.close();
llm.close();
await rm(tmp, { recursive: true, force: true });
process.exit(failures === 0 ? 0 : 1);
