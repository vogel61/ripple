#!/bin/zsh
# RIPPLE — stop the running proxy.
#
# Double-click this file. It is a thin wrapper around `ripple shutdown` (#121):
# the double-click and the command line must not disagree about what stopping
# means, so there is one implementation and this is the window around it.
#
# That command boots the launchd agent out of launchd when it is loaded —
# otherwise launchd would start RIPPLE again immediately — and otherwise sends
# SIGTERM to the process that holds the port, waits for the port to be free and
# reports which of the two it did. Instances on another port are left alone: the
# command only ever acts on the port it resolved (RIPPLE_PORT, else
# listen_port from config.yaml). A process on the port that is not a RIPPLE
# instance is named and not touched.
#
# Every error keeps the window open so the message can be read.

emulate -L zsh
set -u

PORT="${RIPPLE_PORT:-8010}"
export RIPPLE_PORT="$PORT"

# --- show the message and wait for a key press ------------------------------
fail() {
  print ""
  print "  $1"
  print ""
  print "  Press any key to close this window."
  read -k 1 2>/dev/null
  exit 1
}

# --- work from the project directory (this script lives in deploy/) ---------
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
cd "$PROJECT_DIR" || fail "Cannot open the project directory: $PROJECT_DIR"

print "RIPPLE — stopping the instance on port $PORT ..."
print ""

if [[ ! -x ./.venv/bin/python ]]; then
  fail "No Python environment in .venv. See deploy/README.md (installation)."
fi

./.venv/bin/python -m ripple shutdown
RC=$?

print ""
if (( RC != 0 )); then
  print "  Stopping failed (exit $RC) — the message above names the cause."
  print "  Check with: ripple status"
fi
print "  Press any key to close this window."
read -k 1 2>/dev/null
exit "$RC"
