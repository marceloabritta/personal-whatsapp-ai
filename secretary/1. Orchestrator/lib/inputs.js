// ============================================================================
//  lib/inputs.js  —  THE DECLARED-INPUTS CONTRACT. Generic, and it stays generic.
//
//  Each skill DECLARES the inputs it needs (manifest.inputs). The router asks for them in
//  the SAME call that classifies the order, so a turn takes ONE round-trip instead of two.
//  This module is the two halves of that:
//
//    describeInputs(catalog)     -> the prompt text the router shows the model
//    checkPayload(inputs, info)  -> PLAIN CODE, no AI: is the returned payload usable?
//
//  ⚠ IT KNOWS ABOUT *DECLARATIONS*, NEVER ABOUT SKILLS.
//  It never names a field of any skill. It renders whatever a skill declared as opaque text,
//  and validates whatever came back AGAINST that declaration. That is what lets the two-phase
//  call ask for a skill's inputs without the orchestrator importing that skill's schema.
//  The EXTRACTION call now DOES carry output_config — but its schema is derived SHAPE-ONLY from
//  the declaration by buildExecuteSchema (below): it maps the declared types/enums/nullability
//  to a JSON Schema without ever naming a skill or importing a skill's own schema. The invariant
//  the old "no output_config" rule protected — the orchestrator never knows what a calendar IS —
//  is preserved by that shape-only derivation.
//
//  THE DECLARATION (data + skill-owned plain-code predicates):
//    {
//      discriminator: "<field>" | null,   // the field whose VALUE selects the required set
//      fields: { <name>: { type, nullable?, enum?, of?, desc? } },
//      requiredWhen: { <discriminator value>: ["<field>", "<field>[].<sub>"] },
//      consistency: [{ name, test(info) -> boolean }],   // the skill's own rules
//      rulebook: () => "<the skill's extraction rules, verbatim>",
//    }
//  type: string | number | bool | enum | iso | email | array
//
//  THE THREE TIERS, and the difference between them is LOAD-BEARING:
//    1. VALIDITY     (`shapeOk`) — is it an object, are the DECLARED fields present, are the
//                    types right, are there no unexpected fields? THIS TIER ALONE decides
//                    whether the payload is handed to the skill (server.js).
//    2. COMPLETENESS — for the discriminator's value, is every requiredWhen field filled?
//    3. CONSISTENCY  — the skill's own predicates.
//  `ok` = all three. `shapeOk` = validity only.
//
//  Why handover is gated on validity ALONE: a shape-valid but INCOMPLETE payload is still
//  handed over, because the skill's own clarification pass fills the gaps exactly as it does
//  today. Only a shape-INVALID payload is withheld — and then the skill falls back to its own
//  extraction call, which is today's path, unchanged. So the worst case of the merge is
//  "correct but slow", NEVER "fast and wrong".
//
//  And note what a MISSING declared field means: INVALID. Not "null". A declared field that
//  is null is fine; a declared field that is ABSENT means the model was never asked for it —
//  which is what happens when a skill adds a field to its schema and forgets the declaration.
//  Coercing that to a default would silently un-ship the feature. Refusing the payload merely
//  costs a round-trip. That asymmetry is the whole safety net.
// ============================================================================

