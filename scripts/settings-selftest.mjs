#!/usr/bin/env node
// ============================================================================
//  Self-test for the ORCHESTRATOR CONVERSATION LOOP — card 55e00052,
//  "the orchestrator holds the conversation, the AI drives the three-state cycle".
//
//  Written BEFORE the code, from PLAN.md §Tests. Offline: no network, no real API key,
//  no Redis, no framework, no new dependency. FREE.
//
//  WHAT THIS PROVES
//  The pilot `assistant_settings` is converted from a self-driven propose/confirm skill
//  into a skill that rides the orchestrator's three-state cycle: the MODEL says `listen`
//  (ask), `execute` (act), or `done` (close), the orchestrator holds the marker between
//  messages, and `execute` is NON-TERMINAL — a returned value drives a read-back turn. On
//  the pilot's `assistant_settings` this file drives all three states end to end, plus the
//  write invariant, the caps, silence-is-free, and the repair loop. It mirrors PLAN.md's
//  Tests §1-§5 exactly; it invents no coverage of its own.
//
//  WHY IT IS RED TODAY, AND WHY THAT IS THE POINT (this is the whole point of the column)
//  None of the machinery exists yet: no orchestrator turn loop, no three-state route()
//  return (`{ say, next, skills, info }`), no `manifest.conversation`, no converted pilot.
//  At HEAD, route() reads `parsed.tasks` (a field these fixtures deliberately DO NOT carry),
//  so every scenario degrades to tasks:["other"] and the server answers "I didn't understand"
//  — never a proposal, never an execute, never a read-back. So the behavioural assertions
//  below FAIL, and they fail because the LOOP IS ABSENT, not because the harness is broken:
//  the harness integrity block at the bottom (every model call recognised, the server was
//  actually driven) stays GREEN on the same run that reports the reds.
//
//  HOW IT WORKS (harness idiom copied from scripts/turn-latency-selftest.mjs)
//  The REAL server.js is booted as a child process and driven over its REAL /webhook. Only
//  the two outside services are faked, both locally:
//    - Anthropic : a local HTTP server. It identifies WHICH call it is answering by the shape
//                  of the request body (kindOf) and returns a PINNED reply per turn. The TURN
//                  call is the merged, no-output_config call whose system prompt carries the
//                  skill catalog ("Available tasks:") — the same call the read-back turn reuses.
//    - Evolution : a local HTTP server. Records every message the assistant sends, and serves
//                  the chat history back to fetchHistory.
//  Because the pilot mutates PROCESS-GLOBAL identity (setTags rewrites the live TAGS array),
//  each scenario boots its OWN fresh child (fresh TAGS from SECRETARY_TAG, fresh in-memory
//  sessions) so scenarios cannot contaminate each other's trigger tag. The Anthropic/Evolution
//  fakes stay up for the whole run; per-scenario state is reset between children.
//
//  THE LIMIT, stated plainly. The model's outputs are PINNED, not re-derived. This suite proves
//  the ORCHESTRATION — how many turns a message takes, which state each turn is in, what closes
//  the cycle, what refuses to write, what the caps bound. It CANNOT prove a live Claude actually
//  returns `listen`-then-`execute` for a real tag order; that is model judgement, is not
//  catchable offline (CONVENTIONS §5), and is the deferred live router check
//  (scripts/router-selftest.mjs) the plan hands to the human.
//
//  Run:  node scripts/settings-selftest.mjs                    (everything; free)
//        SETTINGS_SELFTEST_DEBUG=1 node scripts/settings-selftest.mjs   (child stdout/stderr)
// ============================================================================
import http from "node:http";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, readdir, rm } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import os from "node:os";

const DEBUG = process.env.SETTINGS_SELFTEST_DEBUG === "1";
const REPO = fileURLToPath(new URL("..", import.meta.url));
const ORCH = fileURLToPath(new URL("../secretary/1. Orchestrator/", import.meta.url));
const SERVER = path.join(ORCH, "server.js");
const [PORT_APP, PORT_LLM, PORT_EVO] = [4330, 4331, 4332];

