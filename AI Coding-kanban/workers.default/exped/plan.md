---
title: Plan
pipeline: exped
description: The whole plan on one page — the change, the test that proves it, the risk. GATE.
tools: Read, Grep, Glob, Write
model: inherit
---

## Entry criteria
- `SCOPE.md` exists and says why this belongs in the fast lane.
- If it flagged that this is NOT small enough for expedited, report BLOCKED and say the card
  should be re-routed to the full pipeline. Do not plan it here anyway.

## Work
This column is a **GATE**: the human approves this plan before a single line of code is
written. So the plan has to be readable by them and complete enough to act on.

- Write the plan file by file: what changes in each, and why.
- **Name the test.** One test that fails today and passes when this is done. For a fix, it
  is the reproduction of the bug. For a small feature, it is the new behaviour. The build
  worker writes it FIRST — that is the safety this pipeline keeps when it drops the others.
- Name what could regress, and how you would notice.
- Keep it short. If the plan does not fit on a page, this card is not an expedited card;
  say so and send it back.

## Exit criteria
- `PLAN.md` exists, names real files, and is executable by someone who has only read it.
- It names exactly one test that fails now and passes after.
- Regression risk is stated, not hand-waved.
- The whole thing fits on a page.

## Output
`PLAN.md` in the card folder:
- **What changes** — file by file.
- **The test** — the one that fails now and passes after, and where it lives.
- **Risk** — what could break, and how we would know.
- **Not doing** — anything deliberately left out.
