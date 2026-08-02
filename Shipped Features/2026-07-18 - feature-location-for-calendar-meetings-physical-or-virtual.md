# PLAN — Location for Calendar Meetings (Physical or Virtual)

Card: 2b586a24 · Flow: **@assistant only** (`secretary/2. Skills/1. Calendar Actions/`).
The `@mary` copy (`secretary/3. Mary Skills/1. Calendar Actions/`) is intentionally NOT touched.

## Planned against
Commit **`d5369d71d02c24f3b0794889e6667fbca2f65160`** (HEAD, `d5369d7`). Every path, line
number and signature below was read in the live tree at this SHA. Re-verify per CONVENTIONS §3
before building.

---

## Data model (the one design the coder must not re-decide)

Location is carried on the draft as **two coupled fields**, treated exactly like `all_day` /
`recurrence` are today:

- `location: string | null` — the **verbatim** physical address (outer whitespace trimmed, no
  lookup, no normalization). null = no physical address.
- `virtual: boolean` — true iff the event is a Google Meet video call.

**Physical XOR virtual** is enforced in ONE normalizer, `normalizeLocation()` (below): `virtual`
wins, a non-empty address means physical, everything else means "no location". This is the same
"one place does the arithmetic" discipline `normalizeAllDay`/`allDayWireDates` use.

The **notify decision** for edits is a per-write signal, carried on the edit draft as
`notify: boolean` (default false), set true only when the model reports an explicit "let the
guests know" (`notify_guests`).

---

## Files

### 1. `secretary/2. Skills/1. Calendar Actions/skill.js`

**New pure helpers (exported, for offline test — same precedent as `toRRule`):**

- `normalizeLocation(location, virtual)` — XOR + verbatim-trim. The single normalizer.
- `locationFromEvent(ev)` — read `{location, virtual}` from a real Google event resource.
- `meetLinkOf(ev)` — the Meet URL to surface (`ev.hangoutLink`, else the `video` entryPoint
  uri, else null). Edge #8: may be null when Google is still provisioning; the event `htmlLink`
  (already shown) is the always-works fallback.
- `locationInsertBody({ location, virtual, seed })` — the location/conference fragment of an
  `events.insert` body, plus whether `conferenceDataVersion:1` is needed.
- `locationUpdateFields(draft, base, seed)` — the location/conference fields for the
  full-resource `events.update`, plus whether `conferenceDataVersion:1` is needed. **This is
  where Nit C (conditional version), Nit D (Meet-clear), edge #2 (idempotent), edge #3 (XOR
  switch) all live.**
- `resolveSendUpdates(draft, base)` — Nit A: `"all"` if any non-location field differs from the
  seed, else `"all"` when `draft.notify`, else `"none"`.

**Existing functions to export (add `export`, no body change beyond noted):**
`draftFromInfo`, `mergeDraft`, `applyDraftUpdate`, `editDraftFromEvent`, `applyPatchToDraft` —
so the carry-through selftest can call them directly. Adding `export` is additive and
skill-local.

**Modified functions** (all in this file, all real at HEAD):

- `createEvent` (345–380) — destructure gains `location, virtual`. Build the location/conference
  body fragment via `locationInsertBody({ location, virtual, seed: start_iso || start_date || title })`;
  add `conferenceDataVersion: 1` to the `events.insert` call **only when virtual**. A create with
  neither field adds **no** key → byte-identical to today's write.
- `updateEvent` (404–413) — signature gains two trailing optional params (below). Passes
  `sendUpdates` (was hardcoded `"all"`) and adds `conferenceDataVersion: 1` **only when**
  `conferenceVersion` is true.
- `createFromDraft` (795–839) — pass `location: draft.location, virtual: draft.virtual` to
  `createEvent`; pass the location line + `meetLinkOf(ev)` to `createDone`.
