#!/usr/bin/env node
// ============================================================================
//  feature-request MESSAGE wording — the single owner-facing line on a
//  capability-gap auto-file.
//
//  The card: when Mary hits a capability gap and auto-files a feature request,
//  she must NOT dump the .md inline. She keeps the spool save (the file that
//  reaches the board) and sends ONE plain-text message that (a) admits she
//  can't do it herself yet and (b) says she's recorded it as a feature request
//  to build — with NO attachment / spec / repo wording (there is no attachment
//  now).
//
//  This is pure strings + a static source check — no API key, no network.
//    - imports `reply` from the Feature Requests prompt.js and asserts on
//      reply(lang).logged({ title }) for en + pt.
//    - reads skill.js source: the inline send (`sendMedia`) is gone, while the
//      spool save (`spoolSpec(`) and the plain-text reply (`ctx.send(`) remain.
//
//  Run:  node scripts/feature-request-message-selftest.mjs
// ============================================================================
import { readFile } from "node:fs/promises";
import path from "node:path";

const SKILL_DIR = path.resolve("secretary/3. Mary Skills/4. Feature Requests");

const { reply } = await import(
  path.join("file://", SKILL_DIR, "prompt.js")
);

let failures = 0;
function check(name, cond) {
  if (cond) {
    console.log(`  ok    ${name}`);
  } else {
    console.error(`  FAIL  ${name}`);
    failures++;
  }
}

const TITLE = "Book flights";

// ---- 1. the logged() wording, en + pt --------------------------------------
const expectations = {
  en: {
    cantDoYet: /can'?t do that myself yet/i,
    recorded: /feature request to build/i,
  },
  pt: {
    cantDoYet: /ainda não consigo/i,
    recorded: /pedido de novo recurso/i,
  },
};

const noAttachment = /spec|repo|attach|arquivo|repositório/i;

for (const lang of ["en", "pt"]) {
  const msg = reply(lang).logged({ title: TITLE });
  console.log(`\n${lang}  logged: ${msg}`);
  check(`${lang}: title interpolated`, msg.includes(TITLE));
  check(`${lang}: admits she can't do it yet`, expectations[lang].cantDoYet.test(msg));
  check(`${lang}: says recorded as a feature request`, expectations[lang].recorded.test(msg));
  check(`${lang}: no attachment/spec/repo wording`, !noAttachment.test(msg));
}

// ---- 2. static source check on skill.js ------------------------------------
const skillSrc = await readFile(path.join(SKILL_DIR, "skill.js"), "utf8");
console.log("");
check("skill.js: no inline send (sendMedia removed)", !skillSrc.includes("sendMedia"));
check("skill.js: keeps the spool save (spoolSpec()", skillSrc.includes("spoolSpec("));
check("skill.js: sends a plain-text reply (ctx.send()", skillSrc.includes("ctx.send("));

console.log("");
if (failures) {
  console.error(`FAIL — ${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("all passed");