// The three-state caps PLAN.md pins (server.js module-locals, added by the Coding column).
// The suite drives past each of them; the exact numbers are asserted, not assumed.
const MAX_TURNS = 10;
const MAX_DISPATCHES = 3;

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ============================================================================
//  THE FAKES.
// ============================================================================
let history = [];      // the chat as the fake Evolution serves it to fetchHistory
let scripted = [];     // [{ kind, json }] — the pinned model replies for the CURRENT webhook
let sent = [];         // every message the assistant sent, this scenario
let llmCalls = [];     // [{ kind }] — every Claude call, in order, this scenario
let bodies = [];       // every request body the fake Anthropic saw (whole run)
let unscripted = [];   // a recognised call with no fixture -> informational
let unrecognised = []; // a call kindOf could NOT place -> harness fault
let CLOCK = 1768307000;

// Identify WHICH call the fake is answering, from the WHOLE request body.
//  - "turn"             : the merged/turn call — NO output_config, and the skill catalog
//                         ("Available tasks:") in the system prompt. The read-back turn reuses
//                         the same system prompt, so it is a "turn" too (told apart by order).
//  - "selflearn_analyze": lib/selflearning.js analyze() — a prose call fired by every capture.
//  Anything else with no output_config is a harness fault, surfaced loudly (never a default).
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
  // The NEW (@secretaria) pilot has NO output_config path. But the LEGACY (@assistant) flow — run
  // in parallel by the dual-tag change and exercised in §6 — DOES: the frozen assistant_settings
  // makes a propose call (PROPOSE_SCHEMA: tags+reasoning) and a classifyConfirmation call
  // (CONFIRM_SCHEMA: decision). Recognise those two; anything else with a schema is a real fault.
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
      text = "Likely cause: the fixture said so.\nSuspected area: scripts/settings-selftest.mjs";
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
// The pilot never touches Google, but loadSkills() imports EVERY skill at boot, incl. the
// calendar skill which imports googleapis. Stub it so boot is deterministic and no scenario
// can ever reach the network — the same ESM-loader trick turn-latency-selftest uses.
const tmp = await mkdtemp(path.join(os.tmpdir(), "settings-selftest-"));
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

// ---- boot / teardown a fresh server child -----------------------------------
async function startServer(selfLearnDir) {
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
        // DUAL-TAG: @assistant is the LEGACY (OLD) flow, @secretaria the NEW flow. The existing
        // §1-§5 drive "@secretaria …" and so exercise the NEW turn loop unchanged; §6 drives BOTH
        // tags in one server to prove OLD and NEW run side by side, isolated.
        SECRETARY_TAG: "@assistant",
        SECRETARY_TAG_NEW: "@secretaria",
        SELF_LEARNING_DIR: selfLearnDir,
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
  // webhook — turn loop, dispatch, read-back — runs async after the fast 200, so this waits
  // for the whole cycle, however many turns it took.
  let idle = 0;
  let mark = sent.length + llmCalls.length;
  while (idle < 12) {
    await new Promise((r) => setTimeout(r, 100));
    const now = sent.length + llmCalls.length;
    if (now !== mark) { mark = now; idle = 0; } else idle++;
  }
  return {
    out: sent.slice(before),
    turnCalls: llmCalls.slice(callsBefore).filter((c) => c.kind === "turn").length,
  };
}

function resetScenario() {
  history = [];
  sent = [];
  llmCalls = [];
  scripted = [];
}

// ---- fixtures: a single pinned TURN reply in the new three-state shape -------
// PLAN.md §route: route(ctx, turn) -> { say, next, skills, info, lang }. `awaitFrom` rides the
// reply so the orchestrator can persist the marker with the party it should listen to next
// (PLAN.md Sequence 6.4 "the model's declared awaitFrom").
const turnReply = (o) => ({ kind: "turn", json: JSON.stringify({ lang: "en", ...o }) });
const listen = (say, awaitFrom = "owner") =>
  turnReply({ say, next: "listen", awaitFrom, skills: [], info: null });
const silent = (awaitFrom = "owner") =>
  turnReply({ say: null, next: "listen", awaitFrom, skills: [], info: null });
const execute = (tags) =>
  turnReply({ say: null, next: "execute", skills: ["assistant_settings"], info: { tags } });
const done = (say = null) => turnReply({ say, next: "done", skills: [], info: null });

