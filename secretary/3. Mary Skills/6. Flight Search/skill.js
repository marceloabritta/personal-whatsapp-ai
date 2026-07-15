// ============================================================================
//  Skill "Flight Search" — LOGIC.  CONVERTED (pure task, read-then-act).
//  In the NEW (@mary) flow the ORCHESTRATOR runs the conversation (the slot chase and the
//  confirmation) and hands a validated payload in ctx.info. run() is a pure dispatch on
//  ctx.info.intent:
//    - search (READ/act): search Kiwi, select the options, send them, STASH them in the sidecar,
//                         and return { options:[{n,summary,price,bookingUrl}], count }.
//    - link   (ACT):      read the sidecar, resolve the numbered option, send the booking link,
//                         return { ok, option }.
//    - book / other:      say the secretary cannot buy / this is not a flight, return { ok:false }.
//  It never buys anything. There is NO in-skill session and NO LLM pass — but the OPTIONS
//  SIDECAR stays: it is a data cache keyed off a SEPARATE redis key (`${remoteJid}|flights`),
//  NOT a conversation session (it never sets session.skill), so it does not violate the
//  pure-task rule.
//
//  Run by the orchestrator when the router picks "flight_search".
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description, conversation, inputs }
//    export async function run(ctx) -> a JSON-serializable value (the read-back)
//
//  TWO THINGS IN HERE WILL LOOK LIKE BUGS AND ARE NOT. Read SKILL.md before "fixing":
//  1. THE RESULT FILTER RUNS BEFORE THE SORT. selectOptions() owns filter -> sort -> take-3.
//  2. INVARIANT S — a new search DESTROYS the old options AT FLOW START. writeTombstone() is
//     called in run() the moment intent is "search", before Kiwi is ever queried.
// ============================================================================
import { renderOptions, reply } from "./prompt.js";

// `inputs` — the DECLARED input contract (see 1. Orchestrator/lib/inputs.js). The orchestrator
// fills it in the same round-trip that classifies the order, and gates on `ok` before dispatch.
// `intent` is the discriminator: a search names origin/destination/depart_date; a link names the
// option_number of an option already shown.
export const manifest = {
  id: "flight_search",
  // CONVERTED (pure task): the model runs the dialogue and confirms before a search; run() acts.
  conversation: "orchestrator",
  description:
    "search for flights and show the cheapest sensible options (origin, destination, dates, " +
    "passengers, cabin), and send the booking link for a flight option ALREADY shown in this " +
    "conversation (e.g. 'link for option 2'); it only SEARCHES and never buys — it is NOT for " +
    "adding a flight-related to-do or reminder to the task list",
  inputs: {
    discriminator: "intent",
    fields: {
      intent: { type: "enum", enum: ["search", "link", "book", "other"] },
      option_number: {
        type: "number",
        nullable: true,
        desc: 'link/book only: which option already shown (1-based)',
      },
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
      summary: { type: "string", nullable: true },
    },
    requiredWhen: {
      search: ["origin", "destination", "depart_date"],
      link: ["option_number"],
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
    rulebook: () =>
      "A flight order is one of: search (find flights — needs origin, destination and a depart " +
      "date; a return_date makes it round-trip), link (send the booking link of an option ALREADY " +
      "shown — needs its 1-based option_number), or book (the owner asked to BUY — the secretary " +
      "cannot, and says so). Dates are ISO YYYY-MM-DD; never invent an origin the owner didn't give.",
  },
};

// ---- Kiwi -------------------------------------------------------------------
const KIWI_URL = "https://mcp.kiwi.com";
const KIWI_TOOL = "search-flight";
const TIMEOUT_MS = 20_000;
const TTL = 600; // sidecar stash TTL, seconds

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
    // `locale` drives KIWI's booking page, not our reply — deliberately fixed.
    locale: "pt",
  };
  const back = toKiwiDate(draft.return_date);
  if (back) args.returnDate = back;
  return args;
}

// SSE text -> { ok: true, payload } | { ok: false, reason }. Never throws.
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
  // THE TRAP: an HTTP 200 whose body says isError. content[0].text is then a plain error string.
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
const legsOf = (it) => [it?.outbound, it?.inbound].filter((l) => l != null);

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

// Rule 1: more than 1 stop on a leg. Rule 2: a leg whose segments are not all the same carrier —
// the self-transfer / virtual-interlining fallback, which Kiwi exposes no marker for.
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

// The owner's OWN hard filters, applied ON TOP of the mandatory one — never instead of it.
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

// ---- The options sidecar (a data cache, NOT a conversation session) ----------
// The options live under their OWN key, `${remoteJid}|flights`. Three states:
//   { options } -> live       -> send the link
//   { discarded: true } -> a new search killed them (Invariant S) -> say so
//   null -> nothing here       -> "I have no options on hand"
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
    currency: ctx.env?.FLIGHT_CURRENCY || "BRL",
    summary: info?.summary || "",
  };
}

