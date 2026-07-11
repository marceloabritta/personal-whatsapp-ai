# Task Capture ("Todo Inbox") — Implementation Plan

> **Freshness note (2026-07-11).** Re-aligned to the current codebase: structured
> outputs (`output_config.format` + JSON Schemas), the stateful session/continuation
> layer (`ctx.sessions` + `ctx.session`, `awaitFrom`), the per-skill `prompt.js`
> string convention (NOT a central i18n catalog — that was rejected in
> `multilingual-brain.md`), the `SKILL.md` doc convention, and skill folder
> numbering (`3. Tasks/`, not `5.`). Multilingual is still a plan, so this ships
> English-only now and slots into the per-skill EN+PT map when that lands.

## Goal

Capture todos from chat and read them back, backed by Google Tasks.

- Add explicitly: `@brain add "buy flight to SP" to my todos`
- Capture from a message: reply with `@brain turn this into a task`
- Read back: `@brain what's on my list?`
- Complete: `@brain mark the flight one done` (confirm-first)
- (Phase 2) From a summary's action items: "save these 3 as tasks?"

## Why it fits the architecture

- **Same Google OAuth client** already wired for Calendar Actions
  (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN`) — Google
  Tasks is the same `googleapis` package (already a dependency, `>=140`) and the
  same OAuth2 auth, just a different API surface (`google.tasks`).
- **Auto-discovery is unchanged.** Drop a folder under `2. Skills/` exporting
  `{ manifest, run }`; the orchestrator loads it at boot and the router starts
  routing to it. No edits to `server.js` or the router.
- **The stateful layer already exists.** `complete` reuses the same confirm-first
  session mechanics the Calendar delete flow uses today (`ctx.sessions` +
  `ctx.session` + `awaitFrom`), so "type yes" continuations need no new plumbing.

## Prerequisite: Google Tasks scope (blocking)

- The current refresh token is scoped for Calendar only
  (`https://www.googleapis.com/auth/calendar`). Google Tasks needs
  `https://www.googleapis.com/auth/tasks`.
- **Re-consent with BOTH scopes at once** (calendar + tasks) and mint a new refresh
  token, so Calendar keeps working. Same OAuth app; use the OAuth Playground → gear
  → own client id/secret → both scopes → authorize → exchange → copy the `1//…`
  token into `/opt/brain/.env` `GOOGLE_REFRESH_TOKEN=` → `--force-recreate`.
- The consent screen must be **"In production"** — in "Testing" the refresh token
  expires in ~7 days and Google returns `invalid_grant` (see PROJECT_LOG §8).
- Document this in `SKILL.md` so a scope gap fails loudly, not silently.

## New skill — `2. Skills/3. Tasks/`

Standard skill contract (`manifest` + `run`), auto-discovered at boot. Files mirror
the Calendar skill: `skill.js` (logic + Google Tasks client), `prompt.js` (prompt
builders + **JSON Schemas** + user-facing strings), `SKILL.md` (human doc).

- `manifest`:
  ```js
  export const manifest = {
    id: "task_action",
    description:
      "add a task/todo, list the owner's open tasks, or complete/check off a task in Google Tasks",
  };
  ```

- `run(ctx)` — mirror Calendar's shape:
  1. **Continuation first.** If `ctx.session?.intent === "complete"` and
     `stage === "await_confirmation"`, resume the pending completion (see below) and
     return. The orchestrator only sets `ctx.session` when this message is a genuine
     continuation of *this* skill's session.
  2. Otherwise **interpret**: one Claude call (`interpret`) with the skill's system
     prompt and a `TASK_SCHEMA` structured output that classifies the action and
     extracts data:
     `{ action: "add" | "list" | "complete" | "other", title: string|null, due_iso: string|null, task_ref: string|null }`.
  3. Dispatch on `info.action`:
     - **add** → `addTask`, then confirm:
       `Added to your list: "<title>"[ · due <localized when>]`.
     - **list** → `listTasks`, format the open items (see below).
     - **complete** → resolve which task `task_ref` means (a focused second LLM pass
       matching against the fetched open list, same spirit as Calendar's
       `inspectMissing`/`matchDeletionTargets`), then **open a confirm session** and
       mark done only on a "yes" continuation.
     - **other / unmatched** → a short "I didn't identify a task action" reply.
  4. Source of the title:
     - reply/quote (`@brain turn this into a task`) → `ctx.quoted.text`.
     - inline (`add "…" to my todos`) → the extracted `title`.

### Structured outputs (match the Calendar skill exactly)

The Calendar skill no longer asks for "JSON only" prose — it passes a JSON Schema via
`output_config: { format: { type: "json_schema", schema } }` and reads the guaranteed
-valid reply. Copy that verbatim:

- Define schemas in `prompt.js` (`TASK_SCHEMA`, and for `complete` a `COMPLETE_SCHEMA`
  and a `CONFIRM_SCHEMA`). Every object needs `additionalProperties: false` and a full
  `required` list; optional fields use a nullable union (`{ type: ["string","null"] }`).
- Copy the `jsonFormat()` + `readReply()` helpers (and the `parseJsonReply` balanced-
  brace fallback) from `2. Skills/1. Calendar Actions/skill.js` — they handle a model
  refusal and a non-structured fallback. Don't reinvent them.
- Reuse Calendar's `CONFIRM_SCHEMA` shape (`{ decision: "confirm"|"decline"|"unrelated" }`)
  and its `buildConfirmSystem`/`classifyConfirmation` pattern for the complete "yes".

### Google Tasks client (in `skill.js`)

- Build `google.tasks({ version: "v1", auth })` the same way `calendarClient(env)`
  builds the calendar client (OAuth2 with the refresh token).
- Helpers: `addTask(env, {title, notes, due})`, `listTasks(env)`,
  `completeTask(env, taskId)`. Default task list = `@default` (or a
  `GOOGLE_TASKLIST_ID` env override, mirroring `GOOGLE_CALENDAR_ID` / `calId()`).
- Due handling: Google Tasks stores `due` as an **RFC3339 date (date-only, no time)**.
  Convert `due_iso` accordingly and set expectations in the confirm copy.

### Complete flow — reuse the confirm-first SESSION pattern

Model it on Calendar's delete (`handleDelete` → `resumeDelete`):

1. Resolve `task_ref` to a specific open task (LLM match against `listTasks`). If no
   confident match, ask which one instead of guessing.
2. `ctx.sessions.set(remoteJid, { skill: "task_action", intent: "complete",
   stage: "await_confirmation", awaitFrom: "owner", data: { taskId, title } }, 600)`
   and send the confirm prompt.
3. The "yes" arrives as a **continuation** (no `@brain`, no reply needed): the
   orchestrator sees the open session, sets `ctx.session`, and calls `run(ctx)`
   again. `run` routes to `resumeComplete`, which uses `classifyConfirmation`
   (`confirm`/`decline`/`unrelated`) — staying **silent on chatter** — and calls
   `completeTask` + `ctx.sessions.clear` on confirm.

### Prompt (`2. Skills/3. Tasks/prompt.js`)

- `buildSystem(owner)` + `buildUserPrompt(owner, { order, transcript, nowStr, contact, quoted })`
  — classify into add/list/complete/other and extract `{title, due_iso, task_ref}`.
  Use `nowStr` for relative-date conversion (same convention as Calendar).
- `TASK_SCHEMA`, `COMPLETE_SCHEMA`, `CONFIRM_SCHEMA` live here (single source of truth
  for reply shape).
- For `complete`, `task_ref` is free-text; the skill matches it to an open task via a
  focused second pass over the fetched list.

## Read-back formatting (example)

```
[AI Brain]:

Your list (3 open):
1. Buy flight to SP — due Fri
2. Send contract to João
3. Call the accountant
```

## Strings & multi-lingual (updated — no central catalog)

- **Multilingual is not built yet** (`ctx.lang` / a `send()`-level localizer do not
  exist today). Ship this skill **English-only now**, with all user-facing strings
  isolated in `prompt.js` as a `MSG`-style object (exactly like the Audio skill's
  `MSG`, and where Calendar is heading).
- **When multilingual lands**, `multilingual-brain.md` decided **against** a central
  `i18n.js` catalog: each skill keeps its own EN + PT string maps in its `prompt.js`,
  language rides in `ctx.lang`, and the `send()` choke point handles the long tail via
  LLM translation. So keep the strings structured and body-only (never touch the
  `[AI Brain]:` header). Task **titles** always stay verbatim as written.
- Suggested keys: `task.added`, `task.listHeader`, `task.empty`, `task.completed`,
  `task.confirmComplete`, `task.notFound`, `task.failed`.

## Composability

- **Summarizer (idea 2):** after a summary with action items, hand them here to
  batch-add via a confirmation session. Keep as phase 2.

## Files touched

- **New:** `2. Skills/3. Tasks/skill.js`, `2. Skills/3. Tasks/prompt.js`,
  `2. Skills/3. Tasks/SKILL.md` (every skill now ships a human doc).
- **Edit:** `brain/.env.example` (add `GOOGLE_TASKLIST_ID` note + the tasks scope
  reminder); `ARCHITECTURE.md` (add the tasks flow to the "Flow" section);
  `PROJECT_LOG.md` (changelog entry).
- **Setup:** re-consent OAuth with calendar **+** tasks scopes; update
  `GOOGLE_REFRESH_TOKEN` in `/opt/brain/.env`.
- **No i18n catalog file** (there isn't one). No `server.js` / router edits.

## Build order

1. OAuth scope + new refresh token (blocking prerequisite).
2. Tasks client helpers (add / list / complete).
3. Skill + prompt with `TASK_SCHEMA`: **add** and **list** paths first (copy the
   `jsonFormat`/`readReply` structured-output helpers from Calendar).
4. **complete** path with the confirm session (`resumeComplete` + `classifyConfirmation`).
5. `MSG` strings in `prompt.js` (English), structured for later PT.
6. `SKILL.md` + PROJECT_LOG changelog entry.
7. (Phase 2) Summarizer → batch add action items.

## Notes / risks

- **Scope gap** is the main gotcha — without re-consent, Tasks calls 401 and, worse,
  a wrong re-consent could drop the calendar scope. Re-consent with BOTH scopes and
  fail loudly with a clear message if Tasks returns 401/403.
- Google Tasks due dates are **date-only** (no time). Set expectations in the confirm
  copy; a precise-time nudge would need Reminders/Calendar, not Tasks.
- **Complete is destructive-ish** (marks done) → always confirm-first via the session,
  never auto-complete on a bare match.
