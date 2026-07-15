// ============================================================================
//  legacy/inputs.js  —  FROZEN. Verbatim copy of lib/inputs.js as it was at HEAD (commit
//  before card 55e00052). Pure (no imports). Provides describeInputs() + checkPayload() to
//  the legacy (@assistant / OLD) path ONLY, so the OLD router prompt renders exactly as it
//  did at HEAD — no CONVERSATION line, no scalar-`of` — and the OLD payload gate validates
//  exactly as it did. The NEW flow uses the live lib/inputs.js. Do NOT edit.
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
//  and validates whatever came back AGAINST that declaration. That is what lets the merged
//  call ask for a skill's inputs without the orchestrator importing that skill's schema —
//  and the reply format is demanded in the PROMPT, not via output_config, for exactly the
//  same reason: an output_config would need the schema, and the orchestrator would then have
//  to know what each skill is. It must not. (scripts/turn-latency-selftest.mjs T2.6 lints it.)
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
      const sub = Object.entries(f.of)
        .map(
          ([k, v]) =>
            `${k}: ${v.type === "enum" ? JSON.stringify(v.enum) : v.type}${v.nullable ? "|null" : ""}`
        )
        .join(", ");
      t = `array of {${sub}}`;
    }
    lines.push(
      `${indent}${name}: ${t}${f.nullable ? "|null" : ""}${f.desc ? `   // ${f.desc}` : ""}`
    );
  }
  return lines.join("\n");
}

// One skill's declared inputs, as prompt text. A skill with no inputs says so out loud —
// "(no inputs)" is an answer, and it stops the model inventing a payload for it.
function describeSkill(spec) {
  if (!spec || !spec.fields || !Object.keys(spec.fields).length)
    return `        (no inputs — this skill reads the conversation itself)`;
  const req = Object.entries(spec.requiredWhen || {})
    .filter(([, v]) => Array.isArray(v) && v.length)
    .map(([k, v]) => `${spec.discriminator}="${k}" requires ${v.join(" + ")}`)
    .join("; ");
  return describeFields(spec.fields) + (req ? `\n        REQUIRED TO ACT: ${req}` : "");
}

// The orchestrator's view of the catalog, as two blocks of OPAQUE TEXT:
//   .tasks     — the skill menu, each entry followed by the inputs that skill declared
//   .rulebooks — each skill's own extraction rules, verbatim, exactly as it wrote them
// Carrying the rulebooks matters and it is nearly free: input tokens are cheap, output tokens
// are the clock. A lean prompt without them measurably DROPS people from terse orders, and a
// dropped attendee is a person who is silently never invited.
// catalog: [{ id, description, inputs }] — built by server.js loadSkills().
export function describeInputs(catalog) {
  const list = (catalog || [])
    .map(
      (t) =>
        `  - "${t.id}": ${t.description}\n` +
        `      INPUTS (fill these into "info" if you pick this skill):\n${describeSkill(t.inputs)}`
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
      val.forEach((item, n) => {
        if (item === null || typeof item !== "object") {
          problems.push(`${at}[${n}]: not an object`);
          return;
        }
        for (const [k, sub] of Object.entries(f.of || {}))
          checkType(item[k], sub, `${at}[${n}].${k}`, problems);
      });
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
