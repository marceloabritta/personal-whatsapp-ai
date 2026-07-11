// ============================================================================
//  Skill "Tasks" — PROMPT + user-facing STRINGS.
//  Prompt text/rules and localized reply strings only — no logic (that's skill.js).
//  The output JSON must keep matching what skill.js expects.
//
//  LOCALIZATION: user-facing strings are a per-language map, selected at send time
//  with ctx.lang via reply(). English is canonical; pt is maintained; any other
//  language is produced from the `en` copy by the orchestrator's send() translation
//  fallback. Add BOTH en + pt for every new message. Internal/classification prompts
//  (buildPlanSystem / buildConfirmSystem below) stay English.
//
//  ONE-RESOLVER MODEL (see New Features Plans/task-improvements.md): a single
//  list-aware planner (`planTaskOps` in skill.js, PLAN_SCHEMA here) reads the
//  conversation AND the owner's open list, then emits a PLAN — one op per distinct
//  task the owner means (create / complete / edit / delete). Complete, edit and the
//  "how many tasks" of batch-create all fall out of the same call.
// ============================================================================

// ---- JSON Schemas for structured outputs (output_config.format) --------------
// Single source of truth for the SHAPE of each reply. skill.js passes these to
// messages.create so the API returns ONLY schema-valid JSON. Every object needs
// additionalProperties:false + a full `required` list; optional fields use a
// nullable type union.

// The main planner: enumerate the distinct tasks the latest message is about,
// match the ones that point at an existing task, and emit an op per task.
export const PLAN_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["list_requested", "owner_done", "ops"],
  properties: {
    // The owner asked to HEAR their open tasks ("what's on my list?"). Independent of ops.
    list_requested: { type: "boolean" },
    // The latest message just signals they're finished / accept as-is ("that's all",
    // "pronto", "ok perfect") with NO new task. Lets a stateful window close cleanly.
    owner_done: { type: "boolean" },
    // One op per DISTINCT task the owner means. [] when the message has no task action.
    ops: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: [
          "kind",
          "target_index",
          "candidate_indices",
          "ref_text",
          "title",
          "due_iso",
          "assignee",
        ],
        properties: {
          kind: {
            type: "string",
            enum: ["create", "complete", "edit", "delete"],
          },
          // complete/edit/delete: 1-based index into the numbered open list, or null
          // if not confident which task is meant. Always null for create.
          target_index: { type: ["number", "null"] },
          // When target_index is null but 2+ open tasks are plausible: their numbers,
          // for a named disambiguation question. Else [].
          candidate_indices: { type: "array", items: { type: "number" } },
          // The owner's own phrase for this task ("the flight one"), for clarify copy.
          // null for a clean create with nothing to disambiguate.
          ref_text: { type: ["string", "null"] },
          // create: the new task text (verbatim). edit: the new text if it changes,
          // else null. null for complete/delete.
          title: { type: ["string", "null"] },
          // create/edit: due date in ISO 8601 with -03:00; null if none/unchanged.
          // (Google Tasks due is date-only; a time, if any, is ignored.)
          due_iso: { type: ["string", "null"] },
          // create only: WHO the to-do is for. null => the owner's own to-do (Google
          // Tasks). A third party {name,email} => the skill turns it into a calendar
          // invite. Always null for complete/edit/delete.
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
      },
    },
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

// ---- Planner (interpret + resolve, in one list-aware call) -------------------
export function buildPlanSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s to-do secretary. Read the conversation, the latest order, any replied-to (quoted) message, AND ${OWNER_NAME}'s current OPEN TASK LIST, then produce a PLAN: the exact set of task operations the LATEST message calls for. (Your reply's shape is enforced separately — focus on getting the values right.)

Think like a human secretary, IN THIS ORDER:
1. ENUMERATE — how many DISTINCT tasks is ${OWNER_NAME} talking about? "I bought the pizza and I got my flights" = TWO tasks. Emit ONE op per distinct task. NEVER merge two tasks into one op; NEVER split one task into two.
2. UNDERSTAND — what is each one about? Use the conversation and the quoted message for context.
3. MATCH — for a task ${OWNER_NAME} wants to COMPLETE, EDIT, or DELETE, work out WHICH open task it means and set "target_index" to its 1-based number in the list. Judge by MEANING, not exact words, using the titles AND the due dates. If you are NOT confident which one (two or more are plausible), set target_index = null and list the plausible numbers in "candidate_indices" — better to ASK than to touch the wrong task. Put ${OWNER_NAME}'s own phrase in "ref_text".
4. CREATE — for a genuinely NEW to-do, set kind = "create" with the "title" (verbatim, do NOT translate) and "due_iso" if a due date is stated.

