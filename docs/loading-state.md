# Loading state, flap guard and per-node load calibration

**Written:** 15.11.2026, 07:20 — specification for a new session. **Nothing of this
is implemented yet.**
**Read first:** `docs/handover.md` (project state), `docs/measurements.md`
(throughput numbers), `docs/loading-state.md` (this file).

## 1. Why

Incident, 14.11.2026 20:30, found by rehearsing `tools/as5_live.py`: a request
caused a 111,6 GB cold load on M3-beta (24,7 s). RIPPLE's health probe runs every
10 s with a 5 s timeout; one probe landed inside the load, timed out, and
`ripple/hosts.py::_apply_health()` set `healthy = False` on that **single**
failure. The next request — one second after the first one had completed — was
answered `503 no_healthy_host` in 1 ms, although the host was fully back.
Evidence: `docs/as5-live-2026-11-14-rehearsal-1.md`.

This hits every client, not just the test tool: trigger a cold load, wait it out,
send the next request, get an error.

## 2. What is to be built

Three parts, in this order.

1. **A `loading` state per host.** RIPPLE knows when *it* causes a cold load.
   While a host loads, its health probes are suspended and it keeps its last known
   health, so it stays in the candidate pool. The state ends on the first of:
   oMLX reports `is_loading: false`, the request that caused the load returns, or
   a ceiling expires.
2. **A general flap guard.** A host becomes `down` only after 3 consecutive failed
   probes (30 s at the current 10 s interval). One or two failures make it
   `degraded`: reported as such, but still usable, with the placement reason
   `degraded_host`, so the fallback is never silent.
3. **Per-node load calibration with a dashboard button.** Each node gets a
   `Calibrate` button that loads one large and one small model *on that node* and
   derives the node's own load-time curve. Until a node is calibrated it uses
   conservative M3-Ultra defaults.

Why part 3: the load rate is a property of the node's storage path, not of RIPPLE.
An M5 loading from an external SSD is roughly twice as slow as one loading from
its internal disk. Hard-coding a single rate makes RIPPLE wrong for every setup
but ours.

## 3. Measured facts (verify before changing anything)

From the AS-5 night run, 15.11.2026 01:00–01:03, on M3-beta:

| model | size | load time | per GB |
|---|---|---|---|
| Qwen3.8-Flash-Next-oQ4e-mtp | 111,6 GB | 24,7 s | 0,221 s/GB (4,52 GB/s) |
| DeepSeek-V4.1-Flash-1017-oQ4e-mtp | 453,6 GB | ~131 s | 0,289 s/GB (3,46 GB/s) |
| unload of DeepSeek | 453,6 GB | 2,9 s | negligible |

**A linear model does not fit.** Fitting `t = a + b·size` through the two points
gives `b = 0.311 s/GB` and **`a = -10.0 s`** — a *negative* fixed cost, which is
not physical. The large model is 30 % slower per gigabyte, not faster, so the curve
is not linear over this range (allocation and materialisation grow with the model).
Richard's observation of a ~10 s fixed overhead may well be true for **small**
models, where startup dominates — we have no measurement there. Assuming 10 s plus
0.311 s/GB would predict 39,8 s for the 111,6 GB model, measured were 24,7 s.

**Consequences for the calibration:** sample **at least three sizes including a
small one**, never assume a positive intercept, and if the samples still fit badly,
store the samples and interpolate monotonically instead of inventing parameters.

**Unload is negligible**, so cycling models during calibration costs almost nothing.

## 4. State machine per host

```
                 probe ok
        +------------------------------+
        |                              v
   [healthy] <---- load done ---- [loading]
        |                              ^
        | 1-2 failed probes            | RIPPLE dispatches a request
        v                              | that needs a cold load
   [degraded]   (usable, reason        |
        |        "degraded_host")       |
        | 3 failed probes in a row      |
        v                               |
     [down]   (out of the candidate pool)|
        +--- probe ok -------------------+
```

Rules:
- `loading` is **not** a health verdict. The host keeps its previous `healthy`
  value and stays in the pool. Setting it to `false` here would reproduce the
  original defect with a different label.
- The probe scheduler skips hosts in `loading`; counter `probes_skipped`.
- `degraded` is a reporting state *plus* a pool rule: `down` leaves the pool,
  `degraded` does not.
