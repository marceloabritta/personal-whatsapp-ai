#!/usr/bin/env node
// ============================================================================
//  Self-test for card 50327a11 — "retire the @assistant flow, keep only @mary".
//
//  Written BEFORE the code, from the Tests column. Offline: no network, no real API key,
//  no Redis, no framework, no new dependency. FREE.
//
//  WHAT THIS PINS — the ONE observable behaviour change of the whole card.
//  This is a REMOVAL card, so it introduces no new capability. It DOES have exactly one
//  user-visible flip, and this file exists to pin it:
//
//    - TODAY (HEAD): a fromMe message tagged `@assistant …` (or `@assistente …`) matches the
//      LEGACY tag list, routes to the frozen legacy propose/confirm dispatch, and gets an
//      OUTBOUND REPLY.
//    - AFTER this card ships: those tags match nothing (only `@mary` survives, via
//      matchedTagNew), so the message is buffered as ordinary chatter and SILENTLY IGNORED —
//      ZERO outbound sends, no reply, no API call — exactly like any non-trigger message.
//      Meanwhile a fromMe `@mary …` message still routes through the surviving turn loop.
//
//  THE ASSERTIONS
//    §1  `@assistant …`   -> ZERO outbound sends.   (RED today: legacy flow replies.)
//    §2  `@assistente …`  -> ZERO outbound sends.   (RED today: legacy flow replies.)
//    §3  `@mary …`        -> routes + sends exactly one reply. (Green today AND after —
//                            proves the collapse did not break the surviving flow, and
//                            proves the harness is alive so §1/§2's absence is real, not a
//                            timing artifact. §3 is driven through the SAME server AFTER
//                            §1/§2, so a later message being fully processed is the bound on
//                            the "no send" assertions — not a wait-for-silence timeout.)
//
//  WHY IT IS RED TODAY, AND WHY THAT IS THE POINT (the whole point of the column).
//  The legacy flow still exists at HEAD: server.js's dual-tag gate matches `@assistant`/
//  `@assistente` with matchedTag(), selects LEGACY_FLOW, and runLegacyFlow() dispatches the
//  frozen assistant_settings skill, which sends a proposal. So §1 and §2 (expecting ZERO
//  sends) FAIL — and they fail because the legacy flow is STILL PRESENT, not because the
//  harness is broken: §3 (the surviving @mary flow) and the harness-integrity block stay
//  GREEN on the very same run that reports §1/§2 red. When the card deletes the legacy flow
//  and collapses the gate to matchedTagNew-only, §1/§2 go green with no change to this file.
//
//  HOW IT WORKS (harness idiom copied verbatim from scripts/settings-selftest.mjs).
//  The REAL server.js is booted as a child process and driven over its REAL /webhook. Only
//  the two outside services are faked, both locally:
//    - Anthropic : a local HTTP server. kindOf() identifies WHICH call it is answering from
//                  the request body shape and returns a PINNED reply per turn.
//    - Evolution : a local HTTP server. RECORDS every message the assistant sends, and serves
//                  the chat history back to fetchHistory. Assertions read the RECORDED sends
//                  after each turn settles — deterministic, never a wall-clock silence guess.
//  The server is booted ONCE with BOTH tag lists live (SECRETARY_TAG=@assistente,@assistant
//  and SECRETARY_TAG_NEW=@mary), so at HEAD the retired tags reach the legacy flow and @mary
//  reaches the new flow in the same process — the exact split this card collapses.
//
//  THE LIMIT, stated plainly. The model's outputs are PINNED, not re-derived. This suite
//  proves the ROUTING — which tag reaches which flow and whether a reply is sent — not the
//  model's judgement about the order's content. That is not catchable offline (CONVENTIONS
//  §5). This card touches no manifest.description and no router/prompt.js, so no live router
//  check is implicated.
//
//  Run:  node scripts/retire-assistant-selftest.mjs                       (everything; free)
//        RETIRE_SELFTEST_DEBUG=1 node scripts/retire-assistant-selftest.mjs  (child stdout/stderr)
// ============================================================================
import http from "node:http";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import os from "node:os";

const DEBUG = process.env.RETIRE_SELFTEST_DEBUG === "1";
const REPO = fileURLToPath(new URL("..", import.meta.url));
const ORCH = fileURLToPath(new URL("../secretary/1. Orchestrator/", import.meta.url));
const SERVER = path.join(ORCH, "server.js");
const [PORT_APP, PORT_LLM, PORT_EVO] = [4340, 4341, 4342];

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
let sent = [];         // every message the assistant sent, this run
let llmCalls = [];     // [{ kind }] — every Claude call, in order
let bodies = [];       // every request body the fake Anthropic saw
let unscripted = [];   // a recognised call with no fixture -> informational
let unrecognised = []; // a call kindOf could NOT place -> harness fault
let CLOCK = 1768307000;

// Identify WHICH call the fake is answering, from the WHOLE request body.
//  - "turn"             : the merged/turn call — NO output_config, skill catalog
//                         ("Available tasks:") in the system prompt. The LEGACY router shares
//                         that system prompt, so its route call is a "turn" to the fake too;
//                         it is told apart by the SHAPE of the pinned reply, not the kind.
//  - "selflearn_analyze": lib/selflearning.js analyze() — a prose call fired by every capture.
//  - "legacy_propose"/"legacy_classify": the frozen legacy assistant_settings output_config
//                         calls (propose: tags+reasoning; classifyConfirmation: decision).
//                         These only exist at HEAD; after the card ships they are never fired.
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
      text = "Likely cause: the fixture said so.\nSuspected area: scripts/retire-assistant-selftest.mjs";
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
// The retired tags never touch Google, but loadSkills() imports EVERY skill at boot, incl. the
// calendar skill which imports googleapis. Stub it so boot is deterministic and no turn can
// ever reach the network — the same ESM-loader trick settings-selftest uses.
const tmp = await mkdtemp(path.join(os.tmpdir(), "retire-assistant-selftest-"));
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
        // BOTH tag lists live in ONE server: at HEAD @assistante/@assistant reach the LEGACY
        // flow and @mary reaches the NEW flow — the exact split this card collapses. After the
        // card ships, SECRETARY_TAG is dropped and only SECRETARY_TAG_NEW survives; this test's
        // extra SECRETARY_TAG env is simply inert (matchedTag is gone), so §1/§2 go silent.
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
  // window and then returns with an empty slice — the deterministic proof of "no send".
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

