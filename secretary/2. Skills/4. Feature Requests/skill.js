// ============================================================================
//  Skill "Feature Requests" (spec-a-feature) — LOGIC.
//  An OPEN-ENDED clarifying conversation (not a slot-filler). The owner starts with
//  `@secretary I have a feature idea …`; the secretary becomes stateful and interviews him,
//  accumulating a running DRAFT in the session, until he signals he's done. Then it
//  renders a Markdown feature spec (ALWAYS English) and delivers it as a real,
//  saveable `.md` document on the chat.
//
//  Run by the orchestrator when the router picks "feature_request".
//
//  Flow:
//    fresh  @secretary <idea>    -> startFeatureRequest -> ask + open session
//    every owner reply while open -> resumeClarify -> ask more / finalize / cancel
//    finalize                    -> generateDoc (English) -> sendMedia(.md) -> clear
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
//  Localized scaffolding replies live in prompt.js (reply(ctx.lang)); the per-turn
//  question is generated in-language by the model; the DOCUMENT is always English.
//  ctx.send is pre-bound to the conversation language. No capability registry, no new
//  env, no OAuth — pure Anthropic + Evolution (sendMedia).
// ============================================================================
import {
  buildClarifySystem,
  buildClarifyUser,
  buildDocSystem,
  buildDocUser,
  slugify,
  reply,
  CLARIFY_SCHEMA,
} from "./prompt.js";
import { headerFor } from "../../1. Orchestrator/lib/identity.js";
import { frame } from "../../1. Orchestrator/lib/format.js";
import { jsonFormat, readReply, readText } from "../../1. Orchestrator/lib/llm.js";
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
// secretary/2. Skills/4. Feature Requests/ to secretary/specs. The container only mounts
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

// `inputs: null` — NO declared inputs (see 1. Orchestrator/lib/inputs.js). This skill opens its
// own clarifying conversation, so there is nothing for the router's merged call to pre-extract,
// and nothing may be handed to it: a task with no declaration is never given a payload.
export const manifest = {
  id: "feature_request",
  inputs: null,
  description:
    "capture and spec out a NEW FEATURE / product idea the owner wants to build: hold a " +
    "clarifying conversation, then produce a Markdown spec document delivered as a file. " +
    "Use for 'I have a feature idea', 'spec this out', 'write up a feature request', " +
    "'new feature' — NOT for adding a to-do (task_action) or scheduling (calendar_action), " +
    "and NOT for changing how the owner summons the assistant, i.e. her trigger tag " +
    "(assistant_settings) — that is a setting she can change on the spot, not an idea to spec.",
};

const SESSION_TTL = 1800; // 30-min clarify window, re-armed each turn

// Empty draft shape (schema requires every field present, arrays default []).
const EMPTY_DRAFT = {
  title: null,
  one_liner: null,
  problem: null,
  trigger: null,
  actors: [],
  steps: [],
  data_touched: null,
  edge_cases: [],
  open_questions: [],
};

// Merge the model's draft over the prior one, always carrying every field forward so a
// dropped/blanked field never erases known content. Arrays stay arrays.
function mergeDraft(prior, next) {
  const p = prior || EMPTY_DRAFT;
  const n = next || {};
  const keepStr = (a, b) => (b != null && String(b).trim() ? b : a);
  const keepArr = (a, b) => (Array.isArray(b) && b.length ? b : a || []);
  return {
    title: keepStr(p.title, n.title),
    one_liner: keepStr(p.one_liner, n.one_liner),
    problem: keepStr(p.problem, n.problem),
    trigger: keepStr(p.trigger, n.trigger),
    actors: keepArr(p.actors, n.actors),
    steps: keepArr(p.steps, n.steps),
    data_touched: keepStr(p.data_touched, n.data_touched),
    edge_cases: keepArr(p.edge_cases, n.edge_cases),
    open_questions: Array.isArray(n.open_questions)
      ? n.open_questions
      : p.open_questions || [], // open_questions SHRINK as they're answered — take latest
  };
}

// ---- The per-turn clarify call ----------------------------------------------
async function clarifyTurn(ctx, priorDraft) {
  const { anthropic, model, owner, order, transcript, nowStr, lang } = ctx;
  const msg = await anthropic.messages.create({
    model,
    // The full draft is re-emitted every turn and grows as the spec fills out, so this
    // must comfortably fit a large draft + the reply — the FINALIZE turn is when the
    // draft is largest. 1500 truncated mid-JSON on real conversations (→ null → no doc).
    max_tokens: 4000,
    system: buildClarifySystem(owner, lang),
    output_config: jsonFormat(CLARIFY_SCHEMA),
    messages: [
      {
        role: "user",
        content: buildClarifyUser(owner, {
          draftJson: JSON.stringify(priorDraft || EMPTY_DRAFT),
          transcript,
          latest: order,
          nowStr,
        }),
      },
    ],
  });
  const out = readReply(msg, "feature_request");
  console.log("FEATURE CLARIFY RAW:", JSON.stringify(out));
  if (!out) return null;
  if (!["clarifying", "finalize", "cancel"].includes(out.status)) {
    out.status = "clarifying";
  }
  out.draft = mergeDraft(priorDraft, out.draft);
  return out;
}

