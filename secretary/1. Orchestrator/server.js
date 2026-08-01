// ============================================================================
//  SECRETARY (v2.0)  —  ORCHESTRATOR.
//  Receives the Evolution webhook, filters (fromMe + trigger tag), builds the
//  context, DISCOVERS the available skills (../3. Mary Skills/*/skill.js), calls the
//  ROUTER to classify intent and dispatches to the chosen skill(s).
//
//  Flow:  webhook -> filter -> context -> router -> skill(s)
//
//  Adding a new skill = create a folder under "3. Mary Skills/" with a skill.js that
//  exports { manifest, run }. The orchestrator loads it on its own at boot; no
//  need to edit this file or the router.
//
//  DUAL-TAG PARALLEL RUN. Two flows live in this one process, selected by the summon tag on each
//  message, as early as possible in the webhook handler:
//    - @mary (SECRETARY_TAG_NEW)      -> the NEW flow: the orchestrator turn loop (three-state
//                                        cycle listen/execute/done, converted skills, read-back,
//                                        native-tools answer pass). Inline in the webhook handler.
//    - @assistant (SECRETARY_TAG)     -> the LEGACY flow: route -> dispatch, on FROZEN
//                                        pre-retirement code under ./legacy/ + "2. Skills/". This
//                                        is runLegacyFlow(), below the webhook handler.
//  The two flows share only the truly-invariant rails (message I/O, sessions, formatting, the
//  wrapped Anthropic client, self-learning). They do NOT share the router, the input contract, or
//  assistant_settings. An UNTAGGED continuation is routed by an EXPLICIT session flow stamp
//  (legacySessions writes flow:"legacy"), never by the absence of a `.skill` field — see
//  identity.useNewFlowFor and the legacySessions wrapper.
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
  inboundMedia,
  mediaBlockFor,
  remember,
  combine,
  buildTranscript,
  buildLabeledTranscript,
  contactName,
  historyMediaFile,
  mentionsFile,
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
  useNewFlowFor,
} from "./lib/identity.js";
import { frame } from "./lib/format.js";
import { route, extract, needsTaggedReplyFloor } from "./router/router.js";
import { renderStateBlock } from "./router/prompt.js";
import { transcribeAudio } from "./lib/transcribe.js";
import { installLogBuffer } from "./lib/logbuffer.js";
import { captureFailure } from "./lib/selflearning.js";
import { MAINTAINED_LANGS, resolveTurnLang, shouldForceTranslateSay, translationNeeded } from "./lib/lang.js";

// ── LEGACY (@assistant) FLOW — restored beside @mary (card: restore-assistant-flow) ─────────
// The legacy (@assistant/@assistente) path runs entirely on FROZEN copies of the pre-retirement
// code under ./legacy/ — its own router, prompt, input contract and assistant_settings — none of
// which the @mary path imports. That is the structural guarantee that a bug anywhere in the @mary
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
// The LEGACY (@assistant) flow discovers its OWN isolated skill tree under "2. Skills/". Same
// discovery machinery (loadSkills below is parametrized), a different folder — so @assistant keeps
// loading the frozen "2. Skills/" tree while @mary routes to the converted pure-task stack.
const SKILLS_DIR = path.join(__dirname, "..", "2. Skills");
// The @mary flow discovers its skill tree (the converted pure-task stack) via loadSkills below.
const NEW_SKILLS_DIR = path.join(__dirname, "..", "3. Mary Skills");

// ---- Config -----------------------------------------------------------------
const EVOLUTION_URL = process.env.EVOLUTION_URL || "http://api:8080";
const APIKEY = process.env.EVOLUTION_APIKEY;
const INSTANCE = process.env.EVOLUTION_INSTANCE || "secretary";
// Trigger tags + reply header live in lib/identity.js (single source of truth,
// shared with skills). NEW_TAGS is the accepted-tag list; headerFor(lang)/isOwnMessage/
// matchedTagNew are imported above.
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";
// Cheap model for the long-tail translation fallback (see localizeBody).
const TRANSLATE_MODEL =
  process.env.TRANSLATE_MODEL || "claude-haiku-4-5-20251001";
