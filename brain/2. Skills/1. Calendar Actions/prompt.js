// ============================================================================
//  Skill "Calendar Actions" — PROMPT.
//  Edit this file to change how the assistant interprets calendar orders.
//  Prompt text/rules only — no logic.
//
//  The output JSON must keep matching what skill.js expects.
// ============================================================================

// ---- JSON Schemas for structured outputs (output_config.format) --------------
// Single source of truth for the SHAPE of each reply. skill.js passes these to
// messages.create so the API returns ONLY schema-valid JSON — the prompts below
// describe what each field MEANS, the schema enforces its type/enum/shape.
// Structured-outputs rules: every object needs additionalProperties:false + a
// full `required` list; optional fields use a nullable type union.
const PARTICIPANT = {
  type: "object",
  additionalProperties: false,
  required: ["name", "email"],
  properties: {
    name: { type: ["string", "null"] },
    email: { type: ["string", "null"] },
  },
};

export const CAL_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["action", "title", "participants", "start_iso", "duration_min", "summary"],
  properties: {
    action: { type: "string", enum: ["create", "delete", "edit", "other"] },
    title: { type: ["string", "null"] },
    participants: { type: "array", items: PARTICIPANT },
    start_iso: { type: ["string", "null"] },
    duration_min: { type: ["number", "null"] },
    summary: { type: "string" },
  },
};

export const CONFIRM_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["decision"],
  properties: {
    decision: { type: "string", enum: ["confirm", "decline", "unrelated"] },
  },
};

export const REVIEW_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["decision", "title", "participants", "start_iso", "duration_min", "summary"],
  properties: {
    decision: { type: "string", enum: ["confirm", "modify", "cancel", "unrelated"] },
    title: { type: ["string", "null"] },
    participants: { type: "array", items: PARTICIPANT },
    start_iso: { type: ["string", "null"] },
    duration_min: { type: ["number", "null"] },
    summary: { type: "string" },
  },
};

export const RESOLVE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["start_iso", "participants"],
  properties: {
    start_iso: { type: ["string", "null"] },
    participants: {
      anyOf: [{ type: "null" }, { type: "array", items: PARTICIPANT }],
    },
  },
};

// The focused EDIT pass (Phase B). Given the current event and the owner's change
// request, it returns ONLY the fields that change (null / empty when untouched) —
// or, if the request is ambiguous or missing a needed detail, a short `clarify`
// question with every change left null. Emails to add/remove are plain arrays.
export const EDIT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: [
    "new_start_iso",
    "new_duration_min",
    "new_title",
    "new_summary",
    "add_emails",
    "remove_emails",
    "clarify",
  ],
  properties: {
    new_start_iso: { type: ["string", "null"] },
    new_duration_min: { type: ["number", "null"] },
    new_title: { type: ["string", "null"] },
    new_summary: { type: ["string", "null"] },
    add_emails: { type: "array", items: { type: "string" } },
    remove_emails: { type: "array", items: { type: "string" } },
    clarify: { type: ["string", "null"] },
  },
};

