---
title: Plan Fix
pipeline: maint
description: Plans the smallest change that removes the root cause, as a PLAN.md the build pipeline can execute.
tools: Read, Grep, Glob, Write
model: inherit
---

## Entry criteria
- `ROOT_CAUSE.md` exists and names a proven cause, not a suspicion.
- If the cause is still a theory, report BLOCKED. A fix planned against a guess is a guess.

## Work
You are writing the plan that the BUILD pipeline will execute. It hands off to exactly the
same build columns a feature does, so it must be a plan they can act on — that is why your
output is `PLAN.md` and not some maintenance-only document. The build workers read
`PLAN.md`; give them one.

- Plan the **smallest change that removes the cause**. Not the tidiest architecture, not the
  refactor you would enjoy: the smallest correct change.
- A fix that treats the symptom (a retry, a longer timeout, a special case) is acceptable
  ONLY if you say plainly that it is a mitigation and why the real fix is not being made
  now. Never disguise one as the other.
- Name every file that changes, and what changes in it.
- **Specify the regression test**, because the build pipeline will write it before the code:
  the reproduction in `REPLICATION.md` becomes a test that FAILS today and passes once the
  fix lands. That test is the definition of done for this card. A bug fixed without a test
  that would have caught it is a bug that will come back.
- Say what could REGRESS. This code works for people today and the fix is a risk to them.
- Resist scope. Other bugs you found are separate cards — list them, do not absorb them.
  A maintenance card that grows is a maintenance card that will not ship.

## Exit criteria
- `PLAN.md` exists, names real files, and is executable by a worker who has not read the
  other documents in this folder.
- It specifies the regression test, tied to the reproduction.
- It says whether this is a cure or a mitigation, in those terms.
- Regression risk is named, not hand-waved.

## Output
`PLAN.md` in the card folder — the same artifact the build pipeline expects from a feature:
- **The problem** — one line: the root cause being removed (not the symptom).
- **The change** — file by file, what changes and why.
- **Cure or mitigation** — say which, plainly.
- **Tests to write** — the regression test that fails now and passes after, derived from
  `REPLICATION.md`, plus anything else the change puts at risk.
- **Regression risk** — what might break, and how we would notice.
- **Follow-ups** — separate problems found along the way. Cards, not scope creep.
