# RIPPLE — operating note

RIPPLE listens on `0.0.0.0:8010` and distributes requests over the oMLX nodes
(M5, M3). Clients point at `http://<M5>:8010/v1` instead of `:8000`.

| File | Purpose |
|---|---|
| `start-ripple.command` | start in a Terminal window (double-click) |
| `stop-ripple.command` | stop the running instance (double-click) |
| `verify-ripple.command` | check a running instance end to end (double-click) |
| `ai.ripple.plist` | launchd LaunchAgent: starts at login, restarts on crash |

`verify-ripple.command` is the one to double-click when something feels off: it
checks the port, both hosts, the eviction guard, the warnings, the routable
models, sends one real completion and prints the counters — one line per check,
green or red, and the window stays open so the result can be read. Exit code 0
means everything is fine. `RIPPLE_PORT` overrides the port it looks at.

Run **one** instance only. The start script refuses to start if port 8010 is
already in use and prints the PID that holds it.

## Start and stop by double-click

Double-click `start-ripple.command`. It reads the API key from
`~/.omlx/api_key.txt` (unless `OMLX_API_KEY` is already set), then runs

```
./.venv/bin/python -m ripple --config config.yaml
```

Closing that Terminal window (or Ctrl-C) stops RIPPLE. To stop an instance
started in another window, double-click `stop-ripple.command` — it finds the
process with `ps -Ao pid=,command=`, sends SIGTERM and reports the PID it
stopped. Instances on another port (e.g. a smoke test on 8011) are left alone.

## Install as a launchd service

Only on the M5, in a normal Terminal window (the agent gateway blocks
`launchctl bootstrap`). **Double-click `deploy/install-launchd.command`** — it
stops a hand-started instance, installs the plist, writes the key into it, sets
the permissions, loads the agent and checks `/healthz`. `--dry-run` only says
what it would do.

Do not paste the commands with trailing comments: zsh reads `cp … # 2.` as
`cp` with the arguments `#` and `2.` — that broke an install once.

The manual way, paste-safe:

```bash
cd ~/Projects/ripple
deploy/stop-ripple.command
mkdir -p ~/Library/LaunchAgents
sed -e "s|__REPO__|$PWD|g" -e "s|__HOME__|$HOME|g" deploy/ai.ripple.plist > ~/Library/LaunchAgents/ai.ripple.plist
plutil -replace EnvironmentVariables.OMLX_API_KEY -string "$(cat ~/.omlx/api_key.txt)" ~/Library/LaunchAgents/ai.ripple.plist
chmod 600 ~/Library/LaunchAgents/ai.ripple.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.ripple.plist
launchctl print gui/$(id -u)/ai.ripple
```

The plist in the repository is a template: `__REPO__` is the checkout, `__HOME__`
the installing account. Copying it unrendered fails loudly instead of pointing at
somebody else's directory.

The key never goes into the repository — the plist carries a placeholder
(`PUT_YOUR_OMLX_API_KEY_HERE`) that the `plutil -replace` line fills in locally.
launchd does not read files, so that step is not optional.

`RunAtLoad` starts RIPPLE at login; `KeepAlive {SuccessfulExit: false}` restarts
it after a crash but not after a clean exit. `ThrottleInterval 10` keeps a
crash loop from hammering the system.

Undo: `launchctl bootout gui/$(id -u)/ai.ripple` and remove the plist from
`~/Library/LaunchAgents/`.

## Status

```bash
launchctl print gui/$(id -u)/ai.ripple     # job state, PID, last exit code
curl -s localhost:8010/healthz | python3 -m json.tool
curl -s localhost:8010/stats   | python3 -m json.tool
open http://localhost:8010/                 # read-only dashboard
```

## Logs

```bash
tail -f ~/Library/Logs/ripple.log
```

RIPPLE rotates this file itself (10 MB × 5); launchd appends its own output to
the same path.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/ai.ripple
rm ~/Library/LaunchAgents/ai.ripple.plist
```

To stop it without uninstalling: `launchctl bootout gui/$(id -u)/ai.ripple`
(`launchctl kickstart -k gui/$(id -u)/ai.ripple` restarts it).

## Emergency operation (RB-1)

If RIPPLE itself is down, the M5 still serves on its own port. Point the
clients back at the M5 directly — one line in the client configuration:

```
base_url: http://192.0.2.11:8000/v1      # was: http://<M5>:8010/v1
```

No second instance and no configuration change on the nodes are needed: the
oMLX instances keep running while RIPPLE is down, and RIPPLE holds no state
that has to be recovered — restarting it is enough.
