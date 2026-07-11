# Skill: `calendar_action`

> **For humans — quick read.**
>
> Creates and cancels Google Calendar events from WhatsApp.
>
> **It handles two tasks:**
> 1. **Create** an event and email the invite to the attendees.
> 2. **Cancel/delete** an event you replied to — with a "type *yes* to confirm" step.
>    *(Edit/reschedule is planned, not built yet.)*
>
> **How you call it:**
> - Create: just `@brain schedule this` — it reads the recent conversation for who's
>   invited, the time, and their email. You don't have to spell it out (though you can:
>   `@brain schedule a call with ana@example.com tomorrow 3pm`).
> - Cancel: **reply to the invite message** (the one with the calendar link) with
>   `@brain cancel this`, then just type **`yes`** to confirm (no tag needed).
>
> If something's genuinely missing from the chat (no time, no email anywhere), it tells
> you what it still needs.

---

## For AI / maintainers — detailed

Source: `skill.js` (logic) + `prompt.js` (LLM prompts). Contract: `export const manifest`
+ `export async function run(ctx)`; auto-discovered by the orchestrator at boot.

### How it's invoked
- **Fresh command:** the router classifies an `@brain` order as `calendar_action` and
  the orchestrator calls `run(ctx)`.
- **Continuation (stateful):** while a delete-confirmation session is open, the
  orchestrator routes the owner's next messages here too (no router call) — `run(ctx)`
  sees `ctx.session` set and resumes.

### What it receives (`ctx`)
`owner, tag, anthropic, model, order, transcript, nowStr, contact, number, remoteJid,
fromMe, quoted, hasQuotedAudio, catalog, env, evolution, send, sessions, session`.
Most-used here: `order` (the text), `quoted` (`{id,hasAudio,mediaType,text,calendarLink}`),
`anthropic`+`model` (LLM), `env` (Google creds), `send(number,text)` (WhatsApp reply),
`sessions` (`get/set/clear`), `session` (active session on a continuation), `remoteJid`.

### Control flow — `run(ctx)`
1. **Continuation check:** if `session.intent === "delete"` and
   `session.stage === "await_confirmation"` → `resumeDelete(ctx, session)` and stop.
2. Otherwise **`interpret(ctx)`** — one Claude call (`buildSystem`/`buildUserPrompt`,
   `max_tokens: 700`). It extracts from the **conversation**, not just the order:
   `order` + `transcript` + `contact` all go into the prompt, so a terse order like
   "schedule this" works — participants, time, duration and emails are pulled from the
   recent chat. Output is a JSON object:
   ```jsonc
   { "action": "create"|"delete"|"other", "confirm": bool,
     "participants": [{"name","email"|null}], "start_iso": string|null,
     "duration_min": number|null, "missing": string[], "summary": string }
   ```
   Returns `null` if no JSON is found; throws are caught → "I hit an error…" and stop.
3. **Dispatch on `action`:** `delete` → `handleDelete`; `create` → `handleCreate`;
   anything else → "I didn't identify a calendar action."

### Task: CREATE — `handleCreate(ctx, info)`
- Collect `names`/`emails` from `participants`. Build a `missing` set: adds
  `"start_iso"` if no time, `"email"` if no attendee has an email.
- **If anything is missing → ask the owner and return.** *(Not stateful today:
  the owner must re-send with `@brain`. Making this stateful is Phase C on the roadmap.)*
- Else build `title = "<owner> & <names|contact|Guest>"`, `dur = duration_min || 45`,
  `end_iso = start + dur`.
- **`createEvent(env, …)`** → **Google Calendar `events.insert`** with
  `sendUpdates:"all"` (Google emails the invite). On success → confirmation message
  incl. `htmlLink`. On failure → caught, error reply.

### Task: DELETE — `handleDelete(ctx, info)` + `resumeDelete(ctx, session)`
Two steps, split by the session:

**Step 1 — `handleDelete` (the `@brain cancel` request):**
1. Need `quoted.calendarLink` (the invite link on the replied-to message). If absent →
   ask the owner to reply to the message with the link, and stop.
2. **`resolveEventId(link)`** — decode the link's `eid` (`base64url("<eventId> <calId>")`)
   → `eventId`. If undecodable → error reply, stop.
3. **`getEvent(env, eventId)`** → **Google Calendar `events.get`** (validates it exists;
   fetches title/time). On error → "couldn't find that event", stop.
4. **Becomes stateful:** `sessions.set(remoteJid, { skill:"calendar_action",
   intent:"delete", stage:"await_confirmation", awaitFrom:"owner",
   data:{eventId,title,when} }, 600)` — **10-minute TTL**. Sends the confirm question and
   returns (waits). No calendar write yet.

**Step 2 — `resumeDelete` (runs for every owner message while the session is open):**
1. **`classifyConfirmation(ctx, {action})`** — one Claude call (`buildConfirmSystem`/
   `buildConfirmUser`, `max_tokens: 50`) that reads the pending action + recent
   conversation and returns `confirm | decline | unrelated`. Defaults to `unrelated`
   on any doubt or error.
2. `unrelated` → **return silently** (session kept; normal chatter is ignored — no nag,
   no accidental delete). `decline` → clear session + "I'll keep it". `confirm` →
   **`deleteEvent`** → **Google Calendar `events.delete`** with `sendUpdates:"all"`
   (attendees get the cancellation) → clear session + "Cancelled…". Errors → clear
   session + error reply.

### External APIs
- **Anthropic (Claude):** `interpret` (create/delete extraction, 700 tok) and
  `classifyConfirmation` (yes/no judgment, 50 tok). Model = `ctx.model`.
- **Google Calendar (OAuth refresh token):** `events.insert` (create),
  `events.get` (validate before delete), `events.delete` (cancel). `sendUpdates:"all"`
  makes Google send invite/cancellation emails.
- **WhatsApp:** all user-facing text via `ctx.send`.

### Stateful behavior, timeouts, completion
- **Stateful only in DELETE**, and only after Step 1: a session with `awaitFrom:"owner"`
  and a **600 s (10 min)** TTL. CREATE is currently stateless.
- **Timeout:** if the owner doesn't confirm within 10 min the session expires; a later
  bare `yes` is then ignored (the owner must start over with `@brain`). Google/Claude
  calls are single requests (no internal polling here).
- **Completes when:** CREATE → `events.insert` succeeds and the confirmation (with link)
  is sent. DELETE → `events.delete` succeeds and the session is cleared. A missing-info
  create "completes" by asking and returning (no event yet).
- **Failure modes:** every external call is wrapped; failures send a plain-language
  reply and (for delete) clear the session. `classifyConfirmation` failing → `unrelated`
  (do nothing), which is the safe default.
