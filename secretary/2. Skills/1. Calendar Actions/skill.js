// ============================================================================
//  Skill "Calendar Actions" — LOGIC.
//  Interprets the order with Claude and acts on Google Calendar:
//    - create  : make a new event and fire the invite email.
//    - delete  : cancel an event the owner REPLIED to (its calendar link), with
//                a confirm-first step.
//    - edit    : change an event the owner REPLIED to (move/relength/rename/add or
//                remove an attendee); asks for clarification when ambiguous.
//  Run by the orchestrator when the router picks "calendar_action".
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
// ============================================================================
import { google } from "googleapis";
import {
  buildSystem,
  buildUserPrompt,
  buildConfirmSystem,
  buildConfirmUser,
  buildCreateReviewSystem,
  buildCreateReviewUser,
  buildResolveSystem,
  buildResolveUser,
  buildEditSystem,
  buildEditUser,
  buildEditReviewSystem,
  buildEditReviewUser,
  CAL_SCHEMA,
  CONFIRM_SCHEMA,
  REVIEW_SCHEMA,
  RESOLVE_SCHEMA,
  EDIT_SCHEMA,
  EDIT_REVIEW_SCHEMA,
  reply,
  localizeDate,
} from "./prompt.js";

// Structured outputs: pass a JSON Schema so the API returns ONLY schema-valid
// JSON (no fences, no prose, no shape drift). Helper wraps the schema in the
// output_config.format shape and reads the guaranteed-valid reply — falling back
// to parseJsonReply if the model refused (empty/partial content) or is ever
// swapped to one without structured-output support.
function jsonFormat(schema) {
  return { format: { type: "json_schema", schema } };
}
function readReply(msg) {
  if (msg?.stop_reason === "refusal") {
    console.error("calendar: model refused the request");
    return null;
  }
  const out = (msg?.content || [])
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
  return parseJsonReply(out);
}

// Capabilities exposed to OTHER skills through the orchestrator's registry
// (ctx.callSkill) — NOT seen by the router. `startCreate` runs the full
// confirm-first create flow (draft -> "yes" -> invite) on a caller-supplied `info`;
// the caller (e.g. task_action, for a "task" assigned to someone else) never
// re-implements it. ctx is injected by the orchestrator; the session/continuation
// lifecycle is owned by calendar_action (the session it opens is tagged with our id).
export const capabilities = {
  startCreate: (ctx, info) => handleCreate(ctx, info),
};

export const manifest = {
  id: "calendar_action",
  description:
    "create, edit/reschedule, or delete/cancel a meeting or event in Google Calendar and notify the participants; also read/list what's on the calendar (answer questions like what's on tomorrow, anything Friday afternoon, or what's my next meeting)",
};

const CAL_TZ = "America/Sao_Paulo";

function calendarClient(env) {
  const o = new google.auth.OAuth2(
    env.GOOGLE_CLIENT_ID,
    env.GOOGLE_CLIENT_SECRET
  );
  o.setCredentials({ refresh_token: env.GOOGLE_REFRESH_TOKEN });
  return google.calendar({ version: "v3", auth: o });
}

function calId(env) {
  return env.GOOGLE_CALENDAR_ID || "primary";
}

