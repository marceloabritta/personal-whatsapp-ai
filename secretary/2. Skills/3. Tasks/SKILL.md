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
> so a task you assign to another person (`@secretary remind João to send the contract by
> Friday`) is created as a **5-minute Google Calendar invite** (you + them, at 15:00 on
> the due date) — that's the only way they get notified by email. That path is
> **confirm-first** and runs through the Calendar skill (it even chases a missing email).
>
> **How you call it:**
> - Add for you: `@secretary add "buy flight to SP" to my todos` (or reply to any message
>   with `@secretary turn this into a task`).
> - Add for someone: `@secretary remind Ana to send the deck by Friday, ana@example.com`.
> - List: `@secretary what's on my list?`
> - Complete: `@secretary mark the flight one done`, then `yes`.
>
> **Heads-up:** Google Tasks due dates are **date-only** (no time). And this skill needs
> the Google **Tasks** OAuth scope — see *Setup* at the bottom.

## What you'll see (the full conversation)

Every secretary message is prefixed with the language-aware header — `[Marcelo's AI Secretary]:`
in English, `[Secretaria IA do Marcelo]:` in Portuguese (from `headerFor(lang)`) — and a blank line. Replies come back
in the language you wrote in (English and Portuguese are hand-tuned; other languages are
auto-translated).

### Add a to-do for yourself (immediate, then a short window to change it)

1. You: `@secretary add "buy flight to SP" by friday`.
2. Secretary:
   ```
   Added to your list:
   17/jul - buy flight to SP

   Tell me if you need something to change, if not we are good.
   ```
3. You (optional, no tag needed): `make it a return flight`.
4. Secretary:
   ```
   Updated your list:
   17/jul - buy return flight to SP

   Tell me if you need something to change, if not we are good.
   ```
5. You: `done` → (the window closes silently). Or say nothing — it closes on its own.

> Changed your mind? While the window is open, `actually cancel that` removes the task:
> `Removed "buy return flight to SP" from your list.`
>
> Dates render as **dd/mmm**, localized (`17/jul`; in PT-BR a May date reads `03/mai`,
> in English `03/may`). Tasks without a due date show just the title.

### Capture a to-do from a message

Reply to any message with `@secretary turn this into a task` — the quoted message's text
becomes the task.

### List your open todos

1. You: `@secretary what's on my list?`
2. Secretary:
   ```
   Here are your open tasks:
   17/jul - buy flight to SP
   send contract to João
   03/may - call the accountant
   ```

### Complete a to-do (confirm-first)

1. You: `@secretary mark the flight one done`.
2. Secretary: `Mark this done?\n- Buy flight to SP\n\nReply "yes" to confirm.`
3. You: `yes` → `Done — checked off "Buy flight to SP".`
   *(Anything that isn't a clear yes/no is treated as normal chatter and ignored.)*

### A to-do for someone else (becomes a calendar invite)

1. You: `@secretary remind Ana to send the deck by Friday, ana@example.com`.
2. Secretary (via the Calendar skill): `Confirm this event:\n- remind Ana to send the deck…`
   — you type `yes`, Ana gets the invite email. If you didn't give her email, it asks
   for it and waits.

### Em português (o idioma segue a conversa)

1. Você: `@secretary adiciona "comprar passagem pra SP" até sexta`.
2. Secretary: `Adicionei à sua lista: "comprar passagem pra SP" · vence 17 de jul. Me diga se
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
*(Done in production 2026-07-11: token re-minted with both scopes.)*

### Known limitations (next iteration)
This first version is **single-item** and can't edit an existing task:
- **No batch create** — "add A, B and C" captures one task, not three.
- **No batch complete** — "mark A and B done" resolves only one ref (the extractor
  collapses the list); a multi-task request gets lost.
- **No edit of a stored task** — you can only change a task inside its brief post-add
  amend window; there's no "change the flight task's due to Monday" for an older task.

These are scoped in `New Features Plans/task-improvements.md`.
