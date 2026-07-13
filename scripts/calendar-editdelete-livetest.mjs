#!/usr/bin/env node
// ============================================================================
//  CLOSING THE EDIT/DELETE GAP — card 9af6967a.
//
//  edit and delete are ~⅓ of the owner's real calendar traffic and they have gone UNTESTED
//  against the real Google API through three experiments — because a realistic fixture needs
//  a real event, and creating one risked EMAILING SOMEBODY.
//
//  The offline half is already covered: scripts/turn-latency-selftest.mjs T3.3/T3.4 drive the
//  skill's whole edit and delete flows (quoted invite -> confirm card -> "sim" -> events.patch
//  / events.delete) against a recording stub. What THAT cannot prove is that the real Google
//  Calendar API accepts and performs the wire shapes the skill sends. This script is that half,
//  and it is the only thing here that touches the owner's real calendar.
//
//  ⚠ THE AUTHORIZATION, IN FULL, AND IT IS THE WHOLE OF IT:
//
//      ONE event may be created, edited and deleted, with **NO ATTENDEES** — which makes an
//      invite email STRUCTURALLY IMPOSSIBLE.
//
//  NEVER message a third party. NEVER email an invite. NEVER attach an attendee to a test
//  event. With zero attendees, `sendUpdates: "all"` has nobody to mail: the safety is
//  structural, not a promise. `assertNobodyCanBeEmailed()` below THROWS before any Google call
//  that carries even one attendee, and it is self-tested offline, on every run, for free.
//
//  🛑 NOBODY RUNS THE LIVE HALF ON THEIR OWN INITIATIVE. Like scripts/router-selftest.mjs, it
//  costs money and it writes to the owner's real calendar: THE HUMAN'S CALL, surfaced through
//  the manager. It refuses to run without CAL_LIVE_WRITE=1 AND real Google credentials.
//
//  It NEVER sends a WhatsApp message and never touches Evolution.
//
//  Run:
//    node scripts/calendar-editdelete-livetest.mjs
//        SAFE BY DEFAULT. Runs the offline guard self-test, then REFUSES to go live. Free.
//    CAL_LIVE_WRITE=1 GOOGLE_CLIENT_ID=… GOOGLE_CLIENT_SECRET=… GOOGLE_REFRESH_TOKEN=… \
//      node scripts/calendar-editdelete-livetest.mjs
//        THE REAL RUN. Creates -> edits -> deletes ONE attendee-less event on the owner's
//        real calendar, and verifies it is gone. The human's call.
// ============================================================================
import { createRequire } from "node:module";

const CAL_TZ = "America/Sao_Paulo"; // mirrors skill.js
const TITLE = "SELFTEST — delete me";
const LIVE = process.env.CAL_LIVE_WRITE === "1";

let failures = 0;
const check = (name, cond) => {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
  return cond;
};

// ============================================================================
//  THE SAFETY RAIL. It is code, it runs before every Google call, and it throws.
//  A test that PROMISES not to email anyone is worth nothing. This one CANNOT.
// ============================================================================
function assertNobodyCanBeEmailed(requestBody, where) {
  const attendees = requestBody?.attendees;
  if (Array.isArray(attendees) && attendees.length)
    throw new Error(
      `REFUSING (${where}): this fixture may never invite anyone. ` +
        `${attendees.length} attendee(s) found: ${JSON.stringify(attendees)}`
    );
  return requestBody;
}

// ---- the guard's own self-test. Offline, free, and it runs on EVERY invocation ------------
console.log("\n=== the safety rail — self-tested before anything else can happen ===\n");
const threwOn = (body) => {
  try {
    assertNobodyCanBeEmailed(body, "selftest");
    return false;
  } catch {
    return true;
  }
};
check("G1  a body carrying ONE attendee THROWS — an invite is structurally impossible",
  threwOn({ summary: TITLE, attendees: [{ email: "someone@example.com" }] }));
check("G2  a body carrying SEVERAL attendees THROWS",
  threwOn({ summary: TITLE, attendees: [{ email: "a@x.com" }, { email: "b@x.com" }] }));
check("G3  an attendee-less body is allowed through (attendees: [])",
  !threwOn({ summary: TITLE, attendees: [] }));
check("G4  an attendee-less body is allowed through (no attendees key at all)",
  !threwOn({ summary: TITLE }));

if (failures) {
  console.error("\nFAIL — the safety rail itself is broken. Refusing to go anywhere near Google.\n");
  process.exit(1);
}

// ============================================================================
//  THE GATE. Everything below writes to the owner's REAL calendar.
// ============================================================================
const creds = {
  GOOGLE_CLIENT_ID: process.env.GOOGLE_CLIENT_ID,
  GOOGLE_CLIENT_SECRET: process.env.GOOGLE_CLIENT_SECRET,
  GOOGLE_REFRESH_TOKEN: process.env.GOOGLE_REFRESH_TOKEN,
  GOOGLE_CALENDAR_ID: process.env.GOOGLE_CALENDAR_ID || "primary",
};
const missing = Object.entries(creds)
  .filter(([k, v]) => k !== "GOOGLE_CALENDAR_ID" && !v)
  .map(([k]) => k);