// Robustly pull a JSON object out of an LLM reply. Tolerates ```json fences and
// stray prose, and — unlike a greedy /\{[\s\S]*\}/ match (first "{" to LAST "}",
// which corrupts on any trailing brace) — extracts the FIRST balanced {...}.
// Returns the parsed object, or null if nothing valid is found (never throws).
// (If the SDK is bumped to one supporting output_config.format, structured
// outputs would make this a straight JSON.parse — see PROJECT notes.)
function parseJsonReply(out) {
  if (!out) return null;
  let s = String(out).trim();
  const fenced = s.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  if (fenced) s = fenced[1].trim();
  try {
    return JSON.parse(s); // happy path: reply is exactly the JSON object
  } catch {
    /* fall through to balanced-brace scan */
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

async function createEvent(env, { title, emails, start_iso, end_iso, summary }) {
  const cal = calendarClient(env);
  // Idempotency: repeated "schedule this" (e.g. while testing) used to stack up
  // identical events, which then made "cancel this" leave siblings behind. If an
  // identical confirmed event already exists, reuse it instead of duplicating.
  const existing = await findConfirmedDuplicates(env, { title, startIso: start_iso });
  if (existing.length) return { ...existing[0], reused: true };
  const r = await cal.events.insert({
    calendarId: calId(env),
    sendUpdates: "all", // fires the invite email to the participants
    requestBody: {
      summary: title,
      description: summary || "",
      start: { dateTime: start_iso, timeZone: CAL_TZ },
      end: { dateTime: end_iso, timeZone: CAL_TZ },
      attendees: emails.map((email) => ({ email })),
    },
  });
  return r.data;
}

async function getEvent(env, eventId) {
  const cal = calendarClient(env);
  const r = await cal.events.get({ calendarId: calId(env), eventId });
  return r.data;
}

// Patch an existing event with only the changed fields and notify the attendees.
async function patchEvent(env, eventId, requestBody) {
  const cal = calendarClient(env);
  const r = await cal.events.patch({
    calendarId: calId(env),
    eventId,
    sendUpdates: "all", // email the attendees about the change
    requestBody,
  });
  return r.data;
}

// Find CONFIRMED events that are the same meeting: identical title and the exact
// same start instant. Used to (a) dedupe on create and (b) sweep every copy on
// delete, so cancelling a meeting doesn't leave duplicate rows behind.
async function findConfirmedDuplicates(env, { title, startIso, excludeId }) {
  if (!startIso) return [];
  const cal = calendarClient(env);
  const start = new Date(startIso).getTime();
  // Narrow window around the start instant to bound the query.
  const timeMin = new Date(start - 60000).toISOString();
  const timeMax = new Date(start + 60000).toISOString();
  const r = await cal.events.list({
    calendarId: calId(env),
    timeMin,
    timeMax,
    singleEvents: true,
    showDeleted: false,
    maxResults: 50,
  });
  return (r.data.items || []).filter(
    (e) =>
      e.status === "confirmed" &&
      e.summary === title &&
      e.start?.dateTime &&
      new Date(e.start.dateTime).getTime() === start &&
      e.id !== excludeId
  );
}

// Identify which real calendar event(s) a request targets (cancel or edit), by
// MATCHING the details captured from the conversation against the calendar — not by
// trusting a decoded link alone. Signals, per candidate:
//   +100  the event id decoded from the replied-to link (strong, explicit)
//   + 40  same start instant as the captured date/time
//   + 30  an attendee email overlaps a captured participant email
// A candidate is a confident match at score >= 70, i.e. the decoded id, OR
// start+email together. A bare same-start coincidence (40) is NOT enough to act on
// — it could be a different meeting in the same slot. Returns confident matches
// (deduped by id), each with its event data.
async function matchEventTargets(env, { eidEventId, startIso, emails }) {
  const cal = calendarClient(env);
  const emailSet = new Set((emails || []).map((e) => String(e).toLowerCase()));
  const startMs = startIso ? new Date(startIso).getTime() : null;
  const candidates = new Map(); // id -> event

  // The link the owner replied to (may be absent or a non-decodable short link).
  if (eidEventId) {
    try {
      const ev = await getEvent(env, eidEventId);
      if (ev && ev.status === "confirmed") candidates.set(ev.id, ev);
    } catch {
      /* stale/undecodable — rely on the captured details below */
    }
  }

  // Everything sitting at the captured start instant.
  if (startMs != null) {
    const r = await cal.events.list({
      calendarId: calId(env),
      timeMin: new Date(startMs - 60000).toISOString(),
      timeMax: new Date(startMs + 60000).toISOString(),
      singleEvents: true,
      showDeleted: false,
      maxResults: 50,
    });
    for (const e of r.data.items || []) {
      if (e.status === "confirmed" && e.start?.dateTime) candidates.set(e.id, e);
    }
  }

  const confident = [];
  for (const e of candidates.values()) {
    let score = 0;
    if (eidEventId && e.id === eidEventId) score += 100;
    if (startMs != null && e.start?.dateTime && new Date(e.start.dateTime).getTime() === startMs)
      score += 40;
    const attendees = (e.attendees || []).map((a) => String(a.email || "").toLowerCase());
    if (emailSet.size && attendees.some((a) => emailSet.has(a))) score += 30;
    if (score >= 70) confident.push(e);
  }
  return confident;
}

// Cancel the matched event(s) AND, at delete time, re-inspect the calendar for
// any confirmed duplicate of the same meeting (same title + start) and remove
// those too. Returns how many distinct events were removed. A 410 (already
// deleted) on any single id is treated as success — the goal is "no copy survives".
async function cancelMeeting(env, { eventIds = [], title, startIso }) {
  const cal = calendarClient(env);
  const ids = new Set(eventIds.filter(Boolean));
  try {
    const dupes = await findConfirmedDuplicates(env, { title, startIso });
    for (const d of dupes) ids.add(d.id);
  } catch (e) {
    // If the duplicate lookup fails, still delete the matched targets below.
    console.error("Calendar dup lookup error:", e?.response?.data || e?.message || e);
  }

  let deleted = 0;
  for (const id of ids) {
    try {
      await cal.events.delete({
        calendarId: calId(env),
        eventId: id,
        sendUpdates: "all", // notify attendees of the cancellation
      });
      deleted++;
    } catch (e) {
      const code = e?.code || e?.response?.status;
      if (code === 410) {
        deleted++; // already gone — that's the outcome we wanted
        continue;
      }
      throw e;
    }
  }
  return deleted;
}

// A Google Calendar link carries an `eid` = base64url("<eventId> <calendarId>").
// Decode it back to the eventId we can act on.
function resolveEventId(link) {
  const m = String(link || "").match(/[?&]eid=([^&\s]+)/);
  if (!m) return null;
  try {
    const b64 = m[1].replace(/-/g, "+").replace(/_/g, "/");
    const decoded = Buffer.from(b64, "base64").toString("utf8");
    const eventId = decoded.split(/\s+/)[0];
    return eventId || null;
  } catch {
    return null;
  }
}

async function interpret(ctx) {
  const { owner, anthropic, model, order, transcript, nowStr, contact, quoted } =
    ctx;
  const system = buildSystem(owner);
  const prompt = buildUserPrompt(owner, {
    order,
    transcript,
    nowStr,
    contact,
    quoted,
  });
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 4096,
    system,
    output_config: jsonFormat(CAL_SCHEMA),
    messages: [{ role: "user", content: prompt }],
  });
  const info = readReply(msg);
  console.log("CALENDAR RAW:", JSON.stringify(info));
  return info;
}

