#!/usr/bin/env node
// ============================================================================
//  Self-test for CALENDAR LOCATION (physical XOR virtual) — the deterministic
//  layer of card 2b586a24, "Location for Calendar Meetings (Physical or Virtual)".
//  @assistant flow only (secretary/2. Skills/1. Calendar Actions).
//
//  Written BEFORE the code, from PLAN.md §Tests. Offline: no network, no API
//  key, no Redis, no Google credentials, no framework, no new dependency. FREE.
//
//  WHAT THIS COVERS (and what it deliberately does NOT)
//  It asserts ONLY the deterministic location layer the plan adds to the calendar
//  skill: the XOR normalizer and the pure fold/seed/wire helpers around it —
//    normalizeLocation, locationFromEvent, meetLinkOf, locationUpdateFields,
//    resolveSendUpdates
//  plus the location carry-through in the (newly exported) create/edit draft
//  functions — draftFromInfo, mergeDraft, applyDraftUpdate, editDraftFromEvent,
//  applyPatchToDraft, hasEditChange. Given a structured input each produces an
//  exact, pinnable output, so the behaviour is a regression test, not an argument.
//
//  The model's *recognition* of "make it a video call" or of a verbatim address
//  from free text is model-dependent and NOT offline-testable — that is the paid
//  live end-to-end check the plan flags (CONVENTIONS §5), not this file. Here we
//  prove the deterministic layer is correct GIVEN a good input.
//
//  HOW IT LOADS THE HELPERS
//  Dynamic import of the skill module, the same offline dynamic-import shape
//  calendar-recurrence-selftest.mjs uses (no network, no key; googleapis resolves
//  from secretary/node_modules). Every function below is exported for exactly this
//  purpose (the six pure helpers are new; the five draft functions gain `export`).
//
//  EXPECTED STATE TODAY: this script FAILS. The feature does not exist yet — the
//  new exports are `undefined` and the existing draft functions neither carry nor
//  seed location — so every presence check and every behaviour assertion fails.
//  That is the correct pre-implementation state; the Coding column makes it green.
//
//  Run:  node scripts/calendar-location-selftest.mjs
// ============================================================================
const CAL = await import(
  new URL("../secretary/2. Skills/1. Calendar Actions/skill.js", import.meta.url).href
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
// rather than the first undefined call throwing and hiding the rest. A missing export
// returns a sentinel string; reading `.field` off it yields undefined, never a crash.
function call(fn, ...args) {
  try {
    if (typeof CAL[fn] !== "function") return `MISSING_EXPORT:${fn}`;
    return CAL[fn](...args);
  } catch (e) {
    return `THREW: ${e.message}`;
  }
}

// The rails' public API to the skill, faked to the minimum draftFromInfo/mergeDraft/
// applyDraftUpdate read: `const { owner, contact } = ctx;`.
const ctx = { owner: "Marcelo", contact: null };

// ---------------------------------------------------------------------------
// 0. The exports must exist. If these fail, the feature is simply absent — that
//    is the "fails for the right reason" signal. Six new pure helpers, plus the
//    five existing draft functions the plan newly EXPORTS for this selftest.
// ---------------------------------------------------------------------------
console.log("\n=== 0. the exports exist (feature present) ===\n");
for (const fn of [
  "normalizeLocation",
  "locationFromEvent",
  "meetLinkOf",
  "locationInsertBody",
  "locationUpdateFields",
  "resolveSendUpdates",
  "draftFromInfo",
  "mergeDraft",
  "applyDraftUpdate",
  "editDraftFromEvent",
  "applyPatchToDraft",
  "hasEditChange",
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
// 3. Carry-through (edge #11). Location rides the draft exactly like all_day /
//    recurrence: a location-less merge KEEPS it; the review echo keeps it and a
//    null review CLEARS it (direct read).
// ---------------------------------------------------------------------------
console.log("\n=== 3. carry-through — mergeDraft / applyDraftUpdate ===\n");
const prev = {
  title: "Lunch",
  participants: [{ name: "John", email: "john@example.com", noEmail: false }],
  start_iso: "2026-07-20T12:00:00-03:00",
  duration_min: 60,
  all_day: false,
  all_day_end_iso: null,
  summary: "",
  recurrence: null,
  location: "Rua Prev 45",
  virtual: false,
};
{
  // A resolver patch that says nothing about location must not drop it.
  const d = call("mergeDraft", ctx, prev, { start_iso: "2026-07-20T13:00:00-03:00" });
  check(
    "3a. mergeDraft with a location-less patch KEEPS prev.location/virtual",
    d && d.location === "Rua Prev 45" && d.virtual === false,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  // Review ECHOES the current location on a non-clearing modify -> kept.
  const d = call("applyDraftUpdate", ctx, prev, { location: "Rua Prev 45", virtual: false });
  check(
    "3b. applyDraftUpdate echoing current location KEEPS it (direct read)",
    d && d.location === "Rua Prev 45" && d.virtual === false,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  // Review returns null location -> CLEAR (the recurrence-style direct read: null=clear).
  const d = call("applyDraftUpdate", ctx, prev, { location: null, virtual: false });
  check(
    "3c. applyDraftUpdate with review.location:null CLEARS it -> { location:null, virtual:false }",
    d && d.location === null && d.virtual === false,
    d && { location: d.location, virtual: d.virtual }
  );
}

// ---------------------------------------------------------------------------
// 4. Seed from a real event — locationFromEvent / editDraftFromEvent read the
//    current state; meetLinkOf surfaces the Join URL (hangoutLink first, else the
//    video entryPoint uri, else null).
// ---------------------------------------------------------------------------
console.log("\n=== 4. seed from event — locationFromEvent / editDraftFromEvent / meetLinkOf ===\n");
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
    "4a. locationFromEvent(physical) -> { location:'Rua X 123', virtual:false }",
    r && r.location === "Rua X 123" && r.virtual === false,
    r
  );
}
{
  const r = call("locationFromEvent", meetEvent);
  check(
    "4b. locationFromEvent(Meet) -> { location:null, virtual:true }",
    r && r.location === null && r.virtual === true,
    r
  );
}
{
  const d = call("editDraftFromEvent", physEvent);
  check(
    "4c. editDraftFromEvent(physical) SEEDS { location:'Rua X 123', virtual:false }",
    d && d.location === "Rua X 123" && d.virtual === false,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  const d = call("editDraftFromEvent", meetEvent);
  check(
    "4d. editDraftFromEvent(Meet) SEEDS { location:null, virtual:true } and notify:false",
    d && d.location === null && d.virtual === true && d.notify === false,
    d && { location: d.location, virtual: d.virtual, notify: d.notify }
  );
}
{
  const r = call("meetLinkOf", meetEvent);
  check(
    "4e. meetLinkOf falls back to the video entryPoint uri when no hangoutLink",
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
    "4f. meetLinkOf prefers ev.hangoutLink over the entryPoint uri",
    r === "https://meet.google.com/aaa-bbbb-ccc",
    r
  );
}
{
  const r = call("meetLinkOf", physEvent);
  check("4g. meetLinkOf(non-conference event) -> null", r === null, r);
}

// ---------------------------------------------------------------------------
// 5. Fold (applyPatchToDraft) — XOR + THE-RULE discipline. Bare new_virtual:false
//    is IGNORED (turning virtual off means giving an address). notify_guests sets
//    draft.notify and is NOT counted as a change by hasEditChange.
// ---------------------------------------------------------------------------
console.log("\n=== 5. fold — applyPatchToDraft / hasEditChange ===\n");
const virtualDraft = {
  title: "Standup",
  start_iso: "2026-07-20T09:00:00-03:00",
  duration_min: 30,
  all_day: false,
  all_day_end_iso: null,
  summary: "",
  emails: [],
  location: null,
  virtual: true,
  notify: false,
};
const physicalDraft = {
  title: "Dentist",
  start_iso: "2026-07-20T10:00:00-03:00",
  duration_min: 60,
  all_day: false,
  all_day_end_iso: null,
  summary: "",
  emails: [],
  location: "Rua X 123",
  virtual: false,
  notify: false,
};
{
  // new_location on a VIRTUAL draft -> physical, virtual dropped (address wins over Meet).
  const d = call("applyPatchToDraft", virtualDraft, { new_location: "Rua Y 99" });
  check(
    "5a. {new_location} on a virtual draft -> physical, virtual:false",
    d && d.location === "Rua Y 99" && d.virtual === false,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  // new_virtual:true on a PHYSICAL draft -> virtual, address dropped (Meet wins).
  const d = call("applyPatchToDraft", physicalDraft, { new_virtual: true });
  check(
    "5b. {new_virtual:true} on a physical draft -> virtual, location:null",
    d && d.location === null && d.virtual === true,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  const d = call("applyPatchToDraft", physicalDraft, { remove_location: true });
  check(
    "5c. {remove_location:true} clears BOTH -> { location:null, virtual:false }",
    d && d.location === null && d.virtual === false,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  // A BARE new_virtual:false is ignored: it does not silently strip an existing Meet.
  const d = call("applyPatchToDraft", virtualDraft, { new_virtual: false });
  check(
    "5d. bare {new_virtual:false} is IGNORED -> the virtual draft is unchanged",
    d && d.location === null && d.virtual === true,
    d && { location: d.location, virtual: d.virtual }
  );
}
{
  const d = call("applyPatchToDraft", physicalDraft, { notify_guests: true });
  check(
    "5e. {notify_guests:true} sets draft.notify === true",
    d && d.notify === true,
    d && { notify: d.notify }
  );
}
{
  // notify_guests alone is NOT an edit; a real location change IS.
  check("5f. hasEditChange({notify_guests:true}) === false (not a change)",
    call("hasEditChange", { notify_guests: true }) === false);
  check("5g. hasEditChange({new_location:'Rua Y'}) === true",
    call("hasEditChange", { new_location: "Rua Y" }) === true);
  check("5h. hasEditChange({new_virtual:true}) === true",
    call("hasEditChange", { new_virtual: true }) === true);
  check("5i. hasEditChange({remove_location:true}) === true",
    call("hasEditChange", { remove_location: true }) === true);
}

// ---------------------------------------------------------------------------
// 6. locationUpdateFields(draft, base, seed) — the events.update fragment, plus
//    whether conferenceDataVersion:1 is needed. Nit C (conditional version), Nit D
//    (Meet-clear on virtual->physical), edges #2 (idempotent) / #3 (XOR switch).
// ---------------------------------------------------------------------------
console.log("\n=== 6. locationUpdateFields — the five update branches (Nits C & D) ===\n");
{
  // (1) already virtual -> virtual: idempotent no-op; NO conferenceDataVersion so
  //     Google leaves the live Meet untouched.
  const r = call("locationUpdateFields", { virtual: true, location: null }, { virtual: true, location: null }, "evt1");
  const empty = r && r.fields && Object.keys(r.fields).length === 0;
  check(
    "6a. virtual -> virtual (no-op): fields === {} and conferenceVersion === false",
    empty && r.conferenceVersion === false,
    r
  );
}
{
  // (2) physical -> virtual: provision a Meet with a deterministic requestId, drop address.
  const r = call("locationUpdateFields", { virtual: true, location: null }, { virtual: false, location: "Rua X" }, "evt2");
  const req = r && r.fields && r.fields.conferenceData && r.fields.conferenceData.createRequest;
  check(
    "6b. physical -> virtual: createRequest present with requestId 'meet-evt2', location '', version true",
    req && req.requestId === "meet-evt2" && r.fields.location === "" && r.conferenceVersion === true,
    r
  );
}
{
  // (3) virtual -> physical: Nit D — clear the stale Meet (conferenceData:null) + version:1,
  //     write the new address.
  const r = call("locationUpdateFields", { virtual: false, location: "Rua Z 7" }, { virtual: true, location: null }, "evt3");
  check(
    "6c. virtual -> physical (Nit D): fields.conferenceData === null, location 'Rua Z 7', version true",
    r && r.fields && r.fields.conferenceData === null && r.fields.location === "Rua Z 7" && r.conferenceVersion === true,
    r
  );
}
{
  // (4) physical -> physical, SAME address: a non-location edit disturbs nothing (Nit C).
  const r = call("locationUpdateFields", { virtual: false, location: "Rua X" }, { virtual: false, location: "Rua X" }, "evt4");
  const empty = r && r.fields && Object.keys(r.fields).length === 0;
  check(
    "6d. non-location edit (Nit C): fields === {} and conferenceVersion === false",
    empty && r.conferenceVersion === false,
    r
  );
}
{
  // (5) physical -> physical, CHANGED address: set the address, no conference involved.
  const r = call("locationUpdateFields", { virtual: false, location: "Rua New 1" }, { virtual: false, location: "Rua Old 2" }, "evt5");
  check(
    "6e. changed address: fields.location 'Rua New 1', no conferenceData, version false",
    r && r.fields && r.fields.location === "Rua New 1" &&
      !("conferenceData" in r.fields) && r.conferenceVersion === false,
    r
  );
}

// ---------------------------------------------------------------------------
// 7. resolveSendUpdates(draft, base) — Nit A. "all" if any NON-location field
//    differs from the seed; else "all" when draft.notify; else "none". A silent
//    location-only edit does not spam the guests unless the owner asks.
// ---------------------------------------------------------------------------
console.log("\n=== 7. resolveSendUpdates — Nit A (notify only when asked) ===\n");
const base = {
  title: "Meeting",
  start_iso: "2026-07-20T10:00:00-03:00",
  duration_min: 60,
  all_day: false,
  all_day_end_iso: null,
  summary: "Agenda",
  emails: ["a@example.com", "b@example.com"],
  location: "Rua Base 10",
  virtual: false,
  notify: false,
};
{
  // Location-only change, owner did NOT ask to notify -> silent.
  const d = { ...base, location: "Rua Moved 22", notify: false };
  check("7a. location-only + notify:false -> 'none'",
    call("resolveSendUpdates", d, base) === "none", call("resolveSendUpdates", d, base));
}
{
  // Location-only change, owner ASKED to notify -> notify all.
  const d = { ...base, location: "Rua Moved 22", notify: true };
  check("7b. location-only + notify:true -> 'all'",
    call("resolveSendUpdates", d, base) === "all", call("resolveSendUpdates", d, base));
}
{
  // A substantive non-location change always notifies, even with notify:false.
  const d = { ...base, summary: "New agenda", notify: false };
  check("7c. summary differs -> 'all' (agenda is substantive)",
    call("resolveSendUpdates", d, base) === "all", call("resolveSendUpdates", d, base));
}
{
  const d = { ...base, start_iso: "2026-07-20T11:00:00-03:00", notify: false };
  check("7d. start_iso differs -> 'all'",
    call("resolveSendUpdates", d, base) === "all", call("resolveSendUpdates", d, base));
}
{
  const d = { ...base, emails: ["a@example.com", "b@example.com", "c@example.com"], notify: false };
  check("7e. attendee set differs -> 'all'",
    call("resolveSendUpdates", d, base) === "all", call("resolveSendUpdates", d, base));
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