const ISO_RE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?([+-]\d{2}:\d{2}|Z)?$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ---- the prompt half ---------------------------------------------------------
// Renders ONE declared field as a line the model can read. `desc` is not decoration — it IS
// the prompt the model extracts from, and it arrives verbatim from the skill.
function describeFields(fields, indent = "        ") {
  const lines = [];
  for (const [name, f] of Object.entries(fields)) {
    let t = f.type;
    if (f.type === "enum") t = `one of ${JSON.stringify(f.enum)}`;
    if (f.type === "array" && f.of) {
      // A SCALAR element spec (`of: { type: "string" }`) renders "array of <type>"; an OBJECT
      // element spec (`of: { name: {...}, email: {...} }`) keeps the "array of {sub}" shape.
      // Both halves of this — here and checkType's `case "array"` — ship together or neither,
      // or a scalar spec renders "array of {type: undefined}". See checkType below.
      if (typeof f.of?.type === "string") {
        t = `array of ${f.of.type === "enum" ? JSON.stringify(f.of.enum) : f.of.type}`;
      } else {
        const sub = Object.entries(f.of)
          .map(
            ([k, v]) =>
              `${k}: ${v.type === "enum" ? JSON.stringify(v.enum) : v.type}${v.nullable ? "|null" : ""}`
          )
          .join(", ");
        t = `array of {${sub}}`;
      }
    }
    lines.push(
      `${indent}${name}: ${t}${f.nullable ? "|null" : ""}${f.desc ? `   // ${f.desc}` : ""}`
    );
  }
  return lines.join("\n");
}

// WHO runs the conversation for a skill, as prompt text. Rendered opaquely, exactly like the
// INPUTS block: the orchestrator reads one field with two values and never interprets a skill.
// "skill" — the skill asks/confirms for itself (today's shape, the default). "orchestrator" —
// the model runs the dialogue; the skill just acts and returns.
const CONVERSATION_COPY = {
  skill:
    "this skill talks to him ITSELF. It will ask its own questions and get its own " +
    "confirmation before it writes anything. Do NOT propose or ask before you dispatch it — " +
    "you would be asking him the same thing twice. Hand it the order and let it talk.",
  orchestrator:
    "YOU talk to him for this skill. It does not ask and it does not confirm — it just acts. " +
    "So before you dispatch it for anything irreversible, propose what you are about to do and " +
    "get his agreement first.",
};

// One skill's declared inputs, as prompt text. A skill with no inputs says so out loud —
// "(no inputs)" is an answer, and it stops the model inventing a payload for it. `conversation`
// (opaque, two-valued; absent -> "skill") is appended as its own line so the model knows whether
// dispatching the skill IS asking the owner or whether it must ask first.
function describeSkill(spec, conversation) {
  const conv =
    conversation === "orchestrator" ? CONVERSATION_COPY.orchestrator : CONVERSATION_COPY.skill;
  const convLine = `\n      CONVERSATION: ${conv}`;
  if (!spec || !spec.fields || !Object.keys(spec.fields).length)
    return `        (no inputs — this skill reads the conversation itself)${convLine}`;
  const req = Object.entries(spec.requiredWhen || {})
    .filter(([, v]) => Array.isArray(v) && v.length)
    .map(([k, v]) => `${spec.discriminator}="${k}" requires ${v.join(" + ")}`)
    .join("; ");
  return describeFields(spec.fields) + (req ? `\n        REQUIRED TO ACT: ${req}` : "") + convLine;
}

// The orchestrator's view of the catalog, as two blocks of OPAQUE TEXT:
//   .tasks     — the skill menu, each entry followed by the inputs that skill declared
//   .rulebooks — each skill's own extraction rules, verbatim, exactly as it wrote them
// Carrying the rulebooks matters and it is nearly free: input tokens are cheap, output tokens
// are the clock. A lean prompt without them measurably DROPS people from terse orders, and a
// dropped attendee is a person who is silently never invited.
// catalog: [{ id, description, inputs, conversation }] — built by server.js loadSkills().
export function describeInputs(catalog) {
  const list = (catalog || [])
    .map(
      (t) =>
        `  - "${t.id}": ${t.description}\n` +
        `      INPUTS (fill these into "info" if you pick this skill):\n${describeSkill(t.inputs, t.conversation)}`
    )
    .join("\n");

  const books = (catalog || [])
    .map((t) => {
      let text = "";
      try {
        text = typeof t.inputs?.rulebook === "function" ? t.inputs.rulebook() : "";
      } catch {
        text = ""; // a skill whose rulebook throws must not take the router down with it
      }
      if (!text) return "";
      return `
============ EXTRACTION RULES for "${t.id}" (that skill's own rulebook) ==========
${text}
===============================================================================`;
    })
    .filter(Boolean)
    .join("\n");

  return { tasks: list, rulebooks: books };
}

