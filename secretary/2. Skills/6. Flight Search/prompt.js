// ============================================================================
//  Skill "Flight Search" — PROMPT + user-facing STRINGS.
//  Prompt text/rules and localized reply strings only — no logic (that's skill.js).
//  The output JSON must keep matching what skill.js expects.
//
//  LOCALIZATION: user-facing strings are a per-language map, selected at send time
//  with ctx.lang via reply(). English is canonical; pt is maintained; any other
//  language is produced from the `en` copy by the orchestrator's send() translation
//  fallback. Add BOTH en + pt for every new message. Internal/classification prompts
//  (buildFlightSystem below) stay English.
//
//  THREE discriminators run in this skill, and they are three different questions:
//    FLIGHT_SCHEMA       (C1) — a FRESH message: is this a search, a link request for
//                               options already shown, "book it", or none of those?
//    FLIGHT_REVIEW_SCHEMA (B) — a pending draft is on the table: confirm / modify /
//                               cancel / unrelated (a modify RE-DRAFTS in the same call).
//    LINK_REVIEW_SCHEMA  (C2) — options are on the table: link / book / done / unrelated.
//
//  NOTE ON THE REPLY MAP: no reply in this skill claims a search EXPIRED. The skill
//  cannot know that — an absent stash means "expired" OR "already sent" OR "never
//  searched", and saying the wrong one is a lie. It says only what it knows.
// ============================================================================

// ---- JSON Schemas for structured outputs (output_config.format) --------------
// Single source of truth for the SHAPE of each reply. skill.js passes these to
// messages.create so the API returns ONLY schema-valid JSON. Every object needs
// additionalProperties:false + a full `required` list; optional fields use a
// nullable type union — EXCEPT a nullable ENUM, which the validator rejects as
// type:["string","null"]+enum and which must therefore use anyOf (the precedent is
// CAL_SCHEMA.list_mode, 1. Calendar Actions/prompt.js:50-52).
const CABIN_ENUM = ["economy", "premium_economy", "business", "first"];

export const FLIGHT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: [
    "intent",
    "option_number",
    "origin",
    "destination",
    "depart_date",
    "return_date",
    "adults",
    "children",
    "infants",
    "cabin",
    "prefer",
    "clarify_field",
    "clarify_kind",
    "clarify_options",
    "summary",
  ],
  properties: {
    // What KIND of order this is. "book" exists because "buy option 2" must both send
    // the link and say plainly that the secretary cannot purchase.
    intent: { type: "string", enum: ["search", "link", "book", "other"] },
    // "link"/"book" only: which of the options already shown (1-based). null if unsaid.
    option_number: { type: ["number", "null"] },
    origin: { type: ["string", "null"] },
    destination: { type: ["string", "null"] },
    // ISO YYYY-MM-DD. return_date null = one-way.
    depart_date: { type: ["string", "null"] },
    return_date: { type: ["string", "null"] },
    adults: { type: ["number", "null"] },
    children: { type: ["number", "null"] },
    infants: { type: ["number", "null"] },
    cabin: { anyOf: [{ type: "null" }, { type: "string", enum: CABIN_ENUM }] },
    // The owner's EXPLICIT hard filter only — never inferred from taste.
    prefer: {
      anyOf: [{ type: "null" }, { type: "string", enum: ["direct", "overnight"] }],
    },
    // A city we cannot resolve on our own: which field, and why.
    clarify_field: {
      anyOf: [{ type: "null" }, { type: "string", enum: ["origin", "destination"] }],
    },
    clarify_kind: {
      anyOf: [{ type: "null" }, { type: "string", enum: ["ambiguous", "unknown"] }],
    },
    // For "ambiguous": the candidates to offer, e.g. ["Santiago, Chile (SCL)", …].
    clarify_options: { type: "array", items: { type: "string" } },
    summary: { type: "string" },
  },
};

