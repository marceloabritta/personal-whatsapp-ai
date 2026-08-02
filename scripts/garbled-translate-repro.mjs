#!/usr/bin/env node
// ============================================================================
//  REPLICATION repro (card bea6dea5 — "Mary produced a garbled, off-topic reply,
//  hallucinated a translation task").
//
//  SYMPTOM to reproduce: the owner asked "vc consegue preencher esse formulário?"
//  and Mary replied with an incoherent ENGLISH message framing the turn as a
//  "translate Portuguese to Portuguese" task, quoting back her OWN prior turn
//  ("Registrei como feature request: preencher formulários web...") as the
//  "text to translate", never addressing the form question.
//
//  This script does NOT fix or diagnose. It drives the exact code path the
//  garbled text is a product of — the orchestrator's forced say-translation
//  fallback — and shows the live TRANSLATE_MODEL emitting the garbled meta-reply.
//
//  THE PATH (real code, verbatim):
//    server.js sendSay()  ->  shouldForceTranslateSay(sayLang, ctx.lang)  ->
//      localizeBody(say, ctx.lang, {force:true})  ->  anthropic.messages.create(
//        { model: TRANSLATE_MODEL, system: "<translate into ${l}>", messages:[say] })
//
//  The gate (shouldForceTranslateSay) is the REAL rails module, imported here.
//  localizeBody is a module-local fn in server.js (which self-starts an Express
//  listener and cannot be imported), so its translate call is reconstructed BYTE
//  FOR BYTE from server.js:170-190 (system prompt) and server.js:73-74 / :96
//  (model + thinking-disabled client wrapper).
//
//  Run:  ANTHROPIC_API_KEY=$PROJECT_ANTHROPIC_API_KEY node scripts/garbled-translate-repro.mjs
// ============================================================================
import Anthropic from "@anthropic-ai/sdk";
import { shouldForceTranslateSay } from "../secretary/1. Orchestrator/lib/lang.js";
import { withThinkingDefault } from "../secretary/1. Orchestrator/lib/llm.js";

// --- server.js:73-74 verbatim ------------------------------------------------
const TRANSLATE_MODEL =
  process.env.TRANSLATE_MODEL || "claude-haiku-4-5-20251001";

// --- server.js:96 verbatim (thinking disabled by default) --------------------
const anthropic = withThinkingDefault(
  new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY })
);

// --- server.js:170-190 localizeBody(), reconstructed verbatim ----------------
async function localizeBody(text, lang, { force = false } = {}) {
  const l = (lang || "en").toLowerCase();
  const MAINTAINED = new Set(["en", "pt"]);
  if (!text || (!force && (MAINTAINED.has(l) || l === "en"))) return text;
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
}

// ---------------------------------------------------------------------------
//  THE REPLAYED TURN.
//  - The router's `say` for the offending turn is Portuguese prose. The router
//    is KNOWN to mislabel Portuguese say as lang:"en" — see the captured log at
//    08:25:16Z: {"say":"Registrei como feature request...","lang":"en"}. We use
//    that exact prior say as the say text and lang:"en" (its own mislabel).
//  - ctx.lang (the pinned/target language) is "pt".
//  So the gate sees sayLang="en", targetLang="pt".
// ---------------------------------------------------------------------------
const SAY =
  "Registrei como feature request: preencher formulários web. Mais alguma coisa?";
const SAY_LANG = "en"; // the router's (mis)label, verbatim from the 08:25:16Z log
const TARGET_LANG = "pt"; // ctx.lang — the pinned conversation language

console.log("=== card bea6dea5 — garbled translation-loop reply repro ===\n");

// STEP 1 — does the REAL gate open on this turn?
const gateOpens = shouldForceTranslateSay(SAY_LANG, TARGET_LANG);
console.log(
  `shouldForceTranslateSay("${SAY_LANG}", "${TARGET_LANG}") = ${gateOpens}` +
    `  (real lib/lang.js)\n`
);
if (!gateOpens) {
  console.log("Gate did not open — path not entered. Nothing to reproduce.");
  process.exit(2);
}
console.log(
  "Gate OPEN -> orchestrator calls localizeBody(say, 'pt', {force:true}),\n" +
    "i.e. asks the TRANSLATE_MODEL to translate ALREADY-PORTUGUESE text INTO Portuguese.\n"
);
console.log(`TRANSLATE_MODEL = ${TRANSLATE_MODEL}`);
console.log(`INPUT say (verbatim): ${JSON.stringify(SAY)}\n`);

// STEP 2 — drive the live translate call N times (LLM output is non-deterministic).
const N = 5;
let garbled = 0;
// A reply is "garbled / off-topic" (the SYMPTOM) if it does NOT just return the
// prose, but instead talks ABOUT translating / language, or answers in English.
const looksGarbled = (out) =>
  /\btranslat|already in|misunderstand|clarify|which language|could you\b/i.test(
    out
  );

for (let i = 1; i <= N; i++) {
  let out;
  try {
    out = await localizeBody(SAY, TARGET_LANG, { force: true });
  } catch (e) {
    console.log(`run ${i}: ERROR ${e?.message || e}`);
    continue;
  }
  const bad = looksGarbled(out);
  if (bad) garbled++;
  console.log(`----- run ${i}/${N}  ${bad ? "[GARBLED]" : "[clean]"} -----`);
  console.log(out);
  console.log();
}

console.log("===========================================================");
console.log(
  `RESULT: ${garbled}/${N} runs produced a garbled / off-topic translation-loop reply.`
);
console.log(
  garbled > 0
    ? "REPRODUCED: the forced say-translation path emits the observed symptom."
    : "NOT reproduced on this run (all clean)."
);
process.exit(0);
