#!/usr/bin/env bash
# ============================================================================
#  The daily self-learning run — pull the secretary's spools, triage the reports, and
#  turn every triaged plan (and every owner-reported failure) into a card on the kanban
#  backlog. Driven by launchd (~/Library/LaunchAgents/com.marcelo.secretary-triage.plist).
#
#  1. pull reports + specs off the droplet   -> "Bugs and Malfunctions/inbox/", "New Features Plans/"
#  2. if the inbox has reports, run `/triage-failures` headless -> "Bugs and Malfunctions/bugfix-<slug>.md"
#  3. enqueue: scan the funnels, write one queue entry per NEW plan/spec/owner-report
#  4. drain: POST each queued entry to the board as a typed backlog card (board down -> a no-op)
#
#  EVERY step runs, even if an earlier one failed or was empty: work already on the Mac
#  still gets delivered when the droplet is unreachable, and an empty inbox no longer stops
#  the enqueue/drain. The final exit status reflects every failure that happened.
#
#  IT COMMITS, BUT NEVER PUSHES AND NEVER DEPLOYS. An unattended agent with write access to
#  production was judged the riskiest part of this whole feature; the compromise is that it may
#  write plans and commit them locally, and nothing else. `git push` and `ssh` are explicitly
#  denied below, so a prompt-injected report cannot talk it into shipping anything. The enqueue
#  and drain are plain shell/node HERE — never something the headless agent does. The owner
#  reads the plans and ships them himself.
#
#  Run it by hand any time:  ./scripts/self-learning-daily.sh
#  Log:  ~/Library/Logs/secretary-triage.log
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INBOX="$REPO/Bugs and Malfunctions/inbox"

# launchd starts with a minimal PATH — claude, ssh, rsync, node and git must all be findable.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cd "$REPO" || exit 1
echo "───────────────────────────────────────────────"
echo "$(date '+%Y-%m-%d %H:%M:%S')  self-learning daily run"

rc=0

# ---- 1. pull (reports + specs) ----------------------------------------------
# A failed pull does NOT stop the run: plans already on the Mac still get delivered.
if ! ./scripts/self-learning-pull.sh; then
  echo "pull FAILED (droplet unreachable?) — continuing: work already on the Mac still gets delivered"
  rc=1
fi

# ---- 2. triage any reports in the inbox -------------------------------------
shopt -s nullglob
reports=("$INBOX"/*.md)
if [ ${#reports[@]} -eq 0 ]; then
  echo "inbox empty — nothing to triage."
else
  echo "triaging ${#reports[@]} report(s)…"
  # acceptEdits lets it write the plans without prompting. The allow-list is what it may run;
  # the deny-list is the part that matters — it cannot push, cannot ssh, cannot restart anything.
  claude -p "/triage-failures" \
    --permission-mode acceptEdits \
    --allowedTools \
      "Read" "Write" "Edit" "Grep" "Glob" \
      "Bash(git add:*)" "Bash(git commit:*)" "Bash(git mv:*)" \
      "Bash(git status:*)" "Bash(git log:*)" "Bash(git diff:*)" "Bash(git show:*)" \
      "Bash(ls:*)" "Bash(mv:*)" \
    --disallowedTools \
      "Bash(git push:*)" "Bash(ssh:*)" "Bash(docker:*)" "Bash(rm:*)" "Bash(curl:*)" \
      "WebFetch" "WebSearch"
  status=$?
  left=$(ls -1 "$INBOX"/*.md 2>/dev/null | wc -l | tr -d ' ')
  echo "triage exit=$status; $left report(s) still in the inbox"
  [ "$status" -ne 0 ] && rc=1
fi

# ---- 3. enqueue: turn new plans/specs/owner-reports into queue entries --------
# ALWAYS runs — a new plan written on a day the pull found no reports must still be queued.
if ! ./scripts/board-ingest.sh enqueue; then
  echo "enqueue FAILED"
  rc=1
fi

# ---- 4. drain: deliver the queue to the board as backlog cards ---------------
# ALWAYS runs. A down board is a clean no-op (exit 0) — the queue is retained and the
# launchd drain timer retries every 5 minutes.
if ! ./scripts/board-ingest.sh drain; then
  echo "drain FAILED (board up but an entry errored) — entries stay queued and retry"
  rc=1
fi

echo "plans now in 'Bugs and Malfunctions/'; cards land on the board's backlog. Review, then ship them yourself."
exit $rc
