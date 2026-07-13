#!/usr/bin/env node
// ============================================================================
//  THE ACCURACY BAR for card 9af6967a — "Calendar skill taking too long to reply".
//
//  ⚠ THIS IS THE HALF THAT CAN SINK THE CARD, AND IT IS NOT OPTIONAL.
//  The fix makes the assistant faster by (1) switching off the model's private thinking and
//  (2) merging the router call into the extraction call. Thinking tokens are DISCARDED by the
//  product — but the reasoning may still have been improving the answer. If we make the
//  assistant faster and DUMBER, we have shipped a worse product, and a latency chart would
//  never show it. This script is the detector.
//
//  ⚠ IT COSTS REAL MONEY (a live model call per case per run per arm) AND IT IS THE HUMAN'S
//  CALL TO RUN. No worker runs it on its own initiative (CONVENTIONS §5).
//
//  IT CANNOT MESSAGE ANYONE. Pure model calls: no Google, no WhatsApp, no Evolution, no
//  event, no session, no attendee, no invite. It reads prompts and asserts on the JSON that
//  comes back. That is the entire blast radius.
//
//  THE THREE ARMS, built byte-identically to production from the product's OWN modules:
//    PROD     the calendar extraction call as it ships today — output_config + CAL_SCHEMA,
//             and NO `thinking` parameter (so the model thinks, adaptively, by default).
//    NOTHINK  the same call, with thinking: {type:"disabled"}.             <- STEP 1
//    MERGED   ONE call: the router's own system prompt, carrying every skill's declared
//             inputs, with NO output_config — the reply format is demanded in the PROMPT.
//             Returns {tasks, lang, info}.                                  <- STEP 2
//
//  🛑 THE STOP RULE — pre-declared, and not negotiable at build time.
//  If NOTHINK or MERGED fails even ONE case that PROD passes: DO NOT SHIP. Record the case
//  verbatim and report it. A slower-but-correct assistant beats a fast wrong one — buying
//  speed with accuracy is not a fix, it is a different, quieter bug.
//  A case BOTH arms fail is the separate wrong-recipient card, not this one: record, move on.
//  If ONLY the merged arm fails, step 1 is still shippable alone. Step 2 is NOT shippable
//  alone under any circumstances (thinking-on + merged measured a p90 of 41.5s).
//
//  WHY THE EXPECTATIONS ARE WRITTEN DOWN AND NOT "arm B agrees with arm A":
//  the baseline has its own known bug, and agreement alone would inherit it. See C3.
//
//  🔴 C3 — DO NOT PIN THE BUG INTO THE TEST.
//  On C3, production today attaches the CONTACT'S OWN email to a DIFFERENT person (Sávio),
//  7 times out of 8, and the calendar skill emails invites to participants. That is a live
//  WRONG-RECIPIENT bug and IT BELONGS TO A DIFFERENT CARD — do not fix it here. What matters
//  here is that the CORRECT answer is `email: null` (ask him), and that is what is asserted,
//  in every arm. This card's fix incidentally improves it (measured 7/8 wrong today -> 1/8
//  after step 1 -> 0/8 after step 2). Claim no more than that. The other card stays open.
//
//  Run:
//    EXTRACTION_DRYRUN=1 node scripts/calendar-extraction-livetest.mjs
//        OFFLINE and FREE. Builds all three arms from the product's real modules and asserts
//        each is CONSTRUCTIBLE, without calling the API. Today the MERGED arm is not
//        constructible (lib/inputs.js does not exist; no skill declares manifest.inputs) —
//        which is exactly the red this file is supposed to show before the fix lands.
//    ANTHROPIC_API_KEY=… node scripts/calendar-extraction-livetest.mjs
//        THE REAL RUN. Costs money. RUNS=3 per case per arm (the model is probabilistic and a
//        single sample proves nothing). ARMS=PROD,NOTHINK,MERGED to select arms.
// ============================================================================
import { createRequire } from "node:module";
import { readdir } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const DRYRUN = process.env.EXTRACTION_DRYRUN === "1";
const RUNS = Number(process.env.RUNS || 3);
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5"; // mirrors server.js
const TRANSLATE_MODEL = process.env.TRANSLATE_MODEL || "claude-haiku-4-5-20251001";
const OWNER = process.env.OWNER_NAME || "Marcelo";
const ARMS = (process.env.ARMS || "PROD,NOTHINK,MERGED").split(",").map((s) => s.trim());

