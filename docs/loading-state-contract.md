# Contract v1 — loading state, flap guard, calibration

**Status:** frozen in wave W0 (15.11.2026). Binding for W1 (state machine),
W2a (calibration engine), W2b (dashboard) and W3 (admin endpoints).
**Derived from:** `docs/loading-state.md` (the specification, authoritative) and
the wave split W1–W3 that this contract freezes. If a wave needs a change here, it
stops and reports — it does not edit this file.

Why this document exists: three waves touch the same fleet object from different
files. Without a frozen surface they cannot run in parallel, and without a frozen
vocabulary the tests of one wave would not mean anything to the next.

---

## 1. Config (`ripple/config.py`)

```python
@dataclass
class LoadingConfig:
    enabled: bool = True
    safety_factor: float = 1.3
    max_seconds: float = 900.0
    calibration_file: str = "~/.ripple/calibration.yaml"
    samples: int = 3                  # 2 or 3

# PlacementConfig gains:
    loading: LoadingConfig            # default_factory
    flap_guard_failures: int = 3      # 1 = behaviour before the spec
```

`load_config()` parses `placement.loading.*` and `placement.flap_guard_failures`;
`samples` is validated `2 … 5`, `flap_guard_failures >= 1`,
`safety_factor >= 1.0`. `Config.summary()` exposes both through `asdict()` — no
extra work, and the dashboard's parameter table picks them up for free.

## 2. oMLX signals (`ripple/backends.py`, `tests/support.py`)

```python
ModelInfo.is_loading: bool | None = None          # None = not reported
ModelInfo.loading_started_at: float | None = None # epoch seconds, oMLX's clock
```

Parsed tolerantly in `OmlxBackend.residency()` via `_as_bool_opt()` / `_as_float()`.
**`None` is "unknown", never "not loading".**

**Live caveat (15.11.2026):** `loading_started_at` is **not** a Unix timestamp.
oMLX reports a monotonic clock value there (158021.3 on beta, constant while
the load runs). It may only be used through `ripple.hosts.epoch_seconds()`, which
rejects anything outside the ceiling around *now* and makes the caller fall back
to its own clock. Read as epoch it puts the load ~57 years in the past.

Stub (`tests/support.py`): `StubModel.is_loading` (default `None`),
`StubModel.loading_started_at`; `StubState.start_load(model_id, started_at=None)`
and `StubState.finish_load(model_id)`; `/v1/models/status` emits both fields.

## 3. Host state (`ripple/hosts.py`)

```python
loading: dict | None = None
# {"model": str, "size_gb": float, "started_at": float,
#  "eta_seconds": float,
#  "source": "dispatch" | "omlx" | "calibration" | "preload"}
# `preload` is the fourth value and the only one added after the freeze — see
# §9 (issue #119).

@property
def in_pool(self) -> bool:
    return self.status != "down"
```

Status vocabulary: `unknown | healthy | loading | degraded | down`.

1. **`healthy` keeps its meaning**: the verdict of the last health probe. It is
   *not* redefined and stays in `/healthz`.
2. **`in_pool` is the only placement predicate.** All eight current call sites
   move to it: `hosts.py:346` (`healthy_hosts`), `hosts.py:350` (`entry_host`),
   `placement.py:247`, `:257` (pool), `:456` (rebind destination), `:493`
   (`admit`), `proxy.py:350` (bound-thread saturation branch).
   `healthy_hosts()` keeps its name and becomes "hosts in the pool".
3. **`loading` is not a health verdict.** Entering it changes neither `healthy`
   nor `in_pool`; `status` reports `loading` for the operator only.
4. **Hard vs. soft probe failure.**
   - hard → `down` at once: `httpx.ConnectError`, `httpx.ConnectTimeout`,
     `httpx.UnsupportedProtocol`, `OSError`, a failed `resolve()` (address list
     exhausted), identity mismatch.
   - soft → counts toward `flap_guard_failures`: `httpx.ReadTimeout`,
     `httpx.ReadError`, `httpx.RemoteProtocolError`, `HTTPStatusError`,
     `ValueError`, and an answered health body whose `status != "healthy"`.
   Rationale: a refused TCP connect is not a load artefact, and AS-5 (stopped
   node → 10/10 on the other) must keep passing. The incident on 14.11.2026 was a
   *read timeout* — exactly the soft case.
5. Transition in `_apply_health()`: ok → `healthy` (or `loading` if a load is
   running); soft failures `1 … flap_guard-1` → `degraded` (stays in the pool);
   `>= flap_guard` → `down` + re-resolve (existing behaviour). `resolve()`
   failure → `down`.

## 4. Loading lifecycle (`ripple/hosts.py`)