// ctx (from the orchestrator): { owner, tag, anthropic, model, order, transcript,
//   nowStr, contact, number, quoted, env, send }
export async function run(ctx) {
  const { number, send, session } = ctx;

  // CONTINUATION: resume a pending confirmation (e.g. "yes" to a cancellation).
  // Set by the orchestrator only when this message replies to the secretary's prompt.
  if (session?.intent === "delete" && session.stage === "await_confirmation") {
    return resumeDelete(ctx, session);
  }
  if (session?.intent === "create" && session.stage === "await_info") {
    return resumeInfo(ctx, session);
  }
  if (session?.intent === "create" && session.stage === "await_confirmation") {
    return resumeCreate(ctx, session);
  }
  if (session?.intent === "edit" && session.stage === "await_clarification") {
    return resumeEditClarify(ctx, session);
  }
  if (session?.intent === "edit" && session.stage === "await_confirmation") {
    return resumeEditConfirm(ctx, session);
  }

  let info;
  try {
    info = await interpret(ctx);
  } catch (e) {
    console.error("Calendar/Claude error:", e);
    await send(number, reply(ctx.lang).thinkingError());
    return;
  }

  if (info?.action === "delete") return handleDelete(ctx, info);
  if (info?.action === "create") return handleCreate(ctx, info);
  if (info?.action === "edit") return handleEdit(ctx, info);
  if (info?.action === "list") return handleList(ctx, info);

  await send(number, reply(ctx.lang).noAction({ summary: info?.summary }));
}

// ---- CREATE ----------------------------------------------------------------
// Create is fully STATEFUL and CONFIRM-FIRST. The flow always converges on a
// session: interpret (broad) -> if anything required is missing, a FOCUSED second
// LLM pass re-inspects the chat precisely for it -> still missing? open a gathering
// session and ASK, listening to ANY participant (awaitFrom:"any") until secure ->
// once complete, show the draft and wait for the owner's "yes" before writing to
// Google. Fallbacks (duration 45m, title from topic/names) never count as missing.
async function handleCreate(ctx, info) {
  const resolved = await resolveDraft(ctx, draftFromInfo(ctx, info));
  await advanceCreate(ctx, resolved);
}

// Required to create: a date/time, at least one attendee, and an email for EVERY
// attendee. Everything else has a fallback and never blocks.
function missingOf(draft) {
  return {
    noTime: !draft.start_iso,
    noAttendees: draft.participants.length === 0,
    emailNames: draft.participants
      .filter((p) => !p.email)
      .map((p) => p.name)
      .filter(Boolean),
  };
}

function isComplete(m) {
  return !m.noTime && !m.noAttendees && m.emailNames.length === 0;
}

function sameMissing(a, b) {
  return (
    a.noTime === b.noTime &&
    a.noAttendees === b.noAttendees &&
    a.emailNames.length === b.emailNames.length &&
    a.emailNames.every((n) => b.emailNames.includes(n))
  );
}

// The FOCUSED second pass: given what's missing, re-inspect the chat + latest
// message precisely for those fields and merge whatever it resolves. No LLM call
// when nothing is missing. Used both after the broad extraction and on each
// gathering message.
async function resolveDraft(ctx, draft) {
  const m = missingOf(draft);
  if (isComplete(m)) return draft;
  const patch = await inspectMissing(ctx, draft, m);
  return mergeDraft(ctx, draft, patch);
}

// Decide the next step from a draft: complete -> confirm; otherwise open (or
// refresh) the gathering session and ask precisely for what's still missing.
async function advanceCreate(ctx, draft) {
  const m = missingOf(draft);
  if (isComplete(m)) {
    await openCreateConfirm(ctx, draft);
    return;
  }
  await openInquiry(ctx, draft, m);
}

// Normalize an interpret()/review() result into the draft we store, render, and
// eventually insert. Applies the title fallback: inferred topic, else Owner & names.
function draftFromInfo(ctx, info) {
  const { owner, contact } = ctx;
  const participants = (Array.isArray(info.participants) ? info.participants : [])
    .map((p) => ({ name: p?.name || null, email: p?.email || null }))
    .filter((p) => p.name || p.email);
  const names = participants.map((p) => p.name).filter(Boolean);
  const title =
    String(info.title || "").trim() ||
    `${owner} & ${names.join(" & ") || contact || "Guest"}`;
  const duration_min = Number(info.duration_min) > 0 ? Number(info.duration_min) : 45;
  return {
    title,
    participants,
    start_iso: info.start_iso || null,
    duration_min,
    summary: info.summary || "",
  };
}

function draftEmails(draft) {
  return (draft.participants || []).map((p) => p?.email).filter(Boolean);
}

// Open (or refresh) the confirmation session holding the draft and show it. The
// owner's next plain message resumes via resumeCreate. 10-min window to answer.
async function openCreateConfirm(ctx, draft) {
  const { number, send, sessions, remoteJid } = ctx;
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "create",
      stage: "await_confirmation",
      awaitFrom: "owner", // only the owner approves their own event
      lang: ctx.lang, // reply to the continuation in the flow's language
      data: { draft },
    },
    600
  );
  await send(
    number,
    reply(ctx.lang).createConfirm({
      title: draft.title,
      emails: draftEmails(draft).join(", "),
      when: localizeDate(ctx.lang, draft.start_iso),
      duration: draft.duration_min,
    })
  );
}

// Actually write the confirmed draft to Google and report back.
async function createFromDraft(ctx, draft) {
  const { env, number, send } = ctx;
  const emails = draftEmails(draft);
  const end_iso = new Date(
    new Date(draft.start_iso).getTime() + draft.duration_min * 60000
  ).toISOString();
  const ev = await createEvent(env, {
    title: draft.title,
    emails,
    start_iso: draft.start_iso,
    end_iso,
    summary: draft.summary,
  });
  await send(
    number,
    reply(ctx.lang).createDone({
      reused: !!ev.reused,
      title: draft.title,
      emails: emails.join(", "),
      when: localizeDate(ctx.lang, draft.start_iso),
      duration: draft.duration_min,
      link: ev.htmlLink || "",
    })
  );
}