// ---- The flow ---------------------------------------------------------------
// Kiwi -> selectOptions -> render -> stash -> return the options for the read-back. The counters
// keep "no flights", "no flights worth showing you", and "I could not read the answer" apart.
async function runSearch(ctx, draft) {
  const { number } = ctx;
  const M = reply(ctx.lang);

  const res = await searchKiwi(ctx, draft);
  if (!res?.ok) {
    await ctx.sendFailure(number, M.searchFailed());
    return { ok: false, reason: "searchFailed" };
  }

  const itineraries = res.payload?.itineraries || [];
  if (!itineraries.length) {
    await ctx.send(number, M.emptyResults());
    return { options: [], count: 0 };
  }

  const { options, judgedCount, keptCount, explicitEmptied } = selectOptions(itineraries, draft);

  // Nothing could be JUDGED: provider shape-drift, not an empty result.
  if (judgedCount === 0) {
    await ctx.sendFailure(number, M.searchFailed());
    return { ok: false, reason: "unreadable" };
  }
  if (explicitEmptied) {
    await ctx.send(number, M.explicitFilterEmpty({ prefer: draft.prefer }));
    return { options: [], count: 0 };
  }
  if (keptCount === 0 || !options.length) {
    await ctx.send(number, M.emptyAfterFilter());
    return { options: [], count: 0 };
  }

  await writeOptions(ctx, options);

  const body = renderOptions(ctx.lang, options);
  if (options.length < 3) {
    await ctx.send(number, M.thinnedResults({ options: body, count: options.length }));
  } else {
    await ctx.send(number, M.results({ options: body }));
  }

  // Lean candidates for the read-back so the serialized value stays under READBACK_CAP.
  return {
    options: options.map((o, i) => ({
      n: i + 1,
      summary: `${o?.outbound?.from || "?"} → ${o?.outbound?.to || "?"}`,
      price: o?.priceFormatted || (o?.price != null ? String(o.price) : ""),
      bookingUrl: o?.bookingUrl || null,
    })),
    count: options.length,
  };
}

// The sidecar's three states -> three replies. option_number is required by the gate, so a
// missing/invalid number here is defensive only.
async function answerLink(ctx, optionNumber, { book = false } = {}) {
  const { number } = ctx;
  const M = reply(ctx.lang);
  const stash = await readStash(ctx);

  if (!stash) {
    await ctx.send(number, M.noResultsToLink());
    return { ok: false, reason: "noOptions" };
  }
  if (stash.discarded === true) {
    await ctx.send(number, M.resultsDiscarded());
    return { ok: false, reason: "discarded" };
  }
  const options = Array.isArray(stash.options) ? stash.options : [];
  if (!options.length) {
    await ctx.send(number, M.noResultsToLink());
    return { ok: false, reason: "noOptions" };
  }

  const n = Number(optionNumber);
  if (!Number.isInteger(n) || n < 1 || n > options.length) {
    await ctx.send(number, M.optionOutOfRange({ count: options.length }));
    return { ok: false, reason: "outOfRange" };
  }

  const opt = options[n - 1];
  if (!opt?.bookingUrl) {
    // He asked for the link of an option WE showed him and there isn't one. A real failure.
    await ctx.sendFailure(number, M.linkMissing({ n }));
    return { ok: false, reason: "linkMissing" };
  }

  await ctx.send(number, M.linkSent({ n, url: opt.bookingUrl }));
  // He said "book it". She does not book, and says so — the flow completed as designed.
  if (book) await ctx.send(number, M.cannotBook());
  await clearStash(ctx);
  return { ok: true, option: n };
}

// ---- Entry point -------------------------------------------------------------
export async function run(ctx) {
  const { number } = ctx;
  const M = reply(ctx.lang);
  const info = ctx.info || {};

  if (info.intent === "search") {
    // INVARIANT S — a new search destroys the old options at FLOW START, before Kiwi is queried.
    await writeTombstone(ctx);
    return runSearch(ctx, draftFromInfo(ctx, info));
  }

  if (info.intent === "link") return answerLink(ctx, info.option_number, { book: false });

  if (info.intent === "book") {
    // The secretary does not buy — say so. If he then wants the link, the orchestrator dispatches
    // a `link` intent. (answerLink's `book` path is kept for that link turn.)
    await ctx.send(number, M.cannotBook());
    return { ok: false, reason: "cannotBook" };
  }

  // "other" — a misroute reaches the Bugs board.
  await ctx.sendFailure(number, M.notAFlight({ summary: info.summary }));
  return { ok: false, reason: "notAFlight" };
}

// --- exported ONLY for parity with the old tree's tests (INERT at boot) -------
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
