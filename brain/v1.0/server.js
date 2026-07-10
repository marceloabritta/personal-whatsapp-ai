// ============================================================================
//  BRAIN (v1.0 — the first, single-agent version)  —  receives the Evolution
//  webhook, interprets with Claude and creates the event in Google Calendar.
//  The PROMPT logic lives in prompt.js.
//
//  Kept as a historical snapshot. The current version is brain/v2.0
//  (orchestrator + skills).
// ============================================================================
import express from "express";
import Anthropic from "@anthropic-ai/sdk";
import { google } from "googleapis";
import { buildSystem, buildUserPrompt } from "./prompt.js";

const app = express();
app.use(express.json({ limit: "2mb" }));

const EVOLUTION_URL = process.env.EVOLUTION_URL || "http://api:8080";
const APIKEY = process.env.EVOLUTION_APIKEY;
const INSTANCE = process.env.EVOLUTION_INSTANCE || "secretary";
const TAG = (process.env.SECRETARY_TAG || "@secretary").toLowerCase();
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";
const GCAL_ID = process.env.GOOGLE_CALENDAR_ID || "primary";
const OWNER_NAME = process.env.OWNER_NAME || "User";
const HEADER = "[AI Secretary]:";
const FOOTER = "(sent by AI)";

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const SYSTEM = buildSystem(OWNER_NAME);
const seen = new Set();          // dedup by messageId
const buffers = new Map();       // remoteJid -> [{t, fromMe, text, pushName}]

function remember(remoteJid, e) {
  if (!e.text) return;
  const arr = buffers.get(remoteJid) || [];
  arr.push(e);
  while (arr.length > 50) arr.shift();
  buffers.set(remoteJid, arr);
}

function extractText(msg) {
  if (!msg) return "";
  return msg.conversation
    || msg.extendedTextMessage?.text
    || msg.imageMessage?.caption
    || msg.videoMessage?.caption
    || "";
}

async function sendText(number, text) {
  const full = `${HEADER}\n${text}\n${FOOTER}`;
  const res = await fetch(`${EVOLUTION_URL}/message/sendText/${INSTANCE}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: APIKEY },
    body: JSON.stringify({ number, text: full }),
  });
  if (!res.ok) console.error("sendText failed", res.status, await res.text());
}

async function fetchHistory(remoteJid) {
  try {
    const res = await fetch(`${EVOLUTION_URL}/chat/findMessages/${INSTANCE}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", apikey: APIKEY },
      body: JSON.stringify({ where: { key: { remoteJid } } }),
    });
    if (!res.ok) return [];
    const data = await res.json();
    const recs = Array.isArray(data) ? data : (data?.messages?.records || data?.records || []);
    return recs.map(r => ({ t: Number(r.messageTimestamp) || 0, fromMe: r.key?.fromMe, text: extractText(r.message).trim(), pushName: r.pushName }));
  } catch { return []; }
}

function combine(remoteJid, hist) {
  const buf = buffers.get(remoteJid) || [];
  const all = [...hist, ...buf].filter(m => m.text);
  const map = new Map();
  for (const m of all) map.set(`${m.t}|${m.text}`, m);
  return [...map.values()].sort((a, b) => a.t - b.t).slice(-30);
}

async function interpret(order, transcript, nowStr, contact) {
  const prompt = buildUserPrompt(OWNER_NAME, { order, transcript, nowStr, contact });
  const msg = await anthropic.messages.create({ model: MODEL, max_tokens: 700, system: SYSTEM, messages: [{ role: "user", content: prompt }] });
  const out = msg.content.filter(b => b.type === "text").map(b => b.text).join("");
  console.log("CLAUDE RAW:", out);
  const m = out.match(/\{[\s\S]*\}/);
  return m ? JSON.parse(m[0]) : null;
}

function calendarClient() {
  const o = new google.auth.OAuth2(process.env.GOOGLE_CLIENT_ID, process.env.GOOGLE_CLIENT_SECRET);
  o.setCredentials({ refresh_token: process.env.GOOGLE_REFRESH_TOKEN });
  return google.calendar({ version: "v3", auth: o });
}

