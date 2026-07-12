#!/usr/bin/env bash
# ============================================================================
#  The daily self-learning run — pull the secretary's failure reports and triage them.
#  Driven by launchd (~/Library/LaunchAgents/com.marcelo.secretary-triage.plist).
#
#  1. pull reports off the droplet  -> "Bugs and Malfunctions/inbox/"
#  2. if the inbox is empty, STOP (cheap no-op on a quiet day — no Claude call, no cost)
#  3. otherwise run `/triage-failures` headless -> "Bugs and Malfunctions/bugfix-<slug>.md"
#
#  IT COMMITS, BUT NEVER PUSHES AND NEVER DEPLOYS. An unattended agent with write access to
#  production was judged the riskiest part of this whole feature; the compromise is that it may
#  write plans and commit them locally, and nothing else. `git push` and `ssh` are explicitly
#  denied below, so a prompt-injected report cannot talk it into shipping anything. The owner
#  reads the plans and ships them himself.
#
#  Run it by hand any time:  ./scripts/self-learning-daily.sh
#  Log:  ~/Library/Logs/secretary-triage.log
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INBOX="$REPO/Bugs and Malfunctions/inbox"

# launchd starts with a minimal PATH — claude, ssh, rsync and git must all be findable.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cd "$REPO" || exit 1
echo "───────────────────────────────────────────────"
echo "$(date '+%Y-%m-%d %H:%M:%S')  self-learning daily run"

# ---- 1. pull -----------------------------------------------------------------
if ! ./scripts/self-learning-pull.sh; then
  echo "pull FAILED (droplet unreachable?) — will retry tomorrow."
  exit 1
fi

# ---- 2. anything to do? ------------------------------------------------------
shopt -s nullglob
reports=("$INBOX"/*.md)
if [ ${#reports[@]} -eq 0 ]; then
  echo "inbox empty — nothing to triage. Done."
  exit 0
fi
echo "triaging ${#reports[@]} report(s)…"

# ---- 3. triage, headless -----------------------------------------------------
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
echo "plans now in 'Bugs and Malfunctions/' — review, then ship them yourself."
exit $status
