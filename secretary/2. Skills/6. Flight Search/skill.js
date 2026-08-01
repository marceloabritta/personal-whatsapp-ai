// ============================================================================
//  Skill "Flight Search" — LOGIC.
//  Search flights from a sentence, confirm BEFORE searching, show the 3 cheapest
//  itineraries a human would actually pick, and hand over the booking link for one
//  of them on a follow-up turn. It never buys anything.
//
//  Run by the orchestrator when the router picks "flight_search".
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
//  Localized replies live in prompt.js (reply(ctx.lang)); ctx.send is pre-bound to the
//  conversation language. Provider: Kiwi's public MCP endpoint (https://mcp.kiwi.com) —
//  keyless, no handshake, a plain `fetch`. Only FLIGHT_CURRENCY is configurable.
//
//  TWO THINGS IN HERE WILL LOOK LIKE BUGS AND ARE NOT. Read SKILL.md before "fixing":
//
//  1. THE RESULT FILTER, AND IT RUNS BEFORE THE SORT. Kiwi is a virtual-interlining OTA
//     and has NO max-stops and NO self-transfer parameter, so the filter cannot live at
//     the API — it lives here, and it must run BEFORE the 3-cheapest selection. On a real
//     SAO->LIS capture the FOUR CHEAPEST results were all self-transfer carrier chains:
//     sort-then-filter would have put exactly that junk in front of the owner. selectOptions()
//     owns the filter -> sort -> take-3 order; do not re-implement it inline.
//
//  2. INVARIANT S — a new search DESTROYS the old options AT FLOW START. writeTombstone()
//     is called in run(), the moment interpret() says intent "search": before the slot
//     chase, before the confirmation, before Kiwi is ever called. Not inside runSearch().
//     Otherwise: search A stashes options -> search B finds nothing -> "no flights" ->
//     "link for option 2" -> the skill sends A's stale booking URL. That bug is why the
//     write happens where it happens.
// ============================================================================
import {
  FLIGHT_SCHEMA,
  FLIGHT_REVIEW_SCHEMA,
  LINK_REVIEW_SCHEMA,
  buildFlightSystem,
  buildFlightUser,
  buildReviewSystem,
  buildReviewUser,
  buildLinkReviewSystem,
  buildLinkReviewUser,
  renderConfirm,
  renderOptions,
  reply,
} from "./prompt.js";
import { jsonFormat, readReply } from "../../1. Orchestrator/lib/llm.js";

// `inputs` — the DECLARED input contract (see 1. Orchestrator/lib/inputs.js). The router's
// merged call fills it in the same round-trip that classifies the order.
// ⚠ THIS SKILL DECLARES BUT DOES NOT YET CONSUME `ctx.info` — it still makes its own extraction
// call. Deliberate: only its ROUTING was measured under the merged prompt, never its payload
// accuracy, and adopting ctx.info here needs its own accuracy check.
export const manifest = {
  id: "flight_search",
  // This skill runs its own dialogue (default; explicit so the migration state is a grep).
  conversation: "skill",
  description:
    "search for flights and show the cheapest sensible options (origin, destination, dates, passengers, cabin), and send the booking link for a flight option ALREADY shown in this conversation (e.g. 'link for option 2'); it only SEARCHES and never buys — it is NOT for adding a flight-related to-do or reminder to the task list",
  inputs: {
    discriminator: "intent",
    fields: {
      intent: { type: "enum", enum: ["search", "link", "book", "other"] },
      origin: { type: "string", nullable: true, desc: "IATA code or city" },
      destination: { type: "string", nullable: true },
      depart_date: { type: "iso", nullable: true },
      return_date: { type: "iso", nullable: true },
      adults: { type: "number", nullable: true },
      cabin: {
        type: "enum",
        enum: ["economy", "premium_economy", "business", "first"],
        nullable: true,
      },
      summary: { type: "string" },
    },
    requiredWhen: {
      search: ["origin", "destination", "depart_date"],
      link: [],
      book: [],
      other: [],
    },
    consistency: [
      {
        name: "return_after_depart",
        test: (i) =>
          !(i.depart_date && i.return_date) ||
          Date.parse(i.return_date) >= Date.parse(i.depart_date),
      },
      {
        name: "origin_is_not_destination",
        test: (i) =>
          !(i.origin && i.destination) ||
          String(i.origin).toLowerCase() !== String(i.destination).toLowerCase(),
      },
    ],
    rulebook: () => "",
  },
};

