# Skill: `calendar_action`

> **@mary tree — CONVERTED (pure task).** This is the `secretary/3. Mary Skills/` copy. The
> **orchestrator** runs the whole dialogue; this skill never proposes, confirms, classifies, or
> opens a session, and it exports **no** `capabilities` (the old-tree `startCreate` coupling is
> dropped — a task for someone else is the model chaining a `calendar_action` create itself).
> `manifest.conversation:"orchestrator"`, and `run(ctx)` is a pure dispatch on `ctx.info.action`
> that **returns** a JSON value the model reads back. Declared `inputs` (discriminator
> `action ∈ find|list|create|edit|delete|other`) follow a **READ-then-ACT** contract: `find`/`list`
> READ the calendar and return id-bearing candidates (`{event_id,title,start,…}`), sending nothing
> (find) or the rendered schedule (list); `create` writes the event and returns `{ok,link,eventId}`;
> `edit`/`delete` target the `event_id` the model read back and return `{ok,…}`. The deterministic
> Google / RRULE / all-day helpers below are unchanged. Everything below about the skill *proposing*
> or *confirming* is now the orchestrator's job, not this skill's.

> **For humans — quick read.**
>
> Creates, edits, cancels, and reads Google Calendar events from WhatsApp.
>
> **It handles four tasks:**
> 1. **Create** an event and email the invite to the attendees — **confirm-first**:
>    it shows you a draft and waits for your **`yes`** before writing anything. Handles
>    **recurring** events too ("every Monday", "every 2 weeks until August", "5 times",
>    "daily", "on the 5th every month") — the draft states the repeat in words, and it writes
>    a real repeating Google event. *(Create-only: editing or cancelling a recurring event
>    currently affects a single occurrence, not the series.)*
> 2. **Edit/reschedule** an event you replied to — move it, change its length, rename it,
>    or add/remove an attendee. **Confirm-first**: it shows the updated event and waits
>    for your **`yes`**, and **stays open** so you can keep telling it changes ("actually
>    4:30", "also add bruno@x.com") before saving. Nothing is written until you confirm.
> 3. **Cancel/delete** an event you replied to — with a "type *yes* to confirm" step.
> 4. **Read/list** what's on the calendar — a **read-only** query ("what's on tomorrow?",
>    "anything Friday afternoon?", "what's my next meeting?"). No draft, no confirm,
>    **nothing is ever written**; it just replies with the events.
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
> - Read: just ask — `@secretary what's on my calendar tomorrow?`,
>   `@secretary do I have anything Friday afternoon?`, `@secretary what's my next meeting?`.
>   It replies right away; there's no confirm step because nothing is written.
>
> If something needed is missing (the time, or an attendee's email), it **asks and waits** — no
> re-tag — and the answer can come from you **or** the attendee. Every truthful answer works:
> *"nobody, it's just me"* (an event with **no guests** is fine), *"I don't have her email"* (it
> creates the event **without her, and tells you so**), and *"forget it"* (it drops the whole
> thing).

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
   > - &lt;attendee emails, or **"(no guests)"** when there are none&gt;
   > - &lt;**"Without Laura — I don't have their email."**, only when someone is being left out&gt;
   > - &lt;date, hh:mm AM/PM&gt; (&lt;duration&gt; min)
   >
   > Reply "yes" to confirm and I'll send the invites, or tell me what to change and I'll adjust.
3. **What it picks up next** — any message you send while it's waiting:
   - a **yes**-type answer → it creates the event and emails the invite:
     > Done! Invite created and sent:
     >
     > - &lt;title&gt;
     > - &lt;attendee emails, or **"(no guests)"**&gt;
     > - &lt;date, hh:mm AM/PM&gt; (&lt;duration&gt; min)
     >
     > **I created it without inviting Laura — I don't have their email.** *(only when someone
     > was left out — a guest is never dropped without you being told)*
     >
     > Here is a link for the event:
     > &lt;calendar link&gt;
   - a **change** ("make it 4pm", "add carlos@x.com", "rename to Kickoff") → it edits
     the draft and shows the updated confirmation again.
   - a **cancel** ("no", "forget it", "deixa") → *"Okay, I won't create "&lt;title&gt;"."*
   - **anything else (normal conversation)** → **ignored silently**; it keeps waiting.
