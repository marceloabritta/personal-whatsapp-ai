// ============================================================================
//  Skill "Calendar Actions" — PROMPT + user-facing STRINGS.  CONVERTED (pure task).
//  Prompt text/rules and localized reply strings only — no logic (that's skill.js).
//
//  The structured-output SCHEMAS and the interpret/review/resolve/edit prompt builders that
//  used to live here are gone: the ORCHESTRATOR classifies the order into the skill's declared
//  inputs (skill.js manifest) and runs the propose/confirm dialogue. What remains is:
//    - buildExtractionRules — retained VERBATIM, now feeding manifest.inputs.rulebook only;
//    - the localized OUTCOME + error strings (createDone, editDone, deleteCancelled, list*, all
//      *Error / *NoMatch / noAction) and their render helpers.
//  The PROPOSAL / QUESTION strings (createConfirm, deleteConfirm, editConfirm, inquiry, …) are
//  dropped — the model writes those, in-language, and they never enter the repo.
// ============================================================================

// The skill's own extraction RULEBOOK. Carried VERBATIM into the merged router+extractor call as
// the rulebook of manifest.inputs (skill.js). The router does not read or reword it — it is
// opaque text to the orchestrator, which is what keeps the orchestrator generic. Carried whole,
// the merged call keeps a nameless guest on a terse order; a trimmed rulebook DROPS her.
export function buildExtractionRules(OWNER_NAME) {
  return `Choosing "action":
- "create": ${OWNER_NAME} wants to schedule/create a NEW meeting or event.
- "delete": ${OWNER_NAME} wants to cancel/delete/remove an EXISTING event. This
  almost always happens when the order is a REPLY to a message that contains a
  Google Calendar link. If the quoted message has a calendar link and the order
  asks to cancel/delete/remove — or is just an affirmative like "yes"/"confirm"
  right after a cancellation was proposed — choose "delete".
- "edit": ${OWNER_NAME} wants to CHANGE an EXISTING event — reschedule it (move to
  another time/date), change its length/duration, add or remove an attendee, or
  rename it. Like delete, this is almost always a REPLY to a message that contains a
  Google Calendar link, but the order asks to move/change/reschedule/rename/add/remove
  rather than call the whole event off. Choose "edit" (NOT "delete") whenever the event
  survives with a modification; choose "delete" only when the event is cancelled entirely.
- "list": ${OWNER_NAME} is ASKING what's on the calendar — a READ-ONLY query about
  existing events (e.g. "what's on my calendar tomorrow?", "do I have anything Friday
  afternoon?", "what's my next meeting?"). Nothing is created, changed, or cancelled.
  Choose "list" for any question that just READS the schedule.
- "other": none of the above.

For action="create", fill these (for action="delete", ALSO fill participants and start_iso — see below):
- title = the event's short calendar HEADING (a few words). PRIORITY:
  1. PREFER A MEANINGFUL TOPIC — what the event is ABOUT — inferred from the WHOLE
     conversation and the order, not only an explicitly stated name. "Budget 2026",
     "Q3 budget review", "Apartment viewing" are meaningful. A PARTICIPANT-SHAPED label is
     NOT a topic and must NOT be produced here: "Meeting with John", "Call with Ana" name
     WHO, not WHAT — they do not count as meaningful.
  2. ONLY if the conversation genuinely gives NO subject, set title=null. The code then
     falls back to the participants' names joined with "/", owner first (e.g. "Marcelo/John").
     You do NOT build that string — just leave title=null.
  Do NOT invent a subject the conversation doesn't support, and do NOT dress a participant
  list up as a fake topic.
- participants = ALL the people who will be in the meeting, BESIDES ${OWNER_NAME}. Use the context: if ${OWNER_NAME} talks to X about scheduling a meeting with Y, decide from context who will actually attend (it may be only Y, or X and Y). Include each person's name.
- For each participant, include the email if it appears in the conversation; otherwise email=null.
- You will receive the name of the contact ${OWNER_NAME} is currently talking to; use it when it makes sense.
- start_iso in ISO 8601 with the -03:00 offset; convert relative dates using the current date/time provided.
- duration_min = minutes if stated; otherwise null.
- all_day = true when the order says the event takes the WHOLE DAY ("o dia inteiro", "o dia todo", "all day") rather than starting at a time. STILL fill start_iso — with the FIRST day of the event at 00:00, -03:00 offset (the day is read from it); duration_min is ignored. If the order states a TIME ("amanhã 10h", "at 3pm"), all_day = false.
- all_day_end_iso: ONLY for an all-day RANGE spanning several days ("de segunda a quarta", "a semana toda", "Mon to Wed", "os dois dias"). Set it to the LAST day the event STILL COVERS, at 00:00 with the -03:00 offset — INCLUSIVE: for "segunda a quarta" it is WEDNESDAY, not Thursday. Do NOT add a day. For a single all-day event, and whenever all_day is false, set all_day_end_iso = null.
- "summary": a longer one-line agenda/description for the event BODY — distinct from the short title above; may be "" when there's nothing to add.
- recurrence = the repeat rule when the order asks for a REPEATING event ("every Monday",
  "toda segunda", "every 2 weeks", "a cada 2 semanas", "5 times", "5 vezes", "until August",
  "até agosto", "every morning", "daily", "on the 5th every month", "todo dia 5"). Otherwise
  recurrence = null — a ONE-OFF event, the default. NEVER invent a repeat from a single order.
  start_iso STAYS the FIRST occurrence. The object is {freq, interval, byday, count, until}:
  - freq: "daily" ("every day", "todo dia", "every morning", "daily"); "weekly" ("every Monday",
    "toda segunda", "every week"); "monthly" ("every month", "todo mês", "on the 5th every
    month"). Monthly repeats on start_iso's DAY-OF-MONTH. v1 has NO "first Monday of the month"
    and NO yearly — if the order needs either, set recurrence = null.
  - interval: the N in "every N days/weeks/months" ("every 2 weeks" -> 2; "a cada 2 semanas" ->
    2). null or 1 when not stated.
  - byday: WEEKLY only — the weekdays as ["MO","TU","WE","TH","FR","SA","SU"] ("every Mon & Wed"
    -> ["MO","WE"]; "toda segunda" -> ["MO"]). null for daily/monthly.
  - count: the number of occurrences ("5 times"/"5 vezes"/"for 3 sessions" -> 5/5/3); else null.
  - until: the END date ("until August"/"até 30 de ago"), ISO 8601 with the -03:00 offset,
    resolved from the current date/time; else null.
  If the order gives BOTH a count and an until, fill count and leave until null — a repeat has
  one or the other, never both.
- location = WHERE the meeting is, as a VERBATIM physical place — an address, venue or room
  exactly as ${OWNER_NAME} wrote it ("Rua Augusta 123", "Café Blue", "sala 4", "my office").
  Copy it word for word: NEVER invent, look up, complete, or reformat an address. null when no
  place is given.
- virtual = true when the order asks for a VIDEO CALL / Google Meet ("make it a video call",
  "chamada de vídeo", "por Meet", "online", "call/ligação"). Otherwise false.
- location and virtual are MUTUALLY EXCLUSIVE — a meeting is physical OR virtual, never both.
  If the order gives an address AND asks for a video call, set virtual=true and location=null
  (video wins). Give NEITHER a value it was not told.

To CHANGE or CANCEL an existing event, you FIRST need its id: dispatch action="find" (or
action="list") to READ the matching events back, then act on the one you mean with its event_id.
- action="find": set "query" to a short description of the event (its topic / who / when), and,
  when you know them, start_iso (the event's start) and participants (with any emails). It returns
  candidate events, each with an event_id.
- action="edit": set event_id to the target's id, and set ONLY the fields that CHANGE (a new
  start_iso, duration_min, title, summary, all_day/all_day_end_iso, or the full participants list).
  Leave the rest null. Set location/virtual only when the PLACE changes (a new address, or making
  it a video call); omit otherwise (absent = keep the event's current place).
- action="delete": set event_id to the target's id.

For action="list", resolve the time WINDOW the question implies and set list_mode:
- list_mode: "next" when ${OWNER_NAME} asks for the NEXT / soonest upcoming event without
  naming a day ("what's my next meeting?", "when's my next call?"). Use "window" for
  everything else (a named day, part of a day, or a range).
- range_start_iso / range_end_iso: the window the question implies, ISO 8601 with the
  -03:00 offset, converted from relative phrases using the current date/time. "tomorrow"
  → that whole day (00:00 to 23:59); "Friday afternoon" → that Friday 12:00–18:00; "this
  week" → the week's span. If NO time is expressed ("what's on my calendar?"), leave BOTH
  null with list_mode="window" (the code then defaults to the rest of today). For
  list_mode="next", leave BOTH null (the code scans forward from now).

For EVERY action other than "list", set list_mode=null, range_start_iso=null, and
range_end_iso=null.`;
}