// Pinned "now" so every date assertion is deterministic. Same format server.js builds
// (toLocaleString "en-US", weekday long, 2-digit month/day/hour/minute, America/Sao_Paulo).
//   Monday 13 Jul 2026 ->  tomorrow = Tue 2026-07-14 · "quarta" = Wed 2026-07-15
//                          next week Mon = 2026-07-20
const NOW_STR = "Monday, 07/13/2026, 09:41 AM";

const SKILLS_DIR = fileURLToPath(new URL("../secretary/2. Skills/", import.meta.url));
const CAL_DIR = fileURLToPath(new URL("../secretary/2. Skills/1. Calendar Actions/", import.meta.url));

let failures = 0;
const check = (name, cond) => {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
  return cond;
};

// ---- the product's own modules. No copies, no forks: if the product changes, so does this.
const CAL_PROMPT = await import(new URL("prompt.js", pathToFileURL(CAL_DIR)).href);
const CAL_SKILL = await import(new URL("skill.js", pathToFileURL(CAL_DIR)).href);
const ROUTER_PROMPT = await import(
  new URL("../secretary/1. Orchestrator/router/prompt.js", import.meta.url).href
);
const LLM = await import(new URL("../secretary/1. Orchestrator/lib/llm.js", import.meta.url).href);
const INPUTS = await import(
  new URL("../secretary/1. Orchestrator/lib/inputs.js", import.meta.url).href
).catch((e) => ({ __err: e.code === "ERR_MODULE_NOT_FOUND" ? "lib/inputs.js does not exist" : e.message }));

// The catalog, discovered exactly as server.js loadSkills() does — INCLUDING `inputs`, which
// is what step 2 adds. Building it here from the real skills (rather than hand-writing it) is
// what makes the MERGED arm the production prompt and not an approximation of it.
async function loadCatalog() {
  const catalog = [];
  for (const e of await readdir(SKILLS_DIR, { withFileTypes: true })) {
    if (!e.isDirectory()) continue;
    try {
      const mod = await import(pathToFileURL(path.join(SKILLS_DIR, e.name, "skill.js")).href);
      if (!mod.manifest?.id) continue;
      catalog.push({
        id: mod.manifest.id,
        description: mod.manifest.description || "",
        inputs: mod.manifest.inputs || null,
      });
    } catch {
      /* a skill that will not import is not this script's problem */
    }
  }
  return catalog;
}
const CATALOG = await loadCatalog();

// ============================================================================
//  THE CASES. The owner's REAL orders, verbatim, with written-down expectations.
//  Transcripts are faithful reconstructions of the real chats (the originals were an
//  experiment's scratch file and are gone); every fact they carry that an assertion leans on
//  is named in the case's `note`.
//
//  Load-bearing fields only: action, participants[].name/.email, start_iso (the DAY),
//  duration_min, all_day, all_day_end_iso, list_mode, lang. `title`/`summary` are free text —
//  substring at most, never equality.
// ============================================================================
const day = (iso) => (iso ? String(iso).slice(0, 10) : null); // the DAY, not the timestamp
const hour = (iso) => (iso ? Number(String(iso).slice(11, 13)) : null);
const names = (info) => (info?.participants || []).map((p) => String(p?.name || "").toLowerCase());
const emailOf = (info, who) =>
  // `?? null`, NOT `?? undefined`: every expectation below compares `=== null`, and
  // `undefined === null` is false — so `undefined` here made the "no address was invented"
  // assertions unpassable on a CORRECT product. That assertion is this card's headline safety
  // property; it must be able to go green.
  (info?.participants || []).find((p) => String(p?.name || "").toLowerCase().includes(who))?.email ?? null;
const weekdayOf = (iso) =>
  iso ? new Date(`${day(iso)}T12:00:00-03:00`).toLocaleString("en-US", { weekday: "long", timeZone: "America/Sao_Paulo" }) : null;
const daysBetween = (a, b) =>
  Math.round((Date.parse(`${day(b)}T12:00:00Z`) - Date.parse(`${day(a)}T12:00:00Z`)) / 86400000);

