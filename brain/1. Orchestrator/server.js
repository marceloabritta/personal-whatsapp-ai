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
// Cheap model for the long-tail translation fallback (see localizeBody).
const TRANSLATE_MODEL =
  process.env.TRANSLATE_MODEL || "claude-haiku-4-5-20251001";
const OWNER_NAME = process.env.OWNER_NAME || "User";
const HEADER = "[AI Brain]:";
// Languages the brain writes natively (skills carry en/pt maps). Any other
// detected language is handled by the LLM-translation fallback in send().
const MAINTAINED_LANGS = new Set(["en", "pt"]);

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

// LONG-TAIL TRANSLATION FALLBACK. Maintained languages (en/pt) are already
// localized by the skill/orchestrator map → returned untouched. For any other
// detected language, translate the BODY only (the header is added afterwards, so
// it's never seen here) with a cheap model, preserving structure. On any failure
// we return the source text rather than nothing — a message in English beats no
// message.
async function localizeBody(text, lang) {
  const l = (lang || "en").toLowerCase();
  if (!text || MAINTAINED_LANGS.has(l) || l === "en") return text;
  try {
    const msg = await anthropic.messages.create({
      model: TRANSLATE_MODEL,
      max_tokens: 1024,
      system: `Translate the user's message into the language with ISO 639-1 code "${l}". Output ONLY the translation — no preamble, no quotes, no notes. Preserve EXACTLY, unchanged: URLs, email addresses, numbers, dates, times, and every line break and bullet/dash character. Do NOT translate proper nouns (people's names, event titles). Translate the prose only and keep the original layout and formatting.`,
      messages: [{ role: "user", content: text }],
    });
    const out = (msg?.content || [])
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("")
      .trim();
    return out || text;
  } catch (e) {
    console.error("translation fallback error:", e?.message || e);
    return text;
  }
}

// Sends text to WhatsApp with the brain's standard header (header + blank line).
// `lang` drives the long-tail translation fallback; en/pt pass through unchanged.
// Skills receive a `ctx.send` already bound to the conversation's language.
async function send(number, text, lang = "en") {
  const body = await localizeBody(text, lang);
  const full = `${HEADER}\n\n${body}`;
  return evolution.sendText(number, full);
}

// The orchestrator's OWN user-facing strings (routing/plumbing problems), en + pt.
// Any other language is produced from the `en` copy by the send() fallback.
const ORCH_MSG = {
  notUnderstood: {
    en: (names) =>
      `I didn't understand what you want me to do. Available skills: ${names}.`,
    pt: (names) =>
      `Não entendi o que você quer que eu faça. Habilidades disponíveis: ${names}.`,
  },
  routerError: {
    en: () => "I hit an error understanding the request. Try again?",
    pt: () => "Tive um erro ao entender o pedido. Pode tentar de novo?",
  },
  continuationError: {
    en: () => "I failed to continue that. Error in the log.",
    pt: () => "Não consegui continuar isso. O erro está no log.",
  },
  skillError: {
    en: () => "I failed to run that task. Error in the log.",
    pt: () => "Não consegui executar essa tarefa. O erro está no log.",
  },
};

// Pick an orchestrator string for `lang`, falling back to the English copy (which
// the send() fallback then translates for a non-en/pt language).
function orch(lang, key, ...args) {
  const entry = ORCH_MSG[key];
  const fn = (entry && (entry[lang] || entry.en)) || (() => "");
  return fn(...args);
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

    // Never react to the brain's OWN messages (they start with the header).
    const isBrainMsg = text.startsWith(HEADER);

    // Pending conversation state for this chat (confirmations, clarifications, ...).
    const session = await sessions.get(remoteJid);

    // START: a flow only begins when the OWNER uses the trigger tag.
    const isTagged = fromMe && text.toLowerCase().startsWith(TAG);

    // CONTINUE: while a session is active, the owning skill inspects EVERY message
    // from the party it waits on (session.awaitFrom) and decides — with the LLM —
    // whether the message supplies the awaited info. No reply/tag required; normal
    // chatter is ignored by the skill. awaitFrom: owner (fromMe) | contact (!fromMe)
    // | any. (The contact case lets the person the owner is scheduling with answer.)
    const awaitFrom = session?.awaitFrom || "owner";
    let isContinuation = false;
    if (session && !isTagged && !isBrainMsg) {
      if (fromMe && (awaitFrom === "owner" || awaitFrom === "any"))
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
      sessions, // store: get/set/clear per-chat state
      session: isContinuation ? session : null, // present only on a continuation
    };

    // Conversation language. On a continuation it's persisted in the session (so a
    // "yes" answers in the language the flow started in); on a fresh command the
    // router fills it in below. Default English. `ctx.send` reads ctx.lang lazily,
    // so setting it after routing still applies to every skill send.
    ctx.lang = (isContinuation ? session?.lang : null) || "en";
    ctx.send = (number, text) => send(number, text, ctx.lang);

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
        await send(number, orch(ctx.lang, "continuationError"), ctx.lang);
      }
      return;
    }

    // FRESH COMMAND: a new @brain order overrides any pending session.
    if (session) await sessions.clear(remoteJid);

    // ROUTER: decide which skill(s) to run — and detect the conversation language.
    let tasks;
    try {
      const routed = await route(ctx);
      tasks = routed.tasks;
      ctx.lang = routed.lang || ctx.lang; // reply in the detected language
    } catch (e) {
      console.error("Router error:", e);
      await send(number, orch(ctx.lang, "routerError"), ctx.lang);
      return;
    }
    console.log("ROUTER -> tasks:", tasks, "lang:", ctx.lang);

    // No recognized skill.
    if (!tasks.length || tasks.every((x) => !SKILLS[x])) {
      const names = CATALOG.map((c) => c.id).join(", ");
      await send(number, orch(ctx.lang, "notUnderstood", names), ctx.lang);
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
        await send(number, orch(ctx.lang, "skillError"), ctx.lang);
      }
    }
  } catch (e) {
    console.error("Webhook error:", e);
  }
});

app.listen(process.env.PORT || 3000, () =>
  console.log("Brain v2.0 (orchestrator) listening on port 3000")
);