// A pending draft is on the table (the slot chase or the confirmation). A "modify"
// carries the corrected draft fields, so one call both classifies AND re-drafts —
// the REVIEW_SCHEMA precedent (1. Calendar Actions/prompt.js:61-66).
export const FLIGHT_REVIEW_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: [
    "decision",
    "origin",
    "destination",
    "depart_date",
    "return_date",
    "adults",
    "children",
    "infants",
    "cabin",
    "prefer",
  ],
  properties: {
    decision: {
      type: "string",
      enum: ["confirm", "modify", "cancel", "unrelated"],
    },
    origin: { type: ["string", "null"] },
    destination: { type: ["string", "null"] },
    depart_date: { type: ["string", "null"] },
    return_date: { type: ["string", "null"] },
    adults: { type: ["number", "null"] },
    children: { type: ["number", "null"] },
    infants: { type: ["number", "null"] },
    cabin: { anyOf: [{ type: "null" }, { type: "string", enum: CABIN_ENUM }] },
    prefer: {
      anyOf: [{ type: "null" }, { type: "string", enum: ["direct", "overnight"] }],
    },
  },
};

// The numbered options are on the table (stage await_link).
export const LINK_REVIEW_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["decision", "option_number"],
  properties: {
    decision: { type: "string", enum: ["link", "book", "done", "unrelated"] },
    option_number: { type: ["number", "null"] },
  },
};

// ---- C1: the fresh-message discriminator + extraction ------------------------
export function buildFlightSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s flight assistant. Read the conversation and the latest order, then decide FIRST what kind of order this is, and only then extract its data. (Your reply's shape is enforced separately — focus on getting the values right.)

Choosing "intent":
- "search": ${OWNER_NAME} wants you to LOOK FOR flights ("find me a flight to Lisbon on the 14th", "voos pra Lisboa dia 14"). Any new or changed search is "search".
- "link": ${OWNER_NAME} is asking for the BOOKING LINK of an option you ALREADY showed him ("link for option 2", "manda o link do 2", "send me the second one"). Nothing new is being searched.
- "book": ${OWNER_NAME} is asking you to BUY / BOOK an option you already showed him ("book it", "compra a 2", "buy option 2"). This is NOT "link" — you cannot purchase, and the difference must reach the code.
- "other": none of the above (ordinary conversation, a question you cannot answer with a flight search).

For "link" and "book": "option_number" is the 1-based number he named, or null if he named none ("send me the link", "the good one").

For "search", fill the trip:
- origin / destination: an IATA code, a metro code (SAO, RIO, LON) or a plain city name — the provider resolves any of them. Do NOT invent an origin: if he never says where he is flying FROM, leave origin null.
- depart_date / return_date: ISO "YYYY-MM-DD", resolving relative dates ("the 14th", "next Friday", "tomorrow") against the current date/time provided. return_date = null means a ONE-WAY trip; only fill it if he asks to come back.
- adults / children / infants: only if stated; null otherwise (the code defaults to 1 adult).
- cabin: only if stated ("business", "executiva", "primeira") — null otherwise.
- prefer: ONLY when he states a hard requirement himself: "direct" ("direct only", "sem escalas", "só voo direto") or "overnight" ("overnight", "voo noturno", "red-eye"). Never infer it from taste; null when he does not say it.

City trouble (fill these ONLY for "search"):
- clarify_field + clarify_kind = "ambiguous" when the city he names is genuinely ambiguous and you would have to GUESS a country (e.g. "Santiago", "San José", "Córdoba"). Put the plausible readings in "clarify_options" (e.g. ["Santiago, Chile (SCL)", "Santiago de Compostela, Spain (SCQ)"]). Never guess.
- clarify_field + clarify_kind = "unknown" when the place has no airport you can name at all.
- Otherwise all three are null / [].
- A city with exactly one obvious airport is NOT ambiguous (Lisbon = LIS). Only genuine ambiguity counts.

"summary": one line describing what you understood.

When in doubt between "search" and "other", prefer "other" — a wrong search wastes a real call and shows him three useless options.`;
}

export function buildFlightUser(ctx) {
  return `Current date/time: ${ctx.nowStr} (America/Sao_Paulo, -03:00).

Recent conversation:
${ctx.transcript || "(no history)"}

${ctx.owner}'s latest message: ${ctx.order}`;
}

