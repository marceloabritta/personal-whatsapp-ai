// ============================================================================
//  Skill "Tasks" (todo inbox) — LOGIC.  CONVERTED (pure task, read-then-act).
//  In the NEW (@mary) flow the ORCHESTRATOR runs the conversation and hands a validated
//  payload in ctx.info. run() is a pure dispatch on ctx.info.mode:
//    - list  (READ): fetch the open tasks, send them, AND return them structured (each with
//                    its id) so the model can target a follow-up complete/edit/delete by id.
//    - apply (ACT):  execute ctx.info.ops in order (add / complete / patch / delete, the
//                    mutations targeting a task_id), send the applied summary, return
//                    { applied, failed }.
//  There is NO planner, NO confirm-first session, NO stateful "engaged" window, and NO
//  calendar coupling: a to-do FOR SOMEONE ELSE is now the model chaining a calendar_action
//  create (SCOPE Q5), not this skill reaching into calendar_action.startCreate.
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description, conversation, inputs }
//    export async function run(ctx) -> a JSON-serializable value (the read-back)
//  Localized replies live in prompt.js (reply(ctx.lang)); ctx.send is pre-bound to the
//  conversation language. Google Tasks needs the OAuth scope
//  https://www.googleapis.com/auth/tasks on GOOGLE_REFRESH_TOKEN (see SKILL.md).
// ============================================================================
import { google } from "googleapis";
import { reply, localizeDueDate } from "./prompt.js";
import { googleAuth } from "../../1. Orchestrator/lib/google.js";

// `inputs` — the DECLARED input contract (see 1. Orchestrator/lib/inputs.js). The orchestrator
// fills it in the same round-trip that classifies the order, and gates on `ok` before dispatch.
// `mode` is the discriminator: a READ (list) carries no requiredWhen; an ACT (apply) requires
// `ops`. `ops` is declared nullable so a `list` payload (ops:null) is shape-valid.
export const manifest = {
  id: "task_action",
  // CONVERTED (pure task): the model runs the dialogue; run() only reads/acts + returns.
  conversation: "orchestrator",
  inputs: {
    discriminator: "mode",
    fields: {
      mode: {
        type: "enum",
        enum: ["list", "apply", "other"],
        desc: "list = read the open tasks; apply = run the ops; other = none of these",
      },
      ops: {
        type: "array",
        nullable: true,
        of: {
          kind: { type: "enum", enum: ["create", "complete", "edit", "delete"] },
          task_id: { type: "string", nullable: true },
          title: { type: "string", nullable: true },
          due_iso: { type: "iso", nullable: true },
        },
        desc: "the task operations to perform (apply only). create carries a title; complete/edit/delete carry the task_id read back from a list. edit also carries the new title and/or due_iso.",
      },
    },
    requiredWhen: {
      list: [],
      apply: ["ops"],
      other: [],
    },
    consistency: [
      {
        name: "create_op_has_a_title",
        test: (i) =>
          !Array.isArray(i.ops) ||
          i.ops.every(
            (o) => o?.kind !== "create" || (o?.title && String(o.title).trim() !== "")
          ),
      },
      {
        // complete / edit / delete target an EXISTING task, so each must carry its task_id.
        name: "mutate_op_has_a_task_id",
        test: (i) =>
          !Array.isArray(i.ops) ||
          i.ops.every(
            (o) =>
              !["complete", "edit", "delete"].includes(o?.kind) ||
              (o?.task_id && String(o.task_id).trim() !== "")
          ),
      },
      {
        name: "apply_has_at_least_one_op",
        test: (i) => i.mode !== "apply" || (Array.isArray(i.ops) && i.ops.length > 0),
      },
    ],
    rulebook: () =>
      "Two-step contract. To COMPLETE, EDIT or DELETE an existing task you need its task_id, so " +
      'first dispatch mode="list" to read the open tasks (each carries an id); read the ids back, ' +
      'then dispatch mode="apply" with ops targeting the right task_id. Ops: {kind:"create", ' +
      "title, due_iso?} to add a to-do; {kind:\"complete\"|\"edit\"|\"delete\", task_id} to change " +
      "a stored task (edit also carries the new title and/or due_iso). due_iso is ISO-8601 with " +
      "the -03:00 offset (Google Tasks keeps the date only). A to-do FOR SOMEONE ELSE is NOT a " +
      "task op — chain a calendar_action create instead.",
  },
  description:
    "add one or more to-dos for the owner, list open tasks, complete/check off tasks, or " +
    "edit/rename/reschedule/delete existing tasks. A to-do for SOMEONE ELSE is not this skill — " +
    "that goes out as a calendar invite (calendar_action).",
};

