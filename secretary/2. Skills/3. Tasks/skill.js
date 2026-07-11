// ============================================================================
//  Skill "Tasks" (todo inbox) — LOGIC.
//  Interprets the order with Claude and acts on the owner's to-dos:
//    - add (self)   : create a private Google Tasks item immediately, then keep a
//                     short AMEND window open to correct/delete it (no confirm step).
//    - add (other)  : a task FOR someone else has no private-list equivalent — it
//                     must reach them by email, so we DELEGATE to calendar_action's
//                     `startCreate` capability (a 5-min invite at 15:00). Confirm-first
//                     and the whole lifecycle is owned by calendar_action.
//    - list         : read back the open tasks.
//    - complete     : resolve which task, then confirm-first before marking done.
//  Run by the orchestrator when the router picks "task_action".
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
//  Localized replies live in prompt.js (reply(ctx.lang)); ctx.send is pre-bound to
//  the conversation language. Google Tasks needs the OAuth scope
//  https://www.googleapis.com/auth/tasks on GOOGLE_REFRESH_TOKEN (see SKILL.md) —
//  without it, Tasks calls 401 and we reply with reply().failed().
// ============================================================================
import { google } from "googleapis";
import {
  buildSystem,
  buildUserPrompt,
  buildResolveRefSystem,
  buildResolveRefUser,
  buildReviewAddSystem,
  buildReviewAddUser,
  buildConfirmSystem,
  buildConfirmUser,
  reply,
  localizeDueDate,
  threePmOnDue,
  TASK_SCHEMA,
  RESOLVE_REF_SCHEMA,
  REVIEW_ADD_SCHEMA,
  CONFIRM_SCHEMA,
} from "./prompt.js";

export const manifest = {
  id: "task_action",
  description:
    "add a to-do for the owner OR for another person, list the owner's open tasks, or complete/check off a task",
};

// ---- Structured-output helpers (same pattern as calendar_action) -------------
function jsonFormat(schema) {
  return { format: { type: "json_schema", schema } };
}
function readReply(msg) {
  if (msg?.stop_reason === "refusal") {
    console.error("tasks: model refused the request");
    return null;
  }
  const out = (msg?.content || [])
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
  return parseJsonReply(out);
}
// Pull the FIRST balanced {...} out of an LLM reply; tolerates ```json fences and
// stray prose. Returns the parsed object or null (never throws).
function parseJsonReply(out) {
  if (!out) return null;
  let s = String(out).trim();
  const fenced = s.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  if (fenced) s = fenced[1].trim();
  try {
    return JSON.parse(s);
  } catch {
    /* fall through */
  }
  const start = s.indexOf("{");
  if (start < 0) return null;
  let depth = 0,
    inStr = false,
    esc = false;
  for (let i = start; i < s.length; i++) {
    const c = s[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
    } else if (c === '"') inStr = true;
    else if (c === "{") depth++;
    else if (c === "}" && --depth === 0) {
      try {
        return JSON.parse(s.slice(start, i + 1));
      } catch {
        return null;
      }
    }
  }
  return null;
}

// ---- Google Tasks client -----------------------------------------------------
function tasksClient(env) {
  const o = new google.auth.OAuth2(
    env.GOOGLE_CLIENT_ID,
    env.GOOGLE_CLIENT_SECRET
  );
  o.setCredentials({ refresh_token: env.GOOGLE_REFRESH_TOKEN });
  return google.tasks({ version: "v1", auth: o });
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

// Open tasks only, with a non-blank title.
function openWithTitle(items) {
  return (items || []).filter(
    (t) => t.status !== "completed" && (t.title || "").trim()
  );
}

// ---- Interpret ---------------------------------------------------------------
async function interpret(ctx) {
  const { anthropic, model, owner, order, transcript, nowStr, contact, quoted } =
    ctx;
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 1024,
    system: buildSystem(owner),
    output_config: jsonFormat(TASK_SCHEMA),
    messages: [
      {
        role: "user",
        content: buildUserPrompt(owner, {
          order,
          transcript,
          nowStr,
          contact,
          quoted,
        }),
      },
    ],
  });
  const info = readReply(msg);
  console.log("TASK RAW:", JSON.stringify(info));
  return info;
}

// A meaningful third-party assignee (has a name or email, and isn't the owner).
function otherAssignee(ctx, info) {
  const a = info?.assignee;
  if (!a || (!a.name && !a.email)) return null;
  const owner = String(ctx.owner || "").trim().toLowerCase();
  const nm = String(a.name || "").trim().toLowerCase();
  if (nm && (nm === owner || nm === "me" || nm === "myself" || nm === "eu"))
    return null;
  return { name: a.name || null, email: a.email || null };
}

