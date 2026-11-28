#!/usr/bin/env bash
# Install the fix loop's daily worker on this Mac, or update it after a change to fixer/.
#
#   fixer/install.sh            run every day at 08:00
#   fixer/install.sh 07:30      another time (keep FIX_RUN_AT on the droplet the same)
#   fixer/install.sh --remove   stop and remove the daily job (the queue and the cases stay)
#
# It copies fixer/ to ~/.lisa-fixer/app, OUTSIDE the repo, so that what runs every day is the
# copy installed on purpose and not whatever branch a checkout happens to be on, and so that the
# fix run cannot rewrite the one command it may call. Run it again from an up-to-date tree to
# update. launchd starts a job whose time passed while the Mac slept as soon as it wakes.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
state="$HOME/.lisa-fixer"
label="com.lisa.fixer"
plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "$domain/$label" 2>/dev/null || true
  rm -f "$plist"
  echo "removed the daily job; $state is left as it is"
  exit 0
fi

at="${1:-08:00}"
[[ "$at" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "time must be HH:MM, got: $at" >&2; exit 2; }
hour=$((10#${at%%:*})); minute=$((10#${at##*:}))
py=$(command -v python3) || { echo "python3 not found" >&2; exit 2; }

mkdir -p "$state/app" "$state/bin" "$state/logs" "$state/cases" "$HOME/Library/LaunchAgents"
chmod 700 "$state" "$state/cases"
rsync -a --delete --exclude tests --exclude __pycache__ --exclude install.sh "$here"/ "$state/app/"
install -m 755 "$here/offline-tests.sh" "$state/bin/offline-tests"

cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$py</string><string>$state/app/run.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>WorkingDirectory</key><string>$state</string>
  <key>StandardOutPath</key><string>$state/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$state/logs/launchd.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST

launchctl bootout "$domain/$label" 2>/dev/null || true
launchctl bootstrap "$domain" "$plist"
echo "installed: runs every day at $at"
echo "  run it now:   launchctl kickstart $domain/$label"
echo "  its log:      $state/logs/launchd.log"
echo "  settings:     $state/config.env  (optional; see fixer/README.md)"
