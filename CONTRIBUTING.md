# Contributing

RIPPLE is a placement proxy in front of one or more oMLX nodes. The interesting
part is not the forwarding — it is deciding *which* node gets a request, keeping
a conversation on one node while that pays, and being honest about what it
measured. Contributions that keep that honest are welcome.

## Getting a working tree

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'      # or: pip install -r requirements.txt
./.venv/bin/python -m pytest -q          # ~160 s, 700+ tests
```

Python **3.9 or newer**. The code is written for 3.9 on purpose (the reference
machine's system Python): keep `from __future__ import annotations`, and do not
put `X | Y` unions or `list[str]` in a position that is evaluated at runtime
(default values, dataclass field types, `isinstance`).

No `sudo`, no `brew` needed. On a machine without a suitable Python, `uv python
install 3.12` is the documented detour.

## Running it

```bash
ripple discover                      # which oMLX nodes are there?
ripple init                          # write config.yaml and ~/.ripple/keys.yaml
ripple doctor                        # is everything green?
ripple --config config.yaml          # serve
```

`ripple init` writes the two files a stranger needs; `ripple doctor` says what is
missing. Both are meant to be runnable without reading the source.

## Tests

* Run the suite before every commit. A red test is fixed, not explained away.
* New behaviour comes with a test that would fail without it. A test that only
  re-states the implementation is not a test.
* Tests must never read or write the operator's real stores — `~/.ripple/*`,
  `~/Library`, `config.yaml`. Use `tmp_path` and the helpers in
  `tests/support.py`, which redirect every operator store to `/tmp`. If you add a
  new store, add the redirect and a guard test with it: a test that reads the
  real `maintenance.yaml` fails on the day the operator takes a node out of the
  pool, for a reason that has nothing to do with your change.
* Stub nodes (`tests/conftest.py: stub_factory`) instead of a real oMLX. The
  suite must pass on a machine with no inference server at all.

## Commits and language

* Code, comments, commit messages, issues and pull requests: **English**.
  `docs/dashboard-design.md` and the other `docs/dashboard-*.md` are German — the
  operator judges the page in German.
* One commit, one concern. No `WIP`, no `--no-verify`.
* Say what you measured. A number in a commit message or a document must come
  from an output you can quote; if you did not measure it, write that.

## Secrets

Never commit a key, a token, a password or a real internal address. Examples use
placeholders or documentation ranges (`192.168.1.x`, `127.0.0.1`). Before
anything is published:

```bash
tools/secret_scan.py --key-file ~/.omlx/api_key.txt
```

It scans the tracked tree and the whole history, and prints only redacted
samples. History hits cannot be fixed by editing a file — they need a history
rewrite, which is the maintainer's decision, not a contributor's.

## Reporting a problem

Security issues: see `SECURITY.md`, not a public issue. Everything else: an issue
with the output you saw, the version (`ripple --version`) and what you expected.
