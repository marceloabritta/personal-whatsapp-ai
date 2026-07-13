#!/usr/bin/env node
// ============================================================================
//  Self-test for the flight_search skill (secretary/2. Skills/6. Flight Search).
//
//  Card f6ea4100. Written BEFORE the code, from PLAN.md rev 2. Offline: no network,
//  no keys, no framework. `fetch` and `ctx.anthropic` are stubbed and `createSessions()`
//  is called with NO url, so it is the in-memory Map (lib/sessions.js:23-27).
//
//  The two bugs this exists to prevent — both already cost this card a revision:
//
//  A. SORT BEFORE FILTER.  Kiwi is a virtual-interlining OTA: it returns self-transfer
//     chains across unrelated carriers on separate tickets, and on a real SAO->LIS capture
//     THE FOUR CHEAPEST RESULTS WERE ALL SELF-TRANSFER CHAINS. If the code takes the 3
//     cheapest and then filters, the owner is shown junk — or nothing. The filter must run
//     FIRST, over BOTH legs, and only then the sort and the take-3. Test #4.
//
//  B. THE STALE BOOKING LINK.  Search A stashes options -> search B finds nothing -> "no
//     flights found" -> owner says "link for option 2" -> the skill sends A's booking URL.
//     Invariant S: a new search DESTROYS the old options AT FLOW START — not at result time,
//     not on success. Test #14.
//
//    1.  the real self-transfer chain (LA->TP, 1 stop) is DROPPED
//    2.  the real direct single-carrier itinerary is KEPT
//    3.  rule 1 (>1 stop) and rule 2 (carrier chain) — and BOTH legs are judged
//    4.  FILTER BEFORE SORT — via selectOptions(), the production composition   <-- the big one
//    4a. the fixture still DISCRIMINATES (guards the frozen fixture)
//    5.  the SSE frame parses: real CRLF, LF, and a multi-`data:` frame
//    6.  isError:true (HTTP 200, plain-string body) -> searchFailed, and does not throw
//    7.  a one-way itinerary (inbound === null) renders NO return leg
//    8.  the sidecar stash survives the orchestrator's session clear
//    9.  the stash's three states -> three different replies (answerLink, read side)
//    10. no reply string, en or pt, claims a search EXPIRED
//    11. edge #20: nothing judgeable -> searchFailed; judged-but-none-kept -> emptyAfterFilter
//    12. every sessions.set() in the flow passes ttl 600
//    13. en and pt ship the same 23 reply keys
//    14. INVARIANT S — the tombstone is written AT FLOW START                   <-- the other big one
//    15. dd/mm/yyyy on the wire, and the cabin enum
//
//  ⚠ THE FIXTURE RULE — READ BEFORE YOU TOUCH A FIXTURE.
//    EVERY FIXTURE HERE IS FROZEN. IT IS NEVER REGENERATED FROM A LIVE KIWI CALL.
//    Kiwi's results are VOLATILE: the identical query, run four times while this card was
//    planned, returned four DISJOINT result sets. On one of them 15 of 15 itineraries survive
//    the filter and the naive sort-then-filter top-3 EQUALS the correct filter-then-sort top-3
//    — i.e. on live data, test #4 passes on the very bug it exists to catch. The FIXTURE below
//    is hand-built from the real Kiwi shape precisely so that it DISCRIMINATES. It is not a
//    sample of today's prices; it is a discriminator, and it is correct forever.
//    If your instinct says "these numbers look stale, let me refresh them" — that instinct is
//    the bug. Test #4a exists to catch you.
//
//  Run:  node scripts/flights-selftest.mjs
// ============================================================================
import { createSessions } from "../secretary/1. Orchestrator/lib/sessions.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ---- the feature under test -------------------------------------------------
// Dynamic import so a missing feature fails LOUDLY and legibly, instead of dying in
// the module loader with a stack trace that looks like a broken test script.
const SKILL_URL = new URL("../secretary/2. Skills/6. Flight Search/skill.js", import.meta.url);
const PROMPT_URL = new URL("../secretary/2. Skills/6. Flight Search/prompt.js", import.meta.url);

let S, P;
try {
  S = await import(SKILL_URL.href);
  P = await import(PROMPT_URL.href);
} catch (e) {
  console.log("  FAIL  0. the flight_search skill exists and imports");
  console.log(`\n        ${e.code || e.name}: ${e.message.split("\n")[0]}`);
  console.log(`
        EXPECTED, for now. The Tests column ships red on purpose: this suite is
        written BEFORE the feature. It goes green when the build column creates

          secretary/2. Skills/6. Flight Search/skill.js
          secretary/2. Skills/6. Flight Search/prompt.js

        with the exports PLAN.md rev 2 pins (Interfaces §skill.js / §prompt.js).\n`);
  process.exit(1);
}

const NEED_SKILL = [
  "run", "manifest",
  "selectOptions", "filterItineraries", "applyExplicitFilters", "topCheapest",
  "parseKiwiResponse", "toKiwiDate", "KIWI_CABIN",
  "stashKey", "readStash", "writeOptions", "writeTombstone", "answerLink",
];
const NEED_PROMPT = ["reply", "renderOptions", "localizeFlightDate", "fmtDuration"];
const missing = [
  ...NEED_SKILL.filter((k) => S[k] === undefined).map((k) => `skill.js: ${k}`),
  ...NEED_PROMPT.filter((k) => P[k] === undefined).map((k) => `prompt.js: ${k}`),
];
if (missing.length) {
  console.log("  FAIL  0. the exports the tests call are all present");
  console.log(`\n        missing: ${missing.join(", ")}`);
  console.log("        (PLAN.md rev 2, Interfaces — the named-export block.)\n");
  process.exit(1);
}

