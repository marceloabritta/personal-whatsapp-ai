# Skill: `task_action`

> **For humans — quick read.**
>
> Your to-do inbox, from WhatsApp. Capture todos (one or many), hear them back, check
> them off, and change ones already on your list — talking to it like a secretary.
>
> **It handles:**
> 1. **Add** to-dos — one *or several at once* ("add A, B and C"). For **yourself** they
>    go to your private **Google Tasks** list right away.
> 2. **List** your open todos.
> 3. **Complete** to-dos — one or several ("I bought the pizza and got my flights" checks
>    off both) — **confirm-first**: it asks, you type **`yes`**.
> 4. **Edit / delete** a task already on your list ("change the flight task's due to
>    Monday", "rename the contract task to…", "delete the pizza one") — also confirm-first.
>
> **It stays engaged — no re-tagging.** Once a task exchange is underway, follow-ups need
> **no `@secretary`** for about 10 minutes: correct what you just added, add more, or check
> another off, just by talking. Off-topic chatter is ignored; say "that's all" (or just
> wait) to close it.
>
> **It figures out *which* task you mean.** For a complete/edit/delete it reads the
> conversation and matches your words to a task on the list by meaning ("the flight one",
> "the one due Monday"). If two could fit, it asks which — by name — rather than guessing.
>
> **A to-do for someone else is different.** Google Tasks is private and emails no one,
> so a task you assign to another person (`@secretary remind João to send the contract by
> Friday`) is created as a **5-minute Google Calendar invite** (you + them, at 15:00 on
> the due date) — that's the only way they get notified by email. That path is
> **confirm-first** and runs through the Calendar skill (it even chases a missing email).
> *In one message, only the first such reminder is set up* — send additional ones separately.
>
> **How you call it:**
> - Add for you: `@secretary add "buy flight to SP" to my todos` (or reply to any message
>   with `@secretary turn this into a task`).
> - Add several: `@secretary add buy milk, book the dentist and renew my passport`.
> - Add for someone: `@secretary remind Ana to send the deck by Friday, ana@example.com`.
> - List: `@secretary what's on my list?`
> - Complete: `@secretary mark the flight one done`, then `yes` (or just `@secretary I got
>   my flights and paid the rent`).
> - Edit: `@secretary change the contract task's due to Monday`, then `yes`.
>
> **Heads-up:** Google Tasks due dates are **date-only** (no time). And this skill needs
> the Google **Tasks** OAuth scope — see *Setup* at the bottom.

## What you'll see (the full conversation)

Every secretary message is prefixed with the language-aware header — `[Marcelo's AI Secretary]:`
in English, `[Secretaria IA do Marcelo]:` in Portuguese (from `headerFor(lang)`) — and a blank line. Replies come back
in the language you wrote in (English and Portuguese are hand-tuned; other languages are
auto-translated).

### Add one or several to-dos (immediate, then a window to change them — no re-tag)

1. You: `@secretary add "buy flight to SP" by friday, and book the dentist`.
2. Secretary:
   ```
   Added to your list:
   17/jul - buy flight to SP
   book the dentist

   Tell me if you need to change anything, otherwise we're good.
   ```
3. You (optional, no tag needed): `make the flight a return flight`.
4. Secretary:
   ```
   Updated your list:
   17/jul - buy return flight to SP

   Anything else to change?
   ```
5. You: `that's all` → (the window closes silently). Or say nothing — it closes on its own
   (~10 min). Off-topic messages in between are ignored.

> Changed your mind? While the window is open, `actually cancel the flight one` removes just
> that task: `Removed "buy return flight to SP" from your list.`
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

### Complete one or several (confirm-first, one confirmation)

1. You: `@secretary I bought the pizza and got my flights`.
2. Secretary:
   ```
   Mark these done?
   - buy the pizza
   - buy flight to SP

   Reply "yes" to confirm.
   ```
3. You: `yes` →
   ```
   Done:
   - buy the pizza — done
   - buy flight to SP — done
   ```
   *(Anything that isn't a clear yes/no is treated as normal chatter and ignored — unless
   it's clearly a new task, which is captured while the confirmation stays pending.)*

> **Partial match, never silent.** If one of several couldn't be matched, it confirms what
> it found and names what it didn't: `Mark these done?\n- buy the pizza\n(couldn't find:
> the report thing)\n\nReply "yes" to confirm.`

### Edit or delete a task already on your list (confirm-first)

1. You: `@secretary change the contract task's due to Monday`.
2. Secretary: `Make this change?\n- "send contract to João" — due → 20/jul\n\nReply "yes" to confirm.`
3. You: `yes` → `Done:\n- "send contract to João" (due 20/jul)`.

> **Two flights on your list?** It won't guess — `Which one for "the flight one"?` followed
> by the candidates, so you can pick by name.

### A to-do for someone else (becomes a calendar invite)

1. You: `@secretary remind Ana to send the deck by Friday, ana@example.com`.
2. Secretary (via the Calendar skill): `Confirm this event:\n- remind Ana to send the deck…`
   — you type `yes`, Ana gets the invite email. If you didn't give her email, it asks
   for it and waits.

> Batching more than one reminder-for-someone in a single message? Only the **first** is set
> up; the secretary asks you to send the others separately (each needs its own confirm flow).

### Em português (o idioma segue a conversa)

1. Você: `@secretary adiciona "comprar passagem pra SP" até sexta e marca o dentista`.
2. Secretary:
   ```
   Adicionei à sua lista:
   17/jul - comprar passagem pra SP
   marcar o dentista

   Me diga se precisa mudar algo, senão está tudo certo.
   ```

## For AI / maintainers — detailed

Files: `skill.js` (logic + Google Tasks client), `prompt.js` (planner + confirm prompts,
JSON Schemas, and the localized `reply(lang)` string map + `localizeDueDate`).

### Contract & flow — one resolver, then routing
- `manifest = { id: "task_action", description }`, `run(ctx)` — discovered at boot. Also
  exports `capabilities = { list }` (an internal open-list read for other skills).
- **One list-aware planner.** `planTaskOps(ctx, open)` (schema `PLAN_SCHEMA`) reads the
  conversation AND the numbered open list, returns `{ list_requested, owner_done, ops[] }`.
  Each op is one distinct task: `{ kind: create|complete|edit|delete, target_index,
  candidate_indices, ref_text, title, due_iso, assignee }`. This *replaces* the old
  single-item `interpret` + `resolveTaskRef` — enumeration and list-aware matching happen
  in the same call, which is the fix for the "compound ref got lost" bug.
- **`dispatchPlan`** partitions the ops and acts:
  - `create` → **`handleCreates`**: self items `tasks.insert` immediately (batch); the
    FIRST third-party item is delegated to `calendar_action.startCreate` (5-min invite at
    15:00), guarded by `ctx.hasSkill(...)` → `reply().calendarUnavailable()` if absent —
    additional third-party items are capped (`reply().thirdPartyCapped`). After a self
    create, the stateful window (below) is armed with the batch as `recent`.
  - `complete` / `edit` / `delete` of a **stored** task → collected into one
    **confirm session** (`stage:"await_confirmation"`, `data.mutations[]` + `data.missed[]`,
    TTL 600). `resumeConfirm` runs `classifyConfirmation`; on `confirm` it applies each
    (`tasks.patch status:completed` / `tasks.patch` / `tasks.delete`), reporting per-item
    ok/fail. Unmatched refs are surfaced (`data.missed`), never dropped.
  - An `edit`/`delete` of a task in the **current window** (`recent`) is applied
    **frictionlessly** (no confirm) — this is the old post-add amend, now expressed as ops.
  - `list_requested` → formatted by `reply().formatList`; ambiguous refs →
    `reply().disambiguate` (by name) or `reply().notFound`.
- **Stateful window (no re-tag).** After any interaction, `armEngaged` opens a session
  (`stage:"engaged"`, `awaitFrom:"owner"`, TTL 600, `data.recent[]`). The orchestrator's
  generic continuation (`server.js`: dispatches by `skill`+`awaitFrom`, ignores intent/
  stage) routes the next **untagged** owner message back here; `resumeEngaged` re-runs the
  planner against the fresh list. `owner_done` (or the TTL) closes it; unrelated chatter is
  a silent no-op. A pending confirm short-circuits to yes/no, but a clearly-new self task
  re-plans and is created while the confirmation stays pending.
- **Localization:** every reply comes from `reply(ctx.lang)` (en + pt), a single render
  layer (`makeReply`) driven by a per-language vocabulary so list/confirm/applied views are
  written once; dates via `localizeDueDate`; sessions persist `lang`. Titles are verbatim.
  Follows the ARCHITECTURE "Localization convention".

### Google Tasks specifics
- Client: `google.tasks({ version: "v1", auth: googleAuth(env) })` — the OAuth2 client is
  built once in `1. Orchestrator/lib/google.js` (shared with `calendar_action`); the service
  stays here. List: `GOOGLE_TASKLIST_ID || "@default"`.
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

### Known limitations / non-goals
- **Third-party batch capped at 1/message.** Only the first "remind X…" in a message is set
  up (the rest are flagged); a serial queue is out of scope because the one-session-per-chat
  model means a calendar confirm session and the Tasks window can't coexist, and `callSkill`
  returns on session-open, not on the invite's final confirmation. Send extras separately.
- **Due is date-only** (Google Tasks); an edit can set/keep a due but not clear it in v1.
- **Out of scope:** recurring tasks, subtasks, reordering, cross-list moves.

Design + rationale: `New Features Plans/task-improvements.md`.