// LEGACY (@assistant) flow fixtures (§6 only). The OLD router shares the "Available tasks:" system
// prompt, so its call is a "turn" to the fake too — but it returns the OLD { tasks, lang, info }
// shape (NOT the three-state shape). Then the frozen assistant_settings makes its two
// output_config calls: propose (tags + reasoning) and classifyConfirmation (decision).
const legacyRoute = (tasks, info = null) =>
  ({ kind: "turn", json: JSON.stringify({ tasks, lang: "en", info }) });
const legacyPropose = (tags, reasoning = "Collapsing to the short form.") =>
  ({ kind: "legacy_propose", json: JSON.stringify({ tags, reasoning }) });
const legacyClassify = (decision) =>
  ({ kind: "legacy_classify", json: JSON.stringify({ decision }) });

const raw = (msgs) => msgs.join("\n~~~\n");
const has = (msgs, s) => raw(msgs).includes(s);
const nReports = async (dir) => (await readdir(dir)).filter((f) => f.endsWith(".md")).length;
const reportBlob = async (dir) => {
  const files = (await readdir(dir)).filter((f) => f.endsWith(".md"));
  const { readFile } = await import("node:fs/promises");
  const out = [];
  for (const f of files) out.push(await readFile(path.join(dir, f), "utf8"));
  return out.join("\n---\n");
};

async function scenario(title) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "settings-selftest-sl-"));
  const child = await startServer(dir);
  resetScenario();
  return { dir, child, done: () => stopServer(child) };
}

// ============================================================================
//  §1 — ask -> execute -> read-back -> close  (FLOW A, all three states)
// ============================================================================
console.log("\n=== §1  ask -> execute -> read-back -> close (flow A) ===\n");
{
  const s = await scenario();

  // TURN 1: a tag-change order. The model asks (listen) — one proposal message, nothing
  // written, the marker left open.
  scripted = [listen("Want me to switch how you summon me? Reply to confirm.", "owner")];
  const t1 = await say("@secretaria change your tag to @assist");
  console.log(`   owner    : @secretaria change your tag to @assist`);
  console.log(`   assistant: ${t1.out.map((m) => JSON.stringify(m.slice(0, 60))).join(" | ") || "(nothing)"}`);
  console.log(`   -> turn calls: ${t1.turnCalls}, messages: ${t1.out.length}`);
  check("§1.1  turn 1 (listen) sends EXACTLY ONE message", t1.out.length === 1);
  check("§1.2  …and it is the PROPOSAL (carries the model's `say`, not 'I didn't understand')",
    has(t1.out, "Reply to confirm") && !has(t1.out, "didn't understand") && !has(t1.out, "Available skills"));
  check("§1.3  turn 1 writes NOTHING yet (no outcome message names the new tag)", !has(t1.out, "@assist"));

  // TURN 2: the owner confirms (UNTAGGED). The model executes (the skill runs, saves, reports
  // ONE outcome), then the returned value drives a READ-BACK turn that says done -> close.
  scripted = [
    execute(["@assist"]), // the execute turn: dispatch the skill once
    done(null),           // the read-back turn: nothing more to say -> close
  ];
  const t2 = await say("yeah go for it");
  console.log(`   owner    : yeah go for it   <- UNTAGGED confirmation`);
  console.log(`   assistant: ${t2.out.map((m) => JSON.stringify(m.slice(0, 60))).join(" | ") || "(nothing)"}`);
  console.log(`   -> turn calls: ${t2.turnCalls}, messages: ${t2.out.length}`);
  check("§1.4  the execute turn runs the skill and sends EXACTLY ONE outcome message", t2.out.length === 1);
  check("§1.5  …the tag is APPLIED (the outcome names @assist)", has(t2.out, "@assist"));
  check("§1.6  a READ-BACK turn fires after execute (execute + read-back = 2 turn calls)", t2.turnCalls === 2);

  // The cycle CLOSED: a further untagged owner message is now ignored (the marker is gone).
  scripted = [done(null)]; // unused if closed, harmless if the loop mistakenly re-opens
  const probe = await say("thanks");
  console.log(`   owner    : thanks   <- probe: the cycle should be CLOSED`);
  check("§1.7  the conversation is CLOSED — a later untagged message triggers no turn, no reply",
    probe.turnCalls === 0 && probe.out.length === 0);

  // Across flow A the owner saw exactly TWO messages: the proposal and the outcome.
  check("§1.8  end to end: exactly TWO owner-visible messages (proposal + outcome)", sent.length === 2);

  await s.done();
}