// The model pinned for a file-carrying @mary turn (see the media-prep block + router route()).
// Independent of CLAUDE_MODEL so the droplet's model choice never disables vision/PDF reading.
// claude-haiku-4-5 also supports vision + PDF if a cheaper pin is wanted.
const VISION_MODEL = process.env.VISION_MODEL || "claude-sonnet-5";
const IMAGE_MAX_BYTES = 5 * 1024 * 1024; // Anthropic per-image cap (~5 MB)
const PDF_MAX_BYTES = 32 * 1024 * 1024; // Anthropic per-request PDF cap (32 MB)
const MAX_FILES_PER_TURN = 10; // per-turn file cap (a real turn holds 2–3; 10 is hostile-payload headroom)
const OWNER_NAME = process.env.OWNER_NAME || "User";
// The owner's OWN WhatsApp number/JID, for the additive owner-DM note (ctx.dmOwner below).
// Optional — when unset, ctx.dmOwner is a no-op. Whether Evolution delivers a message the
// account sends to its own number is an ops question, not a code assumption.
const OWNER_NUMBER = process.env.OWNER_JID || process.env.OWNER_NUMBER || null;
// Languages the secretary writes natively (skills carry en/pt maps). Any other
// detected language is handled by the LLM-translation fallback in send().
// MAINTAINED_LANGS now lives in ./lib/lang.js (imported above) so the pin policy
// and the maintained-language set share one rails module.

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
// LEGACY (@assistant) FLOW-STAMP WRAPPER over the SAME session store. Its `.set` injects
// flow:"legacy" into every stored value, so a legacy skill session is self-identifying: an
// untagged continuation is then routed by that explicit stamp (useNewFlowFor), NOT by the absence
// of a `.skill` field — which at HEAD a @mary-dispatched skill may legitimately own (SCOPE edge
// case 5). The @mary flow keeps the RAW `sessions` store (no flow stamp). `.get`/`.clear` pass
// through unchanged; `sessions.set`'s default-ttl applies when `ttl` is undefined (sessions.js:75).
const legacySessions = {
  get: (jid) => sessions.get(jid),
  set: (jid, value, ttl) => sessions.set(jid, { ...value, flow: "legacy" }, ttl),
  clear: (jid) => sessions.clear(jid),
};
// Durable settings on the SAME Redis (no TTL, own key space). Today: the tag list the owner
// summons her with, which he can change by asking (the `assistant_settings` skill). SECRETARY_TAG_NEW
// is the SEED; a stored value wins — see the boot load below. Namespaced (secretary:settings:new:tags).
const newSettings = createSettings({ url: REDIS_URL, ns: "new" });
// The LEGACY (@assistant) flow's OWN durable tag store, DEFAULT namespace (secretary:settings:tags),
// so it can never overwrite the @mary key. SECRETARY_TAG is its SEED; a stored value wins (boot load).
const settings = createSettings({ url: REDIS_URL });

const seen = new Set(); // dedup by messageId

