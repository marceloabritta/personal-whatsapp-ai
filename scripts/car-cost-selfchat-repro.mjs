#!/usr/bin/env node
// ============================================================================
//  REPLICATION harness — card b133fd86 "New Mary flow failed at answering a question"
//
//  Symptom (human's words): "I have just asked Mary how much a car would cost in
//  Brazil. She answered nothing. It was a conversation with myself."
//
//  This drives the REAL orchestrator decision path with the LIVE model, faithfully
//  reproducing what server.js's webhook does for a SELF-CONVERSATION (owner messages
//  their own number, so every message is fromMe=true, contact === yourself):
//
//    1. gate:   matchedTagNew(text) on a fromMe message (must open the turn loop)
//    2. build:  order (tag sliced), labeledTranscript (buildLabeledTranscript), contact
//    3. route(ctx, turn) LIVE  -> {next, say, ...}    (the classification turn)
//    4. if next==="answer": answer(ctx, turn) LIVE    (the tool-carrying prose pass)
//    5. MAP route()/answer() output through server.js's OWN branch logic to decide:
//         does the owner SEE a message, or SILENCE?
//
//  It is NOT a fix and changes NO product code. It only observes the real modules.
//  Runs the same self-conversation ask N times to measure reproducibility, and also
//  runs a normal (non-self) contact chat as a control.
//
//  Run (needs a real key — same one production uses):
//    ANTHROPIC_API_KEY=$PROJECT_ANTHROPIC_API_KEY node scripts/car-cost-selfchat-repro.mjs
// ============================================================================
import Anthropic from "@anthropic-ai/sdk";
import { readdir } from "node:fs/promises";
import { pathToFileURL, fileURLToPath } from "node:url";
import path from "node:path";

import { withThinkingDefault } from "../secretary/1. Orchestrator/lib/llm.js";
import { route, answer } from "../secretary/1. Orchestrator/router/router.js";
import {
  combine,
  buildLabeledTranscript,
  buildTranscript,
  contactName,
  remember,
} from "../secretary/1. Orchestrator/lib/whatsapp.js";
import { matchedTagNew, NEW_TAGS } from "../secretary/1. Orchestrator/lib/identity.js";

const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";
const OWNER_NAME = process.env.OWNER_NAME || "Marcelo";
const NATIVE_MAX_ANSWERS = Number(process.env.NATIVE_MAX_ANSWERS) || 6;

if (!process.env.ANTHROPIC_API_KEY) {
  console.error("No ANTHROPIC_API_KEY set — pass ANTHROPIC_API_KEY=$PROJECT_ANTHROPIC_API_KEY");
  process.exit(2);
}
const anthropic = withThinkingDefault(new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY }));

// ---- load the REAL skills catalog (replicates server.js loadSkills) ----------
const SKILLS_DIR = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "secretary",
  "3. Mary Skills"
);
async function loadCatalog(dir) {
  const catalog = [];
  const entries = await readdir(dir, { withFileTypes: true });
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    const file = path.join(dir, e.name, "skill.js");
    try {
      const mod = await import(pathToFileURL(file).href);
      const id = mod.manifest?.id;
      if (!id || typeof mod.run !== "function") continue;
      catalog.push({
        id,
        description: mod.manifest.description || "",
        inputs: mod.manifest.inputs || null,
        conversation:
          mod.manifest.conversation === "orchestrator" ? "orchestrator" : "skill",
      });
    } catch (err) {
      console.error(`skill "${e.name}" failed to load:`, err.message);
    }
  }
  return catalog;
}
const CATALOG = await loadCatalog(SKILLS_DIR);
console.log("catalog:", CATALOG.map((c) => c.id).join(", ") || "(none!)");

const nowStr = "Wednesday, 07/31/2026, 10:00 AM";

