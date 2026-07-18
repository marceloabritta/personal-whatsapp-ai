#!/usr/bin/env node
// ============================================================================
//  Self-test for the calendar_action CREATE flow (secretary/2. Skills/1. Calendar Actions).
//
//  Card 33bb6637 — "Event creation malfunction". Written BEFORE the code, from PLAN.md
//  (13:53) §Tests, and seeded from the card's own reproduction harness
//  (repro-minimal-33bb6637.mjs). Offline: no network, no API key, no Redis, no Google
//  credentials, no framework, no new dependency.
//
//  THE BUG THIS EXISTS TO PREVENT
//  The create flow had required fields a TRUTHFUL answer could not satisfy, and no channel
//  through which an answer could be anything but a *fill*:
//    - "nobody, it's just me"      -> `missingOf().noAttendees` demanded >= 1 guest, forever.
//    - "I don't have her email"    -> `missingOf().emailNames` demanded an email, forever.
//    - "forget it, drop it"        -> `resumeInfo` had NO cancel branch at all.
//  And because `resumeInfo` inferred "was that message for me?" from a field diff
//  (`if (sameMissing(before, after)) return;`), every one of those answers was met with
//  TOTAL SILENCE. The owner could not escape the gathering loop. Two live incidents.
//
//  HOW IT WORKS
//  The REAL server.js is booted as a child process and driven over its REAL /webhook.
//  Only three things are faked, all locally:
//    - Anthropic  : a local HTTP server. Model outputs are PINNED per scenario — that is
//                   what makes this deterministic, and it is also the limit of what this
//                   suite can prove (see THE LIMIT, below).
//    - Evolution  : a local HTTP server. Records every message the assistant sends.
//    - googleapis : swapped for a RECORDING STUB via an ESM loader hook written to a temp
//                   dir at run time (nothing is written inside the repo). Every calendar
//                   write is captured, so "was the event created, and with which attendees?"
//                   is an assertion and not a guess. Google is never contacted.
//
//  ⚠ THE TRAP — READ BEFORE YOU TOUCH `kindOf`.
//  The fake model has to know WHICH of the flow's five Claude calls it is answering. It
//  decides by sniffing the JSON schema it was handed. The seed harness did this:
//        if ("decision" in props) return "create_review";
//        if (keys.sort().join(",") === "participants,start_iso") return "resolve";
//  BOTH lines are fatal against the fixed schema. RESOLVE_SCHEMA gains a `decision` (so
//  every gathering call is answered with the confirm-step fixture), and it gains
//  `no_email_for` (so the exact-key-set match can never fire again). Reordering does NOT
//  fix it. `kindOf` below identifies each call POSITIVELY, works against the schema as it
//  is TODAY and as the plan will leave it, and THROWS on anything it does not recognise —
//  it never falls through to a default. A sniffer that mis-routes does not fail loudly:
//  it reports GREEN while asserting nothing. Section 0 pins it against the real, imported
//  schemas so that can never happen silently.
//
//  THE ASSERTIONS
//    0.  harness integrity (static)  — the sniffer routes every real schema correctly
//    a.  a ZERO-GUEST create succeeds — no attendees is an ordinary, complete event
//    b.  "I don't have their email"  — event IS created, guest NOT invited, owner IS TOLD
//    c.  an untagged correction gets a REPLY, not silence
//    d.  CANCEL works — "esquece, deixa pra la" ends the booking, says so, and DISARMS it
//    e.  GUARD: genuine chatter is still met with silence (no parrot, no nag)
//    f.  GUARD: a bare email sent by the GUEST is still understood as an answer
//    g.  ALL-DAY create (card 0822a8e0) — "o dia inteiro" / "de segunda a quarta o dia todo"
//        produce a REAL Google all-day event: start:{date} / end:{date}, the end EXCLUSIVE
//    h.  harness integrity (runtime) — nothing unrecognised, nothing unscripted
//  (a)-(d) MUST FAIL before the fix and pass after it. (e)-(f) MUST PASS BOTH BEFORE AND
//  AFTER: they are the over-correction guards. A red guard means the TEST is wrong.
//
//  THE LIMIT, stated plainly. The model's outputs are PINNED, not re-derived. This suite
//  proves the CODE ACCEPTS the three answers; it cannot prove a live Claude PRODUCES them
//  (decision:"cancel", no_email_for:["Laura"], participants:[]). That half is prompt
//  behaviour and is not catchable offline (CONVENTIONS §5). It is NOT a router bug —
//  scripts/router-selftest.mjs is the wrong instrument and must not be run for this card.
//  The live check is a human one, in WhatsApp; see PLAN.md §Tests.
//
//  Run:  node scripts/calendar-create-selftest.mjs
//        CAL_SELFTEST_DEBUG=1 node scripts/calendar-create-selftest.mjs   (child stdout)
// ============================================================================
import http from "node:http";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import os from "node:os";

