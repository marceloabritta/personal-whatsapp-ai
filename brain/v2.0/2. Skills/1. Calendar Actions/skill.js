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
import { buildSystem, buildUserPrompt } from "./prompt.js";

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

async function createEvent(env, { title, emails, start_iso, end_iso, summary }) {
  const cal = calendarClient(env);
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

async function deleteEvent(env, eventId) {
  const cal = calendarClient(env);
  await cal.events.delete({
    calendarId: calId(env),
    eventId,
    sendUpdates: "all", // notify attendees of the cancellation
  });
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
    ? new Date(dateTime).toLocaleString("en-US", { timeZone: CAL_TZ })
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
  const m = out.match(/\{[\s\S]*\}/);
  return m ? JSON.parse(m[0]) : null;
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
async function handleCreate(ctx, info) {
  const { owner, number, env, send, tag, contact } = ctx;

  // one participant -> one email. Each attendee may or may not have an email.
  const participants = Array.isArray(info.participants) ? info.participants : [];
  const names = participants.map((p) => p?.name).filter(Boolean);
  const emails = participants.map((p) => p?.email).filter(Boolean);

  const missing = new Set(info.missing || []);
  if (!info.start_iso) missing.add("start_iso");
  if (!emails.length) missing.add("email");
  if (missing.size) {
    await send(
      number,
      `Almost there. Still missing: ${[...missing].join(
        ", "
      )}. Send it in the chat and call ${tag} again.`
    );
    return;
  }

  const title = `${owner} & ${names.join(" & ") || contact || "Guest"}`;
  const dur = Number(info.duration_min) > 0 ? Number(info.duration_min) : 45; // default 45 min
  const end_iso = new Date(
    new Date(info.start_iso).getTime() + dur * 60000
  ).toISOString();

  try {
    const ev = await createEvent(env, {
      title,
      emails,
      start_iso: info.start_iso,
      end_iso,
      summary: info.summary,
    });
    await send(
      number,
      `Done! Invite created and sent:\n- ${title}\n- ${emails.join(
        ", "
      )}\n- ${whenStr(info.start_iso)} (${dur} min)\n${ev.htmlLink || ""}`
    );
  } catch (e) {
    console.error("Calendar error:", e?.response?.data || e?.message || e);
    await send(
      number,
      "I understood the request but failed to create it in Google. Error in the log."
    );
  }
}

// ---- DELETE (reply to a calendar link) -------------------------------------
// Initial request: resolve the event from the replied-to link, then open a
// confirmation SESSION and ask. The "yes" arrives as a continuation (a reply to
// this message) and is handled by resumeDelete — no @brain tag needed.
async function handleDelete(ctx, info) {
  const { number, env, send, tag, quoted, sessions, remoteJid } = ctx;

  const link = quoted?.calendarLink;
  if (!link) {
    await send(
      number,
      `To cancel an event, reply to the message that has its Google Calendar link and call ${tag} again.`
    );
    return;
  }

  const eventId = resolveEventId(link);
  if (!eventId) {
    await send(
      number,
      "I couldn't read the calendar link on that message. Reply to the message that has the Google Calendar link."
    );
    return;
  }

  let ev;
  try {
    ev = await getEvent(env, eventId);
  } catch (e) {
    console.error("Calendar get error:", e?.response?.data || e?.message || e);
    await send(
      number,
      "I couldn't find that event — it may already be cancelled or gone."
    );
    return;
  }

  const title = ev.summary || "(untitled)";
  const when = whenStr(ev.start?.dateTime);

  // Confirm-first: remember the event and ask. No link needed in the message —
  // the session holds the eventId, so a "yes" reply resolves it.
  await sessions.set(remoteJid, {
    skill: "calendar_action",
    intent: "delete",
    stage: "await_confirmation",
    awaitFrom: "owner", // only the owner confirms their own cancellation
    data: { eventId, title, when },
  });
  await send(
    number,
    `Confirm the cancelation of this event?\n- ${title}\n- ${when}\n\nReply to this message with "yes" to confirm.`
  );
}

// Resume a pending cancellation: the owner replied to the confirmation message.
async function resumeDelete(ctx, session) {
  const { number, env, send, sessions, remoteJid, order } = ctx;
  const { eventId, title } = session.data || {};
  const ans = classifyYesNo(order);

  if (ans === "no") {
    await sessions.clear(remoteJid);
    await send(number, `Okay, I'll keep "${title}".`);
    return;
  }
  if (ans !== "yes") {
    await send(number, `Reply "yes" to cancel "${title}", or "no" to keep it.`);
    return; // keep the session; the next reply can still resolve it
  }

  try {
    await deleteEvent(env, eventId);
    await sessions.clear(remoteJid);
    await send(number, `Cancelled "${title}" and notified the attendees.`);
  } catch (e) {
    console.error("Calendar delete error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await send(
      number,
      "I found the event but failed to cancel it in Google. Error in the log."
    );
  }
}

// Cheap yes/no classifier (EN + PT-BR), first word or whole message.
function classifyYesNo(text) {
  const t = String(text || "")
    .trim()
    .toLowerCase()
    .replace(/[.!,?]/g, "");
  const yes = ["y", "yes", "yep", "yeah", "yup", "ok", "okay", "sure", "confirm",
    "confirmed", "confirmo", "confirmar", "sim", "isso", "pode", "vai", "manda",
    "do it", "go ahead"];
  const no = ["n", "no", "nope", "nah", "nao", "não", "negativo", "keep",
    "dont", "don't", "deixa"];
  const first = t.split(/\s+/)[0];
  if (yes.includes(t) || yes.includes(first)) return "yes";
  if (no.includes(t) || no.includes(first)) return "no";
  return "unknown";
}