// ---- Kiwi -------------------------------------------------------------------
// The wire contract, probed live (see PROJECT_LOG §8):
//   - keyless: no API key, no `initialize` handshake, no Mcp-Session-Id header.
//   - Accept MUST list BOTH application/json and text/event-stream (json alone -> HTTP 406).
//   - the response is ALWAYS an SSE frame, and the frame is CRLF.
//   - a bad argument comes back on an HTTP 200 with `isError: true` and a PLAIN, non-JSON
//     body — so isError is checked BEFORE anything is parsed out of content[0].text.
const KIWI_URL = "https://mcp.kiwi.com";
const KIWI_TOOL = "search-flight";
const TIMEOUT_MS = 20_000;
const TTL = 600; // session/stash TTL, seconds (the store's default is 900)

// Kiwi's cabin enum. NOT "economy"/"business" — M | W | C | F.
const KIWI_CABIN = {
  economy: "M",
  premium_economy: "W",
  business: "C",
  first: "F",
};

// The wire wants dd/mm/yyyy. An ISO date returns isError:true and every search fails.
function toKiwiDate(iso) {
  const m = String(iso ?? "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : null;
}

function buildKiwiArgs(draft, env) {
  const args = {
    flyFrom: draft.origin,
    flyTo: draft.destination,
    departureDate: toKiwiDate(draft.depart_date),
    adults: Number(draft.adults) || 1,
    children: Number(draft.children) || 0,
    infants: Number(draft.infants) || 0,
    cabinClass: KIWI_CABIN[draft.cabin] || KIWI_CABIN.economy,
    currency: draft.currency || env?.FLIGHT_CURRENCY || "BRL",
    // `locale` drives KIWI's booking page, not our reply — it is deliberately fixed
    // and NOT tied to ctx.lang. ctx.lang controls only what WE say.
    locale: "pt",
  };
  const back = toKiwiDate(draft.return_date);
  if (back) args.returnDate = back;
  return args;
}

// SSE text -> { ok: true, payload } | { ok: false, reason }
//   reason: "http" | "rpc" | "tool" | "unreadable"
// Split on /\r?\n/ (the live wire is CRLF; be robust to LF), take EVERY `data: ` line
// and concatenate them, then parse once. Never throws.
function parseKiwiResponse(rawText) {
  const text = String(rawText ?? "");
  const data = text
    .split(/\r?\n/)
    .filter((l) => l.startsWith("data: "))
    .map((l) => l.slice(6))
    .join("");
  let envelope;
  try {
    envelope = JSON.parse(data || text.trim());
  } catch {
    return { ok: false, reason: "unreadable" };
  }
  if (!envelope || typeof envelope !== "object") return { ok: false, reason: "unreadable" };
  if (envelope.error) return { ok: false, reason: "rpc" };

  const result = envelope.result;
  if (!result || typeof result !== "object") return { ok: false, reason: "unreadable" };
  // THE TRAP: an HTTP 200 whose body says isError. content[0].text is then a plain
  // error string, not JSON — a naive JSON.parse of it throws.
  if (result.isError === true) return { ok: false, reason: "tool" };

  let payload = result.structuredContent;
  if (!payload) {
    try {
      payload = JSON.parse(result.content?.[0]?.text ?? "");
    } catch {
      return { ok: false, reason: "unreadable" };
    }
  }
  if (!payload || !Array.isArray(payload.itineraries)) return { ok: false, reason: "unreadable" };
  return { ok: true, payload };
}

async function searchKiwi(ctx, draft) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(KIWI_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        // BOTH, always. application/json alone -> HTTP 406.
        accept: "application/json, text/event-stream",
      },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: { name: KIWI_TOOL, arguments: buildKiwiArgs(draft, ctx.env) },
      }),
      signal: controller.signal,
    });
    if (!res.ok) {
      console.error("Kiwi HTTP error:", res.status);
      return { ok: false, reason: "http" };
    }
    return parseKiwiResponse(await res.text());
  } catch (e) {
    console.error("Kiwi request error:", e?.message || e);
    return { ok: false, reason: "http" };
  } finally {
    clearTimeout(timer);
  }
}

