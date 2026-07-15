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
//
//  DUAL-TAG PARALLEL RUN. Two flows live in this one process, selected by the summon tag on
//  each message, as early as possible in the webhook handler:
//    - @assistant (SECRETARY_TAG)     -> the LEGACY flow: route -> dispatch, on FROZEN pre-card
//                                        code under ./legacy/. This is the owner's daily driver
//                                        and it is byte-for-byte the committed (HEAD) behaviour.
//    - @mary (SECRETARY_TAG_NEW)      -> the NEW flow: the orchestrator turn loop (three-state
//                                        cycle, converted skills, read-back). The owner tests
//                                        this live without touching @assistant.
//  The two flows share only the truly-invariant rails (message I/O, sessions, formatting, the
//  wrapped Anthropic client, self-learning) — exactly what the legacy path used at HEAD. They do
//  NOT share the router, the input contract, or assistant_settings. The NEW flow's turn loop is
//  inline in the webhook handler; the OLD flow is runLegacyFlow() (frozen dispatch), below it.
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
  buildLabeledTranscript,
  contactName,
} from "./lib/whatsapp.js";
import { createSessions } from "./lib/sessions.js";
import { createSettings } from "./lib/settings.js";
import { withThinkingDefault } from "./lib/llm.js";
import { checkPayload, describeProblems } from "./lib/inputs.js";
import {
  TAGS,
  NEW_TAGS,
  setTags,
  setNewTags,
  headerFor,
  isOwnMessage,
  matchedTag,
  matchedTagNew,
} from "./lib/identity.js";
import { frame } from "./lib/format.js";
import { route } from "./router/router.js";
import { installLogBuffer } from "./lib/logbuffer.js";
import { captureFailure } from "./lib/selflearning.js";

