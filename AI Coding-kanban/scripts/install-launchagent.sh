#!/usr/bin/env bash
# Put the board under launchd, so it stops being a child of whatever terminal started it.
#
# THE BUG THIS FIXES: run.sh ends in `exec python -m manager`, and uvicorn runs in the
# foreground — so the server is a child of the terminal that launched it. Closing the VS Code
# window whose integrated terminal was running it tears down that process group, the server
# takes a SIGKILL, and nothing brings it back. That is exactly how a four-minute scoping run
# was lost one second before it hit disk.
#
#   ./scripts/install-launchagent.sh                 always-on: starts at login, restarts on ANY exit
#   ./scripts/install-launchagent.sh --on-crash-only starts only when you say so; restarts only if it CRASHES
#   ./scripts/install-launchagent.sh --uninstall     remove it
#   ./scripts/install-launchagent.sh --status        is it loaded? is it running?
#
# Either way, closing VS Code no longer touches it, and a `kill -9` is back inside ~5s with
# the interrupted run resumed automatically (see manager/recovery.py).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="always-on"
for arg in "$@"; do
  case "$arg" in
    --on-crash-only) MODE="on-crash-only" ;;
    --uninstall)     MODE="uninstall" ;;
    --status)        MODE="status" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPO="${MANAGER_REPO_DIR:-$(dirname "$HERE")}"
SLUG="$(basename "$REPO" | tr '[:upper:] ' '[:lower:]-' | tr -cd '[:alnum:]-')"
LABEL="com.manager-kanban.${SLUG}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

WORKSPACE="$("$HERE/run.sh" where 2>/dev/null || true)"
[ -z "$WORKSPACE" ] && WORKSPACE="$HOME/.manager-kanban/$SLUG"
LOGDIR="$WORKSPACE/logs"

case "$MODE" in
  status)
    echo "label:     $LABEL"
    echo "plist:     $PLIST $([ -f "$PLIST" ] && echo '(installed)' || echo '(NOT installed)')"
    launchctl list | grep -F "$LABEL" || echo "not loaded"
    exit 0
    ;;
  uninstall)
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✓ removed $LABEL. The board is no longer supervised — start it yourself with ./run.sh"
    echo "  Nothing in your working folder was touched."
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# macOS TCC: a launchd agent runs in a context with NO consent to read ~/Desktop,
# ~/Documents or ~/Downloads. If the system folder lives in one of those, bash cannot even
# exec run.sh ("Operation not permitted", exit 126) — and with KeepAlive it would sit there
# crash-looping every 5 seconds forever. Refuse up front instead of shipping that.
# ---------------------------------------------------------------------------
case "$HERE/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*)
    PROTECTED="$(echo "$HERE" | sed "s|$HOME/\([^/]*\)/.*|\1|")"
    cat >&2 <<MSG
✗ Refusing to install: this folder is inside ~/$PROTECTED, which macOS protects.

  A launchd agent runs without permission to read ~/Desktop, ~/Documents or ~/Downloads,
  so it cannot even start run.sh — it would fail with "Operation not permitted" and then
  crash-loop every 5 seconds, forever. That is worse than no supervision at all.

  system folder: $HERE

  Fix it in one of two ways:

  1. Move the system folder somewhere launchd can reach, then re-run this:
       mv "$HERE" ~/manager-kanban && cd ~/manager-kanban && ./scripts/install-launchagent.sh
     Your working folder ($WORKSPACE) is NOT in the way and does not move.
     Nothing on the board is affected — the system is disposable, the state is elsewhere.

  2. Or grant Full Disk Access to /bin/bash in
       System Settings → Privacy & Security → Full Disk Access
     (broader than you probably want, for a board.)

  Without launchd you lose nothing except automatic restarts: a killed run is still
  journalled and still resumes the next time you run ./run.sh.
MSG
    exit 1
    ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

# always-on:      RunAtLoad + KeepAlive=true  → up at login, back after ANY exit (incl. kill -9)
# on-crash-only:  KeepAlive={SuccessfulExit:false} → you start it; launchd only resurrects it
#                 if it dies badly. A clean Ctrl-C stays stopped.
if [ "$MODE" = "always-on" ]; then
  RUN_AT_LOAD="<true/>"
  KEEP_ALIVE="<true/>"
else
  RUN_AT_LOAD="<false/>"
  KEEP_ALIVE="<dict><key>SuccessfulExit</key><false/></dict>"
fi

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${HERE}/run.sh</string>
  </array>
  <key>WorkingDirectory</key>   <string>${HERE}</string>
  <key>RunAtLoad</key>          ${RUN_AT_LOAD}
  <key>KeepAlive</key>          ${KEEP_ALIVE}
  <key>ThrottleInterval</key>   <integer>5</integer>
  <key>ProcessType</key>        <string>Background</string>
  <key>StandardOutPath</key>    <string>${LOGDIR}/launchd.log</string>
  <key>StandardErrorPath</key>  <string>${LOGDIR}/launchd.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MANAGER_REPO_DIR</key> <string>${REPO}</string>
    <key>MANAGER_WORKSPACE</key><string>${WORKSPACE}</string>
    <key>PATH</key>             <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

# Never walk away from a KeepAlive job without checking it actually came up. A job that
# cannot start is a job that restarts forever — verify, and undo ourselves if it failed.
if [ "$MODE" = "always-on" ]; then
  PORT="${MANAGER_PORT:-4173}"
  for _ in $(seq 1 30); do
    curl -sf -o /dev/null "http://127.0.0.1:${PORT}/api/config" && UP=1 && break
    sleep 1
  done
  if [ "${UP:-0}" != "1" ]; then
    echo "✗ The agent was installed but the board never came up on port $PORT." >&2
    echo "  Backing the install out so it does not sit there crash-looping." >&2
    echo >&2
    echo "  Last lines of $LOGDIR/launchd.log:" >&2
    tail -5 "$LOGDIR/launchd.log" 2>/dev/null | sed 's/^/    /' >&2
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo >&2
    echo "  Nothing is supervised, and nothing on your board was touched. Start it by hand:" >&2
    echo "    ./run.sh" >&2
    exit 1
  fi
fi

echo "✓ installed $LABEL  ($MODE)"
echo "  plist:     $PLIST"
echo "  repo:      $REPO"
echo "  workspace: $WORKSPACE"
echo "  log:       $LOGDIR/launchd.log  (and $LOGDIR/manager.log)"
if [ "$MODE" = "on-crash-only" ]; then
  echo
  echo "  It is NOT running yet. Start it when you want it:"
  echo "    launchctl kickstart gui/$UID/$LABEL"
  echo "  From then on, a crash brings it back; a clean stop leaves it stopped."
else
  echo
  echo "  The board is now running at http://127.0.0.1:4173 and will come back at login,"
  echo "  after a crash, and after you close any terminal or editor window."
fi
echo
echo "  Undo at any time:  ./scripts/install-launchagent.sh --uninstall"