async function createEvent({ title, emails, start_iso, end_iso, summary }) {
  const cal = calendarClient();
  const r = await cal.events.insert({
    calendarId: GCAL_ID,
    sendUpdates: "all", // fires the invite email to the participants
    requestBody: {
      summary: title,
      description: summary || "",
      start: { dateTime: start_iso, timeZone: "America/Sao_Paulo" },
      end: { dateTime: end_iso, timeZone: "America/Sao_Paulo" },
      attendees: emails.map(email => ({ email })),
    },
  });
  return r.data;
}

app.get("/", (_req, res) => res.send("Brain up."));

app.post("/webhook", async (req, res) => {
  res.sendStatus(200); // reply fast so Evolution does not resend
  try {
    const data = req.body?.data;
    if (!data?.key) return;
    const { fromMe, remoteJid, id } = data.key;
    const text = extractText(data.message).trim();
    const t = Number(data.messageTimestamp) || Math.floor(Date.now() / 1000);
    if (text) remember(remoteJid, { t, fromMe, text, pushName: data.pushName }); // buffer EVERY message

    if (!fromMe) return;                                   // only the owner's order
    if (!text.toLowerCase().startsWith(TAG)) return;       // only act on the @secretary tag
    if (id && seen.has(id)) return;                        // dedup
    if (id) { seen.add(id); if (seen.size > 500) seen.delete(seen.values().next().value); }

    const order = text.slice(TAG.length).trim();
    const number = remoteJid.split("@")[0]; // v1.0: reply in the originating chat

    const nowStr = new Date().toLocaleString("en-US", { timeZone: "America/Sao_Paulo", weekday: "long", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    const conv = combine(remoteJid, await fetchHistory(remoteJid));
    const transcript = conv.map(m => `${m.fromMe ? "ME" : "OTHER"}: ${m.text}`).join("\n");
    const contact = [...conv].reverse().find(m => !m.fromMe && m.pushName)?.pushName;
    console.log("TRANSCRIPT>>>\n" + transcript + "\n<<<");

    let info;
    try { info = await interpret(order, transcript, nowStr, contact); }
    catch (e) { console.error("Claude error:", e); await sendText(number, "I hit an error while thinking. Try again?"); return; }

    if (!info || info.intent !== "create_event") {
      await sendText(number, `I didn't identify an invite request. ${info?.summary || ""}`.trim());
      return;
    }

    // v1.0: one participant -> one email. Each attendee may or may not have an email.
    const participants = Array.isArray(info.participants) ? info.participants : [];
    const names = participants.map(p => p?.name).filter(Boolean);
    const emails = participants.map(p => p?.email).filter(Boolean);

    const missing = new Set(info.missing || []);
    if (!info.start_iso) missing.add("start_iso");
    if (!emails.length) missing.add("email");
    if (missing.size) {
      await sendText(number, `Almost there. Still missing: ${[...missing].join(", ")}. Send it in the chat and call @secretary again.`);
      return;
    }

    const title = `${OWNER_NAME} & ${names.join(" & ") || contact || "Guest"}`;
    const dur = Number(info.duration_min) > 0 ? Number(info.duration_min) : 45; // default 45 min
    const end_iso = new Date(new Date(info.start_iso).getTime() + dur * 60000).toISOString();

    try {
      const ev = await createEvent({ title, emails, start_iso: info.start_iso, end_iso, summary: info.summary });
      const when = new Date(info.start_iso).toLocaleString("en-US", { timeZone: "America/Sao_Paulo" });
      await sendText(number, `Done! Invite created and sent:\n- ${title}\n- ${emails.join(", ")}\n- ${when} (${dur} min)\n${ev.htmlLink || ""}`);
    } catch (e) {
      console.error("Calendar error:", e?.response?.data || e?.message || e);
      await sendText(number, "I understood the request but failed to create it in Google. Error in the log.");
    }
  } catch (e) {
    console.error("Webhook error:", e);
  }
});

app.listen(process.env.PORT || 3000, () => console.log("Brain v1.0 listening on port 3000"));
