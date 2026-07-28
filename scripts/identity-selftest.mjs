#!/usr/bin/env node
// ============================================================================
//  Self-test for lib/identity.js — the trigger tags and the reply HEADER.
//
//  identity.js is the single source of truth for (a) which tags start a flow and
//  (b) the header stamped on every outgoing message. Two lists, opposite answers:
//
//    NEW_TAGS        — what the bot ANSWERS TO. Retiring a tag means it stops matching.
//    LEGACY_HEADERS  — what the bot RECOGNISES AS ITS OWN. Nothing is ever retired here.
//
//  The second one is the load-bearing invariant, and it is the one that breaks
//  silently. Every bot message already sitting in WhatsApp history carries an OLD
//  header. `5. Feedback/skill.js:94` decides whether a quoted message is the bot's own
//  by calling isOwnMessage() on it. Drop a legacy header and every quoted back-catalogue
//  message is re-filed as "NOT secretary output — treat as context only", and the
//  feedback skill quietly stops recognising the bot's own defects. No error. No log line.
//  Assertion 4a is the only thing standing between LEGACY_HEADERS and a future cleanup.
//
//    1  NEW_TAGS default is @mary
//    2  headerFor() returns the new en/pt headers
//    3  headerFor() still falls back to en for an unmaintained/absent lang
//    4a isOwnMessage() STILL recognises the RETIRED headers       <-- the standing guard
//    4b isOwnMessage() recognises the NEW headers
//    5  a genuine owner message is NOT mistaken for the bot's
//    6  matchedTagNew() matches @mary and NOT other tags
//    7  slice-by-matched-tag-length still yields the order (server.js:271's contract)
//    8  SECRETARY_TAG_NEW still overrides the code default (the production trap)
//
//  identity.js reads process.env at MODULE TOP LEVEL, and a static `import` is hoisted
//  above any env setup — so the env is set FIRST and the module pulled in with a dynamic
//  await import(). Same reason assertion 8 re-imports with a `?env=1` cache-buster: ESM
//  caches by specifier, and re-importing the bare path would hand back the module that
//  was already evaluated, i.e. a false pass.
//
//  No network, no keys, no API calls.
//
//  Run:  node scripts/identity-selftest.mjs
// ============================================================================

const MODULE = "../secretary/1. Orchestrator/lib/identity.js";

// Env FIRST, import second.
delete process.env.SECRETARY_TAG_NEW; // prove the CODE DEFAULT, not whatever the shell exports
process.env.OWNER_NAME = "Marcelo"; // make the header assertions concrete

const { NEW_TAGS, headerFor, isOwnMessage, matchedTagNew } = await import(MODULE);

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// The header goes out BOLD (`*[...]:*` — lib/format.js frame()), so that is how it
// actually arrives on the wire. Older messages in history are unbolded. Both must match.
const bold = (h) => `*${h}*`;

const EN_NEW = "[Marcelo's AI Assistant]:";
const PT_NEW = "[Assistente IA do Marcelo]:";
const EN_OLD = "[Marcelo's AI Secretary]:";
const PT_OLD = "[Secretaria IA do Marcelo]:";
const BRAIN = "[AI Brain]:";

console.log("\nidentity self-test  (offline)\n");

// ---- 1: the tag the bot answers to ------------------------------------------
check(
  "1 NEW_TAGS default",
  eq(NEW_TAGS, ["@mary"]) &&
    !NEW_TAGS.includes("@assistente") &&
    !NEW_TAGS.includes("@assistant")
);

// ---- 2: the header it stamps -------------------------------------------------
check("2 headerFor values", headerFor("pt") === PT_NEW && headerFor("en") === EN_NEW);

// ---- 3: unmaintained languages fall back to en (identity.js:28) --------------
// Asserts the FALLBACK, not the header's value — that is assertion 2's job. Compared
// against headerFor("en") rather than the literal string on purpose: this guards
// behaviour that must hold whatever the en header happens to say, so it is green before
// this card and green after. Hardcoding the new string here would just make it a
// duplicate of 2 that goes red for 2's reason.
check(
  "3 headerFor en-fallback",
  headerFor("es") === headerFor("en") && headerFor(undefined) === headerFor("en")
);

// ---- 4a: THE STANDING GUARD — retired headers are still recognised -----------
// GREEN before this card and green after. It is not a regression proof; it is what goes
// red the day someone "tidies up" LEGACY_HEADERS and blinds the feedback skill.
check(
  "4a legacy headers still own",
  isOwnMessage(`${bold(EN_OLD)}\n\n_Event created._`) &&
    isOwnMessage(`${bold(PT_OLD)}\n\n_Evento criado._`) &&
    isOwnMessage(`${EN_OLD} Event created.`) &&
    isOwnMessage(`${PT_OLD} Evento criado.`) &&
    isOwnMessage(`${bold(BRAIN)} hi`) &&
    isOwnMessage(`${BRAIN} hi`)
);