// Resume a pending create. Runs for EVERY owner message while the session is open:
// classify + (if a change) re-draft in one call, then act. Silent on chatter.
async function resumeCreate(ctx, session) {
  const { number, send, sessions, remoteJid } = ctx;
  const draft = session.data?.draft;
  if (!draft) {
    await sessions.clear(remoteJid);
    return;
  }

  const review = await reviewCreate(ctx, draft);
  if (!review || review.decision === "unrelated") return; // not for us — ignore

  if (review.decision === "cancel") {
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).createCancelled({ title: draft.title }));
    return;
  }

  if (review.decision === "modify") {
    // Re-route the revised draft: re-show the confirm, or chase a newly-missing
    // email exactly like a fresh order (a change may drop an attendee's email).
    return advanceCreate(ctx, applyDraftUpdate(ctx, draft, review));
  }

  // decision === "confirm"
  try {
    await createFromDraft(ctx, draft);
    await sessions.clear(remoteJid);
  } catch (e) {
    console.error("Calendar error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).createGoogleError());
  }
}

// Merge a "modify" review onto the current draft: prefer the review's fields, fall
// back to the previous draft for anything it didn't return, then re-normalize.
function applyDraftUpdate(ctx, prev, review) {
  const participants =
    Array.isArray(review.participants) && review.participants.length
      ? review.participants
      : prev.participants;
  return draftFromInfo(ctx, {
    title: review.title ?? prev.title,
    participants,
    start_iso: review.start_iso ?? prev.start_iso,
    duration_min: review.duration_min ?? prev.duration_min,
    summary: review.summary ?? prev.summary,
  });
}

// LLM: is the latest owner message a confirm / modify / cancel of the pending draft?
// Returns the parsed review (with a normalized decision) or null on doubt/error —
// null is treated by the caller as "ignore silently", the safe default.
async function reviewCreate(ctx, draft) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 4096,
      system: buildCreateReviewSystem(owner),
      output_config: jsonFormat(REVIEW_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildCreateReviewUser({
            draftJson: JSON.stringify(draft),
            transcript,
            latest: order,
            nowStr,
          }),
        },
      ],
    });
    const parsed = readReply(msg);
    console.log("CREATE REVIEW RAW:", JSON.stringify(parsed));
    if (!parsed) return null;
    if (!["confirm", "modify", "cancel", "unrelated"].includes(parsed.decision)) {
      parsed.decision = "unrelated";
    }
    return parsed;
  } catch (e) {
    console.error("create review error:", e?.message || e);
    return null; // on error, do nothing (safe)
  }
}

// ---- CREATE: stateful gathering --------------------------------------------
// Ask precisely for what's missing and keep the session open, listening to ANY
// participant (awaitFrom:"any"), until every required field is secure. Each
// incoming message re-runs the focused resolver; progress → ask for the rest,
// complete → confirm, nothing new → stay silent (chatter).
async function openInquiry(ctx, draft, m) {
  const { number, send, sessions, remoteJid } = ctx;
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "create",
      stage: "await_info",
      awaitFrom: "any", // the owner OR any attendee in the chat may answer
      lang: ctx.lang, // reply to the continuation in the flow's language
      data: { draft },
    },
    600
  );
  await send(number, reply(ctx.lang).inquiry(m));
}

const normName = (s) => String(s || "").trim().toLowerCase();

// Merge a resolver patch onto the draft: take start_iso if provided; fill emails
// for known attendees by name and append any newly-identified attendees. Robust
// single case: one attendee still missing an email + one email in the patch →
// assign it directly even if the names don't line up. Re-normalized at the end.
function mergeDraft(ctx, prev, patch) {
  if (!patch) return prev;
  const participants = prev.participants.map((p) => ({ ...p }));

  if (Array.isArray(patch.participants)) {
    const clean = patch.participants.filter((p) => p && (p.name || p.email));
    const missing = participants.filter((p) => !p.email);
    const patchEmails = clean.filter(
      (p) => typeof p.email === "string" && p.email.includes("@")
    );
    // Bare-email fallback: one attendee still missing + one UN-named email → assign
    // it directly. A named email goes through the by-name matcher below instead.
    if (missing.length === 1 && patchEmails.length === 1 && !patchEmails[0].name) {
      missing[0].email = patchEmails[0].email;
    } else {
      for (const pp of clean) {
        const idx = participants.findIndex(
          (p) => pp.name && normName(p.name) === normName(pp.name)
        );
        if (idx >= 0) {
          if (!participants[idx].email && pp.email) participants[idx].email = pp.email;
        } else if (pp.name || pp.email) {
          participants.push({ name: pp.name || null, email: pp.email || null });
        }
      }
    }
  }

  return draftFromInfo(ctx, {
    title: prev.title,
    participants,
    start_iso: patch.start_iso ?? prev.start_iso,
    duration_min: prev.duration_min,
    summary: prev.summary,
  });
}

// Resume a gathering session. Runs for EVERY owner/contact message while open:
// re-inspect precisely for what's still missing, merge, then ask for the rest or
// move to confirm. Silent when the message resolves nothing new (chatter).
async function resumeInfo(ctx, session) {
  const { sessions, remoteJid } = ctx;
  const draft = session.data?.draft;
  if (!draft) {
    await sessions.clear(remoteJid);
    return;
  }

  const before = missingOf(draft);
  if (isComplete(before)) return advanceCreate(ctx, draft);

  const patch = await inspectMissing(ctx, draft, before);
  const updated = mergeDraft(ctx, draft, patch);
  const after = missingOf(updated);

  if (sameMissing(before, after)) return; // nothing new resolved — ignore silently
  await advanceCreate(ctx, updated); // progressed → ask for the rest, or confirm
}