- Entering `down` re-probes the address list (existing behaviour,
  `discovery.unhealthy_after`), unchanged.

## 5. How a load is detected — three signals, first one wins

1. **Local and certain.** `choose()` returned a candidate with tier
   `TIER_COLD_LOAD` for model M on host H and the request is about to be
   dispatched: set `H.loading = {model: M, size: estimated_size, started: now}`.
   No network call, and it covers the case that matters.
2. **Authoritative.** oMLX's `/v1/models/status` carries `is_loading` and
   `loading_started_at` per model (verified on 15.11.2026). The existing residency
   poll already reads that endpoint. When `is_loading` is true, set the state from
   oMLX's own timestamp (more accurate than ours); when it flips false, clear it.
   This is the only signal that covers loads RIPPLE did **not** cause (oMLX UI, a
   client talking to `:8000` directly).
3. **Ceiling.** `loading` ends at the latest after `placement.loading.max_seconds`
   (default 900 s), so a hung load cannot suspend probes forever.

## 6. Calibration

### 6.1 UI

One `Calibrate` button per node in the dashboard, next to the node's health.
Admin-key protected (same pattern as `/admin/keys`, see `ripple/keys_page.py`).
Disabled with a tooltip when the node holds an active thread; a confirmation
dialog lists which models will be loaded and what may be evicted.

**This contradicts NZ-4 ("dashboard is read-only").** Amend that requirement: this
is an operator-initiated maintenance action, not a general load/unload button.
Record the amendment in `docs/requirements.md` and in the acceptance table.

### 6.2 Endpoints

```
POST /admin/nodes/{name}/calibrate      -> 202 {"job": "<id>"}     (admin key)
GET  /admin/nodes/{name}/calibration    -> status, samples, fit, source
```
Asynchronous: a 453 GB load takes minutes, the HTTP call must not block.

### 6.3 Procedure per node

1. Refuse if the node is unreachable or unhealthy, or if it holds a bound thread
   (unless the operator passed `force` after an explicit confirmation).
2. Choose the models from the node's allow-list, **not currently loaded**, largest
   and smallest by `estimated_size`, size ratio >= 3, preferring models that are
   not the entry session's model.
3. For each model: `POST /v1/models/{id}/load`, poll `/v1/models/status` every
   0,5 s until `loaded: true`, and measure from `loading_started_at` (oMLX's own
   timestamp) to the first `loaded: true`. Store `(size_bytes, seconds)`.
4. Unload every calibration model afterwards and verify. Report anything that was
   evicted on the way — the FA-18 forecast should have predicted it.
5. Fit and store.

### 6.4 Storage

`~/.ripple/calibration.yaml`, mode 0600, **keyed by `node_id`** (the FA-21
identity), not by host name — a node keeps its calibration when it is renamed or
gets a new address.

```yaml
version: 1
defaults:
  source: m3u-conservative
  samples:
    - {size_gb: 111.6, seconds: 24.7}
    - {size_gb: 453.6, seconds: 131.0}
nodes:
  e19d4c08-e64f-4641-98eb-19721a27d449:
    calibrated_at: '2026-11-15T07:20:00+01:00'
    calibrated_by: dashboard
    source: measured
    samples:
      - {model: 'Qwen3.8-Flash-Next-oQ4e-mtp', size_gb: 111.6, seconds: 24.7}
      - {model: 'DeepSeek-V4.1-Flash-1017-oQ4e-mtp', size_gb: 453.6, seconds: 131.0}
```

### 6.5 Estimate function

```
estimate(node, size_gb):
    samples = calibration of node, else the defaults
    if len(samples) >= 2:
        t = monotone piecewise-linear interpolation over the samples,
            extrapolated outside the measured range with the WORST slope
    else:
        t = size_gb * worst default slope
    return clamp(t * placement.loading.safety_factor, 10 s, max_seconds)
```
The estimate drives the ceiling and the ETA in the dashboard. It must never be
*shorter* than the longest observed load of comparable size: too long is harmless
(probes stay off a little longer), too short is not.

## 7. Config surface

```yaml
placement:
  loading:
    enabled: true
    safety_factor: 1.3
    max_seconds: 900
    calibration_file: ~/.ripple/calibration.yaml
  flap_guard_failures: 3      # 3 = 30 s at discovery.interval_seconds 10
```
`flap_guard_failures: 1` restores today's behaviour (drop on the first failure).

