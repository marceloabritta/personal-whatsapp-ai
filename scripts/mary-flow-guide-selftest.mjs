#!/usr/bin/env node
// ============================================================================
//  Self-test for the built flow-guide doc: secretary/docs/mary-flow-guide.html
//
//  This is a DOCS card (card 22b90cb6) — a single, hand-authored, file://-openable
//  static HTML page documenting the @mary secretary runtime. There is no runtime
//  surface to exercise, so this script does not drive any code. Instead it guards
//  the four invariants that make THIS deliverable trustworthy, and that a
//  hand-authored doc is most likely to break:
//
//    1. EXISTS            — the built doc exists and is non-empty.
//    2. PROVENANCE RESOLVES — every code-citation the doc makes points at a real
//                           file:line on disk. (The #1 risk: rotted / fictional
//                           provenance — a doc that lies about where it came from.)
//    3. LOCAL-ONLY        — the doc loads ZERO external network resources. (The #2
//                           risk: an accidental Google-Fonts <link> / CDN / fetch()
//                           breaks the "local only" constraint.)
//    4. HONESTY LABELS    — every worked-example hop panel is labelled either
//                           verbatim or reconstructed. (The worst failure: a
//                           reconstructed hop dressed up as a verbatim log capture.)
//
//  ----------------------------------------------------------------------------
//  TWO STRUCTURAL CONVENTIONS THE CODER MUST FOLLOW (also stated in TESTS.md)
//  ----------------------------------------------------------------------------
//  Because raw prose is full of colons and filenames, this test does NOT try to
//  guess which strings are provenance. It reads two machine-checkable markers:
//
//  A. PROVENANCE — every code citation the doc makes (on a flow node, a prompt
//     panel, an infra block, or a trace hop) is the text content of a LEAF element
//     carrying the class `cite`, e.g.
//         <code class="cite">router/prompt.js:28</code>
//         <a class="cite" href="...">secretary/1. Orchestrator/server.js:332</a>
//     The citation text is a repo path, optionally `:<line>`. Paths resolve
//     relative to the repo root, then `secretary/`, then
//     `secretary/1. Orchestrator/`, then `secretary/3. Mary Skills/` (so the
//     doc may use the natural short forms `server.js:332`, `lib/confirm.js:33`,
//     `1. Calendar Actions/prompt.js:19`). A bare mention of a filename in prose
//     WITHOUT the `cite` class is not a provenance claim and is not checked.
//
//  B. HOP LABELS — every worked-example hop panel is an element whose class list
//     contains the marker `hop`, AND exactly one of `hop-verbatim` /
//     `hop-reconstructed`, e.g.
//         <div class="hop hop-verbatim">…</div>
//         <div class="hop hop-reconstructed">…</div>
//     (the visible `[verbatim — …]` / `[reconstructed from real shape — …]` tag
//     lives inside). A `hop` panel with neither label is a lie waiting to happen
//     and fails the test.
//
//  Run:  node scripts/mary-flow-guide-selftest.mjs
//  Exit: 0 only if ALL four checks pass; non-zero otherwise.
// ============================================================================
import { readFile, stat } from "node:fs/promises";

const ROOT = new URL("../", import.meta.url);
const DOC_REL = "secretary/docs/mary-flow-guide.html";
const DOC_URL = new URL(DOC_REL, ROOT);

// Floors — guard against a doc that "passes" a check vacuously (e.g. by using a
// different markup so the test finds nothing to resolve/label). The plan
// enumerates ~50 file:line anchors and 11 traces of several hops each, so these
// are comfortably below a real build yet high enough to catch a missing set.
const MIN_CITATIONS = 20;
const MIN_HOPS = 11;

let failures = 0;
function check(name, cond) {
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) failures++;
}

// Path bases the doc's short-form citations may be relative to.
const BASES = ["", "secretary/", "secretary/1. Orchestrator/", "secretary/3. Mary Skills/"];

const lineCountCache = new Map();
async function lineCount(absUrl) {
  const key = absUrl.href;
  if (lineCountCache.has(key)) return lineCountCache.get(key);
  const txt = await readFile(absUrl, "utf8");
  // A trailing newline does not add a line; count content lines.
  const n = txt.length === 0 ? 0 : txt.replace(/\n$/, "").split("\n").length;
  lineCountCache.set(key, n);
  return n;
}

// Resolve a cited path against the known bases. Returns { url, lines } or null.
async function resolveCited(citedPath) {
  for (const base of BASES) {
    const u = new URL(base + citedPath, ROOT);
    try {
      const s = await stat(u);
      if (s.isFile()) return { url: u, lines: await lineCount(u) };
    } catch {
      /* try next base */
    }
  }
  return null;
}

function decodeEntities(s) {
  return s
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#0*39;|&#x0*27;|&apos;/g, "'");
}

console.log(`\nmary-flow-guide self-test  (doc -> ${DOC_REL})\n`);

// ---- 1: EXISTS --------------------------------------------------------------
console.log("1  EXISTS: the built doc exists and is non-empty");
let html = null;
try {
  const s = await stat(DOC_URL);
  if (s.isFile() && s.size > 0) html = await readFile(DOC_URL, "utf8");
  check(`${DOC_REL} exists and is non-empty`, !!html);
} catch {
  check(`${DOC_REL} exists and is non-empty`, false);
}