// LLM: the focused second pass. Told exactly what's missing, it resolves precisely
// those fields from the conversation + latest message. Returns the patch, or null
// on doubt/error (caller then keeps the draft unchanged → stays silent / re-asks).
async function inspectMissing(ctx, draft, m) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 2048,
      system: buildResolveSystem(owner),
      output_config: jsonFormat(RESOLVE_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildResolveUser({
            draftJson: JSON.stringify(draft),
            needsTime: m.noTime,
            needsAttendees: m.noAttendees,
            needEmailFor: m.emailNames,
            transcript,
            latest: order,
            nowStr,
          }),
        },
      ],
    });
    const patch = readReply(msg);
    console.log("RESOLVE RAW:", JSON.stringify(patch));
    return patch;
  } catch (e) {
    console.error("resolve error:", e?.message || e);
    return null;
  }
}

// ---- EDIT / RESCHEDULE (Phase B) -------------------------------------------
// Change an EXISTING event the owner REPLIED to (its calendar link): move it,
// relength it, rename it, add/remove an attendee. Like create, edit is now
// CONFIRM-FIRST and stays open: the change is applied to a DRAFT of the event's
// target state, shown for confirmation, and only written to Google on the owner's
// "yes". While the confirm session is open the owner can keep refining the same
// event tagless ("actually 4:30", "also add bruno@x.com") — same review machinery
// as create (confirm | modify | cancel | unrelated). Nothing is written until "yes".

// Compact view of the real event handed to the focused edit pass.
function eventForLLM(ev) {
  const start = ev.start?.dateTime || null;
  const end = ev.end?.dateTime || null;
  const duration_min =
    start && end ? Math.round((new Date(end) - new Date(start)) / 60000) : null;
  return {
    title: ev.summary || "",
    start_iso: start,
    end_iso: end,
    duration_min,
    attendees: (ev.attendees || []).map((a) => a.email).filter(Boolean),
  };
}

// The editable DRAFT = the event's target state. Seeded from the current event, then
// each requested change is folded in; the confirm writes it to Google.
function editDraftFromEvent(ev) {
  const start = ev.start?.dateTime || null;
  const end = ev.end?.dateTime || null;
  const duration_min =
    start && end ? Math.round((new Date(end) - new Date(start)) / 60000) : 45;
  return {
    title: ev.summary || "",
    start_iso: start,
    duration_min,
    summary: ev.description || "",
    emails: (ev.attendees || []).map((a) => a.email).filter(Boolean),
  };
}

// Does the patch actually change anything? (An all-null/empty patch means the model
// couldn't extract a change — distinct from a `clarify` question.)
function hasEditChange(p) {
  return !!(
    p.new_start_iso ||
    Number(p.new_duration_min) > 0 ||
    p.new_title ||
    (typeof p.new_summary === "string" && p.new_summary.trim()) ||
    (Array.isArray(p.add_emails) && p.add_emails.length) ||
    (Array.isArray(p.remove_emails) && p.remove_emails.length)
  );
}

// Fold a change patch onto the draft (immutably): overwrite the touched fields, merge
// attendees (case-insensitive remove, then dedup add). Untouched fields carry over.
function applyPatchToDraft(draft, patch) {
  const d = { ...draft, emails: [...draft.emails] };
  if (patch.new_title) d.title = patch.new_title;
  if (typeof patch.new_summary === "string" && patch.new_summary.trim())
    d.summary = patch.new_summary;
  if (patch.new_start_iso) d.start_iso = patch.new_start_iso;
  if (Number(patch.new_duration_min) > 0) d.duration_min = Number(patch.new_duration_min);

  const remove = new Set(
    (patch.remove_emails || []).map((e) => String(e || "").trim().toLowerCase())
  );
  d.emails = d.emails.filter((e) => !remove.has(String(e).toLowerCase()));
  const have = new Set(d.emails.map((e) => String(e).toLowerCase()));
  for (const e of (patch.add_emails || []).map((x) => String(x || "").trim()).filter(Boolean)) {
    if (!have.has(e.toLowerCase())) {
      d.emails.push(e);
      have.add(e.toLowerCase());
    }
  }
  return d;
}

// The draft rendered as an "event" for the review LLM (so it judges against the
// currently-proposed target, not the original).
function draftAsEventJson(d) {
  const end_iso = d.start_iso
    ? new Date(new Date(d.start_iso).getTime() + d.duration_min * 60000).toISOString()
    : null;
  return JSON.stringify({
    title: d.title,
    start_iso: d.start_iso,
    end_iso,
    duration_min: d.duration_min,
    attendees: d.emails,
  });
}

// The focused EDIT extraction (first pass): given the real event + the change request,
// resolve only the changed fields (or a clarify question). Throws on API error (callers
// handle it); returns null on a refusal/unparseable reply.
async function interpretEdit(ctx, ev) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 2048,
    system: buildEditSystem(owner),
    output_config: jsonFormat(EDIT_SCHEMA),
    messages: [
      {
        role: "user",
        content: buildEditUser({
          eventJson: JSON.stringify(eventForLLM(ev)),
          transcript,
          latest: order,
          nowStr,
        }),
      },
    ],
  });
  const patch = readReply(msg);
  console.log("EDIT RAW:", JSON.stringify(patch));
  return patch;
}