// ---- 4b: the NEW headers are recognised as the bot's own ---------------------
check(
  "4b new headers own",
  isOwnMessage(`${bold(EN_NEW)}\n\n_hi_`) &&
    isOwnMessage(`${bold(PT_NEW)}\n\n_oi_`) &&
    isOwnMessage(`${EN_NEW} hi`) &&
    isOwnMessage(`${PT_NEW} oi`)
);

// ---- 5: a real owner message is not the bot's -------------------------------
check(
  "5 negative control",
  isOwnMessage("marque uma reuniao com o savio amanha") === false
);

// ---- 6: the @mary tag matches, other tags do not -----------------------------
check(
  "6 matchedTagNew mary-not-other",
  matchedTagNew("@mary marque uma reuniao") === "@mary" &&
    matchedTagNew("@Mary marque uma reuniao") === "@mary" &&
    matchedTagNew("@mary find me a flight") === "@mary" &&
    matchedTagNew("@assistente marque uma reuniao") === null &&
    matchedTagNew("@assistant find me a flight") === null
);

// ---- 7: server.js:271's contract — slice by the MATCHED tag's own length -----
// "@secretaria" is 11 chars, "@secretary" is 10: the caller must never slice by a fixed
// constant. Driven with a two-tag NEW_TAGS list of differing lengths (via SECRETARY_TAG_NEW
// + a cache-busting re-import), then sliced by the RETURNED tag's own length. Guarded — a
// bare `.length` on a null match would throw a stack trace instead of a clean failed assertion.
process.env.SECRETARY_TAG_NEW = "@secretaria,@secretary";
const { matchedTagNew: matchLen } = await import(`${MODULE}?len=1`);
const mPt = "@secretaria marque uma reuniao com o savio amanha";
const mEn = "@secretary find me a flight to Lisbon";
const tPt = matchLen(mPt);
const tEn = matchLen(mEn);
check(
  "7 slice-by-length",
  !!tPt &&
    !!tEn &&
    mPt.slice(tPt.length).trim() === "marque uma reuniao com o savio amanha" &&
    mEn.slice(tEn.length).trim() === "find me a flight to Lisbon"
);
delete process.env.SECRETARY_TAG_NEW;

// ---- 8: the env var still wins over the code default ------------------------
// This is the production trap in one assertion: the live compose sets SECRETARY_TAG_NEW, so
// the deployed value — not the code default — is what the bot actually answers to.
process.env.SECRETARY_TAG_NEW = "@foo,@bar";
const { NEW_TAGS: TAGS_ENV } = await import(`${MODULE}?env=1`); // ?env=1 = a distinct specifier
check("8 env override", eq(TAGS_ENV, ["@foo", "@bar"]));
delete process.env.SECRETARY_TAG_NEW;

// ---- 9: the PREFIX TRAP — a standing guard on matchedTagNew() ----------------
// The owner can now change his tags at runtime (the `assistant_settings` skill), so a list
// where one tag is a PREFIX of another is a supported combination, not a hypothetical. Two
// ways it goes wrong, and both are silent — the order is not rejected, it is MANGLED:
//   9a  first-match-wins would slice "@assistente marque…" by the shorter "@assist" (7 chars)
//       and hand the router "ente marque uma reunião". Longest tag first.
//   9b  length order alone still lets a tag that is NOT in the list through: with "@assist"
//       live, a RETIRED "@assistant do X" starts with it, so she would answer and hand the
//       router "ant do X". A tag must END (space/punctuation/end-of-message) to match.
//   9c  the sort must be on a COPY: NEW_TAGS[0] is the primary tag (server.js falls back to
//       it for ctx.tag), so sorting NEW_TAGS itself would silently re-elect it.
// The full apply→live→persist flow is scripts/settings-tag-selftest.mjs; this is the guard
// that stays behind in identity's own test.
process.env.SECRETARY_TAG_NEW = "@assist,@assistente";
const { NEW_TAGS: TAGS_PFX, matchedTagNew: matchPfx } = await import(`${MODULE}?prefix=1`);
const pt = "@assistente marque uma reuniao";
const tp = matchPfx(pt);
check(
  "9 prefix trap: longest-first, tag must end, NEW_TAGS not reordered",
  tp === "@assistente" &&
    pt.slice(tp.length).trim() === "marque uma reuniao" && // 9a: not "ente marque…"
    matchPfx("@assist do X") === "@assist" &&
    matchPfx("@assistant do X") === null && // 9b: a non-tag that merely EXTENDS a tag
    eq(TAGS_PFX, ["@assist", "@assistente"]) && // 9c: still the owner's order
    TAGS_PFX[0] === "@assist"
);
delete process.env.SECRETARY_TAG_NEW;

// ---- done -------------------------------------------------------------------
console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);
process.exit(failures === 0 ? 0 : 1);
