#!/usr/bin/env node
// ============================================================================
//  Self-test / regression guard — card 1600b424 "Calendar: editing or
//  cancelling an event can't find the target".
//
//  THE BUG THIS EXISTS TO CATCH (2026-07-28)
//  The @mary pure-task conversion (d5369d7) stripped target-resolution out of the
//  calendar edit/delete ACT handlers AND left `event_id` mandatory at the
//  completeness gate. So an edit/cancel of a just-referenced event has NO walkable
//  path to a write:
//    Barrier 1 (the gate): `calendar_action` is conversation:"orchestrator", so the
//      orchestrator gates dispatch on checkPayload(...).ok (validity + completeness +
//      consistency). With event_id in requiredWhen.{edit,delete}, an event_id-less
//      edit/delete FAILS completeness ("event_id: required, missing"), is bounced into
//      the repair loop, and after MAX_REPAIRS the owner gets repairGiveUp — the skill
//      is never reached.
//    Barrier 2 (the handler): even if the payload reached the skill, handleEdit /
//      handleDelete bail `noEventId` before ever consulting the quoted invite link or
//      the start_iso + attendee-email locators the router already emits — the two
//      deterministic resolvers (resolveEventId / matchEventTargets) sit UNWIRED.
//
//  This test drives the REAL production path — the completeness gate (checkPayload,
//  lib/inputs.js) THEN skill dispatch (run(), skill.js) — NOT a bare run() with a
//  pre-supplied event_id (that reaches a branch production never hits; ROOT_CAUSE.md
//  proved it). Google is stubbed OFFLINE via the repo's established ESM loader-hook
//  mechanism (a temp-dir gstub.mjs swapped in for `googleapis` by an --import register
//  hook; harness lifted from the retired scripts/turn-latency-selftest.mjs). No
//  network, no API key, no Redis, no model call. FREE.
//
//  ASSERTIONS (each case: the GATE, then the SKILL DISPATCH)
//    1. GATE (barrier 1, the red->green flip): checkPayload(manifest.inputs, payload).ok
//       === true for the event_id-absent edit/delete payloads. FAILS today
//       (ok:false, "event_id: required, missing"); passes after requiredWhen.{edit,
//       delete} := []. This is the single assertion proving the payload now reaches the
//       skill instead of repairGiveUp.
//    2. SKILL DISPATCH (barrier 2, resolution + act): with that same payload, simulate
//       what server.js does on g.ok — set ctx.info = payload and call run(ctx) — and
//       assert the skill RESOLVES the target and records the right Google write, and
//       does NOT bail noEventId/noMatch:
//         A. CANCEL by start+email  -> events.delete evt_1, ok, cancelled>=1
//         B. CANCEL by quoted link  -> events.delete evt_1, ok
//         C. EDIT (add email + 2h) by start+email -> events.update evt_1 (end=+120min,
//                                                     attendees include laura), ok
//         D. EDIT by quoted link    -> events.update evt_1, ok
//         E. GUARD (no id, no link, no start+email) -> gate ok, but NO events.delete and
//                                                     deleteNoMatch sent (return noMatch).
//            Pins that relaxing the gate did NOT turn a target-less order into an
//            accidental delete.
//
//  WHAT THIS TEST DELIBERATELY CANNOT COVER (CONVENTIONS §5): PLAN.md File 3's rulebook
//  change is MODEL behaviour (whether the model CHOOSES to dispatch a direct edit/delete
//  with locators vs. find-first). That is not offline-assertable — it is covered by the
//  live router check (ANTHROPIC_API_KEY=… node scripts/router-selftest.mjs, the human's
//  call) and by scripts/calendar-editdelete-livetest.mjs. This asserts the deterministic
//  layers around it: the gate, the resolvers, the act.
//
//  EXPECTED STATE TODAY (before the fix): the GATE assertions FAIL (ok:false,
//  "event_id: required, missing") and the edit/cancel DISPATCH assertions FAIL (the
//  handlers bail noEventId, no Google write). That red IS the point of this column.
//  After the fix (drop event_id from requiredWhen.{edit,delete} + wire the resolvers
//  into handleEdit/handleDelete) every assertion goes green.
//
//  Run:  node scripts/calendar-editdelete-resolve-selftest.mjs
//        -> exits non-zero today; exits 0 after the fix.
// ============================================================================
import { mkdtemp, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import os from "node:os";

const THIS_FILE = fileURLToPath(import.meta.url);

// ============================================================================
//  PARENT — write the googleapis loader-hook stub to a temp dir, then re-exec THIS
//  file as a child under `node --import <register> <thisFile>` so the child's
//  `import "googleapis"` resolves to the recording stub. (Same trick the retired
//  turn-latency-selftest.mjs used; here it runs the assertions in-process rather than
//  booting the server.) The stub records every Google call onto globalThis.__GCAL_CALLS,
//  which the child branch reads directly.
// ============================================================================
if (!process.env.CAL_EDITDEL_CHILD) {
  const tmp = await mkdtemp(path.join(os.tmpdir(), "cal-editdel-selftest-"));

  // ONE confirmed event, evt_1. events.get returns it for that id (404 otherwise);
  // events.list returns [evt_1]; delete/update record their eventId (+ requestBody).
  // The htmlLink carries eid = base64url("evt_1 primary"), so resolveEventId decodes evt_1.
  await writeFile(
    path.join(tmp, "gstub.mjs"),
    `globalThis.__GCAL_CALLS = globalThis.__GCAL_CALLS || [];
const rec = (name, a) => {
  globalThis.__GCAL_CALLS.push({ name, eventId: a?.eventId ?? null, requestBody: a?.requestBody ?? null });
};
const LINK = "https://calendar.google.com/event?eid=" + Buffer.from("evt_1 primary").toString("base64url");
const EV = {
  id: "evt_1", status: "confirmed", summary: "Reunião com a Laura", htmlLink: LINK,
  start: { dateTime: "2026-07-14T15:00:00-03:00", timeZone: "America/Sao_Paulo" },
  end:   { dateTime: "2026-07-14T16:00:00-03:00", timeZone: "America/Sao_Paulo" },
  attendees: [{ email: "laura@example.com" }],
};
const calendar = () => ({
  events: {
    get: async (a) => {
      rec("events.get", a);
      if (a?.eventId === "evt_1") return { data: EV };
      const e = new Error("Not Found"); e.code = 404; throw e;
    },
    list:   async (a) => { rec("events.list", a);   return { data: { items: [EV] } }; },
    update: async (a) => { rec("events.update", a); return { data: { id: a.eventId, htmlLink: LINK } }; },
    delete: async (a) => { rec("events.delete", a); return { data: {} }; },
    insert: async (a) => { rec("events.insert", a); return { data: { id: "evt_new" } }; },
    patch:  async (a) => { rec("events.patch", a);  return { data: { id: a.eventId } }; },
  },
});
class OAuth2 { constructor(...a) { this._a = a; } setCredentials(c) { this._c = c; } }
export const google = { calendar, tasks: () => ({ tasks: {}, tasklists: {} }), auth: { OAuth2 } };
export default { google };
`
  );
  await writeFile(
    path.join(tmp, "hooks.mjs"),
    `const STUB = ${JSON.stringify(pathToFileURL(path.join(tmp, "gstub.mjs")).href)};
export async function resolve(spec, ctx, next) {
  if (spec === "googleapis") return { url: STUB, format: "module", shortCircuit: true };
  return next(spec, ctx);
}
`
  );
  await writeFile(
    path.join(tmp, "register.mjs"),
    `import { register } from "node:module";
register("./hooks.mjs", import.meta.url);
`
  );

  const child = spawn(
    process.execPath,
    ["--import", pathToFileURL(path.join(tmp, "register.mjs")).href, THIS_FILE],
    { stdio: "inherit", env: { ...process.env, CAL_EDITDEL_CHILD: "1" } }
  );
  child.on("exit", (code) => process.exit(code ?? 1));
} else {
  // ==========================================================================
  //  CHILD — googleapis is now the stub. Import the REAL gate + skill and drive them.
  //  Dynamic import (not top-level static) so the PARENT never loads the skill with the
  //  real googleapis. The loader hook, registered via --import, intercepts the skill's
  //  `import "googleapis"` triggered here.
  // ==========================================================================
  const { checkPayload } = await import("../secretary/1. Orchestrator/lib/inputs.js");
  const { manifest, run } = await import(
    "../secretary/3. Mary Skills/1. Calendar Actions/skill.js"
  );

  globalThis.__GCAL_CALLS = globalThis.__GCAL_CALLS || [];

  let failures = 0;
  function check(name, cond) {
    console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
    if (!cond) failures++;
  }

  // The quoted-invite link the orchestrator builds from a replied-to event (server.js
  // getQuoted -> ctx.quoted.calendarLink). Its eid decodes to evt_1 (link scores 100 in
  // matchEventTargets — a confident match on its own).
  const LINK =
    "https://calendar.google.com/event?eid=" + Buffer.from("evt_1 primary").toString("base64url");
  const START = "2026-07-14T15:00:00-03:00"; // evt_1's CURRENT start — used as a locator
  const LAURA = "laura@example.com";

  // A full-shape payload: every DECLARED field present (a MISSING declared field is itself
  // invalid — lib/inputs.js header), nulled/defaulted, so each case isolates the target-
  // resolution behaviour and the ONLY gate problem today is the event_id completeness rule.
  const base = {
    action: null, query: null, event_id: null, title: null, participants: null,
    start_iso: null, duration_min: null, all_day: false, all_day_end_iso: null,
    summary: "", list_mode: null, range_start_iso: null, range_end_iso: null,
    recurrence: null, location: null, virtual: false,
  };

  // ---- drive one case through the skill exactly as server.js would on g.ok ----------
  async function drive(payload, quoted) {
    globalThis.__GCAL_CALLS.length = 0;
    const sent = [];
    const ctx = {
      owner: "Marcelo",
      contact: "Laura",
      number: "5511999999999@s.whatsapp.net",
      env: {}, // calId falls back to "primary"; the stub OAuth2 ignores creds
      lang: "pt",
      send: async (_n, text) => { sent.push({ via: "send", text }); },
      sendFailure: async (_n, text) => { sent.push({ via: "sendFailure", text }); },
      info: payload,
      quoted: quoted || null,
    };
    let ret;
    try {
      ret = await run(ctx);
    } catch (e) {
      ret = { threw: String(e?.message || e) };
    }
    return { ret, calls: [...globalThis.__GCAL_CALLS], sent };
  }
  const callsOf = (calls, name) => calls.filter((c) => c.name === name);
  const failed = (sent) => sent.some((s) => s.via === "sendFailure");

  // ==========================================================================
  //  CASE A — CANCEL by start+email (Flow B analogue)
  // ==========================================================================
  const payloadA = {
    ...base, action: "delete", event_id: null,
    start_iso: START, participants: [{ name: null, email: LAURA }], duration_min: 120,
  };
  const gateA = checkPayload(manifest.inputs, payloadA);
  console.log("\n[A] CANCEL by start+email");
  console.log("    gate.ok =", gateA.ok, "| problems =", JSON.stringify(gateA.problems));
  check("A1. gate PASSES an event_id-absent delete (start+email)  <-- barrier 1", gateA.ok === true);
  const A = await drive(payloadA, null);
  const delA = callsOf(A.calls, "events.delete");
  console.log("    ret =", JSON.stringify(A.ret), "| calls =", A.calls.map((c) => c.name).join(","));
  check(
    "A2. skill resolves & records events.delete evt_1, returns ok/cancelled>=1  <-- barrier 2",
    delA.length > 0 && delA[0].eventId === "evt_1" &&
      A.ret?.ok === true && A.ret?.cancelled >= 1 && !failed(A.sent)
  );

  // ==========================================================================
  //  CASE B — CANCEL by quoted invite link (everything else null)
  // ==========================================================================
  const payloadB = { ...base, action: "delete", event_id: null };
  const gateB = checkPayload(manifest.inputs, payloadB);
  console.log("\n[B] CANCEL by quoted invite link");
  console.log("    gate.ok =", gateB.ok, "| problems =", JSON.stringify(gateB.problems));
  check("B1. gate PASSES an all-null delete (link is on ctx.quoted, not info)  <-- barrier 1", gateB.ok === true);
  const B = await drive(payloadB, { calendarLink: LINK });
  const delB = callsOf(B.calls, "events.delete");
  console.log("    ret =", JSON.stringify(B.ret), "| calls =", B.calls.map((c) => c.name).join(","));
  check(
    "B2. skill resolves via the link & records events.delete evt_1, returns ok  <-- barrier 2",
    delB.length > 0 && delB[0].eventId === "evt_1" && B.ret?.ok === true
  );

  // ==========================================================================
  //  CASE C — EDIT (add attendee email + duration 2h) by start+email
  //  (Flow A's CONFIRMED-event analogue — NOT the out-of-scope unconfirmed draft)
  // ==========================================================================
  const payloadC = {
    ...base, action: "edit", event_id: null,
    start_iso: START, participants: [{ name: "Laura", email: LAURA }], duration_min: 120,
  };
  const gateC = checkPayload(manifest.inputs, payloadC);
  console.log("\n[C] EDIT (add email + 2h) by start+email");
  console.log("    gate.ok =", gateC.ok, "| problems =", JSON.stringify(gateC.problems));
  check("C1. gate PASSES an event_id-absent edit (start+email)  <-- barrier 1", gateC.ok === true);
  const C = await drive(payloadC, null);
  const updC = callsOf(C.calls, "events.update");
  const endOkC =
    updC.length > 0 &&
    new Date(updC[0].requestBody?.end?.dateTime).getTime() ===
      new Date(START).getTime() + 120 * 60000;
  const attOkC =
    updC.length > 0 && (updC[0].requestBody?.attendees || []).some((a) => a.email === LAURA);
  console.log("    ret =", JSON.stringify(C.ret), "| calls =", C.calls.map((c) => c.name).join(","));
  check(
    "C2. skill resolves & records events.update evt_1 (end=+120min, attendee laura), returns ok  <-- barrier 2",
    updC.length > 0 && updC[0].eventId === "evt_1" && endOkC && attOkC &&
      C.ret?.ok === true && C.ret?.eventId === "evt_1" && !failed(C.sent)
  );

  // ==========================================================================
  //  CASE D — EDIT by quoted invite link (locator via ctx.quoted, no start/participants)
  // ==========================================================================
  const payloadD = { ...base, action: "edit", event_id: null, duration_min: 120 };
  const gateD = checkPayload(manifest.inputs, payloadD);
  console.log("\n[D] EDIT by quoted invite link");
  console.log("    gate.ok =", gateD.ok, "| problems =", JSON.stringify(gateD.problems));
  check("D1. gate PASSES an event_id-absent edit (link on ctx.quoted)  <-- barrier 1", gateD.ok === true);
  const D = await drive(payloadD, { calendarLink: LINK });
  const updD = callsOf(D.calls, "events.update");
  console.log("    ret =", JSON.stringify(D.ret), "| calls =", D.calls.map((c) => c.name).join(","));
  check(
    "D2. skill resolves via the link & records events.update evt_1, returns ok  <-- barrier 2",
    updD.length > 0 && updD[0].eventId === "evt_1" && D.ret?.ok === true
  );

  // ==========================================================================
  //  CASE E — GUARD: no event_id, no quoted link, no start+email.
  //  Gate now passes (reaches the skill), but the skill must NOT delete anything — it
  //  sends deleteNoMatch. Pins that relaxing the gate opened no accidental-delete hole.
  // ==========================================================================
  const payloadE = { ...base, action: "delete", event_id: null };
  const gateE = checkPayload(manifest.inputs, payloadE);
  console.log("\n[E] GUARD — target-less delete must NOT delete");
  console.log("    gate.ok =", gateE.ok, "| problems =", JSON.stringify(gateE.problems));
  check("E1. gate PASSES a target-less delete (reaches the skill)  <-- barrier 1", gateE.ok === true);
  const E = await drive(payloadE, null);
  const delE = callsOf(E.calls, "events.delete");
  console.log("    ret =", JSON.stringify(E.ret), "| calls =", E.calls.map((c) => c.name).join(","));
  check(
    "E2. skill records NO events.delete and sends deleteNoMatch (return noMatch)  <-- no accidental delete",
    delE.length === 0 && E.ret?.ok === false && E.ret?.reason === "noMatch" && failed(E.sent)
  );

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
}