// ── DUAL-TAG PARALLEL RUN (@assistant = OLD flow, @mary = NEW flow) ──────────
// The legacy (@assistant) path runs entirely on FROZEN copies of the pre-card code under
// ./legacy/ — its own router, prompt, input contract and assistant_settings — none of which the
// new (@mary) path imports. That is the structural guarantee that a bug anywhere in the @mary
// path cannot change what @assistant does: they do not share the code that differs between them.
import { route as routeLegacy } from "./legacy/router.js";
import { checkPayload as checkPayloadLegacy } from "./legacy/inputs.js";
import {
  run as runAssistantSettingsLegacy,
  manifest as legacyAssistantSettingsManifest,
} from "./legacy/assistant-settings.js";

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
// The NEW (@mary) flow's OWN durable tag store, namespaced so it can never overwrite the legacy
// (@assistant) tag key. Same Redis, different key (secretary:settings:new:tags).
const newSettings = createSettings({ url: REDIS_URL, ns: "new" });

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
        // WHO runs the conversation for this skill. "orchestrator" = the model runs the dialogue
        // and reads the return value back; anything else (incl. absent) = "skill", today's shape:
        // the skill asks/confirms for itself. The safe default is what keeps an undeclared skill
        // behaving exactly as it does today. Rendered opaquely into the prompt (lib/inputs.js).
        conversation:
          mod.manifest.conversation === "orchestrator" ? "orchestrator" : "skill",
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
  // The orchestrator's OWN caps/stalls (its failures, no skill's) — deterministic prose, never
  // model-authored: you cannot ask the model to apologise for the model looping or being down.
  turnCap: {
    en: () => "I'm going in circles on this — let's start over. Send it again fresh.",
    pt: () => "Estou dando voltas nisso — vamos recomeçar. Me mande de novo do zero.",
  },
  dispatchCap: {
    en: () => "I've done a few things in a row here — let's pause. Send me the next one fresh.",
    pt: () => "Já fiz algumas coisas seguidas aqui — vamos pausar. Me mande a próxima do zero.",
  },
  repairGiveUp: {
    en: () => "I couldn't get that right after a couple of tries. Can you tell me again, more simply?",
    pt: () => "Não consegui acertar isso depois de algumas tentativas. Pode me dizer de novo, de forma mais simples?",
  },
  // Flag (a): a second, converted skill was asked for alongside the first but cannot run in a
  // batch. The first thing was done; ask him to re-send the other part on its own.
  dispatchSkipped: {
    en: () =>
      "I did the first thing you asked, but I can only handle one of those at a time — send me the other part on its own and I'll take care of it.",
    pt: () =>
      "Fiz a primeira coisa que você pediu, mas só consigo cuidar de uma dessas por vez — me mande a outra parte separadamente que eu resolvo.",
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

// The orchestrator turn-loop bounds. A model that can call skills in a loop can LOOP on skills —
// the bound is code, never the model. `MAX_TURNS` counts only PRODUCTIVE turns (silence is free);
// `MAX_DISPATCHES` is a DISPATCH ceiling, NOT "3 writes" (a dispatch can be a read). `MAX_REPAIRS`
// bounds consecutive payload-validation failures on a converted skill. READBACK_CAP truncates the
// serialized return value shown to the model. MARKER_TTL is the conversation marker's lifetime.
const MAX_TURNS = 10;
const MAX_DISPATCHES = 3;
const MAX_REPAIRS = 2;
const READBACK_CAP = 8192; // bytes
const MARKER_TTL = 15 * 60; // seconds — same as the session default

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

// ---- DUAL-TAG: the LEGACY view of the discovered skills ----------------------
// The discovered SKILLS/CATALOG are the NEW flow's (assistant_settings is the CONVERTED pilot,
// conversation:"orchestrator"). The LEGACY flow gets its OWN catalog + skill map, which differ
// from the NEW ones in exactly ONE skill — assistant_settings:
//   - LEGACY_SKILLS runs the FROZEN propose/classify assistant_settings (from ./legacy/), not the
//     converted one; every other skill's run() is shared (its module is byte-for-byte HEAD but for
//     a one-line `conversation:"skill"` note the legacy router never reads).
//   - LEGACY_CATALOG carries the legacy assistant_settings manifest (inputs:null, no conversation),
//     so the frozen legacy router prompt renders exactly as it did at HEAD. Every other entry keeps
//     its HEAD description + inputs (the `conversation` field is simply not read by legacy/inputs.js).
const LEGACY_SKILLS = { ...SKILLS, assistant_settings: runAssistantSettingsLegacy };
const LEGACY_CATALOG = CATALOG.map((c) =>
  c.id === "assistant_settings"
    ? {
        id: c.id,
        description: legacyAssistantSettingsManifest.description,
        inputs: legacyAssistantSettingsManifest.inputs, // null — the propose flow declares no inputs
      }
    : { id: c.id, description: c.description, inputs: c.inputs }
);

// Per-flow context bits used to BUILD ctx (tags/catalog/settings). The dispatch code itself is the
// NEW turn loop (inline in the webhook) and runLegacyFlow (the OLD frozen dispatch, below).
const NEW_FLOW = { tags: NEW_TAGS, catalog: CATALOG, settings: newSettings };
const LEGACY_FLOW = { tags: TAGS, catalog: LEGACY_CATALOG, settings };

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

    // START: a flow only begins when the OWNER uses a trigger tag. We check BOTH tag lists —
    // the legacy (@assistant) and the new (@mary) — because the summon tag is what selects the
    // flow. `tag` is the tag this message actually starts with (or null) — used below to slice it
    // off. If a message somehow matched BOTH lists (they are meant to be disjoint), the LEGACY
    // flow wins, so @assistant is never starved by a NEW-flow tag collision.
    const legacyTag = fromMe ? matchedTag(text) : null;
    const newTag = fromMe ? matchedTagNew(text) : null;
    const taggedNew = !!newTag && !legacyTag; // a fresh NEW-flow order
    const tag = taggedNew ? newTag : legacyTag;
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

    // WHICH FLOW OWNS THIS MESSAGE (decided as early as possible).
    //  - A TAGGED message: the tag decides — a NEW-flow tag -> the new turn loop, else legacy.
    //  - A CONTINUATION: a SKILL-owned session (it carries a `skill` field) is a legacy-style
    //    hand-off (the legacy dispatch bypass); a MARKER (no `skill`) is the new turn loop. The
    //    NEW flow's converted assistant_settings never opens a skill session, so a
    //    `skill:"assistant_settings"` session can only be the legacy propose/confirm flow — which
    //    makes the split unambiguous. A NEW-flow-dispatched skill (e.g. calendar) that opens its
    //    own session is continued via the same shared skill run in either case, so routing its
    //    continuation through the legacy bypass is behaviourally identical.
    const useNewFlow = isTagged ? taggedNew : !session?.skill;
    const flow = useNewFlow ? NEW_FLOW : LEGACY_FLOW;

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
      tag: tag || flow.tags[0], // the tag this order used (fallback: the flow's primary tag)
      tags: flow.tags, // the ACTIVE flow's accepted-tag list (TAGS legacy | NEW_TAGS new)
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
      // substitute — it falls back to the flow's primary tag and is always truthy.
      isTagged,
      quoted,
      hasQuotedAudio: !!quoted?.hasAudio,
      catalog: flow.catalog, // the ACTIVE flow's catalog (legacy renders assistant_settings as HEAD)
      env: process.env,
      evolution,
      sessions, // store: get/set/clear per-chat state
      settings: flow.settings, // the ACTIVE flow's durable tag store (legacy | namespaced-new)
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

    // ctx.send — an ORDINARY reply. It is NEVER scanned, sniffed or second-guessed. It ALSO
    // records the body it sent onto ctx._turn.said (last outbound wins) — that is the outcome
    // message the read-back turn shows the model. Additive; invisible to every caller.
    ctx.send = (number, text, opts) => {
      ctx._turn.said = String(text);
      return send(number, text, ctx.lang, opts);
    };

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
      ctx._turn.said = String(text); // record onto the turn too (a failing read-back must not re-narrate)
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

    // LEGACY (@assistant) FLOW. A fresh @assistant order, OR any skill-session continuation (a
    // follow-up owned by a SKILL — it carries a `skill` field), runs the FROZEN pre-card dispatch
    // (runLegacyFlow) and returns. Routing every skill-session continuation here is deliberate: the
    // NEW flow's converted skills never open a skill session, and a NEW-flow-dispatched skill (e.g.
    // calendar) that does is continued via the same shared run, so the hand-off is behaviourally
    // identical. The NEW (@mary) turn loop is below; a marker (no `skill` field) reaches it.
    if (!useNewFlow) return await runLegacyFlow(ctx, { session, isContinuation, number });

    // ========================================================================
    //  THE ORCHESTRATOR TURN LOOP  (the NEW / @mary flow).
    //  Reached by a FRESH @mary order, or by an untagged follow-up on a conversation the
    //  orchestrator itself holds (a marker with no `skill` field). The model drives a three-state
    //  cycle — listen / execute / done — and `execute` is non-terminal (a converted skill's
    //  return value drives a read-back turn). The whole loop runs inside THIS webhook request;
    //  only the counters cross a message boundary, on the marker.
    // ========================================================================
    const labeledTranscript = buildLabeledTranscript(conv); // the model's OWN/HIS/CONTACT view

    // The marker + its counters. Carried over on an orchestrator-owned continuation; fresh on a
    // tagged order (which overrides any pending session).
    let marker;
    if (isContinuation) {
      marker = {
        awaitFrom: session.awaitFrom || "owner",
        turns: session.turns || 0,
        dispatches: session.dispatches || 0,
      };
    } else {
      if (session) await sessions.clear(remoteJid);
      marker = { awaitFrom: "owner", turns: 0, dispatches: 0 };
    }

    // Persist / clear the marker ONLY while the orchestrator still owns the key. Edge case 6, BOTH
    // directions: a dispatched skill may have taken the key mid-turn (its `skill` field), and its
    // pending confirmation outranks our marker — `sessions.set` is a FULL overwrite, so we must
    // neither clobber it (writing) nor destroy it (clearing).
    const persistMarker = async () => {
      const cur = await sessions.get(remoteJid);
      if (cur && cur.skill) return; // a skill owns the key now — leave it alone
      await sessions.set(
        remoteJid,
        {
          open: true,
          awaitFrom: marker.awaitFrom,
          lang: ctx.lang,
          turns: marker.turns,
          dispatches: marker.dispatches,
        },
        MARKER_TTL
      );
    };
    const closeMarker = async () => {
      const cur = await sessions.get(remoteJid);
      if (cur && cur.skill) return; // a skill owns the key now — leave it alone
      await sessions.clear(remoteJid);
    };

    // State that rides between turns of THIS webhook only.
    let pendingReadback = null; // { result, said } after a successful CONVERTED dispatch
    let pendingRepair = null; // describeProblems(...) after a failed `ok` validation
    let repairs = 0; // consecutive `ok`-validation failures on the converted primary

    for (let turnIndex = 0; ; turnIndex++) {
      // Build the turn argument. A genuine READ-BACK and a REPAIR are DIFFERENT turns and get
      // DIFFERENT prompts:
      //   - read-back (turn.readback): the skill already acted; the model may NOT execute again
      //     (the write invariant). buildReadbackUser says so.
      //   - repair (turn.repair): the model's last payload failed validation; it MUST re-emit a
      //     CORRECTED execute. buildRepairUser INVITES that — the code already permits it
      //     (thisTurnIsReadback is false on a repair), so the prompt must not fight it.
      const turnArg = { labeledTranscript };
      const thisTurnIsReadback = !!pendingReadback;
      if (pendingReadback) turnArg.readback = pendingReadback;
      else if (pendingRepair) turnArg.repair = pendingRepair;
      pendingReadback = null;
      pendingRepair = null;

      let reply;
      try {
        reply = await route(ctx, turnArg);
      } catch (e) {
        console.error("Router error:", e);
        if (thisTurnIsReadback) {
          // Edge 8: the skill already wrote and already told him. `routerError` would be a lie —
          // say nothing, close if we still own the key, file a report.
          await closeMarker();
          await fireCapture(ctx, { phase: "throw:readback", taskId: "router", error: e });
        } else {
          // Edge 11 (first turn) / a continuation turn: keep today's behaviour.
          await send(number, orch(ctx.lang, "routerError"), ctx.lang);
          await fireCapture(ctx, { phase: "throw:router", taskId: "router", error: e });
        }
        return;
      }

      ctx.lang = reply.lang || ctx.lang;
      console.log("TURN ->", JSON.stringify({ next: reply.next, skills: reply.skills, hasSay: !!reply.say }));

      // Productivity: a deliberate-silence turn ({say:null, next:"listen"}) is FREE — it does not
      // consume MAX_TURNS. Anything else (a reply, an execute, a done, a read-back) counts.
      const productive = !(reply.next === "listen" && !reply.say);
      if (productive) marker.turns++;
      if (marker.turns > MAX_TURNS) {
        await send(number, orch(ctx.lang, "turnCap"), ctx.lang);
        await closeMarker();
        await fireCapture(ctx, { phase: "turn_cap", taskId: "orchestrator", turnCap: MAX_TURNS });
        return;
      }

      if (reply.next === "listen") {
        if (reply.say) await send(number, reply.say, ctx.lang);
        marker.awaitFrom = reply.awaitFrom || marker.awaitFrom || "owner";
        await persistMarker();
        return; // wait for his next message
      }

      if (reply.next === "done") {
        // A degraded/refusal close on the FIRST turn (nothing said, nothing to run, nothing yet
        // dispatched) is "I didn't understand" — keep today's alarm. A later `done` (a read-back
        // close, or "forget it") is an ordinary ending.
        if (turnIndex === 0 && !thisTurnIsReadback && !reply.say && !reply.skills.length) {
          const names = CATALOG.map((c) => c.id).join(", ");
          await send(number, orch(ctx.lang, "notUnderstood", names), ctx.lang);
          await closeMarker();
          await fireCapture(ctx, { phase: "unrouted", taskId: "router", unroutedOrder: ctx.order });
          return;
        }
        if (reply.say) await send(number, reply.say, ctx.lang);
        await closeMarker();
        return;
      }

      // ---- reply.next === "execute" ----------------------------------------------------------
      // THE WRITE INVARIANT: a read-back turn may not execute. Refuse, treat as done, file a report.
      if (thisTurnIsReadback) {
        await fireCapture(ctx, { phase: "readback_execute", taskId: "orchestrator" });
        await closeMarker();
        return;
      }

      // THE DISPATCH CEILING (a dispatch can be a READ, not only a write — see ORCHESTRATOR.md).
      if (marker.dispatches >= MAX_DISPATCHES) {
        await send(number, orch(ctx.lang, "dispatchCap"), ctx.lang);
        await closeMarker();
        await fireCapture(ctx, { phase: "dispatch_cap", taskId: "orchestrator", dispatchCap: MAX_DISPATCHES });
        return;
      }

      // Dispatch the batch — deduped, order preserved (exactly as today's dual-intent dispatch).
      const batch = [...new Set(reply.skills)];
      const dispatchable = batch.filter((s) => SKILLS[s]);
      if (!dispatchable.length) {
        // Only "other" / unknown ids — the router ran fine and understood nothing. Today's path.
        const names = CATALOG.map((c) => c.id).join(", ");
        await send(number, orch(ctx.lang, "notUnderstood", names), ctx.lang);
        await closeMarker();
        await fireCapture(ctx, { phase: "unrouted", taskId: "router", unroutedOrder: ctx.order });
        return;
      }

      const primary = dispatchable[0];
      const primaryEntry = CATALOG.find((c) => c.id === primary);
      const info = reply.info;

      // WHICH tier gates the dispatch is read off the declaration, not guessed:
      //  - "orchestrator" -> gate on `ok` (all three tiers). A failure is the REPAIR loop, NOT a
      //    dispatch: the write budget is untouched, describeProblems goes back to the model.
      //  - "skill"        -> gate on `shapeOk` (today's gate). Shape-valid is handed over,
      //    incomplete or not; shape-invalid is withheld and the skill re-extracts for itself.
      let infoFor = null;
      if (primaryEntry?.conversation === "orchestrator") {
        const g = checkPayload(primaryEntry.inputs, info);
        if (!g.ok) {
          repairs++;
          if (repairs >= MAX_REPAIRS) {
            await send(number, orch(ctx.lang, "repairGiveUp"), ctx.lang);
            await closeMarker();
            await fireCapture(ctx, { phase: "repair_giveup", taskId: primary, repairProblems: g.problems });
            return;
          }
          pendingRepair = describeProblems(g.problems);
          console.log("ORCHESTRATOR repair:", g.problems.join("; "));
          continue; // re-turn — NOT a dispatch (turns already counted; dispatches untouched)
        }
        infoFor = primary;
      } else {
        const g = checkPayload(primaryEntry?.inputs, info);
        infoFor = g.shapeOk ? primary : null;
        if (!g.shapeOk && info) console.log("ROUTER payload withheld:", g.problems.join("; "));
      }

      let skippedConverted = false;
      let result = undefined;
      for (const task of dispatchable) {
        const entry = CATALOG.find((c) => c.id === task);
        // Flag (a): a NON-PRIMARY converted skill cannot run in a batch (no extractor, and the one
        // dispatch/message is spent by the primary). Skip it and tell the owner below; do NOT stash
        // a read-back note — it would never fire when the primary is unconverted (B2).
        if (task !== primary && entry?.conversation === "orchestrator") {
          skippedConverted = true;
          continue;
        }
        const run = SKILLS[task];
        ctx._turn.skill = task; // so a soft report names the skill, not just "soft"
        // The pre-extracted payload, for the ONE task it belongs to and no other. Every other skill
        // sees null and extracts for itself — today's behaviour, unchanged.
        ctx.info = task === infoFor ? info : null;
        ctx.session = null; // never hand a dispatched skill the orchestrator's marker (edge 5)
        ctx._turn.said = null;
        try {
          const r = await run(ctx);
          if (task === primary) result = r; // only the primary's return drives a read-back
        } catch (e) {
          console.error(`Skill '${task}' error:`, e);
          await send(number, orch(ctx.lang, "skillError"), ctx.lang);
          await fireCapture(ctx, { phase: "throw:skill", taskId: task, error: e });
        }
      }
      marker.dispatches++; // one batch = one dispatch
      repairs = 0; // a dispatch happened — reset the consecutive-repair counter

      // Flag (a) signal (B2), on THIS dispatch turn — the only turn guaranteed to fire when the
      // primary is unconverted (no read-back). Then close cleanly (the primary may have opened its
      // own session; closeMarker leaves it alone).
      if (skippedConverted) {
        await send(number, orch(ctx.lang, "dispatchSkipped"), ctx.lang);
        await closeMarker();
        return;
      }

      // Read-back decision. `undefined` (every unconverted primary) -> no read-back, cycle ends.
      // A returned value -> serialize (truncated) and loop back as a READ-BACK turn.
      if (result === undefined) {
        await closeMarker();
        return;
      }
      let serialized;
      try {
        serialized = JSON.stringify(result);
      } catch {
        serialized = String(result);
      }
      if (serialized && serialized.length > READBACK_CAP)
        serialized = serialized.slice(0, READBACK_CAP) + " …[truncated]";
      pendingReadback = { result: serialized, said: ctx._turn.said };
      // loop back — the next iteration is a read-back turn
    }
  } catch (e) {
    console.error("Webhook error:", e);
  }
});

