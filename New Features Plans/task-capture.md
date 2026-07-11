# Task Capture ("Todo Inbox") — Implementation Plan

> **Freshness note (2026-07-11).** Aligned to the current codebase: structured
> outputs (`output_config.format` + JSON Schemas), the stateful session/continuation
> layer (`ctx.sessions` + `ctx.session`, `awaitFrom`), the per-skill `prompt.js`
> string convention (NOT a central i18n catalog — see the localization convention in `../ARCHITECTURE.md`),
> the `SKILL.md` doc convention, and skill folder numbering (`3. Tasks/`).
> Multilingual is still a plan, so this ships English-only and slots into the
> per-skill EN+PT map when that lands.

## Goal

Capture todos from chat, back them with the right Google service depending on WHO
the todo is for, and read them back.

- Add for yourself: `@brain add "buy flight to SP" to my todos`
- Add for someone else: `@brain remind João to send the contract by Friday`
- Capture from a message: reply with `@brain turn this into a task`
- Read back: `@brain what's on my list?`
- Complete: `@brain mark the flight one done` (confirm-first)
- (Phase 2) From a summary's action items: "save these 3 as tasks?"

## Core design decision — split by TARGET

A "task" means two different things depending on the recipient, so it maps to two
backends. **Google Tasks has no attendees, no sharing, and sends no email** — it's a
private, single-user list. So:

| Target | Backend | Why |
|---|---|---|
| **Yourself** | Google Tasks (private list) | No one to notify; a fast personal inbox |
| **Someone else** | Google **Calendar** (5-min event, you + them as attendees) | Calendar already emails the invite (`sendUpdates=all`) — the only Google surface that notifies an external person |

The third-party case **reuses the existing Calendar create flow** rather than
duplicating it (see "Composing skills" below). This keeps one maintained
implementation of the confirm-first + email-chase + modify lifecycle.

## Both paths are STATEFUL at creation

