# Distributing agent load across the two hosts (2:1)

While RIPPLE is not yet in production, agentic work (subagents) is spread over
both machines at roughly **2 : 1 (M5 : M3)**. The M5 is the stronger machine and
also carries the main session, so it gets the larger share; the M3 takes the
smaller one.

This is not a RIPPLE feature — RIPPLE balances client requests. This note is
about how *development work* is parallelised while the project is being built.

## The lever

Subagents inherit their endpoint from the `delegation` section of
`~/.hermes/config.yaml`:

```yaml
delegation:
  model: DeepSeek-V4.1-Flash-1017-oQ4e-mtp
  base_url: http://localhost:8000/v1      # M5
  # base_url: http://192.0.2.12:8000/v1   # M3
```

The value is read **per spawn** (config cache is invalidated by mtime), so no
gateway restart is needed. Flip `base_url` between spawn calls:

1. leave `localhost` (M5) — dispatch two children;
2. set `192.0.2.12` (M3) — dispatch one child;
3. back to `localhost`.

Running children keep their connection; only new spawns move.

## Verifying the split

`lsof` on the Hermes process shows which endpoint a child actually uses:

```bash
lsof -iTCP:8000 -n -P | awk '$2==<hermes-pid>'     # children
lsof -iTCP@127.0.0.1:8000 -n -P | awk '$2==<pid>' | wc -l   # on M5
lsof -iTCP@192.0.2.12:8000 -n -P | awk '$2==<pid>' | wc -l  # on M3
```

A quick probe child (long output, no tools) makes the connection visible for
long enough to read.

## Limits

- Both hosts serve DeepSeek-V4.1 (403 GB actual). Concurrent children add KV
  cache, not model memory, so 2–3 children per host are unproblematic.
- `delegation.max_concurrent_children` caps the fan-out (currently 4). At
  capacity a spawn is rejected, so keep one slot free while long tasks run.
- The split is **not** applied while a measurement runs (AS-6): a busy M3
  produces wrong tok/s numbers. Measure when the fleet is idle.