const CASES = [
  {
    id: "C1",
    order: "agendar amanha o dia inteiro biopsia laura",
    contact: "Laura",
    transcript: ["OTHER: oi Marcelo, tudo bem?", "ME: tudo! vou marcar a biopsia"].join("\n"),
    note: "REAL, terse ALL-DAY create. Laura's email is NOT in the chat — she must not be dropped, and no address may be invented for her.",
    // 🔴 STRICT. This is the case that guards commit 6c76dab (all-day events). Its old
    // expectation asserted only create + Laura, so the STOP rule was blind to the exact
    // regression this card is most likely to cause: step 2 un-shipping all-day. Do NOT
    // weaken it back. Assert the DAY of start_iso, never the timestamp — draftFromInfo /
    // createFromDraft only ever read the day out of it on an all-day event (dayOfIso), so
    // pinning T00:00 would go red on a harmless T09:00. Do NOT assert duration_min: it is
    // ignored when all_day is true.
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["Laura is a participant (never silently dropped)", names(info).some((n) => n.includes("laura"))],
      ["Laura's email is null — no address was invented", emailOf(info, "laura") === null],
      ["all_day === true  (STRICT — guards 6c76dab)", info?.all_day === true],
      ["all_day_end_iso === null (a single day)", info?.all_day_end_iso == null],
      ["start_iso's DAY is tomorrow, 2026-07-14", day(info?.start_iso) === "2026-07-14"],
    ],
  },
  {
    id: "C7",
    order: "agendar o dia inteiro de segunda a quarta o offsite",
    contact: null,
    transcript: "",
    note: "NEW. A multi-day all-day RANGE. The end day is INCLUSIVE: for 'segunda a quarta' it is WEDNESDAY, not Thursday. An off-by-one here READS RIGHT AND IS WRONG — it books an extra day onto the owner's calendar and nothing downstream would object.",
    // 🔴 STRICT on the inclusive end day. Asserted as a SPAN and as weekdays rather than as
    // absolute dates: "de segunda a quarta" on a Monday is legitimately readable as this week
    // or next, and that ambiguity is not what this case is testing. The off-by-one is.
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["all_day === true  (STRICT)", info?.all_day === true],
      ["all_day_end_iso is set (it is a RANGE)", !!info?.all_day_end_iso],
      ["start_iso's day is a MONDAY", weekdayOf(info?.start_iso) === "Monday"],
      [
        "all_day_end_iso is a WEDNESDAY — INCLUSIVE, not Thursday (STRICT)",
        weekdayOf(info?.all_day_end_iso) === "Wednesday",
      ],
      [
        "the span is exactly 2 days (Mon->Wed inclusive), not 3",
        info?.start_iso && info?.all_day_end_iso && daysBetween(info.start_iso, info.all_day_end_iso) === 2,
      ],
    ],
  },
  {
    id: "C2",
    order: "marque uma reuniao com o savio amanha",
    contact: "Domingos",
    transcript: ["OTHER: bom dia Marcelo", "ME: bom dia! preciso falar com o savio"].join("\n"),
    note: "REAL. create; no time and no email anywhere in the chat.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["Sávio is a participant", names(info).some((n) => n.includes("savio") || n.includes("sávio"))],
      [
        "Sávio's email is null — it is not in the chat, and a null is ALWAYS better than a guess",
        (emailOf(info, "savio") ?? emailOf(info, "sávio")) === null,
      ],
    ],
  },
  {
    id: "C3",
    order: "agendar",
    contact: "Domingos",
    // The real chat, reconstructed. Every fact an assertion below leans on is here:
    //   - the meeting is with SÁVIO, tomorrow at 15h  (so the create is complete on those)
    //   - domingos.carissimo@gmail.com is the CONTACT'S OWN address, posted by the CONTACT
    //   - Marcelo says SÁVIO will send his address himself => Sávio's email is NOT KNOWN
    transcript: [
      "ME: preciso marcar uma reuniao com o savio",
      "OTHER: pode ser amanha as 15 horas?",
      "ME: perfeito, 15 horas amanha",
      "OTHER: meu email e domingos.carissimo@gmail.com",
      "ME: obrigado. o email o savio vai enviar",
      "OTHER: combinado",
    ].join("\n"),
    note: "REAL, the BARE order. 🔴 The correct answer for Sávio's email is NULL. domingos.carissimo@gmail.com is the CONTACT'S address and attaching it to Sávio invites the WRONG PERSON — a live bug, and a DIFFERENT card. Do NOT write the expectation to match today's output.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["Sávio is a participant", names(info).some((n) => n.includes("savio") || n.includes("sávio"))],
      ["start_iso's day is tomorrow, 2026-07-14", day(info?.start_iso) === "2026-07-14"],
      ["start_iso's hour is 15h", hour(info?.start_iso) === 15],
      // 🔴 THE ONE THAT MATTERS. Not "what production does" — what is TRUE.
      [
        "Sávio's email is NULL — the contact's own address is NOT Sávio's",
        (emailOf(info, "savio") ?? emailOf(info, "sávio")) === null,
      ],
      [
        "domingos.carissimo@gmail.com is attached to NOBODY (wrong-recipient guard)",
        !(info?.participants || []).some((p) => /domingos\.carissimo@gmail\.com/i.test(String(p?.email || ""))),
      ],
    ],
  },
  {
    id: "C4",
    order: "maque na minha agenda para amanha 16hrs pegar cachorros",
    contact: null,
    transcript: "",
    note: "REAL, typo'd, SOLO event — nobody to invite. The NEGATIVE CONTROL for C1: it proves the model is not simply saying 'all day' to everything. A TIME was given, so it is a timed event.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["participants is EMPTY — a zero-guest create is complete, and must not be gated", (info?.participants || []).length === 0],
      ["start_iso's day is tomorrow, 2026-07-14", day(info?.start_iso) === "2026-07-14"],
      ["start_iso's hour is 16h", hour(info?.start_iso) === 16],
      // TRUTHINESS, not === false. CAL_SCHEMA permits null here and draftFromInfo coerces with
      // `!!info.all_day`, so `false` and `null` are BEHAVIOURALLY IDENTICAL. A strict === false
      // would go red on a model that returned null — a NON-regression — and, worse, could trip
      // the STOP rule and park step 2 for nothing. C1/C7 stay strict; the negative cases do not.
      ["NOT all-day (a TIME was given) — truthiness, since null and false are identical to the product", !info?.all_day],
    ],
  },
  {
    id: "C5",
    order: "como esta a agenda pra quarta?",
    contact: "Marina",
    transcript: ["OTHER: oi! conseguimos falar essa semana?"].join("\n"),
    note: "REAL. A read-only LIST, pt.",
    expect: (info) => [
      ["action = list", info?.action === "list"],
      ['list_mode = "window" (a bounded span, not a forward scan)', info?.list_mode === "window"],
      ["the window starts on Wednesday 2026-07-15", day(info?.range_start_iso) === "2026-07-15"],
    ],
  },
  {
    id: "C6",
    order: "how is my calendar next week?",
    contact: null,
    transcript: "",
    lang: "en", // asserted on the MERGED arm only — see langNote below
    note: "REAL. A read-only LIST, en. The `lang` half is the ROUTER's job, so it exists only in the merged reply.",
    expect: (info) => [
      ["action = list", info?.action === "list"],
      ['list_mode = "window"', info?.list_mode === "window"],
      ["the window starts on Monday 2026-07-20 (next week)", day(info?.range_start_iso) === "2026-07-20"],
    ],
  },

  // ---- R1-R7: the reproduction's own orders (REPLICATION.md). Posted into the owner's
  //      self-chat with no relevant history, so the transcript is genuinely empty.
  {
    id: "R1",
    order: "agendar uma reuniao com a Laura", // S1 — measured 19s live
    contact: null,
    transcript: "",
    note: "REPLICATION S1. INCOMPLETE on purpose: no date, no email. Both gaps must be reported as nulls, not invented.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["Laura is a participant", names(info).some((n) => n.includes("laura"))],
      ["Laura's email is null (it is nowhere in the chat)", emailOf(info, "laura") === null],
      ["start_iso is null — no date was given, and a null is better than a guess", info?.start_iso == null],
    ],
  },
  {
    id: "R2",
    order: "agendar amanha as 15h uma call com o Pedro", // S2
    contact: null,
    transcript: "",
    note: "REPLICATION S2. A time, but no email.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["Pedro is a participant", names(info).some((n) => n.includes("pedro"))],
      ["Pedro's email is null", emailOf(info, "pedro") === null],
      ["start_iso's day is tomorrow, 2026-07-14", day(info?.start_iso) === "2026-07-14"],
      ["start_iso's hour is 15h", hour(info?.start_iso) === 15],
      ["NOT all-day (truthiness)", !info?.all_day],
    ],
  },
  {
    id: "R3",
    order:
      "agendar amanha as 16h uma call de 30 minutos com o Pedro Teste, email pedro.teste@example.com", // S3
    contact: null,
    transcript: "",
    note: "REPLICATION S3. COMPLETE — everything is in the order. This is the shape measured at 18s with only TWO calls: the one that proves the third call is not the whole bug.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["Pedro Teste is a participant", names(info).some((n) => n.includes("pedro"))],
      ["and his email is the one he gave", emailOf(info, "pedro") === "pedro.teste@example.com"],
      ["start_iso's day is tomorrow, 2026-07-14", day(info?.start_iso) === "2026-07-14"],
      ["start_iso's hour is 16h", hour(info?.start_iso) === 16],
      ["duration_min = 30", info?.duration_min === 30],
      ["NOT all-day (truthiness)", !info?.all_day],
    ],
  },
  {
    id: "R4",
    order: "como esta minha agenda amanha?", // S4
    contact: null,
    transcript: "",
    note: "REPLICATION S4. A list.",
    expect: (info) => [
      ["action = list", info?.action === "list"],
      ['list_mode = "window"', info?.list_mode === "window"],
      ["the window starts tomorrow, 2026-07-14", day(info?.range_start_iso) === "2026-07-14"],
    ],
  },
  {
    id: "R5",
    order: "agendar cafe com a Marina semana que vem", // S6
    contact: null,
    transcript: "",
    note: "REPLICATION S6. A vague date ('next week') — the model may pick a day; what must NOT happen is an invented email.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["Marina is a participant", names(info).some((n) => n.includes("marina"))],
      ["Marina's email is null", emailOf(info, "marina") === null],
    ],
  },
  {
    id: "R6",
    order: "marcar consulta com o Joao na sexta", // S8
    contact: null,
    transcript: "",
    note: "REPLICATION S8.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["João is a participant", names(info).some((n) => n.includes("joao") || n.includes("joão"))],
      ["his email is null", (emailOf(info, "joao") ?? emailOf(info, "joão")) === null],
      ["start_iso's day is a Friday", weekdayOf(info?.start_iso) === "Friday"],
    ],
  },
  {
    id: "R7",
    order:
      "agendar amanha as 10h uma reuniao com a Laura (laura@example.com) e o Pedro (pedro.teste@example.com)",
    contact: null,
    transcript: "",
    note: "REPLICATION's hard TWO-PARTICIPANT order. Two people, two addresses, and neither may be swapped onto the other — a swap here emails the wrong person.",
    expect: (info) => [
      ["action = create", info?.action === "create"],
      ["exactly TWO participants", (info?.participants || []).length === 2],
      ["Laura carries LAURA's address (not Pedro's)", emailOf(info, "laura") === "laura@example.com"],
      ["Pedro carries PEDRO's address (not Laura's)", emailOf(info, "pedro") === "pedro.teste@example.com"],
      ["start_iso's day is tomorrow, 2026-07-14", day(info?.start_iso) === "2026-07-14"],
      ["start_iso's hour is 10h", hour(info?.start_iso) === 10],
    ],
  },
];

