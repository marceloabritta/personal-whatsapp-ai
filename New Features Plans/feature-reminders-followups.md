# Reminders & Follow-ups — Implementation Plan

## Goal

Let the owner ask the brain to ping them (or nudge them to act) at a future time.

- **Reminder:** `@brain remind me to send the contract to João on Friday 3pm`
- **Follow-up:** reply to a message with `@brain follow up if he doesn't answer by tomorrow`

When the time arrives the brain sends a `[AI Brain]:` message into the originating
chat. For follow-ups, it first checks whether the contact already replied and stays
silent if they did.

## Why it fits the architecture

- Time parsing (relative date → ISO with `-03:00`, `America/Sao_Paulo`) already
  exists in **Calendar Actions** — reuse the same prompt technique.
- Delivery reuses the existing `send()` / `evolution.sendText` path into `remoteJid`.
- Redis is already running (sessions) — the new part is a **durable job store**
  plus a **tick loop**, which is the main lift of this feature.

## The core challenge: firing without an incoming message

The webhook only runs when a message arrives. A reminder must fire on a schedule
with no triggering message. Solution: a background poller inside the long-running
`server.js` process.

### Job store (Redis)

- Redis **sorted set** `brain:jobs` scored by `dueAt` (unix seconds).
- Each member is a job id; job body stored at `brain:job:<id>` (JSON):
  ```
  { id, kind: "reminder" | "followup",
    remoteJid, number,
    text,                 // what to say / what to remind about
    dueAt,                // unix seconds
    createdAt,
    lang,                 // for the multi-lingual reply
    awaitContactReply }   // followups only: silence if the contact replied
  ```
- If `REDIS_URL` is empty (in-memory mode, see sessions.js), fall back to an
  in-memory array + `setTimeout` so local dev still works.

### Tick loop

- In `server.js` boot, start `setInterval(tick, 30_000)` (30s granularity is plenty
  for human reminders).
- `tick()`: `ZRANGEBYSCORE brain:jobs -inf <now>` → for each due job:
  1. Load the job body.
  2. **Follow-up guard:** if `kind === "followup"`, fetch recent history for
     `remoteJid` (via `evolution.fetchHistory` + the in-memory buffer) and check for
     a message from the contact (`!fromMe`) newer than `createdAt`. If found, drop
     the job silently.
  3. Otherwise `send(number, <localized reminder text>)`.
  4. Remove the job (`ZREM` + `DEL`).
- Wrap each job in try/catch so one failure doesn't stall the loop.

## New skill — `2. Skills/3. Reminders/`

Standard skill contract (`manifest` + `run`), discovered by the orchestrator at boot.

- `manifest`:
  ```
  { id: "reminder",
    description: "set a reminder or a follow-up nudge for a future time, and notify the owner in this chat when it is due" }
  ```
- `run(ctx)`:
  1. Call Claude (new `prompt.js`) to extract:
     `{ kind, due_iso, text, awaitContactReply }` from `order` + `transcript` +
     `nowStr` + `quoted`.
  2. Missing time → ask for it (reuse the "still missing" pattern from Calendar).
  3. Persist the job to the Redis job store with `remoteJid`, `number`, and
     `ctx.lang`.
  4. Confirm: `Reminder set for <localized when>: "<text>"`. (For a follow-up:
     mention it only fires if the contact hasn't replied.)

### Prompt (`2. Skills/3. Reminders/prompt.js`)

- System: "extract a reminder/follow-up from the order. Output JSON:
  `{kind:"reminder"|"followup", due_iso:string|null, text:string, awaitContactReply:boolean}`."
- Convert relative dates using the provided `nowStr`, same rules as Calendar.

## Management commands (nice-to-have, phase 2)

- `@brain list my reminders` → read `brain:jobs` for this `remoteJid` and list them.
- `@brain cancel that reminder` → open a confirm **session** (reuse the
  session/continuation flow) and `ZREM` on "yes".

## Multi-lingual

- Store `ctx.lang` on the job so the fired message is localized even though no live
  message triggers it. User-facing strings live in this skill's `prompt.js` as a per-skill
  `{ en, pt }` map (`reminder.set`, `reminder.missingTime`, `reminder.due`, …) selected by
  `ctx.lang`. See the localization convention in `../ARCHITECTURE.md`.

## Files touched

- **New:** `2. Skills/3. Reminders/skill.js`, `2. Skills/3. Reminders/prompt.js`
- **New:** `1. Orchestrator/lib/jobs.js` (Redis job store + tick loop; mirrors the
  shape of `lib/sessions.js`)
- **Edit:** `1. Orchestrator/server.js` (start the tick loop at boot; pass the job
  store into `ctx`)
- **Edit:** i18n catalog (new reminder keys)

## Build order

1. `lib/jobs.js` (store + tick loop) with an in-memory fallback.
2. Wire the tick loop into `server.js` boot; verify a hand-inserted job fires.
3. `Reminders` skill + prompt (create path).
4. Follow-up guard (contact-replied check).
5. List / cancel management commands.

## Notes / risks

- **Process restarts:** jobs live in Redis, so a restart is fine; the tick loop just
  resumes. In-memory fallback loses jobs on restart (acceptable for dev only).
- **Duplicate fires:** remove the job before/at send; guard with a short per-job lock
  if two ticks ever overlap (they won't at 30s, single process).
- **Time zone:** keep everything in `America/Sao_Paulo` like Calendar.