// ctx (from the orchestrator): { owner, tag, anthropic, model, order, transcript,
//   nowStr, contact, remoteJid, number, fromMe, quoted, env, evolution, send,
//   sessions, session, lang, hasSkill, callSkill }
export async function run(ctx) {
  const { number, send, session } = ctx;

  // CONTINUATIONS owned by this skill (set by the orchestrator on a continuation).
  if (session?.intent === "add" && session.stage === "await_amend") {
    return resumeAmend(ctx, session);
  }
  if (session?.intent === "complete" && session.stage === "await_confirmation") {
    return resumeComplete(ctx, session);
  }

  let info;
  try {
    info = await interpret(ctx);
  } catch (e) {
    console.error("Tasks/Claude error:", e);
    await send(number, reply(ctx.lang).thinkingError());
    return;
  }

  if (info?.action === "add") {
    const assignee = otherAssignee(ctx, info);
    return assignee
      ? handleAddOther(ctx, info, assignee)
      : handleAddSelf(ctx, info);
  }
  if (info?.action === "list") return handleList(ctx);
  if (info?.action === "complete") return handleComplete(ctx, info);

  await send(number, reply(ctx.lang).noAction());
}

// ---- ADD (self): immediate write + amend window ------------------------------
async function handleAddSelf(ctx, info) {
  const { env, number, send, sessions, remoteJid } = ctx;
  const title = (info.title || ctx.quoted?.text || "").trim();
  if (!title) {
    await send(number, reply(ctx.lang).needTitle());
    return;
  }
  const due = toTasksDue(info.due_iso) || null;

  let task;
  try {
    task = await addTask(env, { title, due });
  } catch (e) {
    console.error("Tasks add error:", e?.response?.data || e?.message || e);
    await send(number, reply(ctx.lang).failed());
    return;
  }

  // Keep a short window open so a follow-up can correct/delete the created task.
  await sessions.set(
    remoteJid,
    {
      skill: "task_action",
      intent: "add",
      stage: "await_amend",
      awaitFrom: "owner",
      lang: ctx.lang,
      data: { taskId: task.id, title, due },
    },
    600
  );
  await send(
    number,
    reply(ctx.lang).added({ title, when: localizeDueDate(ctx.lang, due) })
  );
}

// Resume a just-added task's amend window. Runs for EVERY owner message while open:
// correct/delete on a real edit, silent on chatter.
async function resumeAmend(ctx, session) {
  const { env, number, send, sessions, remoteJid } = ctx;
  const data = session.data || {};
  if (!data.taskId) {
    await sessions.clear(remoteJid);
    return;
  }

  const review = await reviewAdd(ctx, data);
  if (!review || review.decision === "unrelated") return; // ignore chatter
  if (review.decision === "keep") {
    await sessions.clear(remoteJid);
    return; // accepted as-is
  }
  if (review.decision === "delete") {
    try {
      await deleteTask(env, data.taskId);
    } catch (e) {
      console.error("Tasks delete error:", e?.response?.data || e?.message || e);
      await sessions.clear(remoteJid);
      await send(number, reply(ctx.lang).failed());
      return;
    }
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).removed({ title: data.title }));
    return;
  }

  // decision === "amend": apply whatever actually changed.
  const patch = {};
  const newTitle = (review.title || "").trim();
  if (newTitle && newTitle !== data.title) patch.title = newTitle;
  const newDue = toTasksDue(review.due_iso) || null;
  if (newDue && newDue !== data.due) patch.due = newDue;
  if (patch.title === undefined && patch.due === undefined) return; // nothing new

  try {
    await patchTask(env, data.taskId, patch);
  } catch (e) {
    console.error("Tasks patch error:", e?.response?.data || e?.message || e);
    await send(number, reply(ctx.lang).failed());
    return;
  }

  const finalTitle = patch.title ?? data.title;
  const finalDue = patch.due ?? data.due;
  await sessions.set(
    remoteJid,
    {
      skill: "task_action",
      intent: "add",
      stage: "await_amend",
      awaitFrom: "owner",
      lang: ctx.lang,
      data: { taskId: data.taskId, title: finalTitle, due: finalDue },
    },
    600 // re-arm for further edits
  );
  await send(
    number,
    reply(ctx.lang).updated({
      title: finalTitle,
      when: localizeDueDate(ctx.lang, finalDue),
    })
  );
}

async function reviewAdd(ctx, data) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 1024,
      system: buildReviewAddSystem(owner),
      output_config: jsonFormat(REVIEW_ADD_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildReviewAddUser({
            taskJson: JSON.stringify(data),
            transcript,
            latest: order,
            nowStr,
          }),
        },
      ],
    });
    const parsed = readReply(msg);
    console.log("TASK REVIEW RAW:", JSON.stringify(parsed));
    if (!parsed) return null;
    if (!["amend", "keep", "delete", "unrelated"].includes(parsed.decision)) {
      parsed.decision = "unrelated";
    }
    return parsed;
  } catch (e) {
    console.error("task reviewAdd error:", e?.message || e);
    return null;
  }
}