```python
def begin_loading(self, model: str, *, size_gb: float | None,
                  eta_seconds: float, source: str) -> None
def end_loading(self, reason: str) -> None      # idempotent
def loading_expired(self, now: float | None = None) -> bool
```

- `begin_loading` overwrites an existing entry (the newer load is the truth) and
  is a no-op when `placement.loading.enabled` is false.
- The entry carries two flags beyond the reporting fields: `seen_loading` (the node
  has confirmed this load as running at least once) and `idle_unloaded` (one
  answer said "not loading, not loaded" — one tick is not a verdict, §12.1).
- `end_loading` clears the dict, restores `status` from `healthy`
  (`healthy → "healthy"`, else `degraded`/`down` per counter), logs one line
  `loading_end host=… model=… reason=… seconds=…`. It never counts: it cannot
  tell a load that produced a model from one that did not. That is
  `_poll_loading`'s job (§12.1).
- Probe suppression lives in `refresh()`: if `self.loading is not None`, call
  `_poll_loading()` (reads `/v1/models/status` **only** — no `/health`),
  increment `stats.probes_skipped`, and return. `_poll_loading()` ends the state
  when `is_loading` flips false, when `loading_expired()` is true, or when the
  model is gone from the node. The last two are not the same kind of end: a model
  that is gone, or still `loaded: false` after two answers, is a **failed load**
  (`stats.loads_failed`, one `loading_failed` line); the ceiling is our own
  verdict and counts nothing.
- `resolve()`/`reinit()` clear `loading` (a re-resolved host is not loading).

## 5. Placement (`ripple/placement.py`)

- Pool and admission use `in_pool` (§3.2).
- New reason `degraded_host`: when the chosen host has `status == "degraded"`,
  `Decision.reason` becomes `degraded_host` and `stats.degraded_pool_decisions += 1`;
  `Decision.note` keeps the tier reason. Precedence: `slot_evict` and
  `model_pinned` win over `degraded_host` — they say something stronger about
  *this* request. The counter counts **decisions**, not requests: `choose()` runs
  again for a `retry_other_node` hop, so one client request can count twice (§8).
- A host in `loading` needs no extra rule: it is in the pool by definition.

## 6. Calibration module (`ripple/calibration.py`, W2a)

Pure logic + storage. No FastAPI, no network, no import of `ripple.hosts`.

```python
DEFAULT_SAMPLES: list[tuple[float, float]]   # [(111.6, 24.7), (453.6, 131.0)]

@dataclass
class Calibration:
    defaults: list[tuple[float, float]]
    nodes: dict[str, dict]      # node_id -> {calibrated_at, calibrated_by,
                                #             source, samples: [{model,size_gb,seconds}]}

def load(path) -> Calibration
def save(path, cal) -> None                      # atomic, mode 0600
def samples_for(cal, node_id) -> list[tuple[float, float]]
def worst_slope(samples) -> float                # max s/GB
def fit(samples) -> dict                         # {"slope","intercept","worst_slope","linearity"}
def estimate(cal, node_id, size_gb, *, safety_factor, max_seconds) -> float
```

`estimate` = monotone piecewise-linear interpolation between samples,
extrapolation outside the measured range with the **worst** slope, one sample →
`size_gb * worst_slope`, then `× safety_factor`, then `clamp(…, 10, max_seconds)`.
`fit()` is reporting only: the negative intercept from the spec §3 must stay
visible in `/admin/nodes/{n}/calibration`, not be "fixed".

## 7. Admin surface (`ripple/calibration_api.py`, W3)

```python
def build_router(ripple) -> APIRouter
```

Mounted from `proxy.py` in W0 with 501 bodies; W3 replaces the bodies only.

```
POST /admin/nodes/{name}/calibrate[?force=1]
  202 {"job": "<uuid>", "node": "<name>", "node_id": "<uuid>",
       "models": ["<id>", …], "evicts": ["<id>", …]}
  409 {"error": {"message": "…"}}   # busy thread / no models / already running
  401 (no admin key, same as /admin/keys) · 404 (unknown node)

GET  /admin/nodes/{name}/calibration
  200 {"node": …, "node_id": …, "source": "measured" | "default",
       "samples": [{"model": …, "size_gb": …, "seconds": …}],
       "fit": {…}, "calibrated_at": …, "calibrated_by": …,
       "job": {"id": …, "state": "running|done|failed", "step": …, "started_at": …} | null}
```

## 8. Reporting (`ripple/stats.py`, `ripple/proxy.py`, `ripple/dashboard.py`)

Counters declared in W0 (fields + `as_dict()`), incremented by the owning wave:
`loads_supervised`, `probes_skipped`, `loads_failed`, `degraded_pool_decisions`,
`calibrations_started`, `calibrations_ok`, `calibrations_forced`.

