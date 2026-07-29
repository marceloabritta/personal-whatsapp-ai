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
//  A SINGLE ALWAYS-@mary FLOW. A message summoned by the @mary tag (SECRETARY_TAG_NEW) runs the
//  orchestrator turn loop: the model drives a three-state cycle (listen / execute / done), the
//  orchestrator holds a marker between messages, and `execute` is non-terminal — a converted
//  skill's return value drives a read-back turn. The turn loop is inline in the webhook handler.
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
  NEW_TAGS,
  setNewTags,
  headerFor,
  isOwnMessage,
  matchedTagNew,
} from "./lib/identity.js";
import { frame } from "./lib/format.js";
import { route, answer } from "./router/router.js";
import { installLogBuffer } from "./lib/logbuffer.js";
import { captureFailure } from "./lib/selflearning.js";
import { MAINTAINED_LANGS, resolveTurnLang, shouldForceTranslateSay } from "./lib/lang.js";

// SELF-LEARNING: wrap console so the secretary can read its own recent logs back when it
// writes a failure report. Must run before anything else logs — including loadSkills()
// below. stdout is untouched, so `docker logs` still works exactly as before.
installLogBuffer();

const __dirname = path.dirname(fileURLToPath(import.meta.url));
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
// Durable settings on the SAME Redis (no TTL, own key space). Today: the tag list the owner
// summons her with, which he can change by asking (the `assistant_settings` skill). SECRETARY_TAG_NEW
// is the SEED; a stored value wins — see the boot load below. Namespaced (secretary:settings:new:tags).
const newSettings = createSettings({ url: REDIS_URL, ns: "new" });

const seen = new Set(); // dedup by messageId