const DEBUG = process.env.CAL_SELFTEST_DEBUG === "1";
const REPO = fileURLToPath(new URL("..", import.meta.url));
const SERVER = fileURLToPath(
  new URL("../secretary/1. Orchestrator/server.js", import.meta.url)
);
const [PORT_APP, PORT_LLM, PORT_EVO] = [4310, 4311, 4312];

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ============================================================================
//  THE CALL SNIFFER — the fake model's router. See THE TRAP above.
//  Every branch identifies its call POSITIVELY, by a property no other call in the
//  flow has. There is no default branch: an unrecognised call throws.
//
//  ⚠ IT TAKES THE WHOLE REQUEST BODY, NOT THE SCHEMA — and it has to (card 9af6967a).
//  The merged router+extractor call carries NO output_config at all: its reply format is
//  demanded in the PROMPT, which is what keeps the orchestrator generic. A sniffer handed only
//  `output_config.format.schema` is handed `undefined` and CANNOT see the absence of one. So
//  kindOf reads the body itself and identifies the merged call POSITIVELY — no schema, plus
//  the skill catalog in the system prompt.
//  🚩 If this suite ever goes red and the tempting fix is "put output_config back on the router
//  call", STOP: that silently undoes card 9af6967a. The router sends no schema, on purpose.
// ============================================================================
function kindOf(body) {
  const schema = body?.output_config?.format?.schema;

  if (!schema) {
    const sys = String(body?.system || "");
    if (/Available tasks/.test(sys)) return "route_extract"; // the merged router+extractor
    throw new Error(
      `kindOf: a call with NO output_config that is not the merged router — system="${sys.slice(0, 60)}…"`
    );
  }

  const props = schema.properties || {};
  const has = (k) => Object.prototype.hasOwnProperty.call(props, k);
  const keys = Object.keys(props);

  // CAL_SCHEMA — only it has `action`. interpret() survives the merge as the FALLBACK (a
  // shape-invalid payload, or a dual-intent turn), so this kind must stay.
  if (has("action")) return "calendar";
  if (has("new_start_iso")) return has("decision") ? "edit_review" : "edit"; // EDIT_*
  if (has("decision") && keys.length === 1) return "confirm_classify"; // CONFIRM_SCHEMA
  // REVIEW_SCHEMA (the confirm-step review). `decision` is NOT a usable discriminator —
  // the fixed RESOLVE_SCHEMA has one too. `title` + `duration_min` are fields the resolve
  // schema has never carried, in either version.
  if (has("title") && has("duration_min")) return "create_review";
  // RESOLVE_SCHEMA (the gathering pass), in BOTH shapes:
  //   today    { start_iso, participants }
  //   post-fix { decision, start_iso, participants, no_email_for }
  // Matched on what it HAS and what it CANNOT have — never on an exact key set, which is
  // precisely what broke the seed harness.
  if (has("start_iso") && has("participants") && !has("title")) return "resolve";

  throw new Error(`kindOf: UNRECOGNISED SCHEMA — properties=${JSON.stringify(keys)}`);
}

// The H0 pins below hand kindOf a bare schema; the product hands it a whole request body.
// This is the adapter, so the pins keep testing the sniffer and not the wrapper.
const asBody = (schema) => ({ output_config: { format: { type: "json_schema", schema } } });

// ============================================================================
//  0. HARNESS INTEGRITY (static). Pin the sniffer against the REAL schemas, imported
//     from the product. This is the assertion that makes a green run mean something:
//     if the Coding column changes RESOLVE_SCHEMA and the sniffer stops recognising it,
//     THIS goes red — instead of the suite quietly answering every gathering call with
//     the wrong fixture and passing on nothing.
// ============================================================================
console.log("\n=== 0. harness integrity — the fake model routes every real schema ===\n");

const P = await import(
  new URL("../secretary/2. Skills/1. Calendar Actions/prompt.js", import.meta.url).href
);
const C = await import(
  new URL("../secretary/1. Orchestrator/lib/confirm.js", import.meta.url).href
);

const safeKind = (body) => {
  try {
    return kindOf(body);
  } catch (e) {
    return `THREW(${e.message})`;
  }
};
const kindOfSchema = (schema) => safeKind(asBody(schema));

// H0.1 was the ROUTER_SCHEMA pin. It is GONE, with the schema itself: the merged
// router+extractor call sends no output_config at all, so there is no router schema left to
// pin. Its replacement is H0.1b, which pins the thing that took its place.
check(`H0.1b the MERGED call (no output_config, catalog in the system) -> "route_extract"  (got "${safeKind({ system: "You are the ROUTER + EXTRACTOR of Marcelo's secretary.\n\nAvailable tasks:\n  - \"calendar_action\": …" })}")`,
  safeKind({ system: "Available tasks:" }) === "route_extract");
check(`H0.2  CAL_SCHEMA           -> "calendar"        (got "${kindOfSchema(P.CAL_SCHEMA)}")`,
  kindOfSchema(P.CAL_SCHEMA) === "calendar");
check(`H0.3  REVIEW_SCHEMA        -> "create_review"   (got "${kindOfSchema(P.REVIEW_SCHEMA)}")`,
  kindOfSchema(P.REVIEW_SCHEMA) === "create_review");
