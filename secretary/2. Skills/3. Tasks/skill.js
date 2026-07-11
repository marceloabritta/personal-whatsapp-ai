// ============================================================================
//  Skill "Tasks" (todo inbox) — LOGIC.
//  ONE list-aware resolver (`planTaskOps`) reads the conversation AND the owner's
//  open list, then emits a PLAN of ops. A little glue routes the ops:
//    - create (self)  : write a private Google Tasks item immediately, then keep a
//                       stateful window open (no re-tag) to correct/delete it.
//    - create (other) : a task FOR someone else must reach them by email, so we
//                       DELEGATE to calendar_action's `startCreate` (a 5-min invite
//                       at 15:00). Confirm-first; the lifecycle is owned by calendar.
//                       Capped at ONE third-party item per message (see plan).
//    - complete/edit/ : mutations of EXISTING stored tasks — confirm-first, ONE
//      delete           confirmation for the whole set.
//    - amend window   : an edit/delete of a JUST-touched task inside the stateful
//                       window is frictionless (no confirm), like the shipped amend.
//    - list           : read back the open tasks.
//  The same resolver drives the first (tagged) message AND every untagged follow-up
//  while a task window is open — so a batch, a correction, or a new task all work
//  without re-tagging. See New Features Plans/task-improvements.md.
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
  buildPlanSystem,
  buildPlanUser,
  buildConfirmSystem,
  buildConfirmUser,
  reply,
  localizeDueDate,
  threePmOnDue,
  PLAN_SCHEMA,
  CONFIRM_SCHEMA,
} from "./prompt.js";

export const manifest = {
  id: "task_action",
  description:
    "add one or more to-dos for the owner OR another person, list open tasks, complete/check off tasks, or edit/rename/reschedule/delete existing tasks",
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

// Open tasks only, with a non-blank title, normalized to { id, title, due }.
async function fetchOpen(ctx) {
  const items = await listTasks(ctx.env);
  return (items || [])
    .filter((t) => t.status !== "completed" && (t.title || "").trim())
    .map((t) => ({ id: t.id, title: t.title.trim(), due: t.due || null }));
}

// ---- The one resolver --------------------------------------------------------
// Reads the conversation + the numbered open list, returns a PLAN (see PLAN_SCHEMA).
async function planTaskOps(ctx, open) {
  const { anthropic, model, owner, order, transcript, nowStr, contact, quoted } =
    ctx;
  const listText = open
    .map(
      (t, i) =>
        `${i + 1}. ${t.title}${t.due ? ` (due ${localizeDueDate("en", t.due)})` : ""}`
    )
    .join("\n");
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 1500,
    system: buildPlanSystem(owner),
    output_config: jsonFormat(PLAN_SCHEMA),
    messages: [
      {
        role: "user",
        content: buildPlanUser(owner, {
          order,
          transcript,
          nowStr,
          contact,
          quoted,
          listText,
        }),
      },
    ],
  });
  const plan = readReply(msg);
  console.log("TASK PLAN RAW:", JSON.stringify(plan));
  return plan;
}

// A meaningful third-party assignee (has a name or email, and isn't the owner).
function otherAssignee(ctx, assignee) {
  const a = assignee;
  if (!a || (!a.name && !a.email)) return null;
  const owner = String(ctx.owner || "").trim().toLowerCase();
  const nm = String(a.name || "").trim().toLowerCase();
  if (nm && (nm === owner || nm === "me" || nm === "myself" || nm === "eu"))
    return null;
  return { name: a.name || null, email: a.email || null };
}

// 1-based index into `open` → { id, title, due }, or null if out of range.
function openAt(open, idx) {
  const n = Number(idx);
  if (!Number.isInteger(n) || n < 1 || n > open.length) return null;
  return open[n - 1];
}

// ---- Entry point -------------------------------------------------------------
// ctx (from the orchestrator): { owner, tag, anthropic, model, order, transcript,
//   nowStr, contact, remoteJid, number, fromMe, quoted, env, evolution, send,
//   sessions, session, lang, hasSkill, callSkill }
export async function run(ctx) {
  const { number, send, session } = ctx;

  // CONTINUATIONS owned by this skill (set by the orchestrator on a continuation).
  if (session?.skill === "task_action" && session.stage === "await_confirmation")
    return resumeConfirm(ctx, session);
  if (session?.skill === "task_action" && session.stage === "engaged")
    return resumeEngaged(ctx, session);

  // FRESH (tagged) message.
  let open;
  try {
    open = await fetchOpen(ctx);
  } catch (e) {
    console.error("Tasks list error:", e?.response?.data || e?.message || e);
    await send(number, reply(ctx.lang).failed());
    return;
  }

  let plan;
  try {
    plan = await planTaskOps(ctx, open);
  } catch (e) {
    console.error("Tasks/plan error:", e);
    await send(number, reply(ctx.lang).thinkingError());
    return;
  }
  if (!plan) {
    await send(number, reply(ctx.lang).thinkingError());
    return;
  }

  return dispatchPlan(ctx, plan, open, { recent: [], fromEngaged: false });
}

