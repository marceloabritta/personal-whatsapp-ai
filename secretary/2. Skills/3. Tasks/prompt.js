// ============================================================================
//  Skill "Tasks" — PROMPT + user-facing STRINGS.
//  Prompt text/rules and localized reply strings only — no logic (that's skill.js).
//  The output JSON must keep matching what skill.js expects.
//
//  LOCALIZATION: user-facing strings are a per-language map, selected at send time
//  with ctx.lang via reply(). English is canonical; pt is maintained; any other
//  language is produced from the `en` copy by the orchestrator's send() translation
//  fallback. Add BOTH en + pt for every new message. Internal/classification prompts
//  (buildPlanSystem below) stay English.
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

// The yes/no/unrelated classifier for a pending confirmation is shared by every
// confirm-first skill — schema + prompts live in 1. Orchestrator/lib/confirm.js.

// ---- Planner (interpret + resolve, in one list-aware call) -------------------
// The planner's core instructions — IDENTICAL for a tagged order and an untagged
// follow-up. The untagged case APPENDS the posture below; it changes nothing here.
function plannerCore(OWNER_NAME) {
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

// The UNTAGGED posture, appended to the core when the message carried no tag. It
// ASKS whether the secretary was addressed; it must NEVER ASSERT that she wasn't.
// Every in-window follow-up is untagged too (server.js:255), so this branch is
// entered by "na verdade muda essa pra sexta" as well — an asserting posture would
// silently swallow real follow-ups (no reply, no failure report: skill.js:368-371).
// The bar is split on purpose: rule (a) — the REFERENT — governs complete/edit/
// delete/list_requested; rule (b) — the FORM OF ADDRESS — governs create. A create
// has no referent by construction, so a single referent rule would forbid EVERY
// untagged create, silently.
function untaggedPosture(OWNER_NAME) {
  return `--- THIS MESSAGE CARRIES NO TAG ---
${OWNER_NAME} did not tag the secretary in this message. You are seeing it only because a task exchange was recently underway and you get to read whatever he types next — and what he types next is often meant for the OTHER PERSON in the chat, not for you. The tag cannot tell you which. That is the question you must answer FIRST:

**Was this message aimed at YOU, or was ${OWNER_NAME} talking to someone else?**

Both are common, and you must handle both:
- AIMED AT YOU — a follow-up to the task exchange you were just having ("na verdade muda essa pra sexta", "cancela essa", "adiciona também: comprar pão", "e o que mais eu tenho?"). These need no tag. ACT on them exactly as if they were tagged.
- NOT AIMED AT YOU — ${OWNER_NAME} talking to the person he is chatting with: his day, his plans, their work, their project, a joke, an opinion. You are merely overhearing. Do NOTHING.

The test is NOT "is this task-shaped?" — ordinary talk is full of task-shaped sentences, and that is exactly how the phantom task below got created. The test is: **is ${OWNER_NAME} giving an instruction to YOU, his secretary, about HIS OWN to-do list — or is he talking to the other person?** How you answer it depends on the kind of op, because the two kinds give you different evidence:

(a) complete / edit / delete / list_requested — these POINT AT SOMETHING THAT ALREADY EXISTS, so the REFERENT is your evidence. What he wants finished, changed, cancelled or read out must be a task on the OPEN TASK LIST above, or one you created or changed earlier in this exchange. If it is anything else — a meeting, a message, an event, the other person's work, their project, their day — he is talking to THEM, not to you. Return ops = [], list_requested = false. ("cancela a reunião com o cliente" is an imperative, but its referent is a meeting that is not on his task list: he is telling the OTHER PERSON to cancel it.)
    The referent test can only RULE OUT. It never licenses on its own: a message whose referent happens to be on his list can still be him talking to the other person — above all when it CONTINUES his own previous message to them. ("e mandar ele ter workers" carries on "amanha vou tentar implementar o tenente…" — same sentence, same listener. Not an edit, even though a task by that name is now on the list.) Ask whom he was last speaking to, and whether this message carries on that speech.

(b) create — a NEW to-do has no referent yet. "It is not on his list" therefore proves NOTHING about a create: no create is EVER on his list — that is what makes it a create. NEVER apply the referent test above to a create. Your evidence here is THE FORM OF THE ADDRESS: is he telling YOU to write something down, or is he thinking out loud?
    - ACT — an imperative to you to record a to-do: "adiciona também: comprar pão", "bota aí: ligar pro dentista", "me lembra de pagar o aluguel", "anota: falar com a Ana", "add X to my list", "remind me to X". He is handing you an item for HIS list. These are creates, tag or no tag. ACT on them exactly as if they were tagged.
    - DO NOT ACT — a first-person statement of intent, plan or future action: "amanhã vou tentar X", "vou fazer X", "amanhã eu tento X de novo", "preciso fazer X uma hora dessas", "I'll do X tomorrow", "we should X". This is CONVERSATION, NEVER a task, however task-shaped it sounds. ${OWNER_NAME} musing aloud about tomorrow is not delegating to you. A human secretary does not silently add a to-do every time her boss thinks out loud. Return ops = [].
    - DO NOT ACT — an imperative aimed at the OTHER person or at THEIR list ("adiciona isso no teu backlog", "bota isso no Jira de vocês"): a recording is being asked for, but not from you and not on his list.

A QUOTED MESSAGE IS CONTEXT, NEVER AN ORDER. Quoting is simply how people reply in WhatsApp; the quote itself carries no address. An untagged message that merely quotes or reacts to something — a comment, a laugh, an opinion, a "temos que fazer isso um dia" — is talk to the other person: return ops = []. Do NOT turn the quoted text into a task. The "turn this into a task" idiom in the rules above still stands, but it clears the same bar as everything else: act only when the untagged message ITSELF is an explicit imperative to you to record it ("transforma isso em tarefa", "vira tarefa isso aí", "anota isso aí"). A quote plus a musing is a musing.

Also:
- Opinions, jokes, stories about his own work, questions to the other person, and anything about the OTHER person's work or projects are never task ops.
- TIE-BREAK, and it is ONLY that: if after (a) and (b) you still cannot tell whether ${OWNER_NAME} was speaking to you or to the other person, return the EMPTY plan. A phantom task is far worse than a missed one — he can always re-tag. This breaks TIES; it does NOT overrule (a) or (b). Where (a) or (b) has already given you a clear answer — the referent is a task on his list AND the message reads as an instruction to you, or the message is a plain imperative to you to record a to-do — the message IS aimed at you: act on it, and do not talk yourself out of it.
- This holds even when there is no other person (a note-to-self chat) or when the chat is a group: act only on what is aimed at you.

THE SAME BAR APPLIES TO list_requested. Reading his list out loud SENDS IT INTO THIS CHAT, where the other person will read it. "quantas tarefas você tem pra amanhã?" asked OF the other person, about THEIR day, is not a request for his list — set list_requested = false. Set list_requested = true only when he is asking YOU what is on HIS list ("e o que mais eu tenho?", "o que falta?").

owner_done is the ONE exception: you may still set it on a message that merely closes the exchange ("pronto, é isso", "ok, valeu"), because it only ends the window — it writes nothing and says nothing.

EXAMPLES — untagged messages while the window is open.

DO NOTHING:

"amanha vou tentar implementar o tenente dentro do VsCode"
  -> ops: [], list_requested: false   <- rule (b): a statement of intent, to the OTHER person.
                                         NOT a task. (This exact message created a real phantom
                                          task on 2026-07-11. Do not repeat it.)
"e mandar ele ter workers"
  -> ops: [], list_requested: false   <- continues the sentence above, still to the other
                                         person — even though the phantom is now ON his list.
                                         Rule (a) rules out; it never licenses.
"amanhã eu tento de novo kkkk"
  -> ops: []                          <- musing, with laughter.
"esse projeto seu n parece ser mto grande. é?"
  -> ops: []                          <- a question to the other person.
"cancela a reunião com o cliente"
  -> ops: []                          <- rule (a): an imperative, but the referent is a meeting
                                         that is not on his task list. He is talking to them.
"quantas tarefas você tem pra amanhã?"
  -> list_requested: false, ops: []   <- "você" is the OTHER person; he is asking about THEIR
                                         day. Printing his list here would show it to them.
"kkk isso é muito bom"  [quoting a message from the other person]
  -> ops: []                          <- a reaction to a quote. The quote is context, not an order.
"temos que fazer isso um dia"  [quoting a message from the other person]
  -> ops: []                          <- a musing about the quoted message, said to THEM. Do NOT
                                         create a task out of the quoted text.

ACT:

"na verdade muda essa pra sexta"
  -> edit                            <- rule (a): refers to the task just created. Aimed at you.
"pode marcar a de comprar leite como feita"
  -> complete                        <- rule (a): refers to a task on his open list.
"cancela essa"
  -> delete                          <- rule (a): refers to the task just touched.
"adiciona também: comprar pão"
  -> create                          <- rule (b): an imperative to YOU to record a to-do. It is
                                        not on his list — of course not; no create ever is.
"me lembra de pagar o aluguel"
  -> create                          <- rule (b): the same thing in another form. An untagged
                                        create is still a create. ACT.
"transforma isso em tarefa"  [quoting a message]
  -> create                          <- an explicit imperative to record the quoted message.
                                        Title = the QUOTED text (see the core rules above).
"e o que mais eu tenho?"
  -> list_requested: true            <- he is asking YOU what is on HIS list.
"pronto, é isso"
  -> owner_done: true                <- closes the window.`;
}

// addressed = ctx.isTagged: did this message tag the secretary?
//   true  -> an order. Today's posture, unchanged.
//   false -> untagged. It reached us only because a window is open. It may be a genuine
//            follow-up OR the owner talking to someone else — the model must DECIDE.
// `addressed` is REQUIRED: calling buildPlanSystem(owner) throws on the destructure.
export function buildPlanSystem(OWNER_NAME, { addressed }) {
  const core = plannerCore(OWNER_NAME);
  if (addressed) return core;
  return `${core}\n\n${untaggedPosture(OWNER_NAME)}`;
}

export function buildPlanUser(
  OWNER_NAME,
  { order, transcript, nowStr, contact, quoted, listText, addressed }
) {
  // States the UNCERTAINTY on an untagged message — it never asserts the message was
  // not for us. An in-window follow-up is untagged too.
  const addressLine = addressed
    ? `Addressed to you: YES — ${OWNER_NAME} tagged the secretary in this message. It is an order to you.`
    : `Addressed to you: UNKNOWN — this message carries no tag. It reached you only because a task exchange is open. It may be a follow-up aimed at you, or ${OWNER_NAME} talking to someone else. Decide which (see the rules), and if it is plausible he was not talking to you, return the empty plan.`;
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
${addressLine}
Contact of this conversation: ${contact || "(yourself)"}
Replied-to (quoted) message: ${quoted?.text || "(none)"}

${OWNER_NAME}'s OPEN TASKS (1-based; use these numbers for target_index):
${listText || "(none open)"}

Recent conversation:
${transcript || "(no history)"}

${OWNER_NAME}'s latest message: ${order}`;
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
