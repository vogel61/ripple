#!/bin/zsh
# RIPPLE — stop the running proxy.
#
# Double-click this file. The process is found with `ps -Ao pid=,command=`,
# not with `pkill -f` and path patterns: those silently missed the process in
# this environment. The script reports which PID it stopped.
#
# Instances listening on another port (e.g. a smoke test on 8011) are left
# alone. If the launchd agent ai.ripple is loaded, it is removed first —
# otherwise launchd would restart RIPPLE immediately.

emulate -L zsh
setopt extended_glob
set -u

PORT=8010
UID_NR="$(id -u)"

# --- show the message and wait for a key press ------------------------------
fail() {
  print ""
  print "  $1"
  print ""
  print "  Press any key to close this window."
  read -k 1 2>/dev/null
  exit 1
}

print "RIPPLE — looking for a running instance ..."

# --- launchd would restart the service: remove it there first ---------------
LAUNCHD_WAS_LOADED=0
if launchctl print "gui/$UID_NR/ai.ripple" >/dev/null 2>&1; then
  LAUNCHD_WAS_LOADED=1
  print "  launchd agent ai.ripple is loaded — removing it from launchd"
  launchctl bootout "gui/$UID_NR/ai.ripple" >/dev/null 2>&1
  sleep 1
fi

# --- collect matching PIDs --------------------------------------------------
typeset -a FOUND
typeset -a SKIPPED
FOUND=()
SKIPPED=()

while IFS= read -r line; do
  [[ -n "$line" ]] || continue
  line="${line##[[:space:]]#}"
  pid="${line%% *}"
  cmd="${line#* }"
  [[ "$pid" == <-> ]] || continue
  [[ "$cmd" == *"-m ripple"* ]] || continue
  [[ "$cmd" == *python* ]] || continue
  [[ "$cmd" == *"stop-ripple"* ]] && continue
  [[ "$cmd" == *"ps -Ao"* ]] && continue
  if [[ "$cmd" =~ '--port[= ]+([0-9]+)' ]]; then
    if [[ "$match[1]" != "$PORT" ]]; then
      SKIPPED+=("$pid $cmd")
      continue
    fi
  fi
  FOUND+=("$pid $cmd")
done < <(ps -Ao pid=,command=)

if (( ${#SKIPPED} > 0 )); then
  for entry in "${SKIPPED[@]}"; do
    print "  left untouched (other port): PID ${entry%% *}"
  done
fi

if (( ${#FOUND} == 0 )); then
  if (( LAUNCHD_WAS_LOADED )); then
    print "  Stopped: launchd agent ai.ripple removed, no process left."
  else
    print "  Nothing to stop — no RIPPLE process is running."
  fi
  BUSY_PID="$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null | head -n 1)"
  if [[ -n "$BUSY_PID" ]]; then
    print "  Port $PORT is in use by PID $BUSY_PID (not a RIPPLE process)."
  else
    print "  Port $PORT is free."
  fi
  exit 0
fi

# --- terminate and wait -----------------------------------------------------
for entry in "${FOUND[@]}"; do
  pid="${entry%% *}"
  cmd="${entry#* }"
  print "  stopping PID $pid"
  print "    $cmd"
  kill -TERM "$pid" 2>/dev/null
done

alive=0
for _ in {1..20}; do
  alive=0
  for entry in "${FOUND[@]}"; do
    pid="${entry%% *}"
    if kill -0 "$pid" 2>/dev/null; then
      alive=1
    fi
  done
  (( alive == 0 )) && break
  sleep 0.5
done

if (( alive )); then
  for entry in "${FOUND[@]}"; do
    pid="${entry%% *}"
    if kill -0 "$pid" 2>/dev/null; then
      print "  PID $pid did not stop on SIGTERM — sending SIGKILL"
      kill -KILL "$pid" 2>/dev/null
    fi
  done
  sleep 1
fi

# --- report the result ------------------------------------------------------
STILL=0
for entry in "${FOUND[@]}"; do
  pid="${entry%% *}"
  kill -0 "$pid" 2>/dev/null && STILL=1
done
if (( STILL )); then
  fail "Some RIPPLE processes are still running. Check with: ps -Ao pid=,command= | grep ripple"
fi

BUSY_PID="$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null | head -n 1)"
if [[ -n "$BUSY_PID" ]]; then
  fail "RIPPLE stopped, but port $PORT is still in use by PID $BUSY_PID."
fi

print ""
print "  Stopped ${#FOUND} RIPPLE process(es). Port $PORT is free."
exit 0
