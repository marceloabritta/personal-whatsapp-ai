#!/usr/bin/env bash
# ============================================================================
#  Thin wrapper around scripts/board-ingest.mjs — the ONE place `node` is located.
#
#  Both the daily self-learning run and the launchd drain timer call this, so PATH and
#  the working directory are set here once. launchd starts jobs with a minimal PATH, so
#  node (Homebrew or /usr/local) must be put back on it explicitly.
#
#  Usage:  ./scripts/board-ingest.sh <seed|enqueue|drain>
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cd "$REPO" || exit 1
exec node scripts/board-ingest.mjs "$@"
