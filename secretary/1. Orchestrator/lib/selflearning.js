// ============================================================================
//  selflearning.js  —  FAILURE CAPTURE.
//
//  Turns a failure into a Markdown report on disk, which scripts/self-learning-pull.sh
//  syncs to the Mac and /triage-failures turns into an implementation plan.
//
//  This is orchestrator INFRASTRUCTURE, not a skill: every loaded skill is auto-appended
//  to CATALOG (the router's menu), and a skill the router must never pick is a misroute
//  hazard with no upside. server.js imports this directly.
//
//  Five triggers, four of them machine-detected (server.js) and one human (the `feedback`
//  skill — "@secretary you made a mistake here", the only detector for a wrong-but-confident
//  answer, which no amount of try/catch can see):
//    throw:continuation | throw:router | throw:skill | unrouted | soft | reported
//
//  TWO RULES THIS FILE NEVER BREAKS:
//    1. It never throws. Capture must not break the user's flow or mask the original error.
//    2. It never silently drops an OWNER-REPORTED note (see the `reported` exemptions below).
// ============================================================================
import { mkdir, writeFile, appendFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { getRecentLogs, redact } from "./logbuffer.js";
import { readText } from "./llm.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// lib/ -> "1. Orchestrator"/ -> secretary/improvements. INSIDE the app dir on purpose: the
// container only mounts /opt/secretary:/app, so nothing outside secretary/ is writable at
// runtime. The sync step relocates the reports afterwards.
const REPORTS_DIR =
  process.env.SELF_LEARNING_DIR ||
  path.join(__dirname, "..", "..", "improvements");

// The auto-analysis runs on the CHEAP model. Note ctx.model is the MAIN model (sonnet) and
// TRANSLATE_MODEL is a module-local const in server.js that is never passed down — so read
// the env directly rather than a ctx field that doesn't exist.
const ANALYSIS_MODEL =
  process.env.TRANSLATE_MODEL || "claude-haiku-4-5-20251001";

const DEDUPE_WINDOW_MS = 10 * 60 * 1000; // same failure twice in 10 min -> one report
const MACHINE_CAP_PER_HOUR = 20; // a crash loop must not fill the droplet's disk
const REPORTED_CAP_PER_HOUR = 10; // a human typing on a phone cannot loop; pure disk backstop

const recentHashes = new Map(); // hash -> ts
const captureTimes = []; // ts of every report written (both kinds)

// ---- What counts as a malfunction --------------------------------------------
// EXACTLY THREE THINGS:
//   1. a code error                    -> the catch blocks in server.js  (throw:*)
//   2. a soft landing of an UNCOMPLETED task, declared by the skill -> ctx.sendFailure (soft)
//      ...including "I didn't understand": the `unrouted` branch and the skills' noAction.
//      It reads like guidance, but the owner asked and got nothing — and it is the clearest
//      signal of a MISSING CAPABILITY, i.e. what to build next. (Decided 2026-07-12: file it.)
//   3. the owner saying it got something wrong -> the `feedback` skill  (reported)
//
// Everything else the secretary says is GUIDANCE — "reply to the audio you want
// transcribed", "which task did you mean?", "what should the task say?", "your list is
// empty" — and guidance is NOT a malfunction. Asking a question, or truthfully reporting
// an empty result, is the secretary working.
//
// THE TEST IS NOT WHETHER THE MESSAGE SOUNDS APOLOGETIC. It is whether the owner asked for
// something and did not get it. "I couldn't find: buy milk. Which one did you mean?" sounds
// like a failure and is a question. "Done — but couldn't do these: call Ana" sounds like a
// success and IS a failure. Read the outcome, not the tone.
//
// There is deliberately NO runtime text scanning. An earlier version regex-scanned every
// outgoing message and was wrong in both directions: it missed half the real failures ("I
// hit an error while thinking" contains no failure word) and it fired on "I couldn't find:
// X. Which one did you mean?" — a clarifying QUESTION. Prose cannot be classified by
// keyword; only the skill knows whether it just failed the owner or just asked him
// something. So only the skill decides, at the call site, by choosing sendFailure or send.
//
// The guard against a skill FORGETTING is a lint over the call sites in
// scripts/selflearning-selftest.mjs — a test-time failure, not a production guess.
// FAILURE_KEY_RE is that lint's rule, exported so the check and the convention can't drift:
// a reply key that names an error/failure must be sent with ctx.sendFailure.
export const FAILURE_KEY_RE = /(error|failed|failure|unavailable|noMatch|noAction)/i;

// ---- Time -------------------------------------------------------------------
// "sv-SE" formats as "2026-07-12 14:03:21" — ISO-shaped, no parsing games, and the
// timeZone option puts it in the owner's wall-clock (America/Sao_Paulo), which is the
// only clock he can correlate a report against when he reads it.
function saoPauloStamp(d = new Date()) {
  return d.toLocaleString("sv-SE", { timeZone: "America/Sao_Paulo" });
}

function slug(s, fallback = "unknown") {
  const out = String(s || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "") // strip diacritics
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  return out || fallback;
}

// ---- Rate limiting ----------------------------------------------------------
function underHourlyCap(phase) {
  const cutoff = Date.now() - 60 * 60 * 1000;
  while (captureTimes.length && captureTimes[0] < cutoff) captureTimes.shift();
  const cap = phase === "reported" ? REPORTED_CAP_PER_HOUR : MACHINE_CAP_PER_HOUR;
  return captureTimes.length < cap;
}

// Machine failures dedupe; OWNER REPORTS NEVER DO. The dedupe window exists to survive a
// crash loop — a machine emitting the same stack hundreds of times a minute. Two notes from
// the owner 30 seconds apart are two distinct complaints, and silently dropping one is the
// worst thing this feature could do: he'd believe it was filed, and stop reporting after the
// second one that went nowhere.
function isDuplicate(phase, taskId, error) {
  if (phase === "reported") return false;
  const firstLine = String(error?.message || "").split("\n")[0];
  const hash = `${phase}|${taskId}|${firstLine}`;
  const prev = recentHashes.get(hash);
  const now = Date.now();
  if (prev && now - prev < DEDUPE_WINDOW_MS) return true;
  recentHashes.set(hash, now);
  if (recentHashes.size > 200) {
    for (const [k, t] of recentHashes) {
      if (now - t > DEDUPE_WINDOW_MS) recentHashes.delete(k);
    }
  }
  return false;
}

// ---- Optional auto-analysis (best-effort, unverified) ------------------------
// A cheap 3–5 line guess at the cause, to give the triage agent a lead. Kept in its own
// try/catch and in its OWN section of the report: the triage prompt is told to treat this as
// a discardable hunch, and never to confuse it with the owner's ground-truth account.
async function analyze(ctx, { phase, what, logs }) {
  if (!ctx?.anthropic) return null;
  try {
    const msg = await ctx.anthropic.messages.create({
      model: ANALYSIS_MODEL,
      max_tokens: 400,
      system:
        "You are a senior engineer triaging a failure in a Node.js WhatsApp assistant " +
        "(an Express webhook -> LLM router -> skills). Given the failure and recent logs, " +
        "reply with 3-5 short lines: the LIKELY CAUSE and the SUSPECTED file/area. " +
        "Be concrete, flag uncertainty plainly, and never invent file names you cannot " +
        "infer from the evidence. Plain text, no preamble, no markdown headers.",
      messages: [
        {
          role: "user",
          content: `Trigger: ${phase}\n\nWhat happened:\n${what}\n\nRecent logs:\n${logs}`,
        },
      ],
    });
    return readText(msg) || null;
  } catch (e) {
    console.error("selflearning/analysis error:", e?.message || e);
    return null; // the report ships without it
  }
}

// ---- Writing ----------------------------------------------------------------
// The filename stamp has ONE-SECOND resolution, so two reports in the same second collide.
// Write with the "wx" flag (exclusive create: fail if it exists) and suffix on collision,
// rather than the default "w", which would SILENTLY OVERWRITE the first report. That
// distinction is the difference between "two complaints filed" and "one complaint lost
// without a trace" — the exact failure this whole feature is built to prevent.
async function writeUnique(base, contents) {
  for (let i = 0; i < 50; i++) {
    const full = path.join(REPORTS_DIR, i === 0 ? `${base}.md` : `${base}-${i + 1}.md`);
    try {
      await writeFile(full, contents, { encoding: "utf8", flag: "wx" });
      return full;
    } catch (e) {
      if (e?.code === "EEXIST") continue; // same second, different report — take the next name
      throw e; // anything else (ENOENT, EACCES, ENOSPC) is the caller's outer catch
    }
  }
  console.error("selflearning: could not find a free report filename for", base);
  return null;
}

// ---- The report --------------------------------------------------------------
function render({ phase, taskId, when, ctx, what, stack, report, analysis, logs, detection }) {
  const isReported = phase === "reported";
  const q = report?.quotedText;

  const ownerSection = isReported
    ? `
## Owner's report
**What the owner says went wrong:** ${report?.whatWentWrong || report?.note || "(not stated)"}
**What they expected instead:** ${report?.expected || "not stated"}
**His exact words:** ${report?.note || "(none)"}
${report?.followUpTo ? `**Follow-up to:** ${report.followUpTo}\n` : ""}
### The offending message (quoted)
${
  q
    ? `${report?.quotedIsSecretary ? "Confirmed to be one of the secretary's own messages.\n" : "NOTE: this quoted message is NOT secretary output.\n"}\n\`\`\`\n${q}\n\`\`\``
    : "_Not quoted — the owner reported this without replying to a specific message. The evidence is in the transcript and the logs below._"
}
`
    : "";

  return `# Failure report — ${phase} / ${taskId}  (${when})

| Field       | Value |
|-------------|-------|
| When        | ${when} (America/Sao_Paulo) |
| Chat        | ${ctx?.remoteJid || "?"} (${ctx?.contact || "?"}) |
| Trigger     | ${phase} |
| Source      | ${isReported ? "**OWNER-REPORTED** (human-verified)" : "machine-detected"} |
| Failed task | ${taskId} |${detection ? `\n| Detected via | ${detection} |` : ""}
| Status      | needs-plan |

## What the user asked
${ctx?.order || "(no order)"}${isReported ? "\n\n_(On an owner-reported failure this is the NOTE itself, not the order that misbehaved — that one is in the transcript.)_" : ""}

## What happened
${what}

\`\`\`
${stack || "n/a"}
\`\`\`
${ownerSection}
## Auto-analysis (best-effort, UNVERIFIED — a cheap model's guess, not evidence)
${analysis || "n/a"}

## Recent logs
\`\`\`
${logs || "(none)"}
\`\`\`

## Conversation transcript
\`\`\`
${ctx?.transcript || "(none)"}
\`\`\`
`;
}

