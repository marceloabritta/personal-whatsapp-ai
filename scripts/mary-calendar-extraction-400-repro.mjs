// ============================================================================
//  THROWAWAY REPRO — card e405206c "New Mary flow failing at calendar task".
//
//  Reproduces the production symptom: the @mary extraction call to the Anthropic
//  API returns HTTP 400
//    output_config.format.schema: Invalid schema: Enum value 'window' does not
//    match declared type '['string', 'null']'
//  which makes the calendar create silently fail (hasSay:false, no event).
//
//  The 400 is produced by an output_config JSON schema in which a NULLABLE ENUM
//  field is rendered as  { enum: [...], type: ["string","null"] }  — a union
//  `type` array beside `enum`, which the API's structured-output validator rejects.
//  The offending field is the calendar skill's `list_mode` (enum ["window","next"],
//  nullable:true) at "3. Mary Skills/1. Calendar Actions/skill.js:92-97".
//
//  This drives the API the way lib/confirm.js does — same SDK (loaded from
//  secretary/node_modules), same model default (claude-sonnet-5), same
//  output_config wrapper (lib/llm.js jsonFormat) — and prints the verbatim error.
//
//  Run:  ANTHROPIC_API_KEY=$PROJECT_ANTHROPIC_API_KEY node scripts/mary-calendar-extraction-400-repro.mjs
// ============================================================================
import { createRequire } from "node:module";

// The SDK lives in secretary/node_modules — same trick as calendar-extraction-livetest.mjs.
const require = createRequire(new URL("../secretary/package.json", import.meta.url));
const Anthropic = require("@anthropic-ai/sdk").default ?? require("@anthropic-ai/sdk");

// The calendar skill's REAL declared inputs (proves list_mode's enum/nullable are not hand-typed).
const { manifest } = await import(
  new URL("../secretary/3. Mary Skills/1. Calendar Actions/skill.js", import.meta.url).href
);

const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";
const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

// The output_config wrapper, byte-identical to lib/llm.js jsonFormat().
const jsonFormat = (schema) => ({ format: { type: "json_schema", schema } });

// Render one declared field as a JSON-schema property. A NULLABLE field becomes a
// `type` UNION array — the shape whose presence beside `enum` the production error
// reveals. (This is the minimal converter needed to reproduce the 400; it is NOT a
// claim about how the deployed build builds its schema — see REPLICATION.md.)
function toProp(f) {
  const base =
    f.type === "bool" ? "boolean" : f.type === "number" ? "number" : f.type === "array" ? "array" : "string";
  const prop = { type: f.nullable ? [base, "null"] : base };
  if (f.type === "enum") prop.enum = f.enum;
  if (f.desc) prop.description = f.desc;
  return prop;
}

function schemaFromInputs(inputs) {
  const properties = {};
  for (const [name, f] of Object.entries(inputs.fields)) properties[name] = toProp(f);
  return { type: "object", additionalProperties: false, properties };
}

async function callWith(label, schema) {
  console.log(`\n===== ${label} =====`);
  console.log("schema.properties.list_mode:", JSON.stringify(schema.properties.list_mode));
  try {
    const msg = await anthropic.messages.create({
      model: MODEL,
      max_tokens: 256,
      system: "Extract the fields as JSON.",
      output_config: jsonFormat(schema),
      messages: [
        { role: "user", content: "agende churrasco na casa do avelino amanhã, rua copaíba 44" },
      ],
    });
    console.log("NO ERROR — call succeeded. stop_reason:", msg?.stop_reason);
  } catch (e) {
    console.log("HTTP status:", e?.status);
    // The raw API body verbatim, exactly as the orchestrator's `extract: call failed` line logs it.
    console.log("API error body (verbatim):", e?.error ? JSON.stringify(e.error) : e?.message);
    console.log("request_id:", e?.request_id ?? e?.headers?.["request-id"] ?? "(n/a)");
  }
}

if (!process.env.ANTHROPIC_API_KEY) {
  console.error("ANTHROPIC_API_KEY is not set — this makes a LIVE model call.");
  process.exit(2);
}

// ---- Repro A: MINIMAL — the single nullable enum field, alone. ---------------
await callWith("A: minimal — single nullable enum (list_mode)", {
  type: "object",
  additionalProperties: false,
  properties: { list_mode: { type: ["string", "null"], enum: ["window", "next"] } },
});

// ---- Repro B: the FULL calendar-skill inputs, derived from the real manifest. -
await callWith("B: full calendar skill inputs (derived from manifest)", schemaFromInputs(manifest.inputs));

// ---- Control: the SAME enum WITHOUT null in the type — should NOT 400. --------
await callWith("C: control — same enum, non-nullable (type:'string')", {
  type: "object",
  additionalProperties: false,
  properties: { list_mode: { type: "string", enum: ["window", "next"] } },
});
