# Skill: `calendar_action`

> **For humans — quick read.**
>
> Creates, edits, and cancels Google Calendar events from WhatsApp.
>
> **It handles three tasks:**
> 1. **Create** an event and email the invite to the attendees — **confirm-first**:
>    it shows you a draft and waits for your **`yes`** before writing anything.
> 2. **Edit/reschedule** an event you replied to — move it, change its length, rename it,
>    or add/remove an attendee. **Confirm-first**: it shows the updated event and waits
>    for your **`yes`**, and **stays open** so you can keep telling it changes ("actually
>    4:30", "also add bruno@x.com") before saving. Nothing is written until you confirm.
> 3. **Cancel/delete** an event you replied to — with a "type *yes* to confirm" step.
>
> **How you call it:**
> - Create: just `@secretary schedule this` — it reads the recent conversation for the
>   subject, who's invited, the time, and their emails. You don't have to spell it out
>   (though you can: `@secretary schedule a Q3 budget review with ana@example.com tomorrow 3pm`).
> - Edit: **reply to the invite message** (the one with the calendar link) with the change,
>   e.g. `@secretary move it to 4pm`, `@secretary make it 30 min`, `@secretary add carlos@example.com`,
>   `@secretary rename to Kickoff`. If it's ambiguous ("move it earlier") it asks, and you can
>   answer without re-tagging.
> - Cancel: **reply to the invite message** (the one with the calendar link) with
>   `@secretary cancel this`, then just type **`yes`** to confirm (no tag needed).
>
> If something needed is missing (the time, who to invite, or an attendee's email), it
> **asks and waits** — no re-tag — and the answer can come from you **or** the attendee.

> **Reused by other skills.** This skill exports a `capabilities.startCreate` entry —
> the full confirm-first create flow (draft → `yes` → invite, including chasing a missing
> email). `task_action` calls it (via `ctx.callSkill`) to turn a **to-do assigned to
> someone else** into a 5-minute invite, so that path is created and confirmed here, in
> the owner's language, with no duplicated code. The session it opens is a
> `calendar_action` session, so the `yes`/modify/email continuations are handled here.

## What you'll see (the full conversation)

Every secretary message is prefixed with the language-aware header — `[Marcelo's AI Secretary]:`
in English, `[Secretaria IA do Marcelo]:` in Portuguese (from `headerFor(lang)`) — and a blank line.

### Creating an event (confirm-first, and it waits for what's missing)

1. You (in a chat where the subject, people, time, and emails have come up):
   `@secretary schedule this`.
2. **The secretary never writes to Google first — it shows a draft and asks you to confirm**,
   then watches this chat for your answer for 10 minutes (no tag needed):
   > Confirm this event:
   > - &lt;title&gt;
   > - &lt;attendee emails&gt;
   > - &lt;date, hh:mm AM/PM&gt; (&lt;duration&gt; min)
   >
   > Reply "yes" to confirm and I'll send the invites, or tell me what to change and I'll adjust.
3. **What it picks up next** — any message you send while it's waiting:
   - a **yes**-type answer → it creates the event and emails the invite:
     > Done! Invite created and sent:
     >
     > - &lt;title&gt;
     > - &lt;attendee emails&gt;
     > - &lt;date, hh:mm AM/PM&gt; (&lt;duration&gt; min)
     >
     > Here is a link for the event:
     > &lt;calendar link&gt;
   - a **change** ("make it 4pm", "add carlos@x.com", "rename to Kickoff") → it edits
     the draft and shows the updated confirmation again.
   - a **cancel** ("no", "forget it", "deixa") → *"Okay, I won't create "&lt;title&gt;"."*
   - **anything else (normal conversation)** → **ignored silently**; it keeps waiting.
4. **If something needed is missing** (the secretary first re-inspects the chat for it, then
   asks only if it truly can't find it):
   - one attendee's email → *"Ana, I'm missing your email. Can you send it so I can add
     you to the invite?"* — and the answer may come from **you or from Ana herself**.
   - the date/time or who to invite → *"Before I can set this up, I still need the date
     and time / who to invite. Send it here and I'll continue."*
   Once every required detail is in, it rolls into the confirmation in step 2.
5. Other one-off replies: *"I understood the request but failed to create it in Google.
   Error in the log."* (Google error) · *"I didn't identify a calendar action. …"* (the
   order wasn't calendar-related) · *"I hit an error while thinking. Try again?"* (LLM error).

The **title** is inferred from what the meeting is about (e.g. "Q3 budget review"); if
the chat gives no subject it falls back to `Owner & <names>`. You can always fix it at
the confirm step ("rename to …").

### Editing / rescheduling an event (confirm-first, and it stays open)

1. You **reply to the invite message** (the one with the calendar link) with the change:
   `@secretary move it to 4pm` · `@secretary make it 30 min` · `@secretary add carlos@example.com` ·
   `@secretary remove ana@example.com` · `@secretary rename to Kickoff`.
2. **The secretary doesn't write to Google yet — it shows the updated event and asks you to
   confirm**, then watches this chat for your answer for 10 minutes (no tag needed):
   > Here's the updated event:
   > - &lt;title&gt;
   > - &lt;attendee emails&gt;
   > - &lt;date, hh:mm AM/PM&gt; (&lt;duration&gt; min)
   >
   > Reply "yes" to save and notify everyone, or tell me what else to change.
3. **What it picks up next** — any message you send while it's waiting (all tagless):
   - a **yes**-type answer → it saves the change and Google re-emails the attendees:
     > Done! Updated the event and notified the attendees:
     >
     > - &lt;title&gt; / - &lt;attendee emails&gt; / - &lt;date, hh:mm AM/PM&gt; (&lt;duration&gt; min)
     >
     > Here is a link for the event: &lt;calendar link&gt;
   - a **further change** ("actually 4:30", "also add bruno@example.com", "rename to
     Kickoff") → it folds it into the same draft and shows the updated confirmation again,
     **staying open** — so you can refine as many times as you like before saving.
   - a **cancel** ("no", "leave it", "deixa") → *"Okay, I'll leave "&lt;title&gt;" as it was."*
   - **anything else (normal conversation)** → **ignored silently**; it keeps waiting.
4. **If the first change is ambiguous** (e.g. "move it earlier" with no target time, or
   "add João" with no email on record), it asks a short question first:
   > About when should I move it to?

   Your plain reply ("4pm") is folded in and rolls into the confirmation in step 2.
5. If step 1 wasn't a reply to a message with a readable calendar link, or the event is
   already gone, you get a plain-language message explaining what to do (reply to the
   invite and try again).

### Cancelling an event (also waits for your answer)

1. You **reply to the invite message** (the one with the calendar link) with
   `@secretary cancel this`.
2. The secretary looks the event up and asks to confirm — watching this chat for your answer
   for 10 minutes, no tag needed:
   > Confirm the cancelation of this event?
   > - &lt;title&gt;
   > - &lt;date, hh:mm AM/PM&gt;
   >
   > Reply "yes" to confirm, or "no" to keep it.
3. **What it picks up next** — any message you send while waiting:
   - a **yes**-type answer → cancels and emails the attendees:
     > Cancelled "&lt;title&gt;" and notified the attendees.
   - a **no**-type answer → *"Okay, I'll keep "&lt;title&gt;"."*
   - **anything else** → **ignored silently**; it keeps waiting.
   - **after 10 minutes** with no yes/no → expires (start over with `@secretary cancel this`).
4. If step 1 wasn't a reply to a message with a readable calendar link, or the event is
   already gone, you get a plain-language message explaining what to do.

---

## For AI / maintainers — detailed

Source: `skill.js` (logic) + `prompt.js` (LLM prompts, the JSON Schemas, **and** the
localized user-facing reply strings). Contract: `export const manifest` +
`export async function run(ctx)`; auto-discovered by the orchestrator at boot.

**Localization:** every reply the skill sends comes from `prompt.js` — a per-language map
of render functions (`REPLY = { en, pt }`), selected with `reply(ctx.lang)` (fallback `en`);
dates via `localizeDate(ctx.lang, …)` (always 3-letter month + AM/PM; the locale sets
day/month order). List grammar and pluralization are rendered **per language** (never a
shared English builder). Sessions persist `lang` so the confirm/cancel/gather continuations
answer in the flow's language. Any language without a map is translated from the `en` copy
by the orchestrator's `send()` fallback; the reply header (produced per-language by
`headerFor(lang)`) and the LLM system prompts stay as-is. The example strings below are the **en** copy.

### Structured outputs (all six LLM calls)
Every LLM call passes `output_config: { format: { type: "json_schema", schema } }`, so the
API returns **only** schema-valid JSON. The six schemas live in `prompt.js` as the single
source of truth for reply *shape* (`CAL_SCHEMA`, `CONFIRM_SCHEMA`, `REVIEW_SCHEMA`,
`RESOLVE_SCHEMA`, `EDIT_SCHEMA`, `EDIT_REVIEW_SCHEMA`); the prompts describe what each field
*means*. In `skill.js`,
`jsonFormat(schema)` builds the `output_config`, and `readReply(msg)` reads it — guarding
`stop_reason:"refusal"` (→ `null`, a safe no-op) and falling back to `parseJsonReply`
(fence-strip + whole-parse + balanced-brace scan) if the model is ever swapped to one
without structured-output support. Requires `@anthropic-ai/sdk` ≥ 0.111 (installed on
container boot via `npm install`; the model is `claude-sonnet-5`).

### How it's invoked
- **Fresh command:** the router classifies an `@secretary` order as `calendar_action` and the
  orchestrator calls `run(ctx)`.
- **Continuation (stateful):** while a session is open, the orchestrator routes the awaited
  party's next messages here too (no router call) — `run(ctx)` sees `ctx.session` set and
  resumes. Create sessions use `awaitFrom:"any"` while gathering (owner **or** attendee)
  and `awaitFrom:"owner"` at the confirm step; delete uses `awaitFrom:"owner"`.

### Control flow — `run(ctx)`
Continuation checks first (each reads `ctx.session`):
1. `intent:"delete"` + `stage:"await_confirmation"` → `resumeDelete`.
2. `intent:"create"` + `stage:"await_info"` → `resumeInfo` (gathering).
3. `intent:"create"` + `stage:"await_confirmation"` → `resumeCreate` (yes/modify/cancel).
4. `intent:"edit"` + `stage:"await_clarification"` → `resumeEditClarify` (first request
   was ambiguous; roll into confirm once resolved).
5. `intent:"edit"` + `stage:"await_confirmation"` → `resumeEditConfirm` (yes/modify/cancel).

Otherwise **`interpret(ctx)`** — one Claude call (`buildSystem`/`buildUserPrompt`,
`CAL_SCHEMA`, `max_tokens: 4096`) that extracts from the **whole conversation**, not just
the order (`order` + `transcript` + `contact`), so a terse "schedule this" works. Schema:
```jsonc
{ "action": "create"|"delete"|"edit"|"other",
  "title": string|null,                              // inferred subject heading, or null
  "participants": [{ "name": string|null, "email": string|null }],
  "start_iso": string|null, "duration_min": number|null, "summary": string }
```
Dispatch on `action`: `delete` → `handleDelete`; `create` → `handleCreate`; `edit` →
`handleEdit`; else "I didn't identify a calendar action." (For `edit`, `interpret` only
**classifies** — the specific change is extracted later by a focused pass against the real
event; the other `CAL_SCHEMA` fields are ignored.)

### Task: CREATE — fully stateful, confirm-first
Required to create (everything else has a fallback and never blocks): a **date/time**,
**≥1 attendee**, and an **email for every attendee**. `missingOf(draft)` /
`isComplete(m)` compute this; `draftFromInfo` normalizes and applies fallbacks
(title → inferred or `Owner & names`; `duration_min` → 45).

**`handleCreate` → `resolveDraft` → `advanceCreate`:**
1. `interpret` builds the draft.
2. **`resolveDraft`** — if anything required is missing, a **focused second LLM pass**
   (`inspectMissing`, `buildResolveSystem`, `RESOLVE_SCHEMA`, `max_tokens: 2048`)
   re-inspects the chat + latest message *precisely* for the missing fields, told exactly
   what's missing via a structured contract (`needsTime` / `needsAttendees` /
   `needEmailFor`). `mergeDraft` folds in what it found (fill emails by name; add
   newly-named attendees; a lone bare email fills the one missing attendee). No LLM call
   when nothing is missing.
3. **`advanceCreate`** — complete → **`openCreateConfirm`** (session `await_info` →
   `await_confirmation`, `awaitFrom:"owner"`, `reply(lang).createConfirm`); incomplete →
   **`openInquiry`** (session `await_info`, `awaitFrom:"any"`, `reply(lang).inquiry` — a
   single missing email keeps the "Ana, I'm missing your email…" phrasing, otherwise a
   composed "Before I can set this up, I still need …"). Both `openCreateConfirm` and
   `openInquiry` persist `lang` in the session.

**`resumeInfo`** (every owner/attendee message while gathering): re-run `inspectMissing`,
`mergeDraft`; **nothing new resolved → return silently** (chatter); progressed →
`advanceCreate` (ask for the rest, or confirm). Loops until `isComplete`, bounded by the
10-min TTL.

**`resumeCreate`** (every owner message at the confirm step): one Claude call
(`reviewCreate`, `buildCreateReviewSystem`, `REVIEW_SCHEMA`, `max_tokens: 4096`) that both
classifies and, for a change, re-drafts → `confirm | modify | cancel | unrelated`.
- `confirm` → `createFromDraft` + clear session.
- `modify` → `applyDraftUpdate` then `advanceCreate` (re-show confirm, or chase a
  newly-missing email).
- `cancel` → clear + "Okay, I won't create …".
- `unrelated` → return silently.

**`createFromDraft` → `createEvent`** is **idempotent**: it first calls
`findConfirmedDuplicates` (title + exact start instant) and **reuses** an identical
confirmed event instead of inserting a duplicate; otherwise `events.insert` with
`sendUpdates:"all"` (Google emails the invite). Success → confirmation with `htmlLink`
(reworded when reused).

### Task: DELETE — `handleDelete` + `resumeDelete`
**Unchanged.** The target is found by **matching the event's captured identity against the
calendar**, not by trusting a decoded link alone. `interpret` fills `participants` (with
emails) and `start_iso` for deletes too.
1. **`handleDelete`:** gather `emails` + `start_iso` from `info` and `eidEventId =
   resolveEventId(quoted.calendarLink)` (may be null). Need the link id **or** both a start
   time and an email, else ask and stop. `matchEventTargets` (shared with edit) lists candidates (decoded
   id via `events.get`, plus every confirmed event at the start instant via `events.list`)
   and **scores** them: `+100` decoded-id, `+40` same start, `+30` attendee-email overlap;
   confident = **≥ 70**. No confident match → "couldn't find a matching event". Otherwise
   open a session (`intent:"delete"`, `stage:"await_confirmation"`, `awaitFrom:"owner"`,
   600 s) with the matched ids and send the confirm question.
2. **`resumeDelete`:** `classifyConfirmation` (one Claude call, `buildConfirmSystem`,
   `CONFIRM_SCHEMA`, `max_tokens: 1024`) → `confirm | decline | unrelated` (default
   `unrelated`). `unrelated` → silent. `decline` → clear + "I'll keep it". `confirm` →
   `cancelMeeting` (delete each matched id **and** sweep same-meeting duplicates via
   `findConfirmedDuplicates`, each `events.delete` with `sendUpdates:"all"`; `410` counts as
   success) → clear + "Cancelled…".

### Task: EDIT — `handleEdit` + `resumeEditClarify` + `resumeEditConfirm`
Change an existing event, resolved **like delete** (shared `matchEventTargets`) — a decoded
link **or** start-time + attendee-email match, so replying to the summary/confirm bubble (no
link) works too. **Confirm-first and stays open** (reuses
create's confirm/modify machinery): the change is folded into a **draft** of the event's
target state, shown for confirmation, and written to Google only on `yes`. While the confirm
session is open the owner can keep refining the same event tagless. **The draft** — seeded by
`editDraftFromEvent(ev)` — is `{ title, start_iso, duration_min, summary, emails[] }`;
`applyPatchToDraft(draft, patch)` folds a change onto it (overwrite touched fields; merge
attendees: case-insensitive remove then dedup add).

1. **`handleEdit`:** gather `emails` + `start_iso` from `info` and `eidEventId =
   resolveEventId(quoted.calendarLink)` (may be null) — same signals delete uses. Need the
   link id **or** both a start time and an email, else ask and stop. `matchEventTargets`
   returns the confident, confirmed-only matches; none → "couldn't find that event"; else
   patch the primary (`matches[0]`) — no separate `getEvent`/status recheck, the matcher
   already returns the full event. Then the **first-pass** `interpretEdit` (`buildEditSystem`,
   `EDIT_SCHEMA`, 2048) reads the change against the real event (`eventForLLM`) and returns
   only what changes:
   ```jsonc
   { "new_start_iso": string|null, "new_duration_min": number|null,
     "new_title": string|null, "new_summary": string|null,
     "add_emails": string[], "remove_emails": string[],
     "clarify": string|null }   // a question when the request is ambiguous/underspecified
   ```
   - `clarify` set **and** no concrete change (`hasEditChange` false) → open a session
     (`stage:"await_clarification"`, `awaitFrom:"owner"`, 600 s, holds only `eventId`) and
     ask the question.
   - a concrete change → `applyPatchToDraft(editDraftFromEvent(ev), patch)` → **`openEditConfirm`**
     (session `stage:"await_confirmation"`, `awaitFrom:"owner"`, holds `{ eventId, draft }`;
     `editConfirm` shows title / emails / when / duration). **Nothing written yet.**
   - no change and no `clarify` → `editNoChange`.
2. **`resumeEditClarify`** (`await_clarification`): re-`getEvent` (fresh; vanished/cancelled
   → clear) and re-run `interpretEdit` on the answer. Resolves to a concrete change → build
   the draft and roll into **`openEditConfirm`**. Still ambiguous / chatter → silent. This
   stage exists only for an ambiguous *first* request.
3. **`resumeEditConfirm`** (`await_confirmation`, every owner message): one review call
   **`reviewEdit`** (`buildEditReviewSystem`, `EDIT_REVIEW_SCHEMA`, 2048) — the change fields
   **plus** a `decision` — judged against the *proposed draft* (`draftAsEventJson`):
   - `confirm` → re-`getEvent` (still `confirmed`?) then **`applyEditDraft`**: patch
     `summary`/`description`/`start`/`end` (end = start + duration)/`attendees` from the
     draft, `events.patch({ sendUpdates:"all" })`, `editDone` from the returned event, clear.
   - `modify` → `hasEditChange` → `applyPatchToDraft` onto the draft + `openEditConfirm`
     (re-show, **keep open**); ambiguous further change (`clarify`, no change) → ask + keep
     open; nothing resolved → silent.
   - `cancel` → clear + `editCancelled` ("I'll leave it as it was").
   - `unrelated` / null (doubt/error) → silent (safe default).

### External APIs
- **Anthropic (Claude), all structured-output calls:** `interpret` (create/delete/edit
  classification + create/delete extraction, 4096) · `inspectMissing` (focused
  missing-field resolver, 2048) · `reviewCreate` (create confirm/modify/cancel + re-draft,
  4096) · `interpretEdit` (first-pass edit extraction, 2048) · `reviewEdit` (edit
  confirm/modify/cancel + re-draft, 2048) · `classifyConfirmation` (delete yes/no, 1024).
  Model = `ctx.model`.
- **Google Calendar (OAuth refresh token):** `events.list` (dedupe on create + match/sweep
  on delete), `events.get` (resolve a decoded link id; read current state on edit),
  `events.insert` (create), `events.patch` (edit, `sendUpdates:"all"`), `events.delete`
  (cancel). `sendUpdates:"all"` sends invite / change / cancellation emails.
- **WhatsApp:** all user-facing text via `ctx.send`.

### Stateful behavior, timeouts, completion
- **CREATE, DELETE, and EDIT are all stateful and confirm-first.** CREATE sessions:
  `await_info` (gathering, `awaitFrom:"any"`) then `await_confirmation` (`awaitFrom:"owner"`);
  DELETE: `await_confirmation` (`awaitFrom:"owner"`); EDIT: `await_clarification`
  (`awaitFrom:"owner"`, only when the first request is ambiguous) then `await_confirmation`
  (`awaitFrom:"owner"`) which **stays open across refinements** — each further change folds
  onto the draft and re-shows, until `yes`. All TTL **600 s (10 min)**, refreshed on each
  re-show.
- **Timeout:** no confirmation/answer within 10 min → the session expires; a later bare
  `yes`/answer is ignored (start over with `@secretary`).
- **Completes when:** CREATE → owner confirms and `events.insert` succeeds (message + link
  sent). DELETE → owner confirms and `events.delete` succeeds. EDIT → owner confirms and
  `events.patch` succeeds (message + link sent; session cleared). **No calendar write until
  the owner confirms**, for all three.
- **Failure modes:** every external call is wrapped; failures send a plain-language reply
  and clear the session where relevant. A model refusal or unparseable reply resolves to
  `null` → a safe no-op (nothing written). `classifyConfirmation` / `reviewCreate` failing
  → treated as `unrelated` (do nothing), the safe default.
