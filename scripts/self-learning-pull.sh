#!/usr/bin/env bash
# ============================================================================
#  Pull the secretary's spools off the droplet.  Runs on the MAC.
#
#  Pull-based by necessity, not by taste: the droplet's GitHub deploy key is READ-ONLY
#  (PROJECT_LOG.md), so production cannot push anything to the repo. The Mac reaches in
#  over the existing `secretaria-droplet` SSH alias instead.
#
#  TWO independent funnels, pulled by ONE function:
#    /opt/secretary/improvements  -> "Bugs and Malfunctions/inbox"   (failure reports)
#    /opt/secretary/specs         -> "New Features Plans"            (feature specs)
#  Each spool is ARCHIVED on the droplet (moved to _synced/) after a successful pull, so
#  the same file is never processed twice — and is still recoverable if this script's local
#  copy is lost. Nothing is EVER deleted from the droplet without first landing on the Mac.
#
#  WHY `set -e` IS DROPPED HERE (this is load-bearing, do not "restore" it): with errexit
#  on, a function invoked as `pull_spool … || rc=1` runs with errexit SUPPRESSED, so a
#  failing rsync would fall straight through to the archive step and move files that were
#  never transferred — the exact silent-drop this script exists to prevent. Instead every
#  step checks its own status explicitly, and the archive moves ONLY the names captured
#  BEFORE the transfer. `--remove-source-files` is FORBIDDEN: it deletes the droplet's copy
#  instead of staging it into _synced/, destroying the recoverability promised above.
#
#  Usage:  ./scripts/self-learning-pull.sh
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="secretaria-droplet"

# ----------------------------------------------------------------------------
#  pull_spool <remote_dir> <local_dest> <label>
#    0 = pulled (or nothing to pull);  1 = a real failure (transfer or archive).
#  Independent: one spool's failure or emptiness never affects the other.
# ----------------------------------------------------------------------------
pull_spool() {
  local remote_dir="$1" local_dest="$2" label="$3"
  mkdir -p "$local_dest"
  echo "pulling $label from $REMOTE:$remote_dir …"

  # 1. Capture the file list BEFORE the transfer. THIS list — not the directory contents
  #    after the pull — is what gets archived, so a file written into the spool mid-pull is
  #    never archived out from under itself (edge 18).
  local names
  names="$(ssh "$REMOTE" "ls -1 $remote_dir/*.md 2>/dev/null | xargs -n1 basename")" || {
    echo "ls FAILED for $label (droplet unreachable?) — nothing pulled, nothing archived"
    return 1
  }

  # 2. An empty spool is a SKIP, not a stop. (An `exit 0` here once made the whole feature
  #    unreachable on a quiet day — it aborted before the other funnel could run.)
  if [ -z "$names" ]; then
    echo "no new $label."
    return 0
  fi

  # 3. Transfer only the captured names. --ignore-existing: a file already in the dest is
  #    never overwritten (it may already be half-processed); timestamps make collisions the
  #    same file, not two.
  if ! printf '%s\n' "$names" | rsync -az --ignore-existing --files-from=- "$REMOTE:$remote_dir/" "$local_dest/"; then
    echo "rsync FAILED for $label — NOTHING archived (files stay on the droplet; next run re-pulls)"
    return 1
  fi

  # 4. Archive ONLY the captured names into _synced/ on the droplet. A file written into the
  #    spool between steps 1 and 3 is not in this list, is not archived, and is pulled next
  #    run. NEVER a blind `mv *.md`, and NEVER `rsync --remove-source-files`.
  if ! printf '%s\n' "$names" | ssh "$REMOTE" "cd $remote_dir && mkdir -p _synced && xargs -I{} mv -- {} _synced/"; then
    echo "archive FAILED for $label (files stay; next run re-pulls)"
    return 1
  fi

  local n
  n="$(printf '%s\n' "$names" | grep -c .)"
  echo "pulled $n $label -> $local_dest/  (archived to $remote_dir/_synced/)"
  return 0
}

# ---- The two funnels, each into its own status; neither skips the other ------
rc=0

pull_spool "/opt/secretary/improvements" "$REPO/Bugs and Malfunctions/inbox" "report(s)"
rc_reports=$?

pull_spool "/opt/secretary/specs" "$REPO/New Features Plans" "spec(s)"
rc_specs=$?

# Non-zero iff a spool actually FAILED — and only after BOTH were attempted.
if [ "$rc_reports" -ne 0 ] || [ "$rc_specs" -ne 0 ]; then
  rc=1
fi
exit $rc
