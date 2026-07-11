// ============================================================================
//  router/router.js  —  ROUTER LOGIC.
//  Calls Claude with the classification prompt and returns the list of tasks,
//  validated against the catalog of skills discovered by the orchestrator.
// ============================================================================
import { buildRouterSystem, buildRouterUser, ROUTER_SCHEMA } from "./prompt.js";

// Robustly pull a JSON object out of an LLM reply (fallback for the rare case the
// API doesn't honor output_config — refusal, or a model without structured-output
// support). Extracts the FIRST balanced {...}. Returns the object or null.
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

// ctx: { owner, anthropic, model, order, transcript, hasQuotedAudio, catalog }
// -> { tasks: string[], lang: string, reason: string }
export async function route(ctx) {
  const {
    owner,
    anthropic,
    model,
    order,
    transcript,
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
  });

  const msg = await anthropic.messages.create({
    model,
    max_tokens: 200,
    system,
    output_config: { format: { type: "json_schema", schema: ROUTER_SCHEMA } },
    messages: [{ role: "user", content: user }],
  });

  // Structured outputs guarantee schema-valid JSON; parseJsonReply is the fallback
  // for a refusal (empty/partial) or a model swapped to one without support.
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

  return { tasks, lang, reason: parsed?.reason || "" };
}
