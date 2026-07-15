# PLAN — New Architecture: Convert all skills

> Turns the FIXED design (memory `mary-skill-stack-migration.md`) and the authoritative revised
> `SCOPE.md` into an executable build. The two SCOPE_REVIEW blockers are already resolved IN the
> scope (rails change (b) authorized for transcribe; `CAPS` explicitly left on the old tree), so
> ENTRY passes. This plan holds the design; it does not re-open it.

## Planned against

- Commit SHA: **`667262e86047befcee80a50282373af6381b36ad`** (`git rev-parse HEAD`).
- ⚠️ **The working tree is DIRTY at this SHA** (uncommitted `M` on `2. Skills/1. Calendar Actions/*`,
  several `scripts/*`, `PROJECT_LOG.md`, `README.md`, etc. — see `git status`). Per CONVENTIONS §2 a
  build must start from a clean tree so this card's diff is readable on its own. **The build cannot
  begin until those changes are committed, stashed, or reverted by the human.** Flagged below.

---

## The shape every converted skill takes (the reference: `7. Assistant Settings`)

Each new-tree skill is a **pure task** exactly like the shipped settings pilot
(`secretary/2. Skills/7. Assistant Settings/skill.js`):

```
export const manifest = {
  id: "<same id as today>",
  conversation: "orchestrator",         // the model runs the dialogue; the skill never asks/confirms/classifies
  inputs: <declared contract> | null,   // validated by lib/inputs.js checkPayload BEFORE dispatch
  description: "<reworded: no 'she proposes/asks' — the orchestrator does>",
};
export async function run(ctx) { /* validate defensively → execute → send ONE outcome → RETURN a JSON value */ }
// NO `capabilities` export in the new tree (calendar drops startCreate; tasks drops list — CAPS is not repointed anyway).
```

Load-bearing invariants carried from the reference:
- The skill **never** imports `lib/confirm.js` (`classifyConfirmation`) and **never** opens a
  `sessions.set(...,{skill,...})` conversation session. All propose/ask/confirm/classify logic is
  deleted from the skill; the orchestrator model owns it via `listen` turns.
- `run()` **returns** a JSON-serializable value → the orchestrator serializes it (≤ `READBACK_CAP`)
  and drives a read-back turn. This is how the model "picks a candidate" in the read-then-act loop.
- Every user-facing string stays in the skill's `prompt.js` as an `{ en, pt }` map consumed via
  `reply(ctx.lang)`. A `*Error`/`*Failed`/`*NoMatch`/`noAction` key is sent with `ctx.sendFailure`
  (linted by `scripts/selflearning-selftest.mjs`); a plain outcome uses `ctx.send`.
- Relative imports resolve identically: `secretary/3. Mary Skills/<N>/` sits at the SAME depth as
  `secretary/2. Skills/<N>/`, so every `../../1. Orchestrator/lib/...` import in a copied file
  resolves unchanged. **This is why the new tree can be seeded by copying the old files.**

### The read-then-act contract (calendar / tasks / flights) and the checkPayload constraint