// ---- B: the review of a pending draft (slot chase + confirmation) ------------
export function buildReviewSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s flight assistant. A flight search DRAFT is pending: he has been shown what you understood (or asked for a missing detail) and his latest message is the answer. Classify it, and — when he changes something — return the CORRECTED draft in the same reply.

"decision":
- "confirm": he agrees / says go ahead ("yes", "sim", "go", "isso", "perfect", "manda ver"). Nothing changes.
- "modify": he supplies missing information OR corrects/changes the trip ("2 people", "business", "make it the 15th", "from Rio actually", "add a return on the 22nd"). This includes ANSWERING a question you asked him (an origin, a destination, a date, which of two cities he meant).
- "cancel": he calls the search off ("no", "leave it", "deixa pra lá", "esquece").
- "unrelated": ordinary conversation that is not an answer to the pending draft. When in doubt, choose "unrelated" — silence is the safe default; a wrong search is not.

For "modify", return the FULL corrected trip in the fields below, keeping every value that did not change (they are given to you). For any other decision, just echo the current draft's values.
- origin / destination: an IATA code, a metro code, or a city name.
- depart_date / return_date: ISO "YYYY-MM-DD" (resolve relative dates against the current date/time). return_date = null means one-way — set it to null if he drops the return.
- adults / children / infants: numbers; cabin: one of economy / premium_economy / business / first; prefer: "direct", "overnight", or null.`;
}

export function buildReviewUser(ctx, draft) {
  return `Current date/time: ${ctx.nowStr} (America/Sao_Paulo, -03:00).

The PENDING draft:
- origin: ${draft?.origin ?? "(missing)"}
- destination: ${draft?.destination ?? "(missing)"}
- depart_date: ${draft?.depart_date ?? "(missing)"}
- return_date: ${draft?.return_date ?? "(none — one-way)"}
- adults: ${draft?.adults ?? 1}
- children: ${draft?.children ?? 0}
- infants: ${draft?.infants ?? 0}
- cabin: ${draft?.cabin ?? "economy"}
- prefer: ${draft?.prefer ?? "(none)"}

Recent conversation:
${ctx.transcript || "(no history)"}

${ctx.owner}'s latest message: ${ctx.order}`;
}

// ---- C2: the review while the numbered options are on the table --------------
export function buildLinkReviewSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s flight assistant. You have just shown him a short numbered list of flight options, each with a price. His latest message may or may not be about them. Classify it.

"decision":
- "link": he wants the BOOKING LINK of one of the options ("link for option 2", "manda o link do 2", "send me the second one", or just "2").
- "book": he wants you to BUY / BOOK one of them ("book it", "compra essa", "buy option 2"). Keep this apart from "link" — you cannot purchase, and the difference must reach the code.
- "done": he is finished with the options ("that's all", "pronto", "no thanks", "deixa").
- "unrelated": ordinary conversation. When in doubt, choose "unrelated" — a stray "2pm" is not "option 2", and silence is the safe default.

"option_number": for "link"/"book", the 1-based number he named — or null if he named none ("send me the link", "the cheap one"). null for every other decision.`;
}

export function buildLinkReviewUser(ctx, options) {
  const list = (options || [])
    .map(
      (o, i) =>
        `${i + 1}. ${o?.priceFormatted || o?.price || "?"} — ${o?.outbound?.from || "?"} → ${
          o?.outbound?.to || "?"
        }`
    )
    .join("\n");
  return `Current date/time: ${ctx.nowStr} (America/Sao_Paulo, -03:00).

The options ${ctx.owner} is looking at:
${list || "(none)"}

Recent conversation:
${ctx.transcript || "(no history)"}

${ctx.owner}'s latest message: ${ctx.order}`;
}

// ============================================================================
//  RENDERERS (localized). They live here, not in skill.js — every user-facing
//  string in this skill ships `en` AND `pt` from this file.
// ============================================================================

