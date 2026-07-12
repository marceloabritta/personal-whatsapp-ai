// ============================================================================
//  SECRETARY (v2.0)  —  ORCHESTRATOR.
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
import { TAGS, headerFor, isOwnMessage, matchedTag } from "./lib/identity.js";
import { frame } from "./lib/format.js";
import { route } from "./router/router.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SKILLS_DIR = path.join(__dirname, "..", "2. Skills");

// ---- Config -----------------------------------------------------------------
const EVOLUTION_URL = process.env.EVOLUTION_URL || "http://api:8080";
const APIKEY = process.env.EVOLUTION_APIKEY;
const INSTANCE = process.env.EVOLUTION_INSTANCE || "secretary";
// Trigger tags + reply header live in lib/identity.js (single source of truth,
// shared with skills). TAGS is the accepted-tag list; headerFor(lang)/isOwnMessage/
// matchedTag are imported above.
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";
// Cheap model for the long-tail translation fallback (see localizeBody).
const TRANSLATE_MODEL =
  process.env.TRANSLATE_MODEL || "claude-haiku-4-5-20251001";
const OWNER_NAME = process.env.OWNER_NAME || "User";
// Languages the secretary writes natively (skills carry en/pt maps). Any other
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
//   export const capabilities = { name: (ctx, ...args) => ... }   // OPTIONAL
// -> SKILLS: { [id]: run }  |  CATALOG: [{ id, description }] (the router's menu)
//  | CAPS: { [id]: capabilities } — the internal skill-to-skill API (see ctx.callSkill).
//    Capabilities are NEVER shown to the router; they let one skill compose another
//    (e.g. task_action delegating a "task for someone" to calendar_action.startCreate)
//    without importing its file — decoupled from folder paths, graceful when absent.
async function loadSkills() {
  const skills = {};
  const catalog = [];
  const caps = {};
  let entries = [];
  try {
    entries = await readdir(SKILLS_DIR, { withFileTypes: true });
  } catch (e) {
    console.error("Could not read the skills folder:", SKILLS_DIR, e.message);
    return { skills, catalog, caps };
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
      if (mod.capabilities && typeof mod.capabilities === "object") {
        caps[id] = mod.capabilities;
        console.log(
          `skill loaded: "${e.name}" -> ${id} (capabilities: ${Object.keys(
            mod.capabilities
          ).join(", ")})`
        );
      } else {
        console.log(`skill loaded: "${e.name}" -> ${id}`);
      }
    } catch (err) {
      console.error(`failed to load skill "${e.name}":`, err.message);
    }
  }
  return { skills, catalog, caps };
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

// Sends text to WhatsApp with the secretary's standard framing (bold header, blank
// line, italic body — see lib/format.js). `lang` drives the long-tail translation
// fallback; en/pt pass through unchanged. Markers are added AFTER localizeBody() so
// the translation model never sees them (its prompt promises to preserve URLs and
// line breaks, but says nothing about `_`/`*`). `opts.italic:false` sends a plain body.
// Skills receive a `ctx.send` already bound to the conversation's language.
async function send(number, text, lang = "en", opts = {}) {
  const body = await localizeBody(text, lang);
  return evolution.sendText(number, frame(headerFor(lang), body, opts));
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

// Max depth of skill→skill delegation (ctx.callSkill), a loop/recursion backstop.
const MAX_SKILL_DEPTH = 4;

// ---- Boot -------------------------------------------------------------------
const { skills: SKILLS, catalog: CATALOG, caps: CAPS } = await loadSkills();
console.log(
  "available skills:",
  CATALOG.map((c) => c.id).join(", ") || "(none!)"
);

const app = express();
app.use(express.json({ limit: "8mb" }));

app.get("/", (_req, res) => res.send("Secretary v2.0 up."));

app.post("/webhook", async (req, res) => {
  res.sendStatus(200); // reply fast so Evolution does not resend
  try {
    const data = req.body?.data;
    if (!data?.key) return;
    const { fromMe, remoteJid, id } = data.key;
    const text = extractText(data.message).trim();
    const t = Number(data.messageTimestamp) || Math.floor(Date.now() / 1000);

    // buffer EVERY message (context), even the ones that don't trigger the secretary.
    if (text) remember(remoteJid, { t, fromMe, text, pushName: data.pushName });

    const quoted = getQuoted(data); // { id, hasAudio, mediaType, text, calendarLink } | null

    // Never react to the secretary's OWN messages. They arrive with fromMe=true
    // (same account as the owner), so this header check is the ONLY thing telling
    // them apart from a genuine owner message — it must match every header variant
    // the secretary emits (both languages + legacy), see lib/identity.js.
    const isOwnMsg = isOwnMessage(text);

    // Pending conversation state for this chat (confirmations, clarifications, ...).
    const session = await sessions.get(remoteJid);

    // START: a flow only begins when the OWNER uses a trigger tag. `tag` is the tag
    // this message actually starts with (or null) — used below to slice it off.
    const tag = fromMe ? matchedTag(text) : null;
    const isTagged = !!tag;

    // CONTINUE: while a session is active, the owning skill inspects EVERY message
    // from the party it waits on (session.awaitFrom) and decides — with the LLM —
    // whether the message supplies the awaited info. No reply/tag required; normal
    // chatter is ignored by the skill. awaitFrom: owner (fromMe) | contact (!fromMe)
    // | any. (The contact case lets the person the owner is scheduling with answer.)
    const awaitFrom = session?.awaitFrom || "owner";
    let isContinuation = false;
    if (session && !isTagged && !isOwnMsg) {
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

    // Slice off the matched tag by ITS own length (tags can differ in length).
    const order = isTagged ? text.slice(tag.length).trim() : text.trim();
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
      tag: tag || TAGS[0], // the tag this order used (fallback: the primary tag)
      tags: TAGS, // full accepted-tag list, for any skill that wants to show them
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
    ctx.send = (number, text, opts) => send(number, text, ctx.lang, opts);

    // Cross-skill composition. `hasSkill` guards a friendly fallback; `callSkill`
    // invokes another skill's exported capability, auto-injecting THIS ctx (so the
    // callee shares owner/lang/sessions/send) with a depth guard against loops. A
    // session the callee opens is tagged with the callee's id, so its continuations
    // route to the callee. Missing capability -> throws (caught by the per-skill catch).
    ctx.hasSkill = (id, name) => typeof CAPS[id]?.[name] === "function";
    ctx.callSkill = async (id, name, ...args) => {
      const fn = CAPS[id]?.[name];
      if (!fn) throw new Error(`capability ${id}.${name} unavailable`);
      const depth = (ctx._skillDepth || 0) + 1;
      if (depth > MAX_SKILL_DEPTH)
        throw new Error(`skill-call depth exceeded at ${id}.${name}`);
      return fn({ ...ctx, _skillDepth: depth }, ...args);
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
        await send(number, orch(ctx.lang, "continuationError"), ctx.lang);
      }
      return;
    }

    // FRESH COMMAND: a new tagged order overrides any pending session.
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
  console.log("Secretary v2.0 (orchestrator) listening on port 3000")
);