const {
  run, selectOptions, filterItineraries, applyExplicitFilters, topCheapest,
  parseKiwiResponse, toKiwiDate, KIWI_CABIN,
  stashKey, readStash, writeOptions, writeTombstone, answerLink,
} = S;
const { reply, renderOptions, localizeFlightDate } = P;

// ============================================================================
//  FIXTURES — FROZEN. NEVER REGENERATE. (See THE FIXTURE RULE, above.)
// ============================================================================

// ---- FROZEN. HAND-BUILT FROM THE REAL KIWI SHAPE. NEVER REGENERATE. -----------
const seg = (from, to, carrier, n) => ({
  from, to, fromCity: from, toCity: to,
  departureTime: "2026-08-14T10:00:00", arrivalTime: "2026-08-14T14:00:00",
  durationSeconds: 14400, carrier, flightNumber: `${carrier}${n}`, cabinClass: "Economy",
});
// route ["A","B","C"] -> 2 segments, stops = 1. carriers[i] flies route[i] -> route[i+1].
const leg = (route, carriers) => ({
  from: route[0], to: route[route.length - 1],
  departureTime: "2026-08-14T10:00:00", arrivalTime: "2026-08-15T08:00:00",
  durationSeconds: 79200, stops: route.length - 2, route, cabinClass: "Economy",
  segments: carriers.map((c, i) => seg(route[i], route[i + 1], c, 100 + i)),
});
const itin = (id, price, outbound, inbound) => ({
  id, price, priceFormatted: `${price} BRL`, totalDurationSeconds: 100000,
  bookingUrl: `https://kiwi.com/u/${id}`, outbound, inbound,
});

const FIXTURE = [
  // --- the three CHEAPEST are all junk. Every one MUST be dropped. -------------
  // A: rule 2 — carrier chain (self-transfer) on BOTH legs. The real 5392 shape.
  itin("A", 3000, leg(["CGH","REC","LIS"], ["LA","TP"]), leg(["LIS","REC","CGH"], ["TP","G3"])),
  // B: rule 1 — TWO stops on the outbound. Same carrier throughout, so ONLY rule 1 catches it.
  itin("B", 3100, leg(["GRU","GIG","MAD","LIS"], ["TP","TP","TP"]), leg(["LIS","GRU"], ["TP"])),
  // C: rule 2 on the INBOUND ONLY. The outbound is a clean direct. A filter that judges
  //    only `outbound` keeps this — and then it lands in the top 3. The leg-coverage trap.
  itin("C", 3200, leg(["VCP","LIS"], ["TP"]), leg(["LIS","REC","CGH"], ["TP","G3"])),

  // --- the four survivors -------------------------------------------------------
  itin("D", 4000, leg(["VCP","LIS"], ["AD"]), leg(["LIS","VCP"], ["AD"])),          // direct both ways
  itin("E", 4200, leg(["CGH","REC","LIS"], ["TP","TP"]), leg(["LIS","CGH"], ["TP"])), // 1 stop, SAME
                                                                                     // carrier = legit
  itin("F", 4300, leg(["GRU","LIS"], ["LA"]), leg(["LIS","GRU"], ["LA"])),
  itin("G", 4900, leg(["GRU","LIS"], ["TP"]), leg(["LIS","GRU"], ["TP"])),
];

// ---- REAL CAPTURES (Kiwi, SAO->LIS 14/08->22/08/2026, BRL, 2026-07-12) ---------
// The shape anchors: they prove the filter reads the REAL key names, not invented ones.

// DROP: a 1-stop carrier chain — LATAM (LA) then TAP (TP) on one leg = self-transfer.
const REAL_DROP = {
  id: "138d0bf8", price: 5392, priceFormatted: "5392 BRL",
  totalDurationSeconds: 109500, bookingUrl: "https://kiwi.com/u/kh9cgd",
  outbound: {
    from: "CGH", to: "LIS", departureTime: "2026-08-14T14:35:00",
    arrivalTime: "2026-08-15T10:00:00", durationSeconds: 55500, stops: 1,
    route: ["CGH", "REC", "LIS"], cabinClass: "Economy",
    segments: [
      { from: "CGH", to: "REC", fromCity: "São Paulo", toCity: "Recife",
        departureTime: "2026-08-14T14:35:00", arrivalTime: "2026-08-14T17:40:00",
        durationSeconds: 11100, carrier: "LA", flightNumber: "LA3976", cabinClass: "Economy" },
      { from: "REC", to: "LIS", fromCity: "Recife", toCity: "Lisboa",
        departureTime: "2026-08-14T22:25:00", arrivalTime: "2026-08-15T10:00:00",
        durationSeconds: 27300, carrier: "TP", flightNumber: "TP12", cabinClass: "Economy" },
    ],
  },
  inbound: {
    from: "LIS", to: "CGH", departureTime: "2026-08-22T10:30:00",
    arrivalTime: "2026-08-23T02:00:00", durationSeconds: 54000, stops: 1,
    route: ["LIS", "REC", "CGH"], cabinClass: "Economy",
    segments: [
      { from: "LIS", to: "REC", fromCity: "Lisboa", toCity: "Recife",
        departureTime: "2026-08-22T10:30:00", arrivalTime: "2026-08-22T16:05:00",
        durationSeconds: 27300, carrier: "TP", flightNumber: "TP11", cabinClass: "Economy" },
      { from: "REC", to: "CGH", fromCity: "Recife", toCity: "São Paulo",
        departureTime: "2026-08-22T21:15:00", arrivalTime: "2026-08-23T02:00:00",
        durationSeconds: 11100, carrier: "G3", flightNumber: "G31417", cabinClass: "Economy" },
    ],
  },
};