// THE ONE THAT MATTERS. Today RESOLVE_SCHEMA is { start_iso, participants }; the fix adds
// `decision` + `no_email_for`. Both must land on "resolve" — never on "create_review".
check(`H0.4  RESOLVE_SCHEMA       -> "resolve"         (got "${kindOfSchema(P.RESOLVE_SCHEMA)}", props=${JSON.stringify(Object.keys(P.RESOLVE_SCHEMA.properties))})`,
  kindOfSchema(P.RESOLVE_SCHEMA) === "resolve");
check(`H0.5  EDIT_SCHEMA          -> "edit"            (got "${kindOfSchema(P.EDIT_SCHEMA)}")`,
  kindOfSchema(P.EDIT_SCHEMA) === "edit");
check(`H0.6  EDIT_REVIEW_SCHEMA   -> "edit_review"     (got "${kindOfSchema(P.EDIT_REVIEW_SCHEMA)}")`,
  kindOfSchema(P.EDIT_REVIEW_SCHEMA) === "edit_review");
check(`H0.7  CONFIRM_SCHEMA       -> "confirm_classify"(got "${kindOfSchema(C.CONFIRM_SCHEMA)}")`,
  kindOfSchema(C.CONFIRM_SCHEMA) === "confirm_classify");
// The resolve and create-review calls must never be confusable — that IS the trap.
check("H0.8  resolve and create_review are DISTINCT kinds",
  kindOfSchema(P.RESOLVE_SCHEMA) !== kindOfSchema(P.REVIEW_SCHEMA));
// A sniffer that guesses is worse than no sniffer: it must refuse, loudly. Both ways in:
// an unknown SCHEMA, and a schemaless call that is not the merged router.
check("H0.9  an unknown schema THROWS — the sniffer has no silent default",
  kindOfSchema({ properties: { banana: {} } }).startsWith("THREW("));
check("H0.9b a schemaless call that is NOT the merged router THROWS too",
  safeKind({ system: "you are a helpful assistant" }).startsWith("THREW("));

// ============================================================================
//  The fakes.
// ============================================================================
let CLOCK = 1768307000;
let history = [];
let scripted = []; // [{ kind, json }] — the pinned model outputs for this turn
let sent = []; // every message the assistant sent, this scenario
let llmCalls = []; // [{ kind, props }] — every Claude call, this scenario
let unscripted = []; // calls with no fixture      -> harness fault
let unrecognised = []; // calls kindOf could not route -> harness fault
let googleCalls = []; // every googleapis call the stub recorded

const evo = http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", () => {
    const p = b ? JSON.parse(b) : {};
    res.setHeader("content-type", "application/json");
    if ((req.url || "").includes("/message/sendText/")) {
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
  req.on("end", () => {
    const p = b ? JSON.parse(b) : {};
    const schema = p?.output_config?.format?.schema;
    let kind;
    try {
      kind = kindOf(p); // the WHOLE body — the merged call has no schema to sniff
    } catch (e) {
      // Refuse loudly. A fake model that guesses on an unknown call is how a suite
      // ends up green and worthless.
      unrecognised.push(Object.keys(schema?.properties || {}));
      console.log(`      !! ${e.message}`);
      res.statusCode = 500;
      res.setHeader("content-type", "application/json");
      return res.end(JSON.stringify({ type: "error", error: { message: e.message } }));
    }
    llmCalls.push({ kind, props: Object.keys(schema?.properties || {}) });
    const i = scripted.findIndex((s) => s.kind === kind);
    let text = "{}";
    if (i >= 0) text = scripted.splice(i, 1)[0].json;
    else {
      unscripted.push(kind);
      console.log(`      !! UNSCRIPTED LLM CALL: ${kind}`);
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

let _jid = "5511994224000@s.whatsapp.net";
const JID = () => _jid;

await new Promise((r) => evo.listen(PORT_EVO, r));
await new Promise((r) => llm.listen(PORT_LLM, r));

// ---- the googleapis stub, written OUTSIDE the repo (temp dir) ----------------
// calendar_action imports `googleapis` at module top, so it cannot be stubbed from the
// test process — it has to be swapped in the child's module loader. The hook records
// every call to stdout; nothing ever leaves this machine.
const tmp = await mkdtemp(path.join(os.tmpdir(), "calendar-create-selftest-"));
await writeFile(
  path.join(tmp, "gstub.mjs"),
  `const rec = (name, args) => console.log("GOOGLE_CALL " + JSON.stringify({ name, args }));
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
    get: async (a) => { rec("events.get", a); const e = new Error("Not Found"); e.code = 404; throw e; },
    patch: async (a) => { rec("events.patch", a); return { data: { id: a.eventId } }; },
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

const selfLearnDir = await mkdtemp(path.join(os.tmpdir(), "calendar-create-selflearn-"));

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
      REDIS_URL: "", // in-memory session store (lib/sessions.js)
      OWNER_NAME: "Marcelo",
      // The rails read TAGS from this env var (lib/identity.js) — pin it, so the suite is
      // immune to a future rename of the trigger tag.
      SECRETARY_TAG: "@secretaria",
      SELF_LEARNING_DIR: selfLearnDir,
      GOOGLE_CLIENT_ID: "",
      GOOGLE_CLIENT_SECRET: "",
      GOOGLE_REFRESH_TOKEN: "",
    },
    stdio: ["ignore", "pipe", "pipe"],
  }
);

// If anything ever reaches the real Google, this flips and the run fails (H3).
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
async function say(text, { fromMe = true, pushName = "Marcelo" } = {}) {
  const before = sent.length;
  await fetch(`http://127.0.0.1:${PORT_APP}/webhook`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      data: {
        key: { fromMe, remoteJid: JID(), id: "m" + ++mid },
        message: { conversation: text },
        messageTimestamp: CLOCK++,
        pushName,
      },
    }),
  });
  // Settle: wait until neither a reply nor an LLM call has happened for 1.2s.
  let idle = 0;
  let mark = sent.length + llmCalls.length;
  while (idle < 12) {
    await new Promise((r) => setTimeout(r, 100));
    const now = sent.length + llmCalls.length;
    if (now !== mark) {
      mark = now;
      idle = 0;
    } else idle++;
  }
  return sent.slice(before);
}

