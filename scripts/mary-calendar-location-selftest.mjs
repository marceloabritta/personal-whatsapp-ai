#!/usr/bin/env node
// ============================================================================
//  Self-test for CALENDAR LOCATION (physical XOR virtual) — the @MARY flow
//  (secretary/3. Mary Skills/1. Calendar Actions). Card 2b9b2952,
//  "Location on calendar events on @Mary flow".
//
//  This is the @mary adaptation of scripts/calendar-location-selftest.mjs (which
//  pins the @assistant flow). @mary was migrated to an isolated pure-task
//  READ-then-ACT tree, so the mechanism differs: the ORCHESTRATOR runs the
//  dialogue, there is NO in-skill session / propose / confirm, and the edit is a
//  single-pass field overlay ("absent = keep"). So the @assistant-only surface —
//  mergeDraft / applyDraftUpdate / applyPatchToDraft / hasEditChange /
//  resolveSendUpdates (Nit A) — does NOT exist here and is deliberately dropped.
//  @mary always writes sendUpdates:"all"; there is no notify concept.
//
//  Written BEFORE the code, from PLAN.md §The test. Offline: no network, no API
//  key, no Redis, no Google credentials, no framework, no new dependency. FREE.
//
//  WHAT THIS COVERS (and what it deliberately does NOT)
//  It asserts ONLY the deterministic location layer the port adds to @mary's
//  calendar skill: the five pure helpers —
//    normalizeLocation, locationFromEvent, meetLinkOf, locationInsertBody,
//    locationUpdateFields
//  plus the location carry-through in the (newly exported) create/edit draft
//  seeds — draftFromInfo, editDraftFromEvent. Given a structured input each
//  produces an exact, pinnable output, so the behaviour is a regression test.
//
//  The model's *recognition* of "make it a video call" or of a verbatim address
//  from free text is model-dependent and NOT offline-testable — that is the paid
//  live end-to-end check the plan flags, not this file. Here we prove the
//  deterministic layer is correct GIVEN a good input.
//
//  EXPECTED STATE BEFORE THE CODE: this script FAILS. The five helpers are not
//  exported yet (undefined) and the draft seeds carry no location, so every
//  presence check and every behaviour assertion fails. That is the correct
//  pre-implementation red; the Coding column makes it green.
//
//  Run:  node scripts/mary-calendar-location-selftest.mjs
// ============================================================================
const CAL = await import(
  new URL("../secretary/3. Mary Skills/1. Calendar Actions/skill.js", import.meta.url).href
);

let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) {
    failures++;
    if (detail !== undefined) console.log(`          got: ${JSON.stringify(detail)}`);
  }
}

// Call a (possibly-missing) export without letting a TypeError abort the run, so that
// when the feature is absent EVERY assertion reports FAIL cleanly with a full count,
// rather than the first undefined call throwing and hiding the rest.
function call(fn, ...args) {
  try {
    if (typeof CAL[fn] !== "function") return `MISSING_EXPORT:${fn}`;
    return CAL[fn](...args);
  } catch (e) {
    return `THREW: ${e.message}`;
  }
}

// draftFromInfo reads `const { owner, contact } = ctx;` — the minimum ctx it needs.
const ctx = { owner: "Marcelo", contact: null };

// ---------------------------------------------------------------------------
// 0. The exports must exist. If these fail, the feature is simply absent — that
//    is the "fails for the right reason" signal. Five new pure helpers, plus the
//    two draft seeds the port newly EXPORTS for this selftest.
// ---------------------------------------------------------------------------
console.log("\n=== 0. the exports exist (feature present) ===\n");
for (const fn of [
  "normalizeLocation",
  "locationFromEvent",
  "meetLinkOf",
  "locationInsertBody",
  "locationUpdateFields",
  "draftFromInfo",
  "editDraftFromEvent",
]) {
  check(`0. skill exports ${fn}()`, typeof CAL[fn] === "function");
}

