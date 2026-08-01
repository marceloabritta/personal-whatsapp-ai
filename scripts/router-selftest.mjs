#!/usr/bin/env node
// ============================================================================
//  Router regression fixture — does a COMPLAINT get filed, or executed?
//
//  Every mitigation protecting the `feedback` skill from misrouting is a PROMPT, and
//  prompts regress silently. The dangerous case isn't an error — it's "you scheduled that
//  at the wrong time" being routed to calendar_action and cheerfully SCHEDULING SOMETHING,
//  which is a second mistake stacked on the first one the owner was complaining about.
//
//  This calls the REAL router against the REAL catalog (it costs a few cents), so run it
//  after any edit to router/prompt.js or to a skill manifest.
//
//  Run:  ANTHROPIC_API_KEY=sk-ant-… node scripts/router-selftest.mjs
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

// Build the catalog EXACTLY as production's loadSkills() does — including SKIPPING a
// `routable:false` manifest (transcribe_audio, flight_search are dormant after card 327be40b).
// A catalog that still carried them would test a prompt production never sends.
const SKILLS_DIR = path.resolve("secretary/3. Mary Skills");
const catalog = [];
for (const e of await readdir(SKILLS_DIR, { withFileTypes: true })) {
  if (!e.isDirectory()) continue;
  const mod = await import(
    pathToFileURL(path.join(SKILLS_DIR, e.name, "skill.js")).href
  );
  if (mod.manifest?.id && mod.manifest.routable !== false) {
    // `inputs` + `conversation` too — the router prompt carries each skill's DECLARED INPUTS,
    // its extraction rulebook, and its conversation mode. This mirrors loadSkills() so the prompt
    // this test builds is byte-identical to production's.
    catalog.push({
      id: mod.manifest.id,
      description: mod.manifest.description || "",
      inputs: mod.manifest.inputs || null,
      conversation: mod.manifest.conversation === "orchestrator" ? "orchestrator" : "skill",
    });
  }
}
console.log(`catalog: ${catalog.map((c) => c.id).join(", ")}\n`);

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";

const TRANSCRIPT = `Marcelo: @secretary schedule a call with Ana tomorrow at 5pm
[Marcelo's AI Secretary]: Event created — "Call with Ana", tomorrow 6:00 PM.
`;

// A FLIGHT context. "link for option 2" against the calendar transcript above would prove
// the wrong thing — the case only means anything if the options really are on the table.
const FLIGHT_TRANSCRIPT = `Marcelo: @secretary find me a flight from São Paulo to Lisbon on the 14th
[Marcelo's AI Secretary]: 1. 4980 BRL — out Fri, Aug 14, 22:10 GRU → 12:35 LIS (direct, 11h25)
2. 5210 BRL — out Fri, Aug 14, 18:40 GRU → 14:55 LIS (1 stop via MAD)
3. 5610 BRL — out Fri, Aug 14, 21:20 VCP → 11:00 LIS (direct, 9h40)
Want the link for one? Say "link for option 2".
`;

// Assertions are on the three-decision envelope's `execute` list (route() no longer returns
// `tasks`). exact: execute must match, in order. contains: the tasks must be present (feedback
// first). empty: execute must be [] — the model answered NATIVELY (no dispatched skill), which is
// now how a flight question / a general lookup is handled (flight_search is dormant).
// Per-case `transcript` overrides the shared calendar one (default below).
const CASES = [
  { order: "you made a mistake here", exact: ["feedback"] },
  { order: "that's wrong", exact: ["feedback"] },
  { order: "você errou nessa", exact: ["feedback"] },
  // THE misroute hazard: the subject is a calendar event, but the intent is a bug report.
  { order: "you scheduled that at the wrong time", exact: ["feedback"] },
  { order: "you got the timezone wrong on that event", exact: ["feedback"] },
  // Both: file the defect AND do the fix. feedback must come first.
  { order: "you got the time wrong, move it to 5pm", contains: ["feedback", "calendar_action"] },
  // Unchanged behaviour — a task must not steal ordinary orders.
  { order: "schedule lunch with Ana tomorrow at noon", exact: ["calendar_action"] },
  { order: "I have a feature idea: let me snooze a task", exact: ["feature_request"] },
  { order: "add buy milk to my tasks", exact: ["task_action"] },
  // FLIGHT is now answered NATIVELY (flight_search dormant) — the model runs a web search and
  // replies in prose, dispatching NO skill. So execute must be EMPTY, never a flight task.
  { order: "find me a flight from São Paulo to Lisbon on the 14th", empty: true },
  { order: "me acha um voo de Sao Paulo pra Lisboa dia 14", empty: true },
  { order: "link for option 2", transcript: FLIGHT_TRANSCRIPT, empty: true },
  // Regression: a to-do that happens to mention a flight is still a task, not a native answer.
  { order: "add buy flight tickets to my tasks", exact: ["task_action"] },
];

let failures = 0;
for (const c of CASES) {
  const ctx = {
    owner: process.env.OWNER_NAME || "Marcelo",
    anthropic,
    model: MODEL,
    order: c.order,
    transcript: c.transcript || TRANSCRIPT,
    hasQuotedAudio: false,
    quoted: null,
    catalog,
    tags: ["@mary"],
    env: process.env,
  };
  let execute = [];
  try {
    ({ execute = [] } = await route(ctx));
  } catch (e) {
    console.error(`  ERROR  "${c.order}" -> ${e?.message || e}`);
    failures++;
    continue;
  }
  const ok = c.empty
    ? Array.isArray(execute) && execute.length === 0
    : c.exact
    ? JSON.stringify(execute) === JSON.stringify(c.exact)
    : c.contains.every((t) => execute.includes(t)) &&
      execute.indexOf("feedback") === 0; // feedback first — file before you fix
  if (!ok) failures++;
  console.log(
    `${ok ? "  ok  " : "  FAIL"}  "${c.order}"\n          -> ${JSON.stringify(execute)}` +
      (ok ? "" : `   expected ${c.empty ? "[] (native answer)" : JSON.stringify(c.exact || c.contains)}`)
  );
}

console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures}/${CASES.length})`}\n`);
process.exit(failures === 0 ? 0 : 1);