// ---- build a faithful ctx exactly as server.js does --------------------------
//  `msgs` is the in-chat message list [{ t, fromMe, text, pushName }]. The LAST one
//  is the incoming @mary ask. We run the same gate + slice + transcript build.
let JID_SEQ = 0;
function buildCtx(msgs, { nativeTools, isContinuation = false }) {
  const jid = `5511999999999_${JID_SEQ++}@s.whatsapp.net`; // FRESH per scenario — no buffer bleed
  for (const m of msgs) remember(jid, m); // populate the in-memory buffer like server.js
  const conv = combine(jid, []); // no Evolution history — buffer only
  const last = msgs[msgs.length - 1];
  const { fromMe, text } = last;

  // A continuation is NEVER tagged (server.js gate). A fresh order carries the tag.
  const tag = !isContinuation && fromMe ? matchedTagNew(text) : null;
  const order = tag ? text.slice(tag.length).trim() : text.trim();

  const ctx = {
    owner: OWNER_NAME,
    tag: tag || NEW_TAGS[0],
    tags: NEW_TAGS,
    anthropic,
    model: MODEL,
    order,
    transcript: buildTranscript(conv),
    nowStr,
    contact: contactName(conv), // undefined on a self-chat -> prompt renders "(yourself)"
    remoteJid: jid,
    number: jid.split("@")[0],
    fromMe,
    isTagged: !!tag,
    quoted: null,
    hasQuotedAudio: false,
    catalog: CATALOG,
    env: {
      ...process.env,
      NATIVE_TOOLS: nativeTools ? "on" : "",
    },
    lang: "en",
    media: null,
  };
  const turn = { labeledTranscript: buildLabeledTranscript(conv) };
  return { ctx, turn, tag, order };
}

// ---- server.js branch logic: given route()/answer() output, what does the owner SEE? ----
async function driveTurn(ctx, turn) {
  const reply = await route(ctx, turn);
  console.log("  TURN ->", JSON.stringify({ next: reply.next, skills: reply.skills, hasSay: !!reply.say, say: reply.say }));

  if (reply.next === "listen") {
    return reply.say
      ? { seen: "MESSAGE", how: "say (listen)", body: reply.say }
      : { seen: "SILENCE", how: "listen + say:null (deliberate silence)" };
  }
  if (reply.next === "answer") {
    if (0 >= NATIVE_MAX_ANSWERS) return { seen: "MESSAGE", how: "costCapHit" };
    const a = await answer(ctx, turn);
    console.log("  ANSWER ->", JSON.stringify({ outcome: a.outcome, hops: a.hops, hasText: !!a.text }));
    if (a.outcome === "ok" && a.text) return { seen: "MESSAGE", how: "answer ok", body: a.text };
    if (a.outcome === "refusal") return { seen: "SILENCE", how: "answer refusal (silent close)" };
    return { seen: "MESSAGE", how: a.outcome === "timeout" ? "toolTimeout notice" : "toolError notice" };
  }
  if (reply.next === "done") {
    if (reply.degraded) return { seen: "MESSAGE", how: 'done+degraded -> "didn\'t understand" menu' };
    return reply.say
      ? { seen: "MESSAGE", how: "say (done)", body: reply.say }
      : { seen: "SILENCE", how: "done + say:null (silent close)" };
  }
  if (reply.next === "execute") {
    return { seen: "MESSAGE", how: "execute -> skill " + JSON.stringify(reply.skills) + " (not the answer path)" };
  }
  return { seen: "?", how: "unknown next=" + reply.next };
}

// ---------------------------------------------------------------------------
const ORDER_TEXT = "@mary how much would a car cost in Brazil?";

async function scenario(label, msgs, opts) {
  console.log("\n=== " + label + " ===");
  const { ctx, turn, tag, order } = buildCtx(msgs, opts);
  console.log("  gate tag:", tag, "| order:", JSON.stringify(order), "| contact:", ctx.contact ?? "(yourself)");
  console.log("  labeledTranscript:\n    " + turn.labeledTranscript.split("\n").join("\n    "));
  // server.js GATE: a fromMe message that is neither tagged nor a continuation is dropped
  // BEFORE the turn loop (`if (!isTagged && !isContinuation) return;`) -> the owner sees nothing.
  if (!tag && !opts.isContinuation) {
    const r = { seen: "SILENCE", how: "GATE drop (no @mary tag, no open session) — turn loop never runs" };
    console.log("  ==> OWNER SEES:", r.seen, "—", r.how);
    return r;
  }
  const r = await driveTurn(ctx, turn);
  console.log("  ==> OWNER SEES:", r.seen, "—", r.how);
  if (r.body) console.log("      body:", JSON.stringify(r.body).slice(0, 300));
  return r;
}

