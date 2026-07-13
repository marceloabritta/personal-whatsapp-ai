#!/usr/bin/env node
// ============================================================================
//  Self-test for the calendar_action EDIT flow (secretary/2. Skills/1. Calendar Actions).
//
//  Card 64ff1f1d — "Edit all day events on calendar". Written BEFORE the code, from
//  PLAN.md §The test. Offline: no network, no API key, no Redis, no Google credentials,
//  no framework, no new dependency. It mirrors scripts/calendar-create-selftest.mjs
//  one-for-one — same instrument, same fakes, same §0 static pin, same §h runtime checks.
//
//  THE BUG THIS EXISTS TO PREVENT
//  An all-day event could be READ and CREATED but never CHANGED. Reply to a biópsia invite
//  with "move a biópsia para quarta" and:
//    - `applyEditDraft`'s guard (`if (draft.start_iso && !draft.all_day)`) sent NO start and
//      NO end at all — the event was renamed and the guests re-invited, ON THE SAME DAY.
//    - the confirm bubble said "(sem horário)" where the date belongs, and "(1440 min)"
//      where an all-day event has no duration to state.
//  The guard was not wrong: patching a `dateTime` start onto an all-day event CORRUPTS it.
//  This card satisfies the guard's intent by writing the CORRECT wire shape instead, so
//  there is nothing left to refuse.
//
//  ⚠ events.update, NOT events.patch — and why the assertions read an `update` body.
//  Converting a TIMED event to an ALL-DAY one means the old `start.dateTime` must not
//  survive. A patch's ability to clear a nested field rests on Google's patch semantics,
//  which NO OFFLINE TEST CAN PROVE — a green suite would mean nothing. `events.update` is a
//  full-resource REPLACE: the half-converted event is structurally impossible, not merely
//  defended against. `resumeEditConfirm` already re-fetches the event before writing, so the
//  full resource is in hand and the body is `{ ...ev, summary, description, attendees,
//  start, end }` — no extra API call. Its ONE cost: a field we fail to carry over is CLEARED.
//  That is what the colorId tripwire (a5, f4) is for.
//
//  THE ASSERTIONS
//    0.  harness integrity (static)  — the sniffer routes every real schema correctly
//    a.  MOVE an all-day event to another day — the headline. RED before, green after.
//    b.  CHANGE ITS RANGE through the tagless refinement loop — "(3 dias)", EXCLUSIVE end
//    c.  TIMED -> ALL-DAY  — start.date written, NO dateTime left behind
//    d.  ALL-DAY -> TIMED  — start.dateTime written, NO date left behind, 45-min default
//    e.  TRIPWIRE: an ordinary TIMED move stays TIMED  (passes BEFORE and AFTER)
//    f.  TRIPWIRE: THE RULE — a RENAME-only patch carrying `new_all_day:false` leaves the
//        event ALL-DAY, on its ORIGINAL day. The worst outcome on the card, killed in code.
//    h.  harness integrity (runtime) — nothing unrecognised, nothing unscripted
//
//  THE LIMIT, stated plainly. The model's outputs are PINNED, not re-derived. This suite
//  proves the CODE writes the right wire shape; it cannot prove a live Claude EMITS
//  `new_all_day:true` for "na verdade é o dia todo". That half is prompt behaviour and is
//  not catchable offline — the live check is a human one, in WhatsApp. Same limit
//  calendar-create-selftest.mjs declares.
//
//  Run:  node scripts/calendar-edit-selftest.mjs
//        CAL_SELFTEST_DEBUG=1 node scripts/calendar-edit-selftest.mjs   (child stdout)
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
const [PORT_APP, PORT_LLM, PORT_EVO] = [4330, 4331, 4332];

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ============================================================================
//  THE CALL SNIFFER — the fake model's router. Lifted from calendar-create-selftest.mjs;
//  read THE TRAP there before touching it. Every branch identifies its call POSITIVELY,
//  by a property no other call in the flow has. There is no default branch: an
//  unrecognised call throws, because a sniffer that mis-routes does not fail loudly — it
//  reports GREEN while asserting nothing.
//
//  This card WIDENS EDIT_SCHEMA / EDIT_REVIEW_SCHEMA (new_all_day, new_all_day_end_iso).
//  Both are still routed by `new_start_iso`, so widening them cannot mis-route — and §0
//  pins that against the REAL, IMPORTED schemas so it can never rot silently.
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

  if (has("action")) return "calendar"; // CAL_SCHEMA — interpret(), the fallback
  if (has("new_start_iso")) return has("decision") ? "edit_review" : "edit"; // EDIT_*
  if (has("decision") && keys.length === 1) return "confirm_classify"; // CONFIRM_SCHEMA
  if (has("title") && has("duration_min")) return "create_review"; // REVIEW_SCHEMA
  if (has("start_iso") && has("participants") && !has("title")) return "resolve"; // RESOLVE_SCHEMA

  throw new Error(`kindOf: UNRECOGNISED SCHEMA — properties=${JSON.stringify(keys)}`);
}

