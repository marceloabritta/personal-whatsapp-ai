// ============================================================================
//  Skill "Tasks" — PROMPT + user-facing STRINGS.
//  Prompt text/rules and localized reply strings only — no logic (that's skill.js).
//  The output JSON must keep matching what skill.js expects.
//
//  LOCALIZATION: user-facing strings are a per-language map, selected at send time
//  with ctx.lang via reply(). English is canonical; pt is maintained; any other
//  language is produced from the `en` copy by the orchestrator's send() translation
//  fallback. Add BOTH en + pt for every new message. Internal/classification prompts
//  (the buildSystem/... below) stay English.
// ============================================================================

// ---- JSON Schemas for structured outputs (output_config.format) --------------
// Single source of truth for the SHAPE of each reply. skill.js passes these to
// messages.create so the API returns ONLY schema-valid JSON. Every object needs
// additionalProperties:false + a full `required` list; optional fields use a
// nullable type union.

// The main interpret: classify the action + extract its data.
export const TASK_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["action", "title", "due_iso", "task_ref", "assignee"],
  properties: {
    action: { type: "string", enum: ["add", "list", "complete", "other"] },
    // For add: the task text. null when not an add (or nothing to capture).
    title: { type: ["string", "null"] },
    // For add: due date in ISO 8601 with the -03:00 offset; null if none.
    due_iso: { type: ["string", "null"] },
    // For complete: the owner's free-text pointer to which task ("the flight one").
    task_ref: { type: ["string", "null"] },
    // WHO the todo is for. null / the owner => a private self-todo (Google Tasks).
    // A third party (name and/or email) => the skill turns it into a calendar invite.
    assignee: {
      anyOf: [
        { type: "null" },
        {
          type: "object",
          additionalProperties: false,
          required: ["name", "email"],
          properties: {
            name: { type: ["string", "null"] },
            email: { type: ["string", "null"] },
          },
        },
      ],
    },
  },
};

// resolveTaskRef: pick which open task a free-text reference means.
export const RESOLVE_REF_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["match_index"],
  properties: {
    // 1-based index into the numbered open-task list, or null if not confident.
    match_index: { type: ["number", "null"] },
  },
};

// reviewAdd: while the just-added task's amend window is open, decide what the
// latest message means and return the updated fields for an "amend".
export const REVIEW_ADD_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["decision", "title", "due_iso"],
  properties: {
    decision: { type: "string", enum: ["amend", "keep", "delete", "unrelated"] },
    title: { type: ["string", "null"] },
    due_iso: { type: ["string", "null"] },
  },
};

// classifyConfirmation: does the latest message confirm/decline a pending action?
export const CONFIRM_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["decision"],
  properties: {
    decision: { type: "string", enum: ["confirm", "decline", "unrelated"] },
  },
};

// ---- Interpret ---------------------------------------------------------------
export function buildSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s to-do assistant. Read the conversation, the order, and any replied-to (quoted) message, then decide the task ACTION and extract its data. (Your reply's shape is enforced separately — here, focus on getting the values right.)

Choosing "action":
- "add": ${OWNER_NAME} wants to capture a NEW to-do/task (for themselves OR for someone else). Includes "turn this into a task" replying to a message.
- "list": ${OWNER_NAME} wants to hear their open tasks ("what's on my list?", "my to-dos").
- "complete": ${OWNER_NAME} wants to mark a task done / check it off.
- "other": none of the above.

Fields:
- title = the task text, a short imperative line (e.g. "Buy flight to SP"). For a reply that says "turn this into a task", use the QUOTED message's text as the title. Keep the user's own wording; do NOT translate it. null when there's nothing to capture.
- due_iso = the due date in ISO 8601 with the -03:00 offset, converting relative dates ("Friday", "tomorrow") using the current date/time provided. null if no due date is stated. (Google Tasks due is date-only; a time, if any, is ignored.)
- task_ref = ONLY for "complete": ${OWNER_NAME}'s free-text description of which task to finish ("the flight one", "the contract task"). Do NOT guess an id. null otherwise.
- assignee = WHO the to-do is FOR:
  - If it's ${OWNER_NAME}'s own to-do (the default, "add to MY list", no other person named as the doer), set assignee = null.
  - If ${OWNER_NAME} is assigning the task to ANOTHER person ("remind João to…", "task for Ana…", "ask Bob to…"), set assignee = { name, email } for that person (email from the conversation if present, else null). This is the person who must DO the task — not merely someone mentioned.`;
}

export function buildUserPrompt(
  OWNER_NAME,
  { order, transcript, nowStr, contact, quoted }
) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Contact of this conversation: ${contact || "(yourself)"}
Replied-to (quoted) message: ${quoted?.text || "(none)"}
Recent conversation:
${transcript || "(no history)"}

${OWNER_NAME}'s order: ${order}`;
}