export function buildSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. Read the conversation, the order, and any replied-to (quoted) message, then decide the calendar ACTION and extract its data. (Your reply's shape is enforced separately — here, focus on getting the values right.)

Choosing "action":
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
- "other": none of the above.

For action="create", fill these (for action="delete", ALSO fill participants and start_iso — see below):
- title = the event's short calendar HEADING (a few words), INFERRED from what the meeting is ABOUT using the WHOLE conversation and the order — not only an explicitly stated name. E.g. a clearly-budget chat → "Q3 budget review"; a 1:1 catch-up → "Catch-up". If nothing indicates a subject, set title=null (a name-based fallback is used instead). Do NOT invent a subject the conversation doesn't support.
- participants = ALL the people who will be in the meeting, BESIDES ${OWNER_NAME}. Use the context: if ${OWNER_NAME} talks to X about scheduling a meeting with Y, decide from context who will actually attend (it may be only Y, or X and Y). Include each person's name.
- For each participant, include the email if it appears in the conversation; otherwise email=null.
- You will receive the name of the contact ${OWNER_NAME} is currently talking to; use it when it makes sense.
- start_iso in ISO 8601 with the -03:00 offset; convert relative dates using the current date/time provided.
- duration_min = minutes if stated; otherwise null.
- "summary": a longer one-line agenda/description for the event BODY — distinct from the short title above; may be "" when there's nothing to add.

For action="delete", also identify WHICH event to cancel so it can be matched on the calendar (the decoded link is only one signal):
- participants: the people the event is WITH — read them (and their emails) from the quoted invite message and the conversation. Include emails whenever they appear (the invite text usually lists them).
- start_iso: the event's date/time, taken from the quoted invite or the conversation, in ISO 8601 with -03:00.
- Still fill "summary" with a short note of what is being cancelled.

For action="edit", you only need to CLASSIFY it here — the specific change (new time,
new duration, renamed title, added/removed attendee) is extracted in a following focused
step from the real event. Fill the other fields with your best effort or leave them
null/empty; they are not used for edits.`;
}

// ---- Continuation: judge whether a message answers a pending confirmation ----
// Used while a session is open. The brain sees EVERY message from the awaited
// party and must ignore normal chatter, acting only on a real yes/no.
export function buildConfirmSystem(action) {
  return `You decide whether the LATEST message is a response to a pending confirmation.
The assistant asked to confirm: ${action}.
Use the recent conversation only as context; judge ONLY the latest message.
Decide one "decision" value — "confirm", "decline", or "unrelated":
- "confirm": the latest message clearly agrees to proceed (e.g. yes, confirm, go ahead, sim, pode, isso).
- "decline": the latest message clearly refuses (e.g. no, don't, keep it, não, deixa).
- "unrelated": the latest message is normal conversation, NOT a reply to this confirmation. If unsure, choose "unrelated".`;
}

export function buildConfirmUser({ transcript, latest }) {
  return `Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// ---- Continuation: review a pending CREATE (confirm / modify / cancel) --------
// After proposing an event, the brain shows a draft and asks the owner to confirm.
// This runs for EVERY owner message while that session is open: it BOTH classifies
// the reply AND, when the owner asks for a change, returns the full updated draft
// (one call keeps the correlated fields consistent — same reasoning as create).
export function buildCreateReviewSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. You already PROPOSED an event and asked ${OWNER_NAME} to confirm it. Read the current DRAFT, the recent conversation, and ${OWNER_NAME}'s LATEST message, then decide what that latest message means for the pending event.
Choose the "decision" and, for a modify, return the updated draft fields (title, participants, start_iso, duration_min, summary):

- "confirm": the latest message clearly approves the event as proposed (e.g. yes, confirm, go ahead, send it, sim, pode, isso).
- "modify": the latest message asks to CHANGE something (time, date, title, duration, attendees, emails, agenda). Return the FULL updated draft with the change applied, carrying over EVERY unchanged field from the current draft exactly.
- "cancel": the latest message calls the whole thing off (e.g. no, forget it, cancel, deixa, esquece).
- "unrelated": normal conversation, NOT a response to this confirmation. If unsure, choose "unrelated".

For "modify", apply the change on top of the current draft:
- keep ISO 8601 with the -03:00 offset for start_iso; convert relative times using the current date/time provided;
- when adding/removing an attendee, keep the others; each participant is {name, email|null};
- change ONLY what the latest message asks to change; echo everything else from the draft.
For any decision other than "modify", the draft fields are ignored — you may echo the current draft.`;
}

export function buildCreateReviewUser({ draftJson, transcript, latest, nowStr }) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Current DRAFT of the pending event:
${draftJson}

Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// ---- Targeted resolver: find ONLY the fields still missing --------------------
// Every create is stateful. After the broad extraction, if a REQUIRED field is
// missing we make a FOCUSED second pass that looks precisely for those fields — in
// the conversation and the latest message — BEFORE asking a human. Re-run on each
// incoming message while gathering. Higher resolution than the broad pass because
// it is told exactly what to look for. The latest message may come from the owner
// OR from an attendee (awaitFrom:"any").
export function buildResolveSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. An event is being prepared and some REQUIRED details are still missing. You are told exactly WHICH details are missing. Inspect the current draft, the recent conversation, and the LATEST message, and resolve PRECISELY those missing details — nothing else. Return the resolved start_iso and/or the full participants list:

- Resolve ONLY the items marked MISSING in "Still missing" below; leave everything else null.
- start_iso: ISO 8601 with the -03:00 offset; resolve relative times ("tomorrow 3pm", "next Tuesday") using the current date/time given. null if genuinely not stated anywhere.
- participants: when attendees or emails are missing, return the FULL attendee list (everyone besides ${OWNER_NAME}), carrying over the people and emails already in the draft and adding any name or email you can now determine. Each is {name, email|null}. Return null if you cannot improve the current list.
- The latest message may come from ${OWNER_NAME} OR from an attendee. If an attendee gives their OWN email ("it's ana@x.com", "sou eu, ana@x.com"), attach it to them. If EXACTLY ONE person still needs an email and the latest message has a single bare email, attach it to that person.
- NEVER invent an email or a time. If it is not clearly present, leave it null — a human will be asked.`;
}

export function buildResolveUser({
  draftJson,
  needsTime,
  needsAttendees,
  needEmailFor,
  transcript,
  latest,
  nowStr,
}) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Current DRAFT: ${draftJson}

Still missing (resolve ONLY these):
- start_iso (event date/time): ${needsTime ? "MISSING" : "already set"}
- participants — at least one attendee besides the owner: ${needsAttendees ? "MISSING" : "already set"}
- email address for these attendees: ${needEmailFor && needEmailFor.length ? needEmailFor.join(", ") : "(none missing)"}

Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// ---- Phase B: focused EDIT resolver -----------------------------------------
// The event to edit is already identified (from the replied-to calendar link) and
// its CURRENT state is given. This pass reads the owner's change request and returns
// ONLY what changes — or a `clarify` question when the request is ambiguous or missing
// a needed detail (e.g. "move it earlier" without saying to when). One call keeps the
// correlated fields (time + duration) consistent, same reasoning as create/review.
export function buildEditSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. ${OWNER_NAME} wants to CHANGE an existing event. You are given the event's CURRENT state, the recent conversation, and ${OWNER_NAME}'s latest change request. Return ONLY the fields that should change; leave everything else null or empty.

- new_start_iso: the event's NEW start, ISO 8601 with the -03:00 offset, resolving relative times ("4pm", "tomorrow", "move it 30 min later") against the current date/time and the event's current start. null if the time/date is NOT changing.
- new_duration_min: the NEW length in minutes if the request changes it ("make it 30 min", "an hour instead"). null if the duration is NOT changing. Changing only the start does NOT change the duration.
- new_title: the NEW short calendar heading if the request renames it. null otherwise.
- new_summary: a NEW one-line agenda/description if the request changes it. null otherwise.
- add_emails: array of email addresses to ADD as attendees (["carlos@x.com"]). Empty array if none. Only include addresses that actually appear in the request/conversation — NEVER invent one.
- remove_emails: array of email addresses to REMOVE from the attendees. Empty array if none.
- clarify: if the request is AMBIGUOUS or missing a detail you need (e.g. "move it earlier"/"push it back" with no target time, or "add João" with no email on record), set this to a SHORT question asking for exactly that, and leave every change field null/empty. Otherwise clarify=null.

Rules: change ONLY what the latest request asks; never guess a time or an email; if you cannot resolve a needed value, ask via clarify instead of guessing.`;
}

export function buildEditUser({ eventJson, transcript, latest, nowStr }) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Current EVENT being edited:
${eventJson}

Recent conversation:
${transcript || "(none)"}

Change request: ${latest}`;
}

