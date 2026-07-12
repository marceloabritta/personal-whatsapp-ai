#!/usr/bin/env node
// ============================================================================
//  Self-test for the self-learning capture layer.
//
//  The app can't be exercised end-to-end on the Mac (it needs the Evolution webhook), so
//  this drives lib/logbuffer.js + lib/selflearning.js directly with a fake ctx and asserts
//  the invariants that are easy to break and expensive to break in production:
//
//    1. a hard-throw report contains the error, stack, transcript and logs
//    2. secrets in the logs come back REDACTED
//    3. the same machine error twice -> ONE report (dedupe)
//    4. a capture through a { ...ctx } SPREAD is suppressed by the shared _turn flag
//       (the regression test for the bug the first draft of the plan had)
//    5. an OWNER-REPORTED note is NEVER deduped — two notes, two reports
//    6. a `reported` capture whose LLM call THROWS still writes a report
//    7. captureFailure with a broken fs does NOT throw
//    8. appendToReport() adds the owner's follow-up to an existing report
//
//  Run:  node scripts/selflearning-selftest.mjs
// ============================================================================
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const DIR = await mkdtemp(path.join(tmpdir(), "selflearning-"));
process.env.SELF_LEARNING_DIR = DIR; // redirect reports away from secretary/improvements

const { installLogBuffer, __resetLogBuffer } = await import(
  "../secretary/1. Orchestrator/lib/logbuffer.js"
);
const { captureFailure, appendToReport, FAILURE_KEY_RE } = await import(
  "../secretary/1. Orchestrator/lib/selflearning.js"
);

installLogBuffer();

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}
const reports = async () => (await readdir(DIR)).filter((f) => f.endsWith(".md"));
const readAll = async () => {
  const out = [];
  for (const f of await reports()) out.push(await readFile(path.join(DIR, f), "utf8"));
  return out.join("\n---\n");
};

// A ctx shaped like the orchestrator's, with a stub Anthropic client.
function makeCtx(overrides = {}) {
  return {
    anthropic: {
      messages: {
        create: async () => ({
          content: [{ type: "text", text: "Likely cause: the stub model said so." }],
        }),
      },
    },
    model: "claude-sonnet-5",
    owner: "Marcelo",
    order: "schedule lunch with Ana tomorrow",
    transcript: "Marcelo: @secretary schedule lunch with Ana tomorrow",
    nowStr: "Saturday, 07/12/2026, 02:00 PM",
    contact: "Ana",
    remoteJid: "5511999@s.whatsapp.net",
    catalog: [{ id: "calendar_action", description: "calendar" }],
    _turn: { captured: false },
    ...overrides,
  };
}

console.log(`\nself-learning self-test  (reports -> ${DIR})\n`);

// ---- 1 + 2: content and redaction -------------------------------------------
console.log("1/2  hard throw: content + redaction");
console.log("ANTHROPIC_API_KEY=sk-ant-abc123def456ghi789 loaded");
console.log("calling google with Authorization: Bearer ya29.super-secret-token");
const err = new Error("boom: calendar insert failed");
const p1 = await captureFailure(makeCtx(), {
  phase: "throw:skill",
  taskId: "calendar_action",
  error: err,
});
const r1 = p1 ? await readFile(p1, "utf8") : "";
check("wrote a report", !!p1);
check("contains the error message", r1.includes("boom: calendar insert failed"));
check("contains the stack", r1.includes("at ") && r1.includes("selflearning-selftest"));
check("contains the transcript", r1.includes("schedule lunch with Ana"));
check("contains the recent logs", r1.includes("ANTHROPIC_API_KEY"));
check("REDACTS the anthropic key", !r1.includes("sk-ant-abc123def456ghi789"));
check("REDACTS the bearer token", !r1.includes("ya29.super-secret-token"));
check("marked machine-detected", r1.includes("machine-detected"));

// ---- 3: dedupe (machine) -----------------------------------------------------
console.log("\n3    machine dedupe");
const before = (await reports()).length;
await captureFailure(makeCtx(), {
  phase: "throw:skill",
  taskId: "calendar_action",
  error: new Error("boom: calendar insert failed"),
});
check("same error twice -> no second report", (await reports()).length === before);

// ---- 4: the ctx-spread regression -------------------------------------------
console.log("\n4    _turn survives a { ...ctx } spread (callSkill)");
const parent = makeCtx();
await captureFailure(parent, {
  phase: "throw:skill",
  taskId: "task_action",
  error: new Error("first failure in this turn"),
});
const n4 = (await reports()).length;
const callee = { ...parent, _skillDepth: 1 }; // exactly what ctx.callSkill does
await captureFailure(callee, {
  phase: "throw:skill",
  taskId: "task_action",
  error: new Error("a DIFFERENT failure, same turn"),
});
check(
  "a callee's capture in the same turn is suppressed",
  (await reports()).length === n4
);

