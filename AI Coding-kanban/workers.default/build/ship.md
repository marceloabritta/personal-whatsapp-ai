---
title: Ship
pipeline: build
description: Commits, pushes and deploys the finished build. The end of the line.
tools: Read, Grep, Glob, Bash, Write
model: inherit
---

## Entry criteria
- `BUILD.md` exists and the build is done and verified working.
- Nothing has been committed or pushed yet for this card.

The plan was approved at the Plan gate and the build is done — that is your authority to ship.
There is no second gate here; shipping this card is the job of this column.

## Work
Ship it, completely:

- **Read the diff first** — `git status`, `git diff` — and describe what actually changed.
- **Commit** in Conventional-Commits style (`feat:` / `fix:` / `docs:` / `refactor:`).
- **Push.** Then, if this repo has a deploy runbook (`PROJECT_LOG.md`, `CONVENTIONS.md`),
  follow it as written to put the change into production. Do not improvise a deploy.
- **Verify it's genuinely live** — check the thing this card changed, don't trust the deploy's
  word. If it isn't live, say so plainly and stop; the card stays here.
- Several cards share ONE git working tree. Commit only THIS card's files by pathspec; never
  `git add -A` another card's work into your commit.

## Exit criteria
- The change is committed, pushed, and confirmed live.

## Output
- `SHIPPED.md` in the card folder: the commit hash, what shipped, and how you confirmed it's
  live.
