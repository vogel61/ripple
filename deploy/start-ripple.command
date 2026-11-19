#!/bin/zsh
# RIPPLE — start the proxy.
#
# Double-click this file. It checks the environment and then replaces this
# shell with the proxy process: closing the window (or Ctrl-C) stops RIPPLE.
# Every error keeps the window open so the message can be read.

emulate -L zsh
setopt extended_glob
set -u

PORT=8010
KEY_FILE="$HOME/.omlx/api_key.txt"

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

print "RIPPLE — project directory: $PROJECT_DIR"

# --- the Python environment must exist and be usable ------------------------
if [[ ! -x ./.venv/bin/python ]]; then
  fail "No Python environment in .venv. See deploy/README.md (installation)."
fi
if ! ./.venv/bin/python -c "import ripple" >/dev/null 2>&1; then
  fail "The Python environment is incomplete. Run: ./.venv/bin/pip install -r requirements.txt"
fi

# --- the configuration must exist -------------------------------------------
if [[ ! -f config.yaml ]]; then
  fail "config.yaml is missing. Copy config.example.yaml to config.yaml and adapt it."
fi

# --- API key: from the environment, otherwise from the oMLX key file --------
if [[ -z "${OMLX_API_KEY:-}" ]]; then
  if [[ ! -r "$KEY_FILE" ]]; then
    fail "No API key found. Expected the key in $KEY_FILE."
  fi
  OMLX_API_KEY="$(tr -d '\r\n' < "$KEY_FILE")"
  if [[ -z "$OMLX_API_KEY" ]]; then
    fail "The API key file $KEY_FILE is empty."
  fi
  export OMLX_API_KEY
  print "API key: read from $KEY_FILE"
else
  print "API key: taken from the environment"
fi

# --- only one instance may own the port -------------------------------------
BUSY_PID="$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null | head -n 1)"
if [[ -n "$BUSY_PID" ]]; then
  BUSY_CMD="$(ps -p "$BUSY_PID" -o command= 2>/dev/null)"
  print ""
  print "  Port $PORT is already in use by PID $BUSY_PID:"
  print "  $BUSY_CMD"
  fail "Stop that process first (deploy/stop-ripple.command), then start again."
fi

# --- run the proxy (exec: this shell becomes the proxy process) -------------
print "Listening on 0.0.0.0:$PORT — log: $HOME/Library/Logs/ripple.log"
print "Stop: Ctrl-C in this window, or double-click deploy/stop-ripple.command"
print ""
exec ./.venv/bin/python -m ripple --config config.yaml