## 8. Counters and reporting

`/stats`: `loads_supervised`, `loads_failed`, `probes_skipped`,
`degraded_pool_decisions`, `calibrations_started`, `calibrations_ok`.
`loads_failed` counts a load that ended without the model becoming resident (the
node stopped reporting it, or dropped the model) — a successful load and a
residency failure end the loading state the same way, and only this number tells
them apart. `degraded_pool_decisions` counts placement *decisions* that used a
degraded host, not requests: a `retry_other_node` hop decides again.
`/healthz` per host: `state` (`healthy|loading|degraded|down`) and, while loading,
`loading: {model, size_gb, started_at, eta_seconds}`.
Request reason `degraded_host` when a degraded host was chosen — visible in
`x-ripple-reason` and in the log line (`reason=degraded_host`).

## 9. Safety rules

- Never calibrate the entry node while a thread is bound there without an explicit
  `force`, and never a model that a bound thread is using.
- Every calibration restores the previous loaded set as far as it can and reports
  what it could not restore.
- The button is admin-key protected; everything else in the dashboard stays
  read-only.
- Do not measure the M5's load rate during working hours: it evicts DeepSeek, which
  carries the operator's session.

## 10. Tests (stub-based, no oMLX, in the existing style)

1. A cold load sets `loading`, probes for that host are skipped, the host keeps
   `healthy: true` and stays in the pool.
2. `loading` clears when the request returns, when `is_loading` flips false, and at
   the ceiling — three separate tests.
3. One failed probe -> `degraded`, still usable, reason `degraded_host`, counter.
4. Two failed probes -> still `degraded`.
5. Three failed probes -> `down`, out of the pool.
6. `flap_guard_failures: 1` restores the old behaviour.
7. Estimate interpolates between samples, extrapolates with the worst slope,
   applies the safety factor and clamps.
8. A single sample falls back to the worst slope.
9. Calibration refuses on a bound thread; `force` overrides.
10. Calibration measures from `loading_started_at`, not from the request start.
11. Calibration unloads its models afterwards and reports evictions.
12. Both endpoints require the admin key.
13. `~/.ripple/calibration.yaml` is keyed by `node_id` and survives a rename.

## 11. Live verification

`tools/as5_live.py` already triggers a real cold load on M3-beta and is the natural
harness:
- Before: the request after the load gets `503 no_healthy_host` (reproduced on
  14.11.2026 20:30).
- After: same run, no 503; `/healthz` shows `loading` with an ETA during the load;
  `probes_skipped` counts, no health flap.
- Then calibrate M3-beta for real (it is the node without the operator session) and
  compare the curve with §3. The third, small sample answers the open question
  whether a fixed overhead exists.

## 12. Acceptance

1. A request sent immediately after a cold load completes is served, not refused.
2. During a cold load `/healthz` reports the host as `loading` with model, size and
   ETA, and no probe failure is counted.
3. A host that misses one or two probes stays usable and reports `degraded`; three
   in a row make it `down`.
4. An uncalibrated node uses the M3-Ultra defaults and reports `source: default`.
5. After calibration the stored samples are used and the ETA changes accordingly.
6. The button is admin-key protected and refuses on a bound thread without `force`.

## 13. Open decisions (do not guess)

1. Calibration models: chosen automatically (largest/smallest not loaded, ratio
   >= 3) or configured per node?
2. Should `force` exist at all, or is calibration on a busy node simply forbidden?
3. Three samples by default (small/medium/large, a few minutes more) or two with a
   warning?
4. Does the dashboard show an ETA, or only the state?

## 14. Files to touch

```
ripple/hosts.py         state machine, probe suppression, flap guard
ripple/placement.py     pool eligibility (degraded), reason degraded_host
ripple/proxy.py         set/clear loading at dispatch, counters
ripple/calibration.py   new: storage, fit, estimate
ripple/dashboard.py     state, ETA, Calibrate button
ripple/keys_page.py     admin auth pattern to reuse
config.example.yaml     placement.loading, flap_guard_failures
docs/requirements.md    NZ-4 amendment
tests/                  the cases in section 10
```

## 15. Out of scope

- Pre-warming or scheduling loads on RIPPLE's own initiative (NZ-4 still forbids it).
- Any change to the oMLX installation.
- Remote providers (M6).
