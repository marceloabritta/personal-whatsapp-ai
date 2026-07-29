// ============================================================================
//  router/router.js  —  ROUTER + EXTRACTOR LOGIC.
//  Calls Claude ONCE with the merged prompt and returns the list of tasks (validated
//  against the catalog of skills discovered by the orchestrator), the conversation
//  language, and the FIRST task's declared inputs as an `info` payload.
//
//  There is NO output_config on this call: the reply format is demanded in the prompt, which
//  is what keeps the orchestrator generic (see router/prompt.js). Two consequences:
//    - parseJsonReply below is now LOAD-BEARING, not a fallback. ~4% of merged replies leak a
//      line of prose before the JSON and are recovered by its balanced-brace scan. Do not
//      remove it and do not "simplify" it.
//    - nothing but the prompt enforces the shape any more. An unparseable/refused reply degrades
//      to { next:"done", skills:[], degraded:true }; server.js fires the "I didn't understand"
//      menu AND the unrouted self-learning report ONLY on that `degraded` flag — the schema-drift
//      alarm. A legitimate empty close (the model deliberately ending chit-chat / a no-op) is
//      degraded:false and closes silently.
// ============================================================================
import { APIConnectionTimeoutError } from "@anthropic-ai/sdk";
import {
  buildRouterSystem,
  buildRouterUser,
  buildReadbackUser,
  buildRepairUser,
  buildAnswerSystem,
  buildAnswerUser,
} from "./prompt.js";
import { buildNativeTools } from "../lib/nativeTools.js";
import { readText } from "../lib/llm.js";

// Robustly pull a JSON object out of an LLM reply. Extracts the FIRST balanced {...},
// tolerating ```json fences and stray prose. Returns the object or null.
function parseJsonReply(out) {
  if (!out) return null;
  let s = String(out).trim();
  const fenced = s.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  if (fenced) s = fenced[1].trim();
  try {
    return JSON.parse(s);
  } catch {
    /* fall through to balanced-brace scan */
  }
  const start = s.indexOf("{");
  if (start < 0) return null;
  let depth = 0,
    inStr = false,
    esc = false;
  for (let i = start; i < s.length; i++) {
    const c = s[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
    } else if (c === '"') inStr = true;
    else if (c === "{") depth++;
    else if (c === "}" && --depth === 0) {
      try {
        return JSON.parse(s.slice(start, i + 1));
      } catch {
        return null;
      }
    }
  }
  return null;
}