// ============================================================================
//  USER-FACING REPLY STRINGS (localized).
//  Per-language render functions for the OUTCOME + error messages this skill sends, selected at
//  send time with ctx.lang via reply(). English is canonical; pt is maintained; any other
//  language is produced from the `en` copy by the orchestrator's send() translation fallback.
//  Keep BOTH en + pt for every new message. Interpolated dates arrive pre-formatted
//  (localizeWhen/localizeDate); list grammar and pluralization are done per language here.
// ============================================================================

const REPLY_TZ = "America/Sao_Paulo";

// Localized date/time for USER-FACING strings. Bare, zero-padded 24-hour time (HH:MM, no
// AM/PM) and a 3-letter month; the locale sets the day/month ORDER. São Paulo, no seconds.
export function localizeDate(lang, dateTime) {
  if (!dateTime) return lang === "pt" ? "(sem horário)" : "(no time)";
  const locale = lang === "pt" ? "pt-BR" : "en-US";
  return new Date(dateTime).toLocaleString(locale, {
    timeZone: REPLY_TZ,
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false, // bare 24-hour, no AM/PM
  });
}

// The WHEN-line of a create/edit draft, from the draft itself. Three shapes: timed, all-day
// single day, all-day range (with a day count). Both endpoints INCLUSIVE.
export function localizeWhen(lang, draft) {
  if (!draft?.all_day) return localizeDate(lang, draft?.start_iso);
  const startMs = Date.parse(draft.start_iso || "");
  if (!Number.isFinite(startMs)) return localizeDate(lang, null);
  const allDay = lang === "pt" ? "Dia todo" : "All day";
  const endMs = Date.parse(draft.all_day_end_iso || "");
  if (!Number.isFinite(endMs) || endMs <= startMs) {
    return `${localizeDay(lang, startMs)} · ${allDay}`;
  }
  const days = Math.round((endMs - startMs) / 86400000) + 1;
  const unit =
    lang === "pt" ? (days === 1 ? "dia" : "dias") : days === 1 ? "day" : "days";
  return `${localizeDay(lang, startMs)} – ${localizeDay(lang, endMs)} · ${allDay} (${days} ${unit})`;
}