// ============================================================================
//  §2 — THE WRITE INVARIANT: a read-back turn that tries to EXECUTE is refused.
// ============================================================================
console.log("\n=== §2  the write invariant: a read-back may not execute ===\n");
{
  const s = await scenario();

  // One legitimate execute (dispatch #1, one outcome, returns a value -> read-back), and the
  // read-back turn ILLEGALLY emits execute again. The orchestrator must treat that as done and
  // dispatch NOTHING: no second write, no @nope outcome, and a read-back-execute capture filed.
  scripted = [
    execute(["@assist"]),  // legit execute
    execute(["@nope"]),    // the read-back turn tries to write again — must be REFUSED
  ];
  const before = await nReports(s.dir);
  const t = await say("@secretaria change your tag to @assist");
  console.log(`   owner    : @secretaria change your tag to @assist`);
  console.log(`   assistant: ${t.out.map((m) => JSON.stringify(m.slice(0, 60))).join(" | ") || "(nothing)"}`);
  console.log(`   -> turn calls: ${t.turnCalls}, messages: ${t.out.length}`);
  check("§2.1  exactly ONE outcome message — the read-back's execute wrote nothing", t.out.length === 1);
  check("§2.2  the first (legit) write landed (@assist named)", has(t.out, "@assist"));
  check("§2.3  the REFUSED read-back write never happened (@nope never named)", !has(t.out, "@nope"));
  const blob = await reportBlob(s.dir);
  check("§2.4  a read-back-execute capture was filed (a report mentions 'readback')",
    (await nReports(s.dir)) > before && /readback/i.test(blob));

  await s.done();
}

// ============================================================================
//  §3 — THE CAPS: MAX_TURNS, MAX_DISPATCHES, one-dispatch-per-message.
// ============================================================================
console.log("\n=== §3a  MAX_TURNS: a conversation that never closes is capped ===\n");
{
  const s = await scenario();

  // A model that ALWAYS says listen-with-prose (a productive turn) and never done. Each such
  // turn consumes one of MAX_TURNS. Drive well past the cap and prove: the marker really did
  // stay open across messages (turn calls fired for the untagged follow-ups), then the cap
  // closed it (a capture was filed and a later message is ignored).
  scripted = [listen("still thinking about it…", "owner")];
  let openTurns = 0;
  await say("@secretaria change your tag to @assist"); // turn 1 opens the marker
  for (let i = 0; i < MAX_TURNS + 3; i++) {
    scripted = [listen("still thinking about it…", "owner")];
    const r = await say(`keep going ${i}`);
    if (r.turnCalls > 0) openTurns++;
    if (r.turnCalls === 0) break; // capped/closed
  }
  console.log(`   -> untagged follow-ups that produced a turn (marker was open): ${openTurns}`);
  check("§3a.1  the marker stayed OPEN across untagged follow-ups (they produced turns)", openTurns >= 1);
  check("§3a.2  …but the loop is BOUNDED — it did not run past MAX_TURNS forever",
    openTurns <= MAX_TURNS + 1);
  check("§3a.3  a MAX_TURNS cap capture was filed", (await nReports(s.dir)) >= 1);
  // The cycle is closed now: a fresh untagged message is ignored.
  scripted = [listen("x", "owner")];
  const after = await say("still there?");
  check("§3a.4  after the cap the conversation is CLOSED (a later message is ignored)",
    after.turnCalls === 0 && after.out.length === 0);

  await s.done();
}

console.log("\n=== §3b  MAX_DISPATCHES + one-dispatch-per-message ===\n");
{
  const s = await scenario();

  // A model that keeps saying execute across owner messages. Each message may dispatch AT MOST
  // ONCE (one batch = one dispatch, PLAN.md 6a). After MAX_DISPATCHES the next execute is
  // capped. So the number of outcome messages that name a written tag == MAX_DISPATCHES, no
  // more — the (N+1)th message writes nothing.
  const scriptExecuteThenListen = () => { scripted = [execute(["@keepx"]), silent("owner")]; };
  scriptExecuteThenListen();
  const t1 = await say("@secretaria set your tag to @keepx");
  let outcomes = has(t1.out, "@keepx") ? 1 : 0;
  for (let i = 0; i < MAX_DISPATCHES + 2; i++) {
    scriptExecuteThenListen();
    const r = await say(`again ${i}`);
    if (has(r.out, "@keepx")) outcomes++;
  }
  console.log(`   -> outcome writes seen: ${outcomes} (cap is MAX_DISPATCHES=${MAX_DISPATCHES})`);
  check(`§3b.1  dispatches are bounded at MAX_DISPATCHES (${MAX_DISPATCHES} writes, no more)`,
    outcomes === MAX_DISPATCHES);
  check("§3b.2  a MAX_DISPATCHES cap capture was filed", (await nReports(s.dir)) >= 1);

  await s.done();
}

