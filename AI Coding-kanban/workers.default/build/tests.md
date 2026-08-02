---
title: Tests
pipeline: build
description: Writes the tests from the plan, before the code exists. They must fail for the right reason.
tools: Read, Grep, Glob, Bash, Write, Edit
model: inherit
---

## Entry criteria
- `PREFLIGHT.md` exists and its final verdict is `GO`.
- `PLAN.md` specifies the tests to write.
- If preflight said NO-GO, report BLOCKED. Do not build on a failed preflight.

## Work
- Write the tests `PLAN.md` describes, following the repository's existing conventions,
  framework and layout. Match what is already there.
- Assert on behaviour, not on implementation detail.
- Cover the edge cases `SCOPE.md` names, not just the happy path.
- Run the suite. The new tests SHOULD fail right now. Confirm they fail because the
  feature is missing — not because the test itself is broken.

## Exit criteria
- The test files exist in the repository (not in the card folder).
- The new tests FAIL, and you have the output proving they fail for the right reason.
- No implementation code was written. That is the next column's job.

## Output
- The test files themselves, in the repository.
- `TESTS.md` in the card folder: each test file, what each test asserts, and the failure
  output proving they fail for the right reason today.
