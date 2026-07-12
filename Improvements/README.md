# Improvements — the self-learning loop's output

Failure reports the secretary wrote about **itself** in production, and the implementation
plans they turn into.

| Path | What's in it |
|---|---|
| `inbox/` | Raw failure reports freshly pulled off the droplet. Not yet triaged. |
| `_reports/` | Raw reports that have already been turned into a plan (archive). |
| `<date>-<slug>.md` | Implementation plans, for review. |

**The loop:** the secretary captures a failure (crash, unrouted order, a soft "I couldn't", or
**you telling it that it was wrong** — see `secretary/2. Skills/5. Feedback/`) →
`scripts/self-learning-pull.sh` pulls the reports to this folder → `/triage-failures` reads each
one, finds the root cause and writes a plan → you review, and ship.

Reports marked `Source: OWNER-REPORTED` come first: they're the only human-verified ones.