- `applyEditDraft` (1346–1381) — compute `base = editDraftFromEvent(ev)`; merge
  `locationUpdateFields(draft, base, eventId)` into `fields`; compute
  `sendUpdates = resolveSendUpdates(draft, base)`; call
  `updateEvent(env, eventId, ev, fields, sendUpdates, conferenceVersion)`; pass the location
  line + `meetLinkOf(updated)` + `notified: sendUpdates === "all"` to `editDone`.
- `draftFromInfo` (701–738) — compute `const { location, virtual } = normalizeLocation(info.location, info.virtual);`
  and add both to the returned draft. This is the single create-side normalizer every merge
  path funnels through (edge #11).
- `mergeDraft` (988–1037) — carry `location: prev.location, virtual: prev.virtual` into the
  `draftFromInfo(...)` call **exactly as `all_day`/`recurrence` are carried** (the resolver never
  touches location — edge #11).
- `applyDraftUpdate` (898–919) — pass `location: review.location, virtual: review.virtual`
  **DIRECT** (not `?? prev`) into `draftFromInfo`, with the same "model echoes current on
  non-clearing modify; null=clear" contract as `recurrence` (comment mirrors the recurrence one).
- `editDraftFromEvent` (1151–1165) — seed `const { location, virtual } = locationFromEvent(ev);`
  into the returned draft; also seed `notify: false`.
- `eventForLLM` (1126–1140) — add `location, virtual` (from `locationFromEvent(ev)`) so the edit
  model sees the current state.
- `applyPatchToDraft` (1196–1224) — fold the location patch with XOR and THE-RULE discipline:
  `new_virtual === true` → virtual (drops address); else a non-empty `new_location` → physical
  (drops Meet); else `remove_location === true` → clear both; a bare `new_virtual === false` is
  **ignored** (turning virtual off means giving an address, mirrors the `new_all_day===false`
  rule). Then `({location, virtual} = normalizeLocation(d.location, d.virtual))`. Set
  `if (patch.notify_guests === true) d.notify = true;` (sticky).
- `hasEditChange` (1173–1184) — count a location change: non-empty `new_location`, or
  `new_virtual === true`, or `remove_location === true`. (`notify_guests` alone is NOT a change.)
- `draftAsEventJson` (1229–1243) — include `location: d.location, virtual: !!d.virtual` so the
  edit-review model judges against the proposed target.
- `openEditConfirm` (1310–1335) — signature gains `base`; store `base` in `session.data`;
  compute `willNotify = resolveSendUpdates(draft, base) === "all"`; pass `location, virtual,
  notifyGuests: willNotify` to `editConfirm`.
- `handleEdit` (1383–1463) — call `openEditConfirm(ctx, eventId, draft, editDraftFromEvent(ev))`.
- `resumeEditClarify` (1468–1499) — call `openEditConfirm(ctx, eventId, draft, editDraftFromEvent(ev))`.
- `resumeEditConfirm` (1504–1552) — read `base` from `session.data`. It has **TWO**
  `openEditConfirm` call sites and BOTH must pass `base`: the clarify/refresh-TTL branch at
  **1524** (`openEditConfirm(ctx, eventId, draft, base)`) AND the modify re-show at **1530**
  (`openEditConfirm(ctx, eventId, updated, base)`). Missing the 1524 call would pass
  `base = undefined` into `resolveSendUpdates` and crash a reachable edit path (PLAN_REVIEW
  blocking issue). `base` is already in scope from `session.data`.
- `openCreateConfirm` (765–791) — pass `location: draft.location, virtual: draft.virtual` to
  `createConfirm`.
- `manifest.inputs.fields` (80–123) — add `location` and `virtual` field declarations **in the
  same commit** as the `CAL_SCHEMA.required` change (T2.10 lint — see Tests). Add a
  `location_virtual_xor` entry to `consistency` (documentary; non-blocking, `normalizeLocation`
  is the real enforcer). NOT added to `requiredWhen` (both nullable/optional).

### 2. `secretary/2. Skills/1. Calendar Actions/prompt.js`

- `CAL_SCHEMA` (53–99) — add to `required` and `properties`:
  `location: { type: ["string","null"] }`, `virtual: { type: ["boolean","null"] }`.
- `REVIEW_SCHEMA` (104–136) — add the same two fields (so create-confirm modify can set/change/
  clear them).
- `EDIT_SCHEMA` (167–192) — add `new_location: {type:["string","null"]}`,
  `new_virtual: {type:["boolean","null"]}`, `remove_location: {type:"boolean"}`,
  `notify_guests: {type:["boolean","null"]}` (all in `required`).
- `EDIT_REVIEW_SCHEMA` (198–227) — add the same four change fields (so refinement at the
  edit-confirm step works: "actually add the address", "and notify them").
- `buildExtractionRules` (238–329) — in the `action="create"` block, teach: extract a **verbatim**
  `location` string (NEVER invent, look up, or normalize an address); set `virtual=true` on a
  video-call/Meet intent ("make it a video call", "chamada de vídeo", "no Meet"); XOR — never both.
  This is the shared rulebook carried verbatim into the merged router+extractor call; reword once.
- `buildCreateReviewSystem` (343–360) — teach the location/virtual echo-on-non-location-modify
  and clear semantics, mirroring the recurrence paragraph.
- `buildEditSystem` (429–446) & `buildEditReviewSystem` (465–479) — teach `new_location`
  (verbatim), `new_virtual` (emit `true` to make it a Meet; a switch back to physical is
  expressed by `new_location`, never by `new_virtual:false`), `remove_location` (clear both),
  `notify_guests` (only on an explicit "let the guests know"); XOR; never look up or normalize
  an address.
- `REPLY` en + pt (685–888) — new **conditional** location line in `createConfirm`, `createDone`,
  `editConfirm`, `editDone`; the physical line (`📍 <address>`), the virtual line
  (`📹 Google Meet (video call)` / `📹 Google Meet (chamada de vídeo)`, + Meet link when present);
  the edit not-notified note (shown when `notifyGuests` false) and notified/not-notified note on
  `editDone` (from `notified`). Every string ships **en + pt**. No inline prose in `skill.js`.

`buildResolveSystem` / `RESOLVE_SCHEMA` are **unchanged** — the gathering resolver chases only
time+email; location rides on the draft via `mergeDraft`'s prev-carry (same as `all_day`).

---

## Interfaces (signatures)

New:
```js
export function normalizeLocation(location, virtual)      // -> { location: string|null, virtual: boolean }
export function locationFromEvent(ev)                      // -> { location: string|null, virtual: boolean }
export function meetLinkOf(ev)                             // -> string | null
export function locationInsertBody({ location, virtual, seed })
                                                           // -> { body: object, conferenceVersion: boolean }
export function locationUpdateFields(draft, base, seed)    // -> { fields: object, conferenceVersion: boolean }
export function resolveSendUpdates(draft, base)            // -> "all" | "none"
```

Changed:
```js
async function createEvent(env, { title, emails, start_iso, end_iso, summary, all_day,
                                  start_date, end_date, recurrence, location, virtual })  // +location, +virtual
async function updateEvent(env, eventId, ev, fields, sendUpdates = "all", conferenceVersion = false)
async function openEditConfirm(ctx, eventId, draft, base)  // +base
```

Newly exported (body unchanged except the field additions noted above):
```js
export function draftFromInfo(ctx, info)
export function mergeDraft(ctx, prev, patch)
export function applyDraftUpdate(ctx, prev, review)
export function editDraftFromEvent(ev)
export function applyPatchToDraft(draft, patch)
```

**`locationUpdateFields(draft, base, seed)` behaviour (authoritative):**
- `draft.virtual && base.virtual` → `{ fields: {}, conferenceVersion: false }` — idempotent
  (edge #2); `{...ev}` re-supplies the live conference, and with NO `conferenceDataVersion`
  Google leaves it untouched.
- `draft.virtual && !base.virtual` → `{ fields: { location: "", conferenceData: { createRequest:
  { requestId: \`meet-${seed}\` } } }, conferenceVersion: true }` — provision Meet, drop address.
- `!draft.virtual && base.virtual` → `{ fields: { location: draft.location || "",
  conferenceData: null }, conferenceVersion: true }` — **Nit D**: virtual→physical clears the
  stale Meet (`conferenceData:null` + version:1).
- `!draft.virtual && !base.virtual && (draft.location||null) === (base.location||null)` →
  `{ fields: {}, conferenceVersion: false }` — non-location edit touches nothing (byte-identical
  to today; **Nit C** — a normal edit never disturbs a Meet or sends a conference version).
- `!draft.virtual && !base.virtual && changed` → `{ fields: { location: draft.location || "" },
  conferenceVersion: false }` — set/clear the address; no conference involved.

`requestId` seeds are **deterministic** (`seed = eventId` on update, `start_iso||start_date||title`
on insert) — no `Date.now`/`Math.random`, and reusing the same id is idempotent per Google.

---

## Rails changes

**None.**

- No file under `secretary/1. Orchestrator/` changes. `lib/google.js`, `lib/confirm.js`,
  `server.js`, `router/*` are all untouched. Verified against the CONVENTIONS §1 test — every
  changed symbol lives inside `2. Skills/1. Calendar Actions/` and can only break this skill.
- **No new `ctx` field.** `notify` and `base` are carried on the skill's own draft/session data,
  not on `ctx`.
- **No new `lib/` module.** All helpers live inside `skill.js`.
- **No new OAuth scope / API key / external service.** Google Meet via
  `conferenceData.createRequest` + `conferenceDataVersion:1` rides the existing Calendar
  read-write scope the refresh token already carries (SCOPE_REVIEW verified this at
  `lib/google.js:18`). `updateEvent`'s new params are additive with defaults that reproduce
  today's exact call (`sendUpdates:"all"`, no `conferenceDataVersion`), so its one existing
  caller — `applyEditDraft` — keeps working unchanged in the non-location path.

**Router catalog / live check.** `manifest.description` is **unchanged**, so the router's
classification catalog is unaffected and the paid `scripts/router-selftest.mjs` is **not
mandated** by routing. `buildExtractionRules` (the rulebook carried verbatim into the merged
router+extractor prompt) does gain two extraction fields — but that changes *extraction*, not
*selection*, and `router-selftest.mjs` tests selection. Offline `calendar-location-selftest.mjs`
+ T2.10 + the `npm start` smoke cover the binding. **FLAG for the manager (human/spend call):
confirm no live router self-test is required.** `router/prompt.js` is not touched.

---

## Sequence (tree working at each step)

1. **Prompts + schema, atomically with the lint.** Add `location`/`virtual` to `CAL_SCHEMA`,
   `REVIEW_SCHEMA`; the four `new_*`/`notify_guests` fields to `EDIT_SCHEMA`/`EDIT_REVIEW_SCHEMA`;
   the extraction/review rules in `prompt.js`; **and** the `manifest.inputs.fields` +
   `consistency` entries in `skill.js` in the **same step** (T2.10 asserts set-equality — split
   this and the lint goes red mid-sequence). Tree still runs; new fields are simply unread yet.
2. **Pure helpers.** Add + export `normalizeLocation`, `locationFromEvent`, `meetLinkOf`,
   `locationInsertBody`, `locationUpdateFields`, `resolveSendUpdates`. No caller yet — inert.
3. **Create path.** Wire `draftFromInfo` (normalize+store), `mergeDraft`/`applyDraftUpdate`
   (carry-through), `createEvent` (`locationInsertBody` + conditional version), `createFromDraft`,
   `openCreateConfirm`, and the `createConfirm`/`createDone` REPLY lines. Add `export` to the
   three create-side merge fns. Create with location/Meet now works end-to-end; edit unaffected.
4. **Edit path.** Wire `editDraftFromEvent` (seed + `notify`), `eventForLLM`, `applyPatchToDraft`
   (fold+XOR+notify), `hasEditChange`, `draftAsEventJson`, `updateEvent` (new params),
   `applyEditDraft` (`locationUpdateFields` + `resolveSendUpdates`), `openEditConfirm` (+`base`)
   and its three callers, and the `editConfirm`/`editDone` REPLY lines. Add `export` to
   `editDraftFromEvent`/`applyPatchToDraft`. Full feature live.
5. **Tests.** Write `scripts/calendar-location-selftest.mjs`; run it green. Run
   `node scripts/turn-latency-selftest.mjs`, `node scripts/calendar-create-selftest.mjs`,
   `node scripts/calendar-edit-selftest.mjs`, `node scripts/calendar-recurrence-selftest.mjs`,
   `node scripts/selflearning-selftest.mjs` — all green. Smoke: `cd secretary &&
   ANTHROPIC_API_KEY=dummy npm start` → `skill loaded: …`.
6. **Docs** (see below).

(Steps 1–4 each leave the tree parseable and every existing selftest green; the new fields are
read only once their path is wired.)

---

## Tests

**New standalone offline script: `scripts/calendar-location-selftest.mjs`** — same shape as
`scripts/calendar-recurrence-selftest.mjs` (dynamic `import()` of `skill.js`, no network/key/
Redis/Google, no framework, safe-call wrappers so missing exports FAIL cleanly). It asserts the
deterministic layer only; the model's *recognition* of "make it virtual"/an address is the paid
live check, not this file. **It must FAIL at HEAD (exports absent) and go green after build.**

Assertions:

- **XOR (`normalizeLocation`)**: `("Rua X", null)`→`{location:"Rua X",virtual:false}`;
  `("Rua X", true)`→`{location:null,virtual:true}` (virtual wins); `("  ", false)` and
  `(null,false)`→`{location:null,virtual:false}`; whitespace trimmed, inner text verbatim.
- **`draftFromInfo` XOR + store**: an info with both `location` and `virtual:true` yields a draft
  with `location:null, virtual:true`.
- **Carry-through (edge #11)**: `mergeDraft(ctx, prev, patch)` with a location-less patch keeps
  `prev.location`/`prev.virtual`; `applyDraftUpdate(ctx, prev, review)` with `review.location`/
  `review.virtual` echoing current keeps them, and `review.location:null,virtual:false` clears
  (direct read). `ctx` is a minimal fake `{ owner:"Marcelo", contact:null }`.
- **Seed (`editDraftFromEvent`/`locationFromEvent`)**: a physical event
  (`{location:"Rua X"}`)→`{location:"Rua X",virtual:false}`; a Meet event
  (`{conferenceData:{conferenceId:"abc",entryPoints:[{entryPointType:"video",uri:"https://meet…"}]}}`)
  →`{location:null,virtual:true}`; `meetLinkOf` returns the uri (and `ev.hangoutLink` when set).
- **Fold (`applyPatchToDraft`)**: `{new_location:"Rua Y"}` on a virtual draft → physical, virtual
  false; `{new_virtual:true}` on a physical draft → virtual, location null; `{remove_location:true}`
  → both cleared; `{new_virtual:false}` alone → **unchanged** (bare-false ignored);
  `{notify_guests:true}` → `draft.notify===true` and is NOT counted by `hasEditChange`.
- **Meet-clear / conditional version (`locationUpdateFields`, Nits C & D, edges #2/#3)**: the five
  branches in Interfaces above — assert `fields.conferenceData === null` on virtual→physical,
  `createRequest` present on physical→virtual, `fields === {}` and `conferenceVersion===false` on
  an already-virtual no-op and on a non-location edit.
- **sendUpdates (`resolveSendUpdates`, Nit A)**: location-only diff + `notify:false`→`"none"`;
  location-only + `notify:true`→`"all"`; `summary` differs→`"all"` (agenda is substantive);
  `start_iso` differs→`"all"`; attendee set differs→`"all"`.

**T2.10 lint** (`scripts/turn-latency-selftest.mjs:498`) goes red between the schema edit and the
manifest edit — that is the lint working; both land in Step 1, ending green. No new test runner,
no `test/` dir, no dependency.

---

## Documentation changes

- **`secretary/2. Skills/1. Calendar Actions/SKILL.md`** — document the new behaviour: attach a
  verbatim physical address at create/edit; "make it virtual" → Google Meet; physical XOR
  virtual; location-only edits are silent unless the owner asks to notify. **Change required.**
- **`PROJECT_LOG.md`** — **§10 changelog: a dated `2026-07-18` entry** for this feature
  (calendar location physical/virtual, Meet via `conferenceData`, conditional `sendUpdates`).
  §1 skill list / §2 status: **no change** (no new skill, no rails move).
- **`README.md`** — extend the `calendar_action` blurb (line 76) with a clause: attach a physical
  address or auto-generate a Google Meet link (physical XOR virtual). **Small change.**
- **`ARCHITECTURE.md`** — **no change** (no rails, `ctx`, dispatch, flow-step, or shared-lib
  change; the feature is entirely skill-local).
- **`secretary/1. Orchestrator/ORCHESTRATOR.md`** — **no change** (rails untouched).
- **`secretary/README.md`** — **no change** (no skill or lib module added).
- **Plan-doc archive** — this card has **no** `New Features Plans/` copy (its source draft lives
  only in the card folder), so there is nothing to `git mv`. Create the shipped record directly:
  `Shipped Features/2026-07-18 - feature-location-for-calendar-meetings-physical-or-virtual.md`
  (this PLAN's content). **FLAG:** if the manager wants the `git mv`-from-`New Features Plans`
  convention honoured, the draft must first be placed under `New Features Plans/`; called out so
  the build doesn't silently diverge from CONVENTIONS §4.

---

## Migrations / config

None. No env var, no new secret, no new dependency, no schema/store migration. Google Meet uses
the Calendar scope the refresh token already holds.

---

## Risks (where this plan is most likely wrong)

1. **Consumer-Gmail vs Workspace Meet provisioning (edge #12).** `conferenceData.createRequest`
   is expected to work on the owner's account with no new scope, but this is the one unknown that
   only a real write proves. Build-time check: create a virtual event and confirm a Meet link
   comes back (or a `pending` status that resolves). If it fails, that is a build-time escalation,
   not a silent failure — the plan cannot de-risk it offline.
2. **`conferenceData` round-trip on the full-resource replace.** The plan leans on Google
   *ignoring* `{...ev}`'s `conferenceData` when `conferenceDataVersion` is absent (so a normal
   edit preserves an existing Meet). If that assumption is wrong, a non-location edit could drop a
   Meet link. Mitigated by the conditional-version design and by `calendar-edit-selftest.mjs`'s
   colorId tripwire, but the conference-preservation itself is only fully provable live — name it
   in the build-time check alongside risk 1.
3. **Meet URI latency (edge #8).** `meetLinkOf` may return null on the immediate insert response;
   the done bubble falls back to the event `htmlLink`. If a raw Meet URL in the bubble is desired
   every time, that needs a re-fetch — deliberately NOT planned (out of scope; the event link
   carries the Join button).
4. **Review-echo discipline for location.** `applyDraftUpdate` reads `review.location`/`virtual`
   DIRECT (null=clear), so a model that fails to echo the current location on an unrelated modify
   would silently drop it — the exact recurrence risk, mitigated the exact same way (prompt
   instruction). If the model proves unreliable here, fall back to `?? prev` for `location` with
   an explicit clear sentinel — a build-time judgement, flagged.