function reset(jid) {
  _jid = jid; // a fresh chat => a fresh (empty) session; no cross-talk between scenarios
  history = [];
  sent = [];
  llmCalls = [];
  scripted = [];
  googleCalls = [];
}

const body = (s) => String(s).replace(/^\*\[[^\]]+\]:\*\s*/, "").trim();
const shown = (out) => (out.length ? JSON.stringify(body(out[0])) : "(NOTHING AT ALL)");
const inserts = () => googleCalls.filter((c) => c.name === "events.insert");
const insertedEmails = () =>
  inserts().flatMap((c) => (c.args?.requestBody?.attendees || []).map((a) => a.email));

// ---- the pinned model outputs ------------------------------------------------
// Every fixture is written in the POST-FIX schema shape. Today's code simply ignores the
// fields it does not know (`decision`, `no_email_for`) — readReply is a plain JSON.parse —
// so ONE fixture set drives both the broken and the fixed product. That is deliberate: it
// means a red assertion below is the PRODUCT's behaviour changing, never the fixture's.
// ROUTE_CAL is a FUNCTION now, not a constant (card 9af6967a). The router call and the
// calendar extraction call used to be two round-trips and are one: a fresh order gets ONE
// merged reply carrying the routing AND the payload. So each scenario's calendar payload is
// folded into the routing fixture instead of being scripted separately.
//   before:  scripted = [ ROUTE_CAL, { kind: "calendar", json: cal({…}) } ]
//   after:   scripted = [ ROUTE_CAL({…}) ]
// It goes through cal() ON PURPOSE — cal() is the single place the payload's default shape
// lives, so a new CAL_SCHEMA field (all_day did exactly this) reaches the merged fixture for
// free, and scenario g's overrides keep working. Hand-writing `info` here is how g breaks.
// (Continuations — the "sim", the gathering replies — bypass the router entirely and keep
// their fixtures unchanged. interpret() is still the fallback, so "calendar" stays a kind.)
const ROUTE_CAL = (o = {}) => ({
  kind: "route_extract",
  json: JSON.stringify({
    tasks: ["calendar_action"],
    lang: "pt",
    info: JSON.parse(cal(o)),
  }),
});
const cal = (o) =>
  JSON.stringify({
    action: "create",
    title: null,
    participants: [],
    start_iso: null,
    duration_min: null,
    summary: "",
    // Card 0822a8e0. Both default to the TIMED shape (all_day falsy), so every scenario
    // above is untouched; scenario g overrides them. Today's code ignores fields it does
    // not know — readReply is a plain JSON.parse — so ONE fixture set drives both the
    // pre-change and post-change product, exactly as `decision`/`no_email_for` did.
    all_day: null,
    all_day_end_iso: null,
    list_mode: null,
    range_start_iso: null,
    range_end_iso: null,
    recurrence: null,
    // Card 2b586a24 (location physical/virtual). Same maintenance all_day/recurrence did above:
    // a new DECLARED manifest.inputs field must be PRESENT in the merged fixture or lib/inputs
    // rejects the payload as shape-invalid and the flow falls back to an unscripted interpret().
    // Both default null (no location), so every scenario above is untouched; overrides can set them.
    location: null,
    virtual: null,
    ...o,
  });
const resolve_ = (o) =>
  JSON.stringify({
    decision: "modify",
    start_iso: null,
    participants: null,
    no_email_for: [],
    ...o,
  });
const review = (o) =>
  JSON.stringify({
    decision: "confirm",
    title: null,
    participants: [],
    start_iso: null,
    duration_min: null,
    summary: "",
    // null = "the review says nothing about these" -> the draft's values are carried over.
    all_day: null,
    all_day_end_iso: null,
    ...o,
  });

