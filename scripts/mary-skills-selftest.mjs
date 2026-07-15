#!/usr/bin/env node
// ============================================================================
//  Self-test for the "New Architecture — convert all skills" card.
//
//  The app can't be exercised end-to-end on the Mac (it needs the Evolution webhook +
//  Redis), and the router's JUDGEMENT can only be checked with a live, real-money call
//  (scripts/router-selftest.mjs — the human's gate). So this drives the DETERMINISTIC
//  discovery + declared-inputs layer directly — the same thing server.js loadSkills() and
//  lib/inputs.js checkPayload() do — over BOTH skill trees, with no server boot, no network,
//  no model. It asserts the contract of @mary's converted stack:
//
//    T1  both trees discover cleanly — every skill.js under "2. Skills/" AND "3. Mary Skills/"
//        imports and exports manifest.id + a run() function.
//    T2  same seven ids in both trees, and per-flow maps DISJOINT: newTree[id].run is a
//        DIFFERENT function object than oldTree[id].run — proves the new stack is an isolated
//        copy, not a shared module (the parallel A/B run must never let one flow reach into
//        the other).
//    T3  every new manifest is a PURE TASK: conversation === "orchestrator" (the model runs
//        the dialogue; the skill only acts + returns).
//    T4  the declared read-then-act inputs validate a READ payload AND an ACT payload through
//        checkPayload, and REJECT an incomplete ACT — for calendar_action / task_action /
//        flight_search. This is the completeness gate server.js:701 runs before dispatching an
//        orchestrator primary; a botched discriminator (a READ that can't validate, or an
//        incomplete ACT that slips through) fails here.
//    T5  transcribe_audio's inputs:null dispatch precondition: manifest is
//        conversation:"orchestrator" AND inputs == null, and checkPayload(null,{}).ok === false
//        — documenting WHY rails change (b) exists (the plain checkPayload gate would trap an
//        inputs:null orchestrator skill in the repair loop forever). The dispatch BRANCH itself
//        lives in server.js and isn't offline-unit-testable; this pins the deterministic
//        contract around it (CONVENTIONS §5).
//
//  Each assertion FAILS if the feature is absent: no "3. Mary Skills/" tree -> T1/T2 fail; an
//  unconverted skill left as conversation:"skill" -> T3 fails; a botched discriminator -> T4
//  fails; transcribe left declaring inputs or as conversation:"skill" -> T5 fails.
//
//  Run:  node scripts/mary-skills-selftest.mjs
// ============================================================================
import { readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { checkPayload } from "../secretary/1. Orchestrator/lib/inputs.js";

const HERE = path.dirname(fileURLToPath(import.meta.url)); // scripts/
const REPO = path.join(HERE, "..");
const OLD_DIR = path.join(REPO, "secretary", "2. Skills");
const NEW_DIR = path.join(REPO, "secretary", "3. Mary Skills");

// The seven skills the card converts. Both trees must expose exactly this set.
const EXPECTED = [
  "calendar_action",
  "transcribe_audio",
  "task_action",
  "feature_request",
  "feedback",
  "flight_search",
  "assistant_settings",
];

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// Replicates server.js loadSkills()' discovery layer for ONE tree, without booting the server:
// readdir + dynamic-import each folder's skill.js, keep manifest + run. A folder with no
// skill.js is "not a skill folder" and is skipped (as loadSkills does); a skill.js that EXISTS
// but throws, or that is missing manifest.id / run(), is a real problem and is recorded.
//   -> { missing, error, byId: { [id]: { folder, manifest, run } }, problems: [] }
async function discover(dir) {
  const byId = {};
  const problems = [];
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch (e) {
    return { missing: true, error: e.message, byId, problems };
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue;
    const file = path.join(dir, e.name, "skill.js");
    try {
      await stat(file);
    } catch {
      continue; // no skill.js -> not a skill folder
    }
    let mod;
    try {
      mod = await import(pathToFileURL(file).href);
    } catch (err) {
      problems.push(`${e.name}: skill.js failed to import — ${err.message}`);
      continue;
    }
    const id = mod.manifest?.id;
    if (!id || typeof mod.run !== "function") {
      problems.push(`${e.name}: missing manifest.id or run()`);
      continue;
    }
    if (byId[id]) problems.push(`${e.name}: duplicate id '${id}'`);
    byId[id] = { folder: e.name, manifest: mod.manifest, run: mod.run };
  }
  return { missing: false, error: null, byId, problems };
}

// A payload shaped exactly like the declared field set: every declared field present (the
// model emits all of them), nulled, then the discriminator + the fields under test overridden.
// Guarantees no "missing"/"unexpected field" noise so checkPayload measures COMPLETENESS, not
// a hand-authored field-name mismatch — mirroring how the router fills every declared field.
function payloadFrom(spec, overrides) {
  const info = {};
  for (const k of Object.keys(spec?.fields || {})) info[k] = null;
  return { ...info, ...overrides };
}

console.log("\nmary-skills self-test  (offline: discovery + declared-inputs contract)\n");

const oldTree = await discover(OLD_DIR);
const newTree = await discover(NEW_DIR);

if (newTree.missing) {
  console.log(
    `NOTE: "secretary/3. Mary Skills/" not found (${newTree.error}). The converted stack ` +
      `does not exist yet — T1–T5 that depend on it will FAIL. This is the expected state ` +
      `until the Coding column builds the new tree.\n`
  );
}

// ---- T1 — both trees discover cleanly ---------------------------------------
console.log("T1   both trees discover cleanly");
check("old tree ('2. Skills/') is readable", !oldTree.missing);
check("NEW tree ('3. Mary Skills/') exists and is readable", !newTree.missing);
check(
  `old tree has no import/export problems (${oldTree.problems.join("; ") || "none"})`,
  oldTree.problems.length === 0
);
check(
  `NEW tree has no import/export problems (${newTree.problems.join("; ") || "none"})`,
  newTree.problems.length === 0
);
check("old tree discovered at least one skill", Object.keys(oldTree.byId).length >= 1);
check("NEW tree discovered at least one skill", Object.keys(newTree.byId).length >= 1);

// ---- T2 — same seven ids, per-flow maps DISJOINT ----------------------------
console.log("\nT2   same seven ids, and the two trees are DISJOINT modules");
const oldIds = Object.keys(oldTree.byId).sort();
const newIds = Object.keys(newTree.byId).sort();
const want = [...EXPECTED].sort();
check(
  `old tree exposes exactly the seven expected ids (${oldIds.join(", ") || "none"})`,
  JSON.stringify(oldIds) === JSON.stringify(want)
);
check(
  `NEW tree exposes exactly the seven expected ids (${newIds.join(", ") || "none"})`,
  JSON.stringify(newIds) === JSON.stringify(want)
);
for (const id of EXPECTED) {
  const oldRun = oldTree.byId[id]?.run;
  const newRun = newTree.byId[id]?.run;
  check(
    `${id}: distinct run() in each tree (isolation, no shared module)`,
    typeof oldRun === "function" && typeof newRun === "function" && oldRun !== newRun
  );
}

// ---- T3 — every new manifest is a pure task ---------------------------------
console.log("\nT3   every NEW manifest is a pure task (conversation: 'orchestrator')");
for (const id of EXPECTED) {
  const m = newTree.byId[id]?.manifest;
  check(`${id}: manifest.conversation === 'orchestrator'`, m?.conversation === "orchestrator");
}

// ---- T4 — declared inputs validate READ + ACT, reject incomplete ACT --------
console.log("\nT4   read-then-act inputs validate a READ and an ACT via checkPayload");
function specFor(id) {
  return newTree.byId[id]?.manifest?.inputs ?? null;
}
function assertReadAct(id, cases) {
  const spec = specFor(id);
  if (!spec) {
    check(`${id}: NEW manifest declares inputs`, false);
    return;
  }
  for (const c of cases) {
    const res = checkPayload(spec, payloadFrom(spec, c.info));
    check(
      `${id}: ${c.label} -> ok === ${c.ok} (${res.problems.join("; ") || "no problems"})`,
      res.ok === c.ok
    );
  }
}

assertReadAct("calendar_action", [
  { label: "READ (action:find)", ok: true, info: { action: "find" } },
  {
    label: "ACT (action:create, dated, 1 attendee)",
    ok: true,
    info: {
      action: "create",
      start_iso: "2026-07-20T15:00:00-03:00",
      participants: [{ name: "A", email: "a@b.com" }],
    },
  },
  {
    label: "incomplete ACT (create, start_iso:null)",
    ok: false,
    info: {
      action: "create",
      start_iso: null,
      participants: [{ name: "A", email: "a@b.com" }],
    },
  },
]);

assertReadAct("task_action", [
  { label: "READ (mode:list)", ok: true, info: { mode: "list" } },
  {
    label: "ACT (mode:apply, one create op)",
    ok: true,
    info: {
      mode: "apply",
      ops: [{ kind: "create", task_id: null, title: "Buy milk", due_iso: null }],
    },
  },
  { label: "incomplete ACT (apply, ops:null)", ok: false, info: { mode: "apply", ops: null } },
]);

assertReadAct("flight_search", [
  {
    label: "READ (intent:search)",
    ok: true,
    info: { intent: "search", origin: "GRU", destination: "JFK", depart_date: "2026-08-01" },
  },
  { label: "ACT (intent:link, option 2)", ok: true, info: { intent: "link", option_number: 2 } },
  {
    label: "incomplete ACT (search, origin:null)",
    ok: false,
    info: { intent: "search", origin: null, destination: "JFK", depart_date: "2026-08-01" },
  },
]);

// ---- T5 — the inputs:null dispatch precondition -----------------------------
console.log("\nT5   transcribe_audio's inputs:null dispatch precondition");
const tr = newTree.byId["transcribe_audio"]?.manifest;
check("transcribe_audio present in the NEW tree", !!tr);
check("transcribe_audio conversation === 'orchestrator'", tr?.conversation === "orchestrator");
check("transcribe_audio inputs == null (nothing to validate)", (tr?.inputs ?? null) == null);
// Deterministic, tree-independent: this is WHY rails change (b) is needed — the plain gate
// would reject an inputs:null orchestrator skill and repair-loop it forever.
check("checkPayload(null, {}).ok === false (the trap rails change (b) avoids)", checkPayload(null, {}).ok === false);

// ---- done -------------------------------------------------------------------
console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`} — mary-skills self-test\n`);
process.exit(failures === 0 ? 0 : 1);
