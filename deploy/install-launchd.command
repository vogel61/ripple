#!/bin/bash
# RIPPLE — install the launchd LaunchAgent. Double-click this file.
#
# Does the whole sequence in one go, because copy-pasting it broke once: shell
# comments at the end of a pasted line are read as arguments by zsh
# (`cp: 2.: Not a directory`). No arguments, no editing, one window.
#
#   ./deploy/install-launchd.command --dry-run   # only say what it would do
#
# Only on macOS, but not only on the operator's machine: the plist is a template
# and this script fills in the repository path and $HOME while installing.

set -u
cd "$(dirname "$0")/.." || exit 1

PLIST_SRC="deploy/ai.ripple.plist"
PLIST_DST="$HOME/Library/LaunchAgents/ai.ripple.plist"
KEY_FILE="$HOME/.omlx/api_key.txt"
LABEL="ai.ripple"
PORT="${RIPPLE_PORT:-8010}"
DOMAIN="gui/$(id -u)"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
FAIL=0

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=1; }
plan() { printf '  would %s\n' "$*"; }

say ""
say "RIPPLE — install the launchd agent (label $LABEL, port $PORT)"
say "──────────────────────────────────────────────────────────────"
[ "$DRY" = "1" ] && say "DRY RUN — nothing is changed."
say "repo:  $(pwd)"

# --- 1 -------------------------------------------------------------------
step "1/5  free port $PORT"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    if [ "$DRY" = "1" ]; then
        plan "stop the instance holding port $PORT (deploy/stop-ripple.command)"
    else
        deploy/stop-ripple.command
        if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            bad "port $PORT is still held — stop it by hand and try again"
        else
            ok "port $PORT is free"
        fi
    fi
else
    ok "port $PORT is free already"
fi

# --- 2 -------------------------------------------------------------------
step "2/5  install the plist"
if [ ! -f "$PLIST_SRC" ]; then
    bad "$PLIST_SRC is missing — wrong directory?"
else
    if [ "$DRY" = "1" ]; then
        plan "render $PLIST_SRC -> $PLIST_DST (repo $(pwd), home $HOME)"
    else
        mkdir -p "$HOME/Library/LaunchAgents"
        if sed -e "s|__REPO__|$(pwd)|g" -e "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"; then
            chmod 600 "$PLIST_DST"
            if grep -q '__REPO__\|__HOME__' "$PLIST_DST"; then
                bad "$PLIST_DST still contains a placeholder"
            else
                ok "rendered to $PLIST_DST (repo $(pwd))"
            fi
        else
            bad "could not write $PLIST_DST"
        fi
    fi
fi

# --- 3 -------------------------------------------------------------------
step "3/5  write the API key into the plist"
if [ ! -r "$KEY_FILE" ]; then
    bad "$KEY_FILE is not readable — the agent cannot start without the key"
elif [ ! -f "$PLIST_DST" ] && [ "$DRY" = "0" ]; then
    bad "no plist to write into"
elif [ "$DRY" = "1" ]; then
    plan "set EnvironmentVariables.OMLX_API_KEY from $KEY_FILE and chmod 600"
else
    if plutil -replace EnvironmentVariables.OMLX_API_KEY -string "$(cat "$KEY_FILE")" "$PLIST_DST"; then
        chmod 600 "$PLIST_DST"
        ok "key written, file is 0600"
    else
        bad "plutil could not write the key"
    fi
fi

if [ "$DRY" = "0" ] && [ -f "$PLIST_DST" ]; then
    plutil -lint "$PLIST_DST" >/dev/null 2>&1 && ok "plist is valid" || bad "plist does not lint"
    if [ "$(plutil -extract EnvironmentVariables.OMLX_API_KEY raw "$PLIST_DST" 2>/dev/null)" = "PUT_YOUR_OMLX_API_KEY_HERE" ]; then
        bad "the key is still the placeholder"
    fi
fi

# --- 4 -------------------------------------------------------------------
step "4/5  load the agent"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    if [ "$DRY" = "1" ]; then
        plan "unload the agent that is already loaded, then load the new one"
    else
        launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
        ok "old agent unloaded"
    fi
fi
if [ "$DRY" = "1" ]; then
    plan "launchctl bootstrap $DOMAIN $PLIST_DST"
else
    if launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>/dev/null; then
        ok "agent loaded"
    else
        bad "launchctl bootstrap failed — run: launchctl print $DOMAIN/$LABEL"
    fi
fi

# --- 5 -------------------------------------------------------------------
step "5/5  verify"
if [ "$DRY" = "1" ]; then
    plan "wait for the port, then check /healthz"
else
    for _ in $(seq 1 20); do
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && break
        sleep 0.5
    done
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid) =" | sed 's/^/  /'
    HEALTH=$(curl -s --max-time 10 "http://127.0.0.1:$PORT/healthz")
    if [ -n "$HEALTH" ]; then
        printf '%s' "$HEALTH" | python3 -c '
import json, sys
d = json.load(sys.stdin)
hosts = d.get("hosts", [])
print("  ok    /healthz: " + str(sum(1 for h in hosts if h.get("status") == "healthy"))
      + "/" + str(len(hosts)) + " hosts healthy, guard "
      + str(d.get("capabilities", {}).get("eviction_guard")))
w = d.get("warnings") or []
print("  ok    no warnings" if not w else "  FAIL  warnings: " + "; ".join(w))
' || bad "/healthz did not parse"
    else
        bad "no answer on /healthz yet — check ~/Library/Logs/ripple.log"
    fi
fi

say ""
say "──────────────────────────────────────────────────────────────"
if [ "$DRY" = "1" ]; then
    say "Result: dry run — nothing was changed."
elif [ "$FAIL" = "0" ]; then
    say "Result: the agent is installed. It starts at login and restarts after a crash."
    say "Check anytime with: deploy/verify-ripple.command (double-click)"
else
    say "Result: FAILED — see the lines marked FAIL above."
    say "Log: ~/Library/Logs/ripple.log"
fi
say ""
if [ -t 0 ]; then
    read -r -n 1 -s -p "Press any key to close …"; say ""
fi
exit "$FAIL"
