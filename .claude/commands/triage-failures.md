---
description: Pull the secretary's failure reports off the droplet and turn each into a bugfix plan in "Bugs and Malfunctions".
---

Turn the secretary's production failures into bugfix plans I can review.

## 1. Pull

Run `./scripts/self-learning-pull.sh` (safe to re-run; a scheduled job may have pulled already).

Then look at `Bugs and Malfunctions/inbox/`. **If it is empty, say so and stop** — do not go
looking for work elsewhere.

## 2. Triage each report in the inbox

**Take `Source: OWNER-REPORTED` reports first, and believe them.**

A machine report (`throw:*`, `unrouted`, `soft`) is a stack trace: it proves *something broke*.
An owner report is a human saying *this output was wrong* — and it is the only evidence that
exists for a whole class of bug (false positives, confidently wrong answers) where the code ran
perfectly and produced garbage.

- The **"Owner's report"** section is **ground truth about the symptom**. Don't argue with it,
  explain it away, or decide he misunderstood his own secretary. Find the code path that
  produced what he saw.
- The **"Auto-analysis"** section is an unverified guess from a cheap model. One lead among
  several — discard it freely. It is not evidence.
- The **quoted message**, when the report confirms it is secretary output, *is the defect*.
  Start there.

For each report:

1. Read it in full — the logs and the transcript included.
2. **Investigate the codebase and find the real root cause.** Read the actual call chain; do
   not pattern-match on the error string. Verify every claim against the code before writing
   it down.
3. Write the plan to **`Bugs and Malfunctions/bugfix-<slug>.md`**.
4. Move the raw report to `Bugs and Malfunctions/_reports/`.
5. `git add` + `git commit` each plan separately, with a descriptive message.

## 3. What a plan looks like

**`Bugs and Malfunctions/bugfix-task-false-positive.md` is the reference — match it.** What
makes it good, and what yours must have:

- A **header table**: when, chat, trigger, source, skill, severity, status.
- A **summary** a human can read in 20 seconds.
- **Evidence** — the timeline from the logs, the verbatim exchange, the raw model output. Quote
  it; don't paraphrase it.
- **The call chain that actually executed**, with `file.js:line` at every step.
- A **root cause** that explains *why*, not just *where*. If there are two defects, say which
  one is the real one.
- **Ruled out** — the plausible explanations you checked and eliminated. This is what makes the
  root cause trustworthy.
- **A proposed fix**: the files to touch, the lines, the prompt text. Say what is deferred and
  why.
- **An honest limitation** — if the fix is probabilistic, say so plainly.
- **Verification** — the acceptance test, plus the regressions to watch for.

Merge reports that describe the same incident (same chat, timestamps minutes apart — e.g. a
`soft` report *and* the owner's note about it) into **one** plan, and file all their raw reports.

## 4. Rules

- **Do not push. Do not deploy. Do not fix the bug.** Plans only — I review, then ship.
  (If the fix is a genuine one-liner, still write the plan, and say so in it.)
- If a report is too thin to act on, say so plainly rather than inventing a root cause to fill
  the page. A short honest note beats a confident wrong one.

## 5. Report back

One line per plan written, plus anything you couldn't triage and why.
