# Task Improvements — one recursive resolver for create / complete / edit (+ stateful, no re-tag)

> **Context.** The `task_action` skill shipped (2026-07-11) as a **single-item** inbox:
> add one to-do, list, complete one (confirm-first), amend the just-added one for a few
> minutes. This plan extends it to **operate on any number of tasks at once**, to **edit a
> task already on file**, and to **stay engaged without re-tagging**. It builds on the
> deployed skill (`secretary/2. Skills/3. Tasks/`) — read `SKILL.md` there first. (The
> original build plan, `task-capture.md`, was retired once shipped; it lives in git history.)

## The one idea this plan is built on

Everything below is one mechanism used three ways. A **secretary reads the conversation,
figures out how many tasks the owner is talking about, works out which task each one is,
and then acts** — create it, check it off, or change it. Complete and edit are the *same*
"which tasks does he mean" problem; batch-create is the *same* "how many distinct tasks"
problem. So we build **one resolver** and route its output. We do **not** write a separate
matcher per action.

Concretely, the two jobs a human secretary does in their head become one structured LLM
call, `planTaskOps`, run against the owner's **open task list**:

1. **Enumerate** — how many distinct tasks is the owner referring to? ("I bought the pizza
   and I got my flights" → two.)
2. **Contextualize** — read the transcript / quoted message for what each one is about.
3. **Match** — for each referenced task, find the one open task it means (or decide it's a
   *new* task to create, or that it's *ambiguous / not found*).
4. **Act** — emit an operation per task.

"Recursive" here means **uniform across 1..N**: one item is just a list of one. The planner,
the confirm step, and every reply render a list; the single-task case is the list-of-one and
must still read naturally ("Mark this done?" not "Mark these 1 done?").

## Motivating bug (observed in production)

Asking to mark several tasks done "got lost." Logs show why: `interpret` returns a **single**
`task_ref`, so "mark A and B done" collapses to one ref — and a compound ref like `"a tarefa
de hoje"` matched nothing (`TASK RESOLVE RAW: 0`) → silent dead-end. The schema and every flow
are single-item, and resolution happens **blind to the open list** (interpret extracts a ref
string; a second call matches it). Fixing enumeration and making resolution **list-aware** is
the same work as batch support — hence this redesign rather than a bolt-on.

## Scope

1. **Batch create** — "add A, B and C to my list" → three tasks.
2. **Batch complete** — "mark A and B done" / "I bought the pizza and got my flights" → both.
3. **Edit an existing task** — "change the flight task's due to Monday", "rename the contract
   task to…" → resolve a stored task and patch it (confirm-first).
4. **Stateful engagement** — once a task interaction is underway, follow-ups need **no
   `@secretary` tag** until the owner is done or the window lapses.

Non-goals (keep out): recurring tasks, subtasks, reordering, cross-list moves.

## Core change — one list-aware planner replaces interpret + resolveTaskRef

Today there are two calls: `interpret` (single `task_ref`, blind to the list) then
`resolveTaskRef` (ref → one index). We collapse them into **one list-aware call** so the model
sees the open tasks while it enumerates and matches — the way a secretary does it. This is
both more robust (transcript + list together) and less code (one prompt, one schema, one
matcher shared by complete and edit).

New output shape (schema in `prompt.js`):

```js
PLAN_SCHEMA = {
  list_requested: boolean,          // "what's on my list?" — independent of ops
  ops: [                            // 0..N, one per DISTINCT task the owner means
    {
      kind: "create" | "complete" | "edit",
      // resolution (complete/edit): which open task, 1-based into the numbered list
      target_index: number | null,      // null = not confidently matched
      candidate_indices: number[],      // when ambiguous (≥2 plausible) — for disambiguation
      ref_text: string | null,          // the owner's own phrase, for clarify copy ("the flight one")
      // data (create/edit): only the fields being set; null = leave/none
      title: string | null,             // create: the task text; edit: new title
      due_iso: string | null,           // -03:00 ISO, relative dates resolved via nowStr
      assignee: { name, email } | null, // create only; a third party ⇒ calendar invite
    },
  ],
}
```

- The list is **passed into the planner** as a numbered list with titles **and due dates**
  (so "the flight one" *and* "the one due Monday" both resolve). One API `list` call precedes
  the planner on every task turn; in a stateful turn we already have the list.
- **Enumerate strictly**: the prompt must instruct — emit **one op per distinct task the owner
  names**; never merge two into one, never split one into two. This is the fix for the compound-ref
  bug.
- **Match by meaning, transcript-aware**: judge each `target_index` by meaning against the list,
  using the transcript/quoted message to disambiguate (e.g. they were just discussing the contract).
  Prefer `null` + `candidate_indices` over a wrong match — "better to ask than to complete/edit the
  wrong task."
- Titles stay verbatim (no translation); dates relative via `nowStr` (−03:00); due is date-only.

A tiny amount of glue in `skill.js` turns `ops` into work:

```
plan = planTaskOps(ctx, openList)          # the one resolver
creates   = ops where kind == "create"
mutations = ops where kind in (complete, edit) and target_index != null
unresolved = ops where kind in (complete, edit) and target_index == null   # ask, don't drop
```

## CREATE (batch) — immediate write + amend window over the whole batch

```
handleCreate(ctx, creates):
  self  = creates where assignee is null/owner
  other = creates where assignee is a third party
  made = [ addTask(env, {title, due: toTasksDue(due_iso)}) for it in self ]   # immediate
  for it in other: ctx.callSkill("calendar_action","startCreate", mapToEvent(it))
  if made: open/refresh the STATEFUL session over the batch (data.items = made)
           send reply.createdBatch(made)   # "Added to your list:\n<dd/mmm> - t1\n<dd/mmm> - t2\n\nTell me if you need to change anything."
```

- **The amend window is just the stateful session (below) carrying the fresh batch.** A follow-up
  like "make the first a return flight" or "delete the flight one" is re-planned as an **edit /
  delete op targeting one of `data.items`** — the same resolver, no separate `reviewAdd` schema.
  (`reviewAdd`/`REVIEW_ADD_SCHEMA` retire; their job is now `kind:"edit"` + a `delete` sub-op.)
- **Delete** during the window: add `"delete"` to the op grammar (a `kind:"edit"` with a delete
  flag, or a small `kind:"delete"`). Removes just that one; keep the rest.
- **Third-party items** still go through Calendar's own confirm-first flow, one session each. To
  avoid competing calendar sessions, **cap third-party items at 1 per message in v1** — if the
  owner batches more than one "remind X…", act on the first and ask them to send the others
  separately (flagged in the reply). A batch that's *all* self opens the Tasks stateful session;
  a mixed batch does both (self items get the window; the single other item is its own calendar
  session).
  - *Why cap rather than queue (verified in `1. Orchestrator/server.js`).* There is **one session
    per chat** (keyed by `remoteJid`), and a session opened inside `callSkill` is **tagged with the
    callee's id** — so the moment `task_action` calls `calendar_action.startCreate`, the calendar
    session takes the chat's only session slot and follow-ups route to calendar, not tasks. Also
    `callSkill` **returns when `startCreate` opens its session** (confirm sent), **not** on final
    confirmation — so there's no clean "calendar finished" signal to drain a queue on. A serial
    queue would need a new cross-skill contract; **deferred**. Cap-at-1 ships within today's model.

## COMPLETE (batch) — one confirmation for the set

```
handleComplete(ctx, mutations, unresolved):
  hit  = [{id,title} for op in mutations]      # already resolved by the planner
  if !hit: reply.notFound(unresolved) ; return
  open confirm session (data.items = hit, data.missed = unresolved)
  send reply.confirmCompleteBatch(hit, unresolved)   # "Mark these done?\n- A\n- B\n(couldn't find: X)\n\nyes?"

resumeComplete(ctx, session):
  decision = classifyConfirmation(...)         # confirm | decline | unrelated
  on confirm: completeTask each id (per-item ok/fail); reply.completedBatch(done, failed)
```

- Resolution already happened in `planTaskOps` — no second matcher. **Partial is fine**: confirm
  what matched, name what didn't. An unmatched ref is now **surfaced, never dropped** (this is the
  core bug fix).
- One `completeTask` per id (Tasks API has no bulk complete); a per-item failure is reported, the
  rest still complete.

## EDIT an existing task — resolve + patch, confirm-first

New `edit` op for tasks **already on file** (beyond the just-added batch).

```
handleEdit(ctx, editOps, unresolved):
  targets = [{id, oldTitle, newTitle?, newDue?} for op in editOps]   # id from target_index
  if !targets: reply.notFound(unresolved) ; return
  open confirm session (data.edits = targets)
  send reply.confirmEdit(targets)   # "Change these?\n- 'A' -> 'A2'\n- 'B' due -> 17/jul\n\nyes?"

resumeEdit(ctx, session):
  on confirm: patchTask(id, {title?, due?}) each; reply.editedBatch(done, failed)
```

- Edit is **confirm-first** (it mutates a stored item), unlike the frictionless post-create amend.
  Title and/or due per item; either may be null (change only what's asked). Reuses `toTasksDue` +
  the same confirm-classifier.
- **Disambiguation, first-class.** When `target_index` is null but `candidate_indices` has ≥2,
  don't fall back to a flat "which one?" — ask against the named candidates: *"Which flight task —
  17/jul buy flight to SP, or 20/jul flight to Rio?"* This is the robustness bar the owner asked
  for on edit.

## Stateful engagement — no re-tag until done

Today only `add` keeps a window open. We generalize that into **one persistent task session**:
after **any** task interaction (create / complete / edit / list / a clarify question), we leave a
session open (`awaitFrom:"owner"`, TTL ~600s) so the **next untagged owner message routes straight
back to `task_action`**. **No orchestrator change** — verified: the router's continuation test
(`server.js:234-239`) is generic (checks only `awaitFrom`, never `intent`/`stage`) and calls
`SKILLS[session.skill](ctx)` for any session; the amend-window dispatch lives entirely in the
skill, not the router. So any session tagged `skill:"task_action"`, `awaitFrom:"owner"` already
continues on an untagged owner message; a re-tag mid-window is a fresh command that clears the
session (`server.js:331`), which is what we want.

Each stateful turn re-runs `planTaskOps` against the **fresh** open list and dispatches its `ops`:
add more, complete, edit, or — on `keep`/`unrelated`/empty ops — **close silently**. This is the
recursion made continuous: the same resolver drives the first tagged message and every follow-up.

- **False-positive guard (the main risk).** The planner must have an explicit "none / unrelated"
  outcome (empty `ops`, `list_requested:false`) and **default to it on any doubt** — exactly how
  `reviewAdd`/`classifyConfirmation` default to `unrelated` today. Off-topic chatter inside the
  window must be a **no-op**, not a stray task. A **~600s TTL** (matching today's amend window)
  keeps it short. **(Decided.)**
- **Pending confirmations short-circuit — but a clear new task re-plans.** If the session is at
  `await_confirmation` (complete/edit), the turn is a yes/no via `classifyConfirmation` first. A
  clear decline/confirm resolves it; an `unrelated` is a no-op; but a message that **clearly names
  a new task** ("actually also add milk") **re-plans** rather than being swallowed — the pending
  confirmation is left intact and re-offered. Ambiguous input stays `unrelated`. **(Decided.)**
- **Exit.** "that's all" / "pronto" / "done" closes; so does the TTL. A `list` request or any op
  **re-arms** the window.

## Session model (unchanged mechanics, batch payloads, one persistent shape)

Same `ctx.sessions` + `awaitFrom:"owner"` + `lang` persistence. One session, three stages:
- `stage:"engaged"`            — no pending mutation; next message re-plans (subsumes amend window).
  `data.recent = [{id,title,due}]` (the just-touched batch, for "the first one" follow-ups).
- `stage:"await_confirmation"`, `intent:"complete"` — `data.items[]` + `data.missed[]`.
- `stage:"await_confirmation"`, `intent:"edit"`     — `data.edits[]`.
Dispatch in `run()` becomes: continuation? → (confirm stage → resume; engaged → re-plan) ; else
fresh `planTaskOps`. The `add/await_amend` special-case is replaced by `engaged` + re-plan.

## Localization (unchanged convention, new plural strings)

New `reply(lang)` entries, en + pt, using the existing `taskLine`/`renderList` + `localizeDueDate`
(`dd/mmm`):
- `createdBatch(items)`, `confirmCompleteBatch(hit, missed)`, `completedBatch(done, failed)`,
  `confirmEdit(targets)`, `editedBatch(done, failed)`, `notFound(missed)`,
  `disambiguate(refText, candidates)`.
Keep the shipped single-item copy reading naturally (a 1-item batch must **not** say "1 task").
Titles verbatim; dates `dd/mmm` localized.

## Files touched

- **Edit:** `secretary/2. Skills/3. Tasks/prompt.js` — replace `TASK_SCHEMA`+`RESOLVE_REF_SCHEMA`
  with `PLAN_SCHEMA` and the list-aware planner prompt (enumerate → contextualize → match → act);
  retire `REVIEW_ADD_SCHEMA`/`buildReviewAdd*` (folded into edit/delete ops); keep
  `CONFIRM_SCHEMA`/`buildConfirm*`; add plural + disambiguate `reply()` strings.
- **Edit:** `secretary/2. Skills/3. Tasks/skill.js` — one `planTaskOps` (replaces `interpret` +
  `resolveTaskRef`); split `ops` into create/mutation/unresolved; batch `handleCreate` (self +
  capped other), `handleComplete`, new `handleEdit`; unify `resumeAmend`→ stateful `engaged`
  re-plan; `resumeComplete`/`resumeEdit`; keep the Google Tasks client (`addTask`/`listTasks`/
  `completeTask`/`deleteTask`/`patchTask`/`toTasksDue`).
- **Docs:** `secretary/2. Skills/3. Tasks/SKILL.md` (batch + edit + no-re-tag examples; drop
  "known limitations"); `PROJECT_LOG.md` changelog; ARCHITECTURE only if the Tasks flow bullet
  changes materially.
- **No orchestrator/router/registry change** — **verified** (`server.js:234-239, 315-322`): the
  router's continuation is generic (dispatches any session by `skill`+`awaitFrom`, ignores
  `intent`/`stage`), so an `engaged` task session continues on an untagged owner message with no
  router edit. The amend-window special-casing lives in the skill, not the router. (One-session-per-chat
  is the constraint that pushed the third-party queue out of scope — see Create note.)

## Build order

1. **`planTaskOps`** (list-aware planner + `PLAN_SCHEMA`) replacing interpret+resolve. Land
   **complete (batch)** first — it directly fixes the observed bug and needs no new write paths,
   and it exercises enumerate + match + partial-miss end to end.
2. **Create (batch)** on the planner; fold the amend window into the stateful `engaged` session
   (create → re-plan handles edit/delete of the fresh batch). Third-party **cap-at-1** (flag the rest).
3. **Edit** op (`handleEdit`/`resumeEdit`, confirm-first) + **disambiguation** copy.
4. **Stateful engagement** — persistent session + re-plan loop + false-positive guard; retire the
   add-only amend special-case.
5. Plural + disambiguate `reply()` strings (en+pt); SKILL.md + PROJECT_LOG.
6. **Verify in production:** multi-add; multi-complete incl. a partial/unmatched ref and the
   "I bought the pizza and got my flights" phrasing; edit due + edit title; ambiguous edit →
   disambiguation; a follow-up with **no tag** inside the window; off-topic chatter inside the
   window stays a no-op; mixed self/other add.

## Risks / decisions to confirm

- **One call vs two.** Merging interpret+resolve into a list-aware `planTaskOps` is the core of the
  redesign (robustness + less code). It costs one `list` API call before the planner on every task
  turn. ✅ recommended; flag if the extra list call is a concern for pure-add latency.
- **Statefulness false positives.** Untagged follow-ups are powerful but risk hijacking normal chat.
  Mitigation: explicit "none/unrelated" default + **~600s TTL** + no-op on ambiguity. ✅ decided.
- **New request while a confirm is pending.** ✅ decided — a clear new task re-plans (pending confirm
  left intact and re-offered); ambiguous input stays unrelated.
- **Partial matches**: confirm what matched, name what didn't — never silently drop. ✅ baked in.
- **Ambiguous refs**: planner returns `candidate_indices`; we disambiguate by name instead of a flat
  "which one?". ✅ upgraded from the old plan.
- **Third-party batch = cap-at-1** in v1 (act on the first, ask for the rest). ✅ decided. A serial
  queue is deferred: the one-session-per-chat model + `callSkill` returning on session-open (not
  completion) means there's no clean "calendar finished" signal to drain on — a queue would need a
  new cross-skill contract. Revisit if the cap proves annoying in practice.