// ---- Dispatch: turn a PLAN into work -----------------------------------------
// Shared by the fresh path and the stateful re-plan. Partitions ops, performs the
// immediate ones (create, in-window amend), and opens a confirm session for
// mutations of stored tasks.
async function dispatchPlan(ctx, plan, open, { recent, fromEngaged }) {
  const { number, send, sessions, remoteJid, lang } = ctx;
  const ops = Array.isArray(plan.ops) ? plan.ops : [];
  const recentById = new Map((recent || []).map((r) => [r.id, r]));

  const creates = [];
  const mutations = []; // confirm-first: { type, id, oldTitle, title?, due? }
  const frictionEdits = []; // in-window: { id, oldTitle, title, due }
  const frictionDeletes = []; // in-window: { id, title }
  const unresolved = []; // { ref, candidates: [{ title, when }] }

  for (const op of ops) {
    if (!op || typeof op !== "object") continue;
    if (op.kind === "create") {
      creates.push(op);
      continue;
    }
    if (!["complete", "edit", "delete"].includes(op.kind)) continue;

    const t = openAt(open, op.target_index);
    if (!t) {
      const cands = (Array.isArray(op.candidate_indices) ? op.candidate_indices : [])
        .map((i) => openAt(open, i))
        .filter(Boolean)
        .map((c) => ({ title: c.title, when: localizeDueDate(lang, c.due) }));
      unresolved.push({ ref: op.ref_text || null, candidates: cands });
      continue;
    }

    const inWindow = fromEngaged && recentById.has(t.id);
    if (inWindow && op.kind === "edit") {
      frictionEdits.push({
        id: t.id,
        oldTitle: t.title,
        title: (op.title || "").trim() || null,
        due: op.due_iso || null,
      });
    } else if (inWindow && op.kind === "delete") {
      frictionDeletes.push({ id: t.id, title: t.title });
    } else {
      mutations.push({
        type: op.kind,
        id: t.id,
        oldTitle: t.title,
        title: op.kind === "edit" ? (op.title || "").trim() || null : null,
        // undefined => leave due unchanged; a string => set it.
        due: op.kind === "edit" && op.due_iso ? op.due_iso : undefined,
      });
    }
  }

  // 1) CREATE immediately.
  const created = creates.length ? await handleCreates(ctx, creates) : null;
  const made = created?.made || [];

  // 2) FRICTIONLESS in-window amends (no confirm, like the shipped amend flow).
  //    Track how `recent` changes so the re-armed window shows current values.
  let nextRecent = (recent || []).map((r) => ({ ...r }));
  const amendedItems = [];
  for (const e of frictionEdits) {
    const patch = {};
    if (e.title && e.title !== e.oldTitle) patch.title = e.title;
    const newDue = toTasksDue(e.due);
    if (newDue) patch.due = newDue;
    if (patch.title === undefined && patch.due === undefined) continue;
    try {
      await patchTask(ctx.env, e.id, patch);
    } catch (err) {
      console.error("Tasks patch error:", err?.response?.data || err?.message || err);
      continue;
    }
    const cur = recentById.get(e.id);
    const finalTitle = patch.title || e.oldTitle;
    const finalDue = patch.due !== undefined ? patch.due : cur?.due ?? null;
    amendedItems.push({ title: finalTitle, when: localizeDueDate(lang, finalDue) });
    nextRecent = nextRecent.map((r) =>
      r.id === e.id ? { ...r, title: finalTitle, due: finalDue } : r
    );
  }
  const removedTitles = [];
  for (const d of frictionDeletes) {
    try {
      await deleteTask(ctx.env, d.id);
      removedTitles.push(d.title);
      nextRecent = nextRecent.filter((r) => r.id !== d.id);
    } catch (err) {
      console.error("Tasks delete error:", err?.response?.data || err?.message || err);
    }
  }

  // 3) NOTIFY on the immediate actions.
  if (made.length)
    await send(
      number,
      reply(lang).createdBatch(
        made.map((m) => ({ title: m.title, when: localizeDueDate(lang, m.due) }))
      )
    );
  if (created?.calendarUnavailable)
    await send(number, reply(lang).calendarUnavailable());
  if (created?.thirdPartyCapped)
    await send(number, reply(lang).thirdPartyCapped(created.otherCount));
  if (creates.length && !made.length && !created?.handedToCalendar && !created?.calendarUnavailable)
    await send(number, reply(lang).needTitle());
  if (amendedItems.length) await send(number, reply(lang).amended(amendedItems));
  if (removedTitles.length) await send(number, reply(lang).removed(removedTitles));

  // 4) CONFIRM-FIRST mutations take the session slot and end the turn.
  if (mutations.length) {
    return openConfirm(ctx, mutations, unresolved);
  }

  // 5) Surface unmatched refs (disambiguate if we have candidates, else ask).
  if (unresolved.length) {
    const u = unresolved.find((x) => x.candidates?.length) || unresolved[0];
    if (u.candidates?.length)
      await send(number, reply(lang).disambiguate(u.ref, u.candidates));
    else
      await send(
        number,
        reply(lang).notFound(unresolved.map((x) => x.ref).filter(Boolean))
      );
  }

  // 6) List, if asked.
  if (plan.list_requested) {
    if (open.length)
      await send(
        number,
        reply(lang).formatList(
          open.map((t) => ({ title: t.title, when: localizeDueDate(lang, t.due) }))
        )
      );
    else await send(number, reply(lang).empty());
  }

  // 7) Session state. If we handed a third-party invite to calendar, it owns the
  //    slot — don't clobber it. Otherwise (re)arm the stateful window if anything
  //    happened, so follow-ups need no tag.
  if (created?.handedToCalendar) return;

  const didSomething =
    made.length ||
    amendedItems.length ||
    removedTitles.length ||
    plan.list_requested ||
    unresolved.length ||
    (creates.length && !made.length); // asked to create but needs a title

  if (didSomething) {
    const armRecent = made.length ? made : nextRecent;
    await armEngaged(ctx, armRecent);
    return;
  }

  // Nothing actionable.
  if (fromEngaged) {
    if (plan.owner_done) await sessions.clear(remoteJid); // clean close
    return; // otherwise a silent no-op inside the window
  }
  await send(number, reply(lang).noAction());
}

