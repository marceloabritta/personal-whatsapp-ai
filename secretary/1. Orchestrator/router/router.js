// ============================================================================
//  router/router.js  —  THE UNIFIED TURN CALL + THE EXTRACTION CALL.
//
//  A @mary turn is ONE unified messages.create (route) carrying the native toolset AND the
//  schema-enforced decision envelope in the SAME call:
//    - output_config: jsonFormat(TURN_DECISION_SCHEMA)  — the reply is schema-locked to the
//      three-decision envelope {say, keepListening, execute} (+ lang, pendingNeed).
//    - tools: buildNativeTools(ctx.env)                  — web_search / web_fetch / (code_exec);
//      the model may search the web mid-turn, then fill `say`. [] when NATIVE_TOOLS is off, so the
//      feature degrades to a tool-less schema call.
//    - thinking: { type: "adaptive" }                   — extended thinking on (output_config,
//      tools and thinking COMPOSE; the old "there is NO output_config on this call" rationale is
//      now false — see PLAN Rev 3).
//  Server-side tools run an internal sampling loop; when it hits its cap the reply comes back with
//  stop_reason "pause_turn" and MUST be resumed by resending [user, assistant(content)] (no
//  "Continue." text). That resume loop is bounded by NATIVE_MAX_TOOL_HOPS.
//
//  Payload extraction is a SECOND call (extract): output_config whose schema is derived SHAPE-ONLY
//  from the chosen skill's declaration by buildExecuteSchema (lib/inputs.js) — so the orchestrator
//  still never imports a skill's schema. On a repair (a payload that failed checkPayload) extract()
//  is RE-RUN with the validation problems threaded into its `problems` param; there is no repair
//  DECISION turn and no answer() pass.
//
//  Both calls read their JSON via readReply (lib/llm.js), which runs parseJsonReply internally —
//  the balanced-brace scan is the RETAINED degrade-to-today fallback for a leaked-prose reply, not
//  removed. A refusal / unparseable / still-paused turn degrades to a silent close with degraded:true;
//  server.js fires the "I didn't understand" menu + the unrouted report ONLY on that flag.
// ============================================================================
import { buildRouterSystem, buildRouterUser, buildReadbackUser, buildExtractionUser } from "./prompt.js";
import { buildNativeTools } from "../lib/nativeTools.js";
import { readReply, jsonFormat } from "../lib/llm.js";
import { buildExecuteSchema } from "../lib/inputs.js";

// The turn call's per-create wall clock (so a web lookup can't hang the turn) and the pause_turn
// resume cap. Read from ctx.env (same env vars the old answer pass used), with locked defaults.
const NATIVE_TURN_TIMEOUT_MS_DEFAULT = 30000;
const NATIVE_MAX_TOOL_HOPS_DEFAULT = 4;
// max_tokens is the budget for the WHOLE completion INCLUDING adaptive-thinking tokens. A lean
// classify used 1024 with thinking OFF; with adaptive thinking ON the model can spend most of the
// budget reasoning and get TRUNCATED before it emits the JSON (stop_reason "max_tokens" -> an
// unparseable reply -> a spurious degrade — observed live on a plain calendar order at 2048). 8192
// leaves ample headroom for the reasoning AND the small decision/extraction payload.
const TURN_MAX_TOKENS = 8192;

// The THREE-DECISION envelope (item 1). additionalProperties:false so the model cannot leak a
// field; nullable via a union type. NO `next` enum, NO `answer`, NO `info` (extraction is a 2nd
// call), NO `awaitFrom`.
//   say           : prose to send this turn, or null (deliberate silence / nothing to add).
//                   Answering a question (incl. from a web search this turn) is say=prose.
//   keepListening : true = conversation stays open (ASK, PROPOSE-and-wait, or ignore chatter);
//                   false = stop / close.
//   execute       : the task id(s) to run now, or [] for none. A LIST to preserve dual-intent
//                   (e.g. ["feedback","calendar_action"]); the primary (execute[0]) gets the
//                   two-phase payload. Usually one id.
//   pendingNeed   : keepListening turns only; one short phrase naming what you await, else null.
//   lang          : ISO 639-1, pinned to the first-call language.
export const TURN_DECISION_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["keepListening"],
  properties: {
    say: { type: ["string", "null"] },
    keepListening: { type: "boolean" },
    execute: { type: ["array", "null"], items: { type: "string" } },
    lang: { type: "string" },
    pendingNeed: { type: ["string", "null"] },
  },
};