// ---- Skill discovery --------------------------------------------------------
// Scans "3. Mary Skills/*/skill.js". Each skill exports:
//   export const manifest = { id, description }
//   export async function run(ctx) { ... }
// -> SKILLS: { [id]: run }  |  CATALOG: [{ id, description }] (the router's menu)
async function loadSkills(dir = NEW_SKILLS_DIR) {
  const skills = {};
  const catalog = [];
  let entries = [];
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch (e) {
    console.error("Could not read the skills folder:", dir, e.message);
    return { skills, catalog };
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
async function localizeBody(text, lang, { force = false } = {}) {
  const l = (lang || "en").toLowerCase();
  if (!text || (!force && (MAINTAINED_LANGS.has(l) || l === "en"))) return text;
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
  // Answer pass (native server-side tools) notices — the orchestrator's OWN, like turnCap/
  // dispatchCap. Informational (not a skill's *Failed/*Error), so sent via bare send().
  toolTimeout: {
    en: () => "That took too long to look up — try asking me again.",
    pt: () => "Isso demorou demais para pesquisar — pode me perguntar de novo?",
  },
  toolError: {
    en: () => "I couldn't look that up right now — try again in a bit.",
    pt: () => "Não consegui pesquisar isso agora — tente de novo daqui a pouco.",
  },
  costCapHit: {
    en: () => "I've answered a few of those in a row — let's pause. Ask me again fresh.",
    pt: () => "Já respondi algumas dessas seguidas — vamos pausar. Me pergunte de novo do zero.",
  },
};

// Pick an orchestrator string for `lang`, falling back to the English copy (which
// the send() fallback then translates for a non-en/pt language).
function orch(lang, key, ...args) {
  const entry = ORCH_MSG[key];
  const fn = (entry && (entry[lang] || entry.en)) || (() => "");
  return fn(...args);
}

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

// NATIVE SERVER-SIDE TOOLS (the answer pass). The bundle is built by lib/nativeTools.js off
// process.env; these are the orchestrator-side ceilings for the answer branch. Locked defaults
// (card 6c09b8ab): 30s per-pass wall clock, 4 pause_turn resumes, 6 answers per conversation.
// router.answer() reads the timeout/hop values from ctx.env itself; NATIVE_MAX_ANSWERS is the
// per-conversation cost ceiling enforced here on the marker.
const NATIVE_MAX_ANSWERS = Number(process.env.NATIVE_MAX_ANSWERS) || 6;

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

// Context bits used to BUILD ctx (tags/catalog/settings). The dispatch code is the turn loop,
// inline in the webhook.
const NEW_FLOW = { tags: NEW_TAGS, catalog: NEW_CATALOG, settings: newSettings };

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

    // START: a flow only begins when the OWNER uses a trigger tag. `tag` is the tag this message
    // actually starts with (or null) — used below to slice it off. The matcher sees an attachment
    // caption, so a captioned document (whose `text` is "") can open the gate on its caption. For a
    // captioned image, text already carries the caption, so gateText === text.
    const gateText = text || attachmentCaption;
    const tag = fromMe ? matchedTagNew(gateText) : null;
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

    // The single @mary flow owns every message the gate lets through.
    const flow = NEW_FLOW;

    // Slice off the matched tag by ITS own length (tags can differ in length). Source the order
    // from the attachment caption so a caption-borne instruction reaches the model on BOTH the
    // first (tagged) turn AND a mid-session (untagged) continuation of a captioned document —
    // whose `text` is "" (Amendment).
    const order = isTagged
      ? gateText.slice(tag.length).trim() // first message, tagged caption -> POST-TAG instruction
      : text.trim() || attachmentCaption.trim(); // mid-session continuation -> caption if text empty
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
      sessions, // store: get/set/clear per-chat state
      settings: flow.settings, // the flow's durable tag store (namespaced-new)
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
        ? await localizeBody(say, ctx.lang, { force: true })
        : say;
      return send(number, body, ctx.lang);
    };

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
        // Re-read the answer count on continuation, or NATIVE_MAX_ANSWERS silently degrades from
        // per-conversation to per-message (an undefined answers restarts the count each inbound).
        answers: session.answers || 0,
      };
    } else {
      if (session) await sessions.clear(remoteJid);
      marker = { awaitFrom: "owner", turns: 0, dispatches: 0, answers: 0 };
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
          openingLang: pinnedLang, // NEW — the immutable opening language (the pin)
          lang: ctx.lang, // keep for backward-compat with any legacy reader; harmless
          turns: marker.turns,
          dispatches: marker.dispatches,
          answers: marker.answers || 0, // per-conversation answer/cost ceiling (NATIVE_MAX_ANSWERS)
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

      ctx.lang = resolveTurnLang(pinnedLang, reply.lang);
      if (!pinnedLang) pinnedLang = ctx.lang; // first turn of a new conversation: pin it now
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
        if (reply.say) await sendSay(reply.say, reply.lang);
        marker.awaitFrom = reply.awaitFrom || marker.awaitFrom || "owner";
        await persistMarker();
        return; // wait for his next message
      }

      // ---- reply.next === "answer" -----------------------------------------------------------
      // The tool-carrying answer pass: a SECOND model call (router.answer) with the native toolset
      // attached, returning PROSE delivered inline via sendSay in the SAME WhatsApp reply. Because
      // it is downstream of a successfully-parsed classification, it can never reach the degraded
      // "didn't understand" menu.
      if (reply.next === "answer") {
        // Write-invariant sibling (server.js read-back guard): a read-back turn must not start a
        // fresh answer pass — a new action needs a new message from him first.
        if (thisTurnIsReadback) {
          await fireCapture(ctx, { phase: "readback_answer", taskId: "orchestrator" });
          await closeMarker();
          return;
        }
        // Per-conversation answer/cost ceiling on the marker.
        if (marker.answers >= NATIVE_MAX_ANSWERS) {
          await send(number, orch(ctx.lang, "costCapHit"), ctx.lang);
          await closeMarker();
          await fireCapture(ctx, { phase: "answer_cap", taskId: "orchestrator", answerCap: NATIVE_MAX_ANSWERS });
          return;
        }
        marker.answers = (marker.answers || 0) + 1;
        const a = await answer(ctx, { labeledTranscript });
        if (a.outcome === "ok" && a.text) {
          await sendSay(a.text, a.lang); // language-pinned, same as a normal say
          marker.awaitFrom = "owner";
          await persistMarker(); // conversation stays open — wait for his next message
          return;
        }
        // A model refusal stays SILENT — matching the classification refusal path (router.js:
        // a refusal degrades to a silent close). Do NOT send the tool-error notice on a refusal.
        if (a.outcome === "refusal") {
          await closeMarker();
          await fireCapture(ctx, { phase: "answer_refusal", taskId: "router" });
          return;
        }
        const key = a.outcome === "timeout" ? "toolTimeout" : "toolError";
        await send(number, orch(ctx.lang, key), ctx.lang);
        await closeMarker();
        await fireCapture(ctx, { phase: "answer_" + a.outcome, taskId: "router" });
        return;
      }

      if (reply.next === "done") {
        // A DEGRADED close (the router refused or produced an unparseable reply — router.js sets
        // reply.degraded) is the real schema-drift alarm: keep the "I didn't understand" menu AND
        // the unrouted capture. A LEGITIMATE empty close has the SAME shape (say:null, skills:[])
        // but is the model deliberately closing chit-chat / a no-op / an out-of-scope line
        // (thanks, "deixa pra la", an emoji) — NOT a malfunction. Close it silently, capture
        // nothing. (card 77cd6542)  reply.degraded implies say:null & skills:[].
        if (reply.degraded && turnIndex === 0 && !thisTurnIsReadback) {
          const names = NEW_CATALOG.map((c) => c.id).join(", ");
          await send(number, orch(ctx.lang, "notUnderstood", names), ctx.lang);
          await closeMarker();
          await fireCapture(ctx, { phase: "unrouted", taskId: "router", unroutedOrder: ctx.order });
          return;
        }
        if (reply.say) await sendSay(reply.say, reply.lang);
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
      const dispatchable = batch.filter((s) => NEW_SKILLS[s]);
      if (!dispatchable.length) {
        // The model chose `execute` but named no dispatchable skill (only "other"/unknown ids —
        // router.js forces an empty-skilled execute to ["other"]). A PARSED reply always: the
        // degrade fallback returns next:"done", never "execute", so a genuine router malfunction
        // can NEVER reach here. This is the model deciding nothing here is for it — close cleanly,
        // no menu, no unrouted capture. The schema-drift alarm still fires on the degraded `done`
        // path above. (card 77cd6542)
        await closeMarker();
        return;
      }

      const primary = dispatchable[0];
      const primaryEntry = NEW_CATALOG.find((c) => c.id === primary);
      const info = reply.info;

      // WHICH tier gates the dispatch is read off the declaration, not guessed:
      //  - "orchestrator" -> gate on `ok` (all three tiers). A failure is the REPAIR loop, NOT a
      //    dispatch: the write budget is untouched, describeProblems goes back to the model.
      //  - "skill"        -> gate on `shapeOk` (today's gate). Shape-valid is handed over,
      //    incomplete or not; shape-invalid is withheld and the skill re-extracts for itself.
      let infoFor = null;
      if (primaryEntry?.conversation === "orchestrator") {
        if (primaryEntry.inputs == null) {
          // A converted skill with NO declared inputs (e.g. transcribe_audio) has nothing to
          // validate or hand over — dispatch it directly and let it run its own check. Without
          // this, checkPayload(null,…).ok===false would trap it in the repair loop forever.
          infoFor = null;
        } else {
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
        }
      } else {
        const g = checkPayload(primaryEntry?.inputs, info);
        infoFor = g.shapeOk ? primary : null;
        if (!g.shapeOk && info) console.log("ROUTER payload withheld:", g.problems.join("; "));
      }

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

// ---- Boot: the STORED tag list wins over the SECRETARY_TAG_NEW seed ----------
// `await newSettings.ready` is the load-bearing word here. createSettings() fires its Redis
// connect without blocking (same shape as sessions.js), so live() is false for the first
// moments of the process. Reading the stored tags WITHOUT awaiting ready would race the
// connection, miss them, and fall back to the env seed — she would answer to the changed tag
// until the first restart and then silently forget it. Top-level await; the package is ESM.
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