// The confirm-step review (runs for every owner message while confirming): one call
// that BOTH classifies (confirm | modify | cancel | unrelated) AND, for a modify,
// returns the further change to fold in. Null on doubt/error → caller ignores silently.
async function reviewEdit(ctx, draft) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 2048,
      system: buildEditReviewSystem(owner),
      output_config: jsonFormat(EDIT_REVIEW_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildEditReviewUser({
            eventJson: draftAsEventJson(draft),
            transcript,
            latest: order,
            nowStr,
          }),
        },
      ],
    });
    const parsed = readReply(msg);
    console.log("EDIT REVIEW RAW:", JSON.stringify(parsed));
    if (!parsed) return null;
    if (!["confirm", "modify", "cancel", "unrelated"].includes(parsed.decision)) {
      parsed.decision = "unrelated";
    }
    return parsed;
  } catch (e) {
    console.error("edit review error:", e?.message || e);
    return null; // on error, do nothing (safe)
  }
}

// Open (or refresh) the confirm session holding the draft, and show the target state.
// The owner's next plain message resumes via resumeEditConfirm. 10-min window.
async function openEditConfirm(ctx, eventId, draft) {
  const { number, send, sessions, remoteJid } = ctx;
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "edit",
      stage: "await_confirmation",
      awaitFrom: "owner", // only the owner approves changes to their event
      lang: ctx.lang,
      data: { eventId, draft },
    },
    600
  );
  await send(
    number,
    reply(ctx.lang).editConfirm({
      title: draft.title,
      emails: draft.emails.join(", "),
      when: localizeDate(ctx.lang, draft.start_iso),
      duration: draft.duration_min,
    })
  );
}

// Write the confirmed draft to Google (patch the existing event) and report back.
async function applyEditDraft(ctx, eventId, draft) {
  const { env, number, send } = ctx;
  const body = {
    summary: draft.title,
    description: draft.summary || "",
    attendees: draft.emails.map((email) => ({ email })),
  };
  if (draft.start_iso) {
    const endIso = new Date(
      new Date(draft.start_iso).getTime() + draft.duration_min * 60000
    ).toISOString();
    body.start = { dateTime: draft.start_iso, timeZone: CAL_TZ };
    body.end = { dateTime: endIso, timeZone: CAL_TZ };
  }

  const updated = await patchEvent(env, eventId, body);

  const finalStart = updated.start?.dateTime || draft.start_iso || null;
  const finalEnd = updated.end?.dateTime || null;
  const finalDur =
    finalStart && finalEnd
      ? Math.round((new Date(finalEnd) - new Date(finalStart)) / 60000)
      : draft.duration_min;
  await send(
    number,
    reply(ctx.lang).editDone({
      title: updated.summary || draft.title || "(untitled)",
      emails: (updated.attendees || []).map((a) => a.email).filter(Boolean).join(", "),
      when: localizeDate(ctx.lang, finalStart),
      duration: finalDur,
      link: updated.htmlLink || "",
    })
  );
}

async function handleEdit(ctx, info) {
  const { number, env, send, tag, quoted, sessions, remoteJid } = ctx;

  // Resolve the event to change the SAME way delete does: MATCH the event's identity
  // against the calendar, not just a decoded link. `info.start_iso` here is the event's
  // CURRENT start (the locator the extraction reads from the replied-to invite/summary or
  // the conversation — NOT the new time being requested; that change is extracted later by
  // interpretEdit). This works whether the owner replied to the invite (link), the
  // summary/confirm bubble (current start + email, no link), or a tagless request that
  // names who + when.
  const participants = Array.isArray(info?.participants) ? info.participants : [];
  const emails = participants.map((p) => p?.email).filter(Boolean);
  const startIso = info?.start_iso || null; // the event's CURRENT start, used to find it
  const eidEventId = resolveEventId(quoted?.calendarLink); // may be null

  // Same guard as delete: need the link, or start+email together.
  if (!eidEventId && !(startIso && emails.length)) {
    await send(number, reply(ctx.lang).editNeedSignal({ tag }));
    return;
  }

  let matches;
  try {
    matches = await matchEventTargets(env, { eidEventId, startIso, emails });
  } catch (e) {
    console.error("Calendar edit match error:", e?.response?.data || e?.message || e);
    await send(number, reply(ctx.lang).editCheckError());
    return;
  }
  if (!matches.length) {
    await send(number, reply(ctx.lang).editNoMatch());
    return;
  }

  // Matcher returns full, confirmed-only event resources; patch the primary (same
  // "primary" pick delete makes for display). The confirm-first step below shows the
  // target, so a wrong pick among same-slot dupes is catchable before any write.
  const ev = matches[0];
  const eventId = ev.id;

  let patch;
  try {
    patch = await interpretEdit(ctx, ev);
  } catch (e) {
    console.error("edit interpret error:", e?.message || e);
    await send(number, reply(ctx.lang).editCheckError());
    return;
  }
  if (!patch) {
    await send(number, reply(ctx.lang).editCheckError());
    return;
  }

  // Ambiguous / missing detail → ask, keep the event id, resume on the answer (which
  // then rolls into the confirm below).
  if (patch.clarify && !hasEditChange(patch)) {
    await sessions.set(
      remoteJid,
      {
        skill: "calendar_action",
        intent: "edit",
        stage: "await_clarification",
        awaitFrom: "owner", // only the owner edits their own event
        lang: ctx.lang,
        data: { eventId },
      },
      600 // 10 min window to answer
    );
    await send(number, reply(ctx.lang).editClarify(patch.clarify));
    return;
  }

  if (!hasEditChange(patch)) {
    await send(number, reply(ctx.lang).editNoChange());
    return;
  }

  // Confirm-first: fold the change into a draft and ask before writing anything.
  const draft = applyPatchToDraft(editDraftFromEvent(ev), patch);
  await openEditConfirm(ctx, eventId, draft);
}

