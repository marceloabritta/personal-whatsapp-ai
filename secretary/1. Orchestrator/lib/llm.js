// ============================================================================
//  Structured-output helpers — shared by every skill that asks Claude for JSON.
//  Was copy-pasted verbatim into calendar_action, task_action and feature_request;
//  lives here now so there is ONE copy to fix.
//
//  Usage (unchanged from the inlined version):
//    const msg = await anthropic.messages.create({
//      model, max_tokens, system,
//      output_config: jsonFormat(MY_SCHEMA),
//      messages: [{ role: "user", content: user }],
//    });
//    const data = readReply(msg, "calendar");   // parsed object | null
// ============================================================================

// Wrap a JSON Schema in the output_config.format shape, so the API returns ONLY
// schema-valid JSON (no fences, no prose, no shape drift).
export function jsonFormat(schema) {
  return { format: { type: "json_schema", schema } };
}

// Read a schema-enforced reply. `who` only labels the log line. Returns the parsed
// object, or null on a refusal / unparseable reply (never throws) — callers treat
// null as "I didn't understand" and say so.
// A null parse is almost always truncation (stop_reason "max_tokens"), so log the
// cause + size: a future failure is diagnosable instead of a silent null.
export function readReply(msg, who = "llm") {
  if (msg?.stop_reason === "refusal") {
    console.error(`${who}: model refused the request`);
    return null;
  }
  const out = readText(msg);
  const parsed = parseJsonReply(out);
  if (!parsed) {
    console.error(
      `${who}: unparseable reply (stop_reason=${msg?.stop_reason}, chars=${out.length})`
    );
  }
  return parsed;
}

// Read the raw text blocks (for a prose reply — NOT JSON).
export function readText(msg) {
  return (msg?.content || [])
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("")
    .trim();
}

// Pull the FIRST balanced {...} out of an LLM reply. Tolerates ```json fences and
// stray prose, and — unlike a greedy /\{[\s\S]*\}/ match (first "{" to LAST "}",
// which corrupts on any trailing brace) — extracts the FIRST balanced object.
// Returns the parsed object, or null if nothing valid is found (never throws).
// With output_config this is normally a straight JSON.parse; the scan is the
// fallback for a model/SDK without structured-output support.
export function parseJsonReply(out) {
  if (!out) return null;
  let s = String(out).trim();
  const fenced = s.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  if (fenced) s = fenced[1].trim();
  try {
    return JSON.parse(s); // happy path: reply is exactly the JSON object
  } catch {
    /* fall through to the balanced-brace scan */
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