// ---- The result filter ------------------------------------------------------
// BOTH legs are judged. A clean outbound with a carrier-chained inbound is still a
// self-transfer itinerary, and an outbound-only filter ships it.
const legsOf = (it) => [it?.outbound, it?.inbound].filter((l) => l != null);

// Can we even judge this itinerary? We need the stop count and a carrier per segment.
// If we cannot, we do NOT show it — but "I couldn't read the answer" is a different
// fact from "there were no good flights", and runSearch keeps them apart (edge #20).
function judgeable(it) {
  const legs = legsOf(it);
  if (!legs.length) return false;
  return legs.every(
    (l) =>
      Number.isFinite(Number(l?.stops)) &&
      Array.isArray(l?.segments) &&
      l.segments.length > 0 &&
      l.segments.every((s) => !!s?.carrier)
  );
}

// Rule 1: more than 1 stop on a leg. Rule 2: a leg whose segments are not all the same
// carrier — the self-transfer / virtual-interlining fallback. Kiwi exposes NO marker for
// it (the whole key union was checked), so this is the only discriminator available. It
// is deliberately over-strict: it also drops a legitimate single-ticket alliance
// connection. When in doubt, drop — a self-transfer chain is only discovered at the
// airport, and that is the failure this skill cannot afford.
function legIsJunk(leg) {
  if (Number(leg.stops) > 1) return true;
  return new Set(leg.segments.map((s) => s.carrier)).size > 1;
}

function filterItineraries(itineraries) {
  const kept = [];
  let judgedCount = 0;
  let droppedAsJunk = 0;
  let droppedForData = 0;
  for (const it of itineraries || []) {
    if (!judgeable(it)) {
      droppedForData++;
      continue;
    }
    judgedCount++;
    if (legsOf(it).some(legIsJunk)) {
      droppedAsJunk++;
      continue;
    }
    kept.push(it);
  }
  return { kept, judgedCount, droppedAsJunk, droppedForData };
}

// The owner's OWN hard filters, applied ON TOP of the mandatory one — never instead of
// it. He can raise the floor; he cannot lower it.
function applyExplicitFilters(kept, draft) {
  const list = kept || [];
  if (draft?.prefer === "direct")
    return list.filter((it) => legsOf(it).every((l) => Number(l.stops) === 0));
  if (draft?.prefer === "overnight")
    return list.filter((it) => {
      const m = String(it?.outbound?.departureTime ?? "").match(/T(\d{2}):/);
      if (!m) return false;
      const hour = Number(m[1]);
      return hour >= 20 || hour <= 5;
    });
  return list;
}

// Sort by price ASC and take n. NEVER call this before the filter.
function topCheapest(list, n = 3) {
  return [...(list || [])].sort((a, b) => Number(a.price) - Number(b.price)).slice(0, n);
}

// THE ONE PLACE THE ORDER LIVES: filter (both legs) -> explicit filters -> sort -> take 3.
// runSearch calls this and does not re-implement it. The counters are what let runSearch
// tell "no flights" from "no GOOD flights" from "I couldn't read the answer".
function selectOptions(itineraries, draft) {
  const { kept, judgedCount } = filterItineraries(itineraries);
  const narrowed = applyExplicitFilters(kept, draft || {});
  const options = topCheapest(narrowed, 3);
  return {
    options,
    judgedCount,
    keptCount: kept.length,
    explicitEmptied: kept.length > 0 && narrowed.length === 0,
  };
}

// ---- The sidecar stash ------------------------------------------------------
// The options live under their OWN session key, `${remoteJid}|flights`, because a TAGGED
// follow-up ("@secretary link for option 2") makes the orchestrator clear the chat's
// session (server.js:402) BEFORE the router has decided which skill the order belongs to.
// The bare-jid session dies there; the sidecar does not (sessions.clear is an exact-key
// delete, no wildcard). Three states, and they are three different facts:
//   { options } -> the options are live      -> send the link
//   { discarded: true } -> a new search killed them (Invariant S) -> say so
//   null -> nothing here                     -> "I have no options on hand"
// No state means "expired" — the skill cannot know that, and never claims it.
function stashKey(remoteJid) {
  return `${remoteJid}|flights`;
}
async function readStash(ctx) {
  return (await ctx.sessions.get(stashKey(ctx.remoteJid))) || null;
}
async function writeOptions(ctx, options) {
  await ctx.sessions.set(stashKey(ctx.remoteJid), { options, lang: ctx.lang }, TTL);
}
async function writeTombstone(ctx) {
  await ctx.sessions.set(stashKey(ctx.remoteJid), { discarded: true }, TTL);
}
async function clearStash(ctx) {
  await ctx.sessions.clear(stashKey(ctx.remoteJid));
}