// KEEP: a real direct, single-carrier survivor.
const REAL_KEEP = {
  id: "01560358", price: 5768, priceFormatted: "5768 BRL",
  totalDurationSeconds: 72000, bookingUrl: "https://kiwi.com/u/q677z7b",
  outbound: {
    from: "VCP", to: "LIS", departureTime: "2026-08-14T21:20:00",
    arrivalTime: "2026-08-15T11:00:00", durationSeconds: 34800, stops: 0,
    route: ["VCP", "LIS"], cabinClass: "Economy",
    segments: [
      { from: "VCP", to: "LIS", fromCity: "São Paulo", toCity: "Lisboa",
        departureTime: "2026-08-14T21:20:00", arrivalTime: "2026-08-15T11:00:00",
        durationSeconds: 34800, carrier: "AD", flightNumber: "AD8900", cabinClass: "Economy" },
    ],
  },
  inbound: {
    from: "LIS", to: "VCP", departureTime: "2026-08-22T12:00:00",
    arrivalTime: "2026-08-22T21:00:00", durationSeconds: 37200, stops: 0,
    route: ["LIS", "VCP"], cabinClass: "Economy",
    segments: [
      { from: "LIS", to: "VCP", fromCity: "Lisboa", toCity: "São Paulo",
        departureTime: "2026-08-22T12:00:00", arrivalTime: "2026-08-22T21:00:00",
        durationSeconds: 37200, carrier: "AD", flightNumber: "AD8901", cabinClass: "Economy" },
    ],
  },
};

// ---- the wire ---------------------------------------------------------------
const payloadOf = (itineraries) => ({
  query: "SAO → LIS on 14/08/2026, returning 22/08/2026, 1 adult",
  currency: "BRL",
  passengers: { adults: 1, children: 0, infants: 0 },
  resultsCount: itineraries.length,
  itineraries,
  searchTimeMs: 1659,
});
const envelopeOf = (payload) => ({
  jsonrpc: "2.0", id: 1,
  result: {
    content: [{ type: "text", text: JSON.stringify(payload) }],
    structuredContent: payload,
    isError: false,
  },
});
// The REAL frame. `od -c` on the live wire: the terminator is CRLF, not LF.
const sseFrame = (obj, eol = "\r\n") => `event: message${eol}data: ${JSON.stringify(obj)}${eol}${eol}`;

// Real capture: a bad argument comes back on an HTTP 200, with isError:true, NO
// structuredContent, and content[0].text as a PLAIN STRING — not JSON. A naive
// JSON.parse(content[0].text) throws.
const ISERROR_ENVELOPE = {
  jsonrpc: "2.0", id: 1,
  result: {
    content: [{
      type: "text",
      text: "Error executing tool search-flight: 1 validation error for SearchFlightsStructuredInput\ndepartureDate\n  Value error, date must be a valid calendar date in dd/mm/yyyy format [type=value_error, input_value='2026-08-14', input_type=str]",
    }],
    isError: true,
  },
};

// ---- ctx / stubs -------------------------------------------------------------
const NOW = "Sunday, 07/12/2026, 02:00 PM";

function makeCtx({ sessions, remoteJid, order = "", session = null, lang = "en", queue = [], sent = [] }) {
  const ctx = {
    owner: "Marcelo",
    tag: "@secretary",
    tags: ["@secretary"],
    anthropic: {
      messages: {
        create: async () => ({
          stop_reason: "end_turn",
          content: [{ type: "text", text: JSON.stringify(queue.shift() ?? {}) }],
        }),
      },
    },
    model: "claude-sonnet-5",
    order,
    transcript: `Marcelo: ${order}`,
    nowStr: NOW,
    contact: null,
    remoteJid,
    number: remoteJid.split("@")[0],
    fromMe: true,
    quoted: null,
    catalog: [{ id: "flight_search", description: "flights" }],
    env: { FLIGHT_CURRENCY: "BRL" },
    sessions,
    session,
    lang,
    _turn: { captured: false },
  };
  ctx.send = async (_number, text) => { sent.push({ ch: "send", text: String(text) }); };
  ctx.sendFailure = async (_number, text) => { sent.push({ ch: "sendFailure", text: String(text) }); };
  ctx.sent = sent;
  ctx.queue = queue;
  return ctx;
}

let fetchCalls = 0;
let stashAtFetch = null;      // the sidecar stash AS IT IS when Kiwi is called (test #14b)
function stubFetch(itineraries, { sessions, jid } = {}) {
  fetchCalls = 0;
  stashAtFetch = null;
  globalThis.fetch = async () => {
    fetchCalls++;
    if (sessions && jid) stashAtFetch = await sessions.get(stashKey(jid));
    return {
      ok: true,
      status: 200,
      headers: { get: (h) => (String(h).toLowerCase() === "content-type" ? "text/event-stream" : null) },
      text: async () => sseFrame(envelopeOf(payloadOf(itineraries))),
      json: async () => {
        throw new Error("Kiwi answers with an SSE frame — read res.text(), not res.json()");
      },
    };
  };
}

// Render a reply thunk defensively. REPLY values are thunks and some destructure their
// argument, so always hand them an object; never assume they take none.
const say = (lang, key, arg = {}) => {
  const fn = reply(lang)[key];
  try {
    return String(typeof fn === "function" ? fn(arg) : fn ?? "");
  } catch {
    return "unrenderable";
  }
};
const texts = (sent) => sent.map((s) => s.text);
const countOf = (hay, needle) => hay.split(needle).length - 1;
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

