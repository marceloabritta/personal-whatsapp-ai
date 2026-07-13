#!/usr/bin/env node
// ============================================================================
//  Self-test for the open-card highlight on the kanban board
//  (AI Coding-kanban/web/index.html).
//
//  The feature: while a card's chat drawer is open, that card is lit on the board with
//  an accent-blue tint, and the drawer's own title block carries the SAME PERCEIVED
//  colour, so the eye connects panel -> card.
//
//  Two things make this harder than "add a class", and both are what this test guards:
//
//  A. THE COLOUR IS A PAIR, NOT A VALUE. The lit card sits UNDER the .4 scrim
//     (`.scrim` z-index 15); the drawer sits ABOVE it (z-index 20). Everything under the
//     scrim is opaque, so seen = 0.6 x raw. The card therefore carries the RAW tint
//     (--card-open) and `#d-card .dh` carries the PRE-COMPOSITED one (--card-open-seen),
//     and the two only *look* identical while --card-open-seen === 0.6 x --card-open.
//     Tune one hex without the other and the feature silently stops doing the one thing
//     it was asked to do. Assertion 2 does that arithmetic. It is the load-bearing one.
//
//  B. THE DRAWERS SHARE THEIR CLASSES. `.dh`, `.badge`, `.badge.busy`, `.badge.gate`,
//     `select.mini` and `.arts a` are used by d-card, d-mgr, d-worker, d-col AND d-trash.
//     The chips inside the tinted title block must be restyled to stay legible on it
//     (accent-blue text on an accent-blue field is unreadable) — but SCOPED under
//     `#d-card`. Doing it globally repaints four innocent drawers. Assertions 5 and 6
//     are that fence.
//
//  And three JS traps, from the scope's edge cases:
//    - renderBoards() does root.innerHTML='' and rebuilds every card, so the class must be
//      RE-DERIVED in cardEl() at render time, never stamped on once (edge case 2).
//    - closeAll() never re-renders, so closing must clear the highlight THERE and then, or
//      it sits lit on the board for minutes after the drawer is gone (edge case 3, and
//      Escape, edge case 12).
//    - openCardDrawer() runs show() -> closeAll() FIRST, which nulls the open card. The
//      repaint must come AFTER openCard=id or nothing lights up at all (edge case 4).
//
//  This is a STATIC test: it reads the page as text and asserts the contract. It cannot
//  render a pixel, so it cannot tell you the tint *looks* right — a human eyeballs that
//  once. The runtime behaviour (ordering, switching, synchronous clear, and that the CSS
//  actually applied) is asserted in a real browser by
//  `AI Coding-kanban/tests/ui_test.py` -> section "the open-card highlight".
//
//  No network, no keys, no dependencies.
//
//  Run:  node scripts/card-highlight-selftest.mjs
// ============================================================================
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PAGE = path.join(HERE, "..", "AI Coding-kanban", "web", "index.html");

let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) {
    failures++;
    if (detail) console.log(`          ${detail}`);
  }
}
const clip = (s, n = 110) => (!s ? "(not found)" : s.length > n ? s.slice(0, n) + "…" : s);

const html = readFileSync(PAGE, "utf8");

// ---- CSS ---------------------------------------------------------------------
// Flatten <style> into {sel, body} rules. Comments are stripped first (a comment sitting
// above a rule would otherwise glue itself onto the selector), bodies have their
// whitespace removed so `background: var(--x)` and `background:var(--x)` compare the same,
// and at-rules (@keyframes) are consumed whole and dropped.
const styleSrc = (html.match(/<style>([\s\S]*?)<\/style>/) || [, ""])[1].replace(
  /\/\*[\s\S]*?\*\//g,
  " "
);

function cssRules(src) {
  const rules = [];
  let i = 0;
  while (i < src.length) {
    const open = src.indexOf("{", i);
    if (open < 0) break;
    const sel = src.slice(i, open).trim().replace(/\s+/g, " ");
    let depth = 1;
    let j = open + 1;
    while (j < src.length && depth > 0) {
      if (src[j] === "{") depth++;
      else if (src[j] === "}") depth--;
      j++;
    }
    if (sel && !sel.startsWith("@")) {
      rules.push({ sel, body: src.slice(open + 1, j - 1).replace(/\s+/g, "") });
    }
    i = j;
  }
  return rules;
}