// ---- Sessions ---------------------------------------------------------------
async function armInfo(ctx, draft) {
  await ctx.sessions.set(
    ctx.remoteJid,
    {
      skill: "flight_search",
      intent: "search",
      stage: "await_info",
      awaitFrom: "owner", // only the owner searches for his own flights
      lang: ctx.lang,
      data: { draft },
    },
    TTL
  );
}
async function armConfirm(ctx, draft) {
  await ctx.sessions.set(
    ctx.remoteJid,
    {
      skill: "flight_search",
      intent: "search",
      stage: "await_confirmation",
      awaitFrom: "owner",
      lang: ctx.lang,
      data: { draft },
    },
    TTL
  );
}
// Invariant T: whenever we ask WHICH option, the session AND the stash are re-armed
// together, so a bare "2" on the next turn still resolves. `book` rides along, so a
// "book it" that arrived without a number does not lose its refusal on the next turn.
async function armLink(ctx, { book = false } = {}) {
  await ctx.sessions.set(
    ctx.remoteJid,
    {
      skill: "flight_search",
      intent: "search",
      stage: "await_link",
      awaitFrom: "owner",
      lang: ctx.lang,
      data: { book: !!book },
    },
    TTL
  );
}
// The flow is over: the chat's session goes. The stash does NOT — it holds the tombstone
// (Invariant S), which is what makes "I dropped those options" true instead of a guess.
async function endFlow(ctx) {
  await ctx.sessions.clear(ctx.remoteJid);
}

// ---- The three LLM passes ---------------------------------------------------
async function interpret(ctx) {
  const msg = await ctx.anthropic.messages.create({
    model: ctx.model,
    max_tokens: 1000,
    system: buildFlightSystem(ctx.owner),
    output_config: jsonFormat(FLIGHT_SCHEMA),
    messages: [{ role: "user", content: buildFlightUser(ctx) }],
  });
  const info = readReply(msg, "flights");
  console.log("FLIGHT RAW:", JSON.stringify(info));
  return info;
}

async function reviewConfirm(ctx, draft) {
  const msg = await ctx.anthropic.messages.create({
    model: ctx.model,
    max_tokens: 1000,
    system: buildReviewSystem(ctx.owner),
    output_config: jsonFormat(FLIGHT_REVIEW_SCHEMA),
    messages: [{ role: "user", content: buildReviewUser(ctx, draft) }],
  });
  const review = readReply(msg, "flights/review");
  console.log("FLIGHT REVIEW RAW:", JSON.stringify(review));
  return review;
}

async function reviewLink(ctx, options) {
  const msg = await ctx.anthropic.messages.create({
    model: ctx.model,
    max_tokens: 300,
    system: buildLinkReviewSystem(ctx.owner),
    output_config: jsonFormat(LINK_REVIEW_SCHEMA),
    messages: [{ role: "user", content: buildLinkReviewUser(ctx, options) }],
  });
  const review = readReply(msg, "flights/link");
  console.log("FLIGHT LINK RAW:", JSON.stringify(review));
  return review;
}

// ---- The draft --------------------------------------------------------------
const str = (v) => {
  const s = String(v ?? "").trim();
  return s || null;
};
const isoDate = (v) => {
  const m = String(v ?? "").match(/^\d{4}-\d{2}-\d{2}/);
  return m ? m[0] : null;
};
const numOr = (v, d) => (Number.isFinite(Number(v)) && Number(v) >= 0 ? Number(v) : d);

