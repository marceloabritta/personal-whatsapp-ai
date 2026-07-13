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
import { createSettings } from "./lib/settings.js";
import { withThinkingDefault } from "./lib/llm.js";
import { checkPayload } from "./lib/inputs.js";
import { TAGS, setTags, headerFor, isOwnMessage, matchedTag } from "./lib/identity.js";
import { frame } from "./lib/format.js";
import { route } from "./router/router.js";
import { installLogBuffer } from "./lib/logbuffer.js";
import { captureFailure } from "./lib/selflearning.js";

// SELF-LEARNING: wrap console so the secretary can read its own recent logs back when it
// writes a failure report. Must run before anything else logs — including loadSkills()
// below. stdout is untouched, so `docker logs` still works exactly as before.
installLogBuffer();

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

// THE one Anthropic client, handed to everything via ctx.anthropic. It is WRAPPED so every
// call site defaults to thinking:{type:"disabled"} — we throw every thinking block away
// (lib/llm.js readText), so generating them was pure latency. Wrapping it here, at the single
// door, is what makes the fix inherited rather than remembered. See lib/llm.js.
const anthropic = withThinkingDefault(new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY }));
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
// Durable settings on the SAME Redis (no TTL, own key space). Today: the tag list the owner
// summons her with, which he can change by asking (the `assistant_settings` skill).
// SECRETARY_TAG is the SEED; a stored value wins — see the boot load below.
const settings = createSettings({ url: REDIS_URL });

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
      // `inputs` is the skill's DECLARED input contract (manifest.inputs, may be null). The
      // router asks the model to fill it in the same call that classifies the order; the
      // orchestrator only ever handles it as opaque text + a declaration to validate against.
      // See lib/inputs.js.
      catalog.push({
        id,
        description: mod.manifest.description || "",
        inputs: mod.manifest.inputs || null,
      });
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