// ---- Resolve which task to complete ------------------------------------------
export function buildResolveRefSystem(OWNER_NAME) {
  return `You match ${OWNER_NAME}'s free-text reference to ONE task in a numbered list of their open tasks. Return "match_index" = the 1-based number of the task they mean, or null if you are not confident which one (better to ask than to complete the wrong task). Judge by meaning, not exact words.`;
}

export function buildResolveRefUser({ taskRef, listText }) {
  return `Open tasks:
${listText || "(none)"}

Which one does this refer to? Reference: "${taskRef || ""}"`;
}

// ---- Amend window: review a just-added task ----------------------------------
// The task was already CREATED; a short session stays open so a follow-up can
// correct it. Runs for EVERY owner message while open — ignore normal chatter.
export function buildReviewAddSystem(OWNER_NAME) {
  return `${OWNER_NAME} just added a to-do and you are listening for a quick correction to it. Read the current TASK, the recent conversation, and ${OWNER_NAME}'s LATEST message, then decide what the latest message means for that task.

Choose "decision":
- "amend": the latest message changes the task's text or its due date. Return the updated "title" (the full new text) and/or "due_iso" (ISO 8601, -03:00, resolving relative dates with the current date/time). Echo the current value for whatever didn't change; keep the user's wording, don't translate.
- "delete": the latest message calls it off ("actually cancel that", "nevermind", "remove it", "deixa", "cancela").
- "keep": the latest message accepts it as-is / signals done ("ok", "that's it", "perfect", "pode deixar", "isso").
- "unrelated": normal conversation, NOT about this task. If unsure, choose "unrelated".

For any decision other than "amend", title/due_iso are ignored — you may echo the current task.`;
}

export function buildReviewAddUser({ taskJson, transcript, latest, nowStr }) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Current TASK: ${taskJson}

Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// ---- Confirmation classifier (mark-done "yes"/"no") --------------------------
export function buildConfirmSystem(action) {
  return `You decide whether the LATEST message is a response to a pending confirmation.
The assistant asked to confirm: ${action}.
Use the recent conversation only as context; judge ONLY the latest message.
Decide one "decision" value — "confirm", "decline", or "unrelated":
- "confirm": the latest message clearly agrees to proceed (e.g. yes, confirm, go ahead, sim, pode, isso).
- "decline": the latest message clearly refuses (e.g. no, don't, leave it, não, deixa).
- "unrelated": the latest message is normal conversation, NOT a reply to this confirmation. If unsure, choose "unrelated".`;
}