One `run(ctx)` is dispatched twice across turns — once to **READ** (return candidates), once to
**ACT** (mutate an explicitly-identified target). Because an `orchestrator` primary is gated on
`checkPayload(inputs, info).ok` (server.js:701), **the declared `inputs` must be shaped so a READ
payload passes completeness**: the discriminator's READ value(s) set **no** `requiredWhen`. All
non-discriminator fields are `nullable` (the model emits every declared field, nulling the
irrelevant ones — exactly today's `CAL_SCHEMA` pattern), so a READ payload is shape-valid. The READ
step **returns structured candidates carrying a stable id** (`event_id`, task `id`, flight
`option_number`); the model reads those back, proposes, and on a later owner turn dispatches the ACT
value with that id in `info`.

---

## Files

### A. Rails — `secretary/1. Orchestrator/server.js` (the TWO authorized changes)

Edits are additive; the LEGACY (@assistant) path is byte-for-byte unchanged. Exact edit points
(current line numbers at the planned SHA):

**Rails change (a) — per-flow skill discovery.**

1. **`:79`** — after `const SKILLS_DIR = …"2. Skills")`, add:
   `const NEW_SKILLS_DIR = path.join(__dirname, "..", "3. Mary Skills");`
2. **`:133`** — parametrize discovery additively: `async function loadSkills()` →
   `async function loadSkills(dir = SKILLS_DIR)`, and inside use `dir` in place of `SKILLS_DIR`
   at the `readdir(dir,…)` (`:139`), the error log (`:141`), and `path.join(dir, e.name,…)`
   (`:146`). The existing zero-arg call keeps working (default = `SKILLS_DIR`).
3. **`:305`** — after the existing
   `const { skills: SKILLS, catalog: CATALOG, caps: CAPS } = await loadSkills();` add:
   `const { skills: NEW_SKILLS, catalog: NEW_CATALOG } = await loadSkills(NEW_SKILLS_DIR);`
   and a `console.log("mary skills:", NEW_CATALOG.map(c=>c.id).join(", ") || "(none!)");`.
   **`SKILLS`/`CATALOG`/`CAPS` stay on the OLD tree** — `LEGACY_SKILLS`/`LEGACY_CATALOG`
   (`:321–330`) derive from them unchanged, and `CAPS` is **not** repointed (its only consumer is
   the shared `ctx.callSkill`/`ctx.hasSkill` closure at `:520–528`, which the legacy Tasks→Calendar
   `startCreate` delegation depends on — repointing it to the caps-less new tree would regress
   @assistant; see SCOPE Issue 2).
4. **`:334`** — `NEW_FLOW`: change `catalog: CATALOG` → `catalog: NEW_CATALOG` (so the NEW router's
   `route()` renders the new-tree catalog via `ctx.catalog`).
5. **Repoint the NEW turn loop's direct references** (all six the scope enumerates) from the old
   globals to the new maps:
   - `:651` `CATALOG.map((c)=>c.id)` → `NEW_CATALOG.map(...)` (the `done`/`notUnderstood` branch)
   - `:680` `batch.filter((s)=>SKILLS[s])` → `…NEW_SKILLS[s])`
   - `:683` `CATALOG.map((c)=>c.id)` → `NEW_CATALOG.map(...)`
   - `:691` `CATALOG.find((c)=>c.id===primary)` → `NEW_CATALOG.find(...)`
   - `:724` `CATALOG.find((c)=>c.id===task)` → `NEW_CATALOG.find(...)`
   - `:732` `const run = SKILLS[task]` → `const run = NEW_SKILLS[task]`

   (These are the ONLY `SKILLS`/`CATALOG` references inside the loop `:592–776`. `route()` already
   reads `ctx.catalog`/`ctx.tags` per-flow, so no change there.)

**Rails change (b) — `inputs:null ⇒ dispatch-without-validation`.** In the dispatch gate
`:700–713`, wrap the existing `checkPayload` gate so a no-declared-inputs orchestrator primary
dispatches instead of repair-looping:

```js
if (primaryEntry?.conversation === "orchestrator") {
  if (primaryEntry.inputs == null) {
    infoFor = null;                       // nothing to validate/hand over; the skill runs its own check
  } else {
    const g = checkPayload(primaryEntry.inputs, info);
    if (!g.ok) { /* …existing repair loop, verbatim… */ }
    infoFor = primary;
  }
}
```

Additive: the `else` branch is today's code unchanged; only the `inputs == null` case is new.
Required by `transcribe_audio` (`conversation:"orchestrator"`, `inputs:null`). Current callers of
this gate are declared-inputs orchestrator skills (`assistant_settings`) → they take the `else`
branch, unchanged.

### B. The new isolated tree — `secretary/3. Mary Skills/` (NEW)

Seven folders, each `skill.js` + `prompt.js` + `SKILL.md`. Seed each by **copying** the matching
`secretary/2. Skills/<N>/` folder, then edit per below. The old `2. Skills/` tree is left untouched.

| # | New folder | Seed from |
|---|---|---|
| 1 | `1. Calendar Actions/` | `2. Skills/1. Calendar Actions/` |
| 2 | `2. Audio transcriptions/` | `2. Skills/2. Audio transcriptions/` |
| 3 | `3. Tasks/` | `2. Skills/3. Tasks/` |
| 4 | `4. Feature Requests/` | `2. Skills/4. Feature Requests/` |
| 5 | `5. Feedback/` | `2. Skills/5. Feedback/` |
| 6 | `6. Flight Search/` | `2. Skills/6. Flight Search/` |
| 7 | `7. Assistant Settings/` | `2. Skills/7. Assistant Settings/` (verbatim copy — already converted) |

**B7 — `7. Assistant Settings/` (COPY VERBATIM).** No code change: it is already the pure-task
reference and imports resolve at the same depth. It exists as a physical copy so the Mary stack is
self-contained; the old folder stays so the legacy `LEGACY_SKILLS`/`LEGACY_CATALOG` swap keeps
working. (Its `setNewTags`/`newSettings` behaviour is already correct for the new flow.)

**B2 — `2. Audio transcriptions/` (`transcribe_audio`; `inputs:null`).**
- `skill.js`: manifest → `conversation:"orchestrator"`, keep `inputs:null`, keep description.
  `run(ctx)` keeps the existing body (reads `ctx.quoted`/`ctx.hasQuotedAudio`, `getMediaBase64`,
  AssemblyAI upload+poll, inline-vs-`.txt` delivery) with two changes: (i) each terminal branch
  **returns a JSON result** so a read-back fires and @mary stays open —
  `{ok:false,reason:"noAudio"|"noKey"|"downloadFailed"|"empty"|"transcribeFailed"}` or
  `{ok:true,delivered:"inline"|"file",chars:<n>}`; (ii) the `noAudio` branch sends via **plain
  `ctx.send`** — MANAGER RULING (resolves FLAG 2): "reply to an audio to transcribe" is user
  guidance, not a malfunction, so it must NOT file a self-learning failure capture; this keeps it
  consistent with the existing server.js:490 rule and overrides the SCOPE edge-case wording that
  said `sendFailure`. The user-visible message is unchanged either way.
- `prompt.js`: unchanged (`MSG`/`msg`); `noAudio` string stays.

**B5 — `5. Feedback/` (`feedback`).**
- `skill.js`: manifest → `conversation:"orchestrator"`, `inputs` declared (below), reworded
  description. `run(ctx)` reduces to: read `ctx.info` + `ctx.quoted`, `captureFailure({phase:
  "reported", taskId: info.suspected_skill||"feedback", report:{note, whatWentWrong, expected,
  quotedText, quotedIsSecretary}})`, then `ctx.send(reply(lang).logged(...))` (or `logFailed` via
  `sendFailure` on a write failure). **Return** `{ok, reportPath, title}`. **Delete**
  `resumeFeedback`, the `enough_context` follow-up session, and the `extract()` LLM call (the
  orchestrator now extracts and, per SCOPE, asks the one clarifying question on a `listen` turn
  before dispatching). Keep `captureFailure`/`appendToReport` imports; drop `jsonFormat/readReply`
  and the feedback schema/system/user prompt builders.
- `prompt.js`: keep outcome strings (`logged`, `logFailed`, `enriched`→ drop, since no follow-up);
  drop `loggedAndAsk`. Drop `buildFeedbackSchema/System/User`.

**B4 — `4. Feature Requests/` (`feature_request`).**
- `skill.js`: manifest → `conversation:"orchestrator"`, `inputs` = the **brief** (below), reworded
  description. `run(ctx)` reduces to a single **render+deliver** path: take `ctx.info` as the draft,
  `generateDoc(ctx, draft)` → md, `spoolSpec(draft, md)`, `evolution.sendMedia(.md)`, **return**
  `{ok, path:spooled, title}`. **Delete** `clarifyTurn`, `startFeatureRequest`, `resumeClarify`,
  `openSession`, `mergeDraft`, `EMPTY_DRAFT`, `CLARIFY_SCHEMA` usage and the continuation branch —
  the orchestrator runs the whole interview over `listen` turns. **Keep** `generateDoc`,
  `buildDocSystem`, `buildDocUser`, `spoolSpec`, `specHeader`, `saoPauloStamp`, `slugify`, and the
  fs/path/url prelude.
- `prompt.js`: keep `buildDocSystem`, `buildDocUser`, `slugify`, and outcome strings
  (`docCaption`, `renderError`, `sendFailed`, `specFileFailed`, `thinkingError`). Drop
  `buildClarifySystem/User`, `CLARIFY_SCHEMA`, and the clarify scaffolding replies (`cancelled`,
  `firstFallback`, `continueFallback`) — the clarifying **guidance** moves into `manifest.inputs
  .rulebook` so the orchestrator knows what a complete brief needs.

**B1 — `1. Calendar Actions/` (`calendar_action`; read-then-act).**
- `skill.js`: manifest → `conversation:"orchestrator"`, `inputs` per the read-then-act contract
  (below), reworded description (drop "she proposes… applies on a yes"). **Remove** `capabilities`
  (drop `startCreate`) and the `classifyConfirmation` import. `run(ctx)` becomes a pure dispatch on
  `ctx.info.action`:
  - `find` (READ): gather candidate events (reuse `matchEventTargets` / `events.list`) matching
    `info.query`/`start_iso`/`participants`; **send nothing**, **return** `{candidates:[{event_id,
    title,start,end,attendees,link}], count}`.
  - `list` (READ): `handleList` — send the rendered events AND **return** the same items
    structured (so a follow-up edit/delete can target by `event_id`).
  - `create` (ACT): `createEvent(...)` from `info`, send `createDone`, return `{ok,link,eventId}`.
  - `edit` (ACT): `getEvent(info.event_id)` → `updateEvent`/`applyEditDraft` with the change
    fields, send `editDone`, return `{ok,eventId}`.
  - `delete` (ACT): `cancelMeeting({eventIds:[info.event_id], …})`, send `deleteCancelled`,
    return `{ok,cancelled:<n>}`.
  **Delete** every `resume*`, `review*`, `openInquiry`, `open*Confirm`, `inspectMissing`,
  `interpret`/`interpretEdit`, and all `sessions.set(...)` calls. **Keep** the deterministic API
  helpers: `createEvent`, `updateEvent`, `cancelMeeting`, `getEvent`, `matchEventTargets`,
  `findConfirmedDuplicates`, `handleList`/`toListItem`/`endOfLocalDay`, `resolveEventId`, and every
  RRULE/all-day/draft-normalization helper (`toRRule`, `allDayWireDates`, `draftFromInfo`, etc.).
- `prompt.js`: keep OUTCOME + error strings (`createDone`, `deleteCancelled`, `editDone`,
  `listEvents`, `listNext`, all `*Error`/`*NoMatch`/`noAction`) and the render helpers
  (`localizeWhen`, `describeRecurrence`, …). Drop PROPOSAL/QUESTION strings (`createConfirm`,
  `deleteConfirm`, `editConfirm`, `inquiry`, `editClarify`, `createCancelled`, `deleteKeep`,
  `editCancelled`) and the review/resolve/interpret prompt builders + their schemas
  (`REVIEW_SCHEMA`, `RESOLVE_SCHEMA`, `EDIT_REVIEW_SCHEMA`, and `buildCreateReviewSystem/User`,
  `buildResolveSystem/User`, `buildEditReviewSystem/User`). `buildExtractionRules` is retained but
  now feeds `manifest.inputs.rulebook` only.

**B3 — `3. Tasks/` (`task_action`; read-then-act; DROP the calendar coupling).**
- `skill.js`: manifest → `conversation:"orchestrator"`, `inputs` per the read-then-act contract
  (below), reworded description. **Remove** `capabilities.list`, the `classifyConfirmation` import,
  and the entire calendar-delegation block (`ctx.hasSkill("calendar_action","startCreate")` /
  `ctx.callSkill(...startCreate…)` at `:459–478` and `threePmOnDue`/`calendarUnavailable` /
  `thirdPartyCapped` handling) — "a task for someone else" is now the model chaining a
  `calendar_action` create (SCOPE Q5). `run(ctx)` becomes a pure dispatch on `ctx.info`:
  - `list` (READ): `fetchOpen(ctx)` → send the rendered list AND **return** `{tasks:[{id,title,
    due}]}`.
  - `apply` (ACT): execute `info.ops` in order via `addTask`/`completeTask`/`patchTask`/
    `deleteTask` (targeting by `task_id` for complete/edit/delete), send the applied-summary
    outcome, **return** `{applied:[…], failed:[…]}`.
  **Delete** `planTaskOps`, `dispatchPlan`, `resumeConfirm`, `resumeEngaged`, `openConfirm`,
  `armEngaged`, `summarizeMutations`, and all `sessions.set(...)`. **Keep** `listTasks`, `fetchOpen`,
  `addTask`, `completeTask`, `deleteTask`, `patchTask`, `tasksClient`, `listId`, `toTasksDue`,
  `localizeDueDate`, and the render helpers.
- `prompt.js`: keep OUTCOME strings (`formatList`/`listHeader`, `createdBatch`/`addedHeader`,
  `amended`/`updatedHeader`, `removed*`, `mutationsApplied`/`renderApplied`, `thinkingError`,
  `failed`, `empty`, `noAction`). Drop PROPOSAL/QUESTION prose (`confirmMutations`/`renderConfirm`
  and its `confirm*` words, `confirmFooter`, `needTitle`, `declined`, `notFound*`, `disambiguate`,
  `whichOne`, `amendHint`, `moreHint`), the planner (`PLAN_SCHEMA`, `plannerCore`,
  `untaggedPosture`, `buildPlanSystem`, `buildPlanUser`), and the dropped-coupling strings
  (`calendarUnavailable`, `thirdPartyCapped`, `threePmOnDue`).

**B6 — `6. Flight Search/` (`flight_search`; read-then-act; keep the options sidecar).**
- `skill.js`: manifest → `conversation:"orchestrator"`, `inputs` (below; the existing `intent`
  discriminator already fits), reworded description. `run(ctx)` becomes a pure dispatch on
  `ctx.info.intent`:
  - `search` (READ/act): `runSearch(ctx, draftFromInfo(info))` — search Kiwi, `selectOptions`,
    send `renderOptions`, `writeOptions(ctx, options)` to the **sidecar** (`${remoteJid}|flights`),
    **return** `{options:[{n,summary,price,bookingUrl}], count}`.
  - `link` (ACT): `answerLink(ctx, info.option_number, {book:false})` — read the sidecar, resolve
    the option, send `linkSent`, **return** `{ok, option:info.option_number}`.
  - `book`/`other`: send `cannotBook`/`notAFlight` and return `{ok:false,reason}`.
  **Delete** `interpret`, `reviewConfirm`, `reviewLink`, `resumeInfo`, `resumeConfirm`,
  `resumeLink`, `openConfirm`, `openInquiry`, `armInfo`/`armConfirm`/`armLink`, `advanceSearch`/
  `missingOf`/`isComplete`, and the `FLIGHT_REVIEW_SCHEMA`/`LINK_REVIEW_SCHEMA` machinery. **Keep**
  `searchKiwi`, `buildKiwiArgs`, `toKiwiDate`, `parseKiwiResponse`, `selectOptions`
  (`filterItineraries`/`applyExplicitFilters`/`topCheapest`), `answerLink`, and the options
  **sidecar** (`writeOptions`/`readStash`/`writeTombstone` — a data cache keyed off a SEPARATE
  redis key; it does NOT set `session.skill`, so it is not a conversation session and does not
  violate the pure-task rule). Keep `writeTombstone` on a fresh `search` (Invariant S).
- `prompt.js`: keep OUTCOME strings (`results`/`thinnedResults`/`emptyResults`/`emptyAfterFilter`/
  `explicitFilterEmpty`, `linkSent`, `linkMissing`, `optionOutOfRange`, `noResultsToLink`,
  `resultsDiscarded`, `searchFailed`, `notAFlight`, `cannotBook`) and `renderOptions` + leg
  formatters. Drop `renderConfirm`/`confirm*`, `askOrigin/Destination/Date`, `cityAmbiguous/Unknown`,
  `badDate`, `returnBeforeDepart`, `declined`, `whichOption`, `thinkingError` (mid-dialogue), and
  the six `build*System/User` conversational prompt builders.

### C. Test — `scripts/mary-skills-selftest.mjs` (NEW)

Offline, no network/model. Shape follows `scripts/selflearning-selftest.mjs`/`history-selftest.mjs`.
(See Tests below for assertions.)

### D. Docs (all written IN this build — see Documentation changes)

`ARCHITECTURE.md`, `PROJECT_LOG.md`, `README.md`, `secretary/1. Orchestrator/ORCHESTRATOR.md`,
`secretary/README.md`, seven new `secretary/3. Mary Skills/<N>/SKILL.md`, and the plan-doc archive.

---

## Interfaces (signatures to add / change)

**server.js**
- `async function loadSkills(dir = SKILLS_DIR)` — was `loadSkills()`. Additive default.
- `const NEW_SKILLS_DIR = path.join(__dirname, "..", "3. Mary Skills");` (new const).
- `const { skills: NEW_SKILLS, catalog: NEW_CATALOG } = await loadSkills(NEW_SKILLS_DIR);` (new).
- `NEW_FLOW = { tags: NEW_TAGS, catalog: NEW_CATALOG, settings: newSettings }` (catalog repointed).
- Dispatch gate `:700–713`: add the `primaryEntry.inputs == null → infoFor = null` branch.
- No signature of any existing export/closure changes. `CAPS`, `ctx.callSkill`, `ctx.hasSkill`,
  `LEGACY_SKILLS`, `LEGACY_CATALOG`, `runLegacyFlow` are untouched.

**Every new skill** — `export const manifest`, `export async function run(ctx) -> <JSON value>`.
No `capabilities` export anywhere in the new tree.

**Declared `inputs` (the design decisions — discriminator + requiredWhen; READ values carry no
`requiredWhen`; every non-discriminator field is `nullable`):**

- `calendar_action` — `discriminator: "action"`, enum `["find","list","create","edit","delete",
  "other"]`; `requiredWhen: { find: [], list: ["list_mode"], create: ["start_iso",
  "participants[].email"], edit: ["event_id"], delete: ["event_id"], other: [] }`. Fields (all
  nullable except `action`): `query`, `event_id`, `title`, `participants[] {name,email}`,
  `start_iso`(iso), `duration_min`(number), `all_day`(bool), `all_day_end_iso`(iso), `summary`,
  `list_mode`(enum `window|next`), `range_start_iso`(iso), `range_end_iso`(iso), `recurrence`(object).
  `consistency`: `attendee_count_matches_email_count`, `create_always_has_a_date`, `end_after_start`,
  `edit_has_a_change` (edit carries ≥1 change field), `window_list_has_a_range`. `rulebook:
  () => buildExtractionRules(...)`.
- `task_action` — `discriminator: "mode"`, enum `["list","apply","other"]`; `requiredWhen:
  { list: [], apply: ["ops"], other: [] }`. Fields: `mode`(enum), `ops`(array of `{kind: enum
  [create|complete|edit|delete], task_id: string|null, title: string|null, due_iso: iso|null}`).
  `consistency`: `create_op_has_a_title`, `mutate_op_has_a_task_id` (complete/edit/delete carry
  `task_id`), `apply_has_at_least_one_op`. No `assignee` field (calendar coupling dropped).
- `flight_search` — keep `discriminator: "intent"`, enum `["search","link","book","other"]`;
  `requiredWhen: { search: ["origin","destination","depart_date"], link: ["option_number"],
  book: [], other: [] }`. Add `option_number`(number|null) to fields; keep `origin`, `destination`,
  `depart_date`(iso), `return_date`(iso), `adults`(number), `cabin`(enum), `summary`.
  `consistency`: `return_after_depart`, `origin_is_not_destination`.
- `feedback` — `discriminator: null`. Fields: `note`(string, NOT nullable), `what_went_wrong`
  (string|null), `expected`(string|null), `suspected_skill`(string|null). `requiredWhen: {}`.
  `consistency`: `note_is_not_blank`.
- `feature_request` — `discriminator: null`. Fields (the brief): `title`(string, NOT nullable),
  `one_liner`(string, NOT nullable), `problem`(string, NOT nullable), `trigger`(string|null),
  `actors`(array of string), `steps`(array of string), `data_touched`(string|null), `edge_cases`
  (array of string), `open_questions`(array of string). `requiredWhen: {}`. `consistency`:
  `brief_has_substance` (title+one_liner+problem non-blank). `rulebook: ()=> "<interview guidance:
  collect title, problem, trigger, actors, steps, edge cases, open questions before dispatching>"`.
- `transcribe_audio` — `inputs: null` (dispatched via rails change (b); runs its own
  `!quoted?.hasAudio → noAudio` check).
- `assistant_settings` — unchanged from the pilot (`discriminator:null`, field `tags` array of
  string; consistency `tags are valid trigger tags`).

---

## Rails changes

**NOT `None.` — exactly the two authorized in SCOPE, both in
`secretary/1. Orchestrator/server.js`, both additive:**

1. **Per-flow discovery split** (change (a) above). *What is added:* `NEW_SKILLS_DIR`, a defaulted
   `dir` param on `loadSkills`, a second `loadSkills(NEW_SKILLS_DIR)` call producing
   `NEW_SKILLS`/`NEW_CATALOG`, `NEW_FLOW.catalog` repointed to `NEW_CATALOG`, and the six NEW-loop
   `SKILLS`/`CATALOG` references (`:651,:680,:683,:691,:724,:732`) repointed to the new maps. *Why
   the skill layer can't do it:* discovery is a single shared `SKILLS_DIR` today; making @mary load
   a different tree than @assistant is by definition orchestrator machinery (CONVENTIONS §1) — it is
   the enabling mechanism of the whole card. *Additive-only argument:* `loadSkills(dir=SKILLS_DIR)`
   preserves the sole existing zero-arg call; `SKILLS`/`CATALOG`/`CAPS`, `LEGACY_SKILLS`/
   `LEGACY_CATALOG`, `runLegacyFlow`, and `ctx.callSkill`/`ctx.hasSkill` are byte-for-byte
   unchanged. *Existing callers that must keep working:* the LEGACY flow (`runLegacyFlow`, reached
   at `:536`) reads only `LEGACY_SKILLS`/`LEGACY_CATALOG` (derived from the untouched old-tree
   globals) and `CAPS` (via `ctx.callSkill`) — none repointed; the legacy Tasks→Calendar
   `startCreate` delegation still resolves.
2. **`inputs:null ⇒ dispatch-without-validation`** (change (b) above). *What is added:* one branch
   in the `conversation==="orchestrator"` dispatch gate. *Why the skill layer can't do it:* the gate
   lives in the orchestrator; `checkPayload(null,…).ok===false` (lib/inputs.js:214) would otherwise
   trap `transcribe_audio` in the repair loop forever. *Additive-only argument:* the existing
   `checkPayload` gate is moved verbatim into the `else`; the new `inputs == null` branch is the
   only added path. *Existing callers:* declared-inputs orchestrator skills (`assistant_settings`,
   and every new declared skill) take the `else` and are unaffected.

**The two rails changes that don't look like one:** *A new `ctx` field* — **none** (every pure task
consumes only existing `ctx` fields: `ctx.info`, `ctx.quoted`, `ctx.hasQuotedAudio`, `ctx.send`,
`ctx.sendFailure`, `ctx.evolution`, `ctx.sessions`, `ctx.settings`, `ctx.anthropic`, `ctx.model`,
`ctx.env`, `ctx.lang`, `ctx.number`, `ctx.remoteJid`). *A new module in `lib/`* — **none** (no new
shared surface; the new skills copy their own helpers into the new tree rather than lifting into
`lib/`, preserving old/new isolation).