"kind" values:
- "create": capture a NEW to-do (for ${OWNER_NAME} OR for someone else). Includes "turn this into a task" replying to a message.
- "complete": mark an EXISTING task done / checked off. Covers "I did X", "bought X", "got the Y", "finished Z" — a report that something on the list is now done.
- "edit": change an EXISTING task's text or due date ("change the flight task to Monday", "rename the contract task to…").
- "delete": remove/cancel an EXISTING task ("delete the flight one", "nevermind the pizza task", "cancela a de hoje").

Fields on each op:
- kind (required).
- target_index: complete/edit/delete → the 1-based open-list number, or null if unsure. Always null for create.
- candidate_indices: for an ambiguous complete/edit/delete, the plausible 1-based numbers; otherwise [].
- ref_text: ${OWNER_NAME}'s phrase for this task ("the flight one"); null when nothing needs disambiguating.
- title: create → the new task text (a short imperative line, verbatim). edit → the new text ONLY if it changes, else null. null for complete/delete. For "turn this into a task", use the QUOTED message's text.
- due_iso: create/edit → the due date in ISO 8601 with the -03:00 offset, resolving relative dates ("Friday","tomorrow") with the current date/time; null if none or unchanged.
- assignee: create ONLY. null = ${OWNER_NAME}'s own to-do (the default). If the task is FOR ANOTHER person ("remind João to…","ask Ana to…"), set { name, email } for that person (email from the conversation if present, else null) — the person who must DO it, not merely someone mentioned. Always null for complete/edit/delete.

Top-level:
- list_requested: true if ${OWNER_NAME} asks to hear their open tasks ("what's on my list?","my to-dos"). Independent of ops.
- owner_done: true if the latest message just signals they're finished / accept things as-is ("that's all","done","pronto","ok, perfect") with NO new task.

If the latest message is normal conversation with NO task action, return ops = [], list_requested = false, owner_done = false. When unsure, PREFER the empty plan over inventing an op — a wrong task is worse than asking.`;
}

export function buildPlanUser(
  OWNER_NAME,
  { order, transcript, nowStr, contact, quoted, listText }
) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Contact of this conversation: ${contact || "(yourself)"}
Replied-to (quoted) message: ${quoted?.text || "(none)"}

${OWNER_NAME}'s OPEN TASKS (1-based; use these numbers for target_index):
${listText || "(none open)"}

Recent conversation:
${transcript || "(no history)"}

${OWNER_NAME}'s latest message: ${order}`;
}

// ---- Confirmation classifier (yes/no on a pending mutation) -------------------
export function buildConfirmSystem(action) {
  return `You decide whether the LATEST message is a response to a pending confirmation.
The assistant asked to confirm: ${action}.
Use the recent conversation only as context; judge ONLY the latest message.
Decide one "decision" value — "confirm", "decline", or "unrelated":
- "confirm": the latest message clearly agrees to proceed (e.g. yes, confirm, go ahead, sim, pode, isso).
- "decline": the latest message clearly refuses (e.g. no, don't, leave it, não, deixa).
- "unrelated": the latest message is normal conversation OR a NEW request, NOT a yes/no to this confirmation. If unsure, choose "unrelated".`;
}

