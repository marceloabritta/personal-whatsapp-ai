#!/usr/bin/env node
// ============================================================================
//  Self-test for the extraction-schema generator's handling of a NULLABLE ENUM
//  (lib/inputs.js `buildExecuteSchema` / `schemaForField`).
//
//  The bug this exists to prevent (card e405206c, "New Mary flow failing at
//  calendar task"): `schemaForField` rendered a nullable enum as
//      { type: ["string","null"], enum: ["window","next", null] }
//  — a `type` keyword sitting BESIDE an enum whose values include `null`. The
//  Anthropic structured-output (`output_config` json_schema) validator REJECTS
//  that shape with HTTP 400, so EVERY calendar_action extraction 400'd and the
//  turn gave up with a content-free apology (no calendar event created).
//
//  The correct encoding, proven live on 2026-07-31, is: a nullable enum carries
//  NO `type` keyword at all — { enum: ["window","next", null] } — while null
//  stays legal through the VALUES. A non-nullable enum keeps its single scalar
//  type; a nullable ARRAY (union type, no enum) is unchanged.
//
//  Load-bearing invariants:
//    a. a nullable enum property has NO `type` keyword                 <-- the regression
//    b. its `enum` still carries null in the values
//    c. minimality — the fix did NOT over-broaden:
//         - a NON-nullable enum keeps { type:"string", enum:[...] }
//         - a nullable ARRAY keeps type ["array","null"], no enum
//
//  PATH B — the SECOND, independent defect in the SAME calendar schema (card
//  e405206c, full fix): the `recurrence` field is declared { type:"object",
//  nullable:true } with its structure living only in the field's prose. The
//  generator emits it as a BARE { type:["object","null"] } — no `properties`,
//  no `additionalProperties`. Strict structured-output REJECTS that with HTTP
//  400: "For 'object' type, 'additionalProperties' must be explicitly set to
//  false". The chosen cure (recurring events must actually work) is to render
//  `recurrence` as a PROPER STRUCTURED object: a nullable object type carrying
//  `additionalProperties:false`, a non-empty `properties`, and a non-empty
//  `required` set drawn from those properties (with `freq` — the mandatory
//  discriminator from the prose — present). Assertions e1–e5 below encode that
//  bar; e2–e5 FAIL today (bare object) and flip green once the object path is
//  taught to emit structure. They are ROBUST to the implementer's exact
//  data-model choice: they assert structural validity, not a hard-coded
//  field-name list (only `freq` is pinned — the prose makes it mandatory).
//
//  The core assertions are OFFLINE and deterministic: the 400 is a property of
//  the schema SHAPE, and the shape is produced by deterministic code. An
//  optional, key-gated live guard sends the WHOLE generated schema to the real
//  API and asserts it is NOT rejected with a 400 — this whole-schema acceptance
//  is the TRUE definition of done; skipped cleanly when no key is present.
//
//  Run offline:  node scripts/nullable-enum-schema-selftest.mjs
//  Run + live guard:
//    ANTHROPIC_API_KEY=$PROJECT_ANTHROPIC_API_KEY node scripts/nullable-enum-schema-selftest.mjs
// ============================================================================
import { buildExecuteSchema } from "../secretary/1. Orchestrator/lib/inputs.js";
import { manifest } from "../secretary/3. Mary Skills/1. Calendar Actions/skill.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

const props = buildExecuteSchema(manifest.inputs).properties;

// --- a + b: the nullable enum (`list_mode`, enum:["window","next"], nullable) ---
check(
  "a. nullable enum carries NO `type` keyword  <-- the 400 regression",
  props.list_mode.type === undefined
);

check(
  "b. nullable enum retains null in its values (enum === [window, next, null])",
  JSON.stringify(props.list_mode.enum) === '["window","next",null]'
);