// ============================================================================
//  BUILDING THE ARMS from the product's real modules.
// ============================================================================
const userPromptFor = (c) =>
  CAL_PROMPT.buildUserPrompt(OWNER, {
    order: c.order,
    transcript: c.transcript,
    nowStr: NOW_STR,
    contact: c.contact,
    quoted: null,
  });

// PROD / NOTHINK — the calendar extraction call, exactly as interpret() makes it (skill.js).
// `thinking` is the ONLY difference between the two arms.
function extractionParams(arm, c) {
  const p = {
    model: MODEL,
    max_tokens: 4096,
    system: CAL_PROMPT.buildSystem(OWNER),
    output_config: LLM.jsonFormat(CAL_PROMPT.CAL_SCHEMA),
    messages: [{ role: "user", content: userPromptFor(c) }],
  };
  if (arm === "NOTHINK") p.thinking = { type: "disabled" };
  return p;
}

// MERGED — ONE call. The router's system prompt now carries every skill's declared inputs and
// demands the reply format IN THE PROMPT: there is NO output_config, which is precisely what
// keeps the orchestrator generic (it never imports a skill's schema, so it never learns what a
// calendar is). It cannot be built until step 2 lands.
function mergedParams(c) {
  return {
    model: MODEL,
    max_tokens: 1024,
    thinking: { type: "disabled" },
    system: ROUTER_PROMPT.buildRouterSystem(OWNER, CATALOG),
    messages: [
      {
        role: "user",
        content: ROUTER_PROMPT.buildRouterUser(OWNER, {
          order: c.order,
          transcript: c.transcript,
          hasQuotedAudio: false,
          hasQuotedCalendarLink: false,
          nowStr: NOW_STR,
          contact: c.contact,
          quotedText: null,
        }),
      },
    ],
  };
}

