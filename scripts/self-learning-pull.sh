#!/usr/bin/env bash
# ============================================================================
#  Pull the secretary's failure reports off the droplet.  Runs on the MAC.
#
#  Pull-based by necessity, not by taste: the droplet's GitHub deploy key is READ-ONLY
#  (PROJECT_LOG.md), so production cannot push anything to the repo. The Mac reaches in
#  over the existing `secretaria-droplet` SSH alias instead.
#
#  Reports are ARCHIVED on the droplet (moved to _synced/) after a successful pull, so the
#  same report is never triaged twice — and is still recoverable if this script's local
#  copy is lost.
#
#  Usage:  ./scripts/self-learning-pull.sh
# ============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="secretaria-droplet"
REMOTE_DIR="/opt/secretary/improvements"
INBOX="$REPO/Bugs and Malfunctions/inbox"

mkdir -p "$INBOX"

echo "pulling failure reports from $REMOTE:$REMOTE_DIR …"

# Nothing to do if the spool is empty. `ls` on an empty glob is not an error we want to see.
count=$(ssh "$REMOTE" "ls -1 $REMOTE_DIR/*.md 2>/dev/null | wc -l" | tr -d ' ')
if [ "$count" = "0" ]; then
  echo "no new reports."
  exit 0
fi

# --ignore-existing: a report already in the inbox is never overwritten (it may already be
# half-triaged). Filenames carry a timestamp, so collisions are the same report, not two.
rsync -az --ignore-existing "$REMOTE:$REMOTE_DIR/*.md" "$INBOX/"

# Archive on the droplet ONLY after rsync succeeded (set -e guarantees we got here).
ssh "$REMOTE" "cd $REMOTE_DIR && mkdir -p _synced && mv *.md _synced/ 2>/dev/null || true"

echo "pulled $count report(s) -> Bugs and Malfunctions/inbox/  (archived to $REMOTE_DIR/_synced/)"
echo "inbox now holds $(ls -1 "$INBOX" | wc -l | tr -d ' ') report(s). Next: /triage-failures"
