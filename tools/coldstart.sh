#!/bin/bash
# RIPPLE — the cold start, as a script (issue #64).
#
# Plays a stranger: a fresh clone into an empty directory, an empty HOME, a fresh
# virtualenv, `ripple init --yes`, `ripple doctor`, one real completion through the
# proxy and a dashboard fetch. Every step prints its output; the first red step
# stops the run. Nothing outside $COLDSTART_ROOT and $COLDSTART_HOME is touched,
# and neither is the operator's own RIPPLE (the port is checked first and the
# script refuses a port that is already serving).
#
#   tools/coldstart.sh                        # clone from the repo, port 8010
#   tools/coldstart.sh --bundle /tmp/ripple.bundle
#   tools/coldstart.sh --port 8099 --keep
#   tools/coldstart.sh --no-key               # the stranger without an oMLX key
#   tools/coldstart.sh --uv                   # use uv instead of python -m venv
#   tools/coldstart.sh --key-file /tmp/k.txt  # the oMLX key, if not in ~/.omlx
#   tools/coldstart.sh --model Qwen3.5-9B     # complete against a named model
#
# Exit code 0 only when every step passed. The transcript is kept at
# $COLDSTART_ROOT/coldstart.log and printed on failure.

set -uo pipefail

STAMP="$(date +%Y%m%d-%H%M%S)"
ROOT="${COLDSTART_ROOT:-/tmp/coldstart-$STAMP}"
# A cold start needs an empty home. One per run, below $ROOT, so two runs can
# never inherit each other's config.yaml — that is what makes the second run a
# test of the first one's leftovers instead of a cold start.
HOME_DIR="${COLDSTART_HOME:-}"
PORT="${PORT:-8010}"
OMLX_URL="${OMLX_URL:-http://127.0.0.1:8000}"
SOURCE=""
BUNDLE=""
KEEP=0
USE_UV=0
WITH_KEY=1
# The key file is the one thing the script reads outside its own root, so it is
# a flag: on a machine that is not the operator's, the key is copied somewhere
# else (mode 0600) instead of being put into the real ~/.omlx.
KEY_FILE=""
# Which model to complete against. Empty means "the first one the proxy offers",
# which is fine on a machine nobody else uses and wrong on a node that serves
# real traffic: there the first id can be a 453 GB model, and asking for it
# loads it (measured on the M3, issue #75).
MODEL_WANT=""
REPO="$(cd "$(dirname "$0")/.." && pwd)"

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --bundle)  BUNDLE="${2:-}"; shift 2 ;;
        --repo)    REPO="${2:-}"; shift 2 ;;
        --root)    ROOT="${2:-}"; shift 2 ;;
        --home)    HOME_DIR="${2:-}"; shift 2 ;;
        --port)    PORT="${2:-}"; shift 2 ;;
        --omlx)    OMLX_URL="${2:-}"; shift 2 ;;
        --key-file) KEY_FILE="${2:-}"; shift 2 ;;
        --model)   MODEL_WANT="${2:-}"; shift 2 ;;
        --keep)    KEEP=1; shift ;;
        --uv)      USE_UV=1; shift ;;
        --no-key)  WITH_KEY=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$HOME_DIR" ] || HOME_DIR="$ROOT/home"

[ -n "$KEY_FILE" ] || KEY_FILE="$HOME/.omlx/api_key.txt"

LOG="$ROOT/coldstart.log"
STEP=0
FAILED=0
SERVER_PID=""

say()  { printf '%s\n' "$*" | tee -a "$LOG" >&2; }
step() { STEP=$((STEP + 1)); printf '\n\033[1m%d. %s\033[0m\n' "$STEP" "$*" | tee -a "$LOG" >&2; }
ok()   { printf '   \033[32mok\033[0m    %s\n' "$*" | tee -a "$LOG" >&2; }
bad()  { printf '   \033[31mFAIL\033[0m  %s\n' "$*" | tee -a "$LOG" >&2; FAILED=1; }
note() { printf '   \033[33mnote\033[0m  %s\n' "$*" | tee -a "$LOG" >&2; }

# Run a command, tee its output into the log, and fail the step on a non-zero exit.
run() {
    local label="$1"; shift
    local out
    out="$("$@" 2>&1)"
    local rc=$?
    printf '%s\n' "$out" | sed 's/^/   | /' | tee -a "$LOG" >&2
    if [ $rc -ne 0 ]; then
        bad "$label (exit $rc)"
        return $rc
    fi
    return 0
}

