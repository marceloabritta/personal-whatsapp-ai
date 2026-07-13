#!/usr/bin/env node
// ============================================================================
//  Self-test for the OWNER CHANGING THE TAG HE SUMMONS HER WITH — "apply → live → persist".
//
//  It drives the exact code path the assistant_settings skill's confirm branch takes,
//  with no LLM and no Redis: normalizeTags() → settings.saveTags() → setTags().
//  The tag it applies is ["@assist", "@assistente"] — the PREFIX OVERLAP the owner may
//  legitimately land on, and the reason this file exists.
//
//  THE LANDMINE. matchedTag() used to be `TAGS.find(t => low.startsWith(t))` — FIRST match
//  wins — and server.js:285 slices the order by the MATCHED TAG'S LENGTH. So with
//  ["@assist", "@assistente"], "@assistente marque uma reunião" matched "@assist", 7 chars
//  came off, and the router was handed "ente marque uma reunião". Every Portuguese command
//  silently corrupted — no error, no log line, just a mangled order. Assertion 2 is that bug,
//  and it prints the corrupted slice when it fails.
//
//  Assertion 1c is the other half: identity-selftest.mjs DESTRUCTURES TAGS on import
//  (`const { TAGS } = await import(...)`), which snapshots the binding. So setTags() must
//  MUTATE THE ARRAY IN PLACE — an `export let` + reassign would be invisible to every reader
//  that already holds the array, including this file and server.js's per-turn ctx build.
//  This test destructures too, on purpose: it is the reader that would be blinded.
//
//  Assertion 1b is the honesty guard. saveTags() returns true ONLY when the store was really
//  written. On the memory fallback (no Redis — which is exactly this test) it returns FALSE
//  while still holding the value for the process lifetime. The skill's success message hangs
//  off that boolean, and that is what stops her ever reporting a change she did not persist.
//
//  identity.js reads process.env at MODULE TOP LEVEL and a static `import` is hoisted above
//  any env setup — so the env is seeded FIRST and the modules pulled in with a dynamic import.
//
//  No network, no keys, no Redis, no API calls.
//
//  Run:  node scripts/settings-tag-selftest.mjs
// ============================================================================

const IDENTITY = "../secretary/1. Orchestrator/lib/identity.js";
const SETTINGS = "../secretary/1. Orchestrator/lib/settings.js";

// Env FIRST, import second. This is the pair she ships with today.
process.env.SECRETARY_TAG = "@assistente,@assistant";

const { TAGS, matchedTag, setTags, normalizeTags } = await import(IDENTITY);
const { createSettings } = await import(SETTINGS);

let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) {
    if (detail) console.log(`        ${detail}`);
    failures++;
  }
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

console.log("\nsettings/tag self-test  (offline — memory fallback, no Redis)\n");

// url:"" -> the in-memory fallback, the same graceful degradation lib/sessions.js does.
const settings = createSettings({ url: "" });
await settings.ready; // resolves (never rejects) once Redis is up OR has given up

// ---- 0: the ground we start from --------------------------------------------
check("0 seeded from SECRETARY_TAG", eq(TAGS, ["@assistente", "@assistant"]));
check("0b nothing stored yet", (await settings.loadTags()) === null);

// ---- the owner's change: "@assistant, change your tag to @assist" ------------
// She deduces the PT call collapses into it too and proposes the complete list.
const PROPOSED = ["@assist", "@assistente"];
const norm = normalizeTags(PROPOSED);
check("norm accepts the proposal", norm.ok && eq(norm.tags, PROPOSED), `got ${JSON.stringify(norm)}`);

const persisted = await settings.saveTags(norm.tags);
setTags(norm.tags);

// ---- 1: it round-trips through the store ------------------------------------
check("1 loadTags round-trips", eq(await settings.loadTags(), PROPOSED));

// ---- 1b: THE HONESTY GUARD — no Redis here, so it must NOT claim it persisted -
check(
  "1b memory fallback reports NOT persisted",
  persisted === false,
  `saveTags() returned ${persisted} with no store behind it — she would claim a change she did not save`
);

// ---- 1c: TAGS went live THROUGH A SNAPSHOTTED BINDING (in-place mutation) ----
check(
  "1c TAGS is live for a reader that destructured it",
  eq(TAGS, PROPOSED),
  `TAGS is ${JSON.stringify(TAGS)} — setTags() reassigned instead of mutating in place`
);

// ---- 2: THE LANDMINE — longest match first, and the slice server.js:285 makes -
const PT = "@assistente marque uma reunião";
const tPt = matchedTag(PT);
const slicedPt = tPt ? PT.slice(tPt.length).trim() : null;
check(
  "2 longest-match: @assistente wins over the @assist prefix",
  tPt === "@assistente" && slicedPt === "marque uma reunião",
  `matchedTag -> ${JSON.stringify(tPt)}, and server.js hands the router ${JSON.stringify(slicedPt)}`
);

// ---- 2b: sorting must not have REORDERED TAGS (TAGS[0] is ctx.tag's fallback) -
check(
  "2b matchedTag did not reorder TAGS",
  eq(TAGS, PROPOSED) && TAGS[0] === "@assist",
  `TAGS is ${JSON.stringify(TAGS)} — matchedTag() sorted TAGS itself instead of a copy`
);

// ---- 3: the short tag still matches on its own -------------------------------
const EN = "@assist do X";
const tEn = matchedTag(EN);
check(
  "3 @assist matches",
  tEn === "@assist" && EN.slice(tEn?.length ?? 0).trim() === "do X",
  `matchedTag -> ${JSON.stringify(tEn)}`
);

// ---- 4: the retired tag is GONE — no alias, no grace period ------------------
check(
  "4 @assistant is retired",
  matchedTag("@assistant do X") === null,
  `matchedTag -> ${JSON.stringify(matchedTag("@assistant do X"))} — the old tag still answers`
);

// ---- 5: validation — she can never be left unsummonable ----------------------
const noAt = normalizeTags(["assist"]);
const tooShort = normalizeTags(["@a"]);
const empty = normalizeTags([]);
const dupes = normalizeTags(["@Assist", " @assist ", "@assistente"]);
check("5a rejects a tag with no @", noAt.ok === false && !!noAt.problem, JSON.stringify(noAt));
check("5b rejects a too-short tag", tooShort.ok === false && !!tooShort.problem, JSON.stringify(tooShort));
check("5c rejects an empty list", empty.ok === false && !!empty.problem, JSON.stringify(empty));
check(
  "5d lowercases, trims and dedupes",
  dupes.ok === true && eq(dupes.tags, ["@assist", "@assistente"]),
  JSON.stringify(dupes)
);

// ---- done -------------------------------------------------------------------
console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);
process.exit(failures === 0 ? 0 : 1);