// ---- Skill discovery --------------------------------------------------------
// Scans "<dir>/*/skill.js". Each skill exports:
//   export const manifest = { id, description }
//   export async function run(ctx) { ... }
//   export const capabilities = { name: (ctx, ...args) => ... }   // OPTIONAL
// -> SKILLS: { [id]: run }  |  CATALOG: [{ id, description }] (the router's menu)
//  | CAPS: { [id]: capabilities } — the internal skill-to-skill API (see ctx.callSkill).
//    Capabilities are NEVER shown to the router; they let one skill compose another
//    (e.g. task_action delegating a "task for someone" to calendar_action.startCreate)
//    without importing its file — decoupled from folder paths, graceful when absent. The @mary
//    boot call (loadSkills(NEW_SKILLS_DIR)) discovers no capabilities and simply ignores `caps`.
async function loadSkills(dir = NEW_SKILLS_DIR) {
  const skills = {};
  const catalog = [];
  const caps = {};
  let entries = [];
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch (e) {
    console.error("Could not read the skills folder:", dir, e.message);
    return { skills, catalog, caps };
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    const file = path.join(dir, e.name, "skill.js");
    try {
      const mod = await import(pathToFileURL(file).href);
      const id = mod.manifest?.id;
      if (!id || typeof mod.run !== "function") {
        console.error(`skill '${e.name}' ignored: missing manifest.id or run()`);
        continue;
      }
      // CATALOG TRIM (items 4 + 5): a skill flagged `routable:false` (transcribe_audio, flight_search)
      // is present on disk but NOT wired into the router catalog or the dispatch map — it is dormant.
      // A future re-add is a one-line manifest flip. Audio is handled by system-side transcription;
      // flight questions are answered natively via the turn call's toolset.
      if (mod.manifest.routable === false) {
        console.log(`skill "${e.name}" present but not routable -> ${id}`);
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
async function localizeBody(text, lang, { force = false, sourceLang } = {}) {
  const l = (lang || "en").toLowerCase();
  if (!text || !translationNeeded(sourceLang, l, { force })) return text;
  try {
    const msg = await anthropic.messages.create({
      model: TRANSLATE_MODEL,
      max_tokens: 1024,
      system: `Translate the user's message into the language with ISO 639-1 code "${l}". Output ONLY the translation — no preamble, no quotes, no notes. Preserve EXACTLY, unchanged: URLs, email addresses, numbers, dates, times, and every line break and bullet/dash character. Do NOT translate proper nouns (people's names, event titles). Translate the prose only and keep the original layout and formatting. If the message is ALREADY written entirely in that language, output it EXACTLY as received, verbatim — do not translate it, do not comment on it, output nothing else.`,
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
  const body = await localizeBody(text, lang, { sourceLang: opts.sourceLang });
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
  // A directly-tagged owner order that the unified turn resolved to silence (say:null, no
  // execute) — the server-side floor substitutes this so the owner is never left in silence.
  noReply: {
    en: () => "Sorry — I didn't catch that. Could you say it again?",
    pt: () => "Desculpa — não peguei isso. Pode repetir?",
  },
  // Inbound-media plumbing notices (the orchestrator's OWN, like turnCap/dispatchCap — sent via
  // the bare send(), NOT ctx.send; informational, so NOT *Failed/*Error keys). One per distinct
  // reason a file could not be relayed on a @mary turn.
  fileDownloadFailed: {
    en: () => "I couldn't open that file — try sending it again.",
    pt: () => "Não consegui abrir esse arquivo — tente enviar de novo.",
  },
  fileTooLarge: {
    en: () => "That file is too big for me to read. Can you send a smaller one, or a screenshot?",
    pt: () => "Esse arquivo é grande demais para eu ler. Pode mandar um menor, ou um print?",
  },
  fileTooMany: {
    en: () => "That's a lot of files at once — send me a few at a time and I'll read them.",
    pt: () => "São muitos arquivos de uma vez — me mande alguns por vez que eu leio.",
  },
  fileUnsupported: {
    en: () => "I can only read images and PDFs right now — send it as one of those and I'll read it.",
    pt: () => "Por enquanto só consigo ler imagens e PDFs — me manda assim que eu leio.",
  },
  // Flag (a): a second, converted skill was asked for alongside the first but cannot run in a
  // batch. The first thing was done; ask him to re-send the other part on its own.
  dispatchSkipped: {
    en: () =>
      "I did the first thing you asked, but I can only handle one of those at a time — send me the other part on its own and I'll take care of it.",
    pt: () =>
      "Fiz a primeira coisa que você pediu, mas só consigo cuidar de uma dessas por vez — me mande a outra parte separadamente que eu resolvo.",
  },
  // System-side audio transcription notice (item 4). Informational (the model can't ingest audio,
  // so the system transcribes it; when that fails there is nothing to fold into the turn). Sent via
  // bare send(), like the file-plumbing notices above.
  audioFailed: {
    en: () => "I couldn't transcribe that audio — try sending it again, or type it out.",
    pt: () => "Não consegui transcrever esse áudio — tente enviar de novo, ou escreva o texto.",
  },
  // The MANDATORY sign-off (item 7). System-guaranteed on a CLEAN task-completion close — after the
  // model's own `say`, before the marker closes — so it cannot be skipped. Interpolates the real
  // trigger tag this conversation used (ctx.tag). NOT sent on error/cap closes, nor on a chatter-
  // ignore turn (which keepListening:true structurally never reaches a close).
  finishedSignOff: {
    en: (tag) => `I have finished this task. Call me ${tag} again if you need further assistance`,
    pt: (tag) => `Terminei esta tarefa. Me chame com ${tag} de novo se precisar de mais ajuda`,
  },
};

// Pick an orchestrator string for `lang`, falling back to the English copy (which
// the send() fallback then translates for a non-en/pt language).
function orch(lang, key, ...args) {
  const entry = ORCH_MSG[key];
  const fn = (entry && (entry[lang] || entry.en)) || (() => "");
  return fn(...args);
}

// Max depth of skill→skill delegation (ctx.callSkill), a loop/recursion backstop. Used by the
// legacy (@assistant) "2. Skills/" tree only; inert on the @mary path (no @mary skill calls it).
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

// NATIVE SERVER-SIDE TOOLS now ride the UNIFIED turn call (router.route), not a separate answer
// pass. The bundle is still built by lib/nativeTools.js off process.env, and route() reads its own
// per-create timeout / pause_turn-hop cap from ctx.env (NATIVE_ANSWER_TIMEOUT_MS / NATIVE_MAX_TOOL_HOPS).
// There is no per-conversation answer budget any more — MARKER_TTL + MAX_TURNS bound the turn loop.

// CONVERSATION STATE (Unit 2). One `state` object rides the marker (goal + decision log + last
// extraction payload + pending need + didWork), feeding every turn call via renderStateBlock. Caps:
// the log keeps the most-recent STATE_LOG_CAP entries; each text field is truncated to STATE_TEXT_CAP.
const STATE_LOG_CAP = 12;
const STATE_TEXT_CAP = 240;

// A fresh conversation state, opened on a tagged order. `goal` is the opening order (the intent).
function freshState(order) {
  return {
    goal: (order || "").slice(0, STATE_TEXT_CAP) || null,
    pendingNeed: null, // model-declared on a keepListening turn
    payload: null, // last successful extraction for the primary
    didWork: false, // a task ran this conversation (the sign-off gate)
    log: [], // decision log, recent-K, newest last
  };
}

// Append one PRODUCTIVE turn to the decision log (deliberate-silence turns are NOT logged). An
// execute entry also carries `fn` (the primary id); its `outcome` is filled once the read-back is
// prepared (setLastOutcome). Bounded by STATE_LOG_CAP.
function logTurn(state, { i, keepListening, execute, say }) {
  if (!state) return;
  const e = {
    i,
    keepListening: !!keepListening,
    execute: Array.isArray(execute) ? execute : [],
    say: say ? String(say).slice(0, STATE_TEXT_CAP) : null,
  };
  if (e.execute.length) e.fn = e.execute[0];
  state.log.push(e);
  while (state.log.length > STATE_LOG_CAP) state.log.shift();
}

// Attach a dispatch's serialized outcome to the most recent execute log entry.
function setLastOutcome(state, outcome) {
  if (!state || !Array.isArray(state.log)) return;
  for (let i = state.log.length - 1; i >= 0; i--) {
    if (state.log[i].execute && state.log[i].execute.length) {
      state.log[i].outcome = String(outcome).slice(0, STATE_TEXT_CAP);
      return;
    }
  }
}

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
// The @mary flow's discovered tree (the converted pure-task stack).
const { skills: NEW_SKILLS, catalog: NEW_CATALOG } = await loadSkills(NEW_SKILLS_DIR);
console.log("mary skills:", NEW_CATALOG.map((c) => c.id).join(", ") || "(none!)");

// The LEGACY (@assistant) flow's OWN discovered tree ("2. Skills/", the frozen pre-retirement
// stack). CAPS is discovered here on purpose — the caps-based Tasks→Calendar startCreate
// delegation is legacy only; the @mary tree exports no capabilities.
const { skills: SKILLS, catalog: CATALOG, caps: CAPS } = await loadSkills(SKILLS_DIR);
console.log("available skills:", CATALOG.map((c) => c.id).join(", ") || "(none!)");

// ---- The LEGACY view of the discovered "2. Skills/" tree ----------------------
// The LEGACY flow gets its OWN catalog + skill map, which differ from the discovered ones in
// exactly ONE skill — assistant_settings:
//   - LEGACY_SKILLS runs the FROZEN propose/classify assistant_settings (from ./legacy/), not the
//     "2. Skills/7. Assistant Settings" converted one; every other skill's run() is the frozen
//     "2. Skills/" module as restored.
//   - LEGACY_CATALOG carries the legacy assistant_settings manifest (inputs:null), so the frozen
//     legacy router prompt renders exactly as it did at retirement. Every other entry keeps its
//     restored description + inputs.
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

// Per-flow context bits used to BUILD ctx (tags/catalog/settings/sessions). The @mary dispatch is
// the turn loop (inline in the webhook); the legacy dispatch is runLegacyFlow (frozen), below.
// NEW_FLOW.sessions === sessions (the raw store), so the @mary ctx.sessions is unchanged; the
// legacy flow uses legacySessions (the flow-stamp wrapper).
const NEW_FLOW    = { tags: NEW_TAGS, catalog: NEW_CATALOG, settings: newSettings, sessions };
const LEGACY_FLOW = { tags: TAGS,     catalog: LEGACY_CATALOG, settings, sessions: legacySessions };

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

    // The turn's inbound media LIST (attachment first, then quote) — computed ONCE, consumed by
    // both the gate-open below and the media-prep block before the @mary turn loop. [] when none.
    let files = inboundMedia(data, quoted);
    // The direct attachment's caption ("" for a quote / none). A captioned document has text==""
    // (extractText has no document branch), so its tag+order ride the caption, not `text`.
    const attachmentCaption = files.find((m) => m.source === "attachment")?.caption || "";

    // Never react to the secretary's OWN messages. They arrive with fromMe=true
    // (same account as the owner), so this header check is the ONLY thing telling
    // them apart from a genuine owner message — it must match every header variant
    // the secretary emits (both languages + legacy), see lib/identity.js.
    const isOwnMsg = isOwnMessage(text);

    // Pending conversation state for this chat (confirmations, clarifications, ...).
    const session = await sessions.get(remoteJid);

    // START: a flow only begins when the OWNER uses a trigger tag. We check BOTH tag lists — the
    // legacy (@assistant, SECRETARY_TAG) and the new (@mary, SECRETARY_TAG_NEW) — because the
    // summon tag is what selects the flow. `tag` is the tag this message actually starts with (or
    // null) — used below to slice it off. If a message somehow matched BOTH lists (they are meant
    // to be disjoint), the LEGACY flow wins, so @assistant is never starved by a NEW-flow tag
    // collision. The NEW-flow matcher sees an attachment caption, so a captioned document (whose
    // `text` is "") can open the @mary gate on its caption. The LEGACY matcher keeps seeing `text`
    // only. For a captioned image, text already carries the caption, so gateText === text.
    const gateText = text || attachmentCaption;
    const legacyTag = fromMe ? matchedTag(text) : null;
    const newTag = fromMe ? matchedTagNew(gateText) : null;
    const taggedNew = !!newTag && !legacyTag; // a fresh NEW-flow order; legacy suppresses @mary
    const tag = taggedNew ? newTag : legacyTag;
    const isTagged = !!tag;

    // CONTINUE: while the orchestrator holds an OPEN marker for this chat (session.open — set only
    // by persistMarker), the NEXT untagged message continues the conversation, from ANY sender. The
    // who-lock (awaitFrom) is gone: the model decides each turn from the whole visible conversation
    // whether a message is for it (ASK/PROPOSE, execute) or is chatter to stay silent on. So a guest
    // the owner is scheduling with can answer inline, and the owner's own follow-up continues too —
    // both are just the next message on an open marker. Gating on `open` keeps this to
    // orchestrator-owned conversations (a skill-owned session sets no `open`, so it is untouched).
    let isContinuation = false;
    if (session?.open && !isTagged && !isOwnMsg) isContinuation = true;

    // Ignore everything else (incl. non-owner messages with no session for them).
    if (!isTagged && !isContinuation) return;
    if (id && seen.has(id)) return; // dedup
    if (id) {
      seen.add(id);
      if (seen.size > 500) seen.delete(seen.values().next().value);
    }

    // WHICH FLOW OWNS THIS MESSAGE (decided as early as possible).
    //  - A TAGGED message: the tag decides — a NEW-flow tag -> @mary, else legacy.
    //  - An UNTAGGED continuation: the flow that OWNS the open session, identified by an EXPLICIT
    //    flow stamp (legacySessions writes flow:"legacy"), NOT by the absence of `.skill`. A
    //    @mary-dispatched skill may legitimately own the key with `.skill` at HEAD, so inferring
    //    @mary from `!session.skill` would misroute an untagged @mary continuation into LEGACY — a
    //    strict-isolation break (SCOPE edge case 5). Anything not stamped flow:"legacy" — a @mary
    //    marker, a @mary skill session, or none — defaults to @mary. See identity.useNewFlowFor.
    const useNewFlow = useNewFlowFor(session, isTagged, taggedNew);
    const flow = useNewFlow ? NEW_FLOW : LEGACY_FLOW;

    // Slice off the matched tag by ITS own length (tags can differ in length). NEW (@mary) flow
    // only: source the order from the attachment caption so a caption-borne instruction reaches the
    // model on BOTH the first (tagged) turn AND a mid-session (untagged) continuation of a captioned
    // document — whose `text` is "" (Amendment). The LEGACY branch is byte-identical to retirement.
    const order = useNewFlow
      ? isTagged
        ? gateText.slice(tag.length).trim() // first message, tagged caption -> POST-TAG instruction
        : text.trim() || attachmentCaption.trim() // mid-session continuation -> caption if text empty
      : isTagged
      ? text.slice(tag.length).trim()
      : text.trim(); // LEGACY — byte-identical to retirement
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
    const history = await evolution.fetchHistory(remoteJid);
    const conv = combine(remoteJid, history);
    const transcript = buildTranscript(conv);
    const contact = contactName(conv);
    console.log("TRANSCRIPT>>>\n" + transcript + "\n<<<");

    // Shared context passed to the router and to every skill.
    const ctx = {
      owner: OWNER_NAME,
      tag: tag || flow.tags[0], // the tag this order used (fallback: the flow's primary tag)
      tags: flow.tags, // the flow's accepted-tag list (NEW_TAGS)
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
      catalog: flow.catalog, // the flow's catalog (NEW_CATALOG)
      env: process.env,
      evolution,
      sessions: flow.sessions, // the flow's session store: @mary = raw `sessions`; legacy =
      // legacySessions (stamps flow:"legacy" on every write, so its skill sessions self-identify).
      settings: flow.settings, // the flow's durable tag store (legacy default | @mary namespaced-new)
      session: isContinuation ? session : null, // present only on a continuation
      // SELF-LEARNING: one failure report per webhook turn. This is an OBJECT, not a boolean,
      // and that is load-bearing: ctx.sendFailure and the read-back both read/write it, so the
      // frame must share one reference rather than a copied flag.
      _turn: { captured: false },
    };

    // Conversation language. On a continuation it's persisted in the session (so a
    // "yes" answers in the language the flow started in); on a fresh command the
    // router fills it in below. Default English. `ctx.send` reads ctx.lang lazily,
    // so setting it after routing still applies to every skill send.
    // The conversation's opening language, carried across the fresh-turn session clear
    // (:495) because `session` was read at :331, before that clear. Present on an ongoing
    // conversation (continuation OR a fresh @mary that the marker still owns); null only
    // when there is no live marker — i.e. a brand-new conversation, which will pin below.
    let pinnedLang = session?.openingLang || null;
    ctx.lang = pinnedLang || "en";

    // ctx.send — an ORDINARY reply. It is NEVER scanned, sniffed or second-guessed. It ALSO
    // records the body it sent onto ctx._turn.said (last outbound wins) — that is the outcome
    // message the read-back turn shows the model. Additive; invisible to every caller.
    ctx.send = (number, text, opts) => {
      ctx._turn.said = String(text);
      return send(number, text, ctx.lang, opts);
    };

    // ctx.dmOwner — an ADDITIVE private note to the OWNER'S OWN number (OWNER_JID/OWNER_NUMBER),
    // framed and localized exactly like ctx.send (via the same choke-point send()). A NO-OP when
    // OWNER_NUMBER is unset, so a skill that calls it stays safe in a deployment that never set
    // the var. Additive: it changes no existing signature or caller — today only
    // calendar_action's Contacts save-back uses it. It is NOT recorded onto ctx._turn.said (it is
    // a side note to the owner, not this chat's outbound reply).
    ctx.dmOwner = (text) => {
      if (!OWNER_NUMBER) return Promise.resolve(null);
      return send(OWNER_NUMBER, text, ctx.lang);
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

    // Send the router's own free-form prose in the PINNED language. localizeBody (inside
    // send) already translates say into any non-maintained pinned lang, but passes en/pt
    // through untouched — so the model can author say in PT under an EN-pinned header. When
    // both langs are maintained and differ, force-translate say from its known source
    // language (reply.lang) to ctx.lang first; send() then passes the already-correct body
    // through. On the common case (say already in ctx.lang) this is a no-op, no LLM call.
    const sendSay = async (say, sayLang) => {
      const body = shouldForceTranslateSay(sayLang, ctx.lang)
        ? await localizeBody(say, ctx.lang, { force: true, sourceLang: sayLang })
        : say;
      return send(number, body, ctx.lang, { sourceLang: sayLang });
    };

    // Cross-skill composition (legacy "2. Skills/" tree only; inert on the @mary path — no @mary
    // skill calls these). `hasSkill` guards a friendly fallback; `callSkill` invokes another skill's
    // exported capability, auto-injecting THIS ctx (so the callee shares owner/lang/sessions/send)
    // with a depth guard against loops. A session the callee opens is tagged with the callee's id,
    // so its continuations route to the callee. Missing capability -> throws (caught by the
    // per-skill catch). References the legacy CAPS + MAX_SKILL_DEPTH.
    ctx.hasSkill = (id, name) => typeof CAPS[id]?.[name] === "function";
    ctx.callSkill = async (id, name, ...args) => {
      const fn = CAPS[id]?.[name];
      if (!fn) throw new Error(`capability ${id}.${name} unavailable`);
      const depth = (ctx._skillDepth || 0) + 1;
      if (depth > MAX_SKILL_DEPTH)
        throw new Error(`skill-call depth exceeded at ${id}.${name}`);
      return fn({ ...ctx, _skillDepth: depth }, ...args);
    };

    // LEGACY (@assistant) FLOW. A fresh @assistant order, OR any legacy skill-session continuation
    // (routed here because useNewFlowFor found the session stamped flow:"legacy"), runs the FROZEN
    // pre-retirement dispatch (runLegacyFlow) and returns — none of the @mary turn-loop machinery
    // below (marker, media prep, native-tools answer pass) is on this path. The @mary turn loop is
    // below; every message useNewFlow selected reaches it.
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
        turns: session.turns || 0,
        dispatches: session.dispatches || 0,
        // Rehydrate the conversation state (old markers without one fall back to a fresh state).
        state: session.state || freshState(order),
      };
    } else {
      if (session) await sessions.clear(remoteJid);
      marker = { turns: 0, dispatches: 0, state: freshState(order) };
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
          open: true, // the continuation gate keys on this (only the orchestrator marker sets it)
          openingLang: pinnedLang, // the immutable opening language (the pin)
          lang: ctx.lang, // keep for backward-compat with any legacy reader; harmless
          turns: marker.turns,
          dispatches: marker.dispatches,
          state: marker.state, // the conversation state (goal + log + payload + pendingNeed + didWork)
        },
        MARKER_TTL
      );
    };
    const closeMarker = async () => {
      const cur = await sessions.get(remoteJid);
      if (cur && cur.skill) return; // a skill owns the key now — leave it alone
      await sessions.clear(remoteJid);
    };

    // ---- MEDIA PREP (before the turn loop) ----------------------------------------------------
    // Relay any inbound media on THIS turn to the turn call as Anthropic multimodal content.
    // Prepared ONCE here and carried on ctx.media for the whole webhook; route() attaches it on
    // every turn that is not a read-back. ctx.media stays null on a text-only turn (byte-identical).
    ctx.media = null;
    // History->media fallback (GATED): an @mary turn that carries no attachment/quote AND whose
    // words refer to a file ("summarize the PDF above") may be pointing at a file sent earlier.
    // Only then do we reach into history for the most-recent relayable file and relay it like an
    // on-turn file. A calendar/time/chit-chat turn -> mentionsFile(order) is false -> nothing is
    // pulled (no download, no vision). Best-effort (see the guard below): a failure to read an
    // INFERRED file must never hijack a turn the owner didn't mean about a file.
    let filesFromHistory = false;
    if (!files.length && mentionsFile(order)) {
      const h = historyMediaFile(history, Math.floor(Date.now() / 1000));
      if (h) { files = [h]; filesFromHistory = true; }
    }
    if (files.length) {
      if (files.length > MAX_FILES_PER_TURN) {
        // Edge 7: reject the whole turn's media rather than silently truncating.
        await send(number, orch(ctx.lang, "fileTooMany"), ctx.lang);
        await closeMarker();
        return;
      }
      const relayedBlocks = [];
      const problems = new Set(); // fileDownloadFailed | fileTooLarge | fileUnsupported
      for (const f of files) {
        // Unsupported types short-circuit WITHOUT downloading, where the mediaType alone decides.
        if (f.mediaType !== "image" && f.mediaType !== "document") {
          problems.add("fileUnsupported");
          continue;
        }
        let dl;
        try {
          dl = await evolution.getMediaBase64(f.id); // { base64, mimetype: real || "audio/ogg" }
        } catch (e) {
          console.error("media download failed:", e?.message || e);
          problems.add("fileDownloadFailed");
          continue;
        }
        if (!dl?.base64) {
          problems.add("fileDownloadFailed");
          continue;
        }
        const bytes = Buffer.byteLength(dl.base64, "base64");
        const cap = f.mediaType === "image" ? IMAGE_MAX_BYTES : PDF_MAX_BYTES;
        if (bytes > cap) {
          problems.add("fileTooLarge");
          continue;
        }
        // PREFER the real webhook mime; fall back to the download's. mediaBlockFor validates it
        // against the allow-list, so getMediaBase64's "audio/ogg" default can never become an
        // image/PDF block — it is rejected to fileUnsupported, never trusted.
        const mime = f.mimetype || dl.mimetype;
        const block = mediaBlockFor({ mediaType: f.mediaType, mimetype: mime, base64: dl.base64 });
        if (!block) {
          problems.add("fileUnsupported"); // non-pdf doc, or audio/ogg leaking onto an image
          continue;
        }
        relayedBlocks.push(block);
      }
      // Consolidated notes: one per distinct reason, fixed order, each at most once (Edge 5 — name
      // what couldn't be read; never silently drop). Realistic turns hit exactly one.
      if (!filesFromHistory) {
        for (const key of ["fileDownloadFailed", "fileTooLarge", "fileUnsupported"]) {
          if (problems.has(key)) await send(number, orch(ctx.lang, key), ctx.lang);
        }
        if (relayedBlocks.length === 0) {
          // Edge 6: nothing readable -> notes already sent, stop without routing.
          await closeMarker();
          return;
        }
      }
      if (relayedBlocks.length) ctx.media = { blocks: relayedBlocks, model: VISION_MODEL };
    }

    // ---- AUDIO PREP: system-side transcription (item 4, Blocker 1) ----------------------------
    // The model cannot ingest audio, so an audio the owner is asking about is transcribed HERE and
    // folded into the turn prompt as inline text on ctx.audioTranscript (rendered by the prompt
    // builders under an "AUDIO" label — NOT a media block; audio never reaches mediaBlockFor). The
    // classic path is a QUOTED audio (a reply to the audio) — ctx.hasQuotedAudio / quoted.id. On a
    // failure or an empty transcript we send a plain notice and carry on text-only (the model can
    // then nudge him). ctx.audioTranscript is a NEW, additive field on ctx (a rails change).
    ctx.audioTranscript = null;
    if (quoted?.hasAudio && quoted.id) {
      try {
        const dl = await evolution.getMediaBase64(quoted.id);
        const clean = dl?.base64
          ? (await transcribeAudio(ctx.env, Buffer.from(dl.base64, "base64"), ctx.lang)).text?.trim()
          : "";
        if (clean) ctx.audioTranscript = clean;
        else await send(number, orch(ctx.lang, "audioFailed"), ctx.lang);
      } catch (e) {
        console.error("audio transcription failed:", e?.message || e);
        await send(number, orch(ctx.lang, "audioFailed"), ctx.lang);
      }
    }

    // State that rides between turns of THIS webhook only.
    let pendingReadback = null; // { result, said } after a successful dispatch that returned a value
    let repairs = 0; // consecutive extraction-validation failures on the orchestrator primary

    for (let turnIndex = 0; ; turnIndex++) {
      // Build the turn argument. A READ-BACK (turn.readback) is a distinct turn — the task already
      // acted; the model may NOT execute again (the write invariant; buildReadbackUser says so). A
      // repair is NO LONGER a decision turn: it re-runs extract() below, so route() never receives a
      // repair arg. Every turn carries the rendered conversation state (renderStateBlock).
      const turnArg = { labeledTranscript, stateBlock: renderStateBlock(marker.state) };
      const thisTurnIsReadback = !!pendingReadback;
      if (pendingReadback) turnArg.readback = pendingReadback;
      pendingReadback = null;

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

      ctx.lang = resolveTurnLang(pinnedLang, reply.lang);
      if (!pinnedLang) pinnedLang = ctx.lang; // first turn of a new conversation: pin it now
      console.log(
        "TURN ->",
        JSON.stringify({ keepListening: reply.keepListening, execute: reply.execute, hasSay: !!reply.say })
      );

      const execute = reply.execute || [];

      // Productivity: a deliberate-silence turn (say:null, keepListening:true, execute:[]) is FREE —
      // it does not consume MAX_TURNS. Anything else (a reply, an execute, a close, a read-back) counts.
      const productive = !(reply.keepListening && !reply.say && execute.length === 0);
      if (productive) marker.turns++;
      if (marker.turns > MAX_TURNS) {
        await send(number, orch(ctx.lang, "turnCap"), ctx.lang);
        await closeMarker();
        await fireCapture(ctx, { phase: "turn_cap", taskId: "orchestrator", turnCap: MAX_TURNS });
        return;
      }

      // Log the productive turn into the conversation state (Unit 2). Silence turns are NOT logged.
      if (productive) logTurn(marker.state, { i: turnIndex, keepListening: reply.keepListening, execute, say: reply.say });

      // ---- LISTEN: execute empty & keepListening -> reply/ask and stay open ---------------------
      if (!execute.length && reply.keepListening) {
        if (reply.say) await sendSay(reply.say, reply.lang);
        else if (needsTaggedReplyFloor({ isTagged, turnIndex, hasSay: !!reply.say, executeCount: execute.length })) {
          console.log("FLOOR -> tagged silent no-op at LISTEN, sending noReply notice");
          await send(number, orch(ctx.lang, "noReply"), ctx.lang);
        }
        marker.state.pendingNeed = reply.pendingNeed || null; // what we await, if anything
        await persistMarker();
        return; // wait for his next message
      }

      // ---- CLOSE: execute empty & !keepListening -> close ---------------------------------------
      if (!execute.length) {
        // A DEGRADED close (the router refused or produced an unparseable reply — router.js sets
        // reply.degraded) is the real schema-drift alarm: keep the "I didn't understand" menu AND
        // the unrouted capture. A LEGITIMATE empty close has the SAME shape but is the model
        // deliberately closing chit-chat / a no-op / an out-of-scope line — NOT a malfunction.
        if (reply.degraded && turnIndex === 0 && !thisTurnIsReadback) {
          const names = NEW_CATALOG.map((c) => c.id).join(", ");
          await send(number, orch(ctx.lang, "notUnderstood", names), ctx.lang);
          await closeMarker();
          await fireCapture(ctx, { phase: "unrouted", taskId: "router", unroutedOrder: ctx.order });
          return;
        }
        if (reply.say) await sendSay(reply.say, reply.lang);
        else if (needsTaggedReplyFloor({ isTagged, turnIndex, hasSay: !!reply.say, executeCount: execute.length })) {
          console.log("FLOOR -> tagged silent no-op at CLOSE, sending noReply notice");
          await send(number, orch(ctx.lang, "noReply"), ctx.lang);
        }
        // MANDATORY SIGN-OFF (Unit 2, item 7): system-guaranteed on a CLEAN task-completion close —
        // after any model `say`, before the marker closes, so it cannot be skipped. Fires ONLY when
        // a task actually ran this conversation (state.didWork) and this is not a degraded close. A
        // chatter-ignore turn is keepListening:true and structurally never reaches this close.
        if (marker.state.didWork && !reply.degraded) {
          await send(number, orch(ctx.lang, "finishedSignOff", ctx.tag), ctx.lang);
        }
        await closeMarker();
        return;
      }

      // ---- EXECUTE: execute non-empty --------------------------------------------------------
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
      const batch = [...new Set(execute)];
      const dispatchable = batch.filter((s) => NEW_SKILLS[s]);
      if (!dispatchable.length) {
        // The model chose `execute` but named no dispatchable skill (unknown/dormant ids — route()
        // already filters execute to valid catalog ids). This is the model deciding nothing here is
        // for it — close cleanly, no menu, no unrouted capture. The schema-drift alarm still fires
        // on the degraded close path above.
        await closeMarker();
        return;
      }

      const primary = dispatchable[0];
      const primaryEntry = NEW_CATALOG.find((c) => c.id === primary);

      // ---- TWO-PHASE: PRODUCE the payload (extract), then VALIDATE it (checkPayload). On an
      // orchestrator-tier `ok` failure, RE-RUN extract() with the problems threaded in — targeted
      // payload self-correction now reaches the call that produces the payload — bounded by
      // MAX_REPAIRS, then repairGiveUp as today. (This is the Rev-3.1 fix: repair re-EXTRACTS, it
      // does NOT re-route.) A genuinely-missing detail is caught UPSTREAM: route()'s certainty rule
      // keeps the task out of execute and asks for it, so this loop only fixes fixable mis-parses.
      let info = null;
      let infoFor = null;
      if (primaryEntry?.inputs != null) {
        let problems = null; // null first pass; describeProblems(...) on a repair re-extraction
        for (;;) {
          info = await extract(ctx, {
            labeledTranscript,
            primary,
            spec: primaryEntry.inputs,
            stateBlock: renderStateBlock(marker.state),
            problems, // <-- the describeProblems feedback reaches extract()
          });
          if (primaryEntry.conversation === "orchestrator") {
            const g = checkPayload(primaryEntry.inputs, info);
            if (g.ok) {
              infoFor = primary;
              break;
            }
            repairs++;
            if (repairs >= MAX_REPAIRS) {
              await send(number, orch(ctx.lang, "repairGiveUp"), ctx.lang);
              await closeMarker();
              await fireCapture(ctx, { phase: "repair_giveup", taskId: primary, repairProblems: g.problems });
              return;
            }
            problems = describeProblems(g.problems); // thread the SAME feedback into the next extract()
            console.log("ORCHESTRATOR extraction repair:", g.problems.join("; "));
            continue; // re-EXTRACT (NOT re-route) — the fix
          }
          // "skill"-tier gate (today's behaviour, no repair loop): shape-valid is handed over as-is.
          const g = checkPayload(primaryEntry.inputs, info);
          infoFor = g.shapeOk ? primary : null;
          if (!g.shapeOk && info) console.log("ROUTER payload withheld:", g.problems.join("; "));
          break;
        }
      }
      // primaryEntry.inputs == null -> info stays null, infoFor null (dead after the catalog trim —
      // no remaining routable skill declares inputs:null — but kept as a harmless guard).
      if (infoFor && info) marker.state.payload = info; // remember the last successful extraction

      let skippedConverted = false;
      let result = undefined;
      for (const task of dispatchable) {
        const entry = NEW_CATALOG.find((c) => c.id === task);
        // Flag (a): a NON-PRIMARY converted skill cannot run in a batch (no extractor, and the one
        // dispatch/message is spent by the primary). Skip it and tell the owner below; do NOT stash
        // a read-back note — it would never fire when the primary is unconverted (B2).
        if (task !== primary && entry?.conversation === "orchestrator") {
          skippedConverted = true;
          continue;
        }
        const run = NEW_SKILLS[task];
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
      marker.state.didWork = true; // a task ran this conversation — arms the sign-off gate

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
      setLastOutcome(marker.state, serialized); // attach the outcome to this turn's execute log entry
      pendingReadback = { result: serialized, said: ctx._turn.said };
      // loop back — the next iteration is a read-back turn
    }
  } catch (e) {
    console.error("Webhook error:", e);
  }
});

// ============================================================================
//  THE LEGACY (@assistant) FLOW  —  FROZEN pre-retirement dispatch, verbatim.
//  This is the code the webhook ran before the @assistant flow was retired: continuation-bypass,
//  then a single router call, then dispatch to each chosen skill. It runs on the FROZEN legacy
//  modules — routeLegacy (legacy/router.js), checkPayloadLegacy (legacy/inputs.js),
//  LEGACY_SKILLS/LEGACY_CATALOG (legacy assistant_settings swapped in) — so @assistant is
//  byte-for-byte the frozen behaviour, and no code the @mary flow can reach is on this path.
//  ctx is already built (with the LEGACY flow's tags/catalog/settings/sessions); this only
//  dispatches. Its ctx.sessions is legacySessions, so any session a legacy skill opens is stamped
//  flow:"legacy" and its untagged continuation routes back here (useNewFlowFor).
// ============================================================================
async function runLegacyFlow(ctx, { session, isContinuation, number }) {
  const { remoteJid } = ctx;

  // CONTINUATION: a follow-up owned by the skill that opened the session. Bypass the router and
  // hand it straight to that skill (it reads ctx.session), exactly as at retirement.
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
    // shape-invalid is withheld and the skill re-extracts for itself (frozen behaviour). Scoped to
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
    // sees null and extracts for itself — which is exactly what it does at retirement.
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