const RULES = cssRules(styleSrc);
const ruleFor = (sel) => RULES.find((r) => r.sel === sel) || null;
const countOf = (sel) => RULES.filter((r) => r.sel === sel).length;

const rootBody = ruleFor(":root")?.body ?? "";
// `--card-open:` cannot match `--card-open-seen:` — the colon is required immediately after.
const token = (name) =>
  (rootBody.match(new RegExp(`--${name}:(#[0-9a-fA-F]{3,6})`)) || [])[1] || null;

function rgb(hex) {
  if (!hex) return null;
  let h = hex.slice(1);
  if (h.length === 3) h = [...h].map((c) => c + c).join("");
  if (h.length !== 6) return null;
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

// ---- JS ----------------------------------------------------------------------
function fnBody(name) {
  const m = new RegExp(`function\\s+${name}\\s*\\(`).exec(html);
  if (!m) return null;
  const open = html.indexOf("{", m.index + m[0].length - 1);
  if (open < 0) return null;
  let depth = 1;
  let j = open + 1;
  while (j < html.length && depth > 0) {
    if (html[j] === "{") depth++;
    else if (html[j] === "}") depth--;
    j++;
  }
  return html.slice(open + 1, j - 1);
}

// A comment that MENTIONS paintOpenCard() is not a call to it. Assert on code, not prose.
const code = (s) =>
  (s ?? "")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ")
    .replace(/\s+/g, "");

const cardElSrc = fnBody("cardEl");
const closeAllSrc = fnBody("closeAll");
const openDrawerSrc = fnBody("openCardDrawer");
const paintSrc = fnBody("paintOpenCard");

const cardEl = code(cardElSrc);
const closeAll = code(closeAllSrc);
const openDrawer = code(openDrawerSrc);
const paint = code(paintSrc);

// ============================================================================
//  0. the harness itself — if THIS goes red, the test is broken, not the feature
// ============================================================================
console.log("\nthe page, and the parse of it");
check(
  "0. the page parsed: :root, .card, cardEl(), closeAll() and openCardDrawer() were all found",
  RULES.length > 20 &&
    !!rootBody &&
    !!ruleFor(".card") &&
    cardElSrc !== null &&
    closeAllSrc !== null &&
    openDrawerSrc !== null,
  `rules=${RULES.length} :root=${!!rootBody} .card=${!!ruleFor(".card")} ` +
    `cardEl=${cardElSrc !== null} closeAll=${closeAllSrc !== null} openCardDrawer=${openDrawerSrc !== null}` +
    ` — parsed from ${PAGE}`
);

// ============================================================================
//  the colour, and the pairing that makes it work
// ============================================================================
console.log("\nthe colour: a pair, not a value");

const raw = token("card-open");
const seen = token("card-open-seen");
const lineTok = token("card-open-line");

check(
  "1. :root declares --card-open, --card-open-seen and --card-open-line",
  !!raw && !!seen && !!lineTok,
  `--card-open=${raw} --card-open-seen=${seen} --card-open-line=${lineTok}`
);

const A = rgb(raw);
const B = rgb(seen);
const composited = A ? A.map((v) => Math.round(v * 0.6)) : null;
check(
  "2. --card-open-seen IS 0.6 x --card-open, channel by channel  <-- the load-bearing invariant",
  !!A && !!B && composited.every((v, i) => v === B[i]),
  A && B
    ? `0.6 x ${raw} = rgb(${composited.join(", ")}), but --card-open-seen is rgb(${B.join(", ")})`
    : "cannot do the arithmetic: --card-open and/or --card-open-seen is missing"
);

check(
  "   2b. the scrim is still rgba(0,0,0,.4) — the 0.6 factor above is only true while it is",
  (ruleFor(".scrim")?.body ?? "").includes("background:rgba(0,0,0,.4)"),
  `.scrim { ${clip(ruleFor(".scrim")?.body)} }`
);

// ============================================================================
//  the two surfaces that must match
// ============================================================================
console.log("\nthe lit card, and the drawer title that must match it");

const openChat = ruleFor(".card.open-chat");
check(
  "3. .card.open-chat exists and gives the lit card a background",
  !!openChat && /background:/.test(openChat.body),
  `.card.open-chat { ${clip(openChat?.body)} }`
);

const dhCard = ruleFor("#d-card .dh");
check(
  "4. #d-card .dh carries the PRE-COMPOSITED value var(--card-open-seen), not the raw tint",
  !!dhCard && dhCard.body.includes("background:var(--card-open-seen)"),
  `#d-card .dh { ${clip(dhCard?.body)} }`
);

// ============================================================================
//  the chips inside the tinted title block — restyled, but SCOPED
// ============================================================================
console.log("\nthe chips in the tinted title block: legible, and scoped to #d-card only");

const CHIPS = [".badge", ".badge.busy", ".badge.gate", "select.mini", ".arts a"];

for (const base of CHIPS) {
  const scoped = RULES.find(
    (r) => r.sel.startsWith("#d-card") && r.sel.endsWith(base) && r.sel !== base && r.body
  );
  check(
    `5. '${base}' inside the open card's drawer is restyled for the tint, scoped under #d-card`,
    !!scoped,
    scoped ? "" : `no rule '#d-card … ${base}' — that chip is left as-is on the blue field`
  );
}

for (const base of CHIPS) {
  const n = countOf(base);
  check(
    `6. the SHARED '${base}' is still exactly one unscoped rule — d-mgr/d-worker/d-col/d-trash untouched`,
    n === 1,
    `${n} rules have the selector exactly '${base}' (expected 1). Restyling it globally repaints four other drawers.`
  );
}

check(
  "   6b. the bare .dh rule still has NO background of its own (a background there tints ALL five drawers)",
  !(ruleFor(".dh")?.body ?? "").includes("background"),
  `.dh { ${clip(ruleFor(".dh")?.body)} }`
);

const tintLeak = RULES.filter(
  (r) =>
    /var\(--card-open/.test(r.body) &&
    r.sel !== ":root" &&
    !r.sel.startsWith("#d-card") &&
    !r.sel.startsWith(".card.open-chat")
);
check(
  "   6c. the tint tokens are used ONLY by .card.open-chat and #d-card — never by a bare selector",
  tintLeak.length === 0,
  `leaked into: ${tintLeak.map((r) => r.sel).join(" | ")}`
);

// ============================================================================
//  the JS: derived at render time, painted on open, cleared on close, in that order
// ============================================================================
console.log("\nthe JS: the highlight survives a re-render, and dies with the drawer");

check(
  "7. cardEl() DERIVES the class from the open card at render time (renderBoards() rebuilds every card)",
  cardEl.includes("openCard") && cardEl.includes("open-chat"),
  `cardEl() has neither the open-card state nor 'open-chat' in it — a class stamped on once is ` +
    `wiped by the next board frame. Body: ${clip(cardEl)}`
);

check(
  "8. cardEl() makes the node addressable (data-id), so the board can be repainted without a re-render",
  /dataset\.id=/.test(cardEl) || /setAttribute\(['"]data-id['"]/.test(cardEl),
  `no data-id on the card element — paintOpenCard() would have nothing to find. Body: ${clip(cardEl)}`
);

check(
  "9. closeAll() repaints the board itself — it never re-renders, so the clear must land THERE (× / scrim / Escape)",
  closeAll.includes("paintOpenCard()"),
  `closeAll() { ${clip(closeAll)} }`
);

const iSet = openDrawer.indexOf("openCard=id");
const iPaint = openDrawer.indexOf("paintOpenCard()");
check(
  "10. openCardDrawer() paints AFTER openCard=id — show() -> closeAll() has just nulled it",
  iSet >= 0 && iPaint >= 0 && iSet < iPaint,
  `openCard=id at ${iSet}, paintOpenCard() at ${iPaint} (both must exist, and the paint must come second, ` +
    `or the open card ends up dark). Body: ${clip(openDrawer)}`
);

check(
  "11. paintOpenCard() TOGGLES the class across every card, so the previously-lit one goes dark",
  paintSrc !== null && paint.includes("classList.toggle"),
  paintSrc === null
    ? "there is no paintOpenCard() at all"
    : `paintOpenCard() { ${clip(paint)} }`
);

// ---- done --------------------------------------------------------------------
console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
