# Commissioning — RIPPLE on the M5

State: **14 November 2026, M3 in progress.** One instance runs on `0.0.0.0:8010`,
started from the repository with `./.venv/bin/python -m ripple --config config.yaml`,
log in `~/Library/Logs/ripple.log`.

## What was verified against the live hosts

| Check | Result |
|---|---|
| `/healthz` | both hosts `healthy`, `eviction_guard: true`, weights 3 / 2 |
| `/v1/models` | 3 models, allow-list applied (no model a host may not serve) |
| non-streaming completion | 200, `x-ripple-host`, `x-ripple-reason` present |
| streaming completion | 200, chunks arrive one by one, `[DONE]` last |
| 5 threads, 2 turns each | exactly 3 on the M5, 2 on the M3 |
| short requests | `reason: ephemeral`, no thread slot consumed |
| `/stats` | `requests_total 8`, `requests_ok 8`, `errors_total 0` |
| a real agent through RIPPLE | `delegation.base_url` pointed at `:8010/v1`, a subagent answered — the request shows up in `/stats` |

## Switching a client over

RIPPLE speaks the OpenAI API, so a client only needs a different base URL:

```
http://127.0.0.1:8010/v1        # from the M5 itself
http://192.0.2.8:8010/v1       # from the LAN (the M5's address)
```

The API key stays the same as for oMLX directly — RIPPLE forwards the client's
key, or the key from `upstream_api_key` if one is configured (FA-15). Nothing is
logged.

| Client | Where |
|---|---|
| Hermes (this agent) | `~/.hermes/config.yaml` → `delegation.base_url`; also `provider.base_url` for the main session |
| opencode | `~/.config/opencode/opencode.json` → `provider.omlx.options.baseURL` |
| curl / scripts | the base URL in the request |

`delegation.base_url` is read per spawn — changing it needs no restart. The main
session's provider URL does need a restart of the gateway, so it is the last step
and belongs to M4.

## Verifying a running instance

The quick way — double-click `deploy/verify-ripple.command` (or run it from a
terminal). It checks the port, both hosts, the eviction guard, the warnings, the
routable models, sends one real completion and prints the counters: one line per
check, green or red, and the window stays open at the end. Exit code 0 means
everything is fine, 1 means something failed.

By hand:

```bash
curl -s localhost:8010/healthz | python3 -m json.tool | head -40
curl -s localhost:8010/stats   | python3 -m json.tool | head -20
curl -s localhost:8010/config  | python3 -m json.tool     # effective values
curl -s localhost:8010/v1/models -H "Authorization: Bearer $(cat ~/.omlx/api_key.txt)" | python3 -m json.tool
open http://localhost:8010/            # dashboard, 2 s refresh
```

Which upstream a request went to is in the response header `x-ripple-host`; why
it went there is in `x-ripple-reason` (one of `new_thread`, `thread_hit`,
`prefix_match`, `model_pinned`, `deficit`, `fallback`, `spillover`, `ephemeral`).

## Stopping and starting

```bash
# stop the instance that holds port 8010
deploy/stop-ripple.command            # or double-click it

# start in a Terminal window
deploy/start-ripple.command           # or double-click it
```

Only one instance may run: the start script refuses if 8010 is taken and prints
the PID that holds it. A smoke instance on another port is left alone by the stop
script.

## launchd (M4)

The LaunchAgent in `deploy/ai.ripple.plist` starts RIPPLE at login and restarts
it after a crash.

**Only on the M5.** The plist is a template (`__REPO__`, `__HOME__`) that
`deploy/install-launchd.command` renders for the checkout and the account it runs
in, and RIPPLE is the single entry point from outside — the M3 runs
oMLX and nothing else. Installing it **must happen from a normal Terminal window
on the M5**, not from inside the agent gateway: the gateway blocks
`launchctl bootstrap` so that a tool call can never restart it indirectly.

**Stop the running instance first.** A hand-started instance holds port 8010;
launchd would start a second one that cannot bind, exit non-zero and be restarted
every 10 s (`ThrottleInterval`) — a crash loop that looks like "RIPPLE is broken".

### The quick way: double-click

`deploy/install-launchd.command` does all five steps, in a normal Terminal window:
stops a hand-started instance, installs the plist, writes the key into it, sets
the permissions, loads the agent and checks `/healthz` afterwards. It prints one
line per step and keeps the window open. `--dry-run` only says what it would do.

This exists because copy-pasting the commands broke once: shell comments at the
end of a pasted line (`cp … # 2.`) are read as arguments by zsh, which turned
`#` and `2.` into filenames. Do not paste command blocks with trailing comments.