export function buildConfirmUser({ transcript, latest }) {
  return `Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// ============================================================================
//  USER-FACING REPLY STRINGS (localized).
//  A single render layer (makeReply) drives BOTH languages from a per-language
//  vocabulary (EN / PT), so the list/confirm/applied logic is written once. Dates
//  arrive pre-formatted (localizeDueDate). Task TITLES pass through verbatim.
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
function bullets(rows) {
  return rows.map(taskLine).join("\n");
}

// One edit line for a confirm/applied view: "'old' -> 'new' (due 17/jul)" etc.,
// showing only what actually changes. `m` = { oldTitle, title?, when? }.
function editLine(w, m) {
  const newTitle = m.title && m.title !== m.oldTitle ? m.title : null;
  if (newTitle && m.when) return `"${m.oldTitle}" → "${newTitle}" (${w.dueWord} ${m.when})`;
  if (newTitle) return `"${m.oldTitle}" → "${newTitle}"`;
  if (m.when) return `"${m.oldTitle}" — ${w.dueWord} → ${m.when}`;
  return `"${m.oldTitle}"`;
}

// "Change these? / Mark these done? / Remove these?" — one confirm view for a
// batch of stored-task mutations (complete/edit/delete), plus any unmatched refs.
function renderConfirm(w, muts, missed) {
  const one = muts.length === 1;
  const types = new Set(muts.map((m) => m.type));
  let header, lines;
  if (types.size === 1) {
    const t = muts[0].type;
    if (t === "complete") {
      header = one ? w.confirmDoneOne : w.confirmDone;
      lines = muts.map((m) => `- ${m.oldTitle}`);
    } else if (t === "delete") {
      header = one ? w.confirmRemoveOne : w.confirmRemove;
      lines = muts.map((m) => `- ${m.oldTitle}`);
    } else {
      header = one ? w.confirmEditOne : w.confirmEdit;
      lines = muts.map((m) => `- ${editLine(w, m)}`);
    }
  } else {
    header = w.confirmMixed;
    lines = muts.map((m) =>
      m.type === "complete"
        ? `- ${w.markDone}: ${m.oldTitle}`
        : m.type === "delete"
          ? `- ${w.remove}: ${m.oldTitle}`
          : `- ${editLine(w, m)}`
    );
  }
  const missedNote =
    missed && missed.length ? `\n${w.missedNote(missed)}` : "";
  return `${header}\n${lines.join("\n")}${missedNote}\n\n${w.confirmFooter}`;
}

// "Done: …" — what actually got applied (and, if any, what failed).
function renderApplied(w, done, failed) {
  if (done.length === 1 && !failed.length && done[0].type === "complete")
    return w.checkedOne(done[0].oldTitle);
  const line = (m) =>
    m.type === "complete"
      ? `- ${m.oldTitle} — ${w.doneWord}`
      : m.type === "delete"
        ? `- ${m.oldTitle} — ${w.removedWord}`
        : `- ${editLine(w, m)}`;
  const parts = [];
  if (done.length) parts.push(`${w.appliedHeader}\n${done.map(line).join("\n")}`);
  if (failed.length)
    parts.push(`${w.failedHeader}\n${failed.map(line).join("\n")}`);
  return parts.join("\n\n") || w.appliedHeader;
}

function makeReply(w) {
  return {
    thinkingError: () => w.thinkingError,
    noAction: () => w.noAction,
    needTitle: () => w.needTitle,
    failed: () => w.failed,
    empty: () => w.empty,
    calendarUnavailable: () => w.calendarUnavailable,
    declined: () => w.declined,
    formatList: (rows) => `${w.listHeader}\n${bullets(rows)}`,
    // items: [{ title, when }]
    createdBatch: (items) => `${w.addedHeader}\n${bullets(items)}\n\n${w.amendHint}`,
    amended: (items) => `${w.updatedHeader}\n${bullets(items)}\n\n${w.moreHint}`,
    // titles: [string]
    removed: (titles) =>
      titles.length === 1
        ? w.removedOne(titles[0])
        : `${w.removedHeader}\n${titles.map((t) => `- ${t}`).join("\n")}`,
    // missed: [string]
    notFound: (missed) =>
      missed && missed.length ? w.notFoundSome(missed) : w.notFoundNone,
    // ref: string|null, candidates: [{ title, when }]
    disambiguate: (ref, candidates) =>
      `${w.whichOne(ref)}\n${bullets(candidates)}`,
    // muts: [{ type, oldTitle, title?, when? }], missed: [string]
    confirmMutations: (muts, missed) => renderConfirm(w, muts, missed),
    mutationsApplied: (done, failed) => renderApplied(w, done, failed),
    // n = total third-party items in the batch (only the first was set up)
    thirdPartyCapped: (n) => w.thirdPartyCapped(n),
  };
}

const EN = {
  thinkingError: "I hit an error while thinking. Try again?",
  noAction: "I didn't catch a task action there.",
  needTitle: "What should the task say? Send me the task text.",
  failed: "Something went wrong with your tasks. Error in the log. Try again?",
  empty: "Your list is empty — nothing open.",
  calendarUnavailable:
    "A task for someone else goes out as a calendar invite, but the calendar isn't available right now.",
  declined: "Okay, leaving those as they are.",
  listHeader: "Here are your open tasks:",
  addedHeader: "Added to your list:",
  amendHint: "Tell me if you need to change anything, otherwise we're good.",
  updatedHeader: "Updated your list:",
  moreHint: "Anything else to change?",
  removedOne: (t) => `Removed "${t}" from your list.`,
  removedHeader: "Removed from your list:",
  notFoundNone: "I couldn't tell which task you mean. Which one?",
  notFoundSome: (m) =>
    `I couldn't find: ${m.join("; ")}. Which one did you mean?`,
  whichOne: (ref) => `Which one${ref ? ` for "${ref}"` : ""}?`,
  confirmDone: "Mark these done?",
  confirmDoneOne: "Mark this done?",
  confirmRemove: "Remove these from your list?",
  confirmRemoveOne: "Remove this from your list?",
  confirmEdit: "Make these changes?",
  confirmEditOne: "Make this change?",
  confirmMixed: "Apply these?",
  markDone: "mark done",
  remove: "remove",
  dueWord: "due",
  missedNote: (m) => `(couldn't find: ${m.join("; ")})`,
  confirmFooter: `Reply "yes" to confirm.`,
  checkedOne: (t) => `Done — checked off "${t}".`,
  appliedHeader: "Done:",
  failedHeader: "Couldn't do these:",
  doneWord: "done",
  removedWord: "removed",
  thirdPartyCapped: (n) =>
    `I set up the first reminder. Send the other ${n - 1} separately and I'll do those too.`,
};

