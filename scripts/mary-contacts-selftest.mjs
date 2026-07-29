#!/usr/bin/env node
// ============================================================================
//  Self-test — card 3c946c4e "Google Contacts integration: look up and save
//  guest emails" (the DETERMINISTIC layer).
//
//  The feature (SCOPE / PLAN): inside 3. Mary Skills/1. Calendar Actions/skill.js,
//  before a `create` writes an event, resolve the ONE chat-counterpart guest's null
//  email from Google People (Contacts) by phone number; and after a create, additively
//  save a freshly-supplied guest email back to Contacts. This test brackets the pure,
//  offline layer that carries that feature — imported as the REAL product surfaces
//  (no network, no googleapis client, no OAuth scope, no keys, no model call):
//
//    pure helpers, exported from skill.js:
//      normalizePhone(raw)             -> digits-only string
//      phoneMatches(a, b)             -> boolean (BR 9th-digit / +55 reconciliation)
//      mergeEmails(existing, newEmail)-> { emails, changed } (NEVER overwrites)
//      counterpartParticipant(ps,ctx) -> the counterpart participant | null
//    injected-client seams, driven with a FAKE peopleClient (no googleapis):
//      resolveCounterpartEmail(peopleClient, jidNumber)
//      saveEmailBack(peopleClient, { jidNumber, name, email })
//
//  ⚠ SCOPE — WHAT THIS TEST DELIBERATELY CANNOT COVER (CONVENTIONS §5):
//  The one-vs-several-vs-none *judgement* against the LIVE People API (real contacts,
//  real BR number formats) and the model's read-back handling are MODEL/LIVE behaviour
//  and are named as human/live checks in PLAN.md — NOT asserted here. This test asserts
//  only the deterministic layer AROUND those: the pure phone/merge/counterpart logic
//  and the two seams driven by a dependency-injected fake client.
//
//  TWO MANAGER OVERRIDES to the PLAN Tests list are applied here:
//    - Assertion 12 is IDENTITY-GATED (not "single participant → return it regardless
//      of name"): a single participant is returned ONLY when it is the chat counterpart
//      (its name matches ctx.contact / pushName); a single THIRD-PARTY participant that
//      does not match the counterpart identity → null (never fill a non-counterpart
//      with the counterpart's email — silence beats misattribution).
//    - CREATE-only: no edit-path cases (edit-event lookup is a separate follow-up card).
//
//  EXPECTED STATE TODAY (before the feature is built): every assertion FAILS because
//  the helpers/seams do not exist on skill.js yet — a namespace import leaves them
//  `undefined`, so each call throws "… is not a function" and the check records a FAIL.
//  That failure is the point of this column. After the coder adds the helpers/seams to
//  their contract, this script exits 0.
//
//  Run:  node scripts/mary-contacts-selftest.mjs
//        -> exits non-zero before the feature, exits 0 after it.
// ============================================================================
import * as skill from "../secretary/3. Mary Skills/1. Calendar Actions/skill.js";

const {
  normalizePhone,
  phoneMatches,
  mergeEmails,
  counterpartParticipant,
  resolveCounterpartEmail,
  saveEmailBack,
} = skill;

let failures = 0;
// Thunk-based so a missing helper (undefined -> "not a function") is caught and
// reported as a FAIL for THIS assertion, instead of crashing the whole run at the
// first call. Every assertion then reports independently — the evidence is per-line.
async function check(name, thunk) {
  let ok = false;
  let note = "";
  try {
    ok = await thunk();
  } catch (e) {
    ok = false;
    note = `  [threw: ${e.message}]`;
  }
  console.log(`${ok ? "  ok  " : "  FAIL"}  ${name}${ok ? "" : note}`);
  if (!ok) failures++;
}

// ============================================================================
//  Fakes — a People client shaped like google.people({version:"v1"}), and a
//  Contacts connection record shaped like people.connections.list returns.
// ============================================================================
function contact({
  resourceName = "people/c1",
  etag = "etag-1",
  name = "Ingra",
  phone = "5531933344455",
  emails = [],
} = {}) {
  return {
    resourceName,
    etag,
    names: name ? [{ displayName: name }] : [],
    phoneNumbers: phone ? [{ value: phone }] : [],
    emailAddresses: emails.map((value) => ({ value })),
    metadata: {},
  };
}

// A fake peopleClient: records every call, returns the seeded connections, and can be
// told to throw from a given method to exercise the degrade-never-block branches.
function fakeClient({ connections = [], throwOn = null } = {}) {
  const calls = { list: [], update: [], create: [] };
  return {
    calls,
    people: {
      connections: {
        list: async (params) => {
          calls.list.push(params);
          if (throwOn === "list") throw new Error("People API 500 (list)");
          return { data: { connections, nextPageToken: undefined } };
        },
      },
      updateContact: async (params) => {
        calls.update.push(params);
        if (throwOn === "update") throw new Error("People API 500 (update)");
        return { data: { ...(params.requestBody || {}), resourceName: params.resourceName } };
      },
      createContact: async (params) => {
        calls.create.push(params);
        if (throwOn === "create") throw new Error("People API 500 (create)");
        return { data: { resourceName: "people/new", ...(params.requestBody || {}) } };
      },
    },
  };
}

