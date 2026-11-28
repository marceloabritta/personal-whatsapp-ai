#!/usr/bin/env bash
# Runs ON THE DROPLET, fed over ssh by fixer/run.py:
#
#   bash -s -- <sha> <tag> <services> <prompt_version|-> <judge_version|-> [drill-rollback]
#
# Puts main at <sha> into production, the way every deploy of Lisa has been done by hand:
# pull the checkout, tag the running image for rollback, build, try the new image, write the
# version lines into the live override (with a backup), swap the container, and check that it
# booted. If it did not boot, it puts the old image and the old override back and says so.
#
#   <services>   "lisa", "dispatcher", "lisa,dispatcher", or "-" to only pull the checkout
#   <tag>        the rollback tag: the running image is kept as evolution-<service>:<tag>
#   versions     PROMPT_VERSION (REVIEW_SCOPE_VERSION follows it) and REVIEW_JUDGE_VERSION to set
#                in the override, or "-" to leave each alone
#   drill-rollback   swap, then roll back as if the boot had failed: a rehearsal of that path
#
# One JSON object on stdout says how it went. Exit 0: live. Exit 3: rolled back. Anything else:
# stopped before the swap, production untouched.
set -uo pipefail
sha="$1"; tag="$2"; services="$3"; pv="${4:--}"; jv="${5:--}"; drill="${6:-}"

REPO=/opt/personal-whatsapp-ai
STACK=/opt/evolution
OVERRIDE="$STACK/docker-compose.override.yml"
declare -A CONTAINER=([lisa]=lisa_brain [dispatcher]=flow_dispatcher)
declare -A OLD NEW
backup=""
t0=""; t1=""

say() { printf '%s\n' "$*" >&2; }
answer() {  # key=value pairs → one JSON line
  python3 -c 'import json,sys; print(json.dumps(dict(a.split("=",1) for a in sys.argv[1:])))' "$@"
}
stop() {  # before the swap: nothing in production changed
  [ -n "$backup" ] && cp "$backup" "$OVERRIDE"
  answer ok=false step="$1" detail="$2" rolled_back=false
  exit 2
}

[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || stop "the deploy" "not a commit id: $sha"
[[ "$tag" =~ ^[a-z0-9][a-z0-9.-]*$ ]] || stop "the deploy" "not a tag: $tag"

# One deploy at a time, whoever starts it.
exec 9>"$STACK/.deploy.lock"
flock -w 600 9 || stop "the deploy" "another deploy held the lock for ten minutes"

cd "$REPO" || stop "the deploy" "no checkout at $REPO"
git pull -q --ff-only 2>/dev/null || stop "the deploy" "the server's checkout could not be fast-forwarded"
have=$(git rev-parse HEAD)
[ "$have" = "$sha" ] || stop "the deploy" "the server's checkout is at ${have:0:7}, not ${sha:0:7}: main moved"
if [ "$services" = "-" ]; then
  answer ok=true pulled="${sha:0:7}"
  exit 0
fi

cd "$STACK" || stop "the deploy" "no stack at $STACK"
list=${services//,/ }
for s in $list; do
  [ -n "${CONTAINER[$s]:-}" ] || stop "the deploy" "unknown service: $s"
  OLD[$s]=$(docker inspect -f '{{.Image}}' "${CONTAINER[$s]}" 2>/dev/null) || stop "the deploy" "$s is not running"
  docker tag "${OLD[$s]}" "evolution-$s:$tag" || stop "the deploy" "could not tag $s for rollback"
done

say "building: $list"
# shellcheck disable=SC2086
docker compose build -q $list >&2 2>&1 || stop "the build" "the image did not build"
for s in $list; do NEW[$s]=$(docker images -q "evolution-$s:latest" | head -1); done

# Try the new image before it carries a single message. The Mac has already run every offline
# test on this tree; this proves the IMAGE holds that tree and starts.
for s in $list; do
  if [ "$s" = "lisa" ]; then
    docker run --rm --network none -e ANTHROPIC_API_KEY=x -w /app evolution-lisa:latest \
      python -c "import app.main" >/dev/null 2>&1 || stop "the image check" "Lisa's new image does not import"
    for t in run_step3.py run_window.py run_fixdesk.py; do
      docker run --rm --network none -e ANTHROPIC_API_KEY=x \
        -v "$REPO/Lisa flow/tests:/app/tests:ro" -w /app evolution-lisa:latest python "tests/$t" >/dev/null 2>&1 \
        || stop "the image check" "$t fails inside Lisa's new image"
    done
  else
    for t in "$REPO"/dispatcher/tests/run_*.py; do
      # run_forward_retry has one timing check that can miss in a cold container: twice.
      ok=0
      for _ in 1 2; do
        docker run --rm --network none -v "$REPO/dispatcher/tests:/app/tests:ro" evolution-dispatcher:latest \
          python "tests/$(basename "$t")" >/dev/null 2>&1 && { ok=1; break; }
      done
      [ $ok = 1 ] || stop "the image check" "$(basename "$t") fails inside the dispatcher's new image"
    done
  fi
done

# The version lines live in the override, not in the code. Only Lisa's service may change.
if [ "$pv" != "-" ] || [ "$jv" != "-" ]; then
  backup="$OVERRIDE.bak.$(date -u +%Y%m%d-%H%M%S)"
  cp "$OVERRIDE" "$backup"
  python3 - "$OVERRIDE" "$pv" "$jv" <<'PY' || stop "the versions" "the override has no line to set (PROMPT_VERSION / REVIEW_SCOPE_VERSION / REVIEW_JUDGE_VERSION)"
import re, sys
path, pv, jv = sys.argv[1:4]
text = open(path).read()
def put(text, key, value):
    new, n = re.subn(rf'^(\s+{key}:\s*).*$', lambda m: f'{m.group(1)}"{value}"', text, flags=re.M)
    if n != 1:
        sys.exit(1)
    return new
if pv != "-":
    text = put(put(text, "PROMPT_VERSION", pv), "REVIEW_SCOPE_VERSION", pv)
if jv != "-":
    text = put(text, "REVIEW_JUDGE_VERSION", jv)
open(path, "w").write(text)
PY
  changed=$(python3 - "$backup" "$OVERRIDE" <<'PY'
import hashlib, json, subprocess, sys
def services(path):
    out = subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "-f", path, "config", "--format", "json"],
                         capture_output=True, text=True, check=True).stdout
    return {k: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest() for k, v in json.loads(out)["services"].items()}
old, new = services(sys.argv[1]), services(sys.argv[2])
print(",".join(sorted(k for k in new if old.get(k) != new[k])))
PY
) || stop "the versions" "the edited override does not parse"
  [ "$changed" = "lisa" ] || [ -z "$changed" ] || stop "the versions" "the override edit would change more than Lisa: $changed"
