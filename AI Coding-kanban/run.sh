#!/usr/bin/env bash
# One-command start for manager-kanban.
#
#   ./run.sh              start the board
#   ./run.sh status       what version am I on, what would an update do
#   ./run.sh where        print this project's working folder
#
# This script is SYSTEM: an update replaces it wholesale. Your cards, columns, prompts and
# .env live in the WORKING FOLDER (see ./update.sh and the README) and it never touches them.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# The repo the manager operates on = the folder this system-folder sits in, unless told
# otherwise. WHERE THE STATE LIVES is resolved in Python (manager/workspace.py), not here.
export MANAGER_REPO_DIR="${MANAGER_REPO_DIR:-$(dirname "$HERE")}"

# A system-level .env is still honoured, but the authoritative one lives in the working
# folder and Python loads that. This is only so MANAGER_PYTHON / MANAGER_WORKSPACE can be
# set before the venv even exists.
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi

# ---------------------------------------------------------------------------
# The Claude Agent SDK needs Python 3.10+. The default `python3` on macOS is 3.9.6, and it
# fails at `pip install` with a message that never mentions the version ("could not find a
# version that satisfies claude-agent-sdk"). Find a real interpreter up front instead, and
# if there isn't one, say exactly that.
# ---------------------------------------------------------------------------
find_python() {
  for py in "${MANAGER_PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3; do
    [ -z "$py" ] && continue
    command -v "$py" >/dev/null 2>&1 || continue
    if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      echo "$py"; return 0
    fi
  done
  return 1
}

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; }

if [ ! -d "$HERE/.venv" ]; then
  if ! PY="$(find_python)"; then
    echo "✗ manager-kanban needs Python 3.10 or newer — the Claude Agent SDK does." >&2
    echo "  Your python3 is: $(python3 -V 2>&1 || echo 'not installed')" >&2
    echo >&2
    echo "  Install one:      brew install python@3.12   (then re-run ./run.sh)" >&2
    echo "  Or point at one:  MANAGER_PYTHON=/path/to/python3.12 ./run.sh" >&2
    exit 1
  fi
  echo "Creating virtualenv with $PY ($("$PY" -V 2>&1)) and installing deps (first run)…"
  "$PY" -m venv "$HERE/.venv"
  "$HERE/.venv/bin/pip" install -q --upgrade pip
  "$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
fi

# An existing venv built by an old Python fails the same confusing way. Catch that too.
if ! py_ok "$HERE/.venv/bin/python"; then
  echo "✗ $HERE/.venv runs Python $("$HERE/.venv/bin/python" -V 2>&1 | cut -d' ' -f2); 3.10+ is required." >&2
  echo "  Rebuild it:  rm -rf '$HERE/.venv' && ./run.sh" >&2
  exit 1
fi

# NO mode banner here. Whether the manager is live or mocked is decided in exactly one
# place (manager/manager.py) and printed by the server itself, with the reason. A shell
# guess based only on ANTHROPIC_API_KEY is how this script used to announce MOCK while
# starting up perfectly live.
exec "$HERE/.venv/bin/python" -m manager "$@"
