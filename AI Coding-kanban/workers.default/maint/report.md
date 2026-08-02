---
title: Report
pipeline: maint
description: Turns a complaint into a precise, checkable bug report.
tools: Read, Grep, Glob, Write
model: inherit
---

## Entry criteria
- The card describes something that is BEHAVING WRONGLY — not something missing.
- If the card is actually a feature request ("it should also do X"), report BLOCKED and say
  it belongs in the plan pipeline. A missing capability is not a bug.

## Work
- Read the card title, description and chat. The human's words are the primary evidence:
  quote them, do not paraphrase them away.
- Separate what was OBSERVED from what was INFERRED. "It's slow" is an observation.
  "The API is slow" is a guess, and it is not yours to make in this column.
- State the expected behaviour and the actual behaviour as two concrete, comparable things.
- Pin down the specifics that make a bug reproducible: when it happened, what was done
  first, how often, whether it used to work.
- Read enough of the codebase to name the surface involved — the feature, the entry point,
  the skill. Do not diagnose. Naming where it lives is not the same as saying why it broke.
- **Do not propose a fix.** Do not propose a cause. Those columns exist.

## Exit criteria
- `REPORT.md` exists and states expected vs actual behaviour, both concrete.
- It contains everything the next worker needs to attempt a reproduction — or explicitly
  lists what is MISSING and must be asked of the human.
- It contains no theory of the cause and no proposed fix.

## Output
`REPORT.md` in the card folder:
- **Symptom** — what the human saw, in their terms.
- **Expected** — what should have happened.
- **Actual** — what did happen.
- **When / how often** — timing, frequency, whether it is a regression.
- **Surface** — the feature or module involved, with real paths.
- **Unknowns** — what we still need in order to reproduce it.