// ctx (from the orchestrator): { owner, tag, anthropic, model, order, transcript,
//   nowStr, contact, remoteJid, number, fromMe, quoted, env, evolution, send,
//   sessions, session, lang, hasSkill, callSkill }
export async function run(ctx) {
  const { session } = ctx;
  // CONTINUATION owned by this skill (set by the orchestrator on a live session).
  if (session?.skill === "feature_request" && session.stage === "clarifying") {
    return resumeClarify(ctx, session);
  }
  // FRESH START: the router already chose this skill — no interpret step.
  return startFeatureRequest(ctx);
}

// ---- Fresh start: seed the draft, ask the first questions, open the session ---
async function startFeatureRequest(ctx) {
  const { number, send, sessions, remoteJid } = ctx;
  let out;
  try {
    out = await clarifyTurn(ctx, EMPTY_DRAFT);
  } catch (e) {
    console.error("feature_request/clarify error:", e?.message || e);
    await ctx.sendFailure(number, reply(ctx.lang).thinkingError());
    return;
  }
  if (!out) {
    await ctx.sendFailure(number, reply(ctx.lang).thinkingError());
    return;
  }
  if (out.status === "cancel") {
    await send(number, reply(ctx.lang).cancelled());
    return;
  }
  if (out.status === "finalize") {
    // Rare: a fully-specced idea handed over in one shot.
    return finalize(ctx, out.draft);
  }
  await openSession(ctx, out.draft);
  await send(number, out.reply || reply(ctx.lang).firstFallback());
}

// ---- Resume: runs on EVERY owner message while the session is open -----------
async function resumeClarify(ctx, session) {
  const { number, send, sessions, remoteJid } = ctx;
  let out;
  try {
    out = await clarifyTurn(ctx, session.draft);
  } catch (e) {
    console.error("feature_request/clarify error:", e?.message || e);
    return; // transient error: stay open, don't nag
  }
  if (!out) {
    // Unparseable turn (e.g. a truncated reply). Don't strand the owner in silence —
    // the prior draft is still in the session, so tell them and keep the window open so
    // "write it up" retries the finalize. (readReply already logged the cause.)
    await ctx.sendFailure(number, reply(ctx.lang).thinkingError());
    return;
  }

  if (out.status === "cancel") {
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).cancelled());
    return;
  }
  if (out.status === "finalize") {
    return finalize(ctx, out.draft);
  }
  // Still clarifying: persist the updated draft, re-arm the window, ask the next thing.
  await openSession(ctx, out.draft);
  await send(number, out.reply || reply(ctx.lang).continueFallback());
}

async function openSession(ctx, draft) {
  await ctx.sessions.set(
    ctx.remoteJid,
    {
      skill: "feature_request",
      intent: "spec",
      stage: "clarifying",
      awaitFrom: "owner",
      lang: ctx.lang,
      draft,
    },
    SESSION_TTL
  );
}

// ---- Finalize: render the English doc and deliver it as a .md attachment ------
async function finalize(ctx, draft) {
  const { number, send, sessions, remoteJid, evolution } = ctx;

  let md;
  try {
    md = await generateDoc(ctx, draft);
  } catch (e) {
    console.error("feature_request/doc error:", e?.message || e);
    md = null;
  }
  if (!md) {
    // Keep the session so the owner can retry the write without re-speccing.
    await ctx.sendFailure(number, reply(ctx.lang).renderError());
    return;
  }

  const slug = slugify(draft.title);
  const fileName = `feature-${slug}.md`; // THE ATTACHMENT NAME — unchanged
  // Spool the spec to secretary/specs BEFORE the send (scope: "written before it is sent"), so a
  // failed send never loses it. Never throws; a null return means the copy was not filed.
  const spooled = await spoolSpec(draft, md);
  const base64 = Buffer.from(md, "utf8").toString("base64");
  // sendMedia bypasses the orchestrator's send(), so frame the caption here — same
  // bold header + italic body as every other secretary message.
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

  await sessions.clear(remoteJid);
  // D3: if both fail, the send-failure wins and the owner gets exactly one reply — he needs to
  // know he never received the file, not that it merely wasn't filed.
  if (!ok) await ctx.sendFailure(number, reply(ctx.lang).sendFailed());
  else if (!spooled) await ctx.sendFailure(number, reply(ctx.lang).specFileFailed());
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