// ============================================================================
//  a. A ZERO-GUEST CREATE SUCCEEDS.
//     "agenda amanhã 16h pegar os cachorros" — an event with nobody but the owner in it.
//     `missingOf().noAttendees` says that is incomplete and asks, forever, "quem convidar".
//     There is no truthful answer. The event must simply be created, with no attendees.
// ============================================================================
console.log("\n=== a. zero-guest create ===\n");
reset("5511111111111@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: "Pegar os cachorros",
    participants: [],
    start_iso: "2026-07-14T16:00:00-03:00",
    duration_min: 60,
  }),
  // Today the resolver IS called (the draft looks "incomplete"); after the fix it is not
  // called at all (a zero-guest draft is complete). Both fixtures are scripted so that
  // NEITHER run makes an unscripted call — the harness must be silent about the fix.
  { kind: "resolve", json: resolve_({}) },
];
let out = await say("@secretaria agenda amanha 16h pegar os cachorros");
console.log(`   owner    : @secretaria agenda amanha 16h pegar os cachorros`);
console.log(`   assistant: ${shown(out)}`);

const a1 = out.length === 1 && /Confirme este evento/i.test(body(out[0]));
check("a1  a zero-guest create reaches the CONFIRM DRAFT (not the 'quem convidar' loop)", a1);
check(
  "a2  and it does NOT ask who to invite — there is nobody to invite",
  out.length === 1 && !/quem convidar/i.test(body(out[0]))
);
// The guests line must SAY there are no guests, not print a bare, empty "- " bullet.
check(
  "a3  the draft states there are no guests (no empty '-' bullet)",
  out.length === 1 &&
    /(ningu[ée]m convidado|no guests)/i.test(body(out[0])) &&
    !/^-\s*$/m.test(body(out[0]))
);

scripted = [
  { kind: "create_review", json: review({ decision: "confirm" }) }, // the post-fix path
  { kind: "resolve", json: resolve_({}) }, // today's path (session is still await_info)
];
out = await say("sim");
console.log(`   owner    : sim                                        <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
check(
  "a4  'sim' CREATES the event — Google receives an insert",
  inserts().length === 1
);
check(
  "a5  the created event has NO attendees",
  inserts().length === 1 && insertedEmails().length === 0
);
check(
  "a6  and the owner is told it is done",
  out.length === 1 && /Convite criado/i.test(body(out[0]))
);
// THE TRIPWIRE (card 0822a8e0). Scenario g adds an all-day branch to the insert payload.
// If that branch ever inverts — or leaks into the ordinary path — a TIMED event silently
// becomes a date-only one, which is the same class of harm the card exists to fix, pointed
// the other way. a-f all drive the timed path end to end; this is the byte that pins it.
check(
  "a7  TRIPWIRE — a timed event stays TIMED: start.dateTime present, start.date ABSENT",
  inserts().length === 1 &&
    !!inserts()[0].args?.requestBody?.start?.dateTime &&
    !inserts()[0].args?.requestBody?.start?.date &&
    !!inserts()[0].args?.requestBody?.end?.dateTime &&
    !inserts()[0].args?.requestBody?.end?.date
);

// ============================================================================
//  b. "I DON'T HAVE THEIR EMAIL."  The whole card, in one scenario.
//     The event MUST be created, the guest must NOT be invited, and the owner must be
//     TOLD. A silent drop is the quieter harm the plan explicitly refuses to ship: an
//     event called "Biópsia Laura" with no Laura on it, and nobody says so.
// ============================================================================
console.log("\n=== b. \"nao tenho o email dela, pode agendar assim mesmo\" ===\n");
reset("5522222222222@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: "Biópsia Laura",
    participants: [{ name: "Laura", email: null }],
    start_iso: "2026-07-14T10:00:00-03:00",
    duration_min: 60,
  }),
  { kind: "resolve", json: resolve_({}) },
];
out = await say("@secretaria agendar biopsia da laura amanha as 10h");
console.log(`   owner    : @secretaria agendar biopsia da laura amanha as 10h`);
console.log(`   assistant: ${shown(out)}`);
// The setup, and it must behave identically before and after the fix: her email really is
// missing, so asking for it is CORRECT. The bug is what happens to the ANSWER.
check(
  "b0  (setup) the assistant asks for Laura's email — correct, before and after",
  out.length === 1 && /e-mail/i.test(body(out[0]))
);

scripted = [
  {
    kind: "resolve",
    json: resolve_({
      decision: "modify",
      participants: [{ name: "Laura", email: null }],
      no_email_for: ["Laura"],
    }),
  },
];
out = await say("nao tenho o email dela, pode agendar assim mesmo");
console.log(`   owner    : nao tenho o email dela, pode agendar assim mesmo   <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
check("b1  'I don't have their email' gets a REPLY — not silence", out.length >= 1);
check(
  "b2  it is the confirm draft, and it names Laura as someone who will NOT be invited",
  out.length >= 1 &&
    /Confirme este evento/i.test(body(out[0])) &&
    /sem convidar[\s\S]*laura/i.test(body(out[0]))
);

scripted = [
  { kind: "create_review", json: review({ decision: "confirm" }) },
  { kind: "resolve", json: resolve_({}) },
];
out = await say("sim");
console.log(`   owner    : sim                                        <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
check("b3  the event IS created", inserts().length === 1);
check(
  "b4  and Laura is NOT on the invite — no address was invented for her",
  inserts().length === 1 && !insertedEmails().some((e) => /laura/i.test(String(e)))
);
// NEVER DROP A PERSON SILENTLY. This is owner ruling #2 and it is the point of the card.
check(
  "b5  the owner is TOLD Laura was left out ('criei sem convidar a Laura')",
  out.length === 1 && /sem convidar[\s\S]*laura/i.test(body(out[0]))
);

// ============================================================================
//  c. THE UNTAGGED CORRECTION GETS A REPLY, NOT SILENCE.
//     The incident, verbatim. A gathering session is open on Laura's email; the owner
//     answers it — untagged, as every continuation is — and gets NOTHING AT ALL, because
//     `sameMissing(before, after)` infers "that wasn't for me" from a field diff.
// ============================================================================
console.log("\n=== c. untagged correction: \"nao precisa convidar a laura\" ===\n");
reset("5533333333333@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: "Biópsia Laura",
    participants: [{ name: "Laura", email: null }],
    start_iso: "2026-07-14T09:00:00-03:00",
    duration_min: 60,
  }),
  { kind: "resolve", json: resolve_({}) },
];
out = await say("@secretaria agendar amanha 9h biopsia laura");
console.log(`   owner    : @secretaria agendar amanha 9h biopsia laura`);
console.log(`   assistant: ${shown(out)}`);

scripted = [
  { kind: "resolve", json: resolve_({ decision: "modify", participants: [] }) },
];
out = await say("nao precisa convidar a laura");
console.log(`   owner    : nao precisa convidar a laura               <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
check("c1  the correction gets a REPLY — not total silence", out.length >= 1);
check(
  "c2  it is the confirm draft — an emptied guest list is an ANSWER, so the draft is complete",
  out.length >= 1 && /Confirme este evento/i.test(body(out[0]))
);
check(
  "c3  Laura is gone from the draft: no guest, no invented address",
  out.length >= 1 &&
    /(ningu[ée]m convidado|no guests)/i.test(body(out[0])) &&
    !/@/.test(body(out[0]))
);

// ============================================================================
//  d. CANCEL WORKS — AND DISARMS THE SESSION.
//     `resumeInfo` has no cancel branch at all. "esquece, deixa pra la" is met with
//     silence AND the abandoned session stays open and armed for 10 minutes: the next
//     stray message that happens to complete the draft resurrects a booking the owner
//     called off. Both halves are asserted; a naive cancel passes d1 and fails d3.
// ============================================================================
console.log("\n=== d. cancel: \"esquece, deixa pra la\" ===\n");
reset("5544444444444@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: "Reunião",
    participants: [{ name: "Laura", email: null }],
    start_iso: "2026-07-14T15:00:00-03:00",
    duration_min: 45,
  }),
  { kind: "resolve", json: resolve_({}) },
];
out = await say("@secretaria agendar reuniao com a laura amanha 15h");
console.log(`   owner    : @secretaria agendar reuniao com a laura amanha 15h`);
console.log(`   assistant: ${shown(out)}`);