console.log("\nflight_search self-test  (offline — fetch and anthropic are stubbed)\n");

// ============================================================================
//  1 + 2 — the filter, against the REAL captures
// ============================================================================
console.log("1/2  the filter, on real Kiwi records");

const dropRes = filterItineraries([REAL_DROP]);
check(
  "1. the real 5392 CGH>REC>LIS chain (LA then TP, 1 stop) is DROPPED   <-- the whole point",
  dropRes.kept.length === 0 && dropRes.droppedAsJunk === 1 && dropRes.judgedCount === 1
);

const keepRes = filterItineraries([REAL_KEEP]);
check(
  "2. the real 5768 VCP>LIS direct (AD, 0 stops) is KEPT",
  keepRes.kept.length === 1 && keepRes.kept[0].id === "01560358" && keepRes.droppedAsJunk === 0
);

// ============================================================================
//  3 — the two rules, and BOTH legs
// ============================================================================
console.log("\n3    rule 1 (stops) and rule 2 (carrier chain), on BOTH legs");

const byId = (id) => FIXTURE.find((i) => i.id === id);

check(
  "3a. rule 1: 2 stops on the outbound is dropped (single carrier — only rule 1 catches it)",
  filterItineraries([byId("B")]).kept.length === 0
);
check(
  "3b. rule 2: a carrier chain on the INBOUND is dropped, though the outbound is clean  <-- both legs are judged",
  filterItineraries([byId("C")]).kept.length === 0
);
check(
  "3c. a 1-stop, SAME-carrier connection is legitimate and survives",
  filterItineraries([byId("E")]).kept.length === 1
);
check(
  "3d. over the whole FIXTURE, exactly D/E/F/G survive",
  eq(filterItineraries(FIXTURE).kept.map((i) => i.id), ["D", "E", "F", "G"])
);

// ============================================================================
//  4 — FILTER BEFORE SORT.  The highest-value test on this card.
// ============================================================================
console.log("\n4    FILTER BEFORE SORT  (selectOptions — the production composition)");

const sel = selectOptions(FIXTURE, {});
const selIds = sel.options.map((i) => i.id);
const selPrices = sel.options.map((i) => i.price);

check(
  "4. selectOptions(FIXTURE, {}) -> prices [4000, 4200, 4300]   <-- sort-then-filter returns []",
  eq(selPrices, [4000, 4200, 4300])
);
check(
  "4b. ...and ids [D, E, F]   <-- an outbound-only filter returns [C, D, E]",
  eq(selIds, ["D", "E", "F"])
);
check(
  "4c. no junk itinerary (A, B or C) reaches the owner",
  !selIds.some((id) => ["A", "B", "C"].includes(id))
);
check(
  "4d. selectOptions reports its counters: 7 judged, 4 kept, nothing emptied by an explicit filter",
  sel.judgedCount === 7 && sel.keptCount === 4 && sel.explicitEmptied === false
);

// ---- 4a: the fixture-integrity guard ----------------------------------------
const naiveIds = topCheapest(FIXTURE, 3).map((i) => i.id);
check(
  "4a. FIXTURE still discriminates — naive top-3 and correct top-3 must be disjoint (see PLAN: THE FIXTURE RULE)",
  eq(naiveIds, ["A", "B", "C"]) &&
    naiveIds.every((id) => !selIds.includes(id)) &&
    new Set(FIXTURE.map((i) => i.price)).size === FIXTURE.length
);

// ---- the explicit filter (draft.prefer) — runs AFTER the junk filter ---------
const direct = selectOptions(FIXTURE, { prefer: "direct" });
check(
  "4e. prefer:'direct' keeps only 0-stop-on-every-leg survivors: D, F, G",
  eq(direct.options.map((i) => i.id), ["D", "F", "G"])
);
const emptied = selectOptions([byId("E")], { prefer: "direct" });
check(
  "4f. an explicit filter that empties a NON-empty kept set is flagged (explicitEmptied)",
  emptied.options.length === 0 && emptied.explicitEmptied === true && emptied.keptCount === 1
);
check(
  "4g. applyExplicitFilters is a pure narrowing of what it is given",
  applyExplicitFilters([byId("D"), byId("E")], { prefer: "direct" }).length === 1
);

// ============================================================================
//  5 + 6 — the SSE wire
// ============================================================================
console.log("\n5/6  the SSE envelope  (the wire is CRLF)");

const wirePayload = payloadOf([REAL_KEEP]);
const crlf = parseKiwiResponse(sseFrame(envelopeOf(wirePayload)));
check(
  "5. the REAL CRLF frame parses -> { ok:true, payload }",
  crlf?.ok === true && crlf.payload?.resultsCount === 1 &&
    crlf.payload.itineraries[0].bookingUrl === "https://kiwi.com/u/q677z7b"
);

const lf = parseKiwiResponse(sseFrame(envelopeOf(wirePayload), "\n"));
check(
  "5a. the same frame with LF-only terminators parses IDENTICALLY (split on /\\r?\\n/)",
  lf?.ok === true && eq(lf.payload, crlf?.payload)
);

const blob = JSON.stringify(envelopeOf(wirePayload));
const half = Math.floor(blob.length / 2);
const multi = `event: message\r\ndata: ${blob.slice(0, half)}\r\ndata: ${blob.slice(half)}\r\n\r\n`;
check(
  "5b. a frame split across TWO `data:` lines is concatenated, then parsed",
  parseKiwiResponse(multi)?.ok === true && eq(parseKiwiResponse(multi).payload, crlf?.payload)
);

