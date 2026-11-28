#!/usr/bin/env bash
# The one command the fix run may execute: Lisa's and the dispatcher's offline tests, in the
# worktree it is standing in. Installed OUTSIDE the repo (~/.lisa-fixer/bin/offline-tests) so the
# run cannot rewrite the command it is allowed to call.
#
#   offline-tests                       every tests/run_*.py of Lisa and of the dispatcher
#   offline-tests run_weekday.py        one or more of Lisa's, by file name
#   offline-tests dispatcher            the dispatcher's only
set -uo pipefail
root=$(git rev-parse --show-toplevel) || exit 2
py="${FIXER_REPO:-$HOME/Code/personal-whatsapp-ai}/Lisa flow/.venv/bin/python"
[ -x "$py" ] || { echo "no test environment at $py" >&2; exit 2; }
fail=0
run() {  # folder, test file
  local out rc
  out=$(cd "$root/$1" && "$py" "tests/$2" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then
    fail=1
    echo "FAILED $1/tests/$2"
    echo "$out" | grep -E '\[FAIL\]|Error|Traceback' | head -40
    echo "$out" | tail -n 5
  else
    echo "ok     $1/tests/$2 :: $(echo "$out" | tail -n 1)"
  fi
}
if [ $# -eq 0 ] || [ "$1" = "all" ]; then
  for t in "$root/Lisa flow/tests"/run_*.py; do run "Lisa flow" "$(basename "$t")"; done
  for t in "$root/dispatcher/tests"/run_*.py; do run "dispatcher" "$(basename "$t")"; done
elif [ "$1" = "dispatcher" ]; then
  for t in "$root/dispatcher/tests"/run_*.py; do run "dispatcher" "$(basename "$t")"; done
else
  for name in "$@"; do
    case "$name" in run_*.py) ;; *) echo "not an offline test: $name" >&2; exit 2 ;; esac
    if [ -f "$root/Lisa flow/tests/$name" ]; then run "Lisa flow" "$name"
    elif [ -f "$root/dispatcher/tests/$name" ]; then run "dispatcher" "$name"
    else echo "no such test: $name" >&2; fail=1; fi
  done
fi
exit $fail