export function buildConfirmUser({ transcript, latest }) {
  return `Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// ============================================================================
//  USER-FACING REPLY STRINGS (localized).
//  Per-language render functions for EVERY message this skill sends, selected at
//  send time with ctx.lang via reply(). Keep BOTH en + pt. Dates arrive
//  pre-formatted (localizeDueDate). Task TITLES pass through verbatim.
// ============================================================================

const DUE_TZ = "America/Sao_Paulo";

// Localized DATE-ONLY string for a task due, rendered as "dd/mmm" (e.g. 17/jul).
// The month abbreviation follows ctx.lang (en "jul"/"may", pt "jul"/"mai"). Google
// Tasks stores due at UTC midnight, so we render in UTC to show the same calendar
// date that was stored; a fresh add passes the same normalized value, so display
// always matches storage.
export function localizeDueDate(lang, iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  const locale = lang === "pt" ? "pt-BR" : "en-US";
  const day = new Intl.DateTimeFormat(locale, {
    timeZone: "UTC",
    day: "2-digit",
  }).format(d);
  const mon = new Intl.DateTimeFormat(locale, {
    timeZone: "UTC",
    month: "short",
  })
    .format(d)
    .replace(/\.$/, "") // pt-BR yields "jul." — drop the trailing period
    .toLowerCase();
  return `${day}/${mon}`;
}

// A -03:00 date string for a task assigned to someone else: the due DATE (São
// Paulo) at 15:00, or — with no due — today (tomorrow if it's already past 15:00).
export function threePmOnDue(iso) {
  const ymd = (d) =>
    new Intl.DateTimeFormat("en-CA", {
      timeZone: DUE_TZ,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(d);
  let dateStr = null;
  if (iso) {
    const d = new Date(iso);
    if (!isNaN(d.getTime())) dateStr = ymd(d);
  }
  if (!dateStr) {
    const now = new Date();
    const hour = Number(
      new Intl.DateTimeFormat("en-US", {
        timeZone: DUE_TZ,
        hour: "2-digit",
        hour12: false,
      }).format(now)
    );
    dateStr = ymd(hour >= 15 ? new Date(now.getTime() + 86400000) : now);
  }
  return `${dateStr}T15:00:00-03:00`;
}

// One task line: "dd/mmm - title" when it has a due date, else just the title.
// Language-agnostic (the date is pre-localized by localizeDueDate).
function taskLine(r) {
  return r.when ? `${r.when} - ${r.title}` : r.title;
}
function renderList(header, rows) {
  return [header, ...rows.map(taskLine)].join("\n");
}

const REPLY = {
  en: {
    thinkingError: () => "I hit an error while thinking. Try again?",
    noAction: () =>
      "I didn't identify a task action (add, list, or complete).",
    needTitle: () => "What should the task say? Send me the task text.",
    added: ({ title, when }) =>
      `Added to your list:\n${taskLine({ title, when })}\n\nTell me if you need something to change, if not we are good.`,
    updated: ({ title, when }) =>
      `Updated your list:\n${taskLine({ title, when })}\n\nTell me if you need something to change, if not we are good.`,
    removed: ({ title }) => `Removed "${title}" from your list.`,
    empty: () => "Your list is empty — nothing open.",
    formatList: (rows) => renderList("Here are your open tasks:", rows),
    notFound: () => "I couldn't tell which task you mean. Which one?",
    confirmComplete: ({ title }) =>
      `Mark this done?\n- ${title}\n\nReply "yes" to confirm.`,
    completed: ({ title }) => `Done — checked off "${title}".`,
    keptOpen: ({ title }) => `Okay, leaving "${title}" open.`,
    calendarUnavailable: () =>
      "A task for someone else goes out as a calendar invite, but the calendar isn't available right now.",
    failed: () =>
      "Something went wrong with your tasks. Error in the log. Try again?",
  },
  pt: {
    thinkingError: () => "Tive um erro ao processar. Pode tentar de novo?",
    noAction: () =>
      "Não identifiquei uma ação de tarefa (adicionar, listar ou concluir).",
    needTitle: () => "O que a tarefa deve dizer? Me envie o texto da tarefa.",
    added: ({ title, when }) =>
      `Adicionei à sua lista:\n${taskLine({ title, when })}\n\nMe diga se precisa mudar algo, senão está tudo certo.`,
    updated: ({ title, when }) =>
      `Atualizei sua lista:\n${taskLine({ title, when })}\n\nMe diga se precisa mudar algo, senão está tudo certo.`,
    removed: ({ title }) => `Removi "${title}" da sua lista.`,
    empty: () => "Sua lista está vazia — nada em aberto.",
    formatList: (rows) => renderList("Aqui estão suas tarefas em aberto:", rows),
    notFound: () => "Não consegui identificar qual tarefa. Qual delas?",
    confirmComplete: ({ title }) =>
      `Marcar como concluída?\n- ${title}\n\nResponda "sim" para confirmar.`,
    completed: ({ title }) => `Pronto — concluí "${title}".`,
    keptOpen: ({ title }) => `Ok, vou deixar "${title}" em aberto.`,
    calendarUnavailable: () =>
      "Uma tarefa para outra pessoa vai como convite de calendário, mas o calendário não está disponível agora.",
    failed: () =>
      "Algo deu errado com suas tarefas. O erro está no log. Pode tentar de novo?",
  },
};

// Pick the reply set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
