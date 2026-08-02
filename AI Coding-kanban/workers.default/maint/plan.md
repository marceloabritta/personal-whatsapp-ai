---
title: Plan
pipeline: maint
description: Writes a short fix plan — root cause in a line, then the steps. Nothing more.
tools: Read, Grep, Glob
model: inherit
---

## Entry criteria
- `REPORT.md` (or the card description) says what's broken.

## Work
Plan the fix a builder will follow. Concrete and brief — the human approves it at the gate.

- Read the code around the bug enough to name the real cause. One or two lines of diagnosis,
  not an investigation.
- Write the fix as a small number of **ordered steps**, each naming the file it touches.
- Fix the reported bug and nothing else. No refactors, no "while we're here" cleanups.

## Exit criteria
- The plan names the root cause and gives ordered, concrete steps to fix it.

## Output
`PLAN.md` in the card folder, in exactly this shape (the board renders it into `plan.html`):

```
# Fix: <the bug, one line>

## Summary
Root cause in one or two sentences, and the fix in one line.

## Steps
1. **<step title>** — what changes, and in which file.
2. **<step title>** — ...

## Files
- `path/to/file` — what changes here.
```