// ---- Is the MERGED arm CONSTRUCTIBLE? Five things must be true, and today none of them is.
// This is the offline, free, right-reason red: the arm cannot be built, so it cannot be run.
function mergedBuildable() {
  const problems = [];
  if (typeof INPUTS.describeInputs !== "function")
    problems.push(INPUTS.__err || "lib/inputs.js exports no describeInputs()");
  if (typeof INPUTS.checkPayload !== "function" && !INPUTS.__err)
    problems.push("lib/inputs.js exports no checkPayload()");
  const cal = CATALOG.find((c) => c.id === "calendar_action");
  if (!cal?.inputs) problems.push("the calendar skill declares no manifest.inputs");
  if (ROUTER_PROMPT.ROUTER_SCHEMA)
    problems.push("router/prompt.js still exports ROUTER_SCHEMA — the merged call sends no schema, so it must be deleted");

  let sys = "";
  let usr = "";
  try {
    sys = ROUTER_PROMPT.buildRouterSystem(OWNER, CATALOG);
    usr = mergedParams(CASES[0]).messages[0].content;
  } catch (e) {
    problems.push(`buildRouterSystem/buildRouterUser threw: ${e.message}`);
  }
  // The declared inputs must actually REACH the prompt — a declaration the model never sees is
  // a declaration that does nothing. `all_day_end_iso` is the canary: it is the field commit
  // 6c76dab added and the one this card nearly dropped.
  if (sys && !/all_day_end_iso/.test(sys))
    problems.push("the merged system prompt does not carry the declared calendar inputs (no `all_day_end_iso` in it)");
  if (sys && !/"info"/.test(sys) && !/\binfo\b/.test(sys))
    problems.push("the merged system prompt does not demand an `info` payload in its reply format");
  // Without nowStr there is no date arithmetic, and every date assertion above is unanswerable.
  if (usr && !usr.includes(NOW_STR))
    problems.push("buildRouterUser does not carry `nowStr` — the merged call cannot do date arithmetic");
  return problems;
}