// ---- ADD (other): delegate to calendar_action (confirm-first invite) ---------
async function handleAddOther(ctx, info, assignee) {
  const { number, send } = ctx;
  if (!ctx.hasSkill("calendar_action", "startCreate")) {
    await send(number, reply(ctx.lang).calendarUnavailable());
    return;
  }
  const title = (info.title || ctx.quoted?.text || "").trim() || null;
  // 5-min invite at 15:00 on the due date; calendar owns the confirm/email-chase.
  return ctx.callSkill("calendar_action", "startCreate", {
    action: "create",
    title,
    participants: [{ name: assignee.name, email: assignee.email }],
    start_iso: threePmOnDue(info.due_iso),
    duration_min: 5,
    summary: title || "",
  });
}

// ---- LIST --------------------------------------------------------------------
async function handleList(ctx) {
  const { env, number, send } = ctx;
  let items;
  try {
    items = await listTasks(env);
  } catch (e) {
    console.error("Tasks list error:", e?.response?.data || e?.message || e);
    await send(number, reply(ctx.lang).failed());
    return;
  }
  const open = openWithTitle(items);
  if (!open.length) {
    await send(number, reply(ctx.lang).empty());
    return;
  }
  const rows = open.map((t) => ({
    title: t.title.trim(),
    when: localizeDueDate(ctx.lang, t.due),
  }));
  await send(number, reply(ctx.lang).formatList(rows));
}

// ---- COMPLETE: confirm-first session -----------------------------------------
async function handleComplete(ctx, info) {
  const { env, number, send, sessions, remoteJid } = ctx;
  let items;
  try {
    items = await listTasks(env);
  } catch (e) {
    console.error("Tasks list error:", e?.response?.data || e?.message || e);
    await send(number, reply(ctx.lang).failed());
    return;
  }
  const open = openWithTitle(items);
  if (!open.length) {
    await send(number, reply(ctx.lang).empty());
    return;
  }

  const match = await resolveTaskRef(ctx, info.task_ref, open);
  if (!match) {
    await send(number, reply(ctx.lang).notFound());
    return;
  }

  await sessions.set(
    remoteJid,
    {
      skill: "task_action",
      intent: "complete",
      stage: "await_confirmation",
      awaitFrom: "owner",
      lang: ctx.lang,
      data: { taskId: match.id, title: match.title },
    },
    600
  );
  await send(number, reply(ctx.lang).confirmComplete({ title: match.title }));
}

async function resumeComplete(ctx, session) {
  const { env, number, send, sessions, remoteJid } = ctx;
  const { taskId, title } = session.data || {};
  if (!taskId) {
    await sessions.clear(remoteJid);
    return;
  }

  const decision = await classifyConfirmation(ctx, {
    action: `mark the task "${title}" done`,
  });
  if (decision === "unrelated") return; // ignore chatter
  if (decision === "decline") {
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).keptOpen({ title }));
    return;
  }

  try {
    await completeTask(env, taskId);
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).completed({ title }));
  } catch (e) {
    console.error("Tasks complete error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).failed());
  }
}

async function resolveTaskRef(ctx, taskRef, open) {
  const { anthropic, model, owner } = ctx;
  const listText = open.map((t, i) => `${i + 1}. ${t.title.trim()}`).join("\n");
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 256,
      system: buildResolveRefSystem(owner),
      output_config: jsonFormat(RESOLVE_REF_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildResolveRefUser({ taskRef: taskRef || "", listText }),
        },
      ],
    });
    const idx = Number(readReply(msg)?.match_index);
    console.log("TASK RESOLVE RAW:", idx);
    if (!Number.isInteger(idx) || idx < 1 || idx > open.length) return null;
    const t = open[idx - 1];
    return { id: t.id, title: t.title.trim() };
  } catch (e) {
    console.error("task resolveRef error:", e?.message || e);
    return null;
  }
}

// LLM: does the latest message confirm/decline the pending action? Defaults to
// "unrelated" on doubt/error (the safe no-op).
async function classifyConfirmation(ctx, { action }) {
  const { anthropic, model, transcript, order } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 256,
      system: buildConfirmSystem(action),
      output_config: jsonFormat(CONFIRM_SCHEMA),
      messages: [
        { role: "user", content: buildConfirmUser({ transcript, latest: order }) },
      ],
    });
    const decision = readReply(msg)?.decision;
    console.log("TASK CONFIRM RAW:", decision);
    return decision === "confirm" || decision === "decline"
      ? decision
      : "unrelated";
  } catch (e) {
    console.error("task confirm classify error:", e?.message || e);
    return "unrelated";
  }
}
