// ============================================================================
//  Skill "Feature Requests" (spec-a-feature) — LOGIC.  CONVERTED (pure task).
//  In the NEW (@mary) flow the ORCHESTRATOR runs the open-ended clarifying interview over
//  `listen` turns and hands the finished BRIEF in ctx.info. This skill no longer holds a
//  session or asks questions: it RENDERS the Markdown feature spec (ALWAYS English) from the
//  brief, spools a copy, delivers it as a real `.md` document, and RETURNS what it filed.
//
//  Run by the orchestrator when the router picks "feature_request".
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description, conversation, inputs }
//    export async function run(ctx) -> a JSON-serializable value (the read-back)
//  Localized outcome strings live in prompt.js (reply(ctx.lang)); the DOCUMENT is always
//  English. ctx.send is pre-bound to the conversation language. No capability registry, no new
//  env, no OAuth — pure Anthropic + Evolution (sendMedia).
// ============================================================================
import { buildDocSystem, buildDocUser, slugify, reply } from "./prompt.js";
import { headerFor } from "../../1. Orchestrator/lib/identity.js";
import { frame } from "../../1. Orchestrator/lib/format.js";
import { readText } from "../../1. Orchestrator/lib/llm.js";
// ESM PRELUDE — REQUIRED. secretary/package.json is "type": "module", so this file is an ES
// module and `__dirname` DOES NOT EXIST. It must be built, and node:fs/promises + node:path +
// node:url imported, EXACTLY as 1. Orchestrator/lib/selflearning.js:20-27 does. Omitting any of
// these throws `ReferenceError: __dirname is not defined` at IMPORT time, and the orchestrator
// then fails to load the whole feature_request skill at boot.
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Spool dir. Same shape as REPORTS_DIR in 1. Orchestrator/lib/selflearning.js:31-33 — env
// override, else inside secretary/. The `../../specs` arithmetic resolves from
// secretary/3. Mary Skills/4. Feature Requests/ to secretary/specs. The container only mounts
// /opt/secretary:/app, so the spool MUST live inside secretary/.
const SPEC_DIR =
  process.env.FEATURE_SPEC_DIR || path.join(__dirname, "..", "..", "specs");

// "2026-07-14 09:12:03" — sv-SE (ISO-shaped, no parsing games) in the owner's wall-clock,
// the same shape selflearning.js uses for its report timestamps.
function saoPauloStamp(d = new Date()) {
  return d.toLocaleString("sv-SE", { timeZone: "America/Sao_Paulo" });
}

// The machine-readable frontmatter header (D2) the board ingest parses. Newlines in the
// title / one-liner are collapsed to single spaces so the ingest's line-based parser can
// never be broken by a multi-line title.
function specHeader(draft, when) {
  const oneLine = (s) => String(s || "").replace(/\s*\n\s*/g, " ").trim();
  return [
    "---",
    `title: ${oneLine(draft.title)}`,
    `one_liner: ${oneLine(draft.one_liner)}`,
    `when: ${when} (America/Sao_Paulo)`,
    "---",
  ].join("\n");
}

// Spool the spec to secretary/specs BEFORE the WhatsApp send, so a failed send never loses it.
// Filename: feature-<slug>-<YYYY-MM-DDTHH-MM-SS>.md (D1 — the timestamp is a SUFFIX so the name
// still matches the enqueue glob `feature-*.md`). Exclusive-create ("wx") + numeric suffix on
// collision, the writeUnique shape from selflearning.js:164-177 (its shape is copied, the module
// is NOT imported). Returns the absolute path written, or null. NEVER THROWS — a spool failure
// must not break the send.
async function spoolSpec(draft, md) {
  try {
    await mkdir(SPEC_DIR, { recursive: true });
    const when = saoPauloStamp();
    const stamp = when.replace(" ", "T").replace(/:/g, "-");
    const base = `feature-${slugify(draft.title)}-${stamp}`;
    const contents = `${specHeader(draft, when)}\n${md}`;
    for (let i = 0; i < 50; i++) {
      const full = path.join(SPEC_DIR, i === 0 ? `${base}.md` : `${base}-${i + 1}.md`);
      try {
        await writeFile(full, contents, { encoding: "utf8", flag: "wx" });
        return full;
      } catch (e) {
        if (e?.code === "EEXIST") continue; // same second, different spec — take the next name
        throw e;
      }
    }
    console.error("feature_request: could not find a free spec filename for", base);
    return null;
  } catch (e) {
    console.error("feature_request/spoolSpec error:", e?.message || e);
    return null;
  }
}

