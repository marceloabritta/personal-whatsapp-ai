#!/usr/bin/env node
// ============================================================================
//  Self-test for the outgoing-message header framer (lib/format.js +
//  lib/identity.js).  Card d37ae619 — "Outgoing message sent with a doubled header".
//
//  The bug this exists to prevent (2026-07-29):
//  The send-boundary framer `frame(header, body)` (lib/format.js) prepended its
//  language-aware header UNCONDITIONALLY. When a caller handed it a body that
//  ALREADY opened with a header — the router occasionally authors one inside its
//  `say` prose — a second header was stacked on top and BOTH shipped. Two verified
//  live instances: a Rascal Itaim address answer and a DENZA PDF summary, both
//  router-authored `say` turns, each sent with the header stamped twice.
//
//  The fix teaches `frame()` to strip a header the body already opens with, before
//  it stamps its one header, via a new pure `stripLeadingHeader(text)` in
//  identity.js that matches the KNOWN header set (both live languages + legacy),
//  whole-line exact — so a legitimate body is never eaten.
//
//  This harness drives the REAL send path modules offline, exactly as the
//  throwaway repro (scripts/doubled-header-repro.mjs) does — sendSay's gate
//  (shouldForceTranslateSay, lib/lang.js) and send()'s composition (localizeBody
//  passthrough for en/pt + frame(headerFor(lang), body), lib/format.js +
//  lib/identity.js) — but asserts the POST-FIX correct output: exactly ONE header.
//  No network, no keys: for the maintained langs (en/pt) localizeBody is a pure
//  passthrough, so the whole framing is deterministic and offline.
//
//  server.js self-starts an Express listener and cannot be imported, so send()'s
//  en/pt composition is reproduced here EXACTLY as written at server.js:194-197
//  and sendSay at server.js:491-496 — the same reproduction the repro proved.
//
//  Load-bearing invariant: a framed outgoing message opens with EXACTLY ONE
//  assistant header, even when the body arrives pre-headered. If anyone reverts
//  frame() to stamp unconditionally, cases 1–2 go red.
//
//  Run:  node scripts/doubled-header-selftest.mjs
// ============================================================================

process.env.OWNER_NAME = process.env.OWNER_NAME || "Marcelo"; // match the report's headers

// Dynamic import (like the repro) so a not-yet-added export resolves to
// `undefined` instead of a module-link SyntaxError — this test is meant to be
// authored BEFORE the fix and to fail cleanly against the pre-fix modules.
const { frame } = await import(
  new URL("../secretary/1. Orchestrator/lib/format.js", import.meta.url).href
);
const { headerFor, stripLeadingHeader } = await import(
  new URL("../secretary/1. Orchestrator/lib/identity.js", import.meta.url).href
);
const { MAINTAINED_LANGS, shouldForceTranslateSay } = await import(
  new URL("../secretary/1. Orchestrator/lib/lang.js", import.meta.url).href
);

// localizeBody, restricted to the offline path this test exercises. For a
// maintained lang (en/pt) with force=false it returns the text UNCHANGED — the
// exact branch server.js takes for these turns (no LLM, no network).
function localizeBodyOffline(text, lang, { force = false } = {}) {
  const l = (lang || "en").toLowerCase();
  if (!text || (!force && (MAINTAINED_LANGS.has(l) || l === "en"))) return text;
  throw new Error(
    `test would need a live translation for lang=${l} force=${force} — not exercised here`
  );
}

// send(), en/pt composition, verbatim from server.js:194-197 minus the network I/O.
function sendFramed(text, lang) {
  const body = localizeBodyOffline(text, lang);
  return frame(headerFor(lang), body); // == evolution.sendText(number, <this>)
}

// sendSay(), verbatim from server.js:491-496.
function sendSayFramed(say, sayLang, ctxLang) {
  const body = shouldForceTranslateSay(sayLang, ctxLang)
    ? localizeBodyOffline(say, ctxLang, { force: true })
    : say;
  return sendFramed(body, ctxLang);
}

// How many assistant headers (bold, either language) the message opens each block
// with. Reused from the repro so the count assertions are apples-to-apples.
function countHeaders(text) {
  const HEADERS = [
    `[${process.env.OWNER_NAME}'s AI Assistant]:`,
    `[Assistente IA do ${process.env.OWNER_NAME}]:`,
  ];
  return text
    .split("\n")
    .filter((line) => HEADERS.some((h) => line.replace(/^[*_~\s]+/, "").startsWith(h)))
    .length;
}

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// A framed-output case: prints diagnostics (so a pre-fix failure legibly shows the
// DOUBLED header), then asserts exact string equality AND header-count === 1.
function checkFramed(title, out, expected) {
  const count = countHeaders(out);
  const exact = out === expected;
  console.log(`\n--- ${title} — header count ${count} (want 1) ---`);
  if (!exact) {
    console.log("ACTUAL:\n" + out);
    console.log("EXPECTED:\n" + expected);
  }
  check(`${title}: framed output === single-header version`, exact);
  check(`${title}: exactly one header`, count === 1);
}

