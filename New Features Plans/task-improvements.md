# Task Improvements — Batch create/complete + Edit existing

> **Context.** The `task_action` skill shipped (2026-07-11) as a **single-item** inbox:
> add one to-do, list, complete one (confirm-first), amend the just-added one for a few
> minutes. This plan extends it to **operate on multiple tasks at once** and to **edit a
> task already on file**. It builds on the deployed skill (`2. Skills/3. Tasks/`) — read
> `SKILL.md` there first. (The original build plan, `task-capture.md`, was retired and
> deleted once shipped; it lives in git history.)

## Motivating bug (observed in production)

Asking to mark several tasks done "got lost." Logs show why: `interpret` returns a
**single** `task_ref`, so "mark A and B done" collapses to one ref — and a compound ref
like `"a tarefa de hoje"` matched nothing (`TASK RESOLVE RAW: 0`) → silent dead-end. The
schema and every flow are single-item. Fixing this is the same work as batch support.

## Scope

1. **Batch create** — "add A, B and C to my list" → three tasks.
2. **Batch complete** — "mark A and B done" → both, one confirmation.
3. **Edit an existing task** — "change the flight task's due to Monday", "rename the
   contract task to…" → resolve a stored task and patch it (confirm-first).

Non-goals (keep out): recurring tasks, subtasks, reordering, cross-list moves.

## Core change — arrays everywhere (one model, N items)

Today `TASK_SCHEMA` is single-valued (`title`, `due_iso`, `task_ref`, `assignee`). The
central change is to make the interpret output **lists**, so one item is just a list of
one. New shape (schema in `prompt.js`):

```js
TASK_SCHEMA = {
  action: "add" | "list" | "complete" | "edit" | "other",
  add_items:     [{ title, due_iso, assignee }] | null,   // for add (1..N)
  complete_refs: [ "free-text ref", ... ]        | null,   // for complete (1..N)
  edits:         [{ ref, new_title, new_due_iso }] | null, // for edit (1..N)
}
```
- `assignee` per add-item keeps the self-vs-other split **per task** (a batch can mix a
  self todo and a "remind Ana…" — each self item → Google Tasks, each other item →
  `calendar_action.startCreate`).
- Titles stay verbatim; dates relative via `nowStr` (−03:00); date-only due unchanged.
- The prompt must be explicit: **enumerate every distinct task/ref** the owner names;
  never merge two into one, never split one into two.

## ADD (batch) — immediate write + amend window over the batch

```
handleAdd(ctx, info):
  self  = add_items where assignee is null/owner
  other = add_items where assignee is a third party
  created = []
  for it in self:  created.push(addTask(env, {title, due: toTasksDue(it.due_iso)}))   # immediate
  for it in other: ctx.callSkill("calendar_action","startCreate", mapToEvent(it))      # each delegated
  if created.length:
     open an amend session over the BATCH (data.items = created ids+titles+dues)
     send(reply.addedBatch(created))          # "Added to your list:\n<dd/mmm> - t1\n<dd/mmm> - t2\n\nTell me if you need something to change…"
```
- **Amend window now covers the batch.** `reviewAdd` (renamed `reviewBatch`) decides,
  per the latest message: amend *which* item / delete *which* item / keep (close) /
  unrelated. It resolves the target among `data.items` by description (small LLM step),
  then patches/deletes that one and re-arms. "delete the flight one" removes just it.
- **Third-party items** still go through Calendar's own confirm-first flow, one session
  each. A batch that's *all* third-party opens no Tasks amend session. Mixed batches:
  self items get the amend window; each other item is its own `calendar_action` session.
  (Sequential `callSkill` — the last calendar session wins the chat's session slot;
  acceptable, and worth a one-line note to the owner if >1 third-party in a batch.
  Decision to confirm: **cap third-party items at 1 per message in v1**, ask the owner
  to send additional ones separately — avoids competing sessions. Flag in reply.)

## COMPLETE (batch) — one confirmation for the set

```
handleComplete(ctx, info):
  open = listTasks(env); if empty -> reply.empty
  matches = resolveTaskRefs(ctx, info.complete_refs, open)   # LLM: refs[] -> indices[]; may be partial
  hit  = matches.resolved     # [{id,title}]
  miss = matches.unresolved   # ["a tarefa de hoje", ...]
  if !hit.length -> reply.notFound(miss)
  open a confirm session (data.items = hit, data.missed = miss)
  send(reply.confirmCompleteBatch(hit, miss))   # "Mark these done?\n- A\n- B\n(couldn't find: X)\n\nyes?"

resumeComplete(ctx, session):
  decision = classifyConfirmation(...)   # confirm | decline | unrelated
  on confirm: completeTask each id (collect per-item ok/fail); reply.completedBatch(done, failed)
```
- **`resolveTaskRefs`** is the batch version of `resolveTaskRef`: one LLM call, the
  numbered open list + all refs, returns an index (or null) per ref → dedup. Partial
  matches are fine: confirm what matched, report what didn't (fixes the "got lost"
  silence — an unmatched ref is now surfaced, not dropped).
