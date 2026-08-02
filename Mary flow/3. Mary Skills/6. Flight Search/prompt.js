// ============================================================================
//  Skill "Flight Search" — PROMPT + user-facing STRINGS.  CONVERTED (pure task).
//  Localized reply strings + the option renderer only — no logic (that's skill.js).
//
//  The structured-output SCHEMAS and the interpret/review/link-review prompt builders that used
//  to live here are gone: the ORCHESTRATOR classifies the order into the skill's declared inputs
//  and runs the slot-chase + confirmation dialogue. What remains is renderOptions (the numbered
//  options block) and the OUTCOME + error strings. The PROPOSAL / QUESTION strings (renderConfirm,
//  askOrigin/Destination/Date, cityAmbiguous/Unknown, …) are dropped — the model writes those.
//
//  LOCALIZATION: keep BOTH en + pt; any other language is produced from the `en` copy by the
//  orchestrator's send() translation fallback.
// ============================================================================

// A DATE-ONLY string ("Fri, Aug 14" / "sex., 14 de ago.") from an ISO date OR datetime. Kiwi's
// times are LOCAL to their airport and carry no offset, so the date is taken from the STRING.
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

// "10h25" from a duration in seconds. Empty string when the provider gave us nothing usable.
export function fmtDuration(lang, seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return "";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h${String(m).padStart(2, "0")}`;
}

// The clock time of an ISO datetime, read from the STRING (local airport times, no offset).
function hhmm(iso) {
  const m = String(iso ?? "").match(/^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/);
  return m ? m[1] : "";
}

const WORDS = {
  en: { out: "out", back: "back", direct: "direct", stop: "stop", stops: "stops", via: "via" },
  pt: { out: "ida", back: "volta", direct: "direto", stop: "parada", stops: "paradas", via: "via" },
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

// The numbered option block. A ONE-WAY carries `inbound: null` (present, not absent), so it
// renders exactly one leg.
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

// ============================================================================
//  USER-FACING REPLY STRINGS (localized).
//  Results + the link turn + failures. `en` and `pt`; searchFailed / linkMissing / notAFlight
//  go out on ctx.sendFailure (see skill.js), the rest on ctx.send.
// ============================================================================
const REPLY = {
  en: {
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
    notAFlight: ({ summary }) =>
      `I didn't identify a flight request. ${summary || ""}`.trim(),
    linkMissing: ({ n }) =>
      `Option ${n} came back without a booking link. Pick another option and I'll send that one.`,
  },

  pt: {
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
