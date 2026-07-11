# Skill: `calendar_action`

> **For humans — quick read.**
>
> Creates, edits, and cancels Google Calendar events from WhatsApp.
>
> **It handles three tasks:**
> 1. **Create** an event and email the invite to the attendees — **confirm-first**:
>    it shows you a draft and waits for your **`yes`** before writing anything.
> 2. **Edit/reschedule** an event you replied to — move it, change its length, rename it,
>    or add/remove an attendee. Applies straight away and re-notifies; if the change is
>    ambiguous it asks a quick question first.
> 3. **Cancel/delete** an event you replied to — with a "type *yes* to confirm" step.
>
> **How you call it:**
> - Create: just `@brain schedule this` — it reads the recent conversation for the
>   subject, who's invited, the time, and their emails. You don't have to spell it out
>   (though you can: `@brain schedule a Q3 budget review with ana@example.com tomorrow 3pm`).
> - Edit: **reply to the invite message** (the one with the calendar link) with the change,
>   e.g. `@brain move it to 4pm`, `@brain make it 30 min`, `@brain add carlos@example.com`,
>   `@brain rename to Kickoff`. If it's ambiguous ("move it earlier") it asks, and you can
>   answer without re-tagging.
> - Cancel: **reply to the invite message** (the one with the calendar link) with
>   `@brain cancel this`, then just type **`yes`** to confirm (no tag needed).
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

Every brain message is prefixed with `[AI Brain]:` and a blank line.

### Creating an event (confirm-first, and it waits for what's missing)

1. You (in a chat where the subject, people, time, and emails have come up):
   `@brain schedule this`.