**Router catalog / live check.** This card adds seven new manifests (six with reworded
descriptions + new `inputs`) that the NEW flow's router renders via `NEW_CATALOG` →
`buildRouterSystem`. Per CONVENTIONS §1/§5 this **changes the NEW flow's classification problem and
requires the live `ANTHROPIC_API_KEY=… node scripts/router-selftest.mjs` check — a real-money call,
the human's decision, never run by the build.** Note: `scripts/router-selftest.mjs` currently
hardcodes `SKILLS_DIR = secretary/2. Skills` (`:29`); to validate the NEW catalog the coder must
point it (or a copy) at `secretary/3. Mary Skills` — a test-script edit, not rails. The old `2.
Skills/` tree and the legacy router are untouched, so @assistant's routing is unaffected. **→ FLAGS.**

---

## Sequence (the tree WORKS at every step; the old @assistant flow stays green throughout)

1. **Create the new tree, discovered but not routed-to.** Add `secretary/3. Mary Skills/` with all
   seven folders (copy-then-convert per §B). At this point nothing loads them → no runtime effect.
2. **Rails change (a): discovery + `NEW_FLOW.catalog`.** Add `NEW_SKILLS_DIR`, the defaulted
   `loadSkills` param, the second discovery call, the `NEW_FLOW.catalog = NEW_CATALOG` repoint, and
   the six NEW-loop reference repoints. Boot smoke test (`ANTHROPIC_API_KEY=dummy npm start`) must
   print `mary skills: calendar_action, …` (7 ids) AND the legacy `available skills:` line. @mary
   now routes to the new tree; @assistant unchanged. Order matters: the folders must exist (step 1)
   before this repoint, or `NEW_CATALOG` is empty.