// ============================================================================
//  DRY RUN — offline, free. Prove each arm is constructible from the real modules.
// ============================================================================
console.log(`\n=== arms: can they be built from the product's own modules? (nowStr pinned: ${NOW_STR}) ===\n`);

let prodOk = false;
try {
  const p = extractionParams("PROD", CASES[0]);
  prodOk =
    !!p.system &&
    p.output_config?.format?.schema === CAL_PROMPT.CAL_SCHEMA &&
    p.thinking === undefined &&
    p.messages[0].content.includes(NOW_STR);
} catch (e) {
  console.log(`        PROD threw: ${e.message}`);
}
check("ARM PROD     — today's extraction call: output_config + CAL_SCHEMA, and NO `thinking`", prodOk);

let nothinkOk = false;
try {
  const p = extractionParams("NOTHINK", CASES[0]);
  nothinkOk = p.thinking?.type === "disabled" && p.output_config?.format?.schema === CAL_PROMPT.CAL_SCHEMA;
} catch (e) {
  console.log(`        NOTHINK threw: ${e.message}`);
}
check('ARM NOTHINK  — the same call with thinking:{type:"disabled"}  [STEP 1]', nothinkOk);

const mergedProblems = mergedBuildable();
for (const p of mergedProblems) console.log(`        · ${p}`);
check(
  `ARM MERGED   — ONE call, format declared in the PROMPT, no output_config  [STEP 2]` +
    (mergedProblems.length ? `  (${mergedProblems.length} reason${mergedProblems.length === 1 ? "" : "s"} it cannot be built yet)` : ""),
  mergedProblems.length === 0
);

