# Calendar Actions — Smart Scheduling & Edit (Implementation Plan)

Forward-looking plan for the calendar features still to build. The stateful
mechanism these rely on is built and live; it's documented in
[ORCHESTRATOR.md](../brain/1.%20Orchestrator/ORCHESTRATOR.md) (the gate, session shape, and
skill contract) and [ARCHITECTURE.md](../ARCHITECTURE.md) (the end-to-end flow).

## Status snapshot (as of 2026-07-11)

Done and in the codebase:
- `calendar_action` skill: **create** and **cancel/delete** events.
- **Stateful conversation layer** (Redis sessions): a flow starts on `@brain`, then
  continues without the tag; the brain uses the LLM to detect the awaited answer and
  ignores normal chatter. `awaitFrom` selects who may answer (owner / contact / any).
- Delete uses a confirm-first session: the owner just types `yes`/`no`.
- **Phase C** (create improvements): conversation-inferred titles, confirm-first create
  with a modify path, and the fully-stateful missing-field chase (C1 + C2/C3 merged).
- **Structured outputs** live at all four LLM call sites (`output_config.format`), on
  `@anthropic-ai/sdk ^0.111.0` — see the extraction section for details.
- **Multilingual layer** (commit `7e86855`): the flow's language is detected once and
  persisted on the session; every calendar reply is rendered per-`lang` and the session
  answers ("yes"/edits) in the language it started in. Woven through this skill's
  sessions, `reply(lang)`, and `localizeDate`.
- Message polish: `[AI Brain]:` header, `SECRETARY_TAG` single source, clean invite text.

Remaining: **edit/reschedule (Phase B)** — not yet built (no `handleEdit`; the
`CAL_SCHEMA` action enum is `create | delete | other`, no `edit`).