const PT = {
  thinkingError: "Tive um erro ao processar. Pode tentar de novo?",
  noAction: "Não entendi uma ação de tarefa aí.",
  needTitle: "O que a tarefa deve dizer? Me envie o texto da tarefa.",
  failed:
    "Algo deu errado com suas tarefas. O erro está no log. Pode tentar de novo?",
  empty: "Sua lista está vazia — nada em aberto.",
  calendarUnavailable:
    "Uma tarefa para outra pessoa vai como convite de calendário, mas o calendário não está disponível agora.",
  declined: "Ok, vou deixar como estão.",
  listHeader: "Aqui estão suas tarefas em aberto:",
  addedHeader: "Adicionei à sua lista:",
  amendHint: "Me diga se precisa mudar algo, senão está tudo certo.",
  updatedHeader: "Atualizei sua lista:",
  moreHint: "Mais alguma coisa pra mudar?",
  removedOne: (t) => `Removi "${t}" da sua lista.`,
  removedHeader: "Removi da sua lista:",
  notFoundNone: "Não consegui identificar qual tarefa. Qual delas?",
  notFoundSome: (m) =>
    `Não encontrei: ${m.join("; ")}. Qual você quis dizer?`,
  whichOne: (ref) => `Qual delas${ref ? ` para "${ref}"` : ""}?`,
  confirmDone: "Marcar estas como concluídas?",
  confirmDoneOne: "Marcar como concluída?",
  confirmRemove: "Remover estas da sua lista?",
  confirmRemoveOne: "Remover esta da sua lista?",
  confirmEdit: "Fazer estas mudanças?",
  confirmEditOne: "Fazer esta mudança?",
  confirmMixed: "Aplicar estas?",
  markDone: "concluir",
  remove: "remover",
  dueWord: "vence",
  missedNote: (m) => `(não encontrei: ${m.join("; ")})`,
  confirmFooter: `Responda "sim" para confirmar.`,
  checkedOne: (t) => `Pronto — concluí "${t}".`,
  appliedHeader: "Pronto:",
  failedHeader: "Não consegui nestas:",
  doneWord: "concluída",
  removedWord: "removida",
  thirdPartyCapped: (n) =>
    `Configurei o primeiro lembrete. Envie os outros ${n - 1} separadamente que eu cuido deles também.`,
};

const REPLY = { en: makeReply(EN), pt: makeReply(PT) };

// Pick the reply set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
