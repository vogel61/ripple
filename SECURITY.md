# Security policy

RIPPLE sits in front of inference nodes and holds two kinds of secrets: the
client keys it issues (`~/.ripple/keys.yaml`, mode 0600) and the oMLX API key of
each node (never in the repository, taken from the environment or from
`~/.omlx/api_key.txt`). A leaked key of either kind is a report we want to hear
about before it is a public issue.

## Reporting a vulnerability

Write to **recipient@example.com** with the subject line `RIPPLE security` and
include:

* what an attacker gains (read traffic, spend GPU time, change configuration,
  reach another machine),
* the version (`ripple --version`) and the configuration shape that shows it,
* a reproduction if you have one.

Do **not** open a public issue for anything that includes a working key, a token
or an exploitable path. Do not include a real key in the mail either — say where
it sits and we will rotate it.

There is no bounty. There is an answer: we aim to reply within a week.

## What is in scope

* Authentication and authorisation: `/v1/*` client keys, `/admin/*` and `/keys`
  admin access, the loopback exemption (`auth.allow_loopback`).
* Anything that lets one client read another client's prompts or responses.
* Anything that lets a request reach a node's filesystem — the replication
  endpoints are the only part of RIPPLE that writes to another machine, and they
  are admin-only by design.
* Secrets in the repository or in its history.

## Known weaknesses, stated on purpose

* The admin key is whatever the operator sets. The original installation used
  four digits, which is weak by nature: it is protected only by the admin rate
  limiter (failed attempts per client address) and by the fact that `/keys` is
  the only door. `ripple init` generates 32 hex characters instead.
* There is no TLS. RIPPLE is meant for a trusted LAN or behind a reverse proxy;
  a client key travels in clear text on the wire.
* `auth.allow_loopback: true` (used during the rollout on the operator's machine)
  accepts unauthenticated requests from `127.0.0.1`. Anything that can run code
  on the host is therefore trusted, which is the normal local-trust assumption
  but worth saying out loud.

## What we do about it

* Every release runs `tools/secret_scan.py` over the tracked tree and the whole
  history, including a literal search for the oMLX key (`--key-file`).
* Keys are only ever printed masked, except once at creation.
* The key store is written with mode 0600 and compared with `hmac.compare_digest`.
