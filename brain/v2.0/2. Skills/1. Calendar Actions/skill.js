// ============================================================================
//  Skill "Calendar Actions" — LOGIC.
//  Interprets the order with Claude, creates the event in Google Calendar and
//  fires the invite email. Run by the orchestrator when the router picks
//  "schedule_meeting".
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
// ============================================================================
import { google } from "googleapis";
import { buildSystem, buildUserPrompt } from "./prompt.js";

export const manifest = {
  id: "schedule_meeting",
  description:
    "create a meeting invite/event in Google Calendar and send it to the participants",
};

function calendarClient(env) {
  const o = new google.auth.OAuth2(
    env.GOOGLE_CLIENT_ID,
    env.GOOGLE_CLIENT_SECRET
  );
  o.setCredentials({ refresh_token: env.GOOGLE_REFRESH_TOKEN });
  return google.calendar({ version: "v3", auth: o });
}

async function createEvent(env, { title, emails, start_iso, end_iso, summary }) {
  const cal = calendarClient(env);
  const r = await cal.events.insert({
    calendarId: env.GOOGLE_CALENDAR_ID || "primary",
    sendUpdates: "all", // fires the invite email to the participants
    requestBody: {
      summary: title,
      description: summary || "",
      start: { dateTime: start_iso, timeZone: "America/Sao_Paulo" },
      end: { dateTime: end_iso, timeZone: "America/Sao_Paulo" },
      attendees: emails.map((email) => ({ email })),
    },
  });
  return r.data;
}

async function interpret(ctx) {
  const { owner, anthropic, model, order, transcript, nowStr, contact } = ctx;
  const system = buildSystem(owner);
  const prompt = buildUserPrompt(owner, { order, transcript, nowStr, contact });
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

// ctx (from the orchestrator): { owner, anthropic, model, order, transcript,
//   nowStr, contact, number, env, send }
export async function run(ctx) {
  const { owner, number, env, send } = ctx;

  let info;
  try {
    info = await interpret(ctx);
  } catch (e) {
    console.error("Calendar/Claude error:", e);
    await send(number, "I hit an error while thinking. Try again?");
    return;
  }

  if (!info || info.intent !== "create_event") {
    await send(
      number,
      `I didn't identify an invite request. ${info?.summary || ""}`.trim()
    );
    return;
  }

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
      )}. Send it in the chat and call @secretary again.`
    );
    return;
  }

  const title = `${owner} & ${names.join(" & ") || ctx.contact || "Guest"}`;
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
    const when = new Date(info.start_iso).toLocaleString("en-US", {
      timeZone: "America/Sao_Paulo",
    });
    await send(
      number,
      `Done! Invite created and sent:\n- ${title}\n- ${emails.join(
        ", "
      )}\n- ${when} (${dur} min)\n${ev.htmlLink || ""}`
    );
  } catch (e) {
    console.error("Calendar error:", e?.response?.data || e?.message || e);
    await send(
      number,
      "I understood the request but failed to create it in Google. Error in the log."
    );
  }
}