// ---------------------------------------------------------------------------
// 1. normalizeLocation — physical XOR virtual, verbatim trim. The single normalizer.
//    virtual wins; a non-empty address means physical; everything else = no location.
// ---------------------------------------------------------------------------
console.log("\n=== 1. normalizeLocation — XOR + verbatim trim ===\n");
{
  const r = call("normalizeLocation", "Rua X", null);
  check(
    "1a. (address, null) -> physical { location:'Rua X', virtual:false }",
    r && r.location === "Rua X" && r.virtual === false,
    r
  );
}
{
  const r = call("normalizeLocation", "Rua X", true);
  check(
    "1b. (address, true) -> VIRTUAL WINS { location:null, virtual:true }",
    r && r.location === null && r.virtual === true,
    r
  );
}
{
  const r = call("normalizeLocation", "   ", false);
  check(
    "1c. (whitespace-only, false) -> no location { location:null, virtual:false }",
    r && r.location === null && r.virtual === false,
    r
  );
}
{
  const r = call("normalizeLocation", null, false);
  check(
    "1d. (null, false) -> no location { location:null, virtual:false }",
    r && r.location === null && r.virtual === false,
    r
  );
}
{
  // Outer whitespace trimmed, inner text preserved VERBATIM (no lookup, no reformat).
  const r = call("normalizeLocation", "  Casa da  Vovó  ", null);
  check(
    "1e. verbatim: outer trimmed, inner double-space kept -> 'Casa da  Vovó'",
    r && r.location === "Casa da  Vovó" && r.virtual === false,
    r
  );
}

// ---------------------------------------------------------------------------
// 2. draftFromInfo — the single create-side normalizer. An info carrying BOTH an
//    address and virtual:true must funnel through the XOR: virtual wins.
// ---------------------------------------------------------------------------
console.log("\n=== 2. draftFromInfo — XOR + store ===\n");
{
  const d = call("draftFromInfo", ctx, {
    title: "Sync",
    participants: [],
    start_iso: "2026-07-20T15:00:00-03:00",
    duration_min: 30,
    location: "Rua X 123",
    virtual: true,
  });
  check(
    "2. draftFromInfo({location, virtual:true}) -> { location:null, virtual:true }",
    d && d.location === null && d.virtual === true,
    d && { location: d.location, virtual: d.virtual }
  );
}

