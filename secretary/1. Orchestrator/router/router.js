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
import { buildRouterSystem, buildRouterUser } from "./prompt.js";

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

// ctx: { owner, anthropic, model, order, transcript, nowStr, contact, hasQuotedAudio,
//        quoted, catalog }
// -> { tasks: string[], lang: string, info: object | null }
//    `info` is the FIRST task's declared inputs, as the model filled them. It is NOT trusted
//    here: server.js runs it through the plain-code gate (lib/inputs.js) before any skill sees
//    it. This function's job is to return what came back, not to judge it.
export async function route(ctx) {
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
  } = ctx;
  const valid = new Set([...(catalog || []).map((c) => c.id), "other"]);

  const system = buildRouterSystem(owner, catalog || []);
  const user = buildRouterUser(owner, {
    order,
    transcript,
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
    parsed = parseJsonReply(out);
  }

  let tasks = Array.isArray(parsed?.tasks) ? parsed.tasks : [];
  tasks = tasks.filter((t) => valid.has(t));
  if (!tasks.length) tasks = ["other"];

  // Normalize the language to a lowercase code; default English when absent/odd.
  const lang =
    typeof parsed?.lang === "string" && parsed.lang.trim()
      ? parsed.lang.trim().toLowerCase()
      : "en";

  return { tasks, lang, info: parsed?.info ?? null };
}