if (DRYRUN) {
  console.log(
    `\n${failures === 0 ? "PASS" : `FAIL (${failures})`} — DRY RUN: no API call was made, nothing was spent.\n` +
      (failures
        ? "The MERGED arm cannot be built yet. That is expected before step 2 lands, and it is\n" +
          "the reason this file is RED today. Nothing here is a syntax or wiring fault.\n"
        : "All three arms build. The live run is the human's call:\n" +
          "  ANTHROPIC_API_KEY=… node scripts/calendar-extraction-livetest.mjs\n")
  );
  process.exit(failures === 0 ? 0 : 1);
}

// ============================================================================
//  THE LIVE RUN. From here on it costs money.
// ============================================================================
if (!process.env.ANTHROPIC_API_KEY) {
  console.error(
    "\nANTHROPIC_API_KEY is not set — this script makes LIVE model calls and costs real money.\n" +
      "Build the arms offline and for free with:\n" +
      "  EXTRACTION_DRYRUN=1 node scripts/calendar-extraction-livetest.mjs\n"
  );
  process.exit(2);
}
if (mergedProblems.length && ARMS.includes("MERGED")) {
  console.error(
    "\nREFUSING to spend money: the MERGED arm cannot be built yet, so the STOP-rule comparison\n" +
      "it exists for is impossible. Land step 2 first, or select arms explicitly:\n" +
      "  ARMS=PROD,NOTHINK ANTHROPIC_API_KEY=… node scripts/calendar-extraction-livetest.mjs\n"
  );
  process.exit(2);
}

// A bare `import Anthropic` from scripts/ throws ERR_MODULE_NOT_FOUND — the SDK lives in
// secretary/node_modules. Same trick as scripts/tasks-addressed-selftest.mjs.
const require = createRequire(new URL("../secretary/package.json", import.meta.url));
const Anthropic = require("@anthropic-ai/sdk").default ?? require("@anthropic-ai/sdk");
const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const median = (xs) => (xs.length ? [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)] : 0);
const stats = {}; // arm -> { ms: [], think: [] }
const results = {}; // arm -> caseId -> [{ pass, failed: [] }]

async function callArm(arm, c) {
  const params = arm === "MERGED" ? mergedParams(c) : extractionParams(arm, c);
  const t0 = Date.now();
  const msg = await anthropic.messages.create(params);
  const ms = Date.now() - t0;
  const think = msg?.usage?.output_tokens_details?.thinking_tokens ?? 0;
  (stats[arm] ??= { ms: [], think: [] }).ms.push(ms);
  stats[arm].think.push(think);

  if (arm === "MERGED") {
    const parsed = LLM.parseJsonReply(LLM.readText(msg));
    return { info: parsed?.info ?? null, lang: parsed?.lang ?? null, tasks: parsed?.tasks ?? [] };
  }
  return { info: LLM.readReply(msg, "calendar"), lang: null, tasks: null };
}

