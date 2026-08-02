# Plan — Bring back the @assistant flow (dual-flow, as a fallback)

## Context

`@mary` (the pure-task flow) is failing more than it should. Commit **`5729e61`
"retire the @assistant/@assistente flow, keep only @mary"** removed the older, more battle-tested
flow end-to-end (**53 files, −15,610 lines**). We want it **back, running alongside @mary** — the
dual-flow that existed just before `5729e61` (OLD flow on `@assistant`/`@assistente`, NEW on
`@mary`) — so the owner has a dependable fallback while the @mary reliability work (the router /
stateful plan) lands.

What `5729e61` removed (this is the restore checklist):
- **Deleted** `secretary/1. Orchestrator/legacy/` (5 files: `router.js`, `inputs.js`, `prompt.js`,
  `assistant-settings.js`, `assistant-settings-prompt.js`) and the whole duplicate skill tree
  `secretary/2. Skills/` (7 folders, 21 files).
- **Collapsed** `server.js`'s dual gate to `flow = NEW_FLOW`: dropped the legacy imports,
  `SKILLS`/`CATALOG`/`CAPS` discovery, `LEGACY_SKILLS`/`LEGACY_CATALOG`, `LEGACY_FLOW`, the
  `legacyTag`/`newTag`/`taggedNew`/`useNewFlow` gate, `MAX_SKILL_DEPTH`, `ctx.callSkill`/
  `ctx.hasSkill`, `runLegacyFlow()`, and the legacy `settings` handle + boot-load.
- **Removed** the legacy tag half from `lib/identity.js` (`TAGS`/`seed()`/`setTags()`/
  `matchedTag()`, seeded from `SECRETARY_TAG` default `@assistente,@assistant`).
- **Dropped** `SECRETARY_TAG` from `.env.example` and `evolution/docker-compose.yml`.
- **Retired tests**: `calendar-create`, `calendar-edit`, `calendar-location`, `flights`,
  `tasks-addressed`, `turn-latency` selftests; added `retire-assistant-selftest.mjs`.
- **Scrubbed** the dual-flow narrative from the docs.

## The catch — it's a revert-and-reconcile, not a clean revert

Since `5729e61`, `server.js` + `identity.js` moved **6 commits** and `lib/` moved **4** (native
server-side tools, Google Contacts lookup, read-a-file-after-the-tag, opening-language pinning,
chit-chat close fix). So a plain `git revert 5729e61` restores the deleted trees cleanly but
**conflicts** on `server.js` / `identity.js` / docs / config / tests. The restoration must
**keep every post-retirement @mary improvement** and re-add the legacy flow beside it.

## Recommended approach

Do it on a clean branch (the working tree currently has unrelated changes). Backbone:

1. **`git revert --no-commit 5729e61`.** This re-creates the deleted `legacy/` + `2. Skills/`
   trees for free and re-adds the removed code, surfacing exactly the files to reconcile.
2. **Resolve the conflicts, keeping BOTH sides:**
   - **`server.js`** (the hard part): re-introduce the dual-tag gate around the *current* handler.
     `legacyTag = matchedTag(gateText)` → `runLegacyFlow()` (restored); `newTag`/continuation →
     the **current** @mary handler (untouched, with its native-tools / Contacts / file-read /
     language-pin logic intact). Restore `LEGACY_FLOW`, the `2. Skills/` discovery
     (`SKILLS`/`CATALOG`/`CAPS`), `MAX_SKILL_DEPTH`, `ctx.callSkill`/`ctx.hasSkill`, and the legacy
     `settings` handle + boot-load. @mary continuations already keyed off `!session?.skill`, so
     the legacy `session.skill` sessions stay isolated from @mary's marker.
   - **`lib/identity.js`**: re-add `TAGS`/`seed()`/`setTags()`/`matchedTag()` (seed
     `SECRETARY_TAG` = `@assistente,@assistant`). Keep all `NEW_TAGS` exports. Preserve the
     structural separation: `setTags` mutates only `TAGS`, `setNewTags` only `NEW_TAGS` — a
     tag change in one flow can never alter the other.
   - **Config**: restore `SECRETARY_TAG` in `secretary/.env.example` and
     `evolution/docker-compose.yml`.
   - **Tests**: restore the retired legacy selftests; **remove/invert `retire-assistant-selftest.mjs`**
     (it asserts the legacy flow is gone — the opposite of our goal); re-point the shared
     selftests that the revert touches.
   - **Docs**: re-add the dual-flow narrative to `ARCHITECTURE.md`, `ORCHESTRATOR.md`, `README.md`,
     and add a dated `PROJECT_LOG.md` §10 entry recording the un-retirement.
