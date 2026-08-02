# Board Inbox

The staging area between the secretary's two funnels and the kanban board. `scripts/board-ingest.mjs`
turns feature specs (`New Features Plans/feature-*.md`) and triaged bugfix plans
(`Bugs and Malfunctions/bugfix-*.md`, plus owner-reported failures no plan claims) into cards on the
board's **backlog**, exactly once, over the board's existing HTTP API. The board itself is never
modified — it is consumed, not edited.

## What is in here

| Path | Tracked? | What it is |
|---|---|---|
| `ledger.tsv` | **yes** | The authority. One append-only line per source file ever accounted for: `<iso>\t<repo-relative path>\t<why>`, `why ∈ seed \| enqueued \| planned`. Membership is tested on the **path** column. |
| `queue/<basename>.json` | no (runtime) | One card waiting to be created. Written by `enqueue`, consumed by `drain`. |
| `delivered/<basename>.json` | no (runtime) | The archive of cards already created. An archived entry is never reconsidered. |
| `.drain.lock` | no (runtime) | The single-flight lockfile (holder PID + start time). A dead holder's lock is broken loudly. |

## Why `ledger.tsv` is tracked — and must never be deleted

The ledger is what stops a card being opened twice, and what stops a file that predates this
feature from ever becoming a card. It has to survive a fresh clone: if it is lost, the next
`enqueue` sees an unaccounted tree and re-opens a card for **everything on disk**. That is why it
is committed while everything else in here is gitignored runtime state.

## Commands

```sh
# One-time, before the first enqueue ever runs — account for everything already on disk.
# Refuses to run if the ledger already exists.
node scripts/board-ingest.mjs seed        # then: git add "Board Inbox/ledger.tsv" && commit

# Scan the funnels and queue any NEW plan/spec/owner-report. Refuses without a ledger.
./scripts/board-ingest.sh enqueue

# Deliver the queue to the board as backlog cards. A down board is a clean no-op.
./scripts/board-ingest.sh drain
```

The daily job (`scripts/self-learning-daily.sh`) runs `enqueue` then `drain` after the pull, and a
launchd timer (`scripts/com.marcelo.board-ingest.plist`) drains every 5 minutes so a plan written
tonight becomes a card within minutes of the board being up.
