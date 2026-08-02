#!/usr/bin/env bash
# Ship an update WITHOUT destroying work in flight.
#
#   ./ship.sh                 drain → wait for running work → stop → migrate → start
#   ./ship.sh --force         kill runs that will not finish. The turn they are in is LOST.
#   ./ship.sh --timeout 600   how long to wait before giving up (default 1800s)
#
# The old way to ship was to kill the server and let recovery pick up the pieces. Recovery
# works — it re-enters the SDK session and re-reads the disk — but it is a seatbelt, not a
# shipping strategy: the turn a run was IN when you killed it is gone, along with whatever
# the worker had not yet written. That was the cost of every single update.
#
# This waits instead. Slower, and nothing is lost.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/run.sh" ship "$@"