// ---- 5: owner reports never dedupe ------------------------------------------
console.log("\n5    owner-reported: never deduped");
const n5 = (await reports()).length;
const rep = {
  note: "you made a mistake here",
  whatWentWrong: "Created the event at 6pm instead of 5pm.",
  expected: "5pm",
  quotedText: "*[Marcelo's AI Secretary]:*\n\n_Event created for 6pm._",
  quotedIsSecretary: true,
};
const pA = await captureFailure(makeCtx(), {
  phase: "reported",
  taskId: "calendar_action",
  report: rep,
});
const pB = await captureFailure(makeCtx(), {
  phase: "reported",
  taskId: "calendar_action",
  report: rep,
}); // byte-identical, would be deduped if it were a machine failure
check("two identical owner notes -> TWO reports", (await reports()).length === n5 + 2);
const rA = await readFile(pA, "utf8");
check("marked OWNER-REPORTED", rA.includes("OWNER-REPORTED"));
check("carries the owner's exact words", rA.includes("you made a mistake here"));
check("carries the quoted offending message", rA.includes("Event created for 6pm"));
check(
  "flags the quote as secretary output",
  rA.includes("Confirmed to be one of the secretary's own messages")
);

// ---- 6: a broken LLM must not cost us the note -------------------------------
console.log("\n6    owner-reported survives an LLM failure");
const brokenLLM = makeCtx({
  anthropic: {
    messages: {
      create: async () => {
        throw new Error("anthropic 529 overloaded");
      },
    },
  },
});
const p6 = await captureFailure(brokenLLM, {
  phase: "reported",
  taskId: "feedback",
  report: { note: "you got that wrong", whatWentWrong: null, expected: null },
});
check("report still written", !!p6);
check(
  "auto-analysis degrades to n/a",
  p6 && (await readFile(p6, "utf8")).includes("Auto-analysis")
);

// ---- 7: never throws ---------------------------------------------------------
console.log("\n7    never throws");
let threw = false;
try {
  const bad = makeCtx();
  process.env.SELF_LEARNING_DIR = "/proc/nonexistent/cannot-write"; // read-only path
  const { captureFailure: cf } = await import(
    `../secretary/1. Orchestrator/lib/selflearning.js?bust=${Date.now()}`
  );
  const r = await cf(bad, { phase: "soft", taskId: "x", softMessage: "I couldn't do it" });
  check("returns null instead of writing", r === null);
} catch {
  threw = true;
} finally {
  process.env.SELF_LEARNING_DIR = DIR;
}
check("did not throw on an unwritable dir", !threw);

// ---- 8: append the owner's follow-up ----------------------------------------
console.log("\n8    appendToReport");
const okAppend = await appendToReport(pA, "## Owner's follow-up\n> the 6pm one");
check("append reports success", okAppend === true);
check("follow-up is in the file", (await readFile(pA, "utf8")).includes("the 6pm one"));
check("append to a missing file returns false", (await appendToReport("/nope/x.md", "y")) === false);

// ---- 9: THE LINT — every failure reply must be DECLARED -----------------------
// A malfunction is exactly three things: a code error, a soft landing of an uncompleted
// task, and the owner saying it got it wrong. The second one is the skill's job to declare
// (ctx.sendFailure). Nothing scans message text at runtime — prose can't be classified by
// keyword, and trying to do so flagged "I couldn't find: X. Which one did you mean?" (a
// clarifying QUESTION) as a malfunction while missing "I hit an error while thinking".
//
// So the guard lives HERE instead: read the skills, and fail the test if a reply whose name
// says "error/failed/unavailable/noMatch/noAction" is sent with plain send(). A forgotten
// call site becomes a red test run, not a bug that quietly never reports itself.
console.log("\n9    lint: failure replies are declared with ctx.sendFailure");
const SKILLS_DIR = new URL("../secretary/2. Skills/", import.meta.url);

// The one deliberate exemption, with its reason.
const EXEMPT = [
  // feedback.logFailed: "I couldn't file that note". This is capture ITSELF having failed —
  // routing it through sendFailure would just re-enter the capture that is already broken.
  "reply(ctx.lang).logFailed()",
];

let linted = 0;
for (const dir of await readdir(SKILLS_DIR)) {
  let src;
  try {
    src = await readFile(new URL(`${dir}/skill.js`, SKILLS_DIR), "utf8");
  } catch {
    continue; // not a skill folder
  }
  src.split("\n").forEach((line, i) => {
    const isSend = /\b(await |return )?send\(\s*number/.test(line) && !/sendFailure\(/.test(line);
    if (!isSend) return;
    // the reply key on this line, e.g. reply(lang).createGoogleError()
    const key = line.match(/reply\([^)]*\)\.(\w+)|\bM\.(\w+)/);
    const name = key?.[1] || key?.[2];
    if (!name || !FAILURE_KEY_RE.test(name)) return;
    if (EXEMPT.some((e) => line.includes(e))) return;
    linted++;
    check(`${dir}/skill.js:${i + 1} — '${name}' must use ctx.sendFailure`, false);
  });
}
check(
  "no failure reply is sent with plain send()",
  linted === 0
);
check("the lint actually ran (skills were readable)", (await readdir(SKILLS_DIR)).length >= 4);

// ---- done --------------------------------------------------------------------
console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`} — ${(await reports()).length} reports written\n`);
if (process.env.KEEP_REPORTS !== "1") await rm(DIR, { recursive: true, force: true });
else console.log(`kept: ${DIR}`);
process.exit(failures === 0 ? 0 : 1);