// ---- Google Tasks client -----------------------------------------------------
function tasksClient(env) {
  return google.tasks({ version: "v1", auth: googleAuth(env) });
}
function listId(env) {
  return env.GOOGLE_TASKLIST_ID || "@default";
}

// Google Tasks due is DATE-ONLY, stored at UTC midnight. Normalize a -03:00 ISO to
// the São Paulo calendar date, pinned to UTC midnight, so the stored/displayed date
// matches what the owner meant. Returns undefined when there's no due.
function toTasksDue(iso) {
  if (!iso) return undefined;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return undefined;
  const ymd = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Sao_Paulo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
  return `${ymd}T00:00:00.000Z`;
}

async function addTask(env, { title, due }) {
  const svc = tasksClient(env);
  const requestBody = { title };
  if (due) requestBody.due = due;
  const r = await svc.tasks.insert({ tasklist: listId(env), requestBody });
  return r.data;
}
async function listTasks(env) {
  const svc = tasksClient(env);
  const r = await svc.tasks.list({
    tasklist: listId(env),
    showCompleted: false,
    showHidden: false,
    maxResults: 100,
  });
  return r.data.items || [];
}
async function completeTask(env, taskId) {
  const svc = tasksClient(env);
  await svc.tasks.patch({
    tasklist: listId(env),
    task: taskId,
    requestBody: { status: "completed" },
  });
}
async function deleteTask(env, taskId) {
  const svc = tasksClient(env);
  await svc.tasks.delete({ tasklist: listId(env), task: taskId });
}
async function patchTask(env, taskId, { title, due }) {
  const svc = tasksClient(env);
  const requestBody = {};
  if (title != null) requestBody.title = title;
  if (due !== undefined) requestBody.due = due;
  await svc.tasks.patch({ tasklist: listId(env), task: taskId, requestBody });
}

// Open tasks only, with a non-blank title, normalized to { id, title, due }.
async function fetchOpen(ctx) {
  const items = await listTasks(ctx.env);
  return (items || [])
    .filter((t) => t.status !== "completed" && (t.title || "").trim())
    .map((t) => ({ id: t.id, title: t.title.trim(), due: t.due || null }));
}

// ---- Entry point -------------------------------------------------------------
// ctx (from the orchestrator): { number, send, sendFailure, env, lang, info, ... }. The model
// has already classified the order into ctx.info; run() just reads or acts on it.
export async function run(ctx) {
  const { number, lang } = ctx;
  const info = ctx.info || {};

  if (info.mode === "list") return handleList(ctx);
  if (info.mode === "apply") return applyOps(ctx, Array.isArray(info.ops) ? info.ops : []);

  // "other" / unknown — the orchestrator gated on `ok`, so this is belt-and-braces.
  await ctx.sendFailure(number, reply(lang).noAction());
  return { ok: false, reason: "noAction" };
}

// ---- LIST (READ) -------------------------------------------------------------
// Send the open list AND return it structured, so a follow-up complete/edit/delete can target
// by id on a later ACT turn.
async function handleList(ctx) {
  const { number, send, lang } = ctx;
  let open;
  try {
    open = await fetchOpen(ctx);
  } catch (e) {
    console.error("Tasks list error:", e?.response?.data || e?.message || e);
    await ctx.sendFailure(number, reply(lang).failed());
    return { ok: false, reason: "failed" };
  }
  if (!open.length) {
    await send(number, reply(lang).empty());
    return { tasks: [] };
  }
  await send(
    number,
    reply(lang).formatList(open.map((t) => ({ title: t.title, when: localizeDueDate(lang, t.due) })))
  );
  // Lean candidate shape (id + title + due) so the serialized read-back stays under READBACK_CAP.
  return { tasks: open.map((t) => ({ id: t.id, title: t.title, due: t.due })) };
}