let threw = false;
let errRes;
try {
  errRes = parseKiwiResponse(sseFrame(ISERROR_ENVELOPE));
} catch {
  threw = true;
}
check(
  "6. isError:true (HTTP 200, plain-string body) -> { ok:false, reason:'tool' }, and does NOT throw",
  !threw && errRes?.ok === false && errRes.reason === "tool"
);
check(
  "6a. a JSON-RPC error frame -> { ok:false, reason:'rpc' }",
  parseKiwiResponse(sseFrame({ jsonrpc: "2.0", id: 1, error: { code: -32600, message: "Not Acceptable" } }))
    ?.reason === "rpc"
);
let threw6b = false;
let junkRes;
try {
  junkRes = parseKiwiResponse("<html>502 Bad Gateway</html>");
} catch {
  threw6b = true;
}
check(
  "6b. an unreadable body -> { ok:false, reason:'unreadable' }, and does NOT throw",
  !threw6b && junkRes?.ok === false && junkRes.reason === "unreadable"
);

// ============================================================================
//  7 — the one-way render.  `inbound` is PRESENT and NULL, not absent.
// ============================================================================
console.log("\n7    one-way: inbound === null renders no return leg");

const legAt = (route, carriers, dep, arr) => ({
  from: route[0], to: route[route.length - 1],
  departureTime: dep, arrivalTime: arr,
  durationSeconds: 41400, stops: route.length - 2, route, cabinClass: "Economy",
  segments: carriers.map((c, i) => ({
    from: route[i], to: route[i + 1], fromCity: route[i], toCity: route[i + 1],
    departureTime: dep, arrivalTime: arr, durationSeconds: 41400,
    carrier: c, flightNumber: `${c}90${i}`, cabinClass: "Economy",
  })),
});
const OUT = legAt(["GRU", "LIS"], ["TP"], "2026-08-14T22:10:00", "2026-08-15T12:35:00");
const BACK = legAt(["LIS", "GRU"], ["TP"], "2026-08-22T11:40:00", "2026-08-22T19:05:00");
const ONE_WAY = { ...itin("OW", 4000, OUT, null) };
const ROUND = { ...itin("RT", 4000, OUT, BACK) };

for (const lang of ["en", "pt"]) {
  const ow = renderOptions(lang, [ONE_WAY]);
  const rt = renderOptions(lang, [ROUND]);
  check(
    `7. [${lang}] a one-way renders exactly ONE leg (the round-trip renders two)`,
    countOf(ow, "GRU") === 1 && countOf(rt, "GRU") >= 2
  );
  check(
    `7a. [${lang}] the one-way render has no empty/blank return field (no null/undefined/NaN)`,
    !/null|undefined|NaN/i.test(ow)
  );
  const backDate = localizeFlightDate(lang, "2026-08-22");
  check(
    `7b. [${lang}] the return date appears in the round-trip render and NOT in the one-way`,
    !!backDate && rt.includes(backDate) && !ow.includes(backDate)
  );
}

// ============================================================================
//  8 — Mechanism A: the sidecar key survives sessions.clear(remoteJid)
// ============================================================================
console.log("\n8    the sidecar stash survives the orchestrator's session clear");

{
  const sessions = createSessions(); // no url -> in-memory Map. Genuinely offline.
  const jid = "5511900000008@s.whatsapp.net";
  await sessions.set(jid, { skill: "flight_search", stage: "await_link" }, 600);
  await sessions.set(stashKey(jid), { options: [REAL_KEEP], lang: "en" }, 600);

  await sessions.clear(jid); // exactly what server.js:402 does on a fresh tagged order

  const gone = await sessions.get(jid);
  const stash = await sessions.get(stashKey(jid));
  check(
    "8. clear(remoteJid) drops the session and leaves the `|flights` sidecar INTACT",
    gone === null && !!stash && stash.options?.[0]?.bookingUrl === REAL_KEEP.bookingUrl
  );
  check("8a. stashKey namespaces on the jid", stashKey(jid) === `${jid}|flights`);
}

// ============================================================================
//  9 — the stash's three states -> three replies (answerLink, the READ side)
// ============================================================================
console.log("\n9    answerLink: the stash's three states");

const OPT = { ...REAL_KEEP, bookingUrl: "https://kiwi.com/u/LIVE1" };

// distinct replies, or the three states are indistinguishable to the owner
check(
  "9. the three state replies are three DIFFERENT strings",
  say("en", "resultsDiscarded") !== say("en", "noResultsToLink") &&
    say("en", "resultsDiscarded") !== "" && say("en", "noResultsToLink") !== ""
);

// 9a — options live -> the link, then session AND stash are cleared
{
  const sessions = createSessions();
  const jid = "5511900000091@s.whatsapp.net";
  const ctx = makeCtx({ sessions, remoteJid: jid });
  await writeOptions(ctx, [OPT]);
  await answerLink(ctx, 1);
  check(
    "9a. options stashed -> the booking link goes out, on ctx.send",
    ctx.sent.some((m) => m.ch === "send" && m.text.includes("https://kiwi.com/u/LIVE1"))
  );
  check(
    "9a2. ...and the session and the stash are cleared afterwards",
    (await sessions.get(jid)) === null && (await readStash(ctx)) === null
  );
}

// 9b — tombstone -> "I dropped those"
{
  const sessions = createSessions();
  const jid = "5511900000092@s.whatsapp.net";
  const ctx = makeCtx({ sessions, remoteJid: jid });
  await writeTombstone(ctx);
  const stash = await readStash(ctx);
  check("9b. writeTombstone -> readStash().discarded === true", stash?.discarded === true);
  await answerLink(ctx, 1);
  check(
    "9b2. a tombstoned stash -> resultsDiscarded (never a stale link)",
    ctx.sent.length === 1 && ctx.sent[0].text.trim() === say("en", "resultsDiscarded").trim() &&
      !ctx.sent[0].text.includes("kiwi.com/u/")
  );
}

