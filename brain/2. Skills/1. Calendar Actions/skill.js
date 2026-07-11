// ============================================================================
//  Skill "Calendar Actions" — LOGIC.
//  Interprets the order with Claude and acts on Google Calendar:
//    - create  : make a new event and fire the invite email.
//    - delete  : cancel an event the owner REPLIED to (its calendar link), with
//                a confirm-first step.
//  (edit/reschedule comes in a later step.)
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
} from "./prompt.js";

export const manifest = {
  id: "calendar_action",
  description:
    "create, edit/reschedule, or delete/cancel a meeting or event in Google Calendar and notify the participants",
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

// Identify which real calendar event(s) a cancel request targets, by MATCHING the
// details captured from the conversation against the calendar — not by trusting a
// decoded link alone. Signals, per candidate:
//   +100  the event id decoded from the replied-to link (strong, explicit)
//   + 40  same start instant as the captured date/time
//   + 30  an attendee email overlaps a captured participant email
// A candidate is a confident match at score >= 70, i.e. the decoded id, OR
// start+email together. A bare same-start coincidence (40) is NOT enough to act on
// — it could be a different meeting in the same slot. Returns confident matches
// (deduped by id), each with its event data.
async function matchDeletionTargets(env, { eidEventId, startIso, emails }) {
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

function whenStr(dateTime) {
  return dateTime
    ? new Date(dateTime).toLocaleString("en-US", {
        timeZone: CAL_TZ,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "numeric",
        minute: "2-digit",
        hour12: true, // hh:mm AM/PM, no seconds
      })
    : "(no time)";
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
    max_tokens: 700,
    system,
    messages: [{ role: "user", content: prompt }],
  });
  const out = msg.content
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
  console.log("CALENDAR RAW:", out);
  return parseJsonReply(out);
}

// ctx (from the orchestrator): { owner, tag, anthropic, model, order, transcript,
//   nowStr, contact, number, quoted, env, send }
export async function run(ctx) {
  const { number, send, session } = ctx;

  // CONTINUATION: resume a pending confirmation (e.g. "yes" to a cancellation).
  // Set by the orchestrator only when this message replies to the brain's prompt.
  if (session?.intent === "delete" && session.stage === "await_confirmation") {
    return resumeDelete(ctx, session);
  }
  if (session?.intent === "create" && session.stage === "await_info") {
    return resumeInfo(ctx, session);
  }
  if (session?.intent === "create" && session.stage === "await_confirmation") {
    return resumeCreate(ctx, session);
  }

  let info;
  try {
    info = await interpret(ctx);
  } catch (e) {
    console.error("Calendar/Claude error:", e);
    await send(number, "I hit an error while thinking. Try again?");
    return;
  }

  if (info?.action === "delete") return handleDelete(ctx, info);
  if (info?.action === "create") return handleCreate(ctx, info);

  await send(
    number,
    `I didn't identify a calendar action. ${info?.summary || ""}`.trim()
  );
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

function renderCreateConfirm(draft) {
  return `Confirm this event:
- ${draft.title}
- ${draftEmails(draft).join(", ")}
- ${whenStr(draft.start_iso)} (${draft.duration_min} min)

Reply "yes" to confirm and I'll send the invites, or tell me what to change and I'll adjust.`;
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
      data: { draft },
    },
    600
  );
  await send(number, renderCreateConfirm(draft));
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
  const header = ev.reused
    ? "That event already exists — here it is (no duplicate created):"
    : "Done! Invite created and sent:";
  await send(
    number,
    `${header}\n\n- ${draft.title}\n- ${emails.join(", ")}\n- ${whenStr(
      draft.start_iso
    )} (${draft.duration_min} min)\n\nHere is a link for the event:\n${
      ev.htmlLink || ""
    }`
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
    await send(number, `Okay, I won't create "${draft.title}".`);
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
    await send(
      number,
      "I understood the request but failed to create it in Google. Error in the log."
    );
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
      max_tokens: 700,
      system: buildCreateReviewSystem(owner),
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
    const out = msg.content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    console.log("CREATE REVIEW RAW:", out);
    const parsed = parseJsonReply(out);
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
function joinList(items) {
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}

function renderInquiry(m) {
  // Marquee case: only a single person's email is missing → address them by name.
  if (!m.noTime && !m.noAttendees && m.emailNames.length === 1) {
    return `${m.emailNames[0]}, I'm missing your email. Can you send it so I can add you to the invite?`;
  }
  const asks = [];
  if (m.noTime) asks.push("the date and time");
  if (m.noAttendees) asks.push("who to invite");
  if (m.emailNames.length === 1) asks.push(`${m.emailNames[0]}'s email`);
  else if (m.emailNames.length > 1) asks.push(`emails for ${joinList(m.emailNames)}`);
  return `Before I can set this up, I still need ${joinList(
    asks
  )}. Send it here and I'll continue.`;
}

async function openInquiry(ctx, draft, m) {
  const { number, send, sessions, remoteJid } = ctx;
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "create",
      stage: "await_info",
      awaitFrom: "any", // the owner OR any attendee in the chat may answer
      data: { draft },
    },
    600
  );
  await send(number, renderInquiry(m));
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
      max_tokens: 500,
      system: buildResolveSystem(owner),
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
    const out = msg.content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    console.log("RESOLVE RAW:", out);
    return parseJsonReply(out);
  } catch (e) {
    console.error("resolve error:", e?.message || e);
    return null;
  }
}

