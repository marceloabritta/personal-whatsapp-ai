#!/usr/bin/env bash
# Install the fix loop's tool on this Mac, or update it after a change to fixer/.
#
#   fixer/install.sh            copy the tool to ~/.lisa-fixer and print the task's prompt path
#   fixer/install.sh --remove   remove the old launchd job, if it is still there
#
# The daily run is a scheduled task in the Claude desktop app (its prompt is
# fixer/task-prompt.md), so there is nothing to schedule here. This only puts the tool that
# task calls where it expects it: OUTSIDE the repo, so that what runs every day is the copy
# installed on purpose and not whatever branch a checkout happens to be on, and so that a fix
# cannot rewrite the commands that check it. Run it again from an up-to-date tree to update.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
state="$HOME/.lisa-fixer"
label="com.lisa.fixer"
plist="$HOME/Library/LaunchAgents/$label.plist"

# The first version ran from launchd, headless. That job is retired wherever it is found.
if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || [ -f "$plist" ]; then
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$plist"
  echo "removed the old launchd job"
fi
[ "${1:-}" = "--remove" ] && exit 0

mkdir -p "$state/app" "$state/bin" "$state/logs" "$state/cases"
chmod 700 "$state" "$state/cases"
rsync -a --delete --exclude tests --exclude __pycache__ --exclude install.sh "$here"/ "$state/app/"
install -m 755 "$here/offline-tests.sh" "$state/bin/offline-tests"
echo "installed the tool: python3 $state/app/run.py pull"
echo "the scheduled task's prompt: $state/app/task-prompt.md"