// Resume a pending edit CLARIFICATION (the first request was ambiguous). Re-inspect the
// fresh event against the answer; once it resolves to a concrete change, roll into the
// confirm; else stay silent (chatter / still ambiguous) until answered or the TTL.
async function resumeEditClarify(ctx, session) {
  const { env, sessions, remoteJid } = ctx;
  const eventId = session.data?.eventId;
  if (!eventId) {
    await sessions.clear(remoteJid);
    return;
  }

  let ev;
  try {
    ev = await getEvent(env, eventId);
  } catch {
    await sessions.clear(remoteJid); // event vanished — drop the stale session
    return;
  }
  if (!ev || ev.status !== "confirmed") {
    await sessions.clear(remoteJid);
    return;
  }

  let patch;
  try {
    patch = await interpretEdit(ctx, ev);
  } catch (e) {
    console.error("edit clarify interpret error:", e?.message || e);
    return; // transient — keep the session, let them try again
  }
  if (!patch || !hasEditChange(patch)) return; // still ambiguous / chatter — wait

  const draft = applyPatchToDraft(editDraftFromEvent(ev), patch);
  await openEditConfirm(ctx, eventId, draft);
}

// Resume a pending edit CONFIRMATION. Runs for every owner message while open: one
// review call classifies + (for a change) re-drafts, then acts. Stays open across
// multiple refinements; silent on chatter; writes to Google only on "yes".
async function resumeEditConfirm(ctx, session) {
  const { number, env, send, sessions, remoteJid } = ctx;
  const { eventId, draft } = session.data || {};
  if (!eventId || !draft) {
    await sessions.clear(remoteJid);
    return;
  }

  const review = await reviewEdit(ctx, draft);
  if (!review || review.decision === "unrelated") return; // not for us — ignore

  if (review.decision === "cancel") {
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).editCancelled({ title: draft.title }));
    return;
  }

  if (review.decision === "modify") {
    // Ambiguous further change → ask and keep the session (draft unchanged).
    if (!hasEditChange(review) && review.clarify) {
      await openEditConfirm(ctx, eventId, draft); // refresh TTL, keep draft
      await send(number, reply(ctx.lang).editClarify(review.clarify));
      return;
    }
    if (!hasEditChange(review)) return; // nothing new resolved — stay silent
    const updated = applyPatchToDraft(draft, review);
    await openEditConfirm(ctx, eventId, updated); // re-show the revised draft, keep open
    return;
  }

  // decision === "confirm" — write it now.
  try {
    // Re-check the event still exists before patching (it may have been deleted).
    const ev = await getEvent(env, eventId);
    if (!ev || ev.status !== "confirmed") {
      await sessions.clear(remoteJid);
      await send(number, reply(ctx.lang).editNoMatch());
      return;
    }
    await applyEditDraft(ctx, eventId, draft);
    await sessions.clear(remoteJid);
  } catch (e) {
    console.error("Calendar edit patch error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).editGoogleError());
  }
}

// ---- DELETE ----------------------------------------------------------------
// Don't trust the link alone: gather what the conversation says about the event
// (start time, participant emails) PLUS the id decoded from any replied-to link,
// then MATCH that against the real calendar. Only open the confirmation SESSION
// when a confident match is found. The "yes" arrives as a continuation (handled
// by resumeDelete) — no @secretary tag needed.
async function handleDelete(ctx, info) {
  const { number, env, send, tag, quoted, sessions, remoteJid } = ctx;

  // Identity captured from the request/conversation, plus the link as one signal.
  const participants = Array.isArray(info?.participants) ? info.participants : [];
  const emails = participants.map((p) => p?.email).filter(Boolean);
  const startIso = info?.start_iso || null;
  const eidEventId = resolveEventId(quoted?.calendarLink); // may be null

  // Need at least one usable signal beyond a bare start time to be sure.
  if (!eidEventId && !(startIso && emails.length)) {
    await send(number, reply(ctx.lang).deleteNeedSignal({ tag }));
    return;
  }

  let matches;
  try {
    matches = await matchEventTargets(env, { eidEventId, startIso, emails });
  } catch (e) {
    console.error("Calendar match error:", e?.response?.data || e?.message || e);
    await send(number, reply(ctx.lang).deleteCheckError());
    return;
  }

  if (!matches.length) {
    await send(number, reply(ctx.lang).deleteNoMatch());
    return;
  }

  // Confident matches of the same meeting (dupes included). Describe them from the
  // first match; the confirm-time sweep re-checks the calendar and removes any copy.
  const primary = matches[0];
  const title = primary.summary || "(untitled)";
  const start = primary.start?.dateTime || startIso || null;
  const when = localizeDate(ctx.lang, primary.start?.dateTime || startIso);
  const ids = matches.map((e) => e.id);

  // Confirm-first: remember the matched ids + identity and ask. The owner can just
  // type "yes"/"no" (no reply, no tag); the secretary watches and ignores chatter.
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "delete",
      stage: "await_confirmation",
      awaitFrom: "owner", // only the owner confirms their own cancellation
      lang: ctx.lang, // reply to the "yes"/"no" in the flow's language
      data: { ids, title, when, start },
    },
    600 // 10 min window to confirm
  );
  await send(
    number,
    reply(ctx.lang).deleteConfirm({ title, when, count: ids.length })
  );
}

