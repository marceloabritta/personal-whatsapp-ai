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
//    - nothing but the prompt enforces the shape any more. An unparseable reply degrades to
//      tasks:["other"], which server.js already answers with "I didn't understand" AND a
//      self-learning report. That existing path is the schema-drift alarm.
// ============================================================================
import {
  buildRouterSystem,
  buildRouterUser,
  buildReadbackUser,
  buildRepairUser,
} from "./prompt.js";

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
      });

  // 1024, not 200: the reply now carries a payload as well as a classification (measured
  // median output: 169 tokens). With thinking disabled the budget can no longer be eaten by
  // reasoning, which is what makes this safe — and is why the thinking fix ships first.
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 1024,
    system,
    messages: [{ role: "user", content: user }],
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
    return { say: null, next: "done", skills: [], info: null, lang: "en", awaitFrom: null };

  const next = ["listen", "execute", "done"].includes(parsed.next) ? parsed.next : "done";

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

  return { say, next, skills, info: parsed.info ?? null, lang, awaitFrom };
}