4. **If something needed is missing** (the secretary first re-inspects the chat for it, then
   asks only if it truly can't find it) — only the **date/time** and a named guest's **email**
   can be missing; an event with **no guests at all** is complete and goes straight to the
   confirmation:
   - one attendee's email → *"Ana, I'm missing your email. Can you send it so I can add
     you to the invite?"* — and the answer may come from **you or from Ana herself**.
   - the date/time → *"Before I can set this up, I still need the date and time. Send it here
     and I'll continue."*
   While it waits, your next message is read as one of four things: an **answer** (including
   *"I don't have her email"* → it books **without her and says so**, and *"don't invite Laura"*
   → she is dropped from the draft), a **cancel** (*"forget it"*, *"esquece"* → *"Okay, I won't
   create …"*, and the pending event is discarded), or **ordinary conversation** → **ignored
   silently**; it keeps waiting. Once every required detail is in, it rolls into the
   confirmation in step 2.
5. Other one-off replies: *"I understood the request but failed to create it in Google.
   Error in the log."* (Google error) · *"I didn't identify a calendar action. …"* (the
   order wasn't calendar-related) · *"I hit an error while thinking. Try again?"* (LLM error).

The **title** is a meaningful **topic** — what the event is about (e.g. "Q3 budget
review"). A participant-shaped label ("Meeting with John") does NOT count as a subject.
Only when the chat gives **no subject** does it fall back to the participants' names,
owner first, joined with "/": `Owner/<names>` (e.g. "Marcelo/John"). You can always fix
it at the confirm step ("rename to …").

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

### Reading / listing what's on the calendar (read-only, instant)

1. You just ask — no reply-to needed:
   `@secretary what's on my calendar tomorrow?` · `@secretary do I have anything Friday
   afternoon?` · `@secretary what's my next meeting?`.
2. It replies immediately with the events in the window it understood — **nothing is ever
   written, and there's no confirm step**. Events are **grouped by day**: a date header, then
   each event as a `time - title` line with any **external** attendees' emails on the line
   below, a blank line between events and before each new day:
   > Jul 13, 2026
   > 9:00 AM - Standup
   >
   > 3:00 PM - Q3 budget review
   > ana@example.com
   >
   > Jul 14, 2026
   > All day - Gym
   - Each event line shows the **time only** (the day is the header); all-day events show
     "All day". Only **external** attendees are listed (the owner's own entry is dropped);
     events with no guests show just the `time - title` line.
   - **"What's my next meeting?"** → it scans forward and shows just the next upcoming event
     (*"Your next event: …"*), or *"Nothing coming up … in the next two weeks."*
   - **No time given** ("what's on my calendar?") → it defaults to the **rest of today** and
     the header names that day, so you know what it assumed.
   - **Empty window** → *"Nothing on your calendar for Jul 12, 2026."*
   - **Very large window** (e.g. "this month") → capped at the first 50, with a
     *"(Showing the first 50.)"* note rather than silently truncating.
3. If the calendar read fails, you get *"I hit an error reading the calendar. Try again?"* —
   no crash, nothing changed.

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
API returns **only** schema-valid JSON. This skill's schemas live in `prompt.js` as the single
source of truth for reply *shape* (`CAL_SCHEMA`, `REVIEW_SCHEMA`, `RESOLVE_SCHEMA`,
`EDIT_SCHEMA`, `EDIT_REVIEW_SCHEMA`); the prompts describe what each field
*means*. `CONFIRM_SCHEMA` is **shared** — it lives in `1. Orchestrator/lib/confirm.js` with the
rest of the confirm-first machinery.
The helpers are shared too (`1. Orchestrator/lib/llm.js`): `jsonFormat(schema)` builds the
`output_config`, and `readReply(msg, "calendar")` reads it — guarding
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

Otherwise the skill needs an extracted `info` — and **usually it already has one**:

```js
let info = ctx.info ?? null;          // the router already extracted it — no second call
if (!info) info = await interpret(ctx);   // …or it didn't: our own extraction call
```

**`ctx.info` — the pre-extracted payload (card 9af6967a).** The skill DECLARES its inputs in
`manifest.inputs`, and the orchestrator's router asks the model to fill them **in the same call
that classifies the order**. Plain code (`1. Orchestrator/lib/inputs.js`) validates the reply
against the declaration; a shape-valid payload arrives as `ctx.info`, in **exactly the field
names of `CAL_SCHEMA`** — which is what makes it a drop-in and why `handleCreate`,
`handleDelete`, `handleEdit` and `handleList` needed no changes at all. A fresh create is now
**one** LLM call before the reply instead of three. User-visible behaviour is unchanged: it is
faster, not different.

> 🔴 **`manifest.inputs.fields` MUST equal `CAL_SCHEMA.required`, as a set — all TWELVE names.**
> That binding is load-bearing, and it is a new way to break this skill *silently*: add a field
> to `CAL_SCHEMA` and forget the declaration, and the merged prompt simply stops asking for it,
> `draftFromInfo` reads `undefined`, and the feature that field implements dies **without a
> single test going red.** It has already happened once (`all_day`). `scripts/turn-latency-selftest.mjs`
> **T2.10** asserts set-equality. **If a future card makes it red, update the declaration —
> never loosen the lint.**

**`interpret(ctx)` is NOT deleted — it is the fallback**, and it still runs whenever `ctx.info`
is absent: a shape-invalid payload, or a dual-intent turn (`["feedback","calendar_action"]`)
where the payload belonged to the *other* skill and this one is handed `null` on purpose. It is
also still the extractor on every continuation path. So no capability depends on the merge
working — the worst case is that the turn is as slow as it used to be.

**`interpret(ctx)`** — one Claude call (`buildSystem`/`buildUserPrompt`,
`CAL_SCHEMA`, `max_tokens: 4096`) that extracts from the **whole conversation**, not just
the order (`order` + `transcript` + `contact`), so a terse "schedule this" works. Its rules
live in **`buildExtractionRules(owner)`** (`prompt.js`) — the same text, verbatim, that
`manifest.inputs.rulebook()` hands the merged router call, so the two extractions read from
one rulebook and cannot drift. Schema:
```jsonc
{ "action": "create"|"delete"|"edit"|"list"|"other",
  "title": string|null,                              // inferred subject heading, or null
  "participants": [{ "name": string|null, "email": string|null }],
  "start_iso": string|null, "duration_min": number|null, "summary": string,
  "all_day": boolean|null,                           // "o dia inteiro" / "all day"
  "all_day_end_iso": string|null,                    // a RANGE: the LAST day COVERED, inclusive
  "list_mode": "window"|"next"|null,                 // action="list" only; null otherwise
  "range_start_iso": string|null, "range_end_iso": string|null }  // the list window
```
Dispatch on `action`: `delete` → `handleDelete`; `create` → `handleCreate`; `edit` →
`handleEdit`; `list` → `handleList`; else "I didn't identify a calendar action." (For
`edit`, `interpret` only **classifies** — the specific change is extracted later by a focused
pass against the real event; the other `CAL_SCHEMA` fields are ignored. The three
`list_*`/`range_*` fields are `null` for every non-`list` action and ignored by those
handlers.)

### Task: CREATE — fully stateful, confirm-first
Required to create (everything else has a fallback and never blocks): a **date/time**, and an
**email for every named guest the owner has not said he lacks one for**. **Zero guests is a
complete, ordinary event** — an event has 0–n outside guests. `missingOf(draft)` /
`isComplete(m)` compute this; `draftFromInfo` normalizes and applies fallbacks
(title → meaningful topic, else `Owner/names`; `duration_min` → 45).

#### ALL-DAY events
"o dia inteiro" / "o dia todo" / "all day" produces a **real Google all-day event** — the one
in the strip at the top of the day — not a 24h timed block. Two fields carry it from
`interpret` through the draft, the confirm bubble and the "sim", to `events.insert`:

- **`all_day`** (bool). `start_iso` is **still required and still filled** (the FIRST day at
  00:00 -03:00): the DAY is *derived* from it in `CAL_TZ` (`localDayStr`), which is why
  `missingOf().noTime` still guards the null-start → 1970 write. `duration_min` is ignored.
- **`all_day_end_iso`** — a multi-day RANGE ("segunda a quarta"): the **LAST day the event
  still COVERS, INCLUSIVE**, at 00:00 -03:00. `null` = a single day. Never required, so
  gathering never chases it (`RESOLVE_SCHEMA` does not carry it; `mergeDraft` carries it
  over from the previous draft instead).

> ⚠️ **Google's `end.date` is EXCLUSIVE.** The draft, the model, the confirm bubble and this
> doc all speak **inclusive** days; the conversion happens in **exactly one place**,
> **`allDayWireDates(draft)`** (`end_date = addDays(last_date, 1)`) — called by
> `createFromDraft` *and* by `applyEditDraft`, which is why it is a function and not a line.
> A single day on 2026-07-14 is `start.date 2026-07-14` / `end.date 2026-07-15`. **Mon 13 →
> Wed 15 is `start.date 2026-07-13` / `end.date 2026-07-16` — a THURSDAY.** Off by one is a
> 2-day event, or a zero-day one Google rejects.

Two sanity clamps, both in **`normalizeAllDay(start_iso, all_day, all_day_end_iso)`** (called
by `draftFromInfo` on create and by `applyPatchToDraft` on edit), both **silent** — the owner
SEES the result in the confirm bubble before anything is written, and confirm-first is the
safety net: an end day **before** the start day is dropped (→ a single-day event), and a span
longer than `MAX_ALL_DAY_DAYS` (31) is **clamped** to it.

**`allDayFromEvent(ev)`** is the READ direction — the inverse — turning a real Google event
back into `{ all_day, start_iso, all_day_end_iso }`: the inclusive last day is
`addDays(ev.end.date, -1)`, and `start_iso` is the event's day at 00:00 -03:00 **because an
all-day event has no `start.dateTime` at all**. Without that seeded start an edit draft has no
day, and a *rename* would reach `allDayWireDates` with a null start and land the event in 1970.

The when-line is rendered by **`localizeWhen(lang, draft)`** (prompt.js): timed →
`localizeDate`; all-day single → `14 de jul. de 2026 · Dia todo`; all-day range → both
endpoints **inclusive** plus the **DAY COUNT** — `13 de jul. de 2026 – 15 de jul. de 2026 ·
Dia todo (3 dias)`. The count is the owner's sanity check: a wrong range that *reads* like a
right one is the real danger. `createConfirm`/`createDone` are passed `duration: null` for an
all-day event, so the `(N min)` suffix disappears entirely. The words "All day"/"Dia todo"
are the ones the READ side (`eventBlock`) already prints.

**EDITING an all-day event works** (card 64ff1f1d) — through the same confirm-first flow as
any other event: move it to another day, change its multi-day range, flip a timed event to
all-day and back. It reuses this exact shape (`all_day` / `all_day_end_iso`, inclusive days,
the same 31-day clamp) — there is **no second all-day model**. See §Task: EDIT.

`applyEditDraft` used to carry a **guard** (`if (draft.start_iso && !draft.all_day)`) that
**refused** to write a start/end for an all-day event, because the only shape it knew how to
write was a `dateTime` one — which would have silently converted the owner's all-day event
into a 45-minute block. **That guard's intent is honoured, not deleted:** the all-day branch
now writes the correct wire shape (`start:{date}` / `end:{date}`), so there is nothing left
to refuse. Read the guard's replacement — **THE RULE** on `new_all_day` — under §Task: EDIT
before touching that path.

**The rule the predicate encodes:** *a required field is legitimate only if a TRUTHFUL answer
can satisfy it.* The old **≥1-attendee** invariant failed that test ("nobody, it's just me" was
unrepresentable) and is **gone**. The email requirement passes it *only because the answer now
exists*: `no_email_for[]` → a participant's **`noEmail`** flag, meaning *the owner has told us he
hasn't got it*. The field is still required; it is now **answerable**.

> ⚠️ **`noTime` stays required, and must.** `createFromDraft` does `new Date(draft.start_iso)` —
> with a null start that is `new Date(null)` = the UNIX epoch, and the event is written to Google
> **in 1970**. `missingOf().noTime` is the **only** guard against that write.

**`handleCreate` → `resolveDraft` → `advanceCreate`:**
1. `interpret` builds the draft.
2. **`resolveDraft`** — if anything required is missing, a **focused second LLM pass**
   (`inspectMissing`, `buildResolveSystem`, `RESOLVE_SCHEMA`, `max_tokens: 2048`)
   re-inspects the chat + latest message *precisely* for the missing fields, told exactly
   what's missing via a structured contract (`needsTime` / `needEmailFor`) and whether this
   message is an answer or the original order (`gathering`). `mergeDraft` folds in what it
   found: the resolver's `participants` is the **FULL, AUTHORITATIVE list and REPLACES** the
   draft's (`null` = "nothing to add", so the list is kept; `[]` = "no outside guests", so it is
   emptied) — with one fallback kept ahead of it: a lone **bare email** fills the single
   attendee still missing one, so a guest can answer with nothing but her address. Names in
   `no_email_for` get `noEmail: true`. No LLM call when nothing is missing.
3. **`advanceCreate`** — complete → **`openCreateConfirm`** (session `await_info` →
   `await_confirmation`, `awaitFrom:"owner"`, `reply(lang).createConfirm`); incomplete →
   **`openInquiry`** (session `await_info`, `awaitFrom:"any"`, `reply(lang).inquiry` — a
   single missing email keeps the "Ana, I'm missing your email…" phrasing, otherwise a
   composed "Before I can set this up, I still need …"). Both `openCreateConfirm` and
   `openInquiry` persist `lang` in the session.

**`resumeInfo`** (every owner/attendee message while gathering — `awaitFrom:"any"`, so it hears
the whole chat): one `inspectMissing` call **classifies and resolves at once**, returning
`confirm | modify | cancel | unrelated` alongside the fields. It no longer infers "was that for
me?" from a field diff (`sameMissing` is **gone**), which is what met every truthful answer the
code had no field for with **total silence**.
- `unrelated` → **return silently** (chatter — the only silent exit left).
- `cancel` → **clear the session** + "Okay, I won't create …" (`createCancelled`). Clearing is
  the load-bearing half: an abandoned draft left armed can be resurrected by a stray message.
- `confirm` / `modify` → `mergeDraft` then `advanceCreate` (ask for the rest, or confirm).
- a **null patch** (API error / refusal / unparseable) → `ctx.sendFailure` + `thinkingError` —
  reported, not swallowed.

Loops until `isComplete`, bounded by the 10-min TTL.

**`resumeCreate`** (every owner message at the confirm step): one Claude call
(`reviewCreate`, `buildCreateReviewSystem`, `REVIEW_SCHEMA`, `max_tokens: 4096`) that both
classifies and, for a change, re-drafts → `confirm | modify | cancel | unrelated`.
- `confirm` → `createFromDraft` + clear session.
- `modify` → `applyDraftUpdate` then `advanceCreate` (re-show confirm, or chase a
  newly-missing email). `applyDraftUpdate` tests `Array.isArray(review.participants)`, **not**
  `.length`: an **emptied** guest list is an *answer* ("don't invite anyone") and sticks; only an
  absent list falls back to the draft's. It carries each person's known email and `noEmail`
  across, so a title-only change never resurrects an email question already answered.
  `REVIEW_SCHEMA` also carries **`all_day` / `all_day_end_iso`** (so *"na verdade, o dia todo"* /
  *"só até terça"* work at the confirm step), folded with `??` — a modify that says nothing about
  them (a rename) **keeps** them; an explicit `false` turns all-day off.
- `cancel` → clear + "Okay, I won't create …".
- `unrelated` → return silently.

**Never drop a person silently.** `createConfirm` and `createDone` both render the guests line as
the email list, or **`(no guests)` / `(ninguém convidado)`** when there are none — never a bare,
empty `- ` bullet. And when a named guest is being left out because the owner said he has no email
for them (`draftUninvited`), both say so outright: *"- Without Laura — I don't have their email."*
in the draft, and *"I created it without inviting Laura — I don't have their email."* on the
confirmation. A guest is never dropped without the owner being told.

**`createFromDraft` → `createEvent`** is **idempotent**: it first calls
`findConfirmedDuplicates` (title + exact start instant) and **reuses** an identical
confirmed event instead of inserting a duplicate; otherwise `events.insert` with
`sendUpdates:"all"` (Google emails the invite). `draftEmails` drops null emails, so a guest
with no address is simply not on the invite. Success → confirmation with `htmlLink`
(reworded when reused).

`createEvent` branches **only the request body**: an all-day event is
`start:{date}/end:{date}` (the shape `toListItem` already recognises on the read side); a timed
one is `start:{dateTime}/end:{dateTime}` + `timeZone`, unchanged. `summary` / `description` /
`attendees` / `sendUpdates:"all"` are the same for both. `findConfirmedDuplicates` had to learn
the all-day case too — it filters on `e.start?.dateTime`, so it was **blind** to an all-day
event, and dedupe-on-create would have silently stopped working for exactly these events: it
now queries the **start day's** window and matches `start.date` **and** `end.date` (both, so a
Mon–Wed order does not dedupe against a Monday-only event). The timed branch keeps its ±60s
window and `dateTime` equality byte for byte, and the delete sweep — its other caller — passes
no all-day flag, so its behaviour is unchanged.

#### RECURRING events (create-only)
A create order that asks for a **repeat** ("every Monday", "toda segunda", "every 2 weeks until
August", "a cada 2 semanas até agosto", "5 times", "daily", "on the 5th every month") produces a
**real recurring Google event** — an RRULE on `events.insert` — instead of a single occurrence.
`start_iso` stays the **first** occurrence; the confirm and done bubbles state the repeat in
words (en/pt), e.g. *"Every week on Mon, 5 times"* / *"A cada 2 semanas às seg, qua"* / *"Todo
dia até 30 de ago. de 2026"*.

- **The extracted shape** is `recurrence = {freq, interval, byday, count, until}` or `null` (a
  one-off — the default). It is the **twelfth** `CAL_SCHEMA`/`manifest.inputs.fields` field, and
  it rides `REVIEW_SCHEMA` too so the confirm step can add / change / **clear** it. It is kept
  **RAW** on the draft — `toRRule` is its single validator, never `missingOf` (a null recurrence
  is an ordinary one-off, never a missing field).
- **The v1 patterns:** daily; weekly-by-day (`byday` = `["MO".."SU"]`, weekly only); an
  `interval` (every N days/weeks/months); a `count` (N occurrences); an `until` end date; and
  **day-of-month monthly** (repeats on `start_iso`'s day-of-month). **OUT of v1** (all →
  `recurrence = null`, a one-off): monthly-by-weekday ("first Monday of the month"), yearly,
  `EXDATE`/`RDATE`, and **series edit/delete**.
- **The compile layer** is `toRRule(rec, {allDay, startIso})` / `toRRuleUntil(untilIso, allDay)`
  (skill.js, exported, pinned offline by `scripts/calendar-recurrence-selftest.mjs`). Part order
  is fixed `FREQ ; INTERVAL ; BYDAY ; (COUNT | UNTIL)`. **COUNT XOR UNTIL** — RRULE forbids both,
  so a `count` wins and the `until` is dropped. A **past `until`** (at/before `start_iso`), an
  unparseable `until`, an unknown `freq`, or a `null` rec all compile to `null` → the event is
  written as a **one-off**, no error (confirm-first is the backstop). `recurrenceLineFor` gates
  the confirm/done line on the SAME `toRRule` call, so the shown text and the written rule can
  never diverge.
- **All-day recurring is supported**, with a **value-type-correct `UNTIL`** (RFC 5545: `UNTIL`
  must match `DTSTART`'s value type). All-day series (`start:{date}`) emit `UNTIL=YYYYMMDD` (a
  DATE); timed series emit `UNTIL=YYYYMMDDTHHMMSSZ` (UTC, pinned to the inclusive end of the
  local until-day). Passing a datetime `UNTIL` to an all-day series is a Google **400**, which
  is why `recurrenceLineFor`/`createFromDraft` pass `allDay` through to `toRRule`.
- **On the confirm step**, `applyDraftUpdate` reads `review.recurrence` **DIRECTLY** (not
  `?? prev`): for recurrence `null` is the **clear** value ("actually just once" / "na verdade só
  uma vez"), so `?? prev` would make clearing impossible. `buildCreateReviewSystem` therefore
  instructs the model to **echo** the current recurrence on every non-clearing modify and return
  `null` **only** to clear — a `null` it did not mean would drop the whole series.

> ⚠️ **Known limitation — create-only.** Recurrence is written **only on create**. The existing
> **edit and delete** flows operate on a **single event id** (`matchEventTargets` +
> `events.update` / `events.delete`), so editing or cancelling a recurring event through the
> current flow affects **one instance** (or behaves unpredictably), **not the series**. Series
> edit/delete — "move every Monday standup to Tuesday", "cancel the whole series" — is a **future
> card**, deliberately out of scope here.

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
2. **`resumeDelete`:** `classifyConfirmation` (the shared one from
   `1. Orchestrator/lib/confirm.js`; one Claude call) → `confirm | decline | unrelated` (default
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
`editDraftFromEvent(ev)` — is `{ title, start_iso, duration_min, all_day, all_day_end_iso,
summary, emails[] }`: the CREATE draft's own all-day shape (see ALL-DAY under CREATE), seeded
off the real event by `allDayFromEvent(ev)`, so `localizeWhen` and `allDayWireDates` serve
both sides. `applyPatchToDraft(draft, patch)` folds a change onto it (overwrite touched
fields; merge attendees: case-insensitive remove then dedup add; then re-run
`normalizeAllDay`, so a move that strands the old range end behind the new start self-heals).

> 🔴 **THE RULE — `new_all_day === false` is honoured ONLY alongside a `new_start_iso`.**
> `EDIT_SCHEMA` **requires** the field, so a model answering an ordinary **rename** can emit
> `false` rather than `null` — and a naive fold would then silently convert the owner's
> all-day event into a **45-minute block**. That is precisely the harm the old guard existed
> to prevent, re-entering through the front door. Turning all-day **off** means **giving the
> event a time** ("na verdade é às 10h") — always. So a bare `false` is **ignored**;
> `new_all_day:true` and `new_all_day_end_iso` are honoured on their own. Enforced in
> `applyPatchToDraft`, **in code, not in prompt hope** — the rename-only tripwire in
> `scripts/calendar-edit-selftest.mjs` (f) is what proves it.

1. **`handleEdit`:** gather `emails` + `start_iso` from `info` and `eidEventId =
   resolveEventId(quoted.calendarLink)` (may be null) — same signals delete uses. Here
   `info.start_iso` is the event's **CURRENT** start (the `interpret` prompt reads it from the
   replied-to invite/summary or the conversation — **not** the new time being requested; that
   change is extracted later by `interpretEdit`), so the calendar search hits the real event.
   Need the link id **or** both a start time and an email, else ask and stop.
   `matchEventTargets` returns the confident, confirmed-only matches; none → "couldn't find
   that event"; else patch the primary (`matches[0]`) — no separate `getEvent`/status recheck,
   the matcher already returns the full event. Then the **first-pass** `interpretEdit` (`buildEditSystem`,
   `EDIT_SCHEMA`, 2048) reads the change against the real event (`eventForLLM`) and returns
   only what changes:
   ```jsonc
   { "new_start_iso": string|null, "new_duration_min": number|null,
     "new_title": string|null, "new_summary": string|null,
     "new_all_day": boolean|null,        // null = not changing. See THE RULE above.
     "new_all_day_end_iso": string|null, // the LAST day COVERED, INCLUSIVE. null = not changing.
     "add_emails": string[], "remove_emails": string[],
     "clarify": string|null }   // a question when the request is ambiguous/underspecified
   ```
   `hasEditChange` counts `new_all_day:true` and a `new_all_day_end_iso` as changes **on
   their own** ("na verdade é o dia todo" says nothing else); a bare `false` is **not** a
   change.
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
   - `confirm` → re-`getEvent` (still `confirmed`?) then **`applyEditDraft(ctx, eventId,
     draft, ev)`** — the fetched resource is **handed to it** — writing
     `summary`/`description`/`attendees` plus either `start:{date}/end:{date}` (all-day, the
     end EXCLUSIVE via `allDayWireDates`) or `start:{dateTime}/end:{dateTime}` (timed, end =
     start + duration). `editDone` renders from the **DRAFT** (`localizeWhen`; `duration:
     null` when all-day), **not** by reading `updated.start.dateTime` back — that is `null`
     for an all-day event, and is where `(sem horário)` came from. Then clear.

     > ⚠️ **The write is `events.update` (`updateEvent`), NOT `events.patch`** — a full
     > **resource replace**. Flipping a timed event to all-day means the old `start.dateTime`
     > must not survive next to the new `start.date`; a half-converted event is the corruption
     > the guard used to refuse the write over. Clearing a nested field via `patch` rests on
     > Google's patch semantics, which **no offline test can prove** — a green suite would mean
     > nothing. A replace makes the half-converted event *structurally impossible*. **Its one
     > cost: what the body does not carry, Google CLEARS** — so the body is `{ ...ev, summary,
     > description, attendees, start, end }`, spreading the freshly-fetched event so
     > `reminders` / `colorId` / `recurrence` / `sequence` survive. `resumeEditConfirm` was
     > **already** re-fetching the event, so this costs **no extra API call**. The `colorId`
     > tripwire in the selftest (a6/f4) is what pins it.
   - `modify` → `hasEditChange` → `applyPatchToDraft` onto the draft + `openEditConfirm`
     (re-show, **keep open**); ambiguous further change (`clarify`, no change) → ask + keep
     open; nothing resolved → silent.
   - `cancel` → clear + `editCancelled` ("I'll leave it as it was").
   - `unrelated` / null (doubt/error) → silent (safe default).

### Task: LIST — `handleList` (read-only, stateless)
The only action with **no session, no confirm, and no write** — the simplest of the four.
`interpret` sets `list_mode` and (for a bounded query) `range_start_iso`/`range_end_iso`;
`handleList` resolves the window, fetches, and replies. Two modes:
- **`list_mode:"next"`** → forward-scan `events.list` (now → now + 14 days, `orderBy:
  "startTime"`, `maxResults:10`), take the first confirmed event → `reply(lang).listNext`
  (or a "nothing coming up" line when empty).
- **`list_mode:"window"`** (default) → `startMs` = `range_start_iso` or **now**; `endMs` =
  `range_end_iso` or **`endOfLocalDay(startMs)`** (a bad/backwards range also falls back to
  end-of-day). `events.list` over `[startMs, endMs]` (`singleEvents:true` expands recurring
  instances, `orderBy:"startTime"`, `maxResults:50`, confirmed-only) → `reply(lang).listEvents`.

`endOfLocalDay(ms)` computes 23:59:59.999 in `CAL_TZ` (America/Sao_Paulo, fixed −03:00 — no
DST) via an `en-CA` `Intl.DateTimeFormat` to get the local Y-M-D, then a −03:00 ISO string.
`toListItem(e)` flattens a Google event to the **locale-neutral** shape the renderers take
(`{ allDay, startIso, dayMs, title, emails, durationMin }`) — all-day events (`e.start.date`,
no `dateTime`) carry `dayMs` and render as "All day"; `emails` is **external attendees only**
(dropping `self` — the owner — and room `resource` entries). The renderers (`listEvents`,
`listNext`, `listError` in `prompt.js`) **group events by day** via `renderDays`: a date
header, then each event as an `eventBlock` — a `time - title` line plus, only if the event has
external attendees, their emails on the next line. Blocks join with a blank line, days join
with a blank line, so there's a break between events in a day and before each new day. Time is
hh:mm (or "All day"); the date lives in the group header, not per line. A 50-item cap appends a
localized "(Showing the first 50.)" note rather than truncating silently; the empty state uses
`sameLocalDay` to word single-day vs range. Errors → `reply(lang).listError` (no crash).

### External APIs
- **Anthropic (Claude), all structured-output calls:** `interpret` (create/delete/edit
  classification + create/delete extraction, 4096) · `inspectMissing` (focused
  missing-field resolver, 2048) · `reviewCreate` (create confirm/modify/cancel + re-draft,
  4096) · `interpretEdit` (first-pass edit extraction, 2048) · `reviewEdit` (edit
  confirm/modify/cancel + re-draft, 2048) · `classifyConfirmation` (delete yes/no, 1024).
  Model = `ctx.model`.
- **Google Calendar (OAuth refresh token):** `events.list` (dedupe on create + match/sweep
  on delete + **read the window on list**), `events.get` (resolve a decoded link id; read current state on edit),
  `events.insert` (create), `events.update` (edit — a full-resource REPLACE, `sendUpdates:"all"`;
  see §Task: EDIT for why it is not `patch`), `events.delete`
  (cancel). `sendUpdates:"all"` sends invite / change / cancellation emails.
- **WhatsApp:** all user-facing text via `ctx.send`.

### Stateful behavior, timeouts, completion
- **LIST is stateless** — no session, no confirm, no write; it replies in one shot and is done.
  The rest below applies only to CREATE / DELETE / EDIT.
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
  `events.update` succeeds (message + link sent; session cleared). **No calendar write until
  the owner confirms**, for all three.
- **Failure modes:** every external call is wrapped; failures send a plain-language reply
  and clear the session where relevant. A model refusal or unparseable reply resolves to
  `null` → a safe no-op (nothing written). `classifyConfirmation` / `reviewCreate` failing
  → treated as `unrelated` (do nothing), the safe default.