// Defaults for what he did not say: 1 adult, economy, one-way. There is no assumed origin.
function draftFromInfo(ctx, info) {
  return {
    origin: str(info?.origin),
    destination: str(info?.destination),
    depart_date: isoDate(info?.depart_date),
    return_date: isoDate(info?.return_date),
    adults: numOr(info?.adults, 1) || 1,
    children: numOr(info?.children, 0),
    infants: numOr(info?.infants, 0),
    cabin: KIWI_CABIN[info?.cabin] ? info.cabin : "economy",
    prefer: info?.prefer === "direct" || info?.prefer === "overnight" ? info.prefer : null,
    clarify_field: info?.clarify_field || null,
    clarify_kind: info?.clarify_kind || null,
    clarify_options: Array.isArray(info?.clarify_options) ? info.clarify_options : [],
    currency: ctx.env?.FLIGHT_CURRENCY || "BRL",
    summary: info?.summary || "",
  };
}

// Fold a review's corrected fields onto the pending draft. The review re-drafts in the
// same call, so a "modify" is a merge, not a second extraction pass.
function applyDraftUpdate(ctx, prev, review) {
  if (!review) return prev;
  const next = { ...prev };
  const patch = draftFromInfo(ctx, { ...prev, ...stripNulls(review) });
  Object.assign(next, patch, {
    // A one-way stays a one-way: only an explicit return_date brings it back.
    return_date: isoDate(review.return_date),
    clarify_field: prev.clarify_field,
    clarify_kind: prev.clarify_kind,
    clarify_options: prev.clarify_options,
    summary: prev.summary,
  });
  // He answered the clarification (the field he was asked about changed) -> drop the flag.
  if (prev.clarify_field && next[prev.clarify_field] !== prev[prev.clarify_field]) {
    next.clarify_field = null;
    next.clarify_kind = null;
    next.clarify_options = [];
  }
  return next;
}
const stripNulls = (o) =>
  Object.fromEntries(Object.entries(o || {}).filter(([, v]) => v !== null && v !== undefined));

// Today, in the owner's timezone — so "the 3rd" resolved to a past date is caught here
// and not by the provider (which answers a past date with a cheerful 0 results).
function todayIso() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Sao_Paulo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

// Everything that stands between the draft and a search. Order matters: a city we can't
// resolve is asked about before a date, and a date we can't use before a missing slot.
function missingOf(draft) {
  const today = todayIso();
  return {
    clarifyAmbiguous: !!draft.clarify_field && draft.clarify_kind === "ambiguous",
    clarifyUnknown: !!draft.clarify_field && draft.clarify_kind === "unknown",
    badDate: !!draft.depart_date && draft.depart_date < today,
    returnBeforeDepart:
      !!draft.depart_date && !!draft.return_date && draft.return_date < draft.depart_date,
    noOrigin: !draft.origin,
    noDestination: !draft.destination,
    noDate: !draft.depart_date,
  };
}
function isComplete(m) {
  return !Object.values(m).some(Boolean);
}

// ---- The flow ---------------------------------------------------------------
async function advanceSearch(ctx, draft) {
  const m = missingOf(draft);
  if (isComplete(m)) return openConfirm(ctx, draft);
  return openInquiry(ctx, draft, m);
}

// Ask for precisely ONE thing and keep the session open. Never guess, never search.
async function openInquiry(ctx, draft, m) {
  const { number } = ctx;
  const M = reply(ctx.lang);
  await armInfo(ctx, draft);
  if (m.clarifyAmbiguous) {
    await ctx.send(number, M.cityAmbiguous({ options: draft.clarify_options }));
    return;
  }
  if (m.clarifyUnknown) {
    await ctx.send(number, M.cityUnknown({ field: draft.clarify_field }));
    return;
  }
  if (m.badDate) {
    await ctx.send(number, M.badDate());
    return;
  }
  if (m.returnBeforeDepart) {
    await ctx.send(number, M.returnBeforeDepart());
    return;
  }
  if (m.noOrigin) {
    await ctx.send(number, M.askOrigin());
    return;
  }
  if (m.noDestination) {
    await ctx.send(number, M.askDestination());
    return;
  }
  await ctx.send(number, M.askDate());
}

// Confirm BEFORE searching: he reads what we understood, not three useless options.
async function openConfirm(ctx, draft) {
  const { number } = ctx;
  await armConfirm(ctx, draft);
  await ctx.send(number, renderConfirm(ctx.lang, draft));
}