// 9c — nothing stashed at all
{
  const sessions = createSessions();
  const jid = "5511900000093@s.whatsapp.net";
  const ctx = makeCtx({ sessions, remoteJid: jid });
  check("9c. an absent stash reads as null", (await readStash(ctx)) === null);
  await answerLink(ctx, 1);
  check(
    "9c2. no stash -> noResultsToLink",
    ctx.sent.length === 1 && ctx.sent[0].text.trim() === say("en", "noResultsToLink").trim()
  );
}

// 9d — an option with no bookingUrl: a real failure, and it must be DECLARED
{
  const sessions = createSessions();
  const jid = "5511900000094@s.whatsapp.net";
  const ctx = makeCtx({ sessions, remoteJid: jid });
  const noUrl = { ...REAL_KEEP, bookingUrl: null };
  await writeOptions(ctx, [noUrl]);
  await answerLink(ctx, 1);
  check(
    "9d. an option with no bookingUrl -> linkMissing, on ctx.sendFailure (a declared failure)",
    ctx.sent.length >= 1 && ctx.sent.every((m) => m.ch === "sendFailure")
  );
  check(
    "9d2. ...and the stash is NOT destroyed (the owner can still ask for another option)",
    !!(await readStash(ctx))?.options
  );
}

// 9e — "book it": the link goes out AND the refusal is said, both on ctx.send
{
  const sessions = createSessions();
  const jid = "5511900000095@s.whatsapp.net";
  const ctx = makeCtx({ sessions, remoteJid: jid });
  await writeOptions(ctx, [OPT]);
  await answerLink(ctx, 1, { book: true });
  const all = texts(ctx.sent).join("\n");
  check(
    "9e. book:true -> the link AND cannotBook",
    all.includes("https://kiwi.com/u/LIVE1") && all.includes(say("en", "cannotBook").trim())
  );
  check(
    "9e2. ...and cannotBook is sent with ctx.send — booking is a product boundary, not a malfunction",
    ctx.sent.every((m) => m.ch === "send")
  );
}

// 9f/9g — no option number / out of range: ask again, and RE-ARM (Invariant T)
{
  const sessions = createSessions();
  const jid = "5511900000096@s.whatsapp.net";
  const ctx = makeCtx({ sessions, remoteJid: jid });
  await writeOptions(ctx, [OPT]);
  await answerLink(ctx, null);
  const s = await sessions.get(jid);
  check(
    "9f. no option number -> whichOption, no link, and the stash survives",
    ctx.sent.length === 1 && !ctx.sent[0].text.includes("kiwi.com/u/") &&
      !!(await readStash(ctx))?.options
  );
  check(
    "9f2. ...and the await_link session is RE-ARMED, so a bare '2' next turn still works (Invariant T)",
    s?.skill === "flight_search" && s?.stage === "await_link"
  );
}
{
  const sessions = createSessions();
  const jid = "5511900000097@s.whatsapp.net";
  const ctx = makeCtx({ sessions, remoteJid: jid });
  await writeOptions(ctx, [OPT]);
  await answerLink(ctx, 9);
  check(
    "9g. an out-of-range option -> optionOutOfRange, no link, stash and session survive",
    ctx.sent.length === 1 && !ctx.sent[0].text.includes("kiwi.com/u/") &&
      ctx.sent[0].text.trim() !== say("en", "noResultsToLink").trim() &&
      !!(await readStash(ctx))?.options &&
      (await sessions.get(jid))?.stage === "await_link"
  );
}

// ============================================================================
//  10 — no reply may claim a search EXPIRED.  (The stash is the only truth.)
// ============================================================================
console.log("\n10   no reply claims an expiry");

// REPLY values are thunks and some destructure their argument, so CALLING them can throw.
// Scan the source text instead: String(fn) contains the literal.
{
  let bad = [];
  for (const lang of ["en", "pt"]) {
    for (const [key, val] of Object.entries(reply(lang))) {
      if (/expir/i.test(String(val))) bad.push(`${lang}.${key}`);
    }
  }
  check(`10. no reply string matches /expir/i  ${bad.length ? `(${bad.join(", ")})` : ""}`, bad.length === 0);
}

// ============================================================================
//  11 — edge #20: unreadable results are NOT "no flights"
// ============================================================================
console.log("\n11   edge #20: nothing judgeable != nothing found");

const blind = (it) => ({ ...it, outbound: { ...it.outbound, segments: [] }, inbound: null });
const noCarrier = (it) => ({
  ...it,
  outbound: { ...it.outbound, segments: [{ ...it.outbound.segments[0], carrier: null }] },
  inbound: null,
});

const allBlind = selectOptions([blind(byId("D")), noCarrier(byId("F"))], {});
check(
  "11. every itinerary unjudgeable -> judgedCount === 0 -> searchFailed (NOT emptyResults)",
  allBlind.judgedCount === 0 && allBlind.keptCount === 0 && allBlind.options.length === 0
);

const mixed = selectOptions([byId("A"), blind(byId("D"))], {});
check(
  "11a. judged-and-rejected + unjudgeable, none surviving -> judgedCount > 0, keptCount === 0 -> emptyAfterFilter",
  mixed.judgedCount === 1 && mixed.keptCount === 0
);

// ============================================================================
//  12 — every sessions.set() in the flow passes ttl 600
// ============================================================================
console.log("\n12   TTL is 600 on every write");