// ---- DELETE ----------------------------------------------------------------
// Don't trust the link alone: gather what the conversation says about the event
// (start time, participant emails) PLUS the id decoded from any replied-to link,
// then MATCH that against the real calendar. Only open the confirmation SESSION
// when a confident match is found. The "yes" arrives as a continuation (handled
// by resumeDelete) — no @brain tag needed.
async function handleDelete(ctx, info) {
  const { number, env, send, tag, quoted, sessions, remoteJid } = ctx;

  // Identity captured from the request/conversation, plus the link as one signal.
  const participants = Array.isArray(info?.participants) ? info.participants : [];
  const emails = participants.map((p) => p?.email).filter(Boolean);
  const startIso = info?.start_iso || null;
  const eidEventId = resolveEventId(quoted?.calendarLink); // may be null

  // Need at least one usable signal beyond a bare start time to be sure.
  if (!eidEventId && !(startIso && emails.length)) {
    await send(
      number,
      `To cancel an event, reply to its invite message, or tell me which meeting (who and when) and call ${tag} again.`
    );
    return;
  }

  let matches;
  try {
    matches = await matchDeletionTargets(env, { eidEventId, startIso, emails });
  } catch (e) {
    console.error("Calendar match error:", e?.response?.data || e?.message || e);
    await send(number, "I hit an error checking the calendar. Try again?");
    return;
  }

  if (!matches.length) {
    await send(
      number,
      "I couldn't find a matching event — it may already be cancelled, or I'm not sure which one you mean. Reply to its invite message and try again."
    );
    return;
  }

  // Confident matches of the same meeting (dupes included). Describe them from the
  // first match; the confirm-time sweep re-checks the calendar and removes any copy.
  const primary = matches[0];
  const title = primary.summary || "(untitled)";
  const start = primary.start?.dateTime || startIso || null;
  const when = whenStr(primary.start?.dateTime || startIso);
  const ids = matches.map((e) => e.id);
  const countNote = ids.length > 1 ? `\n- (${ids.length} matching copies)` : "";

  // Confirm-first: remember the matched ids + identity and ask. The owner can just
  // type "yes"/"no" (no reply, no tag); the brain watches and ignores chatter.
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "delete",
      stage: "await_confirmation",
      awaitFrom: "owner", // only the owner confirms their own cancellation
      data: { ids, title, when, start },
    },
    600 // 10 min window to confirm
  );
  await send(
    number,
    `Confirm the cancelation of this event?\n- ${title}\n- ${when}${countNote}\n\nReply "yes" to confirm, or "no" to keep it.`
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
    await send(number, `Okay, I'll keep "${title}".`);
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
    const dupNote = n > 1 ? ` (removed ${n} copies)` : "";
    await send(
      number,
      `Cancelled "${title}"${dupNote} and notified the attendees.`
    );
  } catch (e) {
    console.error("Calendar delete error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await send(
      number,
      "I found the event but failed to cancel it in Google. Error in the log."
    );
  }
}

// LLM judgment: does the latest message respond to a pending confirmation?
// Returns "confirm" | "decline" | "unrelated" (defaults to "unrelated" on doubt/error).
async function classifyConfirmation(ctx, { action }) {
  const { anthropic, model, transcript, order } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 50,
      system: buildConfirmSystem(action),
      messages: [
        { role: "user", content: buildConfirmUser({ transcript, latest: order }) },
      ],
    });
    const out = msg.content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    console.log("CONFIRM RAW:", out);
    const decision = parseJsonReply(out)?.decision;
    return decision === "confirm" || decision === "decline"
      ? decision
      : "unrelated";
  } catch (e) {
    console.error("confirm classify error:", e?.message || e);
    return "unrelated"; // on error, do nothing (safe)
  }
}
