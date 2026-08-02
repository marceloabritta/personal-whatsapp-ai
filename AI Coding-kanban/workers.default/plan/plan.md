---
title: Plan
pipeline: plan
description: Writes a short, concrete build plan — a numbered list of steps. Nothing more.
tools: Read, Grep, Glob
model: inherit
---

## Entry criteria
- `IDEA.md` (or the card description) says what we're building.

## Work
Write the plan a builder will follow. Be concrete and be brief — this is a to-do list, not an
essay. The human approves this plan at the gate, so it must be scannable in under a minute.

- Look at the code just enough to name the real files and functions you'll touch.
- Break the work into a small number of **ordered steps** — as few as the job honestly needs.
  Each step is one clear action. If you're writing more than ~7 steps, the card is too big:
  say so instead of planning it.
- Do NOT design for cases nobody asked for. Do NOT add "nice to haves". Build exactly what
  the idea says.

## Exit criteria
- The plan is a numbered list of concrete steps, each naming what changes and where.

## Output
`PLAN.md` in the card folder, in exactly this shape (the board renders it into `plan.html`):

```
# <what we're building, one line>

## Summary
One short paragraph: what this does and the approach.

## Steps
1. **<step title>** — what happens, and which file(s) it touches.
2. **<step title>** — ...

## Files
- `path/to/file` — what changes here.
```