| Target | Stateful model | Session owner |
|---|---|---|
| **Yourself** | Create immediately (`addTask`) → **amend window**: a session stays open ~10 min so a follow-up patches/deletes the just-created task. No draft step (a private todo has no external side-effect). | `task_action` |
| **Someone else** | **Draft → `yes` → create + invite** (Calendar's existing confirm-first flow; nothing is written until you approve, because it emails a real person). | `calendar_action` (delegated) |

## Composing skills — capability registry (orchestrator-owned)

Skills never import each other's files. They compose through a **capability registry
the orchestrator builds at boot and injects into `ctx`**. This is the robust design:
decoupled from folder paths, graceful when a skill is absent, and centralized.

**Two planes per skill.** The *routable* face (`manifest` + `run`) is what the router
sees. The new *internal* face is an optional `capabilities` export — functions other
skills may call. Capabilities are NEVER shown to the router; they're a private
skill-to-skill API.

- Calendar exposes its create flow **by id, not by path** — a thin wrapper over the
  existing private `handleCreate(ctx, info)`:
  ```js
  // 2. Skills/1. Calendar Actions/skill.js
  export const capabilities = {
    // ctx is injected by the orchestrator; caller passes only the event info.
    startCreate: (ctx, info) => handleCreate(ctx, info),
  };
  ```
- The orchestrator collects these into `CAPS[skillId]` in `loadSkills()` and injects
  two helpers into every `ctx`:
  ```js
  ctx.hasSkill = (id, name) => typeof CAPS[id]?.[name] === "function";
  ctx.callSkill = async (id, name, ...args) => {
    const fn = CAPS[id]?.[name];
    if (!fn) throw new Error(`capability ${id}.${name} unavailable`);
    const depth = (ctx._skillDepth || 0) + 1;
    if (depth > MAX_SKILL_DEPTH) throw new Error(`skill-call depth exceeded at ${id}.${name}`);
    return fn({ ...ctx, _skillDepth: depth }, ...args);   // auto-injects THIS ctx + loop guard
  };
  ```
- Tasks consumes it with **zero coupling to Calendar's file location**:
  ```js
  if (!ctx.hasSkill("calendar_action", "startCreate"))
    return ctx.send(ctx.number, MSG.calendarUnavailable);
  return ctx.callSkill("calendar_action", "startCreate", mappedInfo);
  ```

**Why this is the robust choice:**
- **No path coupling** — the skill *id* is the contract; renaming the Calendar folder
  never breaks Tasks (its `manifest.id` stays `calendar_action`).
- **Graceful absence** — `hasSkill` gives a friendly "calendar unavailable" message;
  `callSkill` throws a typed error otherwise, caught by the orchestrator's per-skill
  try/catch. A skill that fails to load simply isn't in the registry.
- **Centralized** — ctx-injection and the loop/recursion guard (`MAX_SKILL_DEPTH`)
  live in one place, not scattered across skills.
- **Testable** — inject a fake `CAPS` into `ctx` and assert delegation without loading
  the real Calendar skill.
- `ARCHITECTURE.md` gains a "Composing skills" section documenting the two planes, the
  `capabilities` export, `ctx.hasSkill`/`ctx.callSkill`, and the session-ownership rule
  (below).

### Delegation hands off the whole lifecycle

When Tasks calls `calendar_action.startCreate`, Calendar opens a session tagged
`skill: "calendar_action"`. So the `yes`, the "make it 3pm", and the *chase-the-
missing-email* continuations all flow back to **Calendar's** `run` via the
orchestrator — automatically. **Tasks only initiates the third-party case; it never
handles create confirmations/modifications.**

### Failure semantics (what happens if Calendar breaks)

- **Missing/not loaded** → `hasSkill` is false → Tasks sends `MSG.calendarUnavailable`;
  self-tasks, list, complete keep working.
- **Throws during delegation** (synchronous, inside Tasks' `run`) → bubbles to the
  orchestrator's per-skill try/catch → generic error; **nothing written** (the
  third-party path writes nothing until `yes`, and never calls `addTask`).
- **Throws at the `yes`** → owned entirely by Calendar's `resumeCreate` try/catch
  (clears the session, tells the owner it failed in Google). Tasks isn't involved.
- **No partial state ever**: self → Tasks only; other → Calendar only. Mutually
  exclusive branches, so no double-write or orphan.

## Prerequisite: Google Tasks scope (blocking)

- Current refresh token is Calendar-only (`.../auth/calendar`). Google Tasks needs
  `https://www.googleapis.com/auth/tasks`.
- **Re-consent with BOTH scopes at once** (calendar + tasks) so Calendar keeps working;
  mint a new refresh token (OAuth Playground → own client id/secret → both scopes →
  exchange → copy `1//…` into `/opt/brain/.env` `GOOGLE_REFRESH_TOKEN=` →
  `--force-recreate`).
- Consent screen must be **"In production"** (else ~7-day `invalid_grant`; PROJECT_LOG §8).
- Note it in `SKILL.md` so a scope gap fails loudly.

## New skill — `2. Skills/3. Tasks/`

`skill.js` (logic + Google Tasks client) · `prompt.js` (prompt builders + JSON Schemas
+ `MSG` strings) · `SKILL.md` (human doc). Auto-discovered at boot.

- `manifest`:
  ```js
  export const manifest = {
    id: "task_action",
    description:
      "add a to-do for the owner OR for another person, list the owner's open tasks, or complete a task",
  };
  ```

### `run(ctx)` dispatch (mirrors Calendar's shape)

```
run(ctx):
  # 1. CONTINUATIONS owned by this skill
  if session.intent === "add"      && stage === "await_amend":        return resumeAmend(ctx, session)
  if session.intent === "complete" && stage === "await_confirmation": return resumeComplete(ctx, session)
  # (third-party CREATE continuations belong to calendar_action, not here)

  # 2. FRESH ORDER — one structured call
  info = interpret(ctx)     # { action, title, due_iso, task_ref, assignee }
  if info.action === "add" and assignee is me/absent:  return handleAddSelf(ctx, info)
  if info.action === "add" and assignee is someone:    return handleAddOther(ctx, info)
  if info.action === "list":                            return handleList(ctx)
  if info.action === "complete":                        return handleComplete(ctx, info)
  send(MSG.noAction)
```

### interpret (structured output)

`anthropic.messages.create` with `output_config: jsonFormat(TASK_SCHEMA)`, read via the
`readReply` helper copied from Calendar. Schema:

```js
TASK_SCHEMA = {
  type: "object", additionalProperties: false,
  required: ["action", "title", "due_iso", "task_ref", "assignee"],
  properties: {
    action:   { type: "string", enum: ["add", "list", "complete", "other"] },
    title:    { type: ["string", "null"] },
    due_iso:  { type: ["string", "null"] },   // -03:00
    task_ref: { type: ["string", "null"] },    // free-text, for complete
    assignee: {                                // null/owner => self; else => Calendar
      anyOf: [{ type: "null" },
              { type: "object", additionalProperties: false,
                required: ["name", "email"],
                properties: { name: {type:["string","null"]}, email:{type:["string","null"]} } }],
    },
  },
};
```

## ADD (self) — immediate write + amend window

```
handleAddSelf(ctx, info):
  title = info.title || ctx.quoted?.text
  if !title: send(MSG.needTitle); return
  task = addTask(env, { title, due: dueDate(info.due_iso) })     # writes NOW
  sessions.set(remoteJid, {
    skill:"task_action", intent:"add", stage:"await_amend", awaitFrom:"owner",
    data:{ taskId: task.id, title, due: info.due_iso },
  }, 600)
  send(`Added to your list: "${title}"${dueNote}. Change anything, or say "done".`)

resumeAmend(ctx, session):                       # runs on EVERY owner msg while open
  review = reviewAdd(ctx, session.data)          # {decision: amend|keep|delete|unrelated, title?, due_iso?}
  if unrelated: return                            # silent on chatter
  if keep:   sessions.clear(remoteJid); return    # "done" — silent close
  if delete: deleteTask(env, taskId); sessions.clear; send(`Removed "${title}".`); return
  # amend
  patchTask(env, taskId, { title?, due? })
  sessions.set(... refreshed data + TTL ...)      # re-arm for further edits
  send(`Updated: "${newTitle}"${dueNote}.`)
```

- No draft/confirm before writing: private, reversible (the amend window includes
  `delete`, so "actually cancel that" undoes it).
- `reviewAdd` is Calendar-`reviewCreate`-shaped but patches an already-created task.

## ADD (other) — delegate to Calendar (confirm-first, emails the invite)

```
handleAddOther(ctx, info):
  if !ctx.hasSkill("calendar_action", "startCreate"):
      send(MSG.calendarUnavailable); return
  start_iso = atThreePM(info.due_iso)     # due date @ 15:00 -03:00; no due => today (or tomorrow if past 15:00)
  return ctx.callSkill("calendar_action", "startCreate", {
    action: "create",
    title: info.title || ctx.quoted?.text,
    participants: [{ name: info.assignee.name, email: info.assignee.email }],
    start_iso,
    duration_min: 5,
    summary: info.title || "",
  })
```

- Reuses Calendar's entire stateful flow: if the email is missing it asks and waits;
  shows the draft; `yes` writes + invites; "make it 3pm" modifies — all owned by
  `calendar_action`. Tasks writes **no** create/confirm code for this path.
- Fixed slot: **15:00 −03:00, 5 min**, on the due date.

## LIST — read-back, no session

```
handleList(ctx):
  items = listTasks(env)                 # open only
  if !items.length: send(MSG.empty); return
  send(formatList(items))                # numbered, with due dates (deterministic, no LLM)
```

```
[AI Brain]:

Your list (3 open):
1. Buy flight to SP — due Fri
2. Send contract to João
3. Call the accountant
```

## COMPLETE — confirm-first session (mirror Calendar delete)

```
handleComplete(ctx, info):
  open = listTasks(env); if !open.length: send(MSG.empty); return
  match = resolveTaskRef(ctx, info.task_ref, open)     # focused LLM match; null if unsure
  if !match: send(MSG.notFound); return
  sessions.set(remoteJid, { skill:"task_action", intent:"complete",
    stage:"await_confirmation", awaitFrom:"owner", data:{ taskId, title } }, 600)
  send(`Mark this done?\n- ${title}\n\nReply "yes" to confirm.`)

resumeComplete(ctx, session):
  decision = classifyConfirmation(ctx, { action:`mark "${title}" done` })   # Calendar helper + CONFIRM_SCHEMA
  if unrelated: return                                                       # silent on chatter
  if decline: sessions.clear; send(`Okay, leaving "${title}" open.`); return
  completeTask(env, taskId); sessions.clear; send(`Done — checked off "${title}".`)
```

## Google Tasks client (in `skill.js`)

- `tasksClient(env)` = `google.tasks({version:"v1", auth})`, OAuth2 + refresh token,
  same shape as `calendarClient`. `listId(env)` = `GOOGLE_TASKLIST_ID || "@default"`.
- Helpers: `addTask({title, due})` → `tasks.insert`; `listTasks()` → `tasks.list({showCompleted:false})`;
  `completeTask(taskId)` → `tasks.patch({status:"completed"})`; `deleteTask(taskId)` → `tasks.delete`;
  `patchTask(taskId, {title?, due?})` → `tasks.patch`.
- Due is **date-only** RFC3339 — `dueDate()` truncates; confirm copy shows a date, never a time.

## Strings & multi-lingual (no central catalog)

- Ship **English-only**; all user-facing strings in `prompt.js` as a `MSG` object
  (Audio-skill style), body-only (never the `[AI Brain]:` header). Task **titles stay
  verbatim**.
- Multilingual has landed (2026-07-11): give this `MSG` an `{ en, pt }` shape selected by
  `ctx.lang`, per the per-skill-map convention in `../ARCHITECTURE.md`. No central `i18n.js`.

## Files touched

- **New:** `2. Skills/3. Tasks/skill.js`, `prompt.js`, `SKILL.md`.
- **Edit:** `1. Orchestrator/server.js` (capability registry in `loadSkills()` +
  `ctx.hasSkill`/`ctx.callSkill` + `MAX_SKILL_DEPTH`); `2. Skills/1. Calendar Actions/skill.js`
  (add `capabilities.startCreate`); `ARCHITECTURE.md` ("Composing skills" section +
  tasks flow); `brain/.env.example` (`GOOGLE_TASKLIST_ID` + tasks scope reminder);
  `PROJECT_LOG.md` (changelog).
- **Setup:** re-consent OAuth (calendar + tasks); update `GOOGLE_REFRESH_TOKEN`.
- No i18n catalog file; no router edits.

## Build order

1. OAuth scope + new refresh token (blocking).
2. **Capability registry** in the orchestrator (`loadSkills` collects `capabilities`;
   `ctx.hasSkill`/`ctx.callSkill` + `MAX_SKILL_DEPTH`); expose `capabilities.startCreate`
   from Calendar; document both in ARCHITECTURE "Composing skills". Land this first —
   it's the shared plumbing, independently testable with a fake `CAPS`.
3. Google Tasks client helpers (add / list / complete / delete / patch).
4. Skill + prompt with `TASK_SCHEMA` (copy `jsonFormat`/`readReply`): **add-self** +
   **list** first.
5. **add-self amend window** (`resumeAmend` + `reviewAdd`).
6. **add-other** delegation to `startEventCreate`.
7. **complete** confirm session (`resumeComplete` + `classifyConfirmation`).
8. `MSG` strings, `SKILL.md`, PROJECT_LOG changelog.
9. (Phase 2) Summarizer → batch add.

## Notes / risks

- **Scope gap** — re-consent with BOTH scopes; a wrong re-consent could drop calendar.
  Fail loudly on Tasks 401/403.
- **Tasks due is date-only** — no time nudge; that's what the Calendar path is for.
- **Complete + delete are state-changing** → always via a session, never on a bare match.
- **Self vs other must be unambiguous** — the interpret prompt keys off an explicit
  recipient other than the owner. "schedule a *meeting* with X" is still a Calendar
  action via the router; "add a *task/todo* for X" flows through here then delegates.