scripted = [{ kind: "resolve", json: resolve_({ decision: "cancel" }) }];
out = await say("esquece, deixa pra la");
console.log(`   owner    : esquece, deixa pra la                      <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
check("d1  cancelling gets a REPLY — not silence", out.length >= 1);
check(
  "d2  and it says the booking was dropped ('não vou criar ...')",
  out.length >= 1 && /n[ãa]o vou criar/i.test(body(out[0]))
);

// The session must be CLEARED, not merely answered. Send the very message that would have
// completed the abandoned draft: it must land on nothing.
scripted = [
  {
    kind: "resolve",
    json: resolve_({
      decision: "modify",
      participants: [{ name: "Laura", email: "laura@example.com" }],
    }),
  },
  { kind: "create_review", json: review({ decision: "confirm" }) },
];
out = await say("laura@example.com");
console.log(`   owner    : laura@example.com                          <- UNTAGGED, after the cancel`);
console.log(`   assistant: ${shown(out)}`);
check(
  "d3  the cancelled session is DISARMED — the abandoned draft cannot be resurrected",
  out.length === 0
);

// And now the harm itself (T4). If the session survived the cancel, the completing message
// above re-showed the draft — so a single "sim" writes the event the owner CALLED OFF to
// his real calendar, invites and all. This is the assertion that makes a naive cancel
// (reply, but no sessions.clear) impossible to ship.
out = await say("sim");
console.log(`   owner    : sim                                        <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
check(
  "d4  NOTHING reached Google — the event the owner cancelled is never created",
  inserts().length === 0
);

// ============================================================================
//  e. GUARD — THE PARROT. Must pass BEFORE and AFTER.
//     openInquiry sets awaitFrom:"any" (skill.js:542), so the gathering session hears
//     EVERY message in the chat, from ANYONE. Replacing the silence rule with an AI
//     decision trades the mute trap for an infinite-nag trap unless `unrelated` still
//     means silence. If this one ever goes red, the fix overshot.
// ============================================================================
console.log("\n=== e. GUARD: genuine chatter is still met with silence ===\n");
reset("5555555555555@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: "Reunião",
    participants: [{ name: "Laura", email: null }],
    start_iso: "2026-07-14T11:00:00-03:00",
    duration_min: 45,
  }),
  { kind: "resolve", json: resolve_({}) },
];
out = await say("@secretaria agendar reuniao com a laura amanha 11h");
console.log(`   owner    : @secretaria agendar reuniao com a laura amanha 11h`);
console.log(`   assistant: ${shown(out)}`);

