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
  buildCreateReviewSystem,
  buildCreateReviewUser,
  buildResolveSystem,
  buildResolveUser,
  buildEditSystem,
  buildEditUser,
  buildEditReviewSystem,
  buildEditReviewUser,
  CAL_SCHEMA,
  REVIEW_SCHEMA,
  RESOLVE_SCHEMA,
  EDIT_SCHEMA,
  EDIT_REVIEW_SCHEMA,
  reply,
  localizeDate,
  localizeWhen,
} from "./prompt.js";
// Structured outputs (jsonFormat/readReply), the shared confirm-first classifier and
// Google OAuth all live in the orchestrator's lib — see those files.
import { jsonFormat, readReply } from "../../1. Orchestrator/lib/llm.js";
import { classifyConfirmation } from "../../1. Orchestrator/lib/confirm.js";
import { googleAuth } from "../../1. Orchestrator/lib/google.js";

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

// The longest all-day span we will create from one order. A range is CLAMPED to it rather
// than refused: the owner sees "(31 dias)" in the confirm bubble and corrects it before
// anything is written. Confirm-first is the safety net; a new error string is not.
const MAX_ALL_DAY_DAYS = 31;

// ---- all-day day arithmetic (the ONE place dates are computed) ----------------
// `YYYY-MM-DD` in CAL_TZ — the same Intl.DateTimeFormat("en-CA") trick endOfLocalDay uses.
function localDayStr(ms) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: CAL_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(ms));
}

// The local day of an ISO instant, or null if it is missing/unparseable (localDayStr would
// throw on an Invalid Date, and a bad model value must never crash the flow).
function dayOfIso(iso) {
  const ms = Date.parse(iso || "");
  return Number.isFinite(ms) ? localDayStr(ms) : null;
}

// Shift a `YYYY-MM-DD` by n days. NOON, not midnight — cheap insurance against offset
// arithmetic landing on the wrong side of a day boundary.
function addDays(day, n) {
  return localDayStr(Date.parse(`${day}T12:00:00-03:00`) + n * 86400000);
}

function calendarClient(env) {
  return google.calendar({ version: "v3", auth: googleAuth(env) });
}

function calId(env) {
  return env.GOOGLE_CALENDAR_ID || "primary";
}