{
  const real = createSessions();
  const recorded = [];
  const sessions = {
    get: (jid) => real.get(jid),
    set: (jid, value, ttl) => {
      recorded.push({ jid, ttl });
      return real.set(jid, value, ttl);
    },
    clear: (jid) => real.clear(jid),
  };

  // (a) an INCOMPLETE search -> the slot chase (openInquiry)
  const jidA = "5511900000121@s.whatsapp.net";
  stubFetch([]);
  const ctxA = makeCtx({
    sessions, remoteJid: jidA, order: "find me a flight to Lisbon",
    queue: [{
      intent: "search", option_number: null, origin: null, destination: "LIS",
      depart_date: null, return_date: null, adults: null, children: null, infants: null,
      cabin: null, prefer: null, clarify_field: null, clarify_kind: null, clarify_options: [],
      summary: "flight to Lisbon, origin and date missing",
    }],
  });
  await run(ctxA);

  // (b) a COMPLETE search -> the confirmation (openConfirm), no Kiwi call
  const jidB = "5511900000122@s.whatsapp.net";
  const ctxB = makeCtx({
    sessions, remoteJid: jidB, order: "flight GRU to LIS on the 20th of August",
    queue: [{
      intent: "search", option_number: null, origin: "GRU", destination: "LIS",
      depart_date: "2026-08-20", return_date: null, adults: 1, children: 0, infants: 0,
      cabin: null, prefer: null, clarify_field: null, clarify_kind: null, clarify_options: [],
      summary: "GRU to LIS on 2026-08-20, one way",
    }],
  });
  await run(ctxB);

  // (c) the confirm turn -> runSearch -> writeOptions + await_link.
  //     The session is read back from the store, so the test makes NO assumption about
  //     the session's shape — it hands the skill exactly what the orchestrator would.
  stubFetch(FIXTURE);
  const ctxC = makeCtx({
    sessions, remoteJid: jidB, order: "yes",
    session: await sessions.get(jidB),
    queue: [{
      decision: "confirm", origin: "GRU", destination: "LIS", depart_date: "2026-08-20",
      return_date: null, adults: 1, children: 0, infants: 0, cabin: null, prefer: null,
    }],
  });
  await run(ctxC);

  check(
    "12. every sessions.set() in the flow passes ttl 600 (default is 900 — server.js:70 sets no ttlSec)",
    recorded.length > 0 && recorded.every((r) => r.ttl === 600)
  );
  check(
    `12a. the flow actually WROTE sessions (>= 4 writes; saw ${recorded.length}) — the ttl check cannot pass vacuously`,
    recorded.length >= 4
  );
  check(
    "12b. the confirm turn reached Kiwi exactly once and stashed the options",
    fetchCalls === 1 && (await sessions.get(stashKey(jidB)))?.options?.length === 3
  );
  check(
    "12c. ...and armed the await_link session",
    (await sessions.get(jidB))?.stage === "await_link"
  );
}

// ============================================================================
//  13 — localization: 23 keys, en and pt
// ============================================================================
console.log("\n13   localization");

const SEND_KEYS = [
  "askOrigin", "askDestination", "askDate", "cityAmbiguous", "cityUnknown", "badDate",
  "returnBeforeDepart", "declined", "results", "thinnedResults", "emptyResults",
  "emptyAfterFilter", "explicitFilterEmpty", "linkSent", "whichOption", "optionOutOfRange",
  "resultsDiscarded", "noResultsToLink", "cannotBook",
];
const FAILURE_KEYS = ["searchFailed", "thinkingError", "notAFlight", "linkMissing"];
const ALL_KEYS = [...SEND_KEYS, ...FAILURE_KEYS];

const en = Object.keys(reply("en")).sort();
const pt = Object.keys(reply("pt")).sort();
check("13. en and pt ship the same key set", eq(en, pt));
check(`13a. 23 reply keys in each language (en: ${en.length}, pt: ${pt.length})`, en.length === 23 && pt.length === 23);
check(
  `13b. every key the flow sends exists in BOTH languages`,
  ALL_KEYS.every((k) => reply("en")[k] !== undefined && reply("pt")[k] !== undefined)
);
check(
  "13c. an unknown language falls back to en",
  eq(Object.keys(reply("zz")).sort(), en)
);

// ============================================================================
//  14 — INVARIANT S, THE WRITE SIDE.  The stale-link bug, reproduced.
// ============================================================================
console.log("\n14   INVARIANT S: a new search destroys the old options AT FLOW START");

// ---- 14a: the tombstone is written on a turn that NEVER reaches Kiwi ---------
{
  const sessions = createSessions();
  const jid = "5511900000141@s.whatsapp.net";
  stubFetch(FIXTURE);

  const seedCtx = makeCtx({ sessions, remoteJid: jid });
  await writeOptions(seedCtx, [{ ...REAL_KEEP, bookingUrl: "https://kiwi.com/u/OLD" }]);

  // A COMPLETE draft: the flow stops at the confirmation and runSearch never runs.
  const ctx = makeCtx({
    sessions, remoteJid: jid, order: "flight GRU to LIS on the 20th of August",
    queue: [{
      intent: "search", option_number: null, origin: "GRU", destination: "LIS",
      depart_date: "2026-08-20", return_date: null, adults: 1, children: 0, infants: 0,
      cabin: null, prefer: null, clarify_field: null, clarify_kind: null, clarify_options: [],
      summary: "GRU to LIS on 2026-08-20, one way",
    }],
  });
  await run(ctx);

  check(
    "14a. the flow really did stop at the confirmation (Kiwi was never called)",
    fetchCalls === 0
  );
  const stash = await readStash(ctx);
  const raw = JSON.stringify(await sessions.get(stashKey(jid)));
  check(
    "14a2. the old options are ALREADY a tombstone   <-- a build that tombstones inside runSearch fails HERE",
    stash?.discarded === true && stash.options === undefined && !raw.includes("OLD")
  );
}