### The manual way

Without trailing comments, so a paste works:

```bash
cd /Users/operator/Projects/ripple
deploy/stop-ripple.command
sed -e "s|__REPO__|$PWD|g" -e "s|__HOME__|$HOME|g" deploy/ai.ripple.plist > ~/Library/LaunchAgents/ai.ripple.plist
plutil -replace EnvironmentVariables.OMLX_API_KEY -string "$(cat ~/.omlx/api_key.txt)" ~/Library/LaunchAgents/ai.ripple.plist
chmod 600 ~/Library/LaunchAgents/ai.ripple.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.ripple.plist
launchctl print gui/$(id -u)/ai.ripple
```

The key substitution is not optional: launchd does not read files, so the plist
carries the placeholder `PUT_YOUR_OMLX_API_KEY_HERE` and the real key has to be
written into the copy in `~/Library/LaunchAgents/`. The `chmod 600` keeps it out
of other users' reach.

`launchctl print` should show `state = running` and a PID. Then
`deploy/verify-ripple.command` (double-click) confirms the instance end to end.

If `bootstrap` says the service already exists, the old one was never unloaded:

```bash
launchctl bootout gui/$(id -u)/ai.ripple
```

Undo:

```bash
launchctl bootout gui/$(id -u)/ai.ripple
rm ~/Library/LaunchAgents/ai.ripple.plist
```

## Rolling back

Every client switch is one line. To get everything back to direct oMLX:

```
delegation.base_url: http://localhost:8000/v1     # M5
provider.base_url:   http://localhost:8000/v1     # M5
```

RIPPLE holds no state that a client depends on: stopping it drops the thread
bindings, and the next request is placed from scratch. oMLX is never changed by
RIPPLE — it loads and unloads nothing (NZ-4).

## Soak period (M4)

M4 asks for seven days as a service and then an evaluation of the logs. The
collection side is running:

- `~/.hermes/scripts/soak_ripple.sh` runs `tools/soak_sample.py --quiet` every
  5 minutes as a cron job (`07f886706cb9`, no LLM involved). It appends one line
  with `/stats` + `/healthz` to `tools/soak/samples-YYYY-MM-DD.jsonl` (gitignored —
  raw samples stay local, the evaluation gets committed).
- The job is a watchdog: it prints nothing while RIPPLE is reachable and healthy.
  A delivered message means the proxy was down or reported warnings.
- An unreachable sample is recorded as `reachable: false` instead of being
  skipped, so a gap in the series is visible evidence.

Evaluation after the period:

```bash
./.venv/bin/python tools/soak_sample.py --report
```

It prints the sample span, the number of unreachable samples and gaps over
15 minutes, the counters at the end (requests, errors, 503, spillover, fallbacks,
address switches), the host share, the observed host statuses and any warnings.
Those are exactly the numbers M4 is judged on.

## Concurrency (measured 14.11.2026)

The M5's cap was set by hand in M2 and contradicted its weight, so it was
measured: 3, 6, 9 and 12 concurrent requests of 9 229 prompt tokens, two
configurations. Method, raw data and the full table are in
`docs/concurrency.md`.

| concurrent | M5 cap / spill | wall | split M5 : M3 |
|---|---|---|---|
| 9 | 3 / 2 (hand-set) | 66.5 s | 3 : 6 |
| 9 | 6 / 4 (measured) | 45.2 s | 5 : 4 |

The old `spill_threshold: 2` made the M5 count as full from two concurrent
requests, so the overflow went to the slower node: the split came out *inverted*
at 3 : 6 against a 3 : 2 target, and the same burst took a third longer. The M5
saturates at ~2.5–2.7 k prefill tokens/s from three concurrent requests on, so
`max_inflight: 6` buys latency under load (11 s → 20 s per request), not
throughput — 6 is the compromise, not an optimum.

`max_threads` is the other half of admission, and it is not a concurrency limit:
a thread holds its slot until its TTL expires (FA-19). With 12 slots, twelve
conversations within 30 minutes were enough to have new ones refused with
`503 no_slot (12/12 slots)` while nothing was in flight. Both hosts now run 20,
verified with 12 concurrent requests (6 : 6, no 503).

## Known limits at this point

- The 8010 instance is managed by launchd (`ai.ripple`): it starts at login and is
  restarted after a crash. A clean exit stays down (`SuccessfulExit: false`).
- The M5 carries the interactive agent session; its spare capacity is therefore
  lower than the M3's. The throughput measurement took the M5 *under* that load
  (see `docs/measurements.md`).
- No client is switched over permanently yet. That is the M4 decision.