// ---- the extraction-schema half (shape-only) ---------------------------------
// Map a declared scalar TYPE to its JSON-Schema base type. iso/email/enum are all strings at the
// wire level (their extra constraints are enforced by checkPayload, not the schema).
const SCALAR_BASE = {
  string: "string",
  iso: "string",
  email: "string",
  enum: "string",
  number: "number",
  bool: "boolean",
  object: "object",
};

// One declared field -> its JSON-Schema fragment. `nullable` becomes a `[..., "null"]` type
// union; an enum keeps its values (with `null` appended when nullable, so null stays legal); an
// array's items come from `of` — a scalar `of` maps to that scalar, an object `of` to an object
// schema whose subfields are required. Recurses for nested/object element specs. Shape ONLY —
// it never reads a field's `desc`, `requiredWhen` or `consistency`; those stay in checkPayload.
function schemaForField(f) {
  if (f.type === "array") {
    let items;
    if (f.of && typeof f.of.type === "string") {
      items = schemaForField(f.of); // a SCALAR element spec
    } else {
      const sub = f.of || {};
      const properties = {};
      for (const [k, v] of Object.entries(sub)) properties[k] = schemaForField(v);
      items = {
        type: "object",
        additionalProperties: false,
        required: Object.keys(sub),
        properties,
      };
    }
    return { type: f.nullable ? ["array", "null"] : "array", items };
  }
  const base = SCALAR_BASE[f.type] || "string";
  const out = { type: f.nullable ? [base, "null"] : base };
  if (f.type === "enum" && Array.isArray(f.enum)) {
    out.enum = f.nullable ? [...f.enum, null] : [...f.enum];
  }
  return out;
}

// buildExecuteSchema(spec) — the SHAPE-ONLY generator that derives a skill's extraction JSON
// Schema from its manifest.inputs declaration. `required` lists EVERY declared field (checkPayload
// treats an ABSENT declared field as invalid, so the schema demands them all — a null-but-present
// value is fine, an omitted one is not). additionalProperties:false so the model cannot leak a
// field. NO per-skill knowledge enters here: it reads only the declaration's generic shape
// (type/enum/nullable/of), never a skill's own schema (inputs.js:11 invariant preserved).
export function buildExecuteSchema(spec) {
  const fields = (spec && spec.fields) || {};
  const properties = {};
  for (const [name, f] of Object.entries(fields)) properties[name] = schemaForField(f);
  return {
    type: "object",
    additionalProperties: false,
    required: Object.keys(fields),
    properties,
  };
}

// ---- the plain-code half -----------------------------------------------------
function checkType(val, f, at, problems) {
  if (val == null) {
    if (!f.nullable && f.type !== "array") problems.push(`${at}: null but not nullable`);
    return;
  }
  switch (f.type) {
    case "enum":
      if (!(f.enum || []).includes(val))
        problems.push(`${at}: "${val}" not in ${JSON.stringify(f.enum)}`);
      break;
    case "iso":
      if (typeof val !== "string" || !ISO_RE.test(val) || Number.isNaN(Date.parse(val)))
        problems.push(`${at}: "${val}" is not an ISO-8601 datetime`);
      break;
    case "email":
      if (typeof val !== "string" || !EMAIL_RE.test(val))
        problems.push(`${at}: "${val}" is not an email`);
      break;
    case "number":
      if (typeof val !== "number" || Number.isNaN(val)) problems.push(`${at}: not a number`);
      break;
    case "bool":
      if (typeof val !== "boolean") problems.push(`${at}: not a boolean`);
      break;
    case "string":
      if (typeof val !== "string") problems.push(`${at}: not a string`);
      break;
    case "array":
      if (!Array.isArray(val)) {
        problems.push(`${at}: not an array`);
        break;
      }
      // A SCALAR element spec (`of: { type: "string" }`) checks each element as that scalar —
      // a null element then fails via the "null but not nullable" branch above, and enum/email
      // element types come for free. An OBJECT element spec keeps the per-key sub-check. This is
      // the second half of the scalar-`of` support; see describeFields above.
      if (typeof f.of?.type === "string") {
        val.forEach((item, n) => checkType(item, f.of, `${at}[${n}]`, problems));
      } else {
        val.forEach((item, n) => {
          if (item === null || typeof item !== "object") {
            problems.push(`${at}[${n}]: not an object`);
            return;
          }
          for (const [k, sub] of Object.entries(f.of || {}))
            checkType(item[k], sub, `${at}[${n}].${k}`, problems);
        });
      }
      break;
  }
}