// ---- 14b: at Kiwi-call time the tombstone is already there, and the old link is gone
{
  const sessions = createSessions();
  const jid = "5511900000142@s.whatsapp.net";
  const sent = [];

  // Search A's options. TWO of them, so that "link for option 2" on a broken build
  // resolves to a REAL stale url and actually goes out — the leak, not an out-of-range.
  const seedCtx = makeCtx({ sessions, remoteJid: jid, sent });
  await writeOptions(seedCtx, [
    { ...REAL_KEEP, id: "old1", bookingUrl: "https://kiwi.com/u/OLD1" },
    { ...REAL_KEEP, id: "old2", bookingUrl: "https://kiwi.com/u/OLD2" },
  ]);

  // Kiwi finds NOTHING this time — the exact shape of the original bug.
  stubFetch([], { sessions, jid });

  const t1 = makeCtx({
    sessions, remoteJid: jid, order: "flight GRU to LIS on the 20th of August", sent,
    queue: [{
      intent: "search", option_number: null, origin: "GRU", destination: "LIS",
      depart_date: "2026-08-20", return_date: null, adults: 1, children: 0, infants: 0,
      cabin: null, prefer: null, clarify_field: null, clarify_kind: null, clarify_options: [],
      summary: "GRU to LIS on 2026-08-20, one way",
    }],
  });
  await run(t1);

  const t2 = makeCtx({
    sessions, remoteJid: jid, order: "yes", sent,
    session: await sessions.get(jid),
    queue: [{
      decision: "confirm", origin: "GRU", destination: "LIS", depart_date: "2026-08-20",
      return_date: null, adults: 1, children: 0, infants: 0, cabin: null, prefer: null,
    }],
  });
  await run(t2);

  check(
    "14b. Kiwi was called, and the stash was ALREADY a tombstone at call time (the write happened FIRST)",
    fetchCalls === 1 && stashAtFetch?.discarded === true && stashAtFetch?.options === undefined
  );
  check(
    "14b2. the empty search told the owner so",
    texts(sent).some((t) => t.trim() === say("en", "emptyResults").trim())
  );

  // "link for option 2" — the turn that used to hand out search A's URL.
  const t3 = makeCtx({ sessions, remoteJid: jid, order: "link for option 2", sent });
  await answerLink(t3, 2);

  check(
    "14b3. 'link for option 2' -> resultsDiscarded, not a link",
    texts(sent).some((t) => t.trim() === say("en", "resultsDiscarded").trim())
  );
  check(
    "14b4. NOTHING sent in the whole flow carries the stale bookingUrl   <-- THE BUG",
    !texts(sent).join("\n").includes("OLD")
  );
}

// ---- 14c: the mirror image — a LINK turn must NOT tombstone -------------------
// A build that tombstones unconditionally at the top of run() passes 14a and 14b, and
// destroys the options of every owner who asks for a link.
{
  const sessions = createSessions();
  const jid = "5511900000143@s.whatsapp.net";
  stubFetch(FIXTURE);

  const ctx = makeCtx({
    sessions, remoteJid: jid, order: "link for option 1",
    queue: [{
      intent: "link", option_number: 1, origin: null, destination: null,
      depart_date: null, return_date: null, adults: null, children: null, infants: null,
      cabin: null, prefer: null, clarify_field: null, clarify_kind: null, clarify_options: [],
      summary: "booking link for option 1",
    }],
  });
  await writeOptions(ctx, [{ ...REAL_KEEP, bookingUrl: "https://kiwi.com/u/LIVE2" }]);
  await run(ctx);

  check(
    "14c. a LINK turn does not tombstone: the owner gets the link, not resultsDiscarded",
    texts(ctx.sent).join("\n").includes("https://kiwi.com/u/LIVE2") && fetchCalls === 0
  );
}

// ---- 14d: 'other' -> notAFlight, declared as a failure ------------------------
{
  const sessions = createSessions();
  const jid = "5511900000144@s.whatsapp.net";
  stubFetch(FIXTURE);
  const ctx = makeCtx({
    sessions, remoteJid: jid, order: "what's the weather in Lisbon",
    queue: [{
      intent: "other", option_number: null, origin: null, destination: null,
      depart_date: null, return_date: null, adults: null, children: null, infants: null,
      cabin: null, prefer: null, clarify_field: null, clarify_kind: null, clarify_options: [],
      summary: "not a flight request",
    }],
  });
  await run(ctx);
  check(
    "14d. intent 'other' -> notAFlight, on ctx.sendFailure, and Kiwi is never called",
    fetchCalls === 0 && ctx.sent.length >= 1 && ctx.sent.every((m) => m.ch === "sendFailure")
  );
}

// ============================================================================
//  15 — the wire's date format and cabin enum
// ============================================================================
console.log("\n15   dd/mm/yyyy and the cabin enum");

check("15. toKiwiDate('2026-08-14') === '14/08/2026'  (an ISO date -> isError:true)", toKiwiDate("2026-08-14") === "14/08/2026");
check("15a. toKiwiDate(null) === null  (a one-way sends no returnDate)", toKiwiDate(null) === null);
check(
  "15b. KIWI_CABIN maps the enum: economy->M, business->C",
  KIWI_CABIN.economy === "M" && KIWI_CABIN.business === "C" &&
    KIWI_CABIN.premium_economy === "W" && KIWI_CABIN.first === "F"
);
check("15c. the manifest id is flight_search", S.manifest?.id === "flight_search");

// ---- done --------------------------------------------------------------------
console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