// ---- CREATE ------------------------------------------------------------------
// Self items are written immediately; the FIRST third-party item is delegated to
// calendar_action (the rest are capped — the owner is asked to resend them).
async function handleCreates(ctx, creates) {
  const { env, quoted } = ctx;
  const self = [];
  const other = [];
  for (const c of creates) {
    const a = otherAssignee(ctx, c.assignee);
    (a ? other : self).push({ op: c, assignee: a });
  }

  const made = [];
  for (const s of self) {
    const title = (s.op.title || quoted?.text || "").trim();
    if (!title) continue;
    const due = toTasksDue(s.op.due_iso) || null;
    try {
      const task = await addTask(env, { title, due });
      made.push({ id: task.id, title, due });
    } catch (e) {
      console.error("Tasks add error:", e?.response?.data || e?.message || e);
    }
  }

  let handedToCalendar = false;
  let calendarUnavailable = false;
  let thirdPartyCapped = false;
  if (other.length) {
    if (!ctx.hasSkill("calendar_action", "startCreate")) {
      calendarUnavailable = true;
    } else {
      const first = other[0];
      const title = (first.op.title || quoted?.text || "").trim() || null;
      await ctx.callSkill("calendar_action", "startCreate", {
        action: "create",
        title,
        participants: [
          { name: first.assignee.name, email: first.assignee.email },
        ],
        start_iso: threePmOnDue(first.op.due_iso),
        duration_min: 5,
        summary: title || "",
      });
      handedToCalendar = true;
      if (other.length > 1) thirdPartyCapped = true;
    }
  }

  return {
    made,
    handedToCalendar,
    calendarUnavailable,
    thirdPartyCapped,
    otherCount: other.length,
  };
}

// ---- Confirm-first mutations (complete / edit / delete of stored tasks) -------
async function openConfirm(ctx, mutations, unresolved) {
  const { number, send, sessions, remoteJid, lang } = ctx;
  const missed = (unresolved || []).map((u) => u.ref).filter(Boolean);
  await sessions.set(
    remoteJid,
    {
      skill: "task_action",
      stage: "await_confirmation",
      awaitFrom: "owner",
      lang,
      // Store raw due_iso for edits; normalize at apply time.
      data: {
        mutations: mutations.map((m) => ({
          type: m.type,
          id: m.id,
          oldTitle: m.oldTitle,
          title: m.title ?? null,
          ...(m.due !== undefined ? { due: m.due } : {}),
        })),
        missed,
      },
    },
    600
  );
  await send(number, reply(lang).confirmMutations(confirmView(mutations, lang), missed));
}

// Localize the new-due for edit lines so the confirm/applied views read in dd/mmm.
function confirmView(mutations, lang) {
  return mutations.map((m) => ({
    type: m.type,
    oldTitle: m.oldTitle,
    title: m.title ?? null,
    when: m.type === "edit" && m.due ? localizeDueDate(lang, toTasksDue(m.due)) : null,
  }));
}

