#!/usr/bin/env node
// ============================================================================
//  feature-request auto-file fixture — does a CAPABILITY GAP get FILED, or does
//  Mary ask first?
//
//  The card: when the owner asks Mary for something no skill can do, she must write it up
//  as a feature request in the SAME turn and just announce it — no "should I file this?"
//  proposal, no keepListening-wait. That behaviour is PROMPT-DRIVEN (the feature_request
//  skill's own rulebook + description), and prompts regress silently. The failure this
//  guards against is Mary keeping the conversation open with a "want me to write this up?" say
//  (or routing to nothing) instead of dispatching feature_request in the same turn.
//
//  A deterministic assertion is impossible — this is model judgement — so the test DRIVES
//  the real flow: it builds the REAL catalog from "3. Mary Skills/" and calls the REAL
//  route() (the same construction as scripts/router-selftest.mjs). It costs a few cents.
//
//  For each capability-gap order (no matching skill), it asserts on route()'s return
//  (the three-decision envelope):  execute[0] === "feature_request"
//
//  Run:  ANTHROPIC_API_KEY=sk-ant-… node scripts/feature-request-autofile-selftest.mjs
// ============================================================================
import Anthropic from "@anthropic-ai/sdk";
import { readdir } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import path from "node:path";

import { route } from "../secretary/1. Orchestrator/router/router.js";

if (!process.env.ANTHROPIC_API_KEY) {
  console.error("ANTHROPIC_API_KEY is not set — this fixture calls the live router.");
  process.exit(2);
}

const SKILLS_DIR = path.resolve("secretary/3. Mary Skills");
const catalog = [];
for (const e of await readdir(SKILLS_DIR, { withFileTypes: true })) {
  if (!e.isDirectory()) continue;
  const mod = await import(
    pathToFileURL(path.join(SKILLS_DIR, e.name, "skill.js")).href
  );
  if (mod.manifest?.id) {
    // `inputs` too — the router prompt carries each skill's DECLARED INPUTS and its
    // extraction rulebook, and it is exactly the feature_request rulebook this test exercises.
    catalog.push({
      id: mod.manifest.id,
      description: mod.manifest.description || "",
      inputs: mod.manifest.inputs || null,
    });
  }
}
console.log(`catalog: ${catalog.map((c) => c.id).join(", ")}\n`);

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";

// A neutral opening context — the owner has just addressed Mary. No prior options are on the
// table, so the only thing that can turn a capability-gap order into `execute feature_request`
// is the carved-out rulebook/description, not context.
const TRANSCRIPT = `Marcelo: @mary good morning
[Marcelo's AI Secretary]: Morning, Marcelo — what can I do for you?
`;

// Capability-gap orders: things NO skill in the catalog can do (no restaurant-booking,
// ride-hailing or translation skill exists). The owner asked for a real capability → Mary
// must FILE a feature request in the same turn, not ask whether to.
const CASES = [
  { order: "can you book me a table at a restaurant tonight?" },
  { order: "order me an Uber to the airport" },
  { order: "translate this document to German" },
];

let failures = 0;
for (const c of CASES) {
  const ctx = {
    owner: process.env.OWNER_NAME || "Marcelo",
    anthropic,
    model: MODEL,
    order: c.order,
    transcript: TRANSCRIPT,
    hasQuotedAudio: false,
    quoted: null,
    catalog,
  };
  let res = {};
  try {
    res = await route(ctx);
  } catch (e) {
    console.error(`  ERROR  "${c.order}" -> ${e?.message || e}`);
    failures++;
    continue;
  }
  const { execute = [], say } = res;
  const ok = execute[0] === "feature_request";
  if (!ok) failures++;
  console.log(
    `${ok ? "  ok  " : "  FAIL"}  "${c.order}"\n          -> execute=${JSON.stringify(execute)}` +
      (ok ? "" : `\n          expected execute[0]="feature_request"` +
        (say ? `\n          say=${JSON.stringify(say)}` : ""))
  );
}

console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures}/${CASES.length})`}\n`);
process.exit(failures === 0 ? 0 : 1);