// A DATE-ONLY string ("Fri, Aug 14" / "sex., 14 de ago.") from an ISO date OR an ISO
// datetime. Kiwi's times are LOCAL to their airport and carry no offset, so the date
// is taken from the STRING (the first 10 chars) and formatted in UTC — never through
// the machine's timezone, which would slide a date across midnight.
export function localizeFlightDate(lang, iso) {
  const m = String(iso ?? "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return "";
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  if (isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(lang === "pt" ? "pt-BR" : "en-US", {
    timeZone: "UTC",
    weekday: "short",
    month: "short",
    day: "numeric",
  }).format(d);
}

// "10h25" from a duration in seconds. Empty string when the provider gave us nothing
// usable — a rendered "NaNhNaN" is worse than a missing field.
export function fmtDuration(lang, seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return "";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h${String(m).padStart(2, "0")}`;
}

// The clock time of an ISO datetime, read from the STRING for the same reason as
// localizeFlightDate: these are local airport times with no offset.
function hhmm(iso) {
  const m = String(iso ?? "").match(/^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/);
  return m ? m[1] : "";
}

const WORDS = {
  en: {
    out: "out",
    back: "back",
    direct: "direct",
    stop: "stop",
    stops: "stops",
    via: "via",
    passenger: "passenger",
    passengers: "passengers",
    cabin: {
      economy: "economy",
      premium_economy: "premium economy",
      business: "business",
      first: "first class",
    },
    preferDirect: "direct flights only",
    preferOvernight: "overnight flights",
    oneWay: "one-way",
    confirmHead: "Searching:",
    confirmOut: "out",
    confirmBack: "back",
    confirmPrices: (cur) => `Prices in ${cur}.`,
    confirmAsk: 'Shall I search? Reply "yes" to go ahead, or tell me what to change.',
  },
  pt: {
    out: "ida",
    back: "volta",
    direct: "direto",
    stop: "parada",
    stops: "paradas",
    via: "via",
    passenger: "passageiro",
    passengers: "passageiros",
    cabin: {
      economy: "econômica",
      premium_economy: "econômica premium",
      business: "executiva",
      first: "primeira classe",
    },
    preferDirect: "somente voos diretos",
    preferOvernight: "voos noturnos",
    oneWay: "só ida",
    confirmHead: "Buscando:",
    confirmOut: "ida",
    confirmBack: "volta",
    confirmPrices: (cur) => `Preços em ${cur}.`,
    confirmAsk: 'Posso buscar? Responda "sim" para eu seguir, ou me diga o que mudar.',
  },
};

const words = (lang) => WORDS[lang] || WORDS.en;

// "direct" / "1 stop via REC" / "2 stops via GIG, MAD".
function stopsLabel(lang, leg) {
  const w = words(lang);
  const n = Number(leg?.stops);
  if (!Number.isFinite(n) || n <= 0) return w.direct;
  const mid = Array.isArray(leg.route) ? leg.route.slice(1, -1) : [];
  const label = `${n} ${n === 1 ? w.stop : w.stops}`;
  return mid.length ? `${label} ${w.via} ${mid.join(", ")}` : label;
}

// One leg: "Fri, Aug 14, 22:10 GRU → 12:35 LIS (direct, 11h25)".
function legLine(lang, leg) {
  const date = localizeFlightDate(lang, leg?.departureTime);
  const dep = hhmm(leg?.departureTime);
  const arr = hhmm(leg?.arrivalTime);
  const from = `${dep ? `${dep} ` : ""}${leg?.from || ""}`.trim();
  const to = `${arr ? `${arr} ` : ""}${leg?.to || ""}`.trim();
  const notes = [stopsLabel(lang, leg), fmtDuration(lang, leg?.durationSeconds)].filter(Boolean);
  const route = `${from} → ${to}`;
  return `${date ? `${date}, ` : ""}${route}${notes.length ? ` (${notes.join(", ")})` : ""}`;
}

// The numbered option block. A ONE-WAY carries `inbound: null` (present, not absent),
// so it renders exactly one leg — no dash, no empty return field.
export function renderOptions(lang, options) {
  const w = words(lang);
  return (options || [])
    .map((it, i) => {
      const price = it?.priceFormatted || (it?.price != null ? String(it.price) : "");
      const legs = [];
      if (it?.outbound) legs.push(`${w.out} ${legLine(lang, it.outbound)}`);
      if (it?.inbound) legs.push(`${w.back} ${legLine(lang, it.inbound)}`);
      return `${i + 1}. ${price}\n${legs.join("\n")}`;
    })
    .join("\n\n");
}

function paxLabel(lang, draft) {
  const w = words(lang);
  const n =
    Number(draft?.adults || 0) + Number(draft?.children || 0) + Number(draft?.infants || 0) || 1;
  return `${n} ${n === 1 ? w.passenger : w.passengers}`;
}

// The confirm-first summary, shown BEFORE any search: everything we understood, spelled
// out, so a wrong origin/date/cabin is caught before he reads three useless options.
export function renderConfirm(lang, draft) {
  const w = words(lang);
  const parts = [
    `${draft?.origin || "?"} → ${draft?.destination || "?"}`,
    `${w.confirmOut} ${localizeFlightDate(lang, draft?.depart_date)}`,
  ];
  if (draft?.return_date) parts.push(`${w.confirmBack} ${localizeFlightDate(lang, draft.return_date)}`);
  else parts.push(w.oneWay);
  parts.push(paxLabel(lang, draft));
  parts.push(w.cabin[draft?.cabin] || w.cabin.economy);
  if (draft?.prefer === "direct") parts.push(w.preferDirect);
  if (draft?.prefer === "overnight") parts.push(w.preferOvernight);
  return `${w.confirmHead} ${parts.join(", ")}. ${w.confirmPrices(
    draft?.currency || "BRL"
  )}\n\n${w.confirmAsk}`;
}

// ============================================================================
//  USER-FACING REPLY STRINGS (localized) — 23 keys, `en` and `pt`.
//  19 go out on ctx.send; 4 on ctx.sendFailure (searchFailed, thinkingError,
//  notAFlight, linkMissing). See SKILL.md for the sender of each and why.
// ============================================================================
const REPLY = {
  en: {
    // --- gathering (questions — never failures) ---
    askOrigin: () => "Where are you flying from? Give me the city or the airport.",
    askDestination: () => "Where do you want to fly to?",
    askDate: () => "What day do you want to leave?",
    cityAmbiguous: ({ options }) =>
      `Which one do you mean?\n${(options || []).map((o) => `- ${o}`).join("\n")}`,
    cityUnknown: () =>
      "I couldn't place that city. Can you give me the airport or another nearby city?",
    badDate: () => "That date is in the past. Which day did you mean?",
    returnBeforeDepart: () =>
      "The return is before the departure. Which dates did you mean?",
    declined: () => "Okay, no search then.",

    // --- results ---
    results: ({ options }) =>
      `${options}\n\nPrices are indicative and confirmed at booking. Want the link for one? Say "link for option 2".`,
    thinnedResults: ({ options, count }) =>
      `Only ${count === 1 ? "1 workable option" : `${count} workable options`} on that date — the rest were multi-stop or split-ticket itineraries I wouldn't put in front of you.\n\n${options}\n\nPrices are indicative and confirmed at booking. Want the link for one? Say "link for option 2".`,
    emptyResults: () => "I found no flights for that search. Want to try another date?",
    emptyAfterFilter: () =>
      "Only multi-stop / split-ticket itineraries were on offer for that date — nothing I'd put in front of you. Try another date?",
    explicitFilterEmpty: () =>
      "Nothing matched what you asked for on that date. Want me to drop that condition, or try another date?",

    // --- the link turn ---
    linkSent: ({ n, url }) => `Option ${n} — here's the booking link:\n${url}`,
    whichOption: ({ count }) => `Which one? I showed you ${count}.`,
    optionOutOfRange: ({ count }) =>
      `There ${count === 1 ? "was only 1 option" : `were only ${count} options`}. Which one?`,
    resultsDiscarded: () =>
      "I dropped those options when the new search started. Want me to search again?",
    noResultsToLink: () =>
      "I don't have any flight options on hand. Want me to search?",
    cannotBook: () =>
      "I can't buy the ticket for you — I search and hand you the link; the purchase is yours to make on that page.",

    // --- failures (ctx.sendFailure) ---
    searchFailed: () =>
      "I couldn't complete the flight search. The error is in the log. Try again?",
    thinkingError: () => "I hit an error while thinking. Try again?",
    notAFlight: ({ summary }) =>
      `I didn't identify a flight request. ${summary || ""}`.trim(),
    linkMissing: ({ n }) =>
      `Option ${n} came back without a booking link. Pick another option and I'll send that one.`,
  },

  pt: {
    // --- gathering (questions — never failures) ---
    askOrigin: () => "De onde você vai sair? Me diga a cidade ou o aeroporto.",
    askDestination: () => "Para onde você quer voar?",
    askDate: () => "Que dia você quer ir?",
    cityAmbiguous: ({ options }) =>
      `Qual delas você quis dizer?\n${(options || []).map((o) => `- ${o}`).join("\n")}`,
    cityUnknown: () =>
      "Não consegui identificar essa cidade. Pode me dar o aeroporto ou outra cidade perto?",
    badDate: () => "Essa data já passou. Que dia você quis dizer?",
    returnBeforeDepart: () =>
      "A volta está antes da ida. Quais são as datas certas?",
    declined: () => "Ok, sem busca então.",

    // --- results ---
    results: ({ options }) =>
      `${options}\n\nOs preços são indicativos e confirmados na reserva. Quer o link de alguma? É só dizer "link da opção 2".`,
    thinnedResults: ({ options, count }) =>
      `${count === 1 ? "Só 1 opção decente" : `Só ${count} opções decentes`} nessa data — o resto eram itinerários com muitas conexões ou bilhetes separados, que eu não te mostraria.\n\n${options}\n\nOs preços são indicativos e confirmados na reserva. Quer o link de alguma? É só dizer "link da opção 2".`,
    emptyResults: () => "Não encontrei voos para essa busca. Quer tentar outra data?",
    emptyAfterFilter: () =>
      "Nessa data só havia itinerários com muitas conexões ou bilhetes separados — nada que eu te mostraria. Quer tentar outra data?",
    explicitFilterEmpty: () =>
      "Nada atendeu ao que você pediu nessa data. Quer tirar essa condição, ou tentar outra data?",

    // --- the link turn ---
    linkSent: ({ n, url }) => `Opção ${n} — aqui está o link da reserva:\n${url}`,
    whichOption: ({ count }) => `Qual delas? Te mostrei ${count}.`,
    optionOutOfRange: ({ count }) =>
      `${count === 1 ? "Só havia 1 opção" : `Só havia ${count} opções`}. Qual delas?`,
    resultsDiscarded: () =>
      "Descartei aquelas opções quando a nova busca começou. Quer que eu busque de novo?",
    noResultsToLink: () =>
      "Não tenho nenhuma opção de voo em mãos. Quer que eu busque?",
    cannotBook: () =>
      "Não consigo comprar a passagem para você — eu busco e te mando o link; a compra é você quem faz nessa página.",

    // --- failures (ctx.sendFailure) ---
    searchFailed: () =>
      "Não consegui completar a busca de voos. O erro está no log. Quer tentar de novo?",
    thinkingError: () => "Tive um erro ao processar. Pode tentar de novo?",
    notAFlight: ({ summary }) =>
      `Não identifiquei um pedido de voo. ${summary || ""}`.trim(),
    linkMissing: ({ n }) =>
      `A opção ${n} voltou sem link de reserva. Escolha outra opção que eu mando o link dela.`,
  },
};

// Pick the reply set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