async function resumeConfirm(ctx, session) {
  const { env, number, send, sessions, remoteJid, lang } = ctx;
  const data = session.data || {};
  const mutations = data.mutations || [];
  if (!mutations.length) {
    await sessions.clear(remoteJid);
    return;
  }

  const decision = await classifyConfirmation(ctx, {
    action: summarizeMutations(mutations),
  });

  if (decision === "confirm") {
    const done = [];
    const failed = [];
    for (const m of mutations) {
      try {
        if (m.type === "complete") await completeTask(env, m.id);
        else if (m.type === "delete") await deleteTask(env, m.id);
        else {
          const patch = {};
          if (m.title) patch.title = m.title;
          const nd = m.due ? toTasksDue(m.due) : undefined;
          if (nd) patch.due = nd;
          await patchTask(env, m.id, patch);
        }
        done.push(m);
      } catch (e) {
        console.error("Tasks apply error:", e?.response?.data || e?.message || e);
        failed.push(m);
      }
    }
    await send(
      number,
      reply(lang).mutationsApplied(confirmView(done, lang), confirmView(failed, lang))
    );
    // Keep the window open (tag-free) with edited items as `recent`, so a follow-up
    // "actually make it Tuesday" amends without another confirm.
    const armRecent = done
      .filter((m) => m.type === "edit")
      .map((m) => ({
        id: m.id,
        title: m.title || m.oldTitle,
        due: m.due ? toTasksDue(m.due) : null,
      }));
    await armEngaged(ctx, armRecent);
    return;
  }

  if (decision === "decline") {
    await sessions.clear(remoteJid);
    await send(number, reply(lang).declined());
    return;
  }

  // "unrelated": maybe a NEW task ("actually also add milk") arrived mid-confirm.
  // Re-plan; if it's a clear self-create, do it and RE-OFFER the pending confirm
  // (we never clobber the pending mutation). Otherwise a silent no-op.
  let open;
  try {
    open = await fetchOpen(ctx);
  } catch {
    return;
  }
  let plan;
  try {
    plan = await planTaskOps(ctx, open);
  } catch {
    return;
  }
  const selfCreates = (plan?.ops || []).filter(
    (o) => o?.kind === "create" && !otherAssignee(ctx, o.assignee)
  );
  if (!selfCreates.length) return; // truly unrelated

  const created = await handleCreates(ctx, selfCreates);
  if (created.made.length)
    await send(
      number,
      reply(lang).createdBatch(
        created.made.map((m) => ({
          title: m.title,
          when: localizeDueDate(lang, m.due),
        }))
      )
    );
  // Re-offer the still-pending confirmation and refresh its TTL.
  await send(
    number,
    reply(lang).confirmMutations(confirmView(mutations, lang), data.missed || [])
  );
  await sessions.set(remoteJid, session, 600);
}

function summarizeMutations(mutations) {
  const kinds = mutations.map((m) => m.type).join(", ");
  const n = mutations.length;
  return `apply ${n} change${n > 1 ? "s" : ""} to the owner's tasks (${kinds})`;
}

// ---- Stateful window: re-plan an untagged follow-up --------------------------
async function resumeEngaged(ctx, session) {
  const recent = session.data?.recent || [];
  let open;
  try {
    open = await fetchOpen(ctx);
  } catch (e) {
    console.error("Tasks list error:", e?.response?.data || e?.message || e);
    return; // stay silent inside the window on a transient error
  }
  let plan;
  try {
    plan = await planTaskOps(ctx, open);
  } catch (e) {
    console.error("Tasks/plan error:", e);
    return;
  }
  if (!plan) return;
  return dispatchPlan(ctx, plan, open, { recent, fromEngaged: true });
}

// Open/refresh the stateful, tag-free window over the just-touched tasks.
async function armEngaged(ctx, recent) {
  await ctx.sessions.set(
    ctx.remoteJid,
    {
      skill: "task_action",
      stage: "engaged",
      awaitFrom: "owner",
      lang: ctx.lang,
      data: {
        recent: (recent || []).map((r) => ({
          id: r.id,
          title: r.title,
          due: r.due ?? null,
        })),
      },
    },
    600
  );
}

// ---- LIST (direct capability) ------------------------------------------------
// Kept for callers that just want the open list; the planner also sets
// list_requested, handled inline in dispatchPlan.
async function handleList(ctx) {
  const { number, send, lang } = ctx;
  let open;
  try {
    open = await fetchOpen(ctx);
  } catch (e) {
    console.error("Tasks list error:", e?.response?.data || e?.message || e);
    await send(number, reply(lang).failed());
    return;
  }
  if (!open.length) {
    await send(number, reply(lang).empty());
    return;
  }
  await send(
    number,
    reply(lang).formatList(
      open.map((t) => ({ title: t.title, when: localizeDueDate(lang, t.due) }))
    )
  );
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

// Exposed as an internal capability in case another skill wants the open list.
export const capabilities = { list: handleList };