- One `completeTask` per id (Tasks API has no bulk complete); a per-item failure is
  reported, the rest still complete.

## EDIT an existing task — resolve + patch, confirm-first

New `edit` action for tasks **already on file** (beyond the post-add amend window).

```
handleEdit(ctx, info):
  open = listTasks(env); if empty -> reply.empty
  targets = for each {ref,new_title,new_due_iso} in info.edits:
              match ref -> open task (resolveTaskRefs), attach the requested change
  if no target resolved -> reply.notFound
  open a confirm session (data.edits = [{id, oldTitle, newTitle?, newDue?}])
  send(reply.confirmEdit(targets))   # "Change these?\n- 'A' -> 'A2'\n- 'B' due -> 17/jul\n\nyes?"

resumeEdit(ctx, session):
  on confirm: patchTask(id, {title?, due?}) each; reply.editedBatch(done, failed)
```
- Reuses `resolveTaskRefs` + `toTasksDue` + the same confirm-classifier. Edit is
  **confirm-first** (it mutates a stored item; unlike the frictionless post-add amend).
- Title change and/or due change per item; either may be null (change only what's asked).

## Session model (unchanged mechanics, batch payloads)

Same `ctx.sessions` + `awaitFrom:"owner"` + `lang` persistence. Stages:
- `intent:"add",  stage:"await_amend"`        — data.items[] (batch amend)
- `intent:"complete", stage:"await_confirmation"` — data.items[] + data.missed[]
- `intent:"edit", stage:"await_confirmation"` — data.edits[]
Dispatch in `run()` grows an `edit` branch alongside `add`/`complete`.

## Localization (unchanged convention, new plural strings)

New `reply(lang)` entries, en + pt, using the existing `taskLine`/`renderList` +
`localizeDueDate` (`dd/mmm`):
- `addedBatch(items)`, `confirmCompleteBatch(hit, missed)`, `completedBatch(done, failed)`,
  `confirmEdit(targets)`, `editedBatch(done, failed)`, `notFound(missed)`.
Keep the shipped single-item copy working (a 1-item batch should read naturally, not
"1 task"). Titles verbatim; dates `dd/mmm` localized.

## Files touched

- **Edit:** `2. Skills/3. Tasks/prompt.js` (array `TASK_SCHEMA`; `EDIT`/batch prompts;
  `resolveTaskRefs`; new plural `reply()` strings) and `2. Skills/3. Tasks/skill.js`
  (batch `handleAdd`/`handleComplete`, new `handleEdit`/`resumeEdit`, batch `reviewBatch`).
- **Docs:** `2. Skills/3. Tasks/SKILL.md` (batch + edit examples; drop the "known
  limitations" once shipped); `PROJECT_LOG.md` changelog; ARCHITECTURE only if the Tasks
  flow bullet changes materially.
- **No orchestrator/router/registry change.** The capability registry, delegation, and
  session layer already support all of this.

## Build order

1. Array `TASK_SCHEMA` + `resolveTaskRefs` (batch matcher) + interpret prompt that
   enumerates every task/ref. Land with **complete (batch)** first — it directly fixes
   the observed bug and needs no new write paths.
2. **Add (batch)** + batch amend window (`reviewBatch`), self items first; third-party
   cap-at-1 rule.
3. **Edit** action (`handleEdit`/`resumeEdit`, confirm-first).
4. Plural `reply()` strings (en+pt); SKILL.md + PROJECT_LOG.
5. Verify in production: multi-add, multi-complete (incl. a partial/unmatched ref),
   edit due + edit title, mixed self/other add.

## Risks / decisions to confirm

- **Partial matches**: confirm what matched, name what didn't — never silently drop
  (this is the core bug). ✅ baked in.
- **Third-party batch**: capped at 1/message in v1 to avoid competing calendar sessions.
  Confirm this is acceptable, or we design a queue.
- **Ambiguous refs** ("the meeting one" when two match): resolver returns the best single
  index or null; on null we ask. Consider returning *all* candidates to disambiguate —
  deferred unless it bites.
- **Amend vs edit overlap**: the post-add amend window stays (frictionless, no confirm);
  `edit` is the confirm-first path for older tasks. Two paths, clearly scoped by whether
  a fresh-add session is open.
