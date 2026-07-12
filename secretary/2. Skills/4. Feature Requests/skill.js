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

export const manifest = {
  id: "feature_request",
  description:
    "capture and spec out a NEW FEATURE / product idea the owner wants to build: hold a " +
    "clarifying conversation, then produce a Markdown spec document delivered as a file. " +
    "Use for 'I have a feature idea', 'spec this out', 'write up a feature request', " +
    "'new feature' — NOT for adding a to-do (task_action) or scheduling (calendar_action).",
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
    await send(number, reply(ctx.lang).thinkingError());
    return;
  }
  if (!out) {
    await send(number, reply(ctx.lang).thinkingError());
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
    await send(number, reply(ctx.lang).thinkingError());
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
    await send(number, reply(ctx.lang).renderError());
    return;
  }

  const slug = slugify(draft.title);
  const fileName = `feature-${slug}.md`;
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
  if (!ok) await send(number, reply(ctx.lang).sendFailed());
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