for (const arm of ARMS) {
  console.log(`\n=== ARM ${arm} — ${CASES.length} cases x ${RUNS} runs ===\n`);
  results[arm] = {};
  for (const c of CASES) {
    const failed = new Set();
    let routed = 0;
    for (let run = 0; run < RUNS; run++) {
      let got;
      try {
        got = await callArm(arm, c);
      } catch (e) {
        failed.add(`the call THREW: ${e?.message || e}`);
        continue;
      }
      if (arm === "MERGED") {
        // Routing is part of the merged call's job, and a misroute is the highest-blast-radius
        // failure this change can cause.
        if ((got.tasks || []).includes("calendar_action")) routed++;
        else failed.add(`MISROUTED to ${JSON.stringify(got.tasks)} — not calendar_action`);
        // `lang` lives only in the merged reply: in PROD/NOTHINK it is the ROUTER's, and the
        // extraction call never sees it. So it is asserted here and nowhere else.
        if (c.lang && got.lang !== c.lang) failed.add(`lang = ${JSON.stringify(got.lang)}, expected ${JSON.stringify(c.lang)}`);
      }
      for (const [label, ok] of c.expect(got.info)) if (!ok) failed.add(label);
    }
    const pass = failed.size === 0;
    results[arm][c.id] = { pass, failed: [...failed] };
    console.log(`${pass ? "  ok  " : "  FAIL"}  ${c.id}  ${JSON.stringify(c.order.slice(0, 62))}${arm === "MERGED" ? `  [routed ${routed}/${RUNS}]` : ""}`);
    if (!pass) for (const f of failed) console.log(`          - ${f}`);
    if (!pass) failures++;
  }
}

// ---- the TRANSLATE_MODEL blast-radius check (Regression risk #4) --------------------------
// server.js's localizeBody() runs a DIFFERENT model (claude-haiku-4-5) through the SAME wrapped
// client, so after step 1 it too sends thinking:{type:"disabled"}. If that model rejected the
// parameter the call would 400 — inside a try/catch that returns the untranslated source, so a
// non-en/pt user would silently get English. Degraded, not fatal, and invisible except for one
// console.error. Close it here for a fraction of a cent. If it fires, the fix is one line: skip
// the injection when params.model !== ctx.model.
if (ARMS.includes("NOTHINK") || ARMS.includes("MERGED")) {
  let translateOk = false;
  let translateErr = "";
  try {
    const m = await anthropic.messages.create({
      model: TRANSLATE_MODEL,
      max_tokens: 64,
      thinking: { type: "disabled" },
      messages: [{ role: "user", content: "Translate to Spanish, reply with only the translation: good morning" }],
    });
    translateOk = LLM.readText(m).length > 0;
  } catch (e) {
    translateErr = e?.message || String(e);
  }
  console.log("");
  check(
    `TRANSLATE_MODEL (${TRANSLATE_MODEL}) accepts thinking:{type:"disabled"} — the ` +
      `localizeBody() fallback does not 400${translateErr ? `  [${translateErr}]` : ""}`,
    translateOk
  );
}

// ---- the record: this is where the card's before/after numbers come from -------------------
console.log("\n=== per arm: median latency, and the thinking tokens we were paying for ===\n");
for (const arm of ARMS) {
  const s = stats[arm];
  if (!s) continue;
  console.log(
    `  ${arm.padEnd(8)}  median ${(median(s.ms) / 1000).toFixed(2)}s   ` +
      `median thinking_tokens ${median(s.think)}   (n=${s.ms.length})`
  );
}

// ---- 🛑 THE STOP RULE ----------------------------------------------------------------------
console.log("\n=== 🛑 STOP RULE ===\n");
let stop = false;
for (const arm of ARMS) {
  if (arm === "PROD" || !results.PROD) continue;
  for (const c of CASES) {
    const base = results.PROD?.[c.id];
    const mine = results[arm]?.[c.id];
    if (base?.pass && mine && !mine.pass) {
      stop = true;
      console.log(`  🛑 ${arm} FAILS ${c.id} — a case PROD PASSES. DO NOT SHIP ${arm === "MERGED" ? "step 2" : "step 1"}.`);
      for (const f of mine.failed) console.log(`       - ${f}`);
    } else if (base && !base.pass && mine && !mine.pass) {
      console.log(`  ·  ${c.id} fails in BOTH ${arm} and PROD — that is the separate wrong-recipient card, not this one.`);
    }
  }
}
if (!stop && results.PROD) console.log("  no arm lost a case that PROD passes.");
if (!results.PROD) console.log("  (PROD was not run — the STOP rule needs it as the baseline.)");

console.log(`\n${failures === 0 && !stop ? "PASS" : `FAIL (${failures})${stop ? " + STOP RULE TRIPPED" : ""}`}\n`);
process.exit(failures === 0 && !stop ? 0 : 1);
