# Task Capture ("Todo Inbox") — Implementation Plan

## Goal

Capture todos from chat and read them back, backed by Google Tasks.

- Add explicitly: `@brain add "buy flight to SP" to my todos`
- Capture from a message: reply with `@brain turn this into a task`
- Read back: `@brain what's on my list?`
- (Phase 2) From a summary's action items: "save these 3 as tasks?"

## Why it fits the architecture

- **Same Google OAuth client** already wired for Calendar Actions
  (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN`) — Google
  Tasks is the same `googleapis` auth, just a different API surface.
- Same "new skill folder" discovery pattern (`manifest` + `run`).
- The read-back + confirmation flows reuse the existing `send()` and session
  mechanics.

## Prerequisite: Google Tasks scope

- The current refresh token is scoped for Calendar. Google Tasks needs
  `https://www.googleapis.com/auth/tasks`. **Re-consent** to add the scope and mint
  a new refresh token (one-time setup, same OAuth app). Document this in the skill
  header so it isn't a silent failure.

## New skill — `2. Skills/5. Tasks/`

Standard skill contract (`manifest` + `run`), auto-discovered at boot.

- `manifest`:
  ```
  { id: "task_action",
    description: "add a task/todo, list the owner's tasks, or complete a task in Google Tasks" }
  ```
- `run(ctx)`:
  1. Call Claude (`prompt.js`) to classify the action + extract data:
     `{ action: "add" | "list" | "complete", title: string|null, due_iso: string|null, task_ref: string|null }`.
  2. Dispatch:
     - **add** → insert a task (title, optional due). Confirm:
       `Added to your list: "<title>"[ · due <localized when>]`.
     - **list** → read the task list, format the open items.
     - **complete** → resolve which task from `task_ref` (LLM match against the open
       list), then open a confirm **session** and mark done on "yes" (reuses the
       Calendar delete confirm/continuation pattern).
  3. Source of the title:
     - explicit quoted text (`@brain turn this into a task`) → use `ctx.quoted.text`.
     - inline (`add "…" to my todos`) → the extracted `title`.

### Google Tasks client (`skill.js`)

- Build a `google.tasks({ version: "v1", auth })` client the same way
  Calendar Actions builds its `calendar` client.
- Helpers: `addTask(env, {title, notes, due})`, `listTasks(env)`,
  `completeTask(env, taskId)`. Default task list = `@default` (or
  `GOOGLE_TASKLIST_ID` env override, mirroring `GOOGLE_CALENDAR_ID`).
- Due handling: Google Tasks stores due as an RFC3339 date; convert `due_iso`
  accordingly (Tasks due is date-granular — note this limitation in the confirm
  message).

### Prompt (`2. Skills/5. Tasks/prompt.js`)

- System: classify into add/list/complete and extract `{title, due_iso, task_ref}`.
  Output JSON only. Use `nowStr` for relative-date conversion (same as Calendar).
- For `complete`, `task_ref` is a free-text description; the skill matches it to an
  open task (a second small LLM step or fuzzy match against the fetched list).

## Read-back formatting (example)

```
[AI Brain]:

Your list (3 open):
1. Buy flight to SP — due Fri
2. Send contract to João
3. Call the accountant
```

## Multi-lingual

- All fixed strings via the i18n catalog keyed by `ctx.lang`
  (`task.added`, `task.listHeader`, `task.empty`, `task.completed`,
  `task.confirmComplete`, `task.failed`). Task **titles** stay verbatim as written.
  Consistent with `multilingual-brain.md`.

## Composability

- **Summarizer (idea 2):** after a summary with action items, hand them here to
  batch-add via a confirmation session. Keep as phase 2.

## Files touched

- **New:** `2. Skills/5. Tasks/skill.js`, `2. Skills/5. Tasks/prompt.js`
- **Edit:** i18n catalog (new task keys)
- **Setup:** re-consent OAuth to add the Google Tasks scope; update the refresh
  token in `.env` (+ note it in `.env.example`)

## Build order

1. OAuth scope + new refresh token (blocking prerequisite).
2. Tasks client helpers (add / list / complete).
3. Skill + prompt: **add** and **list** paths first.
4. **complete** path with the confirm session.
5. i18n strings.
6. (Phase 2) Summarizer → batch add action items.

## Notes / risks

- **Scope gap** is the main gotcha — without re-consent, Tasks calls 401. Fail
  loudly with a clear message.
- Google Tasks due dates are **date-only** (no time). Set expectations in the
  confirmation copy; use Reminders (idea 1) when a precise time nudge is wanted.
