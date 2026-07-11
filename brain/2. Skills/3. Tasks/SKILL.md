# Skill: `task_action`

> **For humans — quick read.**
>
> Your to-do inbox, from WhatsApp. Capture todos, hear them back, check them off.
>
> **It handles three things:**
> 1. **Add** a to-do — for **yourself** it goes to your private **Google Tasks** list
>    right away; you can then correct or cancel it for a few minutes without re-tagging.
> 2. **List** your open todos.
> 3. **Complete** a to-do — **confirm-first**: it asks, you type **`yes`**.
>
> **A to-do for someone else is different.** Google Tasks is private and emails no one,
> so a task you assign to another person (`@brain remind João to send the contract by
> Friday`) is created as a **5-minute Google Calendar invite** (you + them, at 15:00 on
> the due date) — that's the only way they get notified by email. That path is
> **confirm-first** and runs through the Calendar skill (it even chases a missing email).
>
> **How you call it:**
> - Add for you: `@brain add "buy flight to SP" to my todos` (or reply to any message
>   with `@brain turn this into a task`).
> - Add for someone: `@brain remind Ana to send the deck by Friday, ana@example.com`.
> - List: `@brain what's on my list?`
> - Complete: `@brain mark the flight one done`, then `yes`.
>
> **Heads-up:** Google Tasks due dates are **date-only** (no time). And this skill needs
> the Google **Tasks** OAuth scope — see *Setup* at the bottom.

## What you'll see (the full conversation)

Every brain message is prefixed with `[AI Brain]:` and a blank line. Replies come back
in the language you wrote in (English and Portuguese are hand-tuned; other languages are
auto-translated).

### Add a to-do for yourself (immediate, then a short window to change it)

1. You: `@brain add "buy flight to SP" by friday`.
2. Brain: `Added to your list: "buy flight to SP" · due Jul 17. Tell me to change
   anything, or say "done".`
3. You (optional, no tag needed): `make it a return flight`.
4. Brain: `Updated: "buy return flight to SP" · due Jul 17.`
5. You: `done` → (the window closes silently). Or say nothing — it closes on its own.

> Changed your mind? While the window is open, `actually cancel that` removes the task:
> `Removed "buy return flight to SP" from your list.`

### Capture a to-do from a message

Reply to any message with `@brain turn this into a task` — the quoted message's text
becomes the task.

### List your open todos

1. You: `@brain what's on my list?`
2. Brain:
   ```
   Your list (3 open):
   1. Buy flight to SP — due Jul 17
   2. Send contract to João
   3. Call the accountant
   ```

### Complete a to-do (confirm-first)

1. You: `@brain mark the flight one done`.
2. Brain: `Mark this done?\n- Buy flight to SP\n\nReply "yes" to confirm.`
3. You: `yes` → `Done — checked off "Buy flight to SP".`
   *(Anything that isn't a clear yes/no is treated as normal chatter and ignored.)*

### A to-do for someone else (becomes a calendar invite)

1. You: `@brain remind Ana to send the deck by Friday, ana@example.com`.
2. Brain (via the Calendar skill): `Confirm this event:\n- remind Ana to send the deck…`
   — you type `yes`, Ana gets the invite email. If you didn't give her email, it asks
   for it and waits.

### Em português (o idioma segue a conversa)

1. Você: `@brain adiciona "comprar passagem pra SP" até sexta`.
2. Brain: `Adicionei à sua lista: "comprar passagem pra SP" · vence 17 de jul. Me diga se
   quer mudar algo, ou diga "pronto".`

## For AI / maintainers — detailed

Files: `skill.js` (logic + Google Tasks client), `prompt.js` (interpret/resolve/review
prompts, JSON Schemas, and the localized `reply(lang)` string map + `localizeDueDate`).

### Contract & flow
- `manifest = { id: "task_action", description }`, `run(ctx)` — discovered at boot.
- **Dispatch:** a continuation first (`ctx.session`), else one structured `interpret`
  call (`TASK_SCHEMA`) → `{ action, title, due_iso, task_ref, assignee }`.
  - `add` + no third-party assignee → **`handleAddSelf`**: `tasks.insert` immediately,
    then open an **amend session** (`intent:"add"`, `stage:"await_amend"`, TTL 600).
    Each later owner message runs `reviewAdd` → `amend` (`tasks.patch`) / `delete`
    (`tasks.delete`) / `keep` (close) / `unrelated` (ignore).
  - `add` + a third-party assignee → **`handleAddOther`**: `ctx.callSkill(
    "calendar_action", "startCreate", …)` with a 5-min event at 15:00 on the due date.
    The session + confirm/email-chase lifecycle is **owned by `calendar_action`**.
    Guarded by `ctx.hasSkill(...)` → `reply().calendarUnavailable()` if absent.
  - `list` → `tasks.list({showCompleted:false})`, formatted by `reply().formatList`.
  - `complete` → `resolveTaskRef` (LLM match to the open list) → confirm session
    (`intent:"complete"`, `stage:"await_confirmation"`) → `resumeComplete` uses
    `classifyConfirmation` and `tasks.patch({status:"completed"})` on a `yes`.
- **Localization:** every reply comes from `reply(ctx.lang)` (en + pt); dates via
  `localizeDueDate`; sessions persist `lang` so continuations answer in-language. Titles
  are verbatim. Follows the ARCHITECTURE "Localization convention".

### Google Tasks specifics
- Client: `google.tasks({version:"v1", auth})` (OAuth2 + refresh token), list
  `GOOGLE_TASKLIST_ID || "@default"`.
- **Due is date-only**, stored at UTC midnight. `toTasksDue` normalizes a −03:00 ISO to
  the São Paulo calendar date pinned to UTC midnight; `localizeDueDate` renders in UTC so
  the shown date matches what was stored.

### Setup — OAuth scope (blocking)
Google Tasks needs `https://www.googleapis.com/auth/tasks`. The refresh token was minted
for Calendar only, so **re-consent with BOTH scopes** (calendar + tasks) and update
`GOOGLE_REFRESH_TOKEN` — otherwise every Tasks call returns 401 and the skill replies
with `reply().failed()`. Keep the consent screen **"In production"** (a Testing token
expires in ~7 days). Optional: `GOOGLE_TASKLIST_ID` to target a non-default list.