// The ONE events.insert in the repo. An ALL-DAY event is a different WIRE SHAPE, not a 24h
// timed block: `start:{date}/end:{date}` — the shape toListItem already recognises on the
// read side. `end_date` is EXCLUSIVE (Google's rule) and is computed by the caller.
async function createEvent(
  env,
  { title, emails, start_iso, end_iso, summary, all_day, start_date, end_date }
) {
  const cal = calendarClient(env);
  // Idempotency: repeated "schedule this" (e.g. while testing) used to stack up
  // identical events, which then made "cancel this" leave siblings behind. If an
  // identical confirmed event already exists, reuse it instead of duplicating.
  const existing = await findConfirmedDuplicates(env, {
    title,
    startIso: start_iso,
    allDay: all_day,
    startDate: start_date,
    endDate: end_date,
  });
  if (existing.length) return { ...existing[0], reused: true };
  const r = await cal.events.insert({
    calendarId: calId(env),
    sendUpdates: "all", // fires the invite email to the participants
    requestBody: {
      summary: title,
      description: summary || "",
      ...(all_day
        ? { start: { date: start_date }, end: { date: end_date } }
        : {
            start: { dateTime: start_iso, timeZone: CAL_TZ },
            end: { dateTime: end_iso, timeZone: CAL_TZ },
          }),
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
// An ALL-DAY event carries `start.date`, never `start.dateTime` — so the timed branch below
// is BLIND to one, and without this dedupe-on-create would silently stop working for exactly
// the events card 0822a8e0 adds. The all-day case queries the START day's window (a range
// still STARTS inside it) and matches BOTH dates: matching only the start would dedupe a
// Mon–Wed order against a Monday-only event. The other caller (the delete sweep) passes no
// all-day flag → falsy → today's exact behaviour, byte for byte.
async function findConfirmedDuplicates(
  env,
  { title, startIso, excludeId, allDay, startDate, endDate }
) {
  if (!startIso) return [];
  if (allDay && !(startDate && endDate)) return [];
  const cal = calendarClient(env);
  const start = new Date(startIso).getTime();
  // Narrow window to bound the query: the start DAY for an all-day event, otherwise a
  // narrow window around the start instant.
  const dayMs = allDay ? Date.parse(`${startDate}T12:00:00-03:00`) : null;
  const timeMin = allDay
    ? new Date(`${startDate}T00:00:00-03:00`).toISOString()
    : new Date(start - 60000).toISOString();
  const timeMax = allDay
    ? new Date(endOfLocalDay(dayMs)).toISOString()
    : new Date(start + 60000).toISOString();
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
      e.id !== excludeId &&
      (allDay
        ? e.start?.date === startDate && e.end?.date === endDate
        : e.start?.dateTime && new Date(e.start.dateTime).getTime() === start)
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
  const info = readReply(msg, "calendar");
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
    await ctx.sendFailure(number, reply(ctx.lang).thinkingError());
    return;
  }

  if (info?.action === "delete") return handleDelete(ctx, info);
  if (info?.action === "create") return handleCreate(ctx, info);
  if (info?.action === "edit") return handleEdit(ctx, info);
  if (info?.action === "list") return handleList(ctx, info);

  await ctx.sendFailure(number, reply(ctx.lang).noAction({ summary: info?.summary }));
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

// Required to create: a date/time, and an email for every named guest the owner has NOT
// told us he lacks one for. An event with ZERO outside guests is an ordinary, complete
// event. Everything else has a fallback and never blocks.
//
// The rule: a required field is legitimate only if a TRUTHFUL answer can satisfy it. The
// old ">= 1 attendee" invariant could not be satisfied by "nobody, it's just me", and the
// email requirement could not be satisfied by "I don't have hers" — so the owner could
// never leave the gathering loop. The email is still REQUIRED; it is now ANSWERABLE
// (`noEmail` = the owner said he hasn't got it), which is what was missing.
//
// ⚠ noTime STAYS. createFromDraft does `new Date(draft.start_iso)`: with a null start that
// is `new Date(null)` = the UNIX epoch, and the event lands in Google in 1970. This
// predicate is the ONLY thing guarding that write.
function missingOf(draft) {
  return {
    noTime: !draft.start_iso,
    emailNames: draft.participants
      .filter((p) => !p.email && !p.noEmail)
      .map((p) => p.name)
      .filter(Boolean),
  };
}

function isComplete(m) {
  return !m.noTime && m.emailNames.length === 0;
}

// The FOCUSED second pass: given what's missing, re-inspect the chat + latest
// message precisely for those fields and merge whatever it resolves. No LLM call
// when nothing is missing. Used both after the broad extraction and on each
// gathering message.
async function resolveDraft(ctx, draft) {
  const m = missingOf(draft);
  if (isComplete(m)) return draft;
  // gathering:false — on this immediate pass the "message" IS the order, so there is
  // nothing to classify; patch.decision is ignored here.
  const patch = await inspectMissing(ctx, draft, m, { gathering: false });
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
  // noEmail rides along: it is the owner's ANSWER ("I don't have hers"), and it must
  // survive every re-normalization or the email question comes back from the dead.
  const participants = (Array.isArray(info.participants) ? info.participants : [])
    .map((p) => ({
      name: p?.name || null,
      email: p?.email || null,
      noEmail: !!p?.noEmail,
    }))
    .filter((p) => p.name || p.email);
  const names = participants.map((p) => p.name).filter(Boolean);
  const title =
    String(info.title || "").trim() ||
    `${owner} & ${names.join(" & ") || contact || "Guest"}`;
  const duration_min = Number(info.duration_min) > 0 ? Number(info.duration_min) : 45;

  // ALL-DAY. `all_day_end_iso` is the LAST day the event still COVERS — INCLUSIVE. Both
  // sanity clamps live HERE and nowhere else, because this is the one normalizer every
  // merge path funnels through. Neither clamp has an error reply: the owner SEES the result
  // in the confirm bubble ("Dia todo", "(3 dias)") and corrects it before anything is written.
  const all_day = !!info.all_day;
  let all_day_end_iso = all_day ? info.all_day_end_iso || null : null;
  if (all_day_end_iso) {
    const startDay = dayOfIso(info.start_iso);
    const endDay = dayOfIso(all_day_end_iso);
    if (!startDay || !endDay || endDay <= startDay) {
      // The end day is BEFORE the start day (or unparseable) → drop it → a single-day event.
      all_day_end_iso = null;
    } else {
      const span =
        Math.round(
          (Date.parse(`${endDay}T12:00:00-03:00`) - Date.parse(`${startDay}T12:00:00-03:00`)) /
            86400000
        ) + 1;
      // Absurdly long range → clamp the SPAN. He sees "(31 dias)" and corrects it.
      if (span > MAX_ALL_DAY_DAYS) {
        all_day_end_iso = `${addDays(startDay, MAX_ALL_DAY_DAYS - 1)}T00:00:00-03:00`;
      }
    }
  }

  return {
    title,
    participants,
    start_iso: info.start_iso || null,
    duration_min,
    all_day,
    all_day_end_iso,
    summary: info.summary || "",
  };
}

function draftEmails(draft) {
  return (draft.participants || []).map((p) => p?.email).filter(Boolean);
}

// Named guests we will NOT be inviting: no email, and the owner has said he hasn't got
// one. They are named in the confirm draft and again in the confirmation — a person is
// never dropped silently.
function draftUninvited(draft) {
  return (draft.participants || []).filter((p) => !p.email && p.name).map((p) => p.name);
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
      when: localizeWhen(ctx.lang, draft),
      // An all-day event has no duration to state — "(1440 min)" is the bug, not the event.
      duration: draft.all_day ? null : draft.duration_min,
      uninvited: draftUninvited(draft),
    })
  );
}

// Actually write the confirmed draft to Google and report back.
//
// THE INCLUSIVE -> EXCLUSIVE CONVERSION HAPPENS HERE, AND ONLY HERE. The draft (and the
// model, and the confirm bubble) speak INCLUSIVE days: all_day_end_iso is the last day the
// event still covers. Google's `end.date` is EXCLUSIVE. A single day on 2026-07-14 is
// start 2026-07-14 / end 2026-07-15; Mon 13 -> Wed 15 is start 2026-07-13 / end 2026-07-16
// (a THURSDAY). Off by one is a 2-day event, or a zero-day one Google rejects.
async function createFromDraft(ctx, draft) {
  const { env, number, send } = ctx;
  const emails = draftEmails(draft);
  const all_day = !!draft.all_day;

  let start_date = null;
  let end_date = null;
  let end_iso = null;
  if (all_day) {
    // The DAY is derived from start_iso in CAL_TZ — which is why start_iso stays REQUIRED
    // and missingOf().noTime still guards the null-start → 1970 write.
    start_date = localDayStr(new Date(draft.start_iso).getTime());
    const last_date = draft.all_day_end_iso
      ? localDayStr(new Date(draft.all_day_end_iso).getTime())
      : start_date;
    end_date = addDays(last_date, 1); // EXCLUSIVE
  } else {
    end_iso = new Date(
      new Date(draft.start_iso).getTime() + draft.duration_min * 60000
    ).toISOString();
  }

  const ev = await createEvent(env, {
    title: draft.title,
    emails,
    start_iso: draft.start_iso,
    end_iso,
    summary: draft.summary,
    all_day,
    start_date,
    end_date,
  });
  await send(
    number,
    reply(ctx.lang).createDone({
      reused: !!ev.reused,
      title: draft.title,
      emails: emails.join(", "),
      when: localizeWhen(ctx.lang, draft),
      duration: all_day ? null : draft.duration_min,
      link: ev.htmlLink || "",
      uninvited: draftUninvited(draft),
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
    await ctx.sendFailure(number, reply(ctx.lang).createGoogleError());
  }
}

// Carry what the owner has ALREADY ANSWERED onto a fresh guest list: a person matched by
// name keeps any known email and their noEmail flag. Without it, a later "modify" would
// resurrect an email question he has already answered. Shared with mergeDraft.
function carryNoEmail(prevList, nextList) {
  return (nextList || []).map((p) => {
    const was = (prevList || []).find(
      (q) => q?.name && p?.name && normName(q.name) === normName(p.name)
    );
    return {
      name: p?.name || null,
      email: p?.email || was?.email || null,
      noEmail: !!(p?.noEmail || was?.noEmail),
    };
  });
}

// Merge a "modify" review onto the current draft: prefer the review's fields, fall
// back to the previous draft for anything it didn't return, then re-normalize.
// Array.isArray, NOT `.length` — an EMPTIED guest list is an ANSWER ("don't invite
// anyone"), not an absence of information, and it must stick. Only a missing list
// (null/undefined) means "the review said nothing about the guests".
function applyDraftUpdate(ctx, prev, review) {
  const participants = Array.isArray(review.participants)
    ? carryNoEmail(prev.participants, review.participants)
    : prev.participants;
  return draftFromInfo(ctx, {
    title: review.title ?? prev.title,
    participants,
    start_iso: review.start_iso ?? prev.start_iso,
    duration_min: review.duration_min ?? prev.duration_min,
    // `??`, so a modify that says nothing about them (a rename, an added guest) KEEPS them.
    // An explicit false still wins — that is the owner turning all-day off.
    all_day: review.all_day ?? prev.all_day,
    all_day_end_iso: review.all_day_end_iso ?? prev.all_day_end_iso,
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
    const parsed = readReply(msg, "calendar");
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

// Merge a resolver patch onto the draft: take start_iso if provided, and treat the
// resolver's guest list as AUTHORITATIVE — its own prompt promises the FULL list, so the
// list REPLACES the draft's rather than being appended to it. That is what makes an
// emptied list ("don't invite Laura") an answer, and it is what stops a substitution
// ("not Laura, Ana") from inviting BOTH — createEvent runs sendUpdates:"all", so an
// appended Laura is a real invite emailed to someone the owner removed.
// patch.participants === null still means "no information" → keep the previous list.
function mergeDraft(ctx, prev, patch) {
  if (!patch) return prev;
  let participants = prev.participants.map((p) => ({ ...p }));

  if (Array.isArray(patch.participants)) {
    const clean = patch.participants.filter((p) => p && (p.name || p.email));
    const missing = participants.filter((p) => !p.email);
    const patchEmails = clean.filter(
      (p) => typeof p.email === "string" && p.email.includes("@")
    );
    // Bare-email fallback — FIRST, and unchanged. One attendee still missing an email +
    // one UN-named email in the patch → assign it directly. This is how a guest who
    // answers with nothing but her address is understood; it must NOT go through the
    // replace below, which would overwrite her name with the patch's null.
    if (missing.length === 1 && patchEmails.length === 1 && !patchEmails[0].name) {
      missing[0].email = patchEmails[0].email;
    } else {
      // Otherwise the patch's list wins, carrying over what we already know (a known
      // email, and the noEmail the owner already answered) for anyone matched by name.
      participants = carryNoEmail(participants, clean);
    }
  }

  // The NEGATIVE channel: the names the owner has ANSWERED that he has no email for.
  // They stay on the guest list (so he can be TOLD they are not invited) but no longer
  // block completion — the requirement is satisfied by the answer, not by an address.
  const noEmailFor = new Set(
    (patch.no_email_for || []).map((n) => normName(n)).filter(Boolean)
  );
  for (const p of participants) {
    if (p.name && noEmailFor.has(normName(p.name))) p.noEmail = true;
  }

  return draftFromInfo(ctx, {
    title: prev.title,
    participants,
    start_iso: patch.start_iso ?? prev.start_iso,
    duration_min: prev.duration_min,
    // Carried from prev, NOT from the patch: the resolver never returns these and never
    // needs to (gathering chases a time and emails, never a range). Without these two lines
    // the all-day flag is silently dropped on every gathering merge.
    all_day: prev.all_day,
    all_day_end_iso: prev.all_day_end_iso,
    summary: prev.summary,
  });
}

// Resume a gathering session. Runs for EVERY owner/contact message while open
// (awaitFrom:"any" — the session hears the whole chat), so it must first decide WHAT the
// message is: an answer, a cancellation, or chatter. It used to infer that from a field
// diff — "did the missing set shrink?" — which meant every truthful answer the code had
// no field for ("nobody", "I don't have her email", "forget it") was met with TOTAL
// SILENCE and the owner could not escape the loop. It now asks, with the same
// confirm|modify|cancel|unrelated channel the rest of the repo uses (6. Flight Search's
// resumeInfo is the template).
async function resumeInfo(ctx, session) {
  const { number, sessions, remoteJid } = ctx;
  const draft = session.data?.draft;
  if (!draft) {
    await sessions.clear(remoteJid);
    return;
  }

  const before = missingOf(draft);
  if (isComplete(before)) return advanceCreate(ctx, draft);

  const patch = await inspectMissing(ctx, draft, before, { gathering: true });

  // The model refused, or the API failed. This used to be silence too — indistinguishable
  // from "your message wasn't for me". Say something.
  if (!patch) {
    await ctx.sendFailure(number, reply(ctx.lang).thinkingError());
    return;
  }
  if (patch.decision === "unrelated") return; // chatter — the ONLY silent exit left
  if (patch.decision === "cancel") {
    await sessions.clear(remoteJid); // DISARM: an abandoned draft must not be resurrectable
    await ctx.send(number, reply(ctx.lang).createCancelled({ title: draft.title }));
    return;
  }

  // confirm | modify — an ANSWER. Merge it and move: ask for what is still missing, or
  // show the confirm draft.
  return advanceCreate(ctx, mergeDraft(ctx, draft, patch));
}

// LLM: the focused second pass. Told exactly what's missing, it resolves precisely
// those fields from the conversation + latest message, AND (while gathering) classifies
// what the message is. Returns the patch, or null on doubt/error — resolveDraft keeps the
// draft unchanged, resumeInfo now REPORTS the null instead of swallowing it.
async function inspectMissing(ctx, draft, m, { gathering = false } = {}) {
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
            needEmailFor: m.emailNames,
            gathering,
            transcript,
            latest: order,
            nowStr,
          }),
        },
      ],
    });
    const patch = readReply(msg, "calendar");
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
//
// ⚠ THE ALL-DAY GUARD (corruption prevention — it does NOT make an all-day event editable).
// An all-day event has NO `start.dateTime`, so `start_iso` is null here and `duration_min`
// falls back to 45. `applyPatchToDraft` then FILLS start_iso in on "move a biópsia pra
// quarta", and `applyEditDraft` — guarded only by `if (draft.start_iso)` — would patch
// `start:{dateTime}` over it, SILENTLY CONVERTING the owner's all-day event into a
// 45-minute block. Carrying `all_day` onto the draft is what lets that guard refuse. The
// event is still renamed / re-invited; it is simply not MOVED. Rescheduling an all-day
// event is a separate card.
function editDraftFromEvent(ev) {
  const start = ev.start?.dateTime || null;
  const end = ev.end?.dateTime || null;
  const duration_min =
    start && end ? Math.round((new Date(end) - new Date(start)) / 60000) : 45;
  return {
    title: ev.summary || "",
    start_iso: start,
    duration_min,
    all_day: !ev.start?.dateTime && !!ev.start?.date,
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
  const patch = readReply(msg, "calendar");
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
    const parsed = readReply(msg, "calendar");
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
  // `!draft.all_day` — see editDraftFromEvent. Writing a `dateTime` start onto an ALL-DAY
  // event would convert it into a timed block. Refuse the write; change everything else.
  if (draft.start_iso && !draft.all_day) {
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
    await ctx.sendFailure(number, reply(ctx.lang).editCheckError());
    return;
  }
  if (!matches.length) {
    await ctx.sendFailure(number, reply(ctx.lang).editNoMatch());
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
    await ctx.sendFailure(number, reply(ctx.lang).editCheckError());
    return;
  }
  if (!patch) {
    await ctx.sendFailure(number, reply(ctx.lang).editCheckError());
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
      await ctx.sendFailure(number, reply(ctx.lang).editNoMatch());
      return;
    }
    await applyEditDraft(ctx, eventId, draft);
    await sessions.clear(remoteJid);
  } catch (e) {
    console.error("Calendar edit patch error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await ctx.sendFailure(number, reply(ctx.lang).editGoogleError());
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
    await ctx.sendFailure(number, reply(ctx.lang).deleteCheckError());
    return;
  }

  if (!matches.length) {
    await ctx.sendFailure(number, reply(ctx.lang).deleteNoMatch());
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
    who: "calendar",
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
    await ctx.sendFailure(number, reply(ctx.lang).deleteGoogleError());
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
      return ctx.sendFailure(number, reply(lang).listError());
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
    return ctx.sendFailure(number, reply(lang).listError());
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
  // External attendees only: drop the owner's own entry (self) and room resources.
  const emails = (e.attendees || [])
    .filter((a) => a.email && !a.self && !a.resource)
    .map((a) => a.email);
  return { allDay, startIso, dayMs, title: e.summary || "", emails, durationMin };
}