// Kiwi -> selectOptions -> render -> stash -> await_link.
// The counters decide the message: "no flights", "no flights worth showing you", and
// "I could not read the provider's answer" are three different facts, and merging them
// would be the system lying about the world.
async function runSearch(ctx, draft) {
  const { number } = ctx;
  const M = reply(ctx.lang);

  const res = await searchKiwi(ctx, draft);
  if (!res?.ok) {
    await endFlow(ctx);
    await ctx.sendFailure(number, M.searchFailed());
    return;
  }

  const itineraries = res.payload?.itineraries || [];
  if (!itineraries.length) {
    await endFlow(ctx);
    await ctx.send(number, M.emptyResults());
    return;
  }

  const { options, judgedCount, keptCount, explicitEmptied } = selectOptions(itineraries, draft);

  // Nothing could be JUDGED: the fields the filter needs were absent. That is provider
  // shape-drift, not an empty result — file it, don't dress it up as "nothing good today".
  if (judgedCount === 0) {
    await endFlow(ctx);
    await ctx.sendFailure(number, M.searchFailed());
    return;
  }
  if (explicitEmptied) {
    await endFlow(ctx);
    await ctx.send(number, M.explicitFilterEmpty({ prefer: draft.prefer }));
    return;
  }
  if (keptCount === 0 || !options.length) {
    await endFlow(ctx);
    await ctx.send(number, M.emptyAfterFilter());
    return;
  }

  await writeOptions(ctx, options);
  await armLink(ctx, { book: false });

  const body = renderOptions(ctx.lang, options);
  // Fewer than 3 survivors: show what there is and say WHY. Never pad back to three,
  // never quietly disable the filter.
  if (options.length < 3) {
    await ctx.send(number, M.thinnedResults({ options: body, count: options.length }));
    return;
  }
  await ctx.send(number, M.results({ options: body }));
}

// The stash's three states -> three replies. BOTH the tagged path (C1, intent "link") and
// the untagged one (C2, decision "link") land here, so they cannot drift apart.
async function answerLink(ctx, optionNumber, { book = false } = {}) {
  const { number } = ctx;
  const M = reply(ctx.lang);
  const stash = await readStash(ctx);

  if (!stash) {
    await ctx.send(number, M.noResultsToLink());
    return;
  }
  if (stash.discarded === true) {
    await ctx.send(number, M.resultsDiscarded());
    return;
  }
  const options = Array.isArray(stash.options) ? stash.options : [];
  if (!options.length) {
    await ctx.send(number, M.noResultsToLink());
    return;
  }

  const n = Number(optionNumber);
  if (!Number.isInteger(n)) {
    await ctx.send(number, M.whichOption({ count: options.length }));
    await rearm(ctx, options, book);
    return;
  }
  if (n < 1 || n > options.length) {
    await ctx.send(number, M.optionOutOfRange({ count: options.length }));
    await rearm(ctx, options, book);
    return;
  }

  const opt = options[n - 1];
  if (!opt?.bookingUrl) {
    // He asked for the link of an option WE showed him and there isn't one. A real
    // failure (provider shape-drift), and it is declared. Never fabricate a URL.
    await ctx.sendFailure(number, M.linkMissing({ n }));
    await rearm(ctx, options, book); // stay at await_link so he can pick another
    return;
  }

  await ctx.send(number, M.linkSent({ n, url: opt.bookingUrl }));
  // He said "book it". She does not book, and says so — but the flow completed as
  // designed, so this is ctx.send, not a declared malfunction.
  if (book) await ctx.send(number, M.cannotBook());
  await ctx.sessions.clear(ctx.remoteJid);
  await clearStash(ctx);
}

// Invariant T: the question and the options must survive together.
async function rearm(ctx, options, book) {
  await writeOptions(ctx, options);
  await armLink(ctx, { book });
}

// ---- Continuations ----------------------------------------------------------
async function resumeInfo(ctx, session) {
  const { number } = ctx;
  const M = reply(ctx.lang);
  const draft = session.data?.draft || {};
  let review;
  try {
    review = await reviewConfirm(ctx, draft);
  } catch (e) {
    console.error("Flights/review error:", e);
    await ctx.sendFailure(number, M.thinkingError());
    return;
  }
  if (!review || review.decision === "unrelated") return; // chatter: stay silent
  if (review.decision === "cancel") {
    await endFlow(ctx);
    await clearStash(ctx);
    await ctx.send(number, M.declined());
    return;
  }
  return advanceSearch(ctx, applyDraftUpdate(ctx, draft, review));
}