// ONE turn of the orchestrator's conversation. Given the whole conversation (labelled, so the
// model can tell HER words from HIS) it returns the control signal for this turn.
//
// ctx  : { owner, anthropic, model, order, transcript, nowStr, contact, hasQuotedAudio, quoted,
//          catalog, tags }
// turn : { labeledTranscript: string, readback?: { result: string, said: string|null } }
//   - labeledTranscript is the model's view of the conversation for EVERY turn (the OWNER/
//     SECRETARY/CONTACT rendering — buildLabeledTranscript, built in server.js). ctx.transcript
//     (the unlabelled ME:/OTHER: string) is NOT read on this path and NOT mutated, so the six
//     unconverted skills' own extractors see today's exact bytes.
//   - readback present -> this is a read-back turn (the model reading a dispatch's result back);
//     it may NOT execute again (buildReadbackUser says so).
//   - repair present -> the last payload failed validation; the model must re-emit a CORRECTED
//     execute (buildRepairUser invites it). readback and repair are mutually exclusive.
//
// -> { say: string|null, next: "listen"|"execute"|"done", skills: string[], info: object|null,
//      lang: string, awaitFrom: string|null }
//    Nothing here is trusted: server.js runs `info` through the plain-code gate (lib/inputs.js)
//    before any skill sees it, and enforces the caps/write-invariant on `next`.
export async function route(ctx, turn = {}) {
  const {
    owner,
    anthropic,
    model,
    order,
    transcript,
    nowStr,
    contact,
    hasQuotedAudio,
    quoted,
    catalog,
    tags,
    media,
  } = ctx;
  const valid = new Set([...(catalog || []).map((c) => c.id), "other"]);

  // The B1 data path: the model reads the LABELLED transcript, not ctx.transcript. Fall back to
  // ctx.transcript only if a caller forgot to pass it — never the normal path.
  const convo = turn.labeledTranscript ?? transcript;

  const system = buildRouterSystem(owner, catalog || [], tags || []);
  const user = turn.repair
    ? // a REPAIR turn: the last payload failed validation. Same system prompt, but a user
      // message that INVITES a corrected execute (the write invariant forbids it on a read-back,
      // NOT on a repair — see server.js). `turn.repair` is the describeProblems prose.
      buildRepairUser(owner, {
        problems: turn.repair,
        transcript: convo,
        nowStr,
        contact,
      })
    : turn.readback
    ? buildReadbackUser(owner, {
        result: turn.readback.result,
        said: turn.readback.said,
        transcript: convo,
        nowStr,
        contact,
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
      });

  // media present AND not a read-back -> attach the N file blocks and pin the vision model.
  // A read-back re-reads a dispatch result and carries NO file (Edge 15); a REPAIR does
  // (turn.repair is not turn.readback), so a corrected re-read of a receipt still sees it.
  const mediaBlocks = media && !turn.readback ? media.blocks : null;
  // NIT (a): never emit an empty/whitespace text block beside media (the Messages API can 400).
  // `user` is the buildRouterUser render and is never empty in practice — but guard explicitly:
  // with media, include the text block only when `user` has content; otherwise send media-only.
  const content = mediaBlocks
    ? user && user.trim()
      ? [...mediaBlocks, { type: "text", text: user }]
      : [...mediaBlocks]
    : user; // no media -> byte-identical string (Decision 8)
  // NIT (c): pin the vision model by reading media.model HERE — the only place the create call's
  // model is chosen. No media -> byte-identical ctx.model.
  const useModel = mediaBlocks ? media.model : model;

  // 1024, not 200: the reply now carries a payload as well as a classification (measured
  // median output: 169 tokens). With thinking disabled the budget can no longer be eaten by
  // reasoning, which is what makes this safe — and is why the thinking fix ships first.
  const msg = await anthropic.messages.create({
    model: useModel,
    max_tokens: 1024,
    system,
    messages: [{ role: "user", content }],
  });

  let parsed = null;
  if (msg?.stop_reason === "refusal") {
    console.error("router: model refused the request");
  } else {
    const out = (msg?.content || [])
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    console.log("ROUTER RAW:", out);
    parsed = parseJsonReply(out); // load-bearing brace-scan; do not remove (see header).
  }

  // A refusal or unparseable reply must NOT loop — degrade to a silent close and let server.js's
  // caller decide (its "I didn't understand" path treats an empty result as not-understood).
  if (!parsed)
    return { say: null, next: "done", skills: [], info: null, lang: "en", awaitFrom: null, degraded: true };

  const next = ["listen", "execute", "done", "answer"].includes(parsed.next) ? parsed.next : "done";

  let skills = Array.isArray(parsed.skills) ? parsed.skills.filter((s) => valid.has(s)) : [];
  // An execute that names no valid skill degrades to ["other"] — server.js's existing
  // "I didn't understand" path (an unrouted report + a reply).
  if (next === "execute" && !skills.length) skills = ["other"];

  const say = typeof parsed.say === "string" ? parsed.say : null;

  // Normalize the language to a lowercase code; default English when absent/odd.
  const lang =
    typeof parsed.lang === "string" && parsed.lang.trim()
      ? parsed.lang.trim().toLowerCase()
      : "en";

  const awaitFrom = typeof parsed.awaitFrom === "string" ? parsed.awaitFrom : null;

  return { say, next, skills, info: parsed.info ?? null, lang, awaitFrom, degraded: false };
}

// ============================================================================
//  THE ANSWER PASS  —  a SECOND, tool-carrying prose call, distinct from route().
//  route() classifies (JSON, no tools). When it returns next="answer", server.js calls answer()
//  which attaches the native toolset (buildNativeTools) and asks Claude to answer the question in
//  PROSE — no JSON contract to fail, so an answer turn can NEVER reach the "didn't understand" menu.
//
//  Server-side tools run an internal sampling loop: when it hits its iteration cap the reply comes
//  back with stop_reason "pause_turn" and MUST be resumed by resending [user, assistant(content)]
//  (the API resumes automatically off the trailing server_tool_use block — no "Continue." text).
//  We bound that resume loop with NATIVE_MAX_TOOL_HOPS and each create() with NATIVE_ANSWER_TIMEOUT_MS.
//
//  ctx  : { owner, anthropic, model, env, order, transcript, nowStr, contact, lang, media }
//  turn : { labeledTranscript?: string }
//  -> { text: string|null, lang: string, hops: number,
//       outcome: "ok" | "timeout" | "tool_error" | "empty" | "refusal" }
//  NEVER throws: a thrown create is caught and classified (timeout -> "timeout", else "tool_error").
// ============================================================================
const NATIVE_ANSWER_TIMEOUT_MS_DEFAULT = 30000;
const NATIVE_MAX_TOOL_HOPS_DEFAULT = 4;