console.log(
  `stripLeadingHeader exported by identity.js: ${
    typeof stripLeadingHeader === "function" ? "yes" : "NO (fix not applied yet)"
  }`
);

// ---------------------------------------------------------------------------
// 1. Instance 2 — Rascal Itaim address. Router `say` already carries a bold EN
//    header; reply.lang="pt", pinned ctx.lang="pt". Post-fix: the embedded EN
//    header is stripped, one PT header is stamped. (Pre-fix: two stacked headers.)
// ---------------------------------------------------------------------------
const say2 =
  "*[Marcelo's AI Assistant]:*\n\n_Rascal Itaim: Rua Joaquim Floriano, 424 - Itaim Bibi, São Paulo - SP._";
const expected2 =
  "*[Assistente IA do Marcelo]:*\n\n_Rascal Itaim: Rua Joaquim Floriano, 424 - Itaim Bibi, São Paulo - SP._";
checkFramed("1. Rascal Itaim (say EN header, lang pt)", sendSayFramed(say2, "pt", "pt"), expected2);

// ---------------------------------------------------------------------------
// 2. Instance 1 — DENZA PDF summary. Router `say` carries an EN header and the
//    pinned lang is also en, so headerFor("en") would stamp the SAME header again.
//    Post-fix: the embedded header is stripped, one EN header is stamped.
// ---------------------------------------------------------------------------
const say1 = "*[Marcelo's AI Assistant]:*\n\n_Aqui está o resumo do PDF — DENZA B5:_";
const expected1 = "*[Marcelo's AI Assistant]:*\n\n_Aqui está o resumo do PDF — DENZA B5:_";
checkFramed("2. DENZA PDF (say EN header, lang en)", sendSayFramed(say1, "en", "en"), expected1);

// ---------------------------------------------------------------------------
// 3. Control — a CLEAN `say` with NO embedded header, lang pt (the over-strip
//    guard). The guard must NOT eat a legitimate body: one PT header, body intact.
//    This already passes today; it must KEEP passing after the fix.
// ---------------------------------------------------------------------------
const sayClean = "Rascal Itaim: Rua Joaquim Floriano, 424 - Itaim Bibi, São Paulo - SP.";
const expected3 =
  "*[Assistente IA do Marcelo]:*\n\n_Rascal Itaim: Rua Joaquim Floriano, 424 - Itaim Bibi, São Paulo - SP._";
const outClean = sendSayFramed(sayClean, "pt", "pt");
checkFramed("3. Control clean say (no embedded header, lang pt)", outClean, expected3);
check(
  "3b. Control: the single header is the PT one (body not eaten)",
  outClean.startsWith("*[Assistente IA do Marcelo]:*") &&
    outClean.includes("Rua Joaquim Floriano, 424")
);

// ---------------------------------------------------------------------------
// 4. Direct over-strip safety on stripLeadingHeader itself. These pin the
//    conservative-by-default contract: strip ONLY a leading, whole-line header.
//    (Pre-fix: stripLeadingHeader is undefined, so every one of these FAILs.)
// ---------------------------------------------------------------------------
const strip = typeof stripLeadingHeader === "function" ? stripLeadingHeader : null;
console.log("\n--- 4. stripLeadingHeader direct safety cases ---");
check(
  "4a. header-free single line is returned unchanged",
  !!strip && strip("Meet at 3pm") === "Meet at 3pm"
);
check(
  "4b. a line merely CONTAINING '*' (not a header) is unchanged",
  !!strip && strip("the *best* option") === "the *best* option"
);
check(
  "4c. a header appearing MID-body (not the first line) is NOT stripped",
  !!strip &&
    strip("real body line\n*[Marcelo's AI Assistant]:*\n\n_x_") ===
      "real body line\n*[Marcelo's AI Assistant]:*\n\n_x_"
);
check(
  "4d. a LEGACY header opening the body IS stripped (ALL_HEADERS coverage)",
  !!strip && strip("*[Marcelo's AI Secretary]:*\n\n_x_") === "_x_"
);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