// ---------------------------------------------------------------------------
// 3. Seed from a real event — locationFromEvent / editDraftFromEvent read the
//    current state; meetLinkOf surfaces the Join URL (hangoutLink first, else the
//    video entryPoint uri, else null).
// ---------------------------------------------------------------------------
console.log("\n=== 3. seed from event — locationFromEvent / editDraftFromEvent / meetLinkOf ===\n");
const physEvent = {
  summary: "Dentist",
  location: "Rua X 123",
  start: { dateTime: "2026-07-20T10:00:00-03:00" },
  end: { dateTime: "2026-07-20T11:00:00-03:00" },
  attendees: [],
};
// A Meet event with conferenceData but NO hangoutLink — meetLinkOf falls back to the
// video entryPoint uri (edge #8: Google may not have populated hangoutLink yet).
const meetEvent = {
  summary: "Standup",
  start: { dateTime: "2026-07-20T09:00:00-03:00" },
  end: { dateTime: "2026-07-20T09:30:00-03:00" },
  attendees: [],
  conferenceData: {
    conferenceId: "abc",
    entryPoints: [
      { entryPointType: "video", uri: "https://meet.google.com/xyz-1234-abc" },
    ],
  },
};
{
  const r = call("locationFromEvent", physEvent);
  check(
    "3a. locationFromEvent(physical) -> { location:'Rua X 123', virtual:false }",
    r && r.location === "Rua X 123" && r.virtual === false,
    r
  );
}
{
  const r = call("locationFromEvent", meetEvent);
  check(
    "3b. locationFromEvent(Meet) -> { location:null, virtual:true }",
    r && r.location === null && r.virtual === true,
    r
  );
}
{
  const d = call("editDraftFromEvent", physEvent);
  check(
    "3c. editDraftFromEvent(physical) SEEDS { location:'Rua X 123', virtual:false }",
    d && d.location === "Rua X 123" && d.virtual === false,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  const d = call("editDraftFromEvent", meetEvent);
  check(
    "3d. editDraftFromEvent(Meet) SEEDS { location:null, virtual:true }",
    d && d.location === null && d.virtual === true,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  const r = call("meetLinkOf", meetEvent);
  check(
    "3e. meetLinkOf falls back to the video entryPoint uri when no hangoutLink",
    r === "https://meet.google.com/xyz-1234-abc",
    r
  );
}
{
  // hangoutLink takes precedence over the entryPoint uri.
  const r = call("meetLinkOf", {
    hangoutLink: "https://meet.google.com/aaa-bbbb-ccc",
    conferenceData: {
      entryPoints: [{ entryPointType: "video", uri: "https://meet.google.com/other" }],
    },
  });
  check(
    "3f. meetLinkOf prefers ev.hangoutLink over the entryPoint uri",
    r === "https://meet.google.com/aaa-bbbb-ccc",
    r
  );
}
{
  const r = call("meetLinkOf", physEvent);
  check("3g. meetLinkOf(non-conference event) -> null", r === null, r);
}

// ---------------------------------------------------------------------------
// 4. locationInsertBody({location, virtual, seed}) — the events.insert fragment,
//    plus whether conferenceDataVersion:1 is needed. Physical adds `location`;
//    virtual provisions a Meet with a DETERMINISTIC requestId 'meet-<seed>';
//    neither adds NO key (byte-identical to today's write).
// ---------------------------------------------------------------------------
console.log("\n=== 4. locationInsertBody — physical / virtual / neither ===\n");
{
  const r = call("locationInsertBody", { location: "Rua X 1", virtual: false, seed: "s1" });
  check(
    "4a. physical: body { location:'Rua X 1' }, conferenceVersion false",
    r && r.body && r.body.location === "Rua X 1" && !("conferenceData" in r.body) &&
      r.conferenceVersion === false,
    r
  );
}
{
  const r = call("locationInsertBody", { location: null, virtual: true, seed: "s2" });
  const req = r && r.body && r.body.conferenceData && r.body.conferenceData.createRequest;
  check(
    "4b. virtual: createRequest requestId 'meet-s2', conferenceVersion true, no location key",
    req && req.requestId === "meet-s2" && !("location" in r.body) && r.conferenceVersion === true,
    r
  );
}
{
  const r = call("locationInsertBody", { location: null, virtual: false, seed: "s3" });
  const empty = r && r.body && Object.keys(r.body).length === 0;
  check(
    "4c. neither: body {} (no key added), conferenceVersion false",
    empty && r.conferenceVersion === false,
    r
  );
}

// ---------------------------------------------------------------------------
// 5. locationUpdateFields(draft, base, seed) — the events.update fragment, plus
//    whether conferenceDataVersion:1 is needed. Nit C (conditional version), Nit D
//    (Meet-clear on virtual->physical), edges #2 (idempotent) / #3 (XOR switch).
//    The five branches.
// ---------------------------------------------------------------------------
console.log("\n=== 5. locationUpdateFields — the five update branches (Nits C & D) ===\n");
{
  // (1) already virtual -> virtual: idempotent no-op; NO conferenceDataVersion so
  //     Google leaves the live Meet untouched.
  const r = call("locationUpdateFields", { virtual: true, location: null }, { virtual: true, location: null }, "evt1");
  const empty = r && r.fields && Object.keys(r.fields).length === 0;
  check(
    "5a. virtual -> virtual (no-op): fields === {} and conferenceVersion === false",
    empty && r.conferenceVersion === false,
    r
  );
}
{
  // (2) physical -> virtual: provision a Meet with a deterministic requestId, drop address.
  const r = call("locationUpdateFields", { virtual: true, location: null }, { virtual: false, location: "Rua X" }, "evt2");
  const req = r && r.fields && r.fields.conferenceData && r.fields.conferenceData.createRequest;
  check(
    "5b. physical -> virtual: createRequest present with requestId 'meet-evt2', location '', version true",
    req && req.requestId === "meet-evt2" && r.fields.location === "" && r.conferenceVersion === true,
    r
  );
}
{
  // (3) virtual -> physical: Nit D — clear the stale Meet (conferenceData:null) + version:1,
  //     write the new address.
  const r = call("locationUpdateFields", { virtual: false, location: "Rua Z 7" }, { virtual: true, location: null }, "evt3");
  check(
    "5c. virtual -> physical (Nit D): fields.conferenceData === null, location 'Rua Z 7', version true",
    r && r.fields && r.fields.conferenceData === null && r.fields.location === "Rua Z 7" && r.conferenceVersion === true,
    r
  );
}
{
  // (4) physical -> physical, SAME address: a non-location edit disturbs nothing (Nit C).
  const r = call("locationUpdateFields", { virtual: false, location: "Rua X" }, { virtual: false, location: "Rua X" }, "evt4");
  const empty = r && r.fields && Object.keys(r.fields).length === 0;
  check(
    "5d. non-location edit (Nit C): fields === {} and conferenceVersion === false",
    empty && r.conferenceVersion === false,
    r
  );
}
{
  // (5) physical -> physical, CHANGED address: set the address, no conference involved.
  const r = call("locationUpdateFields", { virtual: false, location: "Rua New 1" }, { virtual: false, location: "Rua Old 2" }, "evt5");
  check(
    "5e. changed address: fields.location 'Rua New 1', no conferenceData, version false",
    r && r.fields && r.fields.location === "Rua New 1" &&
      !("conferenceData" in r.fields) && r.conferenceVersion === false,
    r
  );
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