// `inputs` — the DECLARED input contract (see 1. Orchestrator/lib/inputs.js): the finished BRIEF.
// The orchestrator interviews the owner over listen turns and gates on `ok` before dispatching,
// so ctx.info arrives with the substance filled. title / one_liner / problem are NOT nullable
// (a brief without them is not a brief); the rest are optional detail the doc render uses.
export const manifest = {
  id: "feature_request",
  // CONVERTED (pure task): the model runs the interview; run() only renders + delivers + returns.
  conversation: "orchestrator",
  inputs: {
    discriminator: null,
    fields: {
      title: { type: "string", desc: "short feature name (a few words) — becomes the filename slug" },
      one_liner: { type: "string", desc: "one-sentence summary of the feature" },
      problem: { type: "string", desc: "the pain / motivation this solves and why it matters" },
      trigger: { type: "string", nullable: true, desc: "how the user starts / triggers the flow" },
      actors: { type: "array", of: { type: "string" }, desc: "who is involved" },
      steps: { type: "array", of: { type: "string" }, desc: "the user-point-of-view flow, one step per item" },
      data_touched: { type: "string", nullable: true, desc: "systems, data, integrations involved" },
      edge_cases: { type: "array", of: { type: "string" }, desc: "edge cases to handle" },
      open_questions: { type: "array", of: { type: "string" }, desc: "unresolved questions" },
    },
    requiredWhen: {},
    consistency: [
      {
        name: "brief_has_substance",
        test: (i) =>
          !!i.title && String(i.title).trim() !== "" &&
          !!i.one_liner && String(i.one_liner).trim() !== "" &&
          !!i.problem && String(i.problem).trim() !== "",
      },
    ],
    rulebook: () =>
      "Interview the owner like a sharp product manager and build up the spec BEFORE you dispatch. " +
      "Collect: title, a one-sentence summary (one_liner), the problem/motivation, how the user " +
      "triggers it, the actors, the user-point-of-view steps, the data/services touched, the edge " +
      "cases, and any open questions. Focus on the flow FROM THE USER'S POINT OF VIEW, not " +
      "implementation internals unless he raises them. Do NOT invent facts he hasn't given — put " +
      "genuine unknowns in open_questions and ask about the important ones. Dispatch only once " +
      "title, one_liner and problem are filled; keep asking otherwise.",
  },
  description:
    "capture and spec out a NEW FEATURE / product idea the owner wants to build: produce a " +
    "Markdown spec document delivered as a file. Use for 'I have a feature idea', 'spec this " +
    "out', 'write up a feature request', 'new feature' — NOT for adding a to-do (task_action) " +
    "or scheduling (calendar_action), and NOT for changing how the owner summons the assistant, " +
    "i.e. her trigger tag (assistant_settings) — that is a setting she can change on the spot, " +
    "not an idea to spec.",
};

// The model has already run the interview (ctx.info IS the brief). run() renders the English doc,
// spools a copy, delivers it, and returns what it filed.
export async function run(ctx) {
  const { number, evolution } = ctx;
  const draft = ctx.info || {};

  let md;
  try {
    md = await generateDoc(ctx, draft);
  } catch (e) {
    console.error("feature_request/doc error:", e?.message || e);
    md = null;
  }
  if (!md) {
    await ctx.sendFailure(number, reply(ctx.lang).renderError());
    return { ok: false, reason: "renderError" };
  }

  const slug = slugify(draft.title);
  const fileName = `feature-${slug}.md`; // THE ATTACHMENT NAME — unchanged
  // Spool the spec to secretary/specs BEFORE the send, so a failed send never loses it. Never
  // throws; a null return means the copy was not filed.
  const spooled = await spoolSpec(draft, md);
  const base64 = Buffer.from(md, "utf8").toString("base64");
  // sendMedia bypasses the orchestrator's send(), so frame the caption here — same bold header +
  // italic body as every other secretary message.
  const caption = frame(
    headerFor(ctx.lang),
    reply(ctx.lang).docCaption({ title: draft.title || slug })
  );

  let ok = false;
  try {
    ok = await evolution.sendMedia(number, {
      mediatype: "document",
      mimetype: "text/markdown",
      media: base64,
      fileName,
      caption,
    });
  } catch (e) {
    console.error("feature_request/sendMedia error:", e?.message || e);
    ok = false;
  }

  // D3: if both fail, the send-failure wins and the owner gets exactly one reply — he needs to
  // know he never received the file, not that it merely wasn't filed.
  if (!ok) {
    await ctx.sendFailure(number, reply(ctx.lang).sendFailed());
    return { ok: false, reason: "sendFailed", path: spooled };
  }
  if (!spooled) {
    await ctx.sendFailure(number, reply(ctx.lang).specFileFailed());
    return { ok: true, path: null, title: draft.title || slug };
  }
  return { ok: true, path: spooled, title: draft.title || slug };
}

// ---- Document generation (ALWAYS English; returns markdown PROSE, not JSON) ---
async function generateDoc(ctx, draft) {
  const { anthropic, model } = ctx;
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 4000, // a full feature spec can run long; leave headroom
    system: buildDocSystem(),
    messages: [
      {
        role: "user",
        content: buildDocUser({ draftJson: JSON.stringify(draft) }),
      },
    ],
  });
  const md = readText(msg);
  console.log("FEATURE DOC LEN:", md.length);
  return md || null;
}