scripted = [{ kind: "resolve", json: resolve_({ decision: "unrelated" }) }];
out = await say("kkkk pois eh, o transito hoje ta impossivel", {
  fromMe: false,
  pushName: "Laura",
});
console.log(`   Laura    : kkkk pois eh, o transito hoje ta impossivel  <- CHATTER, not the owner`);
console.log(`   assistant: ${shown(out)}`);
check("e1  GUARD — chatter gets NO reply. Silence is still the right answer.", out.length === 0);

// ============================================================================
//  f. GUARD — THE BARE EMAIL FROM THE GUEST. Must pass BEFORE and AFTER.
//     This is the biggest regression risk on the card. Today Laura can answer the
//     assistant's question with nothing but her address and it works, via mergeDraft's
//     bare-email fallback (one attendee missing an email + one UN-named email in the
//     patch -> assign it). §2e turns mergeDraft's list into an authoritative REPLACE.
//     If the fallback is not kept FIRST and unchanged, this dies — and it dies quietly.
// ============================================================================
console.log("\n=== f. GUARD: a bare email, sent by the GUEST, is still an answer ===\n");
reset("5566666666666@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: "Reunião de projeto",
    participants: [{ name: "Laura", email: null }],
    start_iso: "2026-07-14T14:00:00-03:00",
    duration_min: 45,
  }),
  { kind: "resolve", json: resolve_({}) },
];
out = await say("@secretaria agendar reuniao de projeto com a laura amanha 14h");
console.log(`   owner    : @secretaria agendar reuniao de projeto com a laura amanha 14h`);
console.log(`   assistant: ${shown(out)}`);

// The naked address: the resolver reports the email with NO name attached, which is
// exactly what the bare-email fallback exists to understand.
scripted = [
  {
    kind: "resolve",
    json: resolve_({
      decision: "modify",
      participants: [{ name: null, email: "laura@example.com" }],
    }),
  },
];
out = await say("laura@example.com", { fromMe: false, pushName: "Laura" });
console.log(`   Laura    : laura@example.com                          <- the GUEST answers`);
console.log(`   assistant: ${shown(out)}`);
check(
  "f1  GUARD — the guest's bare email is understood: the confirm draft appears with it",
  out.length === 1 &&
    /Confirme este evento/i.test(body(out[0])) &&
    /laura@example\.com/i.test(body(out[0]))
);

// ============================================================================
//  g. ALL-DAY CREATE.  Card 0822a8e0.
//     "o dia inteiro" today produces a TIMED block from 00:00 to 00:00 the next day
//     (start + duration_min 1440) — a different thing, and it looks wrong on the calendar.
//     It must produce a REAL Google all-day event: start:{date} / end:{date}.
//
//     TWO DRIVES, one name. A range needs a different PINNED model output — a second
//     FIXTURE, not a second flow. Same assertions, same code path.
//
//     ⚠ THE ONE THAT LOOKS FINE WHILE BEING WRONG: Google's `end.date` is EXCLUSIVE.
//     A single day on 2026-07-14 is start 07-14 / end 07-15. Mon 13 -> Wed 15 (inclusive,
//     3 days) is start 07-13 / end 07-16 — a THURSDAY. Off by one is a 2-day event, or a
//     zero-day one Google rejects. g3 and g6 pin both shapes.
// ============================================================================
console.log("\n=== g. all-day create ===\n");

// ---- g1-g3: a SINGLE all-day day --------------------------------------------
reset("5577777777777@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: "Biópsia Laura",
    participants: [],
    all_day: true,
    // start_iso stays REQUIRED — the DAY is derived from it in CAL_TZ, so missingOf().noTime
    // still guards the null-start -> 1970 write. all_day is not a way around it.
    start_iso: "2026-07-14T00:00:00-03:00",
    all_day_end_iso: null, // single day
    duration_min: null,
  }),
];
out = await say("@secretaria agendar amanha o dia inteiro biopsia laura");
console.log(`   owner    : @secretaria agendar amanha o dia inteiro biopsia laura`);
console.log(`   assistant: ${shown(out)}`);

const gBubble = out.length === 1 ? body(out[0]) : "";
check(
  "g1  the confirm draft says the event is ALL DAY ('Dia todo') — the READ side's own words",
  /Confirme este evento/i.test(gBubble) && /dia todo/i.test(gBubble)
);
check(
  "g2  and it shows NO duration — '12:00 AM (1440 min)' is exactly the bug",
  gBubble !== "" && !/min\)/i.test(gBubble)
);