// ============================================================================
//  captureFailure(ctx, info)
//    info = { phase, taskId, error?, softMessage?, unroutedOrder?, report? }
//    phase = throw:continuation | throw:router | throw:skill | soft | unrouted | reported
//
//  Returns the ABSOLUTE PATH of the report written, or null if nothing was written
//  (deduped, capped, or an internal error). The `feedback` skill relies on the path:
//  it parks it in the session so a later clarifying answer can be appended to the
//  report that ALREADY EXISTS — the report is never contingent on the answer arriving.
//
//  NEVER THROWS.
// ============================================================================
export async function captureFailure(ctx, info = {}) {
  try {
    const { phase = "unknown", error, softMessage, unroutedOrder, report, detection } = info;
    const taskId = slug(info.taskId, "unknown");

    // Per-turn guard. ctx._turn is a shared MUTABLE OBJECT, not a boolean, and that is
    // load-bearing: ctx.callSkill does fn({ ...ctx, _skillDepth }) — a SPREAD — so a flag
    // set by a callee on a copied ctx would never reach the caller. The spread copies the
    // object's REFERENCE, so every frame in the turn sees the same _turn.
    const turn = ctx?._turn;
    if (turn?.captured) return null;

    if (isDuplicate(phase, taskId, error)) return null;
    if (!underHourlyCap(phase)) {
      console.error(`selflearning: hourly cap hit, dropping ${phase} report`);
      return null; // the feedback skill TELLS the owner rather than confirming a lie
    }
    if (turn) turn.captured = true;

    const when = saoPauloStamp();
    // The failure the owner is reporting happened in an EARLIER webhook turn, so its logs
    // sit further back in the ring — under this turn's own router/skill logging.
    const logs = getRecentLogs(phase === "reported" ? 250 : 80);

    const what =
      error?.message ||
      softMessage ||
      (unroutedOrder ? `Router matched no skill for: ${unroutedOrder}` : null) ||
      report?.whatWentWrong ||
      report?.note ||
      "(no detail)";

    const analysis = await analyze(ctx, { phase, what, logs });

    const md = render({
      phase,
      taskId,
      when,
      ctx,
      what,
      stack: error?.stack,
      report,
      analysis,
      logs,
      detection,
    });

    const base = `${when.replace(" ", "T").replace(/:/g, "-")}-${slug(phase)}-${taskId}`;
    await mkdir(REPORTS_DIR, { recursive: true });
    // Redact the WHOLE report, not just the log lines: the transcript and the owner's own
    // note pass through here too, and this file is destined for a git repo.
    const full = await writeUnique(base, redact(md));
    if (!full) return null;

    captureTimes.push(Date.now());
    console.log(`selflearning: wrote report ${path.basename(full)}`);
    return full;
  } catch (e) {
    // Capture failing must never surface to the user or mask the original error.
    console.error("selflearning/captureFailure failed:", e?.message || e);
    return null;
  }
}

// Append the owner's clarifying answer to a report that was ALREADY written. Returns
// true on success. The caller falls back to a fresh, linked report if this fails — the
// answer is never dropped.
export async function appendToReport(reportPath, section) {
  try {
    if (!reportPath || !section) return false;
    await appendFile(reportPath, redact(`\n${section}\n`), "utf8");
    return true;
  } catch (e) {
    console.error("selflearning/appendToReport failed:", e?.message || e);
    return false;
  }
}
