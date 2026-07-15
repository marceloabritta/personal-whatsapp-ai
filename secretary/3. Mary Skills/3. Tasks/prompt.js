// ============================================================================
//  Skill "Tasks" — PROMPT + user-facing STRINGS.  CONVERTED (pure task).
//  Localized reply strings only — no logic (that's skill.js).
//
//  The PLANNER that used to live here (PLAN_SCHEMA, plannerCore, untaggedPosture,
//  buildPlanSystem/User) is gone: the ORCHESTRATOR classifies the order into the skill's
//  declared inputs (mode + ops) and runs the conversation. The confirm-first PROPOSAL and
//  disambiguation prose is gone too (the model proposes before an ACT). What remains is the
//  due-date localizer and the OUTCOME render layer — a single makeReply() driving both
//  languages from a per-language vocabulary (EN / PT). Task TITLES pass through verbatim.
//
//  LOCALIZATION: keep BOTH en + pt for every message; any other language is produced from the
//  `en` copy by the orchestrator's send() translation fallback.
// ============================================================================

// Localized DATE-ONLY string for a task due, rendered as "dd/mmm" (e.g. 17/jul). The month
// abbreviation follows ctx.lang (en "jul"/"may", pt "jul"/"mai"). Google Tasks stores due at
// UTC midnight, so we render in UTC to show the same calendar date that was stored; a fresh
// add passes the same normalized value, so display always matches storage.
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

// One task line: "dd/mmm - title" when it has a due date, else just the title.
// Language-agnostic (the date is pre-localized by localizeDueDate).
function taskLine(r) {
  return r.when ? `${r.when} - ${r.title}` : r.title;
}
function bullets(rows) {
  return rows.map(taskLine).join("\n");
}

// One edit line for an applied view: "'old' -> 'new' (due 17/jul)" etc., showing only what
// actually changes. `m` = { oldTitle, title?, when? }.
function editLine(w, m) {
  const newTitle = m.title && m.title !== m.oldTitle ? m.title : null;
  if (newTitle && m.when) return `"${m.oldTitle}" → "${newTitle}" (${w.dueWord} ${m.when})`;
  if (newTitle) return `"${m.oldTitle}" → "${newTitle}"`;
  if (m.when) return `"${m.oldTitle}" — ${w.dueWord} → ${m.when}`;
  return `"${m.oldTitle}"`;
}

// "Done: …" — what actually got applied (and, if any, what failed). Handles complete / edit /
// delete; creates get their own header (createdBatch).
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
    failed: () => w.failed,
    empty: () => w.empty,
    formatList: (rows) => `${w.listHeader}\n${bullets(rows)}`,
    // items: [{ title, when }]
    createdBatch: (items) => `${w.addedHeader}\n${bullets(items)}`,
    amended: (items) => `${w.updatedHeader}\n${bullets(items)}`,
    // titles: [string]
    removed: (titles) =>
      titles.length === 1
        ? w.removedOne(titles[0])
        : `${w.removedHeader}\n${titles.map((t) => `- ${t}`).join("\n")}`,
    // done/failed: [{ type, oldTitle, title?, when? }]
    mutationsApplied: (done, failed) => renderApplied(w, done, failed),
  };
}

const EN = {
  thinkingError: "I hit an error while thinking. Try again?",
  noAction: "I didn't catch a task action there.",
  failed: "Something went wrong with your tasks. Error in the log. Try again?",
  empty: "Your list is empty — nothing open.",
  listHeader: "Here are your open tasks:",
  addedHeader: "Added to your list:",
  updatedHeader: "Updated your list:",
  removedOne: (t) => `Removed "${t}" from your list.`,
  removedHeader: "Removed from your list:",
  dueWord: "due",
  checkedOne: (t) => `Done — checked off "${t}".`,
  appliedHeader: "Done:",
  failedHeader: "Couldn't do these:",
  doneWord: "done",
  removedWord: "removed",
};

const PT = {
  thinkingError: "Tive um erro ao processar. Pode tentar de novo?",
  noAction: "Não entendi uma ação de tarefa aí.",
  failed:
    "Algo deu errado com suas tarefas. O erro está no log. Pode tentar de novo?",
  empty: "Sua lista está vazia — nada em aberto.",
  listHeader: "Aqui estão suas tarefas em aberto:",
  addedHeader: "Adicionei à sua lista:",
  updatedHeader: "Atualizei sua lista:",
  removedOne: (t) => `Removi "${t}" da sua lista.`,
  removedHeader: "Removi da sua lista:",
  dueWord: "vence",
  checkedOne: (t) => `Pronto — concluí "${t}".`,
  appliedHeader: "Pronto:",
  failedHeader: "Não consegui nestas:",
  doneWord: "concluída",
  removedWord: "removida",
};

const REPLY = { en: makeReply(EN), pt: makeReply(PT) };

// Pick the reply set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