// The H0 pins below hand kindOf a bare schema; the product hands it a whole request body.
const asBody = (schema) => ({ output_config: { format: { type: "json_schema", schema } } });

// ============================================================================
//  0. HARNESS INTEGRITY (static). Pin the sniffer against the REAL schemas, imported from
//     the product. This is the assertion that makes a green run mean something: this card
//     ADDS FIELDS to both edit schemas, and if that ever stopped them routing, THIS goes
//     red — instead of the suite quietly answering every edit call with the wrong fixture.
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

check(`H0.1  the MERGED router+extractor call (no output_config) -> "route_extract"`,
  safeKind({ system: "Available tasks:" }) === "route_extract");
check(`H0.2  CAL_SCHEMA           -> "calendar"        (got "${kindOfSchema(P.CAL_SCHEMA)}")`,
  kindOfSchema(P.CAL_SCHEMA) === "calendar");
check(`H0.3  REVIEW_SCHEMA        -> "create_review"   (got "${kindOfSchema(P.REVIEW_SCHEMA)}")`,
  kindOfSchema(P.REVIEW_SCHEMA) === "create_review");
check(`H0.4  RESOLVE_SCHEMA       -> "resolve"         (got "${kindOfSchema(P.RESOLVE_SCHEMA)}")`,
  kindOfSchema(P.RESOLVE_SCHEMA) === "resolve");
// THE TWO THAT MATTER ON THIS CARD. Both are WIDENED by it, and both must still route.
check(`H0.5  EDIT_SCHEMA          -> "edit"            (got "${kindOfSchema(P.EDIT_SCHEMA)}", props=${JSON.stringify(Object.keys(P.EDIT_SCHEMA.properties))})`,
  kindOfSchema(P.EDIT_SCHEMA) === "edit");
check(`H0.6  EDIT_REVIEW_SCHEMA   -> "edit_review"     (got "${kindOfSchema(P.EDIT_REVIEW_SCHEMA)}")`,
  kindOfSchema(P.EDIT_REVIEW_SCHEMA) === "edit_review");
check(`H0.7  CONFIRM_SCHEMA       -> "confirm_classify"(got "${kindOfSchema(C.CONFIRM_SCHEMA)}")`,
  kindOfSchema(C.CONFIRM_SCHEMA) === "confirm_classify");
check("H0.8  edit and edit_review are DISTINCT kinds",
  kindOfSchema(P.EDIT_SCHEMA) !== kindOfSchema(P.EDIT_REVIEW_SCHEMA));