// ---- APPLY (ACT) -------------------------------------------------------------
// Execute the ops in order. Mutations (complete/edit/delete) target a task_id the model read
// back from a prior list. Titles for the applied summary are resolved from a single up-front
// read of the open list (complete/delete remove the task, so the title must be fetched first).
async function applyOps(ctx, ops) {
  const { env, lang } = ctx;

  let titleById = new Map();
  try {
    const open = await fetchOpen(ctx);
    titleById = new Map(open.map((t) => [t.id, t.title]));
  } catch (e) {
    // Non-fatal: we can still apply; the summary falls back to the op's own title.
    console.error("Tasks pre-list error:", e?.response?.data || e?.message || e);
  }
  const titleOf = (op) => titleById.get(op.task_id) || (op.title || "").trim() || "(task)";

  const createdView = []; // { title, when }
  const doneView = []; // { type, oldTitle, title?, when? }
  const failedView = []; // { type, oldTitle }
  const applied = []; // structured, for the read-back
  const failed = [];

  for (const op of ops) {
    if (!op || typeof op !== "object") continue;
    const kind = op.kind;
    try {
      if (kind === "create") {
        const title = (op.title || "").trim();
        if (!title) {
          failed.push({ kind, reason: "no title" });
          failedView.push({ type: "create", oldTitle: "(new task)" });
          continue;
        }
        const due = toTasksDue(op.due_iso) || null;
        const task = await addTask(env, { title, due });
        applied.push({ kind: "create", id: task.id, title, due });
        createdView.push({ title, when: localizeDueDate(lang, due) });
      } else if (kind === "complete") {
        if (!op.task_id) {
          failed.push({ kind, reason: "no task_id" });
          failedView.push({ type: "complete", oldTitle: titleOf(op) });
          continue;
        }
        const oldTitle = titleOf(op);
        await completeTask(env, op.task_id);
        applied.push({ kind: "complete", id: op.task_id, title: oldTitle });
        doneView.push({ type: "complete", oldTitle });
      } else if (kind === "delete") {
        if (!op.task_id) {
          failed.push({ kind, reason: "no task_id" });
          failedView.push({ type: "delete", oldTitle: titleOf(op) });
          continue;
        }
        const oldTitle = titleOf(op);
        await deleteTask(env, op.task_id);
        applied.push({ kind: "delete", id: op.task_id, title: oldTitle });
        doneView.push({ type: "delete", oldTitle });
      } else if (kind === "edit") {
        if (!op.task_id) {
          failed.push({ kind, reason: "no task_id" });
          failedView.push({ type: "edit", oldTitle: titleOf(op) });
          continue;
        }
        const oldTitle = titleOf(op);
        const patch = {};
        if (op.title && String(op.title).trim()) patch.title = String(op.title).trim();
        const nd = op.due_iso ? toTasksDue(op.due_iso) : undefined;
        if (nd) patch.due = nd;
        await patchTask(env, op.task_id, patch);
        applied.push({
          kind: "edit",
          id: op.task_id,
          title: patch.title || oldTitle,
          due: patch.due ?? null,
        });
        doneView.push({
          type: "edit",
          oldTitle,
          title: patch.title || null,
          when: patch.due ? localizeDueDate(lang, patch.due) : null,
        });
      }
    } catch (e) {
      console.error("Tasks apply error:", e?.response?.data || e?.message || e);
      failed.push({ kind, id: op.task_id || null, reason: "api error" });
      failedView.push({ type: kind || "edit", oldTitle: titleOf(op) });
    }
  }

  // Notify. Successful creates get their own header; complete/edit/delete (and any failure)
  // go through the applied summary — via sendFailure whenever anything failed, so a batch that
  // half-worked still files a self-learning report.
  const { number, send } = ctx;
  if (createdView.length) await send(number, reply(lang).createdBatch(createdView));
  if (doneView.length || failedView.length) {
    const msg = reply(lang).mutationsApplied(doneView, failedView);
    if (failedView.length) await ctx.sendFailure(number, msg);
    else await send(number, msg);
  }
  if (!createdView.length && !doneView.length && !failedView.length) {
    await ctx.sendFailure(number, reply(lang).noAction());
  }

  return { applied, failed };
}
