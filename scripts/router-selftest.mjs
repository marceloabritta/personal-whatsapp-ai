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

const SKILLS_DIR = path.resolve("secretary/2. Skills");
const catalog = [];
for (const e of await readdir(SKILLS_DIR, { withFileTypes: true })) {
  if (!e.isDirectory()) continue;
  const mod = await import(
    pathToFileURL(path.join(SKILLS_DIR, e.name, "skill.js")).href
  );
  if (mod.manifest?.id) {
    catalog.push({ id: mod.manifest.id, description: mod.manifest.description || "" });
  }
}
console.log(`catalog: ${catalog.map((c) => c.id).join(", ")}\n`);

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";

const TRANSCRIPT = `Marcelo: @secretary schedule a call with Ana tomorrow at 5pm
[Marcelo's AI Secretary]: Event created — "Call with Ana", tomorrow 6:00 PM.
`;

// exact: the tasks must match, in order. contains: the task must be present.
const CASES = [
  { order: "you made a mistake here", exact: ["feedback"] },
  { order: "that's wrong", exact: ["feedback"] },
  { order: "você errou nessa", exact: ["feedback"] },
  // THE misroute hazard: the subject is a calendar event, but the intent is a bug report.
  { order: "you scheduled that at the wrong time", exact: ["feedback"] },
  { order: "you got the timezone wrong on that event", exact: ["feedback"] },
  // Both: file the defect AND do the fix. feedback must come first.
  { order: "you got the time wrong, move it to 5pm", contains: ["feedback", "calendar_action"] },
  // Unchanged behaviour — the new skill must not steal ordinary orders.
  { order: "schedule lunch with Ana tomorrow at noon", exact: ["calendar_action"] },
  { order: "I have a feature idea: let me snooze a task", exact: ["feature_request"] },
  { order: "add buy milk to my tasks", exact: ["task_action"] },
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
  let tasks = [];
  try {
    ({ tasks } = await route(ctx));
  } catch (e) {
    console.error(`  ERROR  "${c.order}" -> ${e?.message || e}`);
    failures++;
    continue;
  }
  const ok = c.exact
    ? JSON.stringify(tasks) === JSON.stringify(c.exact)
    : c.contains.every((t) => tasks.includes(t)) &&
      tasks.indexOf("feedback") === 0; // feedback first — file before you fix
  if (!ok) failures++;
  console.log(
    `${ok ? "  ok  " : "  FAIL"}  "${c.order}"\n          -> ${JSON.stringify(tasks)}` +
      (ok ? "" : `   expected ${JSON.stringify(c.exact || c.contains)}`)
  );
}

console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures}/${CASES.length})`}\n`);
process.exit(failures === 0 ? 0 : 1);