// ---- fixtures ---------------------------------------------------------------
// NEW (@mary) flow: a single pinned three-state TURN reply. `listen` sends the model's `say`
// and holds the marker — it dispatches no skill (skills:[]), so §3 needs no converted skill.
const turnReply = (o) => ({ kind: "turn", json: JSON.stringify({ lang: "en", ...o }) });
const listen = (say, awaitFrom = "owner") =>
  turnReply({ say, next: "listen", awaitFrom, skills: [], info: null });

// LEGACY (@assistant/@assistente) flow fixtures — consumed ONLY at HEAD. The OLD router shares
// the "Available tasks:" system prompt (kind "turn") but returns the OLD { tasks, lang } shape;
// the frozen assistant_settings then makes its propose (tags+reasoning) output_config call and
// sends a proposal. After the card ships, the retired tags fire no LLM call at all, so these
// fixtures are simply never consumed.
const legacyRoute = (tasks, info = null) =>
  ({ kind: "turn", json: JSON.stringify({ tasks, lang: "en", info }) });
const legacyPropose = (tags, reasoning = "Collapsing to the short form.") =>
  ({ kind: "legacy_propose", json: JSON.stringify({ tags, reasoning }) });

const has = (msgs, s) => msgs.join("\n~~~\n").includes(s);

// ============================================================================
//  DRIVE — one server, three messages in order: retired tags first, @mary last.
// ============================================================================
const child = await startServer();

// ---- §1  @assistant -> silently ignored (ZERO sends) ------------------------
console.log("\n=== §1  @assistant is silently ignored (zero outbound sends) ===\n");
scripted = [legacyRoute(["assistant_settings"]), legacyPropose(["@assist"])];
const s1 = await say("@assistant change your tag to @assist");
console.log(`   owner    : @assistant change your tag to @assist`);
console.log(`   assistant: ${s1.out.map((m) => JSON.stringify(m.slice(0, 60))).join(" | ") || "(nothing)"}`);
console.log(`   -> sends: ${s1.out.length}, turn calls: ${s1.turnCalls}`);
check("§1.1  @assistant produced ZERO outbound sends (the retired tag matches nothing)",
  s1.out.length === 0);

// ---- §2  @assistente -> silently ignored (ZERO sends) -----------------------
console.log("\n=== §2  @assistente is silently ignored (zero outbound sends) ===\n");
scripted = [legacyRoute(["assistant_settings"]), legacyPropose(["@assist"])];
const s2 = await say("@assistente muda teu marcador para @assist");
console.log(`   owner    : @assistente muda teu marcador para @assist`);
console.log(`   assistant: ${s2.out.map((m) => JSON.stringify(m.slice(0, 60))).join(" | ") || "(nothing)"}`);
console.log(`   -> sends: ${s2.out.length}, turn calls: ${s2.turnCalls}`);
check("§2.1  @assistente produced ZERO outbound sends (the retired tag matches nothing)",
  s2.out.length === 0);

// ---- §3  @mary -> still routes + replies (surviving flow; ordering bound) ----
// Driven through the SAME server AFTER §1/§2. Because this later message IS fully processed
// (it produces a turn call and a send), §1/§2's absence of sends is proven real — the server
// was alive and reachable the whole time, not merely slow.
console.log("\n=== §3  @mary still routes and replies (surviving flow) ===\n");
scripted = [listen("On it — tell me what you need.", "owner")];
const s3 = await say("@mary marque uma reunião amanhã 15h");
console.log(`   owner    : @mary marque uma reunião amanhã 15h`);
console.log(`   assistant: ${s3.out.map((m) => JSON.stringify(m.slice(0, 60))).join(" | ") || "(nothing)"}`);
console.log(`   -> sends: ${s3.out.length}, turn calls: ${s3.turnCalls}`);
check("§3.1  @mary ROUTES — the surviving turn loop fired exactly one turn", s3.turnCalls === 1);
check("§3.2  @mary REPLIED — exactly one outbound send (the model's `say`)", s3.out.length === 1);
check("§3.3  …and it is the model's reply, not an 'I didn't understand' degrade",
  has(s3.out, "tell me what you need") && !has(s3.out, "didn't understand"));

await stopServer(child);

// ============================================================================
//  HARNESS INTEGRITY. If any of these is red, NOTHING above can be trusted in EITHER
//  direction — the reds could be the harness, not the product.
// ============================================================================
console.log("\n=== harness integrity ===\n");
check(`H1  every Claude call was RECOGNISED by the sniffer (${unrecognised.length} unrecognised)`,
  unrecognised.length === 0);
check("H2  the server was actually DRIVEN — the @mary control produced a turn call and a send",
  s3.turnCalls >= 1 && s3.out.length >= 1);
if (unscripted.length)
  console.log(`  ..    ${unscripted.length} recognised-but-unscripted call(s) answered with {} : ${unscripted.join(", ")}`);

// ---- done --------------------------------------------------------------------
console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);
evo.close();
llm.close();
await rm(tmp, { recursive: true, force: true });
process.exit(failures === 0 ? 0 : 1);