async function resumeConfirm(ctx, session) {
  const { number } = ctx;
  const M = reply(ctx.lang);
  const draft = session.data?.draft || {};
  let review;
  try {
    review = await reviewConfirm(ctx, draft);
  } catch (e) {
    console.error("Flights/review error:", e);
    await ctx.sendFailure(number, M.thinkingError());
    return;
  }
  if (!review || review.decision === "unrelated") return; // chatter: stay silent
  if (review.decision === "cancel") {
    await endFlow(ctx);
    await clearStash(ctx);
    await ctx.send(number, M.declined());
    return;
  }
  if (review.decision === "modify")
    return advanceSearch(ctx, applyDraftUpdate(ctx, draft, review));
  return runSearch(ctx, draft);
}

async function resumeLink(ctx, session) {
  const { number } = ctx;
  const M = reply(ctx.lang);
  const stash = await readStash(ctx);
  let review;
  try {
    review = await reviewLink(ctx, stash?.options || []);
  } catch (e) {
    console.error("Flights/link review error:", e);
    await ctx.sendFailure(number, M.thinkingError());
    return;
  }
  if (!review) return;
  if (review.decision === "link" || review.decision === "book") {
    const book = session.data?.book === true || review.decision === "book";
    return answerLink(ctx, review.option_number, { book });
  }
  if (review.decision === "done") {
    await endFlow(ctx);
    await clearStash(ctx);
    return;
  }
  // "unrelated": silence, and NO session write — a stray "2pm" must not re-arm anything.
}

// ---- Entry point -------------------------------------------------------------
// ctx (from the orchestrator): { owner, tag, anthropic, model, order, transcript,
//   nowStr, contact, remoteJid, number, fromMe, quoted, env, evolution, send,
//   sendFailure, sessions, session, lang, hasSkill, callSkill }
export async function run(ctx) {
  const { number, session } = ctx;
  const M = reply(ctx.lang);

  // CONTINUATIONS owned by this skill (set by the orchestrator on a continuation).
  if (session?.skill === "flight_search" && session.stage === "await_info")
    return resumeInfo(ctx, session);
  if (session?.skill === "flight_search" && session.stage === "await_confirmation")
    return resumeConfirm(ctx, session);
  if (session?.skill === "flight_search" && session.stage === "await_link")
    return resumeLink(ctx, session);

  // FRESH (tagged) message.
  let info;
  try {
    info = await interpret(ctx);
  } catch (e) {
    console.error("Flights/Claude error:", e);
    await ctx.sendFailure(number, M.thinkingError());
    return;
  }
  if (!info) {
    await ctx.sendFailure(number, M.thinkingError());
    return;
  }

  if (info.intent === "search") {
    // INVARIANT S. HERE — before the slot chase, before the confirmation, before Kiwi is
    // ever called. A new search destroys the old options at FLOW START, not at result
    // time: otherwise a search that finds nothing leaves the PREVIOUS search's options
    // addressable, and "link for option 2" hands out a stale booking URL.
    await writeTombstone(ctx);
    return advanceSearch(ctx, draftFromInfo(ctx, info));
  }

  // A link request is NOT a search: it must not tombstone anything.
  if (info.intent === "link" || info.intent === "book")
    return answerLink(ctx, info.option_number, { book: info.intent === "book" });

  // He asked, and he got nothing: declare it, so a misroute reaches the Bugs board.
  await ctx.sendFailure(number, M.notAFlight({ summary: info.summary }));
}

// --- exported ONLY for scripts/flights-selftest.mjs ---------------------------
// loadSkills() reads manifest / run / capabilities and nothing else (server.js:100-107),
// so these extra named exports are INERT at boot and cost nothing at runtime. They exist
// so the tests exercise the PRODUCTION functions — in particular selectOptions, which owns
// the load-bearing filter -> sort -> take-3 order.
export {
  selectOptions,
  filterItineraries,
  applyExplicitFilters,
  topCheapest,
  parseKiwiResponse,
  toKiwiDate,
  KIWI_CABIN,
  stashKey,
  readStash,
  writeOptions,
  writeTombstone,
  answerLink,
};