// The degrade envelope: a refusal / unparseable / still-paused turn closes silently, and server.js
// fires the "I didn't understand" alarm off `degraded` (only on the first turn).
function degradeEnvelope() {
  return { say: null, keepListening: false, execute: [], lang: "en", pendingNeed: null, degraded: true };
}

// Build the create `content`: with media, prepend the file blocks (drop an empty text block, which
// the Messages API can 400 on); without, the byte-identical user string.
function withMediaContent(user, mediaBlocks) {
  if (!mediaBlocks) return user;
  return user && user.trim()
    ? [...mediaBlocks, { type: "text", text: user }]
    : [...mediaBlocks];
}

// ONE turn of the orchestrator's conversation — THE UNIFIED TURN CALL. Given the whole
// conversation (labelled, so the model can tell HER words from HIS) it returns the three-decision
// control signal for this turn, having optionally used its native tools inline.
//
// ctx  : { owner, anthropic, model, env, order, transcript, nowStr, contact, hasQuotedAudio, quoted,
//          catalog, tags, media, audioTranscript }
// turn : { labeledTranscript?: string, readback?: { result, said }, stateBlock?: string }
//   - readback present -> a read-back turn (the model reading a dispatch's result back); it may NOT
//     execute again (buildReadbackUser says so) and carries NO media.
//
// -> { say, keepListening, execute, lang, pendingNeed, degraded }
//    Nothing here is trusted: server.js runs the extracted payload through the plain-code gate
//    (lib/inputs.js) before any skill sees it, and enforces the caps on keepListening/execute.
//    Throws propagate to server.js's turn-loop try/catch (today's behaviour).
export async function route(ctx, turn = {}) {
  const {
    owner,
    anthropic,
    model,
    env = {},
    order,
    transcript,
    nowStr,
    contact,
    hasQuotedAudio,
    quoted,
    catalog,
    tags,
    media,
    audioTranscript,
  } = ctx;
  const valid = new Set((catalog || []).map((c) => c.id));

  // The B1 data path: the model reads the LABELLED transcript, not ctx.transcript.
  const convo = turn.labeledTranscript ?? transcript;
  const stateBlock = turn.stateBlock || "";

  const system = buildRouterSystem(owner, catalog || [], tags || []);
  const user = turn.readback
    ? buildReadbackUser(owner, {
        result: turn.readback.result,
        said: turn.readback.said,
        transcript: convo,
        nowStr,
        contact,
        stateBlock,
      })
    : buildRouterUser(owner, {
        order,
        transcript: convo,
        hasQuotedAudio,
        hasQuotedCalendarLink: !!quoted?.calendarLink,
        nowStr,
        contact,
        quotedText: quoted?.text || null,
        hasMedia: !!media,
        stateBlock,
        audioTranscript: audioTranscript || null,
      });

  // media present AND not a read-back -> attach the N file blocks and pin the vision model. A
  // read-back re-reads a dispatch result and carries NO file (Edge 15).
  const mediaBlocks = media && !turn.readback ? media.blocks : null;
  const content = withMediaContent(user, mediaBlocks);
  const useModel = mediaBlocks ? media.model : model;
  const userMsg = { role: "user", content };

  const timeoutMs = Number(env.NATIVE_ANSWER_TIMEOUT_MS) || NATIVE_TURN_TIMEOUT_MS_DEFAULT;
  const maxHops = Number(env.NATIVE_MAX_TOOL_HOPS) || NATIVE_MAX_TOOL_HOPS_DEFAULT;
  const tools = buildNativeTools(env); // [] when NATIVE_TOOLS is off -> a tool-less schema call

  const createParams = {
    model: useModel,
    max_tokens: TURN_MAX_TOKENS,
    system,
    messages: [userMsg],
    tools,
    output_config: jsonFormat(TURN_DECISION_SCHEMA),
    thinking: { type: "adaptive" },
  };

  let msg = await anthropic.messages.create(createParams, { timeout: timeoutMs });
  // pause_turn resume loop: resend [user, assistant(content)] until finished or the hop cap is hit
  // (the documented server-tool resume — no "Continue." text). Bounded by NATIVE_MAX_TOOL_HOPS.
  let hops = 0;
  while (msg?.stop_reason === "pause_turn" && hops < maxHops) {
    hops++;
    msg = await anthropic.messages.create(
      { ...createParams, messages: [userMsg, { role: "assistant", content: msg.content }] },
      { timeout: timeoutMs }
    );
  }
  // Still paused after the cap -> treat as unparseable and degrade (never runs away).
  if (msg?.stop_reason === "pause_turn") return degradeEnvelope();

  const parsed = readReply(msg, "turn"); // load-bearing brace-scan fallback lives inside readReply
  if (!parsed) return degradeEnvelope();

  // Normalize the envelope. keepListening biases to STAY OPEN (default true). execute -> the valid
  // catalog ids (a bare string is wrapped; null/absent -> []).
  const say = typeof parsed.say === "string" ? parsed.say : null;
  const keepListening = typeof parsed.keepListening === "boolean" ? parsed.keepListening : true;
  const rawExecute = Array.isArray(parsed.execute)
    ? parsed.execute
    : typeof parsed.execute === "string"
    ? [parsed.execute]
    : [];
  const execute = rawExecute.filter((s) => valid.has(s));
  const lang =
    typeof parsed.lang === "string" && parsed.lang.trim() ? parsed.lang.trim().toLowerCase() : "en";
  const pendingNeed = typeof parsed.pendingNeed === "string" ? parsed.pendingNeed : null;

  return { say, keepListening, execute, lang, pendingNeed, degraded: false };
}

