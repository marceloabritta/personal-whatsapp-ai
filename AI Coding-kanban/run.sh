#!/usr/bin/env bash
# One-command start for manager-kanban.
# Drop this folder into any repo, then from THIS folder run: ./run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# The repo the manager operates on = the folder this feature-folder sits in,
# unless overridden.
export MANAGER_REPO_DIR="${MANAGER_REPO_DIR:-$(dirname "$HERE")}"

# Load a local .env if present (ANTHROPIC_API_KEY, MANAGER_* overrides).
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi

# Virtualenv + deps (first run only).
if [ ! -d "$HERE/.venv" ]; then
  echo "Creating virtualenv and installing deps (first run)…"
  python3 -m venv "$HERE/.venv"
  "$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ "${MANAGER_MOCK:-}" != "1" ]; then
  echo "⚠  No ANTHROPIC_API_KEY set — starting in MOCK mode (scripted pipeline, no real agent)."
  echo "   Add your key to $HERE/.env to run the real manager."
fi

exec "$HERE/.venv/bin/python" -m manager