// SELF-LEARNING: write a failure report, guarded. captureFailure() already promises never
// to throw; this is the belt to its braces, because a bug in the thing that records bugs
// must never be the thing that breaks a reply the owner was waiting for.
async function fireCapture(ctx, info) {
  try {
    await captureFailure(ctx, info);
  } catch (e) {
    console.error("fireCapture failed:", e?.message || e);
  }
}

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
      // Did THIS message address the secretary by tag? A continuation is NEVER
      // tagged (see the gate above), so a skill reading this learns whether the
      // owner spoke TO it or merely spoke while it was listening. `tag` is not a
      // substitute — it falls back to TAGS[0] and is always truthy.
      isTagged,
      quoted,
      hasQuotedAudio: !!quoted?.hasAudio,
      catalog: CATALOG,
      env: process.env,
      evolution,
      sessions, // store: get/set/clear per-chat state
      settings, // durable settings: loadTags/saveTags (the tag list he summons her with)
      session: isContinuation ? session : null, // present only on a continuation
      // SELF-LEARNING: one failure report per webhook turn. This is an OBJECT, not a
      // boolean, and that is load-bearing: ctx.callSkill spreads the ctx ({ ...ctx }), so a
      // flag set by a callee on a boolean field would mutate a COPY and never be seen by
      // the caller. The spread copies this object's reference, so every frame shares it.
      _turn: { captured: false },
    };

    // Conversation language. On a continuation it's persisted in the session (so a
    // "yes" answers in the language the flow started in); on a fresh command the
    // router fills it in below. Default English. `ctx.send` reads ctx.lang lazily,
    // so setting it after routing still applies to every skill send.
    ctx.lang = (isContinuation ? session?.lang : null) || "en";

    // ctx.send — an ORDINARY reply. It is NEVER scanned, sniffed or second-guessed.
    ctx.send = (number, text, opts) => send(number, text, ctx.lang, opts);

    // SELF-LEARNING (soft failures) — the BIG category, and the one a skill must DECLARE.
    //
    // Most of the time the secretary fails, it does not throw: it understands the order,
    // fails to execute it, and says so politely ("I understood the request but failed to
    // create it in Google", "I hit an error while thinking", "Something went wrong with
    // your tasks"). None of that reaches a catch block, and it is the failure the owner
    // actually experiences.
    //
    // A MALFUNCTION IS EXACTLY THREE THINGS:
    //   1. a code error (the catch blocks below),
    //   2. a soft landing of an UNCOMPLETED task — declared here, via ctx.sendFailure,
    //   3. the owner saying it got something wrong (the `feedback` skill).
    //
    // Everything else is GUIDANCE, and guidance is not a malfunction: "reply to the audio
    // you want transcribed", "which task did you mean?", "what should the task say?",
    // "your list is empty". The secretary asking a question, or truthfully reporting an
    // empty result, is it working — not failing.
    //
    // That line cannot be drawn by reading the prose. An earlier version of this file
    // scanned every ctx.send with a regex, and it was wrong in BOTH directions: it missed
    // half the real failures ("I hit an error while thinking" contains no failure word) and
    // it would have flagged "I couldn't find: X. Which one did you mean?" — a clarifying
    // QUESTION — as a malfunction. Only the skill knows whether it just failed the owner or
    // just asked him something, so only the skill gets to say. There is no runtime guessing.
    // `scripts/selflearning-selftest.mjs` lints the call sites so a forgotten one is caught
    // in the test run, not in production.
    ctx.sendFailure = async (number, text, opts) => {
      const res = await send(number, text, ctx.lang, opts);
      await fireCapture(ctx, {
        phase: "soft",
        taskId: ctx._turn.skill || "soft",
        softMessage: String(text),
        detection: "ctx.sendFailure (declared by the skill)",
      });
      return res;
    };

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
      ctx._turn.skill = session.skill; // so a soft report names the skill, not just "soft"
      try {
        await run(ctx);
      } catch (e) {
        console.error(`Session skill '${session.skill}' error:`, e);
        await send(number, orch(ctx.lang, "continuationError"), ctx.lang);
        await fireCapture(ctx, {
          phase: "throw:continuation",
          taskId: session.skill,
          error: e,
        });
      }
      return;
    }

    // FRESH COMMAND: a new tagged order overrides any pending session.
    if (session) await sessions.clear(remoteJid);

    // ROUTER: decide which skill(s) to run, detect the conversation language — and, in the
    // SAME call, extract the chosen skill's declared inputs.
    let tasks;
    let infoFor = null; // the ONE task allowed to receive the extracted payload
    let routedInfo = null;
    try {
      const routed = await route(ctx);
      tasks = routed.tasks;
      ctx.lang = routed.lang || ctx.lang; // reply in the detected language
      routedInfo = routed.info;

      // The merged call extracted the FIRST task's declared inputs. PLAIN CODE — no AI —
      // decides whether the payload is usable. Shape-invalid (or a task that declares
      // nothing) => the skill re-extracts for itself, which is the old path, unchanged.
      // Shape-VALID but incomplete => hand it over anyway: the skill's own clarification
      // pass fills the gaps exactly as it does today.
      //
      // Scoping it to tasks[0] is not a detail. On a dual-intent turn the payload belongs to
      // the FIRST skill; a second skill must be handed null and extract for itself. Handing a
      // skill someone else's payload is how you act on the wrong data.
      const primary = CATALOG.find((c) => c.id === tasks[0]);
      const gate = checkPayload(primary?.inputs, routedInfo);
      infoFor = gate.shapeOk ? tasks[0] : null;
      if (!gate.shapeOk && routedInfo)
        console.log("ROUTER payload withheld:", gate.problems.join("; "));
    } catch (e) {
      console.error("Router error:", e);
      await send(number, orch(ctx.lang, "routerError"), ctx.lang);
      await fireCapture(ctx, { phase: "throw:router", taskId: "router", error: e });
      return;
    }
    console.log("ROUTER -> tasks:", tasks, "lang:", ctx.lang);

    // No recognized skill. The router ran FINE and understood nothing — that's not a bug,
    // it's a MISSING CAPABILITY, and the highest-signal machine report of the four.
    if (!tasks.length || tasks.every((x) => !SKILLS[x])) {
      const names = CATALOG.map((c) => c.id).join(", ");
      await send(number, orch(ctx.lang, "notUnderstood", names), ctx.lang);
      await fireCapture(ctx, {
        phase: "unrouted",
        taskId: "router",
        unroutedOrder: ctx.order,
      });
      return;
    }

    // Dispatch to each skill in the order decided by the router.
    for (const task of tasks) {
      const run = SKILLS[task];
      if (!run) continue;
      ctx._turn.skill = task; // so a soft report names the skill, not just "soft"
      // The pre-extracted payload, for the ONE task it belongs to and no other. Every other
      // skill sees null and extracts for itself — which is exactly what it does today, so a
      // skill that ignores ctx.info is behaviourally untouched by all of this.
      ctx.info = task === infoFor ? routedInfo : null;
      try {
        await run(ctx);
      } catch (e) {
        console.error(`Skill '${task}' error:`, e);
        await send(number, orch(ctx.lang, "skillError"), ctx.lang);
        await fireCapture(ctx, { phase: "throw:skill", taskId: task, error: e });
      }
    }
  } catch (e) {
    console.error("Webhook error:", e);
  }
});

// ---- Boot: the STORED tag list wins over the SECRETARY_TAG seed --------------
// `await settings.ready` is the load-bearing word here. createSettings() fires its Redis
// connect without blocking (same shape as sessions.js), so live() is false for the first
// moments of the process. Reading the stored tags WITHOUT awaiting ready would race the
// connection, miss them, and fall back to the env seed — she would answer to the changed tag
// until the first restart and then silently forget it. Top-level await; the package is ESM.
await settings.ready;
try {
  const stored = await settings.loadTags();
  if (stored?.length && setTags(stored)) {
    console.log(`tags: ${TAGS.join(", ")} (source: stored setting)`);
  } else {
    console.log(`tags: ${TAGS.join(", ")} (source: SECRETARY_TAG seed)`);
  }
} catch (e) {
  // A settings store that cannot be read is a degraded store, not a failed boot: she still
  // answers to the seed.
  console.error("tags: could not read the stored setting, using the seed:", e.message);
}

app.listen(process.env.PORT || 3000, () =>
  console.log("Secretary v2.0 (orchestrator) listening on port 3000")
);