fi

booted() {  # service → 0 when it is up on the image it should be on
  local s="$1" want="$2" c="${CONTAINER[$1]}" i
  for i in $(seq 1 75); do
    if [ "$s" = "lisa" ]; then
      docker logs "$c" 2>&1 | grep -q "Application startup complete" && break
    else
      docker exec "$c" python -c "import httpx; httpx.get('http://localhost:8090/').raise_for_status()" >/dev/null 2>&1 && break
    fi
    sleep 2
  done
  [ "$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null)" = "$want" ] || return 1
  if [ "$s" = "lisa" ]; then
    docker exec "$c" python -c "import httpx; r = httpx.get('http://localhost:8000/'); assert r.json()['ok']" >/dev/null 2>&1
  else
    docker exec "$c" python -c "import httpx; assert httpx.get('http://localhost:8090/').json()['ok']" >/dev/null 2>&1
  fi
}

t0=$(date -u +%H:%M:%S)
failed=""
for s in $list; do
  want=$(docker inspect -f '{{.Id}}' "evolution-$s:latest")
  say "swapping $s"
  docker compose up -d --no-deps -t 120 "$s" >&2 2>&1 || failed="$s did not start"
  [ -z "$failed" ] && { booted "$s" "$want" || failed="$s did not come up on the new image"; }
  [ -n "$failed" ] && break
done
if [ -z "$failed" ] && [ -n "$pv$jv" ] && [[ " $list " == *" lisa "* ]]; then
  [ "$pv" = "-" ] || [ "$(docker exec lisa_brain printenv PROMPT_VERSION)" = "$pv" ] || failed="Lisa is not on prompt version $pv"
  [ "$jv" = "-" ] || [ "$(docker exec lisa_brain printenv REVIEW_JUDGE_VERSION)" = "$jv" ] || failed="Lisa is not on judge version $jv"
fi
[ -z "$failed" ] && [ "$drill" = "drill-rollback" ] && failed="rollback drill"

if [ -n "$failed" ]; then
  say "rolling back: $failed"
  [ -n "$backup" ] && cp "$backup" "$OVERRIDE"
  back="true"
  for s in $list; do
    docker tag "${OLD[$s]}" "evolution-$s:latest"
    docker compose up -d --no-deps -t 120 "$s" >&2 2>&1 || back="false"
    booted "$s" "${OLD[$s]}" || back="false"
  done
  t1=$(date -u +%H:%M:%S)
  answer ok=false step="the deploy" detail="$failed" rolled_back="$back" swap="$t0-$t1 UTC"
  exit 3
fi

t1=$(date -u +%H:%M:%S)
images=""
for s in $list; do images+="$s=${NEW[$s]} "; done
answer ok=true images="${images% }" rollback_tag="$tag" swap="$t0-$t1 UTC" override_backup="$(basename "${backup:-none}")"