- `loads_supervised` counts the loads the dispatch tier caused
  (`source="dispatch"`). `loads_failed` counts every load the state supervised —
  including one RIPPLE found running — that ended without the model becoming
  resident (§12.1). It is therefore **not** a subset of `loads_supervised`: a load
  started in the oMLX UI can fail without that number ever moving.
- `degraded_pool_decisions` counts placement **decisions** that chose a degraded
  host, not requests served from one. A `retry_other_node` hop calls `choose()`
  again, so one client request can count twice. That is the number the operator
  needs — "how often did the flap guard carry a placement" — and the name says it.

`/healthz` per host gains:

```json
"state": "loading",
"loading": {"model": "…", "size_gb": 111.6, "started_at": 1763…,
            "eta_seconds": 32.1, "source": "dispatch"}
```

`state` is `status` under its reporting name; `status` stays for compatibility.
`loading` is `null` when not loading.

## 9. Dispatch hooks (`ripple/proxy.py`, W1)

- After `choose()` returned and before `_forward()`: `_needs_cold_load(decision)`
  reads the **tier of the chosen candidate**, not the reason, and is true for
  `TIER_COLD_LOAD` *and* `TIER_EVICT_IDLE` — an `evict_idle` placement ends in a cold
  load too, and `degraded_host` overrides the reason without changing the tier. Then
  `host.begin_loading(model, size_gb=…, eta_seconds=…, source="dispatch")`,
  `stats.loads_supervised += 1`.
- In the `finally` block of the dispatch loop, after `host.release()`:
  `host.end_loading("request_returned")` if that host is still loading *this*
  request's model. A `retry_other_node` hop ends the state on the old host.
- The bound-thread branch (`proxy.py:350`) tests `in_pool`: a degraded bound host
  must not produce a 503.

**`cold_load`, `evict_idle`, `no_eviction_forecast` — one distinction, stated once.**
Both tokens are loads, and neither is a promise about eviction. `cold_load` is the
client token for a `TIER_COLD_LOAD` placement: a forecast that fits, *or* no forecast
at all (no `residency`, guard off, `estimated_size` unknown, no ceiling) — the
difference is only the decision note (`no_eviction_forecast`, `size_unknown`,
`no_ceiling`), never the header. `evict_idle` is a `TIER_EVICT_IDLE` placement: idle,
unpinned, unheld models are unloaded first and the model then loads. Both count in
`stats.cold_load`, because the increment is taken from the tier before the reason is
read — `stats.evict_idle` is not reached for a dispatched request. A `cold_load`
header therefore says nothing about whether anything was evicted, and an `evict_idle`
header says nothing about how the load was forecast — only the request log line does.

## 10. Corrections from the live run (15.11.2026)

The stub suite was green while four things were wrong, because a stub answers the
way the specification assumed. Recorded here so the next change does not have to
rediscover them (evidence: the live run of 15.11.2026).

1. **`loading_started_at` is a monotonic value, not an epoch** → use
   `ripple.hosts.epoch_seconds()` and fall back to your own clock (§2).
2. **`/load` blocks until the model is resident**, but a queueing backend returns
   immediately — so a measurement runs from issuing the call to the first
   `loaded: true`, never from the wall time of the call alone.
3. **A model the node refuses to load (409) is skipped, not fatal.** `job.skipped`
   carries `[{model, reason}]`; the job only fails when fewer than two samples
   were measured. A drafter in an allow-list is the normal case for this.
4. **The "entry host model" preference applies only while a 3× spread survives**
   without those models — otherwise it can leave a 1,4× range and make a
   calibration impossible.

Additive to §7: the job object gained `skipped` and the GET gained `plan`
(`{models, evicts, error}`), which is what the dashboard's confirmation dialog
shows before the POST.

## 11. Corrections from the adversarial review (15.11.2026)

The adversarial review of this contract is the full report. These are the rules it changed,
and they hold for anything built on this contract:

1. **A load we dispatched is unobserved until oMLX lists it.** `is_loading: false`
   *before* the node has ever said `true` means "not started yet", not "finished".
   Ending the state on that would put a probe inside the load — the defect the
   state exists to prevent. The entry carries `seen_loading`; `omlx_idle` only
   ends a load that was seen running. External loads are entered with
   `seen_loading=True` (we saw them running by definition).
2. **Only the owner ends a loading entry.** The proxy ends `source="dispatch"`,
   the calibration job ends `source="calibration"`. Anything else would stop
   somebody else's supervision mid-flight.
3. **`in_pool` is a positive list** — `status in ("healthy", "loading", "degraded")`.
   A host that was never resolved (`unknown`) has no backend and must not be
   handed a request.
