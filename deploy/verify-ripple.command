#!/bin/bash
# RIPPLE — health check for a double-click.
#
# Checks a running instance end to end and prints one line per check. The window
# stays open at the end so the result can be read; exit code 0 means everything
# is fine, 1 means at least one check failed.
#
# Double-click this file, or run it from a terminal: ./deploy/verify-ripple.command

cd "$(dirname "$0")/.." || exit 1
PORT="${RIPPLE_PORT:-8010}"
BASE="http://127.0.0.1:${PORT}"
KEY_FILE="$HOME/.omlx/api_key.txt"
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=1; }
skip() { printf '  --    %s\n' "$*"; }

say ""
say "RIPPLE check — $BASE"
say "────────────────────────────────────────────────────────"

# 1. is anything listening?
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    ok "port $PORT is served (pid $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t | head -1))"
else
    bad "nothing is listening on port $PORT — start it with deploy/start-ripple.command"
    say ""
    say "Result: FAILED (not running)"
    read -r -n 1 -s -p "Press any key to close …"; say ""
    exit 1
fi

# 2. /healthz
HEALTH=$(curl -s --max-time 10 "$BASE/healthz")
if [ -z "$HEALTH" ]; then
    bad "/healthz did not answer"
    read -r -n 1 -s -p "Press any key to close …"; say ""
    exit 1
fi

read -r HOSTS_OK WARNINGS MODELS <<EOF
$(printf '%s' "$HEALTH" | python3 -c '
import json, sys
d = json.load(sys.stdin)
hosts = d.get("hosts", [])
healthy = sum(1 for h in hosts if h.get("status") == "healthy")
warn = len(d.get("warnings") or [])
models = len(d.get("models") or [])
print(f"{healthy}/{len(hosts)}", warn, models)
')
EOF

if [ "${HOSTS_OK%%/*}" = "${HOSTS_OK##*/}" ]; then
    ok "hosts: $HOSTS_OK healthy"
else
    bad "hosts: only $HOSTS_OK healthy"
fi

GUARD=$(printf '%s' "$HEALTH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["capabilities"]["eviction_guard"])')
if [ "$GUARD" = "True" ]; then
    ok "eviction guard active (a request cannot evict a running thread)"
else
    bad "eviction guard is off — see /healthz capabilities"
fi

if [ "$WARNINGS" = "0" ]; then
    ok "no warnings"
else
    bad "$WARNINGS warning(s):"
    printf '%s' "$HEALTH" | python3 -c '
import json, sys
for w in json.load(sys.stdin).get("warnings") or []:
    print("          · " + w)
'
fi

# 3. models the fleet may route
LISTED=$(curl -s --max-time 10 "$BASE/v1/models" | python3 -c '
import json, sys
try:
    print(", ".join(m["id"] for m in json.load(sys.stdin).get("data", [])))
except Exception:
    print("")
')
if [ -n "$LISTED" ]; then
    ok "routable models: $LISTED"
else
    bad "/v1/models returned nothing"
fi

# 4. one real completion, if an API key is available
if [ -r "$KEY_FILE" ]; then
    MODEL=$(printf '%s' "$LISTED" | cut -d, -f1 | sed 's/^ *//')
    RESP=$(curl -s --max-time 120 -w '\n%{http_code}\n%header{x-ripple-host}\n%header{x-ripple-reason}' \
        -H "Authorization: Bearer $(cat "$KEY_FILE")" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Sag nur: ok\"}],\"max_tokens\":5}" \
        "$BASE/v1/chat/completions")
    # curl appends: <body>\n<code>\n<host>\n<reason>
    REASON=$(printf '%s' "$RESP" | tail -1)
    HOST=$(printf '%s' "$RESP" | tail -2 | head -1)
    CODE=$(printf '%s' "$RESP" | tail -3 | head -1)
    if [ "$CODE" = "200" ]; then
        ok "completion on $MODEL answered by $HOST ($REASON)"
    else
        bad "completion returned HTTP $CODE"
    fi
else
    skip "no $KEY_FILE — completion test skipped"
fi

# 5. counters
STATS=$(curl -s --max-time 10 "$BASE/stats")
if [ -n "$STATS" ]; then
    python3 - "$STATS" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print(f"  --    requests {d.get('requests_total')}, ok {d.get('requests_ok')}, "
      f"errors {d.get('errors_total')}, 503 {d.get('http_503_total')}, "
      f"threads {d.get('threads_new')} new / {d.get('thread_hits')} hits, "
      f"fallbacks {d.get('fallbacks')}")
by = {k: v.get("requests") for k, v in (d.get("by_host") or {}).items()}
if by:
    print("  --    per host: " + ", ".join(f"{k} {v}" for k, v in by.items()))
PY
else
    skip "/stats not readable"
fi

say "────────────────────────────────────────────────────────"
if [ "$FAIL" = "0" ]; then
    say "Result: everything is fine."
else
    say "Result: FAILED — see the lines marked FAIL above."
fi
say "Dashboard: http://127.0.0.1:${PORT}/    Log: ~/Library/Logs/ripple.log"
say ""
read -r -n 1 -s -p "Press any key to close …"; say ""
exit "$FAIL"
