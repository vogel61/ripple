# AGENTS.md

Instructions for AI coding agents (Codex, Claude Code, Cursor, Kimi, …) working in this
repository. Read this before your first edit; it is short on purpose. `CONTRIBUTING.md`
holds the human-facing version.

## What this is

RIPPLE is a placement proxy in front of one or more oMLX instances on Apple Silicon. A
conversation ("thread") is bound to one node and stays there; new threads are steered so
that the running count per node matches a configured weight (`weight: 3` / `weight: 2` =
60 % / 40 %). Around that sit residency protection (an eviction guard that refuses to let a
model be pushed out by a larger one), a maintenance window, a model policy and a dashboard.

One maintainer, one live deployment, MIT. There is no team to absorb a broad refactor.

## Set up and run

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install .          # `pip install .`, not `-e` (see README)
./.venv/bin/python -m ripple --config config.yaml --check   # validate a config, no server
./.venv/bin/python -m ripple --config config.yaml           # the proxy itself
```

Python ≥ 3.9. `ripple` is the console script; `python -m ripple` is the same entry point.

## Tests

```bash
./.venv/bin/python -m pytest -q                    # the whole suite, ~240 s
./.venv/bin/python -m pytest -q tests/test_placement.py   # while iterating
```

- **Do not run two suites at the same time.** One test binds a port and then asserts that
  the *next* one is free; a second suite running beside it makes that test fail and the
  failure is not real.
- A green suite is the minimum bar, not the proof. Behaviour that a test cannot see (CSS,
  layout, a flag that only matters on this hardware) has to be exercised once for real.
- If you change behaviour, the test that pins it says *why* in its docstring, and the doc
  that describes the behaviour changes in the same commit.

## Conventions that are enforced or that keep the history readable

- **Issue first.** Every change carries an issue number: branch `issue/<n>`, commit
  `<area>: <sentence> (#<n>)`. A commit that points at an issue nobody opened is a false
  claim to whoever reads the log.
- **English** in code, tests, commit messages and `docs/` — with one exception:
  `docs/dashboard-design.md` is German, because the operator judges the page in German.
- **Measured, not estimated.** A number in a document or a test comes from a command, and
  the command is named next to it. "Should be about" is not a number.
- **Comments explain why**, carry the issue number, and are written for the person who
  reads the line in a year.
- **No drive-by reformatting, no new dependencies, no version bumps** unless the task asks
  for exactly that. Both make a review impossible.

## Three traps that have already cost real time

1. **CSS and JS are read as text by the tests.** A `*/` inside a CSS comment ends the
   comment early and silently swallows the declarations after it — the page renders wrong,
   every test still passes. There is a structural test for this (`tests/test_dashboard.py`,
   `test_no_comment_ends_in_the_middle_of_its_line`); keep it green and do not write `a*/b*`.
2. **i18n ids must be literals in `ripple/static/dashboard.js`.** A composed id
   (`t('fleet.series_' + which)`) works at runtime and breaks
   `test_every_id_the_script_asks_for_is_in_the_catalog`, which reads literals. Two labels
   mean two calls.
3. **Static assets carry `?v=<mtime>`.** If a CSS change "does not arrive", it is the
   browser cache or the file's mtime — not the server, which reads from disk per request.

## Safety

- **Never start, restart or kill a running instance** unless the task says so explicitly.
  The maintainer's proxy is in use by other people's conversations while you work.
- To try something against a live server, copy the config, change `listen_port` to a free
  port, and point the operator stores (`placement.model_policy_file`,
  `placement.maintenance_file`) at `/tmp` — otherwise a click or a request writes into the
  real state under `~/.ripple/`.
- The dashboard's write actions (calibration, maintenance, model policy, parameters,
  restart, node list, replication) are gated behind an admin key and are meant for an
  operator looking at the screen. Do not script them against a deployment you do not own.
- Do not paste API keys, host addresses or model paths from a real `config.yaml` into an
  issue, a test fixture or a commit message.

## Where to read before changing something

| Question | File |
|---|---|
| What does it do, how do I install it | `README.md`, `docs/commissioning.md` |
| How the operator uses the page | `docs/manual-en.md` |
| Why the dashboard looks the way it does | `docs/dashboard-design.md` (German) |
| Why threads and not requests | `docs/concurrency.md` |
| How to contribute, what a good report contains | `CONTRIBUTING.md`, `SECURITY.md` |
| What was decided, and when | `CHANGELOG.md`, the issue tracker |

## Pull requests

One topic per pull request. Small. Tests with it. If it changes what a user sees, the
screenshot or the doc paragraph changes with it. If you are an agent and the task is
ambiguous, open an issue describing what you would change and why, instead of guessing in
code.