3. **Compat pass** — the restored `legacy/` + `2. Skills/` import today's `lib/` (4 additive
   commits since). Additive changes shouldn't break them, but **boot the server and run the
   legacy selftests** to catch any signature drift; fix at the call site.

**Fallback if the revert is too messy:** `git checkout 5729e61^ -- "secretary/1. Orchestrator/legacy"
"secretary/2. Skills"` to restore the two trees cleanly, then hand-apply the `server.js` /
`identity.js` / config / docs restore from the `git show 5729e61` diff.

## Deliberate consequences (call these out to the owner)

- **The legacy flow is frozen at retirement.** It will NOT have the post-retirement @mary features
  (Google Contacts lookup, native tools, the calendar fixes since `5729e61`). That's fine for a
  fallback, but `@assistant` will be behind `@mary` on features.
- **Two skill trees return** (`2. Skills/` + `3. Mary Skills/`). A future fix that must apply to
  both flows has to be made twice. This is the standing cost of running a dual flow.
- **Reliability of the goal:** if @mary is the thing failing, a working `@assistant` fallback is
  immediately useful; but the real fix remains the separate @mary router/stateful plan.

## Critical files

- Restore: `secretary/1. Orchestrator/legacy/*` (5), `secretary/2. Skills/**` (21).
- Reconcile: `secretary/1. Orchestrator/server.js`, `secretary/1. Orchestrator/lib/identity.js`.
- Config: `secretary/.env.example`, `evolution/docker-compose.yml`.
- Tests: `scripts/{calendar-create,calendar-edit,calendar-location,flights,tasks-addressed,turn-latency}-selftest.mjs` (restore), `scripts/retire-assistant-selftest.mjs` (remove/invert),
  `scripts/{mary-skills,identity,settings,settings-tag}-selftest.mjs` (re-point if the revert touches them).
- Docs: `ARCHITECTURE.md`, `secretary/1. Orchestrator/ORCHESTRATOR.md`, `README.md`, `PROJECT_LOG.md`.

## Rails changes (for the kanban build review)

This is a large **orchestrator "rails"** change — it restores deleted rails (`legacy/`,
`2. Skills/`) and re-adds the dual-tag gate to `server.js`/`identity.js`. **Authorized.**
Blast radius: it re-introduces a second flow; the **@mary path is preserved unchanged** — the gate
only adds an `@assistant`/`@assistente` branch beside it. Config additions are additive
(`SECRETARY_TAG`); no existing @mary env changes.

## Verification

- **Boot:** the server loads both trees — `loadSkills("3. Mary Skills")` (NEW) and the restored
  `2. Skills/` discovery (OLD) — with no import/signature errors against today's `lib/`.
- **Offline suite:** the restored legacy selftests pass; `identity-selftest` resolves BOTH tag
  lists (`TAGS` = @assistente/@assistant, `NEW_TAGS` = @mary); `settings-tag-selftest` shows a
  @mary tag change never mutates `TAGS` and vice-versa; the @mary selftests still pass.
- **Live, both flows:**
  - `@assistant agende ...` → runs the OLD legacy flow (its own router/extraction passes) and
    creates the event.
  - `@mary agende ...` → runs the current NEW flow unchanged.
  - a tag change made via one flow does not affect the other; a legacy `session.skill` session and
    a @mary marker session don't cross-contaminate.
- **Deploy:** restoring `SECRETARY_TAG` in the droplet `.env` + `docker compose up -d
  --force-recreate secretary` (a secrets/config change, per the §2 runbook).

## Out of scope

- Bringing the legacy `2. Skills/` up to feature parity with `3. Mary Skills/` (it's a frozen
  fallback).
- The @mary reliability rework (separate plan:
  `New Features Plans/router-reliability-and-stateful-mary-conversations.md`).