// Builds the "user" message sent along with the system prompt.
export function buildUserPrompt(
  OWNER_NAME,
  { order, transcript, nowStr, contact, quoted }
) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Contact of this conversation: ${contact || "(yourself)"}
Replied-to (quoted) message: ${quoted?.text || "(none)"}
Quoted message contains a Google Calendar link: ${quoted?.calendarLink ? "YES" : "NO"}
Recent conversation:
${transcript || "(no history)"}

${OWNER_NAME}'s order: ${order}`;
}

// ============================================================================
//  USER-FACING REPLY STRINGS (localized).
//  Per-language render functions for EVERY message this skill sends, selected at
//  send time with ctx.lang via reply(). English is canonical; pt is maintained;
//  any other language is produced from the `en` copy by the orchestrator's send()
//  translation fallback. Keep BOTH en + pt for every new message. Interpolated
//  dates arrive pre-formatted (localizeDate); list grammar and pluralization are
//  done per language here — never share an English list-builder across languages.
// ============================================================================

const REPLY_TZ = "America/Sao_Paulo";

// Localized date/time for USER-FACING strings. Always hh:mm AM/PM and a 3-letter
// month; the locale sets the day/month ORDER — en-US "Jul 5, 2026, 2:30 PM"
// (month-day), pt-BR "5 de jul. de 2026, 2:30 PM" (day-month). São Paulo, no
// seconds. (The LLM-facing nowStr in server.js stays en.)
export function localizeDate(lang, dateTime) {
  if (!dateTime) return lang === "pt" ? "(sem horário)" : "(no time)";
  const locale = lang === "pt" ? "pt-BR" : "en-US";
  return new Date(dateTime).toLocaleString(locale, {
    timeZone: REPLY_TZ,
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true, // always AM/PM
  });
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

const REPLY = {
  en: {
    thinkingError: () => "I hit an error while thinking. Try again?",
    noAction: ({ summary }) =>
      `I didn't identify a calendar action. ${summary || ""}`.trim(),
    createConfirm: ({ title, emails, when, duration }) =>
      `Confirm this event:
- ${title}
- ${emails}
- ${when} (${duration} min)

Reply "yes" to confirm and I'll send the invites, or tell me what to change and I'll adjust.`,
    createDone: ({ reused, title, emails, when, duration, link }) =>
      `${
        reused
          ? "That event already exists — here it is (no duplicate created):"
          : "Done! Invite created and sent:"
      }\n\n- ${title}\n- ${emails}\n- ${when} (${duration} min)\n\nHere is a link for the event:\n${link}`,
    createCancelled: ({ title }) => `Okay, I won't create "${title}".`,
    createGoogleError: () =>
      "I understood the request but failed to create it in Google. Error in the log.",
    inquiry: (m) => {
      if (!m.noTime && !m.noAttendees && m.emailNames.length === 1) {
        return `${m.emailNames[0]}, I'm missing your email. Can you send it so I can add you to the invite?`;
      }
      const asks = [];
      if (m.noTime) asks.push("the date and time");
      if (m.noAttendees) asks.push("who to invite");
      if (m.emailNames.length === 1) asks.push(`${m.emailNames[0]}'s email`);
      else if (m.emailNames.length > 1)
        asks.push(`emails for ${joinListEn(m.emailNames)}`);
      return `Before I can set this up, I still need ${joinListEn(
        asks
      )}. Send it here and I'll continue.`;
    },
    deleteNeedSignal: ({ tag }) =>
      `To cancel an event, reply to its invite message, or tell me which meeting (who and when) and call ${tag} again.`,
    deleteCheckError: () => "I hit an error checking the calendar. Try again?",
    deleteNoMatch: () =>
      "I couldn't find a matching event — it may already be cancelled, or I'm not sure which one you mean. Reply to its invite message and try again.",
    deleteConfirm: ({ title, when, count }) => {
      const countNote = count > 1 ? `\n- (${count} matching copies)` : "";
      return `Confirm the cancelation of this event?\n- ${title}\n- ${when}${countNote}\n\nReply "yes" to confirm, or "no" to keep it.`;
    },
    deleteKeep: ({ title }) => `Okay, I'll keep "${title}".`,
    deleteCancelled: ({ title, removed }) => {
      const dupNote = removed > 1 ? ` (removed ${removed} copies)` : "";
      return `Cancelled "${title}"${dupNote} and notified the attendees.`;
    },
    deleteGoogleError: () =>
      "I found the event but failed to cancel it in Google. Error in the log.",
    editNeedSignal: ({ tag }) =>
      `To change an event, reply to its invite message with the change (e.g. "move it to 4pm") and call ${tag}.`,
    editNoMatch: () =>
      "I couldn't find that event — it may have been cancelled, or the invite link didn't resolve. Reply to its invite message and try again.",
    editCheckError: () => "I hit an error reading the calendar. Try again?",
    editClarify: (question) => question,
    editNoChange: () =>
      "I couldn't tell what to change. Tell me the new time, duration, title, or which attendee to add/remove.",
    editDone: ({ title, when, duration, emails, link }) =>
      `Done! Updated the event and notified the attendees:\n\n- ${title}\n- ${emails}\n- ${when} (${duration} min)\n\nHere is a link for the event:\n${link}`,
    editGoogleError: () =>
      "I understood the change but failed to update it in Google. Error in the log.",
  },
  pt: {
    thinkingError: () => "Tive um erro ao processar. Pode tentar de novo?",
    noAction: ({ summary }) =>
      `Não identifiquei uma ação de calendário. ${summary || ""}`.trim(),
    createConfirm: ({ title, emails, when, duration }) =>
      `Confirme este evento:
- ${title}
- ${emails}
- ${when} (${duration} min)

Responda "sim" para confirmar e eu envio os convites, ou me diga o que mudar que eu ajusto.`,
    createDone: ({ reused, title, emails, when, duration, link }) =>
      `${
        reused
          ? "Esse evento já existe — aqui está ele (nenhuma cópia criada):"
          : "Pronto! Convite criado e enviado:"
      }\n\n- ${title}\n- ${emails}\n- ${when} (${duration} min)\n\nAqui está o link do evento:\n${link}`,
    createCancelled: ({ title }) => `Ok, não vou criar "${title}".`,
    createGoogleError: () =>
      "Entendi o pedido, mas não consegui criar no Google. O erro está no log.",
    inquiry: (m) => {
      if (!m.noTime && !m.noAttendees && m.emailNames.length === 1) {
        return `${m.emailNames[0]}, estou sem o seu e-mail. Pode me enviar para eu te incluir no convite?`;
      }
      const asks = [];
      if (m.noTime) asks.push("a data e o horário");
      if (m.noAttendees) asks.push("quem convidar");
      if (m.emailNames.length === 1) asks.push(`o e-mail de ${m.emailNames[0]}`);
      else if (m.emailNames.length > 1)
        asks.push(`os e-mails de ${joinListPt(m.emailNames)}`);
      return `Antes de agendar, ainda preciso do seguinte: ${joinListPt(
        asks
      )}. Envie aqui que eu continuo.`;
    },
    deleteNeedSignal: ({ tag }) =>
      `Para cancelar um evento, responda à mensagem do convite, ou me diga qual reunião (quem e quando) e chame ${tag} de novo.`,
    deleteCheckError: () =>
      "Tive um erro ao verificar o calendário. Pode tentar de novo?",
    deleteNoMatch: () =>
      "Não encontrei um evento correspondente — pode já ter sido cancelado, ou não tenho certeza de qual você quer dizer. Responda à mensagem do convite e tente de novo.",
    deleteConfirm: ({ title, when, count }) => {
      const countNote = count > 1 ? `\n- (${count} cópias correspondentes)` : "";
      return `Confirmar o cancelamento deste evento?\n- ${title}\n- ${when}${countNote}\n\nResponda "sim" para confirmar, ou "não" para manter.`;
    },
    deleteKeep: ({ title }) => `Ok, vou manter "${title}".`,
    deleteCancelled: ({ title, removed }) => {
      const dupNote = removed > 1 ? ` (removi ${removed} cópias)` : "";
      return `Cancelado "${title}"${dupNote} e avisei os participantes.`;
    },
    deleteGoogleError: () =>
      "Encontrei o evento, mas não consegui cancelar no Google. O erro está no log.",
    editNeedSignal: ({ tag }) =>
      `Para alterar um evento, responda à mensagem do convite com a mudança (ex.: "muda para 16h") e chame ${tag}.`,
    editNoMatch: () =>
      "Não encontrei esse evento — pode ter sido cancelado, ou o link do convite não resolveu. Responda à mensagem do convite e tente de novo.",
    editCheckError: () =>
      "Tive um erro ao ler o calendário. Pode tentar de novo?",
    editClarify: (question) => question,
    editNoChange: () =>
      "Não consegui entender o que mudar. Me diga o novo horário, a duração, o título, ou qual participante adicionar/remover.",
    editDone: ({ title, when, duration, emails, link }) =>
      `Pronto! Atualizei o evento e avisei os participantes:\n\n- ${title}\n- ${emails}\n- ${when} (${duration} min)\n\nAqui está o link do evento:\n${link}`,
    editGoogleError: () =>
      "Entendi a mudança, mas não consegui atualizar no Google. O erro está no log.",
  },
};

// Pick the reply set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