**Deploy status:** Phase C, structured outputs, and the multilingual layer are all
committed; whether they're running on the droplet still needs a production check
(`git log` on the droplet + a restart if it's behind). The manifest already advertises
"edit/reschedule," which oversells until Phase B lands.

---

## Phase C — Smart scheduling (create improvements)

The original goal: name the event by its topic, know how many people should attend,
and collect any missing emails — asking the owner *or the attendee themselves*.
Built in three testable steps.

### C1 — Conversation-inferred naming + confirm-first create (IMPLEMENTED — in codebase)
Merged with the "confirm before writing" improvement below — the two ship together:
inferring the title is safe precisely because the owner now confirms the draft.
- **Title from the conversation (not just an explicit order).** The LLM infers a
  concise `title` from what the meeting is *about*, using the whole chat — e.g. a
  clearly-budget discussion → "Q3 budget review". `title=null` when no subject is
  supported → code falls back to `Owner & names`. No hallucinated subjects.
- **Create is now stateful & confirm-first.** `handleCreate` builds a *draft* and,
  instead of writing to Google, opens a `create / await_confirmation` session
  (`awaitFrom:"owner"`) and shows it:
  `Confirm this event: / - title / - emails / - date & time / Reply "yes" … or tell me what to change`.
- **Resume (`resumeCreate`)** runs one LLM call that classifies AND re-drafts:
  `{decision: confirm | modify | cancel | unrelated, ...updated draft}`.
  - confirm → `events.insert` + notify + clear;
  - modify ("move to 4pm", "add bruno@x.com", "rename to Kickoff") → apply onto the
    draft, re-show, keep the session; if a change leaves a hole (e.g. removed the
    only email) it asks for it and stays open — a free mini-slot-fill;
  - cancel → clear; unrelated → ignore silently (same discipline as delete).
- **Files:** `prompt.js` (add `title` field + rule; add `buildCreateReviewSystem` /
  `buildCreateReviewUser`), `skill.js` (`draftFromInfo` / `renderCreateConfirm` /
  `openCreateConfirm` / `createFromDraft` / `resumeCreate` / `applyDraftUpdate` /
  `reviewCreate`; `run()` routes the create continuation).
- **Done when:** titled by inference when supported, clean fallback otherwise; create
  NEVER writes to Google without an explicit owner confirmation; the draft can be
  edited by plain reply before confirming.

### C2 + C3 — Fully-stateful gathering with a focused resolver (IMPLEMENTED — in codebase)
**Generalized well past the original C2/C3.** Every create is now stateful and always
converges on a session; there are **no re-tag dead-ends**. The email-only chase became a
single mechanism that gathers *any* required field — date/time, attendees, and each
attendee's email — asking whoever's in the chat (`awaitFrom:"any"`) until secure, then
handing off to the C1 confirm.

**Required to create** (everything else has a fallback and never blocks): a `start_iso`,
≥1 attendee, and an email for EVERY attendee. `missingOf` / `isComplete` compute this.

**The flow (`handleCreate` → `resolveDraft` → `advanceCreate`):**
1. Broad `interpret()` builds the draft (with inferred title, 45m default).
2. `resolveDraft`: if anything required is missing, a **focused second LLM pass**
   (`inspectMissing` / `buildResolveSystem`) re-inspects the chat + latest message
   *precisely* for those fields — higher resolution than the broad pass because it's
   told exactly what to look for. No call when nothing is missing.
3. `advanceCreate`: complete → `openCreateConfirm`; else `openInquiry` asks precisely
   for what's still missing (`renderInquiry` — single missing email keeps the "Ana, I'm
   missing your email…" phrasing; otherwise a composed "I still need …" line).
4. **Resume (`resumeInfo`, stage `await_info`, `awaitFrom:"any"`):** each message re-runs
   `inspectMissing`, `mergeDraft` folds in any resolved fields (fill emails by name; add
   newly-named attendees; bare-email single-case assigns directly), then progressed →
   ask for the rest / confirm; **nothing new resolved → stay silent** (chatter). Loops
   until `isComplete`, bounded by the 10-min session TTL.
- **Files:** `prompt.js` (`buildResolveSystem` / `buildResolveUser` replace the old
  email-only builders), `skill.js` (`missingOf` / `isComplete` / `sameMissing` /
  `missingDesc` / `resolveDraft` / `advanceCreate` / `renderInquiry` / `openInquiry` /
  `mergeDraft` / `resumeInfo` / `inspectMissing`; `run()` routes the `await_info`
  continuation).
- **Done when:** any missing required field is chased statefully from either party;
  the focused pass recovers fields the broad pass missed; ignores chatter; nothing is
  created until every field is secure AND the owner confirms.

---

## Extraction architecture — decisions & improvements

Notes on *how* create pulls its data, separate from the stateful flows above.

### What create actually requires (today)
Only **two** fields gate a create ([`skill.js` `handleCreate`](../brain/2.%20Skills/1.%20Calendar%20Actions/skill.js)):
- **`start_iso`** — a date/time (blocks if absent).
- **≥1 attendee email** — at least one email among the participants (blocks if none).

Everything else is defaulted: participant names → title fallback `Owner & Guest`,
`duration_min` → 45 min, `summary` → empty. C1/C2 above upgrade the naming and the
email handling.

### Decision: keep extraction as ONE LLM call (do not split per-field)
`interpret()` extracts action + participants + emails + time + duration in a single
Claude call. This is deliberate — **don't** decompose it into independent per-field
questions ("who?", then "their email?", then "when?"). The fields are **correlated**:
- deciding *who attends* needs the whole conversation;
- attributing an *email* needs to already know *who* the person is;
- resolving "tomorrow 3pm" needs the meeting context.

Independent calls lose that shared context (worse attribution), add latency and tokens,
and add orchestration code — with no accuracy gain for a small 4–5 field extraction.
Slot-filling across turns (C2/C3) is *not* the same thing: that re-runs the same single
extraction as new info arrives, it doesn't split one message into many calls.

### Improvement: confirm before writing to Google (create) — DONE, folded into C1
Delete confirms first; create fires immediately on a single, unverified JSON extraction,
so a mis-parsed time or wrong attendee only surfaces after the invite emails go out.
**Shipped as part of C1 (2026-07-11)** — the draft/confirm/modify flow above is exactly
this step, with an added *modify* path (edit the draft by plain reply, not just yes/no).
Original scope notes kept below for reference.
- **Scope:** after extraction succeeds and no slots are missing, show a summary
  (title / attendees / date-time / duration) and ask the owner to confirm before
  `events.insert`.
- **Reuses:** the exact `sessions` + `classifyConfirmation` machinery already proven in
  delete — open an `await_confirmation` session holding the event draft; `yes` creates,
  `no` discards, chatter is ignored.
- **Test:** "@brain schedule with ana@x.com tomorrow 3pm" → brain shows the draft and
  asks to confirm → `yes` → event created; a mis-parsed time is catchable at that step.
- **Done when:** create never writes to Google without an explicit owner confirmation.
- **Sequencing note:** fits naturally *after* C2 (both use the same session pattern);
  can also land standalone before C2 for immediate safety.

### Gap: per-attendee email coverage (not just "≥1 email") — CLOSED by C2+C3
Create used to require only **one** email among all participants, silently dropping the
rest (Ana + Bruno with only Ana's email → event with just Ana). `advanceCreate` now
chases every attendee missing an email before the confirm, so no one is dropped.

### Extract → focused second pass (DONE — the resolver)
The "targeted second call" idea landed as `inspectMissing` (C2+C3 above): a focused pass
that re-inspects the chat *only* for the fields the broad extraction left missing, before
asking a human. It's a **gap-filler**, triggered only when something required is absent —
not per-field decomposition (still avoided) and not a full-time verifier of already-present
fields. A pure verifier ("is any *present* field likely wrong?") remains a possible future
add, but the confirm step already gives the human that final check for free.

---

## Phase B — Edit / reschedule via reply

- **Scope:** reply to an event's calendar link with a change ("move to 4pm",
  "make it 30 min", "add carlos@x.com", "rename to Kickoff"); apply it, asking for
  clarification when ambiguous.
- **Files:** `prompt.js` (interpret returns `action:"edit"` + a `changes` object and a
  `clarify` field), `skill.js` (`handleEdit`: resolve the event id from the quoted
  link via the existing `resolveEventId`; `events.get` current state; build a patch;
  `events.patch({ sendUpdates:"all" })`; if `clarify` is set, open an
  `await_clarification` session — `awaitFrom:"owner"` — and resume with the answer).
- **Reuses:** `resolveEventId`, `getEvent`, the session/clarification pattern from C2.
- **Test:** reply to an invite with "@brain move it to 4pm" → patched + attendees
  notified; "add carlos@x.com" → added; ambiguous "move it earlier" → asks for the
  time, then applies the plain reply.
- **Done when:** reschedule / relength / add-remove attendee / rename all work;
  ambiguous requests clarify instead of guessing.

---

## Suggested order
C1 + create-confirm ✅ → C2/C3 email-chase ✅ (both in the codebase) → **Phase B (edit)**
is what's next. Each step: implement → deploy → test → next.

## Prompt-quality pass (IMPLEMENTED — in codebase)
Reviewed the four JSON-producing prompts. Applied:
- **Dropped dead fields** from the interpret schema (`prompt.js`): `confirm` (delete now
  runs entirely through the session/`classifyConfirmation` path) and `missing`
  (`advanceCreate` recomputes per-participant via `missingOf`) — both were extracted
  and never read. Fewer tokens, less model distraction.
- **`title` vs `summary`** disambiguated — title = short calendar heading, summary =
  longer event-body agenda.
- **Resolver contract** made structured (`buildResolveUser` takes
  `needsTime`/`needsAttendees`/`needEmailFor` instead of prose phrases).

## Structured outputs (IMPLEMENTED — in codebase)
The SDK-bump follow-up is **done**. `@anthropic-ai/sdk` is on `^0.111.0` and every calendar
LLM call passes a JSON Schema via `output_config.format` so the API returns only
schema-valid JSON:
- **Schemas** are the single source of truth for reply shape (`prompt.js`):
  `CAL_SCHEMA`, `CONFIRM_SCHEMA`, `REVIEW_SCHEMA`, `RESOLVE_SCHEMA` (each
  `additionalProperties:false` + full `required`, nullable unions for optionals). The
  prompts describe what fields MEAN; the schema enforces type/enum/shape.
- **`skill.js`** wraps them via `jsonFormat(schema)` and reads the guaranteed-valid reply
  with `readReply` at all four sites (`interpret`, `reviewCreate`, `inspectMissing`,
  `classifyConfirmation`).
- **`parseJsonReply` kept as a defensive fallback**, not retired: `readReply` falls back
  to it only if the model refuses or is ever swapped to one without structured-output
  support. It still strips ```json fences and does a BALANCED `{...}` scan (never the old
  greedy `/\{[\s\S]*\}/`).
- **Possible future add:** a pure verifier ("is any *present* field likely wrong?") — but
  the confirm step already gives the human that final check for free.

## Notes / smaller follow-ups
- Remove the temporary `QUOTED>>>` diagnostic log in `server.js` once the calendar
  flows are settled.
- The per-message LLM "is this the answer?" check runs only while a session is open;
  if cost matters, add a cheap pre-filter (e.g. only invoke when the message looks
  like it could contain the awaited info).