if (!LIVE || missing.length) {
  console.log(
    "\nNOT RUNNING THE LIVE HALF — and that is the default.\n" +
      (LIVE ? "" : "  · CAL_LIVE_WRITE is not 1\n") +
      (missing.length ? `  · missing Google credentials: ${missing.join(", ")}\n` : "") +
      "\nThis script writes ONE attendee-less event to the owner's REAL Google Calendar and then\n" +
      "deletes it. It costs money and it is THE HUMAN'S CALL — surface it through the manager,\n" +
      "never run it on your own initiative.\n\n" +
      "  CAL_LIVE_WRITE=1 GOOGLE_CLIENT_ID=… GOOGLE_CLIENT_SECRET=… GOOGLE_REFRESH_TOKEN=… \\\n" +
      "    node scripts/calendar-editdelete-livetest.mjs\n\n" +
      "PASS — the safety rail is green; nothing was written.\n"
  );
  process.exit(0);
}

// googleapis lives in secretary/node_modules, so a bare import from scripts/ throws
// ERR_MODULE_NOT_FOUND. Same trick as scripts/tasks-addressed-selftest.mjs.
const require = createRequire(new URL("../secretary/package.json", import.meta.url));
const { google } = require("googleapis");

const auth = new google.auth.OAuth2(creds.GOOGLE_CLIENT_ID, creds.GOOGLE_CLIENT_SECRET);
auth.setCredentials({ refresh_token: creds.GOOGLE_REFRESH_TOKEN });
const cal = google.calendar({ version: "v3", auth });
const calendarId = creds.GOOGLE_CALENDAR_ID;

// Far enough out that it cannot collide with anything real, and obviously disposable.
const START = "2030-01-15T15:00:00-03:00";
const END = "2030-01-15T16:00:00-03:00";
const NEW_START = "2030-01-15T17:00:00-03:00";
const NEW_END = "2030-01-15T18:00:00-03:00";

let eventId = null;
try {
  // ---- CREATE — the exact wire shape skill.js createEvent() sends, minus the attendees ----
  console.log("\n=== LIVE: create -> edit -> delete, ONE event, ZERO attendees ===\n");
  const createBody = assertNobodyCanBeEmailed(
    {
      summary: TITLE,
      description: "Created by scripts/calendar-editdelete-livetest.mjs. Safe to delete.",
      start: { dateTime: START, timeZone: CAL_TZ },
      end: { dateTime: END, timeZone: CAL_TZ },
      attendees: [], // ← nobody. sendUpdates:"all" has nobody to mail.
    },
    "create"
  );
  const created = await cal.events.insert({ calendarId, sendUpdates: "all", requestBody: createBody });
  eventId = created.data.id;
  console.log(`   created: ${eventId}  "${created.data.summary}"  ${created.data.start?.dateTime}`);
  check("L1  CREATE — Google accepted the insert and returned a confirmed event",
    !!eventId && created.data.status === "confirmed");
  check("L2  CREATE — the created event has NO attendees (nobody was, or could be, emailed)",
    (created.data.attendees || []).length === 0);

  // ---- EDIT — the exact wire shape skill.js patchEvent() sends ----------------------------
  const patchBody = assertNobodyCanBeEmailed(
    { start: { dateTime: NEW_START, timeZone: CAL_TZ }, end: { dateTime: NEW_END, timeZone: CAL_TZ } },
    "patch"
  );
  const patched = await cal.events.patch({ calendarId, eventId, sendUpdates: "all", requestBody: patchBody });
  console.log(`   patched: ${patched.data.start?.dateTime}`);
  check("L3  EDIT — the patch MOVED the event, and Google agrees on the new start",
    new Date(patched.data.start?.dateTime).getTime() === new Date(NEW_START).getTime());
  check("L4  EDIT — it still has no attendees", (patched.data.attendees || []).length === 0);

  // ---- READ BACK — getEvent(), the call matchEventTargets() leans on for edit and delete ---
  const got = await cal.events.get({ calendarId, eventId });
  check("L5  READ — events.get returns the CONFIRMED event (this is how edit/delete find it)",
    got.data.id === eventId && got.data.status === "confirmed");

  // ---- DELETE — the exact wire shape skill.js cancelMeeting() sends ------------------------
  await cal.events.delete({ calendarId, eventId, sendUpdates: "all" });
  console.log(`   deleted: ${eventId}`);

  // ---- VERIFY IT IS ACTUALLY GONE. "delete returned 200" is not the same as "it is gone." --
  let gone = false;
  try {
    const after = await cal.events.get({ calendarId, eventId });
    gone = after.data.status === "cancelled";
  } catch (e) {
    gone = e?.code === 404 || e?.code === 410;
  }
  check("L6  DELETE — the event is GONE from the calendar (404/410, or status cancelled)", gone);
  eventId = gone ? null : eventId;
} catch (e) {
  console.error(`\n  FAIL  the live run threw: ${e?.message || e}`);
  failures++;
} finally {
  // Never leave litter on the owner's real calendar, whatever went wrong above.
  if (eventId) {
    try {
      await cal.events.delete({ calendarId, eventId, sendUpdates: "all" });
      console.log(`   cleanup: deleted ${eventId}`);
    } catch (e) {
      console.error(`   ⚠ CLEANUP FAILED — delete "${TITLE}" by hand: ${eventId} (${e?.message || e})`);
      failures++;
    }
  }
}

console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);
process.exit(failures === 0 ? 0 : 1);