// Pull the email strings out of whatever updateContact/createContact was handed,
// tolerant of the standard { emailAddresses:[{value}] } shape.
function emailsOf(requestBody) {
  return ((requestBody && requestBody.emailAddresses) || []).map((e) => e.value);
}

const JID = "5531933344455"; // the counterpart's normalized number (from ctx.number)

console.log("\nmary-contacts self-test  (offline; fake People client)\n");

// ============================================================================
//  Phone normalization / matching — SCOPE edge 1
// ============================================================================
console.log("phone normalization / matching (normalizePhone, phoneMatches)");

await check(
  "1. identical digits across formatting: JID vs '+55 (31) 9 3334-4455' -> match",
  () =>
    normalizePhone("+55 (31) 9 3334-4455") === "5531933344455" &&
    phoneMatches(JID, "+55 (31) 9 3334-4455") === true
);

await check(
  "2. 9th-digit variance: 5531933344455 (with 9) vs national '31 3334-4455' (no 9, no CC) -> match",
  () => phoneMatches(JID, "31 3334-4455") === true
);

await check(
  "3. country-code present-vs-absent: 553133344455 vs 3133344455 -> match",
  () => phoneMatches("553133344455", "3133344455") === true
);

await check(
  "4. near-miss (last digit differs) -> NO match  <-- never a wrong match",
  () => phoneMatches("5531933344455", "5531933344456") === false
);

await check(
  "5. different area code (31 vs 21) -> NO match",
  () => phoneMatches("5531933344455", "5521933344455") === false
);

await check(
  "6. empty / non-numeric input -> '' and no false-positive match",
  () =>
    normalizePhone("") === "" &&
    normalizePhone(null) === "" &&
    normalizePhone("abc") === "" &&
    phoneMatches("", "") === false &&
    phoneMatches("", JID) === false
);

// ============================================================================
//  Never-overwrite email merge — SCOPE "In scope" / edge 9
// ============================================================================
console.log("\nnever-overwrite email merge (mergeEmails)");

await check(
  "7. existing ['a@x.com'] + new 'b@x.com' -> ['a@x.com','b@x.com'], changed:true, existing FIRST",
  () => {
    const r = mergeEmails(["a@x.com"], "b@x.com");
    return (
      r.changed === true &&
      r.emails.length === 2 &&
      r.emails[0] === "a@x.com" &&
      r.emails[1] === "b@x.com"
    );
  }
);

await check(
  "8. duplicate, case-insensitive: ['a@x.com'] + 'A@X.com' -> unchanged, changed:false",
  () => {
    const r = mergeEmails(["a@x.com"], "A@X.com");
    return r.changed === false && r.emails.length === 1 && r.emails[0] === "a@x.com";
  }
);

await check(
  "9. empty existing + new -> ['b@x.com'], changed:true",
  () => {
    const r = mergeEmails([], "b@x.com");
    return r.changed === true && r.emails.length === 1 && r.emails[0] === "b@x.com";
  }
);

await check(
  "10. two existing + a third new -> all three, both originals intact and in order",
  () => {
    const r = mergeEmails(["a@x.com", "b@x.com"], "c@x.com");
    return (
      r.changed === true &&
      r.emails.length === 3 &&
      r.emails[0] === "a@x.com" &&
      r.emails[1] === "b@x.com" &&
      r.emails[2] === "c@x.com"
    );
  }
);

// ============================================================================
//  Counterpart selection — SCOPE edge 3b (IDENTITY-GATED, manager override)
//  Invariant: never fill a non-counterpart with the counterpart's email.
// ============================================================================
console.log("\ncounterpart selection (counterpartParticipant) — identity-gated");

await check(
  "11. two participants, ctx.contact:'Ingra' -> returns Ingra (not Nicolle)",
  () => {
    const cp = counterpartParticipant(
      [{ name: "Ingra", email: null }, { name: "Nicolle", email: null }],
      { contact: "Ingra" }
    );
    return !!cp && cp.name === "Ingra";
  }
);

await check(
  "12a. first-name match: extracted 'Ingra' vs pushName-identity 'Ingra Silva' -> returned (feature fires); and vice-versa",
  () => {
    // The PARADIGM case: owner books 'com a Ingra', pushName is the full 'Ingra Silva'.
    const cp = counterpartParticipant([{ name: "Ingra", email: null }], { contact: "Ingra Silva" });
    // Vice-versa: a multi-token extracted name whose first name IS the single-token pushName.
    const cpRev = counterpartParticipant([{ name: "Ingra Silva", email: null }], { contact: "Ingra" });
    return !!cp && cp.name === "Ingra" && !!cpRev && cpRev.name === "Ingra Silva";
  }
);