// -> { shapeOk, ok, problems[] }
//   shapeOk : VALIDITY only. server.js hands the payload to the skill iff this is true.
//   ok      : validity AND completeness AND consistency.
// A skill with NO declaration gets shapeOk=false: there is no contract to check the payload
// against, so nothing may be handed over and the skill extracts for itself. That is also what
// happens on a dual-intent turn whose first task declares nothing.
export function checkPayload(spec, info) {
  const problems = [];
  if (!spec || !spec.fields || !Object.keys(spec.fields).length)
    return { shapeOk: false, ok: false, problems: ["no declared inputs for this task"] };
  if (info == null || typeof info !== "object" || Array.isArray(info))
    return { shapeOk: false, ok: false, problems: ["info is not an object"] };

  // 1. VALIDITY. A DECLARED field that is absent is invalid — see the header.
  for (const [name, f] of Object.entries(spec.fields)) {
    if (!(name in info)) {
      problems.push(`${name}: missing`);
      continue;
    }
    checkType(info[name], f, name, problems);
  }
  for (const k of Object.keys(info))
    if (!(k in spec.fields)) problems.push(`${k}: unexpected field`);
  const shapeOk = problems.length === 0;

  // 2. COMPLETENESS — the required set for the discriminator's current value.
  // A path of the form `x[].y` means "every element of x that EXISTS has a y". An EMPTY x is
  // COMPLETE, not missing: a create with zero guests is an ordinary event (commit 9eead61 —
  // a required field is only legitimate if a truthful answer can satisfy it, and "nobody"
  // could not satisfy a >=1 rule). Do not reinstate an emptiness check here.
  const disc = spec.discriminator ? info[spec.discriminator] : null;
  const required = (disc && spec.requiredWhen?.[disc]) || [];
  for (const at of required) {
    const m = at.match(/^(\w+)\[\]\.(\w+)$/);
    if (m) {
      const arr = info[m[1]];
      if (Array.isArray(arr))
        arr.forEach((it, n) => {
          const v = it?.[m[2]];
          if (v == null || String(v).trim() === "")
            problems.push(`${m[1]}[${n}].${m[2]}: required, missing`);
        });
    } else if (info[at] == null || String(info[at]).trim() === "") {
      problems.push(`${at}: required, missing`);
    }
  }

  // 3. CONSISTENCY — the skill's own predicates. A throwing predicate FAILS; it never
  // escapes into the request path.
  for (const rule of spec.consistency || []) {
    let good;
    try {
      good = !!rule.test(info);
    } catch {
      good = false;
    }
    if (!good) problems.push(`inconsistent: ${rule.name}`);
  }

  return { shapeOk, ok: problems.length === 0, problems };
}

// Render checkPayload().problems back into a short prose block the repair-loop turn shows the
// model, so it can fix the payload it just sent. PURE — no AI, names no skill; it renders the
// same generic problem strings checkPayload already produced.
export function describeProblems(problems) {
  const lines = (problems || []).map((p) => `- ${p}`).join("\n");
  return `Your last attempt could not be used — it failed validation:\n${lines || "- (no detail)"}\nFix these and try again, or ask him for what you are missing.`;
}
