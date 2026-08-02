# Shipped 2026-07-14 — The orchestrator holds the conversation (NEW flow), run in PARALLEL with the OLD flow

**Commit:** `4ba22bc` (`feat(orchestrator): the model holds the conversation — NEW flow runs in
parallel with the OLD one, split by summon tag`). Built on top of HEAD `d3c5c98`.

> This is the scope/plan summary archived on ship. The card's full working docs (IDEA, SCOPE,
> PLAN, TESTS, BUILD, BUILD_REVIEW) live in the kanban card folder
> `55e00052-new-architecture-ai-on-orchestrator-only`, not in `New Features Plans/` — so there
> was no `New Features Plans/*.md` to `git mv`; this file is created fresh.

## What shipped

Two things, together, in one server.

### 1. The NEW conversation loop (the model holds the conversation)

The orchestrator runs a **turn loop**. Each turn the model chooses one of three states:

- **listen** — ask a question, propose an action, or stay silent; keep the marker open and wait.
- **execute** — run one or more skills. `execute` is **non-terminal**: a *converted* skill returns
  a JSON value that the orchestrator feeds back as a **read-back** turn (the model reads its own
  result, then usually closes).
- **done** — close the conversation.

`route(ctx, turn)` returns `{ say, next, skills, info, lang, awaitFrom }`. The loop enforces the
caps (`MAX_TURNS`, `MAX_DISPATCHES`, `MAX_REPAIRS`), the **write invariant** (a read-back may not
execute), makes deliberate silence free, and runs a **repair loop** for validation failures.

`assistant_settings` is the converted **pilot** (`manifest.conversation:"orchestrator"`, declares
its `inputs`). The other six skills declare `conversation:"skill"` (or default to it) and are
otherwise unchanged.

### 2. Dual-tag parallel run — test the NEW system live without risking the OLD one

Both flows run in ONE server, branched on the summon tag as early as possible in the webhook:

```
@assistant (SECRETARY_TAG)     -> the OLD flow  (his live daily driver, provably unchanged)
@mary      (SECRETARY_TAG_NEW) -> the NEW turn loop  (only he tests it)
```

The OLD flow runs entirely on **frozen copies** of the pre-card code under
`secretary/1. Orchestrator/legacy/` (router, prompt, input-contract, and the propose/confirm
`assistant_settings`) that the NEW flow never imports. The NEW flow's `assistant_settings` mutates
a **separate** tag list (`NEW_TAGS` via `setNewTags`) persisted to a **separate** key
(`secretary:settings:new:tags`).

**The invariant:** a bug anywhere in the `@mary` path is *structurally* incapable of changing what
`@assistant` does. Verified by Build Review (each legacy file diffed byte-for-byte against
`git show HEAD:<path>`; `identity.js`/`settings.js` purely additive; the `useNewFlow` branch and
every `sessions.set` traced so a NEW-flow marker can only be created by `@mary`).

### Folded-in fix

A repair turn now gets its own `buildRepairUser` prompt that **invites** a corrected execute — the
read-back prompt that forbids executing was being reused for the repair loop it was fighting.

## Rails (all authorized by the plan/brief; additive — no existing signature or caller changed)

- `server.js` — the turn loop, the dual-tag branch, `runLegacyFlow` (HEAD body verbatim on frozen
  modules), the NEW-tag boot.
- `router/router.js` + `router/prompt.js` — three-state contract, `CONVERSATION:` catalog line,
  `buildReadbackUser` / `buildRepairUser`.
- `lib/inputs.js` — scalar-`of`, `describeProblems`, the `CONVERSATION:` render.
- `lib/whatsapp.js` — `buildLabeledTranscript`.
- `lib/identity.js` — `NEW_TAGS` / `setNewTags` / `matchedTagNew` (legacy exports untouched).
- `lib/settings.js` — optional `ns` namespace (absent `ns` → byte-for-byte the original key).
- New `legacy/` subtree — frozen HEAD copies, imported only by the OLD path.

## Tests

- New `scripts/settings-selftest.mjs` — the three-state cycle end-to-end + the write invariant +
  the caps + the repair loop + a **§6 dual-tag** assertion proving OLD and NEW run isolated in one
  server.
- `scripts/turn-latency-selftest.mjs` — re-pinned to the three-state shape (+ scalar-array guards).
- All four offline suites green at ship: `settings-selftest`, `turn-latency-selftest`,
  `selflearning-selftest`, `history-selftest`.

## Known follow-ups / gates

- **Live router check (`scripts/router-selftest.mjs`) — still required, human-gated, costs money.**
  `router/prompt.js` and every catalog entry changed, so NEW-flow classification must be checked.
  The manager ruled this deferred: the owner tests `@mary` live himself. The OLD (`@assistant`)
  path uses the frozen HEAD router prompt, so its routing is unchanged from what is already live.
- **Temporary scaffolding.** The `legacy/` subtree + the dual-tag branch exist for a live A/B. When
  the migration completes they are removed and only the turn loop remains — worth its own card.