// ============================================================================
//  THE LEGACY (@assistant) FLOW  —  FROZEN pre-card dispatch, verbatim.
//  This is the code the webhook ran at HEAD (before card 55e00052): continuation-bypass, then a
//  single router call, then dispatch to each chosen skill. It runs on the FROZEN legacy modules —
//  routeLegacy (legacy/router.js), checkPayloadLegacy (legacy/inputs.js), LEGACY_SKILLS/
//  LEGACY_CATALOG (legacy assistant_settings swapped in) — so @assistant is byte-for-byte the
//  committed behaviour, and no code the NEW (@mary) flow can reach is on this path.
//  ctx is already built (with the LEGACY flow's tags/catalog/settings); this only dispatches.
// ============================================================================
async function runLegacyFlow(ctx, { session, isContinuation, number }) {
  const { remoteJid } = ctx;

  // CONTINUATION: a follow-up owned by the skill that opened the session. Bypass the router and
  // hand it straight to that skill (it reads ctx.session), exactly as at HEAD.
  if (isContinuation) {
    const run = LEGACY_SKILLS[session.skill];
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

  // ROUTER: decide which skill(s) to run, detect the language — and, in the SAME call, extract the
  // chosen skill's declared inputs. (Frozen legacy router: returns the OLD { tasks, lang, info }.)
  let tasks;
  let infoFor = null; // the ONE task allowed to receive the extracted payload
  let routedInfo = null;
  try {
    const routed = await routeLegacy(ctx);
    tasks = routed.tasks;
    ctx.lang = routed.lang || ctx.lang; // reply in the detected language
    routedInfo = routed.info;

    // Plain code (no AI) decides whether the payload is usable — shape-valid is handed over,
    // shape-invalid is withheld and the skill re-extracts for itself (HEAD behaviour). Scoped to
    // tasks[0]: on a dual-intent turn the payload belongs to the FIRST skill only.
    const primary = LEGACY_CATALOG.find((c) => c.id === tasks[0]);
    const gate = checkPayloadLegacy(primary?.inputs, routedInfo);
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

  // No recognized skill — the router ran fine and understood nothing (a missing capability).
  if (!tasks.length || tasks.every((x) => !LEGACY_SKILLS[x])) {
    const names = LEGACY_CATALOG.map((c) => c.id).join(", ");
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
    const run = LEGACY_SKILLS[task];
    if (!run) continue;
    ctx._turn.skill = task; // so a soft report names the skill, not just "soft"
    // The pre-extracted payload, for the ONE task it belongs to and no other. Every other skill
    // sees null and extracts for itself — which is exactly what it does today.
    ctx.info = task === infoFor ? routedInfo : null;
    try {
      await run(ctx);
    } catch (e) {
      console.error(`Skill '${task}' error:`, e);
      await send(number, orch(ctx.lang, "skillError"), ctx.lang);
      await fireCapture(ctx, { phase: "throw:skill", taskId: task, error: e });
    }
  }
}

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

// The NEW (@mary) flow's stored tags, from its OWN namespaced store — same load-over-seed rule,
// fully independent of the legacy load above, so the two can never overwrite each other.
await newSettings.ready;
try {
  const stored = await newSettings.loadTags();
  if (stored?.length && setNewTags(stored)) {
    console.log(`new-tags: ${NEW_TAGS.join(", ")} (source: stored setting)`);
  } else {
    console.log(`new-tags: ${NEW_TAGS.join(", ")} (source: SECRETARY_TAG_NEW seed)`);
  }
} catch (e) {
  console.error("new-tags: could not read the stored setting, using the seed:", e.message);
}

app.listen(process.env.PORT || 3000, () =>
  console.log("Secretary v2.0 (orchestrator) listening on port 3000")
);