4. **A `down` host stays `down` across a load.** `begin_loading` does not raise it
   and `end_loading` does not revive it: neither is a probe.
5. **Calibration refuses an unhealthy node** (§6.3.1), not only one outside the
   pool. `force=1` still overrides the thread check only.
6. **`guard_enabled` is reported, never written into `status`** — it used to mask
   a `loading` state discovered in the same tick.
7. `job.restore_failed` lists models that would not unload after a failed job, and
   they are appended to `job.error`. A second POST is refused while the task
   object is alive, not while `state == "running"` (the job sets `done` before its
   cleanup).
8. **`warmup:` is inert.** FA-16 is optional and not implemented; NZ-4 forbids
   RIPPLE to load models, and the only exception is the operator-initiated
   calibration. `discovery.unhealthy_after` has no effect either — the pool
   decision is `placement.flap_guard_failures`. Both now produce a config warning.

## 12. The two findings that were left open (issue #5)

Both are about a number telling the truth.

1. **A residency failure is not a load error — it was neither.** `is_loading:
   false` says the node stopped loading, not that the model is there. A load can
   end with nothing to show for it: the model is still `loaded: false`, or it is
   gone from the list entirely. That used to end the state as an ordinary
   `loading_end`, so `/stats` reported every supervised load as successful and
   the only trace was a `reason=` field an operator had to read. Now
   `stats.loads_failed` counts it and one `loading_failed host=… model=… reason=
   … source=… seconds=…` line names it. Two details:
   - **One tick is not a verdict.** `is_loading: false` with `loaded: false` can
     be the boundary between two fields of one answer, so the first such answer
     only sets `idle_unloaded` and the state stays up; the second counts the
     failure. The same rule `_apply_used_bytes()` applies to a single 0, and
     staying in the state one tick longer only keeps a probe out of a load.
   - **The ceiling is not a failure.** It is RIPPLE's own verdict, not the node's:
     the model may still be loading, so it ends the state and counts nothing.
     `end_loading()` stays free of counting for the same reason — the ends it
     serves (`request_returned`, `resolved`, the calibration's own) are not
     verdicts about residency.
2. **`degraded_pool_uses` became `degraded_pool_decisions`.** The increment lives
   in `choose()`, which runs again for a `retry_other_node` hop and for a
   fallback, so one client request can count twice. Counting *uses* would mean
   moving the count into the dispatch path — the path the live run exercised, and
   the review's own argument against a late edit there. The number is useful as a
   decision count; the name and §5/§8 now say which number it is, and
   `test_the_degraded_counter_counts_decisions_not_requests` pins the difference
   (two decisions, one request).

## 9. Amendment 2026-11-20 — preload of locked models (issue #119)

The freeze exists so three waves can touch one object without disagreeing. This
amendment adds a **fourth way into the loading state** and nothing else: every
existing meaning stays exactly as §3–§8 define it.

What it adds:

* `loading.source` gains the value `preload` (§3). A preload is a load RIPPLE
  starts itself, on a *locked* model, because the host became healthy — not
  because a request asked for it.
* `HostRuntime.preload_due: bool` — set on the transition into a healthy verdict
  (beside `relearn_due`, §3.5 of the spec) and on the end of a maintenance
  window; cleared by the probe tick that schedules the pass. It is the trigger,
  not a second health poller.
* `HostRuntime.preloads: dict[str, dict]` — the last attempt per model:
  `{"model", "state": "loading"|"loaded"|"failed", "reason", "size_gb",
  "started_at", "finished_at", "evicts", "displaced", "by"}`. One entry per
  model, the newer attempt replacing the older one. A failure is a **state**:
  there is no retry loop, and the next attempt is the next healthy transition.
* `HostRuntime.displaced: dict[str, dict]` — `{model: {"by", "at", "reason"}}`,
  the models a preload took the memory from. Cleared when the model is resident
  again.
* `/healthz` grows two keys, both on structures that already exist:
  `hosts[].preloads` (the list above) and `hosts[].displaced`;
  `models[].hosts[].evicted_by` names the model whose preload displaced this one
  on this host. The dashboard renders them in the host card and in the model
  row — no new endpoint, and no key without a place on the page.
* `Fleet.schedule_preload(host)` / `Fleet.await_preloads(timeout=None)` — the
  task handle and the deterministic seam. Tests await the in-flight preloads;
  they never sleep long enough for a 432 GB load.

Unchanged on purpose: `in_pool` (a loading host stays in it), the status
vocabulary, the three signals of §5, and routing — a locked, cold model stays
non-routable (issue #116). The full rules are in `docs/loading-state.md` §5a.