if (!html) {
  // Checks 2–4 cannot run without the doc. Report them BLOCKED (counted as
  // not-passed) so the summary is honest and the run exits non-zero.
  console.log("\n2  PROVENANCE RESOLVES");
  check("cannot run — doc missing (see check 1)", false);
  console.log("\n3  LOCAL-ONLY / ZERO NETWORK");
  check("cannot run — doc missing (see check 1)", false);
  console.log("\n4  HONESTY LABELS");
  check("cannot run — doc missing (see check 1)", false);
  console.log(
    `\nFAIL (${failures}) — the doc is not built yet. Once ` +
      `${DOC_REL} exists, checks 2–4 run against it.\n`
  );
  process.exit(1);
}

// ---- 2: PROVENANCE RESOLVES -------------------------------------------------
console.log("\n2  PROVENANCE RESOLVES: every class=\"cite\" citation is a real file:line");

// Grab the inner text of every leaf element carrying the `cite` class.
const citeEl = /<(\w+)\b[^>]*\bclass="([^"]*)"[^>]*>([\s\S]*?)<\/\1>/g;
const citations = []; // { raw, path, line }
let m;
while ((m = citeEl.exec(html)) !== null) {
  const classes = m[2].split(/\s+/);
  if (!classes.includes("cite")) continue;
  const text = decodeEntities(m[3].replace(/<[^>]+>/g, " ")).trim();
  // Pull path(.ext) with optional :line out of the cite's text.
  const tok = /([\w .\/-]*?\.(?:mjs|js|md|html|sh|json))(?::(\d+))?/g;
  let t;
  while ((t = tok.exec(text)) !== null) {
    citations.push({ raw: t[0], path: t[1].trim(), line: t[2] ? Number(t[2]) : null });
  }
}

check(
  `found at least ${MIN_CITATIONS} code citations (got ${citations.length})`,
  citations.length >= MIN_CITATIONS
);

const broken = [];
for (const c of citations) {
  const hit = await resolveCited(c.path);
  if (!hit) {
    broken.push(`${c.raw}  — no such file under any known base`);
    continue;
  }
  if (c.line !== null && (c.line < 1 || c.line > hit.lines)) {
    broken.push(`${c.raw}  — line ${c.line} out of range (file has ${hit.lines} lines)`);
  }
}
check("every citation resolves to a real file, line in range", broken.length === 0);
if (broken.length) {
  console.log(`       ${broken.length} broken citation(s):`);
  for (const b of broken) console.log(`         - ${b}`);
}

// ---- 3: LOCAL-ONLY / ZERO NETWORK ------------------------------------------
console.log("\n3  LOCAL-ONLY: the doc loads NO external network resource");

// Target resource-loading constructs only — not every "http" in visible prose or
// in a verbatim trace's JSON. Note: HTML-escaped code samples (src=&quot;http…)
// do NOT match, because these patterns require a literal quote/paren, so
// displayed-as-text markup is naturally excluded — only live loads match.
const NET_PATTERNS = [
  [/\b(?:src|href)\s*=\s*["']?\s*(?:https?:)?\/\//gi, "external src/href (http(s) or protocol-relative //)"],
  [/@import\s+(?:url\(\s*)?["']?\s*(?:https?:)?\/\//gi, "@import of an external stylesheet"],
  [/\burl\(\s*["']?\s*(?:https?:)?\/\//gi, "CSS url(...) pointing at an external origin"],
  [/\bfetch\s*\(/g, "fetch( call"],
  [/\bXMLHttpRequest\b/g, "XMLHttpRequest"],
  [/\bnew\s+WebSocket\s*\(/g, "WebSocket"],
  [/\bnew\s+EventSource\s*\(/g, "EventSource"],
  [/\bnavigator\.sendBeacon\s*\(/g, "navigator.sendBeacon"],
];

const netHits = [];
for (const [re, label] of NET_PATTERNS) {
  let h;
  while ((h = re.exec(html)) !== null) {
    const at = html.slice(Math.max(0, h.index - 20), h.index + 40).replace(/\s+/g, " ");
    netHits.push(`${label}  — …${at}…`);
  }
}
check("no external network resource is referenced", netHits.length === 0);
if (netHits.length) {
  console.log(`       ${netHits.length} network reference(s):`);
  for (const n of netHits) console.log(`         - ${n}`);
}

// ---- 4: HONESTY LABELS ------------------------------------------------------
console.log("\n4  HONESTY LABELS: every worked-example hop panel is labelled");

const openTag = /<(?:div|section|article|li)\b[^>]*\bclass="([^"]*)"[^>]*>/gi;
let hopCount = 0;
const unlabelled = [];
let tag;
while ((tag = openTag.exec(html)) !== null) {
  const classes = tag[1].split(/\s+/);
  if (!classes.includes("hop")) continue; // only hop panels are subject to the rule
  hopCount++;
  const labelled = classes.includes("hop-verbatim") || classes.includes("hop-reconstructed");
  if (!labelled) {
    const at = html.slice(tag.index, tag.index + 80).replace(/\s+/g, " ");
    unlabelled.push(`…${at}…`);
  }
}

check(`found at least ${MIN_HOPS} hop panels (got ${hopCount})`, hopCount >= MIN_HOPS);
check(
  "every hop panel is tagged hop-verbatim or hop-reconstructed",
  unlabelled.length === 0
);
if (unlabelled.length) {
  console.log(`       ${unlabelled.length} unlabelled hop panel(s):`);
  for (const u of unlabelled) console.log(`         - ${u}`);
}

// ---- summary ----------------------------------------------------------------
console.log(
  `\n${failures === 0 ? "PASS" : `FAIL (${failures})`} — ` +
    `${citations.length} citations checked, ${hopCount} hop panels checked\n`
);
process.exit(failures === 0 ? 0 : 1);