// Detect the production timeout. The SDK throws APIConnectionTimeoutError when a create()'s
// { timeout } elapses; its `.name` may be left as "Error", so match by instanceof / constructor,
// NOT a name==="APITimeoutError" string (that class does not exist here).
function isTimeoutError(e) {
  if (!e) return false;
  if (e instanceof APIConnectionTimeoutError) return true;
  const name = e.name || e.constructor?.name || "";
  return /timeout/i.test(name) || /timed out/i.test(e.message || "");
}

// A web_search_tool_result / web_fetch_tool_result block whose `content` is an error object
// (not an array of results) means the tool failed. A successful result carries an ARRAY.
function hasToolError(msg) {
  for (const b of msg?.content || []) {
    if (b.type === "web_search_tool_result" || b.type === "web_fetch_tool_result") {
      const c = b.content;
      if (c == null) return true;
      if (!Array.isArray(c) && typeof c === "object") return true; // an error object
      if (Array.isArray(c) && c.length === 0) return true;
    }
  }
  return false;
}

export async function answer(ctx, turn = {}) {
  const { owner, anthropic, model, env = {}, order, transcript, nowStr, contact, media } = ctx;
  const lang =
    typeof ctx.lang === "string" && ctx.lang.trim() ? ctx.lang.trim().toLowerCase() : "en";
  const timeoutMs = Number(env.NATIVE_ANSWER_TIMEOUT_MS) || NATIVE_ANSWER_TIMEOUT_MS_DEFAULT;
  const maxHops = Number(env.NATIVE_MAX_TOOL_HOPS) || NATIVE_MAX_TOOL_HOPS_DEFAULT;
  const tools = buildNativeTools(env);

  const system = buildAnswerSystem(owner, lang);
  const user = buildAnswerUser(owner, {
    order,
    transcript: turn.labeledTranscript ?? transcript,
    nowStr,
    contact,
    hasMedia: !!media,
  });

  // Media present -> attach the file blocks and pin the vision model, mirroring route()'s handling.
  const mediaBlocks = media ? media.blocks : null;
  const content = mediaBlocks
    ? user && user.trim()
      ? [...mediaBlocks, { type: "text", text: user }]
      : [...mediaBlocks]
    : user;
  const useModel = mediaBlocks ? media.model : model;
  const userMsg = { role: "user", content };

  let hops = 0;
  let msg;
  try {
    msg = await anthropic.messages.create(
      { model: useModel, max_tokens: 2048, system, messages: [userMsg], tools },
      { timeout: timeoutMs }
    );
    // pause_turn resume loop: resend [user, assistant(content)] until finished or the hop cap is hit.
    while (msg?.stop_reason === "pause_turn" && hops < maxHops) {
      hops++;
      msg = await anthropic.messages.create(
        {
          model: useModel,
          max_tokens: 2048,
          system,
          messages: [userMsg, { role: "assistant", content: msg.content }],
          tools,
        },
        { timeout: timeoutMs }
      );
    }
  } catch (e) {
    if (isTimeoutError(e)) return { text: null, lang, hops, outcome: "timeout" };
    console.error("answer: tool-carrying call failed:", e?.message || e);
    return { text: null, lang, hops, outcome: "tool_error" };
  }

  // Still paused after the hop cap -> treat as a timeout (bounded, never runs away).
  if (msg?.stop_reason === "pause_turn") return { text: null, lang, hops, outcome: "timeout" };
  // A model refusal stays SILENT downstream (server.js does NOT send the tool-error notice on it),
  // matching the classification refusal path (router.js: refusal -> silent close).
  if (msg?.stop_reason === "refusal") return { text: null, lang, hops, outcome: "refusal" };

  const text = readText(msg);
  // A tool that errored, or no usable prose at all, is a tool_error (server.js sends the notice).
  if (hasToolError(msg) || !text) return { text: null, lang, hops, outcome: "tool_error" };

  return { text, lang, hops, outcome: "ok" };
}