// THE EXTRACTION CALL (Phase 2 of an execute). A second messages.create carrying output_config
// (buildExecuteSchema, derived shape-only from the declaration) + adaptive thinking, NO tools, and
// (like route) the media / audio transcript. Never throws: returns the parsed payload object, or
// null on a refusal/unparseable reply (-> checkPayload fail -> a fresh extract() with `problems`).
//
// turn : { labeledTranscript?, primary: string, spec: <manifest.inputs>, stateBlock?: string,
//          problems?: string }
//   problems: the describeProblems(...) prose from a FAILED checkPayload on the PRIOR extract()
//   pass — present ONLY on a repair re-extraction; rendered by buildExtractionUser as an explicit
//   "fix exactly these problems" block. Absent/null on the first pass.
export async function extract(ctx, turn = {}) {
  const { owner, anthropic, model, env = {}, transcript, nowStr, contact, catalog, tags, media, audioTranscript } = ctx;
  const { primary, spec, problems = null } = turn;
  const convo = turn.labeledTranscript ?? transcript;

  const system = buildRouterSystem(owner, catalog || [], tags || []);
  const user = buildExtractionUser(owner, {
    primary,
    transcript: convo,
    nowStr,
    contact,
    stateBlock: turn.stateBlock || "",
    hasMedia: !!media,
    audioTranscript: audioTranscript || null,
    problems,
  });

  const mediaBlocks = media ? media.blocks : null;
  const content = withMediaContent(user, mediaBlocks);
  const useModel = mediaBlocks ? media.model : model;
  const timeoutMs = Number(env.NATIVE_ANSWER_TIMEOUT_MS) || NATIVE_TURN_TIMEOUT_MS_DEFAULT;

  try {
    const msg = await anthropic.messages.create(
      {
        model: useModel,
        max_tokens: TURN_MAX_TOKENS,
        system,
        messages: [{ role: "user", content }],
        output_config: jsonFormat(buildExecuteSchema(spec)),
        thinking: { type: "adaptive" },
      },
      { timeout: timeoutMs }
    );
    return readReply(msg, "extract");
  } catch (e) {
    console.error("extract: call failed:", e?.message || e);
    return null;
  }
}
