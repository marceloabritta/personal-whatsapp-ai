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

    if (!fromMe) return; // only the owner's order

    const quoted = getQuoted(data); // { id, hasAudio, mediaType, text, calendarLink } | null
    console.log("QUOTED>>>", JSON.stringify(quoted));

    const isTagged = text.toLowerCase().startsWith(TAG);
    // Act on the trigger tag, OR on a tagless reply to a message that carries a
    // Google Calendar link — that's the cancel-confirmation flow, where the owner
    // replies just "yes" to the brain's message (no tag needed).
    if (!isTagged && !quoted?.calendarLink) return;
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
      quoted,
      hasQuotedAudio: !!quoted?.hasAudio,
      catalog: CATALOG,
      env: process.env,
      evolution,
      send,
    };

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
      if (!isTagged) return; // stay silent on tagless replies we can't act on
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