2. **The brain never writes to Google first — it shows a draft and asks you to confirm**,
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
4. **If something needed is missing** (the brain first re-inspects the chat for it, then
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

### Editing / rescheduling an event

1. You **reply to the invite message** (the one with the calendar link) with the change:
   `@brain move it to 4pm` · `@brain make it 30 min` · `@brain add carlos@example.com` ·
   `@brain remove ana@example.com` · `@brain rename to Kickoff`.
2. **If the change is clear**, the brain applies it right away (Google re-emails the
   attendees) and confirms with the new state:
   > Done! Updated the event and notified the attendees:
   >
   > - &lt;title&gt;
   > - &lt;attendee emails&gt;
   > - &lt;date, hh:mm AM/PM&gt; (&lt;duration&gt; min)
   >
   > Here is a link for the event:
   > &lt;calendar link&gt;
3. **If the change is ambiguous** (e.g. "move it earlier" with no target time, or "add
   João" with no email on record), it asks a short question and watches this chat for your
   answer for 10 minutes (no tag needed):
   > About when should I move it to?

   Your plain reply ("4pm") is applied; unrelated chatter is ignored while it waits.
4. If step 1 wasn't a reply to a message with a readable calendar link, or the event is
   already gone, you get a plain-language message explaining what to do (reply to the
   invite and try again). Edit is **not** confirm-first — it isn't destructive, so a clear
   change applies immediately.

### Cancelling an event (also waits for your answer)

1. You **reply to the invite message** (the one with the calendar link) with
   `@brain cancel this`.
2. The brain looks the event up and asks to confirm — watching this chat for your answer
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
   - **after 10 minutes** with no yes/no → expires (start over with `@brain cancel this`).
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
by the orchestrator's `send()` fallback; the `[AI Brain]:` header and the LLM system prompts
stay as-is. The example strings below are the **en** copy.

### Structured outputs (all five LLM calls)
Every LLM call passes `output_config: { format: { type: "json_schema", schema } }`, so the
API returns **only** schema-valid JSON. The five schemas live in `prompt.js` as the single
source of truth for reply *shape* (`CAL_SCHEMA`, `CONFIRM_SCHEMA`, `REVIEW_SCHEMA`,
`RESOLVE_SCHEMA`, `EDIT_SCHEMA`); the prompts describe what each field *means*. In `skill.js`,
`jsonFormat(schema)` builds the `output_config`, and `readReply(msg)` reads it — guarding
`stop_reason:"refusal"` (→ `null`, a safe no-op) and falling back to `parseJsonReply`
(fence-strip + whole-parse + balanced-brace scan) if the model is ever swapped to one
without structured-output support. Requires `@anthropic-ai/sdk` ≥ 0.111 (installed on
container boot via `npm install`; the model is `claude-sonnet-5`).

### How it's invoked
- **Fresh command:** the router classifies an `@brain` order as `calendar_action` and the
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
4. `intent:"edit"` + `stage:"await_clarification"` → `resumeEdit` (apply once resolved).

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
   time and an email, else ask and stop. `matchDeletionTargets` lists candidates (decoded
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

### Task: EDIT — `handleEdit` + `resumeEdit` (Phase B)
Change an existing event the owner **replied to**. Not confirm-first (an edit isn't
destructive); a clear change applies immediately, an ambiguous one asks first.
1. **`handleEdit`:** `eventId = resolveEventId(quoted.calendarLink)` — no link id → ask the
   owner to reply to the invite and stop. `getEvent` the current state; not `confirmed` →
   "couldn't find that event". Then a **focused pass** `interpretEdit` (`buildEditSystem`,
   `EDIT_SCHEMA`, `max_tokens: 2048`) reads the change request against the real event
   (`eventForLLM` gives it title / start / end / duration / attendee emails) and returns
   only what changes:
   ```jsonc
   { "new_start_iso": string|null, "new_duration_min": number|null,
     "new_title": string|null, "new_summary": string|null,
     "add_emails": string[], "remove_emails": string[],
     "clarify": string|null }   // a question when the request is ambiguous/underspecified
   ```
   - `clarify` set **and** no concrete change (`hasEditChange` false) → open a session
     (`intent:"edit"`, `stage:"await_clarification"`, `awaitFrom:"owner"`, 600 s) holding
     the `eventId`, and send the question.
   - a concrete change → **`applyEdit`**: build a minimal patch, carrying the current start
     / duration for the correlated time fields (changing only the start keeps the length;
     changing only the duration keeps the start). Attendees are merged from the current
     list — `remove_emails` filtered (case-insensitive), `add_emails` appended (deduped) —
     and only sent when they change. `events.patch({ sendUpdates:"all" })`; confirm from the
     **returned** event (`editDone` with the new time, duration, attendees, `htmlLink`).
   - no change and no `clarify` → `editNoChange` ("tell me the new time, duration, …").
2. **`resumeEdit`** (every owner message while `await_clarification` is open): re-`getEvent`
   (fresh state; vanished/cancelled → clear the session) and re-run `interpretEdit` against
   the answer. Resolves to a concrete change → `applyEdit` + clear. Still ambiguous or plain
   chatter (`hasEditChange` false) → **return silently** and keep waiting (same discipline
   as create/delete continuations), bounded by the 10-min TTL. Interpret error → keep the
   session so the owner can retry.

### External APIs
- **Anthropic (Claude), all structured-output calls:** `interpret` (create/delete/edit
  classification + create/delete extraction, 4096) · `inspectMissing` (focused
  missing-field resolver, 2048) · `reviewCreate` (confirm/modify/cancel judgment +
  re-draft, 4096) · `interpretEdit` (focused edit resolver, 2048) · `classifyConfirmation`
  (delete yes/no, 1024). Model = `ctx.model`.
- **Google Calendar (OAuth refresh token):** `events.list` (dedupe on create + match/sweep
  on delete), `events.get` (resolve a decoded link id; read current state on edit),
  `events.insert` (create), `events.patch` (edit, `sendUpdates:"all"`), `events.delete`
  (cancel). `sendUpdates:"all"` sends invite / change / cancellation emails.
- **WhatsApp:** all user-facing text via `ctx.send`.

### Stateful behavior, timeouts, completion
- **CREATE, DELETE, and EDIT can all be stateful.** CREATE sessions: `await_info`
  (gathering, `awaitFrom:"any"`) then `await_confirmation` (`awaitFrom:"owner"`); DELETE:
  `await_confirmation` (`awaitFrom:"owner"`); EDIT: `await_clarification`
  (`awaitFrom:"owner"`) — opened **only** when a change is ambiguous, otherwise edit is
  a one-shot apply with no session. All TTL **600 s (10 min)**.
- **Timeout:** no confirmation/answer within 10 min → the session expires; a later bare
  `yes`/answer is ignored (start over with `@brain`).
- **Completes when:** CREATE → owner confirms and `events.insert` succeeds (message + link
  sent). DELETE → owner confirms and `events.delete` succeeds (session cleared). EDIT →
  the change is resolved and `events.patch` succeeds (message + link sent; any session
  cleared).
- **Failure modes:** every external call is wrapped; failures send a plain-language reply
  and clear the session where relevant. A model refusal or unparseable reply resolves to
  `null` → a safe no-op (nothing written). `classifyConfirmation` / `reviewCreate` failing
  → treated as `unrelated` (do nothing), the safe default.