scripted = [{ kind: "create_review", json: review({ decision: "confirm" }) }];
out = await say("sim");
console.log(`   owner    : sim                                        <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);

const gReq = inserts()[0]?.args?.requestBody || {};
console.log(`   -> google : ${JSON.stringify({ start: gReq.start, end: gReq.end })}`);
check(
  "g3  Google receives a REAL all-day event: start.date=2026-07-14, end.date=2026-07-15 (EXCLUSIVE), NO dateTime",
  inserts().length === 1 &&
    gReq.start?.date === "2026-07-14" &&
    gReq.end?.date === "2026-07-15" &&
    !gReq.start?.dateTime &&
    !gReq.end?.dateTime
);

// ---- g4-g6: a multi-day RANGE ("de segunda a quarta") ------------------------
// all_day_end_iso is the LAST day the event still COVERS — inclusive. The
// inclusive -> exclusive conversion happens in exactly one place (createFromDraft).
reset("5588888888888@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: "Biópsia Laura",
    participants: [],
    all_day: true,
    start_iso: "2026-07-13T00:00:00-03:00", // Monday
    all_day_end_iso: "2026-07-15T00:00:00-03:00", // Wednesday — INCLUSIVE, the last day covered
    duration_min: null,
  }),
];
out = await say("@secretaria agendar de segunda a quarta o dia todo biopsia laura");
console.log(`   owner    : @secretaria agendar de segunda a quarta o dia todo biopsia laura`);
console.log(`   assistant: ${shown(out)}`);

const gRange = out.length === 1 ? body(out[0]) : "";
// The DAY COUNT is the owner's sanity check: a wrong range that READS like a right one is
// the real danger, and "(3 dias)" is what catches it before he says "sim".
check(
  "g4  the draft shows BOTH endpoints (13 jul, 15 jul) and the DAY COUNT '(3 dias)'",
  /13 de jul/i.test(gRange) && /15 de jul/i.test(gRange) && /\(3 dias\)/i.test(gRange)
);
check(
  "g5  and still no duration on a range",
  gRange !== "" && !/min\)/i.test(gRange)
);

scripted = [{ kind: "create_review", json: review({ decision: "confirm" }) }];
out = await say("sim");
console.log(`   owner    : sim                                        <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);

const gReq2 = inserts()[0]?.args?.requestBody || {};
console.log(`   -> google : ${JSON.stringify({ start: gReq2.start, end: gReq2.end })}`);
check(
  "g6  the EXCLUSIVE end: start.date=2026-07-13, end.date=2026-07-16 (THURSDAY — Mon..Wed inclusive)",
  inserts().length === 1 &&
    gReq2.start?.date === "2026-07-13" &&
    gReq2.end?.date === "2026-07-16" &&
    !gReq2.start?.dateTime &&
    !gReq2.end?.dateTime
);

// ============================================================================
//  i. NAME FALLBACK — no topic -> the participants' names, joined with "/".
//     When the conversation gives NO subject (title=null) the code builds the heading
//     from the names: owner first, then each guest, "/"-separated -> "Marcelo/John".
//     Before this card the separator was " & " ("Marcelo & John"). One guest WITH an
//     email + a time = a COMPLETE draft, so the confirm bubble renders at once and the
//     fallback string is visible in it (draftFromInfo's fallback only surfaces there).
// ============================================================================
console.log("\n=== i. name fallback ===\n");
reset("5599999999999@s.whatsapp.net");

scripted = [
  ROUTE_CAL({
    title: null,
    participants: [{ name: "John", email: "john@example.com" }],
    start_iso: "2026-07-14T16:00:00-03:00",
    duration_min: 60,
  }),
];
out = await say("@secretaria agendar amanha 16h com o john");
console.log(`   owner    : @secretaria agendar amanha 16h com o john`);
console.log(`   assistant: ${shown(out)}`);
check(
  "i1  no-topic fallback names the event Owner/Guest with '/'",
  out.length === 1 && /Marcelo\/John/.test(body(out[0]))
);
check(
  "i2  and NOT the old ' & ' separator",
  out.length === 1 && !/Marcelo & John/.test(body(out[0]))
);

// ============================================================================
//  h. HARNESS INTEGRITY (runtime). If any of these is red, NOTHING above can be
//     trusted in either direction — a mis-routed or unanswered model call makes both a
//     pass and a fail meaningless.
// ============================================================================
console.log("\n=== h. harness integrity — runtime ===\n");
check(
  `H1  every Claude call was RECOGNISED by the sniffer (${unrecognised.length} unrecognised)`,
  unrecognised.length === 0
);
check(
  `H2  every Claude call had a pinned fixture (${unscripted.length} unscripted: ${unscripted.join(", ") || "none"})`,
  unscripted.length === 0
);
check("H3  Google was NEVER contacted — every calendar call hit the local stub", !googleTouched);

// ---- done --------------------------------------------------------------------
console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);

child.kill("SIGKILL");
evo.close();
llm.close();
process.exit(failures === 0 ? 0 : 1);
