# Calendar — Recurring Events (Implementation Plan)

Split out of the now-retired `calendar-actions.md` backlog (2026-07-11). The `calendar_action`
skill (create / edit / delete) is **shipped and live**; this plan adds **recurrence** to create
("every Monday 10am", "weekly standup", "every 2 weeks until August").

Skill source: [`skill.js`](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js) +
[`prompt.js`](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js). Behaviour reference:
[`SKILL.md`](../secretary/2.%20Skills/1.%20Calendar%20Actions/SKILL.md).

## Goal

Let the owner schedule a **repeating** event. Google Calendar carries recurrence as an
[RRULE](https://developers.google.com/calendar/api/concepts/events-calendars#recurring_events)
string on `events.insert`'s `recurrence` array; the whole job is: detect the pattern, confirm it
in plain language, and pass the RRULE through.

Scope: **create only** for the MVP. Editing/deleting a recurring series (this-instance vs.
whole-series) is a larger surface — captured under "Edit/Delete implications" as follow-up.

## Design decision: structured recurrence → compiled RRULE (don't let the LLM emit raw RRULE)

The `interpret` LLM returns a **structured** recurrence object; code compiles it to the RRULE
string. Rationale: an RRULE has strict grammar (`FREQ`, `INTERVAL`, `BYDAY=MO,WE`, `COUNT` vs.
`UNTIL` with a UTC `Z` timestamp) that a model gets subtly wrong (lowercase days, local vs. UTC
`UNTIL`, `COUNT`+`UNTIL` together). A small validated object + a deterministic `toRRule()` keeps
the model doing what it's good at (pattern *recognition*) and code doing what it's good at
(exact string formatting) — the same split the skill already uses for the resolver contract.

## Behaviour (what the owner sees)

Confirm-first, exactly as today, with the recurrence stated in human terms:

> Confirm this event:
> - Weekly standup
> - team@example.com
> - **Every Monday, 10:00 AM** (starts Jul 13, 2026) (30 min)
>
> Reply "yes" to confirm and I'll send the invites, or tell me what to change and I'll adjust.

- `yes` → `events.insert` with `recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=MO"]`; Google emails the
  series invite.
- Modify at confirm ("make it every other Monday", "only until August", "actually just once")
  → re-drafts through the existing `resumeCreate` → `applyDraftUpdate` path; "just once" clears
  recurrence back to a single event.
- Human phrasing per language ("Every Monday" / "Toda segunda-feira"; "every 2 weeks" / "a cada
  2 semanas"; "until Aug 30" / "até 30 de ago").

## Implementation

### 1. Structured recurrence in `CAL_SCHEMA` (prompt.js:25)
Add one nullable object field (null = one-off, today's behaviour):
```jsonc
recurrence: {
  anyOf: [
    { type: "null" },
    {
      type: "object",
      additionalProperties: false,
      required: ["freq", "interval", "byday", "count", "until_iso"],
      properties: {
        freq:     { type: "string", enum: ["DAILY","WEEKLY","MONTHLY","YEARLY"] },
        interval: { type: ["number","null"] },          // default 1
        byday:    { type: "array", items: { enum: ["MO","TU","WE","TH","FR","SA","SU"] } },
        count:    { type: ["number","null"] },           // N occurrences, OR…
        until_iso:{ type: ["string","null"] }            // …an end date. Never both.
      }
    }
  ]
}
```
Add `recurrence` to `CAL_SCHEMA.required`. The same field must be added to `REVIEW_SCHEMA`
([prompt.js:48](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js#L48)) so the confirm-step
re-draft (`reviewCreate`) can add/change/clear it.

### 2. Prompt copy (`buildSystem`, prompt.js:130; `buildCreateReviewSystem`, prompt.js:202)
Describe `recurrence`: infer it from phrases like "every Monday", "weekly", "each morning",
"every 2 weeks", "until August", "5 times". Rules to spell out:
- one-off → `recurrence: null` (default; do not invent repetition).
- `freq` from the cadence word; `byday` for weekly-on-specific-days; `interval` for "every N".
- **`count` XOR `until_iso`** — set at most one; never both. "until August" → `until_iso`
  (end of the stated day, -03:00); "5 times" → `count`.
- `start_iso` remains the **first occurrence** (the existing field), not the recurrence rule.

### 3. Compile + validate: `toRRule(recurrence)` (skill.js, new helper)
```js
// Structured recurrence -> a single RRULE string, or null if absent/invalid.
function toRRule(rec) {
  if (!rec || !rec.freq) return null;
  const parts = [`FREQ=${rec.freq}`];
  if (rec.interval > 1) parts.push(`INTERVAL=${rec.interval}`);
  if (Array.isArray(rec.byday) && rec.byday.length) parts.push(`BYDAY=${rec.byday.join(",")}`);
  if (rec.count > 0) parts.push(`COUNT=${rec.count}`);              // COUNT xor UNTIL
  else if (rec.until_iso) parts.push(`UNTIL=${toRRuleUntil(rec.until_iso)}`); // UTC basic format, e.g. 20260830T235959Z
  return `RRULE:${parts.join(";")}`;
}
```
`toRRuleUntil(iso)` converts to the RRULE UTC form `YYYYMMDDTHHMMSSZ` (RRULE `UNTIL` must be
UTC when `DTSTART` has a timezone — which ours does, `CAL_TZ`). Guard against `COUNT`+`UNTIL`
both present (prefer `COUNT`, drop `until`).

### 4. Thread it through the draft + insert
- **Draft shape** — carry `recurrence` on the draft object built by `draftFromInfo`
  ([skill.js:426](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L426)) and updated by
  `applyDraftUpdate` ([skill.js:541](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L541)).
  It plays no part in `missingOf`/`isComplete` (recurrence is never *required*).
- **`createEvent`** ([skill.js:130](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L130))
  — accept `recurrence` and, when `toRRule(...)` is non-null, set
  `requestBody.recurrence = [rrule]`.
- **Idempotency** — `findConfirmedDuplicates` matches on title + exact start
  ([skill.js:172](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L172)); a recurring
  master's `start.dateTime` is its first instance, so the existing dedupe still works for a
  repeated "schedule this". No change needed, but verify with a manual double-send.

### 5. Human-readable recurrence line (prompt.js `REPLY.en`/`REPLY.pt`)
Add `describeRecurrence(rec, lang)` → "Every Monday", "Every 2 weeks on Mon, Wed", "Daily until
Aug 30, 2026", "Weekly, 5 times", etc. `createConfirm` and `createDone`
([prompt.js:392](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js#L392),
[:399](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js#L399)) prepend/insert this line
only when `recurrence` is set — one-off output stays byte-identical to today.

## Edit / Delete implications (follow-up, not MVP)
Google models a series as a recurring **master** plus per-date **instances**; changing or
cancelling one is "this event / this-and-following / all events". The current
`matchEventTargets` + `events.patch`/`events.delete` operate on a single event id and would hit
whichever id was resolved (often an instance from the invite link). For the MVP:
- **Do not** claim series-edit support in `SKILL.md`; recurrence is create-only.
- Editing/deleting a recurring event via the existing flow will affect a single instance (or
  error) — acceptable for v1. A later plan adds an explicit "this one vs. the whole series"
  question to `handleEdit`/`handleDelete`. Note this limitation in `SKILL.md` so it's honest.

## Edge cases
- **`COUNT` and `UNTIL` both inferred** — code keeps `COUNT`, drops `until` (documented in the
  prompt too).
- **`UNTIL` timezone** — must be UTC `Z`; `toRRuleUntil` handles the conversion. A local-time
  `UNTIL` is the classic RRULE bug — cover it in tests.
- **`byday` on a non-weekly freq** — Google accepts `BYDAY` with `MONTHLY` ("first Monday" needs
  an ordinal, e.g. `BYDAY=1MO`); MVP supports weekly `BYDAY` only. For monthly, fall back to
  `FREQ=MONTHLY` on the start date's day-of-month and skip `byday` unless an ordinal is clearly
  meant. Keep monthly-by-weekday out of v1 or handle explicitly.
- **DST** — `DTSTART` carries `CAL_TZ`, so Google keeps the local wall-clock time across DST;
  no action needed, but sanity-check one across a Brazil DST boundary if any remain.

## Testing
1. `@secretary weekly standup every Monday 10am with team@example.com` → confirm shows
   "Every Monday, 10:00 AM"; `yes` → series created (verify RRULE in the Google event).
2. "every 2 weeks" → `INTERVAL=2`; "until Aug 30" → `UNTIL=20260830T…Z`; "5 times" → `COUNT=5`.
3. Modify at confirm: "actually just once" → recurrence cleared, single event created.
4. pt: "toda segunda às 10h" → correct RRULE + Portuguese recurrence line.
5. Double-send the same recurring create → no duplicate (idempotency holds).
6. A recurring create then `@secretary what's on my calendar` (once read/query ships) shows the
   right instances (`singleEvents:true` expansion).

## Deploy & done-when
- Deploy = git pull + restart on the droplet; get an explicit go-ahead for the production write.
- **Done when:** the four core patterns (weekly-by-day, interval, until, count) produce correct
  RRULEs and create real recurring series, the confirm/done lines read naturally in en + pt,
  "just once" cleanly clears recurrence, and `SKILL.md` documents both the new capability **and**
  the create-only limitation. Then archive this plan to `Shipped Features/`.