// Resume a pending cancellation. Called for EVERY owner message while the session
// is open — so we ask the LLM whether this message actually confirms/declines, and
// stay SILENT on normal chatter (no nagging, no accidental deletes).
async function resumeDelete(ctx, session) {
  const { number, env, send, sessions, remoteJid } = ctx;
  const { ids, title, when, start } = session.data || {};

  const decision = await classifyConfirmation(ctx, {
    action: `cancel the event "${title}"${when ? ` at ${when}` : ""}`,
  });

  if (decision === "unrelated") return; // not a response to us — ignore silently

  if (decision === "decline") {
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).deleteKeep({ title }));
    return;
  }

  // decision === "confirm"
  try {
    const n = await cancelMeeting(env, {
      eventIds: ids || [],
      title,
      startIso: start,
    });
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).deleteCancelled({ title, removed: n }));
  } catch (e) {
    console.error("Calendar delete error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).deleteGoogleError());
  }
}

// LLM judgment: does the latest message respond to a pending confirmation?
// Returns "confirm" | "decline" | "unrelated" (defaults to "unrelated" on doubt/error).
async function classifyConfirmation(ctx, { action }) {
  const { anthropic, model, transcript, order } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 1024,
      system: buildConfirmSystem(action),
      output_config: jsonFormat(CONFIRM_SCHEMA),
      messages: [
        { role: "user", content: buildConfirmUser({ transcript, latest: order }) },
      ],
    });
    const decision = readReply(msg)?.decision;
    console.log("CONFIRM RAW:", decision);
    return decision === "confirm" || decision === "decline"
      ? decision
      : "unrelated";
  } catch (e) {
    console.error("confirm classify error:", e?.message || e);
    return "unrelated"; // on error, do nothing (safe)
  }
}

// ---- LIST (read-only) ------------------------------------------------------
// Answer a read-only question about the schedule. The simplest action: no session,
// no confirm, no write — resolve the window (or a forward scan for "next"), fetch,
// and reply. The window comes from interpret() (list_mode + range_start_iso/
// range_end_iso); an unresolved window defaults to the rest of today.
async function handleList(ctx, info) {
  const { env, number, send, lang } = ctx;
  const now = Date.now();
  const cal = calendarClient(env);

  // "next meeting" → scan forward and show the first upcoming event.
  if (info?.list_mode === "next") {
    let items;
    try {
      const r = await cal.events.list({
        calendarId: calId(env),
        timeMin: new Date(now).toISOString(),
        timeMax: new Date(now + 14 * 86400000).toISOString(),
        singleEvents: true,
        orderBy: "startTime",
        showDeleted: false,
        maxResults: 10,
      });
      items = (r.data.items || []).filter((e) => e.status === "confirmed");
    } catch (e) {
      console.error("Calendar list(next) error:", e?.response?.data || e?.message || e);
      return send(number, reply(lang).listError());
    }
    const next = items.find((e) => e.start?.dateTime || e.start?.date);
    return send(number, reply(lang).listNext({ event: next ? toListItem(next) : null }));
  }

  // window mode: an explicit range if the LLM resolved one, else now → end of today.
  const parsedStart = info?.range_start_iso ? new Date(info.range_start_iso).getTime() : NaN;
  const startMs = Number.isFinite(parsedStart) ? parsedStart : now;
  const parsedEnd = info?.range_end_iso ? new Date(info.range_end_iso).getTime() : NaN;
  let endMs = Number.isFinite(parsedEnd) ? parsedEnd : endOfLocalDay(startMs);
  if (endMs <= startMs) endMs = endOfLocalDay(startMs); // guard an empty/backwards range

  let items;
  try {
    const r = await cal.events.list({
      calendarId: calId(env),
      timeMin: new Date(startMs).toISOString(),
      timeMax: new Date(endMs).toISOString(),
      singleEvents: true,
      orderBy: "startTime",
      showDeleted: false,
      maxResults: 50,
    });
    items = (r.data.items || []).filter((e) => e.status === "confirmed");
  } catch (e) {
    console.error("Calendar list error:", e?.response?.data || e?.message || e);
    return send(number, reply(lang).listError());
  }

  await send(
    number,
    reply(lang).listEvents({
      startMs,
      endMs,
      events: items.map(toListItem),
      capped: items.length >= 50,
    })
  );
}

// End-of-day (23:59:59.999) for the calendar TZ, in ms. São Paulo is a fixed -03:00
// offset (Brazil has no DST), matching the -03:00 used throughout this skill.
function endOfLocalDay(ms) {
  const [y, m, d] = new Intl.DateTimeFormat("en-CA", {
    timeZone: CAL_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  })
    .format(new Date(ms))
    .split("-");
  return new Date(`${y}-${m}-${d}T23:59:59.999-03:00`).getTime();
}

// Flatten a Google event into the locale-neutral shape the reply renderers need.
// All-day events carry a date (no dateTime); timed events carry start/end instants.
function toListItem(e) {
  const startIso = e.start?.dateTime || null;
  const endIso = e.end?.dateTime || null;
  const allDay = !startIso && !!e.start?.date;
  const dayMs = allDay
    ? new Date(`${e.start.date}T00:00:00-03:00`).getTime()
    : startIso
    ? new Date(startIso).getTime()
    : null;
  const durationMin =
    startIso && endIso ? Math.round((new Date(endIso) - new Date(startIso)) / 60000) : null;
  const emails = (e.attendees || []).map((a) => a.email).filter(Boolean);
  return { allDay, startIso, dayMs, title: e.summary || "", emails, durationMin };
}
