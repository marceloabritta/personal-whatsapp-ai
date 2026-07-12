---
description: Pull the secretary's failure reports off the droplet and turn each into an implementation plan.
---

Pull the secretary's failure reports from production and turn each one into an
implementation plan I can review.

## 1. Pull

Run `./scripts/self-learning-pull.sh`. If the inbox is empty, say so and stop.

## 2. Triage each report in `Improvements/inbox/`

**Take `Source: OWNER-REPORTED` reports first, and believe them.**

A machine report (`throw:*`, `unrouted`, `soft`) is a stack trace: it proves *something
broke*. An owner report is a human saying *this output was wrong* — and it is the only
evidence that exists for a whole class of bug (false positives, confidently wrong answers,
right-but-annoying behaviour) where the code ran perfectly and produced garbage.

- The **"Owner's report"** section is **ground truth about the symptom**. Do not argue with
  it, do not explain it away, do not decide he misunderstood his own secretary. Your job is
  to find the code path that produced what he saw.
- The **"Auto-analysis"** section is an unverified guess from a cheap model. Treat it as one
  lead among several and discard it freely — it is not evidence.
- The **quoted message**, when the report says it is confirmed secretary output, *is the
  defect itself*. Start there.

For each report:

1. Read it in full — including the logs and the transcript.
2. Investigate the codebase properly and find the **root cause**. Read the actual code path;
   don't pattern-match on the error string.
3. Write an implementation plan to `Improvements/<YYYY-MM-DD>-<slug>.md`, in the style of
   `New Features Plans/*.md`: what broke, why, the fix, the risks, and how to verify it.
   Verify every claim against the code before you write it down.
4. Move the raw report to `Improvements/_reports/`.
5. Commit each plan separately, with a descriptive message.

**If several reports describe the same incident** — same chat, timestamps within a few
minutes (e.g. a `soft` report *and* the owner's note about it) — **merge them into one plan**
and move all of their raw reports.

## 3. Rules

- **Do not push. Do not deploy.** Write plans and commit them locally; I review and ship.
- Do not fix the bugs yet — a plan per report, not a patch per report. (If a fix is a genuine
  one-liner, still write the plan, and say so in it.)
- If a report is too thin to act on, say so plainly in your summary rather than inventing a
  root cause to fill the page.

## 4. Report back

A short list: one line per plan written, plus anything you couldn't triage and why.
