---
title: Report
pipeline: maint
description: Captures the bug clearly — what's wrong, expected vs actual. Fast.
tools: Read, Grep, Glob
model: inherit
---

## Entry criteria
- The card describes something that is behaving wrongly.

## Work
Pin down the bug so it can be planned. One short pass.
- State what actually happens and what should happen instead.
- Find the likely place in the code — name the file(s) if you can. Don't fix anything.
- If you genuinely can't tell what's wrong from the report, note the one detail you'd need.

## Exit criteria
- The bug is stated as expected-vs-actual, with a pointer at where it likely lives.

## Output
- `REPORT.md` in the card folder:
  - **Expected:** one line.
  - **Actual:** one line.
  - **Likely cause / location:** file(s) or "unknown".