// ============================================================================
//  §4 — SILENCE IS FREE: deliberate {say:null,next:"listen"} turns do NOT consume MAX_TURNS.
// ============================================================================
console.log("\n=== §4  deliberate silence does not consume MAX_TURNS ===\n");
{
  const s = await scenario();

  // Open the marker to listen to ANYONE (awaitFrom:"any"), then pour MORE than MAX_TURNS worth
  // of silent turns through it — chatter the model answers with {say:null,next:"listen"}. A
  // silent turn is FREE. If it were counted, the cap would have fired by now.
  scripted = [listen("okay, I'm listening — tell me the new tag.", "any")];
  await say("@secretaria I want to change my tag");
  for (let i = 0; i < MAX_TURNS + 4; i++) {
    scripted = [silent("any")];
    await say(`chatter ${i}`, { fromMe: false, pushName: "Ana" }); // a contact talking
  }
  const reportsAfterSilence = await nReports(s.dir);
  console.log(`   -> silent turns poured through: ${MAX_TURNS + 4}; cap captures filed: ${reportsAfterSilence}`);
  check("§4.1  silence filed NO cap capture (deliberate silence did not consume MAX_TURNS)",
    reportsAfterSilence === 0);
  // Prove the conversation is STILL OPEN: an owner message still drives a turn and gets a reply.
  scripted = [listen("still here — what's the new tag?", "any")];
  const probe = await say("ok here it is");
  console.log(`   owner    : ok here it is   <- the conversation should STILL be open`);
  check("§4.2  the conversation is STILL OPEN after all that silence (a turn fires, a reply is sent)",
    probe.turnCalls === 1 && probe.out.length === 1);

  await s.done();
}

// ============================================================================
//  §5 — THE REPAIR LOOP: a payload that fails checkPayload is a TURN, not a dispatch.
// ============================================================================
console.log("\n=== §5  the repair loop: a bad payload re-turns, never writes ===\n");
{
  const s = await scenario();

  // The model executes with a payload that fails the pilot's consistency check
  // (normalizeTags rejects tags with spaces). That is NOT a dispatch: describeProblems is
  // rendered back into a repair turn and the model is asked again — still in the SAME webhook.
  // Two consecutive failures hit MAX_REPAIRS -> repairGiveUp. Nothing is ever written.
  scripted = [
    execute(["@ ass ist"]),  // invalid (spaces) -> repair turn 1
    execute(["@b a d"]),     // invalid again    -> repair turn 2 -> give up
    done(null),              // spare, in case the loop asks once more
  ];
  const before = await nReports(s.dir);
  const t = await say("@secretaria rename yourself to '@ ass ist'");
  console.log(`   owner    : @secretaria rename yourself to '@ ass ist'`);
  console.log(`   assistant: ${t.out.map((m) => JSON.stringify(m.slice(0, 60))).join(" | ") || "(nothing)"}`);
  console.log(`   -> turn calls in the webhook: ${t.turnCalls}`);
  check("§5.1  a failing payload RE-TURNS (>= 2 turn calls) — it repaired, it did not dispatch",
    t.turnCalls >= 2);
  check("§5.2  NOTHING was written (no outcome message names an applied tag)",
    !has(t.out, "@ ass ist") && !has(t.out, "@b a d") && !has(t.out, "@assist"));
  check("§5.3  a repair-give-up capture was filed", (await nReports(s.dir)) > before);

  await s.done();
}