// --- c: minimality guards — the fix must not over-broaden ---------------------
// A NON-nullable enum (`action`) keeps its single scalar type AND its enum.
check(
  "c1. non-nullable enum keeps a single scalar `type:\"string\"`",
  props.action.type === "string"
);
check(
  "c2. non-nullable enum still carries its `enum` values",
  Array.isArray(props.action.enum) && props.action.enum.length > 0
);
// A nullable ARRAY (`participants`) is a union type with NO enum — unchanged.
check(
  "c3. nullable array keeps union `type:[\"array\",\"null\"]` (unchanged)",
  JSON.stringify(props.participants.type) === '["array","null"]'
);

// --- PATH B: structured `recurrence` (nullable object) shape -----------------
// The nullable-OBJECT field must be a PROPER structured object, not a bare
// { type:["object","null"] } — strict structured-output 400s on a typed object
// with no explicit `additionalProperties:false`. Robust structural assertions:
// they pin `freq` (the prose's mandatory discriminator) but NOT the full
// subfield list — the exact fields (interval/byday/count/until) are the
// implementer's data-model call from the field's prose.
const rec = props.recurrence;
const recTypes = Array.isArray(rec?.type) ? rec.type : [rec?.type];
check(
  "e1. nullable object `recurrence` type includes both \"object\" and \"null\"",
  recTypes.includes("object") && recTypes.includes("null")
);
check(
  "e2. `recurrence.additionalProperties === false`  <-- the path-B 400",
  rec?.additionalProperties === false
);
check(
  "e3. `recurrence.properties` is a non-empty object",
  !!rec?.properties &&
    typeof rec.properties === "object" &&
    !Array.isArray(rec.properties) &&
    Object.keys(rec.properties).length > 0
);
check(
  "e4. `recurrence.required` is a non-empty array, every entry a key in properties",
  Array.isArray(rec?.required) &&
    rec.required.length > 0 &&
    rec.required.every((k) => !!rec.properties && k in rec.properties)
);
check(
  "e5. `recurrence.properties` includes at least `freq` (the mandatory discriminator)",
  !!rec?.properties && rec.properties.freq !== undefined
);

// --- optional, key-gated live guard ------------------------------------------
// Sends the WHOLE calendar extraction schema to the real API exactly as the
// product's extract() does — model claude-sonnet-5, output_config json_schema —
// and asserts the call is NOT rejected with an HTTP 400. This whole-schema
// acceptance is the TRUE definition of done for the card. Key from either
// PROJECT_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY; skipped when neither is set.
const LIVE_KEY = process.env.PROJECT_ANTHROPIC_API_KEY || process.env.ANTHROPIC_API_KEY;
if (LIVE_KEY) {
  try {
    const { createRequire } = await import("node:module");
    const { pathToFileURL } = await import("node:url");
    // Resolve the SDK from secretary/node_modules (the scripts/ dir has none).
    const requireFromSecretary = createRequire(
      new URL("../secretary/package.json", import.meta.url)
    );
    const sdkPath = requireFromSecretary.resolve("@anthropic-ai/sdk");
    const sdkMod = await import(pathToFileURL(sdkPath).href);
    const Anthropic = sdkMod.default?.default || sdkMod.default || sdkMod;

    const anthropic = new Anthropic({ apiKey: LIVE_KEY });
    const schema = buildExecuteSchema(manifest.inputs); // the WHOLE schema, not a reduced one

    let live400 = false;
    try {
      await anthropic.messages.create({
        model: "claude-sonnet-5",
        max_tokens: 1024,
        system: "Return a JSON object matching the schema.",
        messages: [
          { role: "user", content: "List what is on my calendar tomorrow." },
        ],
        output_config: { format: { type: "json_schema", schema } },
      });
    } catch (e) {
      if (e && (e.status === 400 || e.statusCode === 400)) {
        live400 = true;
        console.log(`      live 400 detail: ${e.message || e}`);
      } else {
        // A non-400 error (network/timeout/etc.) is not what this guard tests.
        console.log(`      live guard: non-400 error, ignoring: ${e?.message || e}`);
      }
    }
    check("d. live: WHOLE calendar extraction schema is NOT rejected with HTTP 400", !live400);
  } catch (e) {
    console.log(`      live guard setup failed, skipping: ${e?.message || e}`);
  }
} else {
  console.log("  skip (no key)  d. live whole-schema-acceptance guard");
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
