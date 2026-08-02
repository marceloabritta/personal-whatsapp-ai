#!/usr/bin/env bash
# Upgrade the SYSTEM, then bring the WORKING FOLDER along with it.
#
#   ./update.sh            pull, reinstall deps, back up the working folder, migrate it
#   ./update.sh --check    say what would happen; change nothing
#   ./update.sh --no-pull  skip git (use when the code is already where you want it)
#
# What this guarantees:
#   * your cards, their folders, your columns and gates, your worker prompts and your .env
#     are all in the working folder, and none of them are overwritten;
#   * the folder is COPIED to <workspace>/.backups/ before a single migration runs;
#   * migrations run in order and stop at the first failure, loudly, rather than leaving
#     the folder half-migrated;
#   * a default prompt you never edited picks up upstream's improvements; one you DID edit
#     is kept, and the diff is written to <workspace>/PROMPT_CHANGES.md for you to merge.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PULL=1
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check)   CHECK=1 ;;
    --no-pull) PULL=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

export MANAGER_REPO_DIR="${MANAGER_REPO_DIR:-$(dirname "$HERE")}"
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi

PYBIN="$HERE/.venv/bin/python"
if [ ! -x "$PYBIN" ]; then
  echo "No virtualenv yet — run ./run.sh once first." >&2
  exit 1
fi

if [ "$CHECK" = "1" ]; then
  exec "$PYBIN" -m manager status
fi

BEFORE="$(cat "$HERE/VERSION" 2>/dev/null || echo '0.0.0')"

if [ "$PULL" = "1" ]; then
  if [ ! -d "$HERE/.git" ]; then
    echo "✗ $HERE is not a git checkout, so there is nothing to pull." >&2
    echo "  Re-run with --no-pull to just migrate the working folder against the code you have." >&2
    exit 1
  fi
  echo "→ pulling the system…"
  git -C "$HERE" pull --ff-only
  echo "→ reinstalling dependencies…"
  "$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
fi

AFTER="$(cat "$HERE/VERSION" 2>/dev/null || echo '0.0.0')"
echo "→ system $BEFORE → $AFTER"

# Everything that touches YOUR folder: backup, migrations, three-way prompt merge.
echo "→ migrating the working folder…"
"$PYBIN" -m manager migrate

echo "✓ update complete. Start the board with ./run.sh"