cleanup() {
    local rc=$?
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
    if [ "$KEEP" = "0" ] && [ "$rc" -ne 0 ]; then
        printf '\n   transcript kept: %s\n' "$LOG" >&2
    fi
}
trap cleanup EXIT

mkdir -p "$ROOT" 2>/dev/null || { echo "cannot create $ROOT" >&2; exit 2; }
: > "$LOG"
say "RIPPLE cold start — $STAMP"
say "root:  $ROOT"
say "home:  $HOME_DIR"
say "port:  $PORT   node: $OMLX_URL   key: $([ "$WITH_KEY" = 1 ] && echo yes || echo no)"

# --- 1 -----------------------------------------------------------------------
step "preconditions"
MISSING=0
for tool in git python3 curl; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool $( ("$tool" --version 2>&1 || true) | head -n 1)"
    else
        bad "$tool is missing"
        MISSING=1
    fi
done
[ "$MISSING" = "0" ] || { say "cannot continue without them"; exit 1; }
PYV="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "python3 is $PYV"
if [ "$USE_UV" = "1" ] && ! command -v uv >/dev/null 2>&1; then
    bad "--uv was asked for but uv is not installed (curl -LsSf https://astral.sh/uv/install.sh | sh)"
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    HOLDER="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | awk 'NR==2 {print $1" (pid "$2")"}')"
    bad "port $PORT is already in use by $HOLDER — pick another with --port"
else
    ok "port $PORT is free"
fi
if [ -e "$HOME_DIR" ] && [ "$(ls -A "$HOME_DIR" 2>/dev/null)" != "" ]; then
    bad "$HOME_DIR is not empty — that is not a cold start (remove it, or pass another --home)"
fi
[ "$FAILED" = "0" ] || { say "stopping at the first red step"; exit 1; }

# --- 2 -----------------------------------------------------------------------
step "fresh clone (no .venv, no config.yaml, no working copy)"
if [ -n "$BUNDLE" ]; then
    SOURCE="$BUNDLE"
else
    SOURCE="$REPO"
fi
if ! run "clone from $SOURCE" git clone -q "$SOURCE" "$ROOT/repo"; then
    exit 1
fi
ok "cloned to $ROOT/repo"
[ -f "$ROOT/repo/pyproject.toml" ] && ok "pyproject.toml is there" || bad "no pyproject.toml — pip install . will refuse"
[ -f "$ROOT/repo/config.yaml" ] && bad "the clone carries a config.yaml — not a cold start" || ok "no config.yaml in the clone"
[ -d "$ROOT/repo/.venv" ] && bad "the clone carries a .venv" || ok "no .venv in the clone"
[ "$FAILED" = "0" ] || exit 1

# --- 3 -----------------------------------------------------------------------
step "install into a fresh environment"
if [ "$USE_UV" = "1" ]; then
    run "uv venv" uv venv --quiet "$ROOT/venv" || exit 1
    run "uv pip install ." env -u VIRTUAL_ENV uv pip install --quiet \
        --python "$ROOT/venv/bin/python" "$ROOT/repo" || exit 1
else
    # Deliberately the system python3: on the reference machine that is 3.9.6,
    # with no pipx, no brew and no uv. If this works, a stranger needs nothing.
    run "python3 -m venv" python3 -m venv "$ROOT/venv" || exit 1
    run "pip install ." "$ROOT/venv/bin/python" -m pip install --quiet \
        --disable-pip-version-check "$ROOT/repo" || exit 1
fi
if [ -x "$ROOT/venv/bin/ripple" ]; then
    ok "console script installed: $("$ROOT/venv/bin/ripple" --version 2>&1)"
else
    bad "no ripple console script in $ROOT/venv/bin"
    exit 1
fi
CATALOGS="$(ls "$ROOT/venv"/lib/python*/site-packages/ripple/static/i18n/*.json 2>/dev/null | wc -l | tr -d ' ')"
if [ "$CATALOGS" = "16" ]; then
    ok "16 i18n catalogs in site-packages"
else
    bad "$CATALOGS i18n catalogs in site-packages, expected 16 — the dashboard would start without translations"
    exit 1
fi

# --- 4 -----------------------------------------------------------------------
step "ripple init --yes (empty HOME, nothing to overwrite)"
mkdir -p "$HOME_DIR"
INIT_ARGS=(init --yes --port "$PORT" --host "$OMLX_URL"
           --config "$HOME_DIR/config.yaml" --home-dir "$HOME_DIR")
