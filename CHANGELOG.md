# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-19

### Added

- **Sites as data** (issue #67) — a top-level `sites:` block of named locations
  with the CIDR networks they contain, and `hosts[].site` naming one by id. The
  claim is checked: the address a node is actually reached through is matched
  against the networks, and a contradiction is one `ripple doctor` line naming
  both sites plus a hint on the fleet card. `hosts[].replication.address` is an
  ordered list now (bridge first, LAN as the fallback — the first reachable
  entry wins and a fallback is reported). Placement never reads a site. A bare
  `site: WD` still parses; an unknown id is a warning, not an error.
- **`ripple init`** — first-run configuration for a machine that has nothing:
  port check that names the process holding it, nodes from discovery, an optional
  oMLX key (continuing without one works and is reported), a generated 32-hex
  admin key, one client key shown exactly once, and both files written with mode
  0600. `--yes` runs the same code path as the conversation.
- **`ripple discover`** — finds oMLX nodes without an address list. Probes the
  addresses it is given (default `127.0.0.1:8000`) and sweeps a network only with
  `--scan`. Identity over `GET /api/cluster/node_id`, admin rights probed
  separately so a 401 is a verdict about rights, not about the node.
- **`ripple doctor`** — one line per check, `OK`/`WARN`/`FAIL`, exit 0/1, ending
  in a real completion through the proxy. Missing admin rights are a warning, not
  a failure.
- **Packaging** — `pyproject.toml` (PEP 621), distribution `ripple-proxy`,
  console script `ripple`, `requires-python = ">=3.9"`, static assets as package
  data. A wheel test builds the real distribution and counts the 16 i18n
  catalogs, so a wheel that starts the dashboard without translations fails in CI
  rather than in front of a user.
- **`tools/coldstart.sh`** — the cold start as a script: clone, fresh venv,
  `init --yes`, `doctor`, a completion, `doctor` again against the running proxy,
  the dashboard, then stop and report.
- **`tools/secret_scan.py`** — scans the tracked tree and the whole history for
  secrets, including a literal search for a short key (`--key-file`), because a
  four-character key has no entropy signature. Prints redacted samples only.
- `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `docs/handover-coldstart-2.md`.
- **`docs/install.md`** (English, primary) and **`docs/install-de.md`** — the
  walkthrough from nothing to a running proxy:
  requirements, `pip install .` (and why not `-e`), the real `init` conversation,
  `doctor`, the client switch, the dashboard with screenshots, keys, and the
  failures that actually happened.
- **`docs/coldstart-report.md`** — the report of the first cold start on a second
  machine (macOS, system Python 3.9.6): what broke, in the order it broke, why,
  what was changed, and which check covers each finding today.

### Changed

- **`docs/measurements.md` replaces `docs/as6-measurement.md` and
  `docs/load-report.md`.** One document, two parts: what a node can do (decode,
  prefill, and what that says about the 3 : 2 weights) and what the fleet did
  (busy time per host, hourly shape, thread movements). Every number and table is
  carried over unchanged; the internal acceptance-step name is gone from the
  title. A closing section, "Measuring your own node", shows how to run
  `tools/as6_bench.py` against your own machine and what its number does not say.
- `config.example.yaml` no longer carries a live admin key, this fleet's
  `node_id`s, internal addresses, measured throughput or operator paths. It is a
  generic single-node example with the tuning knobs documented.
- `deploy/ai.ripple.plist` is a template (`__REPO__`, `__HOME__`);
  `deploy/install-launchd.command` renders it while installing.
- `ripple/__main__.py` dispatches `init`/`discover`/`doctor` by hand before
  argparse, so `--config`, `--check` and `--version` behave exactly as before.
- **`tools/coldstart.sh` takes `--model`.** Step 7 completed against whatever
  `/v1/models` listed first; on a node that serves real traffic that is the
  453 GB model, so a protocol run could have triggered its load. A named model is
  used when given, and a model the proxy does not offer is a red step.

### Fixed

- **`ripple doctor` called a node healthy when its answer was empty** (issue
  #82). The completion probe sent `max_tokens: 5` and only counted `choices`,
  so a reasoning model that spent the whole budget on its reasoning channel
  returned `content: ""` and the check said `OK` — the exact shape of the first
  cold start's finding 1. The probe now asks for one word and requires it back,
  and an empty answer is a `FAIL` naming the cause ("the node answers, but the
  answer is empty — a reasoning model spent the whole 64-token budget on its
  reasoning channel; raise max_tokens or clients see empty replies"). The budget
  is measured, not rounded: 5, 6, 8 and 10 tokens produced no visible text in
  any run, 16 did in every run, 64 in 43 of 43 (32 failed once in 19). The check
  costs about 0.8 s more per `doctor` run (median 558 ms → 1332 ms against the
  live fleet), because the probe now generates 12-26 tokens instead of five.

- **A loopback node is not placed, and `doctor` says so instead of skipping in
  silence** (issue #76). A fresh `ripple init` writes the `sites:` block but no
  `site:` on `127.0.0.1`, so the site check from #67 could only ever `SKIP` on the
  reference path, while a hand-written `site:` on that node turned it into a
  `WARN`. Loopback is in no site's networks on purpose: every machine's loopback
  is `127.0.0.1`, so a site containing it would contain every machine, and an
  `OK` there would report a comparison that never happened. `init` now prints one
  line naming the loopback node, `doctor` reports `SKIP` with the loopback as the
  reason (also when a `site:` was declared — unconfirmed, not contradicted), and
  both install guides state it.

- **The proxy did not start on Python 3.9.** FastAPI evaluates the annotations of
  a route handler when it builds the app, and `minutes: float | None` raises
  `TypeError: unsupported operand type(s) for |` there — every module imported
  cleanly, so neither an import check nor `--check` could see it. The annotation
  is `Optional[float]` now, guarded by a static test that runs everywhere.
- A test read the operator's real `~/.ripple/maintenance.yaml` and failed on the
  day a node was taken out of the pool, for reasons unrelated to the code under
  test. The harness keeps every operator store under `/tmp` again, with a guard
  test for all four paths.
- `ripple init --yes` ignored `$OMLX_API_KEY` and configured a read-limited node,
  then skipped its own test request.
- `ripple init` wrote the whole offered model list into `models:`. An empty
  allow-list means "everything this node offers"; a frozen list hides every model
  added to the node later, silently.
- `config.yaml.bak*` was not ignored, although a backup carries the same secrets
  as the file.
- **`tools/coldstart.sh` read a reasoning-only answer as a failure.** `max_tokens:
  16` was spent entirely on `reasoning_content`, so `content` came back empty and
  step 7 went red on a machine where the same model simply thought longer. The
  budget is 128 now and the extraction accepts `content` or `reasoning_content`,
  naming which one it used.
- **`tools/coldstart.sh` went red on `--no-key`,** which is a documented path.
  Measured: oMLX answers 401 on `/v1/models` without a key, so a read-limited node
  offers no model and there is nothing to complete with. That is a note now, not a
  failure; with a key an empty model list is still an error.
- **`tools/coldstart.sh` never proved doctor's `dashboard` and `completion`
  checks.** The only doctor run happened before the proxy started, so both were
  `SKIP` — the report said "green, including a real completion" without one ever
  having run. A second `ripple doctor` against the running proxy is step 8 now,
  and a red doctor stops the run.