3. **Rails change (b): the `inputs:null` gate branch.** Without it, `transcribe_audio` in the new
   tree would repair-loop. Adding it now (after the new transcribe exists) makes @mary transcribe
   work; declared skills are unaffected.
4. **Write the offline self-test** (`scripts/mary-skills-selftest.mjs`) and run it green.
5. **Docs** (§Documentation changes), then the plan-doc `git mv` on ship.
6. **Human gate:** the live `scripts/router-selftest.mjs` against the new catalog (money; not the
   build's to run).

Every skill conversion (step 1) is independent and leaves the old tree byte-for-byte, so the order
among the seven is free; do the two rails changes (steps 2–3) only after the folders they point at
exist. At no step is either flow half-wired: until step 2 the new tree is inert; after step 2 the
NEW flow is fully repointed in one commit; the LEGACY flow never changes.

---

## Tests

**`scripts/mary-skills-selftest.mjs` (NEW, offline).** Replicates the deterministic discovery layer
(mirrors `loadSkills`' `readdir`+`import`, no server boot) over both trees and asserts:

- **T1 — both trees discover cleanly.** `readdir`+dynamic-import every `skill.js` under
  `secretary/2. Skills/` and `secretary/3. Mary Skills/`; each exports `manifest.id` + a `run`
  function. Fails if any new folder is missing or throws at import.
- **T2 — same seven ids, per-flow maps DISJOINT.** Both trees expose the same id set
  `{calendar_action, transcribe_audio, task_action, feature_request, feedback, flight_search,
  assistant_settings}`; and for every id `newTree[id].run !== oldTree[id].run` (different modules →
  proves isolation, no shared code).
- **T3 — every new manifest is a pure task.** Each new skill has `conversation === "orchestrator"`.
- **T4 — declared inputs validate a READ and an ACT payload via `checkPayload`.** Import
  `checkPayload` from `lib/inputs.js`. For `calendar_action`: assert
  `checkPayload(inputs, {action:"find", …all-other-fields-null…}).ok === true` (READ passes
  completeness) and `checkPayload(inputs, {action:"create", start_iso:"2026-07-20T15:00:00-03:00",
  participants:[{name:"A",email:"a@b.com"}], …}).ok === true` (ACT). Same for `task_action`
  (`{mode:"list",ops:null?}` — note: `ops` present-but-null must be shape-valid → declare `ops`
  nullable so a `list` payload validates; assert `.ok` for `list` and for an `apply` with one op)
  and `flight_search` (`{intent:"search", origin,destination,depart_date, …}` and
  `{intent:"link", option_number:2, …}`). Assert an incomplete ACT payload (e.g. calendar
  `create` with `start_iso:null`) has `.ok === false` — proving the gate would repair, not
  mis-dispatch.
- **T5 — the `inputs:null` dispatch precondition.** Assert `transcribe_audio` manifest has
  `conversation === "orchestrator"` AND `inputs == null`, and that `checkPayload(null, {}).ok ===
  false` — documenting *why* rails change (b) exists (the plain gate would trap it). The actual
  dispatch-branch behaviour lives in `server.js` and is not offline-unit-testable (no server boot);
  this asserts the deterministic contract around it, per CONVENTIONS §5.

Each assertion would FAIL if the feature were absent: no new tree → T1/T2 fail; an unconverted skill
left as `conversation:"skill"` → T3 fails; a botched discriminator where a READ can't validate →
T4 fails; transcribe left declaring inputs or as `conversation:"skill"` → T5 fails.

**Existing selftests must still pass unchanged** (they target the untouched old tree):
`selflearning-selftest.mjs` (also lints the new tree's `sendFailure` usage — the new `*Error`/
`*Failed`/`noAudio`(if `sendFailure`) call sites must satisfy it), `turn-latency-selftest.mjs`,
`calendar-*-selftest.mjs`, `tasks-addressed-selftest.mjs`, `flights-selftest.mjs`,
`settings-*-selftest.mjs`, `history-selftest.mjs`. The build verifies each runs green.

**Live check (human-run, real money — NOT run by the build):**
`ANTHROPIC_API_KEY=… node scripts/router-selftest.mjs` against the NEW catalog (`3. Mary Skills`).
The router's *judgement* cannot be asserted offline; this is the human's gate. **→ FLAGS.**

---

## Documentation changes (written in this build)

- **`ARCHITECTURE.md`** — document the per-flow discovery split (`NEW_SKILLS_DIR`, `NEW_SKILLS`/
  `NEW_CATALOG`, `NEW_FLOW.catalog`), the isolated `secretary/3. Mary Skills/` tree, the pure-task /
  read-then-act pattern for the six converted skills, and the `inputs:null` dispatch path. Update
  any "adding a skill = no server.js change" note to reflect the now-two-tree discovery.
- **`PROJECT_LOG.md`** — **§10 dated changelog entry (always)** dated the real ship day (planned:
  2026-07-15): "New Architecture — @mary's full isolated skill stack (`3. Mary Skills/`), all seven
  skills converted to pure tasks; two additive server.js rails changes (per-flow discovery,
  `inputs:null` dispatch); @assistant unchanged." Update **§1** skill list (note the parallel Mary
  tree) and **§2** status (@mary now runs a fully converted stack).
- **`README.md`** — "Skills (today)" / roadmap: note @mary's isolated converted stack running in
  parallel with @assistant's legacy stack (A/B), pending the default flip (a later card).
- **`secretary/1. Orchestrator/ORCHESTRATOR.md`** — the two rails changes: two-tree discovery and
  the `inputs:null ⇒ dispatch-without-validation` gate path; note `CAPS` stays on the old tree.
- **`secretary/README.md`** — "adding a skill" now has two trees; note new-flow skills live in
  `3. Mary Skills/` and are discovered into `NEW_SKILLS`/`NEW_CATALOG`.
- **Seven new `secretary/3. Mary Skills/<N>/SKILL.md`** — one per skill, describing its pure-task
  shape (declared inputs, read-then-act where applicable, what it returns). Seed from the old
  `SKILL.md` and rewrite the "how it talks to the user" section (the orchestrator does).
- **Plan-doc archive on ship:** author `New Features Plans/new-architecture-convert-all-skills.md`
  (the shippable copy of this plan), then
  `git mv "New Features Plans/new-architecture-convert-all-skills.md" "Shipped Features/2026-07-15 - new-architecture-convert-all-skills.md"`
  (real ship date).

*No doc needs no change* is not claimed here — every file above changes.

---

## Migrations / config

- **None.** No new env var, no new Redis key space (settings reuse the existing `newSettings`
  namespaced store; the flights options sidecar reuses its existing `${remoteJid}|flights` key).
  `FEATURE_SPEC_DIR`, `ASSEMBLYAI_API_KEY`, `FLIGHT_CURRENCY`, Google OAuth — all already in `.env`.
- No new dependency, no test-runner, no `test/` dir (CONVENTIONS §5).

---

## Risks (where this plan is most likely wrong)

1. **Read-then-act redesign is the deepest change.** Calendar/tasks moving from skill-internal
   targeting (`matchEventTargets`, LLM `planTaskOps` `target_index`) to *orchestrator-picks-an-id-
   from-returned-candidates* is authorized (SCOPE §In-scope, memory) but is more than a mechanical
   strip. The biggest sub-risk: the READ step must return candidate ids the model can echo back
   into the ACT payload within `READBACK_CAP` (8192 B). If a `find`/`list` returns many events, the
   serialized read-back could truncate. Mitigation: the READ returns a lean candidate shape
   (`{event_id,title,start}`), not full event resources; verify at build with the smoke flow.
2. **`transcribe_audio` `noAudio` via `send` (RESOLVED).** Manager ruled `noAudio` uses plain
   `ctx.send` (guidance, not a malfunction) per server.js:490, overriding the SCOPE wording that
   said `sendFailure`. No self-learning capture for a non-bug. Closed — not a build risk.
3. **Router misroute risk is real and only catchable live.** Seven new manifest descriptions +
   `inputs` reshape the NEW router's catalog; a reworded description can misroute a skill this card
   never touched. Offline tests assert the deterministic layer only. The live `router-selftest.mjs`
   (human-run) is the real safety net — do not ship the default flip on the strength of offline
   tests. (Default flip is out of scope regardless.)
4. **Dirty working tree at the planned SHA** (see Planned-against). If the build starts on top of
   the uncommitted `M` changes, this card's diff is unreadable (CONVENTIONS §2 NO-GO). Needs a clean
   tree first.
5. **`feature_request` / `tasks` currently ignore `ctx.info`** (they re-extract in-skill). The
   conversion makes them *consume* `ctx.info` for the first time; the router's extraction accuracy
   for their declared inputs was never measured (unlike calendar's). If extraction is weak, the
   repair loop absorbs it (capped) rather than mis-acting — acceptable, but watch it in live @mary
   testing.
6. **Copy drift.** Seeding by copy means a later fix to an old-tree helper won't reach the new tree
   (and vice-versa) — the intended isolation, but a maintenance trap once the A/B ends. Out of scope
   here (the default-flip card retires the old tree wholesale), noted so it isn't a surprise.

---

ENTRY: PASS
  — `SCOPE.md` (revised) exists; both SCOPE_REVIEW blockers are addressed in it (transcribe rails
    change (b) authorized; `CAPS` explicitly left on the old tree). Verified against the code, not
    assumed.
WORK: Read the card (IDEA/SCOPE/SCOPE_REVIEW), the design memory, and CONVENTIONS; then read
  server.js, lib/inputs.js, the router, the reference settings skill, transcribe, feedback,
  feature_request, and (via three deep-read agents) calendar/tasks/flights fully. Designed the
  seven pure-task `inputs` contracts (READ values carry no requiredWhen), pinned the two additive
  server.js rails edits to exact lines, sequenced the build so both flows stay green, and specified
  the offline self-test. Wrote PLAN.md.
OUTPUT: /Users/marceloabritta/.manager-kanban/personal-whatsapp-ai/cards/plan/planning/e90940f6-new-architecture-convert-all-skills/PLAN.md
EXIT: MET
  — PLAN.md records the SHA; names every file (each new path marked NEW / copy-seeded); carries a
    signature for every manifest/run/inputs and both server.js edits with exact line points; the
    sequence leaves the tree working at each step; the tests are a standalone offline
    `scripts/mary-skills-selftest.mjs` that fails if the feature is absent; the Rails section is
    present with additive-only arguments and named legacy callers, and covers the "no new ctx field
    / no new lib module" cases; the Documentation section names every doc file incl. the
    PROJECT_LOG §10 entry and the plan-doc `git mv`; every user-facing string is planned into
    `prompt.js` with en/pt.
FLAGS:
  1. LIVE ROUTER CHECK (money, human's call) — seven new manifests reshape the NEW flow's router
     catalog. `ANTHROPIC_API_KEY=… node scripts/router-selftest.mjs` must run against the NEW tree
     (`3. Mary Skills/`); the script currently hardcodes `2. Skills` and needs a one-line path
     change to target the new catalog. The build must NOT run it; the human decides at the gate.
  2. RESOLVED by manager: `transcribe_audio` `noAudio` uses plain `ctx.send` (guidance, not a
     malfunction; server.js:490 rule wins over the SCOPE wording). No failure capture. Not a blocker.
  3. DIRTY WORKING TREE at the planned SHA (uncommitted `M` on calendar files, scripts, docs). The
     build needs a clean tree first (CONVENTIONS §2). Human to commit/stash/revert before build.
  4. Read-then-act is a genuine internal redesign of calendar/tasks (targeting moves to the
     orchestrator), authorized by SCOPE but the largest correctness surface — flagging so the coder
     sizes it and the build verifies the candidate→act id round-trip live on @mary.