if [ "$WITH_KEY" = "1" ] && [ -r "$KEY_FILE" ]; then
    # The key travels through the environment of the child only; it is never
    # echoed and never written to the transcript.
    export OMLX_API_KEY="$(tr -d '\r\n' < "$KEY_FILE")"
    ok "oMLX key taken from $KEY_FILE (not printed)"
else
    note "running without an oMLX key: read-only node, doctor must warn instead of fail"
fi
if ! run "ripple init" env -u PYTHONPATH HOME="$HOME_DIR" "$ROOT/venv/bin/ripple" "${INIT_ARGS[@]}"; then
    exit 1
fi
[ -f "$HOME_DIR/config.yaml" ] && ok "config.yaml written" || { bad "no config.yaml"; exit 1; }
[ -f "$HOME_DIR/.ripple/keys.yaml" ] && ok "keys.yaml written" || { bad "no keys.yaml"; exit 1; }
for f in "$HOME_DIR/config.yaml" "$HOME_DIR/.ripple/keys.yaml"; do
    MODE="$(stat -f '%OLp' "$f" 2>/dev/null || stat -c '%a' "$f" 2>/dev/null)"
    [ "$MODE" = "600" ] && ok "$(basename "$f") is 0600" || bad "$f has mode $MODE, expected 600"
done
[ "$FAILED" = "0" ] || exit 1

# --- 5 -----------------------------------------------------------------------
step "ripple doctor (nothing is serving yet)"
run "ripple doctor" env -u PYTHONPATH HOME="$HOME_DIR" "$ROOT/venv/bin/ripple" doctor \
    --config "$HOME_DIR/config.yaml" --url "http://127.0.0.1:$PORT" || {
    note "doctor may report the missing proxy here — that is the next step"
}

# --- 6 -----------------------------------------------------------------------
step "start the proxy from the cold-start config"
env -u PYTHONPATH HOME="$HOME_DIR" "$ROOT/venv/bin/ripple" \
    --config "$HOME_DIR/config.yaml" >> "$LOG" 2>&1 &
SERVER_PID=$!
READY=0
for _ in $(seq 1 40); do
    if curl -sf --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
        READY=1
        break
    fi
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.5
done
if [ "$READY" = "1" ]; then
    ok "serving on 127.0.0.1:$PORT (pid $SERVER_PID)"
else
    bad "the proxy did not answer on /healthz"
    tail -n 20 "$LOG" | sed 's/^/   | /' >&2
    exit 1
fi

# --- 7 -----------------------------------------------------------------------
step "one real completion through the proxy"
# The client key first: the generated config has `allow_loopback: false`, so even
# 127.0.0.1 needs it — measured, the first version of this script asked for
# /v1/models without one and got `auth_denied ... presented=no`.
CLIENT_KEY=""
if [ -r "$HOME_DIR/.ripple/keys.yaml" ]; then
    CLIENT_KEY="$("$ROOT/venv/bin/python" - "$HOME_DIR/.ripple/keys.yaml" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1])) or {}
keys = data.get("keys") or []
if isinstance(keys, dict):
    keys = list(keys.values())
for k in keys:
    if isinstance(k, dict) and k.get("key"):
        print(k["key"]); break
PY
)"
fi
[ -n "$CLIENT_KEY" ] && ok "client key read from the generated keys.yaml (not printed)" \
    || bad "no client key in $HOME_DIR/.ripple/keys.yaml"

# The fleet resolves the node after start-up, so the model list appears a moment
# after /healthz answers. Polling beats a fixed sleep: it is a measurement.
MODEL=""
for _ in $(seq 1 30); do
    MODEL="$(curl -s --max-time 5 \
        ${CLIENT_KEY:+-H "Authorization: Bearer $CLIENT_KEY"} \
        "http://127.0.0.1:$PORT/v1/models" \
        | "$ROOT/venv/bin/python" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    ids = [m.get("id") for m in d.get("data", []) if m.get("id")]
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    if want:
        print(want if want in ids else "")
    else:
        print(ids[0] if ids else "")
except Exception:
    print("")' "$MODEL_WANT")"
    [ -n "$MODEL" ] && break
    sleep 0.5
done
if [ -z "$MODEL" ] && [ "$WITH_KEY" = "0" ]; then
    # Measured on the M3: oMLX answers 401 on /v1/models without a key, so a
    # read-limited node offers nothing and there is no model to complete with.
    # That is exactly the path a stranger without a key walks — a note, not red.
    note "no oMLX key: /v1/models answers 401, no model offered, completion skipped (read-only path)"