// An earlier self-chat where Mary ALREADY answered the SAME question, then the owner
// asks again (fresh @mary). Header-framed line -> labeled SECRETARY. Tests whether the
// model decides "already answered, stay silent".
const PRIOR_MARY = `*[${OWNER_NAME}'s AI Assistant]:*\n\n_A basic new car in Brazil runs about R$70k–90k._`;

const TRIALS = Number(process.env.TRIALS) || 4;
const tally = { self: [], selfNoTools: [], untagged: [], priorAnswer: [], contact: [] };

async function runN(label, key, msgs, opts) {
  for (let i = 0; i < TRIALS; i++) {
    const stamped = msgs.map((m, j) => ({ ...m, t: (tally[key].length + 1) * 100000 + i * 100 + j }));
    const r = await scenario(`${label} #${i + 1}`, stamped, opts);
    tally[key].push(r.seen);
  }
}

// A) SELF-CONVERSATION, fresh single tagged ask, native tools ON — the reported case, clean.
await runN("A. SELF-CHAT fresh tagged ask, tools ON", "self",
  [{ fromMe: true, text: ORDER_TEXT, pushName: undefined }], { nativeTools: true });

// B) SELF-CONVERSATION, native tools OFF (droplet may have NATIVE_TOOLS unset).
await runN("B. SELF-CHAT fresh tagged ask, tools OFF", "selfNoTools",
  [{ fromMe: true, text: ORDER_TEXT, pushName: undefined }], { nativeTools: false });

// C) SELF-CHAT, ask WITHOUT the @mary tag (gate never opens -> total silence by design).
await runN("C. SELF-CHAT UNTAGGED ask (no @mary)", "untagged",
  [{ fromMe: true, text: "how much would a car cost in Brazil?", pushName: undefined }],
  { nativeTools: true });

// D) SELF-CHAT continuation: Mary already answered this in-thread, owner asks the SAME thing again.
await runN("D. SELF-CHAT, Mary already answered, re-ask", "priorAnswer",
  [
    { fromMe: true, text: ORDER_TEXT, pushName: undefined },
    { fromMe: true, text: PRIOR_MARY, pushName: undefined }, // SECRETARY line
    { fromMe: true, text: ORDER_TEXT, pushName: undefined }, // fresh re-ask
  ],
  { nativeTools: true });

// E) CONTROL: same ask in a NORMAL chat with a real contact.
await runN("E. CONTROL normal chat with a contact, tools ON", "contact",
  [
    { fromMe: false, text: "hey", pushName: "Alex" },
    { fromMe: true, text: ORDER_TEXT, pushName: undefined },
  ],
  { nativeTools: true });

// For the untagged case, the gate itself decides — replicate it (no route() call needed):
// server.js returns early when !isTagged && !isContinuation, so the owner sees SILENCE.

const sum = (a) => a.reduce((o, s) => ((o[s] = (o[s] || 0) + 1), o), {});
console.log("\n================ TALLY (", TRIALS, "trials each ) ================");
console.log("A. self-chat, tagged, tools ON :", JSON.stringify(sum(tally.self)));
console.log("B. self-chat, tagged, tools OFF:", JSON.stringify(sum(tally.selfNoTools)));
console.log("C. self-chat, UNTAGGED         :", JSON.stringify(sum(tally.untagged)), "  <- see note: gate below")
console.log("D. self-chat, Mary re-asked    :", JSON.stringify(sum(tally.priorAnswer)));
console.log("E. contact chat (control)      :", JSON.stringify(sum(tally.contact)));
const anySilence = [...tally.self, ...tally.selfNoTools, ...tally.priorAnswer, ...tally.contact].includes("SILENCE");
console.log("\nSILENCE in the TAGGED answer path anywhere:", anySilence ? "YES — symptom reproduced" : "no");
