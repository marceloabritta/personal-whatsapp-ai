# Bugs and Malfunctions

Where the secretary's **self-learning loop** lands. Two kinds of file live here:

| Path | What it is |
|---|---|
| `bugfix-<slug>.md` | **A triaged plan** — root cause + proposed fix, for you to review and ship. The unit of work. |
| `inbox/` | **Raw failure reports**, freshly pulled off the droplet. Not yet triaged. |
| `_reports/` | Raw reports that have already become a plan (archive — the evidence behind it). |

## How a file gets here

The secretary captures its own failures in production (`secretary/improvements/` on the
droplet) — a crash, a task it understood but couldn't execute, an order it couldn't route, or
**you telling it that it made a mistake** (`@secretary you made a mistake here`).

`scripts/self-learning-pull.sh` pulls those reports into `inbox/`. `/triage-failures` then reads
each one, investigates the codebase, and writes a `bugfix-<slug>.md` plan here — filing the raw
report into `_reports/`. **A scheduled job runs both, daily** (see
`scripts/self-learning-daily.sh`). It commits, but never pushes and never deploys: you review,
you ship.

`bugfix-task-false-positive.md` is the reference for what a good plan looks like — evidence from
the logs, a real root cause, a fix that names the files and lines, and an honest statement of
what the fix does *not* guarantee.

Reports marked `Source: OWNER-REPORTED` are triaged first: they're the only human-verified ones.

## The plan header — and how a plan becomes a card

**Every plan opens with a frontmatter header**, above its `# H1`. It is the machine-readable
contract the board ingest (`scripts/board-ingest.mjs`) reads to file the plan as a card:

```
---
title: <becomes the card's title on the board>
one_liner: <one sentence — becomes the card's description>
reports:
  - _reports/<the raw report this plan was written from>
  - _reports/<…and any other it was merged from>
---
```

After triage, `enqueue` + `drain` turn each new `bugfix-*.md` into a card on the kanban board's
**backlog**, typed `kind: maintenance`, over the board's HTTP API (the board is never modified).
The `reports:` list is what stops a report that a plan already covers from *also* earning its own
card. **A report you decline to plan is still filed to `_reports/`** — and because it is named by
no plan, an **owner-reported** one earns its own maintenance card (a machine report stays a filed
no-op). See `Board Inbox/README.md`.