elif [ -z "$MODEL" ] && [ -n "$MODEL_WANT" ]; then
    bad "the proxy does not offer $MODEL_WANT — compare --model with /v1/models"
elif [ -z "$MODEL" ]; then
    bad "the proxy offers no model after 15 s — the node answered nothing (key missing?)"
elif [ "$WITH_KEY" = "0" ]; then
    note "no oMLX key: a completion cannot work, skipping (this is the read-only path)"
else
    ok "model offered through the proxy: $MODEL"
    # max_tokens must leave room for the reasoning a thinking model spends before
    # it writes a single word: with 16 the whole budget went into
    # reasoning_content, `content` came back empty and the step read as red on a
    # machine where the same model simply thought longer (measured on the M3).
    BODY="$("$ROOT/venv/bin/python" -c 'import json,sys
print(json.dumps({"model": sys.argv[1], "messages": [{"role": "user", "content": "Reply with the single word: coldstart"}], "max_tokens": 128, "stream": False}))' "$MODEL")"
    START="$(date +%s)"
    RESP="$(curl -s --max-time 120 -H "Content-Type: application/json" \
        -H "Authorization: Bearer $CLIENT_KEY" \
        -d "$BODY" "http://127.0.0.1:$PORT/v1/chat/completions")"
    END="$(date +%s)"
    printf '%s\n' "$RESP" | sed 's/^/   | /' | tee -a "$LOG" >&2
    # A reasoning model may answer with reasoning_content only; that is a
    # completion too, so it counts — but the line says which one it was.
    PARSED="$(printf '%s' "$RESP" | "$ROOT/venv/bin/python" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    msg = (d.get("choices") or [{}])[0].get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    if content:
        print("content")
        print(content)
    elif reasoning:
        print("reasoning")
        print(reasoning)
except Exception:
    pass')"
    KIND="${PARSED%%$'\n'*}"
    SAID=""
    case "$PARSED" in
        *$'\n'*) SAID="${PARSED#*$'\n'}" ;;
    esac
    if [ -z "$SAID" ]; then
        bad "no completion text came back"
    elif [ "$KIND" = "content" ]; then
        ok "completion in $((END - START)) s: \"$SAID\""
    else
        ok "completion in $((END - START)) s: reasoning only, no content: \"$SAID\""
    fi
fi

# --- 8 -----------------------------------------------------------------------
step "ripple doctor against the running proxy"
# Step 5 runs before the proxy exists, so its `dashboard` and `completion` checks
# are always SKIP — the protocol never proved them. This second run is the one
# that does, and it is where doctor's answer becomes green in the true sense
# (issue #75).
run "ripple doctor" env -u PYTHONPATH HOME="$HOME_DIR" "$ROOT/venv/bin/ripple" doctor \
    --config "$HOME_DIR/config.yaml" --url "http://127.0.0.1:$PORT" || exit 1

# --- 9 -----------------------------------------------------------------------
step "dashboard"
HTML="$(curl -s --max-time 10 "http://127.0.0.1:$PORT/")"
if printf '%s' "$HTML" | grep -qi "ripple"; then
    ok "dashboard page served ($(printf '%s' "$HTML" | wc -c | tr -d ' ') bytes)"
else
    bad "the dashboard page did not come back"
fi
for path in /healthz /stats /config; do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT$path")"
    [ "$CODE" = "200" ] && ok "$path → 200" || bad "$path → $CODE"
done
CAT="$(curl -s --max-time 10 "http://127.0.0.1:$PORT/static/i18n/de.json" | head -c 1)"
[ "$CAT" = "{" ] && ok "an i18n catalog is served from the package" || bad "no catalog served at /static/i18n/de.json"

# --- 10 ----------------------------------------------------------------------
step "stop and report"
kill "$SERVER_PID" 2>/dev/null
wait "$SERVER_PID" 2>/dev/null
SERVER_PID=""
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    bad "something still listens on $PORT"
else
    ok "port $PORT is free again"
fi

say ""
if [ "$FAILED" = "0" ]; then
    say "Result: the cold start ran through — $STEP steps, no red step."
    if [ "$KEEP" = "0" ]; then
        rm -rf "$ROOT/repo" "$ROOT/venv"
        say "Kept: $LOG (clone and venv removed; --keep keeps them)"
    fi
    exit 0
else
    say "Result: FAILED — see the FAIL lines above."
    say "Transcript: $LOG"
    exit 1
fi
