# Shipped — Restore the @assistant flow (dual-flow)

**Shipped:** 2026-08-01
**Card:** a45d87a5-restore-the-assistant-flow

## What shipped
The legacy `@assistant` / `@assistente` flow is restored as a frozen fallback that lives
**beside** the current `@mary` flow. Both can be summoned; the dual-tag gate routes tagged
and untagged messages to the correct flow, with a `useNewFlowFor` discriminator (replacing the
former `!session?.skill` check) and a `legacySessions` `flow:"legacy"` + `open:true` stamp so
that restored legacy confirm→continue windows survive main's `open`-gated continuation.

## Commits (on origin/main)
- `8101087` — feat(orchestrator): restore @assistant/@assistente flow as a frozen fallback beside @mary
- `4127bf7` — fix(orchestrator): reconcile restored legacy flow with @mary's open-gate continuation
- (this archive stub) — docs(shipped): archive restore-the-assistant-flow plan on ship

## Plan-doc note
The original seed plan doc lived at `New Features Plans/bring-back-assistant-flow-dual-flow.md`.
That file was **untracked** and existed only in the main working tree — it was never committed to
git and is absent from this card's branch/worktree (whose base predates it). Rather than reach
across trees, this stub records the ship in `Shipped Features/`. The untracked seed doc in the
main tree should be cleaned up separately by the manager.

See the card folder (`PLAN.md`, `BUILD.md`, `BUILD_FIX.md`, `BUILD_REVIEW.md`, `SHIPPED.md`) for
the full narrative, scope, and review.