await check(
  "12b. genuine third party: extracted 'Igor' vs pushName-identity 'Ingra Silva' -> null (no fill)",
  () => {
    // Shares NO token with the counterpart identity -> never filled (silence beats misattribution).
    const igor = counterpartParticipant([{ name: "Igor", email: null }], { contact: "Ingra Silva" });
    const bob = counterpartParticipant([{ name: "Bob", email: null }], { contact: "Ingra Silva" });
    return (igor === null || igor === undefined) && (bob === null || bob === undefined);
  }
);

await check(
  "13. multiple, none name-matches the counterpart -> null (nobody gets the counterpart's email)",
  () => {
    const cp = counterpartParticipant(
      [{ name: "Ingra", email: null }, { name: "Nicolle", email: null }],
      { contact: "Bob" }
    );
    return cp === null || cp === undefined;
  }
);

// ============================================================================
//  Seam wiring with a FAKE client — resolveCounterpartEmail
// ============================================================================
console.log("\nseam: resolveCounterpartEmail(peopleClient, jidNumber)");

await check(
  "14. one matching contact carrying one email -> {status:'one', emails:[that one]}",
  async () => {
    const client = fakeClient({
      connections: [contact({ phone: JID, emails: ["ingra@x.com"] })],
    });
    const r = await resolveCounterpartEmail(client, JID);
    return r.status === "one" && r.emails.length === 1 && r.emails[0] === "ingra@x.com";
  }
);

await check(
  "15. matching contact with two emails -> {status:'several'}",
  async () => {
    const client = fakeClient({
      connections: [contact({ phone: JID, emails: ["ingra@x.com", "ingra.work@x.com"] })],
    });
    const r = await resolveCounterpartEmail(client, JID);
    return r.status === "several" && r.emails.length >= 2;
  }
);

await check(
  "16a. no phone match -> {status:'none'}",
  async () => {
    const client = fakeClient({
      connections: [contact({ phone: "5521988887777", emails: ["someone@x.com"] })],
    });
    const r = await resolveCounterpartEmail(client, JID);
    return r.status === "none" && Array.isArray(r.emails) && r.emails.length === 0;
  }
);

await check(
  "16b. connections.list THROWS -> {status:'none'} (degrade, never blocks the calendar action)",
  async () => {
    const client = fakeClient({ throwOn: "list" });
    const r = await resolveCounterpartEmail(client, JID);
    return r.status === "none";
  }
);

// ============================================================================
//  Seam wiring with a FAKE client — saveEmailBack
// ============================================================================
console.log("\nseam: saveEmailBack(peopleClient, { jidNumber, name, email })");

await check(
  "17. contact present, NEW email -> updateContact called with MERGED emails + etag; {saved:true,created:false}",
  async () => {
    const client = fakeClient({
      connections: [
        contact({ resourceName: "people/c1", etag: "etag-1", phone: JID, emails: ["old@x.com"] }),
      ],
    });
    const r = await saveEmailBack(client, { jidNumber: JID, name: "Ingra", email: "new@x.com" });
    const call = client.calls.update[0];
    const merged = call ? emailsOf(call.requestBody) : [];
    return (
      r.saved === true &&
      r.created === false &&
      client.calls.update.length === 1 &&
      client.calls.create.length === 0 &&
      merged.includes("old@x.com") && // existing preserved (never overwrites)
      merged.includes("new@x.com") &&
      (call.requestBody && call.requestBody.etag) === "etag-1" // the fetched etag
    );
  }
);

await check(
  "18. no contact for that number -> createContact called with name/phone/email; {saved:true,created:true}",
  async () => {
    const client = fakeClient({ connections: [] });
    const r = await saveEmailBack(client, { jidNumber: JID, name: "Ingra", email: "new@x.com" });
    const call = client.calls.create[0];
    const createdEmails = call ? emailsOf(call.requestBody) : [];
    return (
      r.saved === true &&
      r.created === true &&
      client.calls.create.length === 1 &&
      client.calls.update.length === 0 &&
      createdEmails.includes("new@x.com")
    );
  }
);

await check(
  "19. email already on the contact -> no updateContact call; {saved:false, reason:'duplicate'}",
  async () => {
    const client = fakeClient({
      connections: [contact({ phone: JID, emails: ["dup@x.com"] })],
    });
    // case-insensitive duplicate — must still be a no-op
    const r = await saveEmailBack(client, { jidNumber: JID, name: "Ingra", email: "DUP@x.com" });
    return (
      r.saved === false &&
      r.reason === "duplicate" &&
      client.calls.update.length === 0 &&
      client.calls.create.length === 0
    );
  }
);

await check(
  "20. client throws -> {saved:false, reason:'error'} (never throws out)",
  async () => {
    const client = fakeClient({
      connections: [contact({ phone: JID, emails: ["old@x.com"] })],
      throwOn: "update",
    });
    const r = await saveEmailBack(client, { jidNumber: JID, name: "Ingra", email: "new@x.com" });
    return r.saved === false && r.reason === "error";
  }
);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
