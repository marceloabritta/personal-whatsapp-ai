// ============================================================================
//  BRAIN (v2.0)  —  ORCHESTRATOR.
//  Receives the Evolution webhook, filters (fromMe + trigger tag), builds the
//  context, DISCOVERS the available skills (../2. Skills/*/skill.js), calls the
//  ROUTER to classify intent and dispatches to the chosen skill(s).
//
//  Flow:  webhook -> filter -> context -> router -> skill(s)
//
//  Adding a new skill = create a folder under "2. Skills/" with a skill.js that
//  exports { manifest, run }. The orchestrator loads it on its own at boot; no
//  need to edit this file or the router.
// ============================================================================
import express from "express";
import Anthropic from "@anthropic-ai/sdk";
import { readdir } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

import { createEvolution } from "./lib/evolution.js";
import {
  extractText,
  getQuoted,
  remember,
  combine,
  buildTranscript,
  contactName,
} from "./lib/whatsapp.js";
import { createSessions } from "./lib/sessions.js";
import { route } from "./router/router.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SKILLS_DIR = path.join(__dirname, "..", "2. Skills");

// ---- Config -----------------------------------------------------------------
const EVOLUTION_URL = process.env.EVOLUTION_URL || "http://api:8080";
const APIKEY = process.env.EVOLUTION_APIKEY;
const INSTANCE = process.env.EVOLUTION_INSTANCE || "secretary";
const TAG = (process.env.SECRETARY_TAG || "@brain").toLowerCase();
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";
const OWNER_NAME = process.env.OWNER_NAME || "User";
const HEADER = "[AI Brain]:";

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const evolution = createEvolution({
  url: EVOLUTION_URL,
  apikey: APIKEY,
  instance: INSTANCE,
});
// Per-chat conversation state (project-wide). Empty REDIS_URL -> in-memory only.
const REDIS_URL =
  process.env.REDIS_URL === undefined
    ? "redis://evolution_redis:6379"
    : process.env.REDIS_URL;
const sessions = createSessions({ url: REDIS_URL });

const seen = new Set(); // dedup by messageId

// ---- Skill discovery --------------------------------------------------------
// Scans "2. Skills/*/skill.js". Each skill exports:
//   export const manifest = { id, description }
//   export async function run(ctx) { ... }
// -> SKILLS: { [id]: run }   |   CATALOG: [{ id, description }] (passed to the router)
async function loadSkills() {
  const skills = {};
  const catalog = [];
  let entries = [];
  try {
    entries = await readdir(SKILLS_DIR, { withFileTypes: true });
  } catch (e) {
    console.error("Could not read the skills folder:", SKILLS_DIR, e.message);
    return { skills, catalog };
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    const file = path.join(SKILLS_DIR, e.name, "skill.js");
    try {
      const mod = await import(pathToFileURL(file).href);
      const id = mod.manifest?.id;
      if (!id || typeof mod.run !== "function") {
        console.error(`skill '${e.name}' ignored: missing manifest.id or run()`);
        continue;
      }
      skills[id] = mod.run;
      catalog.push({ id, description: mod.manifest.description || "" });
      console.log(`skill loaded: "${e.name}" -> ${id}`);
    } catch (err) {
      console.error(`failed to load skill "${e.name}":`, err.message);
    }
  }
  return { skills, catalog };
}

// Sends text to WhatsApp with the brain's standard header (header + blank line).
async function send(number, text) {
  const full = `${HEADER}\n\n${text}`;
  return evolution.sendText(number, full);
}

// ---- Boot -------------------------------------------------------------------
const { skills: SKILLS, catalog: CATALOG } = await loadSkills();
console.log(
  "available skills:",
  CATALOG.map((c) => c.id).join(", ") || "(none!)"
);

const app = express();
app.use(express.json({ limit: "8mb" }));

app.get("/", (_req, res) => res.send("Brain v2.0 up."));