// List grammar, per language. EN: "A", "A and B", "A, B, and C".
function joinListEn(items) {
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}
// PT: "A", "A e B", "A, B e C" (no Oxford comma; "e").
function joinListPt(items) {
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} e ${items[1]}`;
  return `${items.slice(0, -1).join(", ")} e ${items[items.length - 1]}`;
}

// ---- LIST (read-only) render helpers ----------------------------------------
// Time-only (bare, zero-padded 24-hour HH:MM, no AM/PM) in the reply TZ — used for event
// lines inside a single-day window.
export function localizeTime(lang, dateTime) {
  if (!dateTime) return "";
  return new Date(dateTime).toLocaleTimeString(lang === "pt" ? "pt-BR" : "en-US", {
    timeZone: REPLY_TZ,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

// Date-only (no time) in the reply TZ — window headers, empty-state, all-day event lines.
function localizeDay(lang, ms) {
  return new Date(ms).toLocaleDateString(lang === "pt" ? "pt-BR" : "en-US", {
    timeZone: REPLY_TZ,
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

// Short localized weekday labels for a recurrence phrase (module-private).
const WEEKDAY_SHORT = {
  en: { MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun" },
  pt: { MO: "seg", TU: "ter", WE: "qua", TH: "qui", FR: "sex", SA: "sáb", SU: "dom" },
};

// The done RECURRENCE line, localized. Assumes `rec` is COMPILABLE — the caller
// (recurrenceLineFor) gates on toRRule first. Returns a capitalized phrase.
export function describeRecurrence(rec, lang) {
  const pt = lang === "pt";
  const iv = Number(rec.interval) > 1 ? Number(rec.interval) : 1;
  const ORDER = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
  const map = WEEKDAY_SHORT[lang] || WEEKDAY_SHORT.en;
  const set = new Set((Array.isArray(rec.byday) ? rec.byday : []).map((d) => String(d).toUpperCase()));
  const days = ORDER.filter((d) => set.has(d)).map((d) => map[d]).join(", ");

  let core;
  if (rec.freq === "daily") {
    core = iv > 1 ? (pt ? `a cada ${iv} dias` : `every ${iv} days`) : pt ? "todo dia" : "every day";
  } else if (rec.freq === "monthly") {
    core = iv > 1 ? (pt ? `a cada ${iv} meses` : `every ${iv} months`) : pt ? "todo mês" : "every month";
  } else {
    // weekly
    const base = iv > 1 ? (pt ? `a cada ${iv} semanas` : `every ${iv} weeks`) : pt ? "toda semana" : "every week";
    core = days ? (pt ? `${base} às ${days}` : `${base} on ${days}`) : base;
  }

  let suffix = "";
  const count = Number(rec.count);
  if (Number.isFinite(count) && count > 0) {
    suffix = pt ? `, ${count} vezes` : `, ${count} times`;
  } else if (rec.until) {
    const ms = Date.parse(rec.until);
    if (Number.isFinite(ms)) {
      suffix = pt ? ` até ${localizeDay("pt", ms)}` : ` until ${localizeDay("en", ms)}`;
    }
  }

  const phrase = core + suffix;
  return phrase.charAt(0).toUpperCase() + phrase.slice(1);
}

// Do two instants fall on the same calendar day in the reply TZ? Used for the empty-state wording.
function sameLocalDay(aMs, bMs) {
  return localizeDay("en", aMs) === localizeDay("en", bMs);
}

// The instant used to place an event on a day (all-day events carry dayMs; timed ones their start).
function eventDayMs(ev) {
  return ev.allDay ? ev.dayMs : new Date(ev.startIso).getTime();
}

// One event rendered as its block: a "time - title" line, then (only if it has external
// attendees) their emails on the next line.
function eventBlock(lang, ev) {
  const title = ev.title || (lang === "pt" ? "(sem título)" : "(no title)");
  const time = ev.allDay ? (lang === "pt" ? "Dia todo" : "All day") : localizeTime(lang, ev.startIso);
  const head = `${time} - ${title}`;
  return ev.emails.length ? `${head}\n${ev.emails.join(", ")}` : head;
}

// Group start-sorted events into consecutive day buckets and render each as a header (the date)
// followed by its event blocks.
function renderDays(lang, events) {
  const days = [];
  for (const ev of events) {
    const key = localizeDay("en", eventDayMs(ev)); // locale-neutral grouping key
    let g = days[days.length - 1];
    if (!g || g.key !== key) {
      g = { key, ms: eventDayMs(ev), items: [] };
      days.push(g);
    }
    g.items.push(ev);
  }
  return days
    .map((g) => `${localizeDay(lang, g.ms)}\n${g.items.map((ev) => eventBlock(lang, ev)).join("\n\n")}`)
    .join("\n\n");
}

// The CONDITIONAL location line(s) for the done bubbles, per language. A physical event prints
// "📍 <verbatim address>"; a virtual one prints "📹 Google Meet (video call)" and, when a Meet
// link is already known (the create/edit response may still be provisioning it — edge #8), the
// join URL beneath it. No location -> "" (no bullet at all). Returns the bullet(s) WITH the
// leading "\n- " so callers append it inline, exactly like the recurrence line.
function locationLineEn({ location, virtual, meetLink }) {
  if (virtual) return `\n- 📹 Google Meet (video call)${meetLink ? `\n  ${meetLink}` : ""}`;
  if (location) return `\n- 📍 ${location}`;
  return "";
}
function locationLinePt({ location, virtual, meetLink }) {
  if (virtual) return `\n- 📹 Google Meet (chamada de vídeo)${meetLink ? `\n  ${meetLink}` : ""}`;
  if (location) return `\n- 📍 ${location}`;
  return "";
}

const REPLY = {
  en: {
    thinkingError: () => "I hit an error while thinking. Try again?",
    noAction: ({ summary }) =>
      `I didn't identify a calendar action. ${summary || ""}`.trim(),
    createDone: ({ reused, title, emails, when, duration, link, uninvited, recurrence, location, virtual, meetLink }) => {
      const guests = emails || "(no guests)";
      const without = uninvited?.length
        ? `\n\nI created it without inviting ${joinListEn(uninvited)} — I don't have their email.`
        : "";
      const rec = recurrence ? `\n- ${recurrence}` : "";
      const loc = locationLineEn({ location, virtual, meetLink });
      return `${
        reused
          ? "That event already exists — here it is (no duplicate created):"
          : "Done! Invite created and sent:"
      }\n\n- ${title}\n- ${guests}\n- ${when}${duration ? ` (${duration} min)` : ""}${loc}${rec}${without}\n\nHere is a link for the event:\n${link}`;
    },
    createGoogleError: () =>
      "I understood the request but failed to create it in Google. Error in the log.",
    deleteCheckError: () => "I hit an error checking the calendar. Try again?",
    deleteNoMatch: () =>
      "I couldn't find a matching event — it may already be cancelled, or I'm not sure which one you mean.",
    deleteCancelled: ({ title, removed }) => {
      const dupNote = removed > 1 ? ` (removed ${removed} copies)` : "";
      return `Cancelled "${title}"${dupNote} and notified the attendees.`;
    },
    deleteGoogleError: () =>
      "I found the event but failed to cancel it in Google. Error in the log.",
    editNoMatch: () =>
      "I couldn't find that event — it may have been cancelled. Try finding it again.",
    editCheckError: () => "I hit an error reading the calendar. Try again?",
    editNoChange: () =>
      "I couldn't tell what to change. Tell me the new time, duration, title, or which attendee to add/remove.",
    editDone: ({ title, when, duration, emails, link, location, virtual, meetLink }) => {
      const loc = locationLineEn({ location, virtual, meetLink });
      return `Done! Updated the event and notified the attendees:\n\n- ${title}\n- ${emails}\n- ${when}${duration ? ` (${duration} min)` : ""}${loc}\n\nHere is a link for the event:\n${link}`;
    },
    editGoogleError: () =>
      "I understood the change but failed to update it in Google. Error in the log.",
    listEvents: ({ startMs, endMs, events, capped }) => {
      if (!events.length) {
        return sameLocalDay(startMs, endMs)
          ? `Nothing on your calendar for ${localizeDay("en", startMs)}.`
          : `Nothing on your calendar between ${localizeDay("en", startMs)} and ${localizeDay("en", endMs)}.`;
      }
      const capNote = capped ? "\n\n(Showing the first 50.)" : "";
      return `${renderDays("en", events)}${capNote}`;
    },
    listNext: ({ event }) => {
      if (!event) return "Nothing coming up on your calendar in the next two weeks.";
      return `Your next event:\n${renderDays("en", [event])}`;
    },
    listError: () => "I hit an error reading the calendar. Try again?",
  },
  pt: {
    thinkingError: () => "Tive um erro ao processar. Pode tentar de novo?",
    noAction: ({ summary }) =>
      `Não identifiquei uma ação de calendário. ${summary || ""}`.trim(),
    createDone: ({ reused, title, emails, when, duration, link, uninvited, recurrence, location, virtual, meetLink }) => {
      const guests = emails || "(ninguém convidado)";
      const without = uninvited?.length
        ? `\n\nCriei sem convidar ${joinListPt(uninvited)} — não tenho o e-mail.`
        : "";
      const rec = recurrence ? `\n- ${recurrence}` : "";
      const loc = locationLinePt({ location, virtual, meetLink });
      return `${
        reused
          ? "Esse evento já existe — aqui está ele (nenhuma cópia criada):"
          : "Pronto! Convite criado e enviado:"
      }\n\n- ${title}\n- ${guests}\n- ${when}${duration ? ` (${duration} min)` : ""}${loc}${rec}${without}\n\nAqui está o link do evento:\n${link}`;
    },
    createGoogleError: () =>
      "Entendi o pedido, mas não consegui criar no Google. O erro está no log.",
    deleteCheckError: () =>
      "Tive um erro ao verificar o calendário. Pode tentar de novo?",
    deleteNoMatch: () =>
      "Não encontrei um evento correspondente — pode já ter sido cancelado, ou não tenho certeza de qual você quer dizer.",
    deleteCancelled: ({ title, removed }) => {
      const dupNote = removed > 1 ? ` (removi ${removed} cópias)` : "";
      return `Cancelado "${title}"${dupNote} e avisei os participantes.`;
    },
    deleteGoogleError: () =>
      "Encontrei o evento, mas não consegui cancelar no Google. O erro está no log.",
    editNoMatch: () =>
      "Não encontrei esse evento — pode ter sido cancelado. Tente localizá-lo de novo.",
    editCheckError: () =>
      "Tive um erro ao ler o calendário. Pode tentar de novo?",
    editNoChange: () =>
      "Não consegui entender o que mudar. Me diga o novo horário, a duração, o título, ou qual participante adicionar/remover.",
    editDone: ({ title, when, duration, emails, link, location, virtual, meetLink }) => {
      const loc = locationLinePt({ location, virtual, meetLink });
      return `Pronto! Atualizei o evento e avisei os participantes:\n\n- ${title}\n- ${emails}\n- ${when}${duration ? ` (${duration} min)` : ""}${loc}\n\nAqui está o link do evento:\n${link}`;
    },
    editGoogleError: () =>
      "Entendi a mudança, mas não consegui atualizar no Google. O erro está no log.",
    listEvents: ({ startMs, endMs, events, capped }) => {
      if (!events.length) {
        return sameLocalDay(startMs, endMs)
          ? `Nada na sua agenda para ${localizeDay("pt", startMs)}.`
          : `Nada na sua agenda entre ${localizeDay("pt", startMs)} e ${localizeDay("pt", endMs)}.`;
      }
      const capNote = capped ? "\n\n(Mostrando os primeiros 50.)" : "";
      return `${renderDays("pt", events)}${capNote}`;
    },
    listNext: ({ event }) => {
      if (!event) return "Nada na sua agenda nas próximas duas semanas.";
      return `Seu próximo evento:\n${renderDays("pt", [event])}`;
    },
    listError: () => "Tive um erro ao ler o calendário. Pode tentar de novo?",
  },
};

// Pick the reply set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
