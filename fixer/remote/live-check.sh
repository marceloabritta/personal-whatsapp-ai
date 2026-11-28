#!/usr/bin/env bash
# Runs ON THE DROPLET, fed over ssh by fixer/run.py:  bash -s -- <dir> <container> <image> <test>
# One live check of Lisa against the real model, with the tree in <dir> mounted over the image
# that is running. The keys come from the running container, then from tests.env, which swaps in
# the development key: every live_*.py refuses to run without it (tests/live_key.py).
set -euo pipefail
dir="$1"; container="$2"; image="$3"; test="$4"
case "$test" in live_*.py) ;; *) echo "not a live check: $test" >&2; exit 2 ;; esac
[ -f "$dir/Lisa flow/tests/$test" ] || { echo "no such check: $test" >&2; exit 2; }
[ -f /opt/evolution/tests.env ] || { echo "/opt/evolution/tests.env is missing" >&2; exit 2; }
net=$(docker inspect "$container" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}')
log=$(mktemp)
set +e
docker run --rm --network "$net" \
  --env-file <(docker exec "$container" env | grep -v -E '^(PATH|HOSTNAME|HOME|PWD|TERM)=') \
  --env-file /opt/evolution/tests.env \
  -v "$dir/Lisa flow/app:/app/app:ro" -v "$dir/Lisa flow/tests:/app/tests:ro" \
  -w /app "$image" python "tests/$test" >"$log" 2>&1
rc=$?
set -e
# The traces flood the output; only the verdict lines and the tail are worth sending back.
grep -E '^\s*\[(PASS|FAIL)\]' "$log" || true
tail -n 3 "$log" >&2
rm -f "$log"
exit "$rc"