// ============================================================================
//  §6 — DUAL-TAG: OLD (@assistant) and NEW (@secretaria) run side by side in ONE server.
//  This is the load-bearing deliverable of the parallel-run change: the same running process
//  answers @secretaria with the NEW turn loop AND @assistant with the OLD propose/confirm
//  machinery, and a tag change made through the NEW flow does NOT disturb @assistant.
// ============================================================================
console.log("\n=== §6  dual-tag: OLD (@assistant) + NEW (@secretaria) in one server ===\n");
{
  const s = await scenario();

  // (A) NEW flow. @secretaria drives the three-state turn loop: propose (listen), then confirm
  // EXECUTEs the converted skill, which applies the tag to the NEW list and returns a read-back.
  scripted = [listen("Switch how you summon me to @maria? Reply to confirm.", "owner")];
  const a1 = await say("@secretaria change your tag to @maria");
  console.log(`   owner    : @secretaria change your tag to @maria     [NEW flow]`);
  console.log(`   assistant: ${a1.out.map((m) => JSON.stringify(m.slice(0, 50))).join(" | ") || "(nothing)"}`);
  check("§6.1  @secretaria(NEW): the FIRST turn is the model's proposal via the turn loop",
    a1.turnCalls === 1 && has(a1.out, "Reply to confirm") && !has(a1.out, "didn't understand"));
  scripted = [execute(["@maria"]), done(null)];
  const a2 = await say("yes do it");
  console.log(`   owner    : yes do it   <- UNTAGGED confirm [NEW flow]`);
  check("§6.2  @secretaria(NEW): confirm EXECUTEs the converted skill (@maria) + a read-back turn fires",
    has(a2.out, "@maria") && a2.turnCalls === 2);

  // (B) OLD flow, in the SAME server, AFTER the NEW-flow change above. @assistant drives the
  // LEGACY propose/confirm machinery — output_config calls, the "hold this for 15 minutes"
  // proposal — proving @assistant is untouched by the NEW-flow tag change to @maria.
  scripted = [
    legacyRoute(["assistant_settings"]), // the OLD router picks the skill
    legacyPropose(["@assist"], "Collapsing @assistant to @assist."), // …which then proposes
  ];
  const b1 = await say("@assistant change your tag to @assist");
  console.log(`   owner    : @assistant change your tag to @assist     [OLD flow]`);
  console.log(`   assistant: ${b1.out.map((m) => JSON.stringify(m.slice(0, 50))).join(" | ") || "(nothing)"}`);
  check("§6.3  @assistant(OLD): drives the LEGACY propose flow (the 15-minute hold), NOT the turn loop",
    has(b1.out, "hold this for 15 minutes") && has(b1.out, "@assist"));
  check("§6.4  @assistant(OLD): the proposal WROTE nothing yet (no 'Done'), and never leaked the NEW tag",
    !has(b1.out, "Done") && !has(b1.out, "@maria"));
  scripted = [legacyClassify("confirm")];
  const b2 = await say("sim, pode");
  console.log(`   owner    : sim, pode   <- UNTAGGED confirm [OLD flow]`);
  console.log(`   assistant: ${b2.out.map((m) => JSON.stringify(m.slice(0, 50))).join(" | ") || "(nothing)"}`);
  // The outcome names the NEW @assist and RETIRES the old @assistant (in-memory store here, so
  // it's the "no longer works" outcome, not the persisted "Done." — either way the change applied).
  check("§6.5  @assistant(OLD): the untagged confirm APPLIES via the legacy resumeConfirm (@assist, retires @assistant)",
    has(b2.out, "@assist") && has(b2.out, "no longer"));
  check("§6.6  isolation: the OLD flow never emitted the NEW tag (@maria)", !has(b2.out, "@maria"));

  await s.done();
}

// ============================================================================
//  HARNESS INTEGRITY. If any of these is red, NOTHING above can be trusted in EITHER
//  direction — the reds could be the harness, not the product.
// ============================================================================
console.log("\n=== harness integrity ===\n");
check(`H1  every Claude call was RECOGNISED by the sniffer (${unrecognised.length} unrecognised)`,
  unrecognised.length === 0);
check(`H2  the server was actually DRIVEN — ${bodies.length} model calls reached the fake Anthropic`,
  bodies.length >= 6);
if (unscripted.length)
  console.log(`  ..    ${unscripted.length} recognised-but-unscripted call(s) were answered with {} : ${unscripted.join(", ")}`);

// ---- done --------------------------------------------------------------------
console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);
evo.close();
llm.close();
await rm(tmp, { recursive: true, force: true });
process.exit(failures === 0 ? 0 : 1);