check("H0.9  an unknown schema THROWS — the sniffer has no silent default",
  kindOfSchema({ properties: { banana: {} } }).startsWith("THREW("));

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
      kind = kindOf(p);
    } catch (e) {
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
// The event the owner is editing is keyed BY ID, so a scenario chooses which event it is
// replying to simply by quoting that event's invite link (the id is the decoded `eid`):
//   evt_allday_1 — a REAL all-day event: start:{date}, end:{date} (EXCLUSIVE), no dateTime
//   evt_timed_1  — an ordinary timed event, 10:00–10:45
// Both carry colorId:"5" — a field the product NEVER touches. `events.update` REPLACES the
// whole resource, so if the body does not carry colorId, the owner's colour is CLEARED.
// That is the one real cost of choosing update over patch, and it is what f4/a5 pin.
const tmp = await mkdtemp(path.join(os.tmpdir(), "calendar-edit-selftest-"));
await writeFile(
  path.join(tmp, "gstub.mjs"),
  `const rec = (name, args) => console.log("GOOGLE_CALL " + JSON.stringify({ name, args }));
const EVENTS = {
  evt_allday_1: {
    id: "evt_allday_1", status: "confirmed", summary: "Biópsia Laura", description: "",
    htmlLink: "https://calendar.google.com/event?eid=ALLDAY",
    colorId: "5", reminders: { useDefault: true }, sequence: 1,
    start: { date: "2026-07-14" }, end: { date: "2026-07-15" },
    attendees: [{ email: "laura@example.com", responseStatus: "needsAction" }],
  },
  evt_timed_1: {
    id: "evt_timed_1", status: "confirmed", summary: "Biópsia Laura", description: "",
    htmlLink: "https://calendar.google.com/event?eid=TIMED",
    colorId: "5", reminders: { useDefault: true }, sequence: 1,
    start: { dateTime: "2026-07-14T10:00:00-03:00", timeZone: "America/Sao_Paulo" },
    end: { dateTime: "2026-07-14T10:45:00-03:00", timeZone: "America/Sao_Paulo" },
    attendees: [{ email: "laura@example.com", responseStatus: "needsAction" }],
  },
};
const calendar = () => ({
  events: {
    get: async (a) => {
      rec("events.get", a);
      const e = EVENTS[a.eventId];
      if (!e) { const err = new Error("Not Found"); err.code = 404; throw err; }
      return { data: JSON.parse(JSON.stringify(e)) };
    },
    // The write. BOTH are stubbed on purpose: today the product calls events.patch, after
    // the card it calls events.update. The assertions read WHICHEVER write happened, so a
    // red assertion is the product's BEHAVIOUR — never a missing stub method.
    update: async (a) => {
      rec("events.update", a);
      return { data: { ...a.requestBody, id: a.eventId, htmlLink: "https://calendar.google.com/event?eid=UPDATED" } };
    },
    patch: async (a) => {
      rec("events.patch", a);
      return { data: { ...a.requestBody, id: a.eventId, htmlLink: "https://calendar.google.com/event?eid=PATCHED" } };
    },
    // Empty: matchEventTargets falls back to the decoded link (score 100), which is exactly
    // how the owner reaches an all-day event — by replying to its invite.
    list: async (a) => { rec("events.list", a); return { data: { items: [] } }; },
    insert: async (a) => { rec("events.insert", a); return { data: { id: "evt_new", status: "confirmed" } }; },
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

const selfLearnDir = await mkdtemp(path.join(os.tmpdir(), "calendar-edit-selflearn-"));

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
// A Google Calendar link carries eid = base64url("<eventId> <calendarId>") — resolveEventId
// decodes it back. Quoting an invite that carries it IS how the owner points at an event.
const eid = (id) => Buffer.from(`${id} primary`).toString("base64url");
const invite = (id, when) =>
  `*[Secretária]:* Pronto! Convite criado e enviado:\n\n- Biópsia Laura\n- laura@example.com\n- ${when}\n\nAqui está o link do evento:\nhttps://calendar.google.com/calendar/event?eid=${eid(id)}`;
const INVITE_ALLDAY = invite("evt_allday_1", "14 de jul. de 2026 · Dia todo");
const INVITE_TIMED = invite("evt_timed_1", "14 de jul. de 2026, 10:00 AM (45 min)");

let mid = 0;
async function say(text, { fromMe = true, pushName = "Marcelo", quote = null } = {}) {
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
        // Evolution puts a plain-text reply's context at the SIBLING `data.contextInfo`
        // (lib/whatsapp.js getQuoted). This is the REPLY-TO-THE-INVITE the card is about.
        ...(quote
          ? {
              contextInfo: {
                stanzaId: "q" + mid,
                quotedMessage: { conversation: quote },
              },
            }
          : {}),
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

// THE WRITE. Every calendar write the edit path made, whichever verb it used — today
// events.patch, after this card events.update. Reading BOTH is what makes a red assertion
// below mean "the product wrote the wrong thing", never "the stub lacked a method".
const writes = () =>
  googleCalls.filter((c) => c.name === "events.update" || c.name === "events.patch");
const wroteBody = () => writes()[0]?.args?.requestBody || {};
const wroteVerb = () => writes()[0]?.name || "(NO WRITE AT ALL)";
const logWrite = () =>
  console.log(
    `   -> google : ${wroteVerb()} ${JSON.stringify({
      start: wroteBody().start,
      end: wroteBody().end,
      colorId: wroteBody().colorId,
    })}`
  );

// ---- the pinned model outputs ------------------------------------------------
// Every fixture is written in the POST-CARD schema shape. Today's code ignores the fields
// it does not know (`new_all_day`, `new_all_day_end_iso`) — readReply is a plain JSON.parse
// — so ONE fixture set drives both the pre-change and the post-change product. That is
// deliberate: a red assertion below is the PRODUCT's behaviour, never the fixture's.
const ROUTE_EDIT = (o = {}) => ({
  kind: "route_extract",
  json: JSON.stringify({
    tasks: ["calendar_action"],
    lang: "pt",
    info: {
      action: "edit",
      title: null,
      // WHICH event, not what changes: the CURRENT start + the attendee, read off the
      // quoted invite. Same locator delete uses (skill.js handleEdit).
      participants: [{ name: "Laura", email: "laura@example.com" }],
      start_iso: "2026-07-14T00:00:00-03:00",
      duration_min: null,
      all_day: null,
      all_day_end_iso: null,
      summary: "",
      list_mode: null,
      range_start_iso: null,
      range_end_iso: null,
      ...o,
    },
  }),
});
// EDIT_SCHEMA — the focused edit pass. null everywhere = "this is not changing".
const edit = (o) =>
  JSON.stringify({
    new_start_iso: null,
    new_duration_min: null,
    new_title: null,
    new_summary: null,
    new_all_day: null,
    new_all_day_end_iso: null,
    add_emails: [],
    remove_emails: [],
    clarify: null,
    ...o,
  });
// EDIT_REVIEW_SCHEMA — the confirm-step review: the same change fields plus a decision.
const editReview = (o) => JSON.stringify({ ...JSON.parse(edit({})), decision: "confirm", ...o });

// ============================================================================
//  a. MOVE AN ALL-DAY EVENT TO ANOTHER DAY.  The headline of the card.
//     "move a biópsia para quarta" — today the guard sends NO start and NO end, so the
//     event is renamed and the guests re-invited ON THE SAME DAY. It must MOVE, and stay
//     all-day: start.date 2026-07-15, end.date 2026-07-16 (EXCLUSIVE), no dateTime anywhere.
// ============================================================================
console.log("\n=== a. move an ALL-DAY event to another day ===\n");
reset("5511111111111@s.whatsapp.net");

scripted = [
  ROUTE_EDIT(),
  { kind: "edit", json: edit({ new_start_iso: "2026-07-15T00:00:00-03:00", new_all_day: true }) },
];
let out = await say("@secretaria move a biopsia para quarta", { quote: INVITE_ALLDAY });
console.log(`   owner    : @secretaria move a biopsia para quarta   <- REPLY to the all-day invite`);
console.log(`   assistant: ${shown(out)}`);

const aBubble = out.length === 1 ? body(out[0]) : "";
check(
  "a1  the confirm bubble says the event is ALL DAY ('Dia todo') — never '(sem horário)'",
  /evento atualizado/i.test(aBubble) &&
    /dia todo/i.test(aBubble) &&
    !/sem hor[áa]rio/i.test(aBubble)
);
check(
  "a2  and it shows NO duration — '(45 min)' / '(1440 min)' on an all-day event is the bug",
  aBubble !== "" && !/min\)/i.test(aBubble)
);
check("a3  and it shows the NEW day (15 de jul.)", /15 de jul/i.test(aBubble));

scripted = [{ kind: "edit_review", json: editReview({ decision: "confirm" }) }];
out = await say("sim");
console.log(`   owner    : sim                                      <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
logWrite();

check(
  "a4  Google receives a REAL all-day move: start.date=2026-07-15, end.date=2026-07-16 (EXCLUSIVE), NO dateTime",
  writes().length === 1 &&
    wroteBody().start?.date === "2026-07-15" &&
    wroteBody().end?.date === "2026-07-16" &&
    !wroteBody().start?.dateTime &&
    !wroteBody().end?.dateTime
);
check(
  `a5  the write is events.UPDATE — a full-resource replace, so no old dateTime can survive (got "${wroteVerb()}")`,
  wroteVerb() === "events.update"
);
// THE COST OF CHOOSING UPDATE. What the body does not carry, Google CLEARS. The freshly
// fetched event is spread into the body precisely so the fields we never touch survive.
check(
  "a6  TRIPWIRE — colorId:'5' (a field we never touch) SURVIVES the update; update CLEARS what it does not receive",
  wroteBody().colorId === "5"
);
check(
  "a7  the 'done' message says Dia todo too, and states no duration",
  out.length === 1 && /dia todo/i.test(body(out[0])) && !/min\)/i.test(body(out[0]))
);

// ============================================================================
//  b. CHANGE THE RANGE, through the TAGLESS REFINEMENT LOOP.
//     The confirm session stays open, so the owner keeps adjusting before anything is
//     written ("na verdade vai até sexta"). `new_all_day_end_iso` is the LAST day the event
//     STILL COVERS — INCLUSIVE. Wed 15 -> Fri 17 is THREE days, and Google's end.date is
//     2026-07-18 (a SATURDAY). Off by one is a 2-day event, or one Google rejects.
// ============================================================================
console.log("\n=== b. change the RANGE — \"na verdade vai até sexta\" ===\n");
reset("5522222222222@s.whatsapp.net");

scripted = [
  ROUTE_EDIT(),
  { kind: "edit", json: edit({ new_start_iso: "2026-07-15T00:00:00-03:00", new_all_day: true }) },
];
out = await say("@secretaria move a biopsia para quarta", { quote: INVITE_ALLDAY });
console.log(`   owner    : @secretaria move a biopsia para quarta`);
console.log(`   assistant: ${shown(out)}`);

// The refinement — UNTAGGED, while the confirm is open. Only the range end changes: no new
// start, no new title. `new_all_day_end_iso` alone MUST count as a change (hasEditChange).
scripted = [
  {
    kind: "edit_review",
    json: editReview({
      decision: "modify",
      new_all_day_end_iso: "2026-07-17T00:00:00-03:00", // FRIDAY — inclusive, the last day covered
    }),
  },
];
out = await say("na verdade vai ate sexta");
console.log(`   owner    : na verdade vai ate sexta                 <- UNTAGGED, refinement`);
console.log(`   assistant: ${shown(out)}`);

const bBubble = out.length === 1 ? body(out[0]) : "";
// The DAY COUNT is the owner's sanity check — a wrong range that READS right is the real
// danger, and "(3 dias)" is what catches it before he says "sim". Same words the create
// bubble prints (localizeWhen).
check(
  "b1  the refined bubble shows BOTH endpoints (15 jul, 17 jul) and the DAY COUNT '(3 dias)'",
  /15 de jul/i.test(bBubble) && /17 de jul/i.test(bBubble) && /\(3 dias\)/i.test(bBubble)
);
check("b2  and still no duration on a range", bBubble !== "" && !/min\)/i.test(bBubble));

scripted = [{ kind: "edit_review", json: editReview({ decision: "confirm" }) }];
out = await say("sim");
console.log(`   owner    : sim                                      <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
logWrite();

check(
  "b3  the EXCLUSIVE end: start.date=2026-07-15, end.date=2026-07-18 (SATURDAY — Wed..Fri inclusive)",
  writes().length === 1 &&
    wroteBody().start?.date === "2026-07-15" &&
    wroteBody().end?.date === "2026-07-18" &&
    !wroteBody().start?.dateTime
);

// ============================================================================
//  c. TIMED -> ALL-DAY.  "na verdade é o dia todo" on a 10:00 appointment.
//     THE WRITE THE GUARD EXISTS TO PREVENT, now done correctly. The event currently
//     carries start.dateTime; the new resource must carry start.date and NOTHING ELSE —
//     a half-converted event (both, or a stale dateTime) is what corrupts the calendar.
//     Note the patch carries NO new_start_iso: the DAY comes from the event's own start.
// ============================================================================
console.log("\n=== c. TIMED -> ALL-DAY — \"na verdade é o dia todo\" ===\n");
reset("5533333333333@s.whatsapp.net");

scripted = [
  ROUTE_EDIT({ start_iso: "2026-07-14T10:00:00-03:00" }),
  { kind: "edit", json: edit({ new_all_day: true }) }, // no new_start_iso — all_day ALONE is a change
];
out = await say("@secretaria na verdade e o dia todo", { quote: INVITE_TIMED });
console.log(`   owner    : @secretaria na verdade e o dia todo      <- REPLY to the TIMED invite`);
console.log(`   assistant: ${shown(out)}`);

const cBubble = out.length === 1 ? body(out[0]) : "";
check(
  "c1  'o dia todo' IS a change — the confirm bubble appears and says 'Dia todo'",
  /evento atualizado/i.test(cBubble) && /dia todo/i.test(cBubble)
);
check("c2  and the '(45 min)' suffix is gone", cBubble !== "" && !/min\)/i.test(cBubble));

scripted = [{ kind: "edit_review", json: editReview({ decision: "confirm" }) }];
out = await say("sim");
console.log(`   owner    : sim                                      <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
logWrite();

check(
  "c3  the day comes from the event's OWN start: start.date=2026-07-14, end.date=2026-07-15",
  writes().length === 1 &&
    wroteBody().start?.date === "2026-07-14" &&
    wroteBody().end?.date === "2026-07-15"
);
// THE ONE THAT MATTERS. A surviving `dateTime` is the corruption the old guard refused to risk.
check(
  "c4  NO dateTime survives the conversion — start.dateTime and end.dateTime are ABSENT",
  writes().length === 1 && !wroteBody().start?.dateTime && !wroteBody().end?.dateTime
);

// ============================================================================
//  d. ALL-DAY -> TIMED.  "na verdade é às 10h" on the all-day biópsia.
//     The mirror image: start.dateTime written, NO start.date left behind, and the usual
//     45-minute default (the owner stated no length).
// ============================================================================
console.log("\n=== d. ALL-DAY -> TIMED — \"na verdade é às 10h\" ===\n");
reset("5544444444444@s.whatsapp.net");

scripted = [
  ROUTE_EDIT(),
  // THE RULE: new_all_day=false is honoured ONLY because the patch also carries a
  // new_start_iso. Turning all-day off means GIVING the event a time — always.
  {
    kind: "edit",
    json: edit({ new_start_iso: "2026-07-14T10:00:00-03:00", new_all_day: false }),
  },
];
out = await say("@secretaria na verdade e as 10h", { quote: INVITE_ALLDAY });
console.log(`   owner    : @secretaria na verdade e as 10h          <- REPLY to the all-day invite`);
console.log(`   assistant: ${shown(out)}`);

const dBubble = out.length === 1 ? body(out[0]) : "";
check(
  "d1  the bubble is a TIMED one again: a clock time, the 45-min default, no 'Dia todo'",
  /10:00/.test(dBubble) && /\(45 min\)/.test(dBubble) && !/dia todo/i.test(dBubble)
);

scripted = [{ kind: "edit_review", json: editReview({ decision: "confirm" }) }];
out = await say("sim");
console.log(`   owner    : sim                                      <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
logWrite();

check(
  "d2  Google receives a TIMED event: start.dateTime present, start.date ABSENT",
  writes().length === 1 &&
    !!wroteBody().start?.dateTime &&
    !wroteBody().start?.date &&
    !!wroteBody().end?.dateTime &&
    !wroteBody().end?.date
);
check(
  "d3  and it is 45 minutes long (the default — he stated no length)",
  writes().length === 1 &&
    Math.round(
      (new Date(wroteBody().end?.dateTime) - new Date(wroteBody().start?.dateTime)) / 60000
    ) === 45
);

// ============================================================================
//  e. TRIPWIRE — AN ORDINARY TIMED MOVE STAYS TIMED.  Passes BEFORE and AFTER.
//     The all-day branch must not leak into the ordinary path. This is the edit-side twin
//     of create-selftest's a7, and if it ever goes red the fix overshot.
// ============================================================================
console.log("\n=== e. TRIPWIRE: an ordinary TIMED move stays TIMED ===\n");
reset("5555555555555@s.whatsapp.net");

scripted = [
  ROUTE_EDIT({ start_iso: "2026-07-14T10:00:00-03:00" }),
  // A model answering a TIMED move may legitimately emit new_all_day:false. It carries a
  // new_start_iso, so THE RULE honours it — and the event was timed anyway.
  {
    kind: "edit",
    json: edit({ new_start_iso: "2026-07-14T16:00:00-03:00", new_all_day: false }),
  },
];
out = await say("@secretaria move para 16h", { quote: INVITE_TIMED });
console.log(`   owner    : @secretaria move para 16h                <- REPLY to the TIMED invite`);
console.log(`   assistant: ${shown(out)}`);
check(
  "e1  the bubble is timed: 4:00 PM and the event's own 45 min",
  out.length === 1 && /4:00/.test(body(out[0])) && /\(45 min\)/.test(body(out[0]))
);

scripted = [{ kind: "edit_review", json: editReview({ decision: "confirm" }) }];
out = await say("sim");
console.log(`   owner    : sim                                      <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
logWrite();

check(
  "e2  TRIPWIRE — start.dateTime present, start.date ABSENT: a timed event is still timed",
  writes().length === 1 &&
    !!wroteBody().start?.dateTime &&
    !wroteBody().start?.date &&
    !!wroteBody().end?.dateTime &&
    !wroteBody().end?.date
);

// ============================================================================
//  f. TRIPWIRE — THE RULE.  A RENAME-ONLY PATCH CARRYING `new_all_day:false`.
//     The worst outcome on this card. EDIT_SCHEMA REQUIRES new_all_day, so a model
//     answering an ordinary rename can emit `false` rather than `null` — and a naive fold
//     would SILENTLY CONVERT the owner's all-day event into a 45-minute block. `false` is
//     honoured ONLY when the patch also carries a new_start_iso. Enforced in code, not in
//     prompt hope; this is the assertion that proves it.
//
//     f3 is the second half: the event must be written back on its ORIGINAL day. The draft
//     gets its day from the EVENT (an all-day event has no start.dateTime to seed from), so
//     a rename that forgot to carry it would write `new Date(null)` — the event lands in 1970.
// ============================================================================
console.log("\n=== f. TRIPWIRE: a RENAME carrying new_all_day:false must NOT un-all-day it ===\n");
reset("5566666666666@s.whatsapp.net");

scripted = [
  ROUTE_EDIT(),
  {
    kind: "edit",
    json: edit({
      new_title: "Biópsia (Dra. Ana)",
      new_all_day: false, // the stray `false` — a rename says NOTHING about all-day
      new_start_iso: null, // ...and it carries no time. THE RULE: `false` is IGNORED here.
    }),
  },
];
out = await say("@secretaria muda o titulo para Biopsia (Dra. Ana)", { quote: INVITE_ALLDAY });
console.log(`   owner    : @secretaria muda o titulo para Biopsia (Dra. Ana)`);
console.log(`   assistant: ${shown(out)}`);

const fBubble = out.length === 1 ? body(out[0]) : "";
check(
  "f1  the renamed event is STILL ALL-DAY in the bubble — a stray 'false' did not turn it into a 45-min block",
  /dia todo/i.test(fBubble) && !/min\)/i.test(fBubble)
);
check("f2  and the rename DID apply", /Dra\. Ana/i.test(fBubble));

scripted = [{ kind: "edit_review", json: editReview({ decision: "confirm" }) }];
out = await say("sim");
console.log(`   owner    : sim                                      <- UNTAGGED`);
console.log(`   assistant: ${shown(out)}`);
logWrite();

check(
  "f3  Google gets an ALL-DAY event on its ORIGINAL day: start.date=2026-07-14, end.date=2026-07-15, NO dateTime (and NOT 1970)",
  writes().length === 1 &&
    wroteBody().start?.date === "2026-07-14" &&
    wroteBody().end?.date === "2026-07-15" &&
    !wroteBody().start?.dateTime
);
check(
  "f4  TRIPWIRE — colorId:'5' survives the rename's update too",
  wroteBody().colorId === "5"
);
check(
  "f5  and the new title reached Google",
  wroteBody().summary === "Biópsia (Dra. Ana)"
);

// ============================================================================
//  h. HARNESS INTEGRITY (runtime). If any of these is red, NOTHING above can be trusted in
//     either direction — a mis-routed or unanswered model call makes both a pass and a fail
//     meaningless.
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