app.post("/webhook", async (req, res) => {
  res.sendStatus(200); // reply fast so Evolution does not resend
  try {
    const data = req.body?.data;
    if (!data?.key) return;
    const { fromMe, remoteJid, id } = data.key;
    const text = extractText(data.message).trim();
    const t = Number(data.messageTimestamp) || Math.floor(Date.now() / 1000);

    // buffer EVERY message (context), even the ones that don't trigger the brain.
    if (text) remember(remoteJid, { t, fromMe, text, pushName: data.pushName });

    const quoted = getQuoted(data); // { id, hasAudio, mediaType, text, calendarLink } | null
    console.log("QUOTED>>>", JSON.stringify(quoted));

    // Pending conversation state for this chat (confirmations, clarifications, ...).
    const session = await sessions.get(remoteJid);

    // START: a flow only begins when the OWNER uses the trigger tag.
    const isTagged = fromMe && text.toLowerCase().startsWith(TAG);

    // CONTINUE: an active session accepts a follow-up, depending on who it waits on
    // (session.awaitFrom, default "owner"):
    //   - owner (fromMe): only when replying to one of the brain's own messages,
    //     so we never grab the owner's normal chatter.
    //   - contact (!fromMe): any normal message — e.g. the person the owner is
    //     scheduling with just types their email, not as a reply to the brain.
    const repliesToBrain = !!quoted?.text && quoted.text.startsWith(HEADER);
    const awaitFrom = session?.awaitFrom || "owner";
    let isContinuation = false;
    if (session && !isTagged) {
      if (fromMe && repliesToBrain && (awaitFrom === "owner" || awaitFrom === "any"))
        isContinuation = true;
      else if (!fromMe && (awaitFrom === "contact" || awaitFrom === "any"))
        isContinuation = true;
    }

    // Ignore everything else (incl. non-owner messages with no session for them).
    if (!isTagged && !isContinuation) return;
    if (id && seen.has(id)) return; // dedup
    if (id) {
      seen.add(id);
      if (seen.size > 500) seen.delete(seen.values().next().value);
    }

    const order = isTagged ? text.slice(TAG.length).trim() : text.trim();
    const number = remoteJid.split("@")[0]; // reply in the originating chat

    // Conversation context (Evolution history + in-memory buffer).
    const nowStr = new Date().toLocaleString("en-US", {
      timeZone: "America/Sao_Paulo",
      weekday: "long",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    const conv = combine(remoteJid, await evolution.fetchHistory(remoteJid));
    const transcript = buildTranscript(conv);
    const contact = contactName(conv);
    console.log("TRANSCRIPT>>>\n" + transcript + "\n<<<");

    // Shared context passed to the router and to every skill.
    const ctx = {
      owner: OWNER_NAME,
      tag: TAG,
      anthropic,
      model: MODEL,
      order,
      transcript,
      nowStr,
      contact,
      remoteJid,
      number,
      fromMe, // who sent this message: owner (true) vs the contact (false)
      quoted,
      hasQuotedAudio: !!quoted?.hasAudio,
      catalog: CATALOG,
      env: process.env,
      evolution,
      send,
      sessions, // store: get/set/clear per-chat state
      session: isContinuation ? session : null, // present only on a continuation
    };

    // CONTINUATION: a follow-up owned by the skill that opened the session.
    // Bypass the router and hand it straight to that skill (it reads ctx.session).
    if (isContinuation) {
      const run = SKILLS[session.skill];
      if (!run) {
        await sessions.clear(remoteJid); // owning skill gone; drop stale state
        return;
      }
      try {
        await run(ctx);
      } catch (e) {
        console.error(`Session skill '${session.skill}' error:`, e);
        await send(number, "I failed to continue that. Error in the log.");
      }
      return;
    }

    // FRESH COMMAND: a new @brain order overrides any pending session.
    if (session) await sessions.clear(remoteJid);

    // ROUTER: decide which skill(s) to run.
    let tasks;
    try {
      ({ tasks } = await route(ctx));
    } catch (e) {
      console.error("Router error:", e);
      await send(number, "I hit an error understanding the request. Try again?");
      return;
    }
    console.log("ROUTER -> tasks:", tasks);

    // No recognized skill.
    if (!tasks.length || tasks.every((x) => !SKILLS[x])) {
      const names = CATALOG.map((c) => c.id).join(", ");
      await send(
        number,
        `I didn't understand what you want me to do. Available skills: ${names}.`
      );
      return;
    }

    // Dispatch to each skill in the order decided by the router.
    for (const task of tasks) {
      const run = SKILLS[task];
      if (!run) continue;
      try {
        await run(ctx);
      } catch (e) {
        console.error(`Skill '${task}' error:`, e);
        await send(number, "I failed to run that task. Error in the log.");
      }
    }
  } catch (e) {
    console.error("Webhook error:", e);
  }
});

app.listen(process.env.PORT || 3000, () =>
  console.log("Brain v2.0 (orchestrator) listening on port 3000")
);
