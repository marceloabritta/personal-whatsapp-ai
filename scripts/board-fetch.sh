#!/usr/bin/env bash
# ============================================================================
#  board-fetch.sh — the FAST lane: get new work onto the board within ~1 minute.
#
#  Runs every ~60s from launchd (com.marcelo.board-fetch). Three cheap, deterministic
#  steps — NO model call, NO triage:
#
#    1. pull    self-learning-pull.sh — rsync the droplet's spools onto the Mac:
#                 /opt/secretary/specs        -> New Features Plans/   (feature requests)
#                 /opt/secretary/improvements -> Bugs and Malfunctions/inbox/ (error logs)
#    2. enqueue board-ingest.sh enqueue — scan the funnels, queue every NEW spec / triaged
#                 plan / owner-reported failure (ledger-guarded, so nothing is re-queued).
#    3. drain   board-ingest.sh drain — POST each queued entry to the board as a typed
#                 backlog card. Single-flight lock, idempotent, a down board is a no-op.
#
#  This is deliberately split from self-learning-daily.sh (the ~30-min TRIAGE loop): a
#  FEATURE REQUEST needs no thinking, so it lands here in ~1 min; a machine ERROR LOG is
#  noisy, so it waits for triage to filter it into a real bugfix plan — which THIS loop
#  then cards within ~1 min of triage finishing. Every step is idempotent and non-fatal:
#  a failing pull (droplet asleep) still lets work already on the Mac reach the board.
#
#  Nothing here ever runs the headless agent, so there is no injection surface — it is
#  plain shell + node against the board's HTTP API.
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$REPO" || exit 1

echo "───────────────────────────────────────────────"
echo "$(date '+%Y-%m-%d %H:%M:%S')  board-fetch (fast lane)"
rc=0

# 1. Pull the droplet's spools. A failure here (droplet unreachable) is not fatal: work
#    already on the Mac still gets enqueued and delivered below.
if ! ./scripts/self-learning-pull.sh; then
  echo "pull FAILED (droplet unreachable?) — continuing with what is already on the Mac"
  rc=1
fi

# 2. Enqueue new specs / plans / owner-reports. Ledger-guarded and idempotent.
if ! ./scripts/board-ingest.sh enqueue; then
  echo "enqueue FAILED"
  rc=1
fi

# 3. Deliver the queue to the board as backlog cards. Locked, idempotent, board-down = no-op.
if ! ./scripts/board-ingest.sh drain; then
  echo "drain FAILED (board up but an entry errored) — entries stay queued and retry next tick"
  rc=1
fi

echo "$(date '+%Y-%m-%d %H:%M:%S')  board-fetch done (rc=$rc)"
exit "$rc"
