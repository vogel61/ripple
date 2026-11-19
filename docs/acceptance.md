# Acceptance record

**Date:** 14 November 2026
**State:** M2 and M3 delivered. `pytest -q` → **39 passed in 21 s**, no oMLX, no
models, no network needed.

This document maps every acceptance scenario of `docs/requirements.md` §9 to the
test that covers it and to the evidence that was measured against the live fleet.
It exists so that acceptance is a checklist, not a conversation.

## Scenarios

| ID | Expectation | Covered by | Status |
|---|---|---|---|
| AS-1 | 5 threads → 3 / 2 | `test_distribution.py::test_as1_five_threads_split_three_to_two` | automated + live |
| AS-2 | 10 threads → 6 / 4 | `test_distribution.py::test_as2_ten_threads_split_six_to_four` | automated |
| AS-3 | 5 turns of one conversation stay on one node | `test_affinity.py::test_as3_five_turns_of_one_conversation_stay_on_one_host` | automated + live |
| AS-4 | 20 short requests → 12 / 8, afterwards 0 slots | `test_distribution.py::test_as4_twenty_short_requests_split_twelve_to_eight` | automated |
| AS-4b | after the TTL the distribution starts at zero again | `test_operation.py::test_as4b_expired_threads_release_their_slots` | automated |
| AS-5 | M3 stopped → 10/10 on M5, `/healthz` shows `down` | `test_routing.py::test_fa11_as5_fallback_when_one_host_is_down` | automated |
| AS-5b | DeepSeek on M5, request for Qwen3.8 → 10/10 to M3, no eviction | `test_eviction.py::test_fa18_cold_load_goes_to_the_host_that_does_not_evict` | automated (stub) |
| AS-5c | both nodes hold DeepSeek → 503 with conflict details | `test_eviction.py::test_fa18_pinned_model_blocks_the_host_with_503`, `…active_thread_blocks_the_host_with_503` | automated |
| AS-6 | the measured tok/s ratio justifies the weights | `docs/measurements.md` | measured |
| AS-7 | a long stream runs through, no cut-off | `test_operation.py::test_as7_a_slow_stream_is_not_cut_off` | automated + live |
| AS-8 | request for an M5-exclusive model → 10/10 M5, reason `model_pinned` | `test_routing.py::test_as8_fa4_model_offered_by_one_host_is_pinned_there`, `…placement_decides_model_pinned`, `…reports_model_pinned_as_the_request_reason` | automated |
| AS-9 | restart breaks running streams, new requests run | — | documented, by construction |
| AS-10 | no key in logs or `/stats` | `test_keys.py::test_fa15_as10_keys_never_appear_in_stats_healthz_or_logs` | automated |
| AS-11 | unreachable first address → the second is used | `test_resolution.py::test_as11_fa21_unreachable_first_address_falls_back_to_the_second`, `…requests_keep_working_while_the_first_address_stays_dead` | automated |
| AS-12 | foreign `node_id` → host rejected, no request to it | `test_resolution.py::test_as12_fa21_foreign_node_id_is_rejected`, `…no_usable_host_never_reaches_the_foreign_instance` | automated |
| AS-13 | `--reinit` after an address change, bindings preserved | `test_operation.py::test_as13_reinit_keeps_thread_bindings` | automated |

## Evidence measured against the live fleet

Run on 14.11.2026 against M5 (`127.0.0.1:8000`, weight 3) and M3
(`192.0.2.12:8000`, weight 2), RIPPLE on `:8010`.

- **AS-1** — 5 threads, 2 turns each: 3 on `M5-alpha`, 2 on `M3-beta`.
- **AS-3** — 5 turns of one conversation: 5/5 on the same host.
- **AS-7** — streaming answered with 6 `data:` chunks and `[DONE]`, no buffering.
- **AS-10** — `/stats`, `/healthz` and the log searched for the key: no hits.
- `/healthz`: both hosts `healthy`, `eviction_guard: true`, weights 3 / 2.
- `/v1/models`: 3 models, the per-host allow-list applied.
- `/stats` after the run: `requests_total 9`, `requests_ok 9`, `errors_total 0`.
- A real subagent with `delegation.base_url = http://localhost:8010/v1` answered;
  the call shows up in `/stats` as a DeepSeek request on the M5.

## What is not automated, and why

- **AS-5b live** — proving it live means asking for Qwen3.8 (104 GB) while both
  nodes hold DeepSeek. RIPPLE must refuse or route to the free node; a mistake
  would evict the running session. The stub test asserts the stronger property:
  the blocking host receives **zero** upstream calls and the request goes to the
  node with the headroom. The live run is deliberately left for a maintenance
  window with Richard present.
- **AS-6 idle** — the M5 numbers were taken while the agent session was running on
  it, so they are a lower bound (documented in `docs/measurements.md`). A
  clean measurement needs an idle M5, which means no interactive session.
- **AS-9** — a restart drops the in-memory thread bindings and any stream in
  flight. That is the documented behaviour (no state is persisted, FA-6/NFA-6);
  there is nothing to test beyond the absence of persistence.
- **AS-8 live** — every model in the current allow-list is served by both hosts,
  so no M5-exclusive model exists to ask for. The reason label is covered by the
  stub tests.

## Functional requirements without their own scenario

`test_distribution.py` (FA-2 atomic reservation, hysteresis, FA-3 weights,
FA-5 spill threshold), `test_affinity.py` (FA-6 compressed conversation,
fingerprint vs. header, FA-7 short requests), `test_routing.py` (FA-4 unknown
model, FA-10 retry rules, allow-list), `test_eviction.py` (FA-12 residency
preference, FA-18 forecast, FA-23 capability intersection), `test_keys.py`
(FA-15 pass-through), `test_streaming.py` (FA-8 one upstream call per stream).

**FA-12** — a warm node beats a heavier cold one: a cold node with weight 3 does
not see a request while the warm node with weight 1 still has a slot; only when
it is full does the cold one take over, and then the reason is `cold_load`.

**FA-5 (capacity and spillover), live** — measured, not assumed, in
`docs/concurrency.md`. With the hand-set values (`max_inflight: 3`,
`spill_threshold: 2` on the M5) a burst of 9 concurrent 9.2k-token requests put 6
on the M3 and 3 on the M5 — the opposite of the 3:2 target — and took 66.5 s.
With `6 / 4` the split became 5 / 4 and the burst finished in 45.2 s. The M5
saturates at ~2.5–2.7 k prefill tokens/s from 3 concurrent requests on, so more
slots there add latency, not throughput. `max_threads` turned out to be the other
half of admission: a thread holds its slot until its TTL expires (FA-19), so with
`max_threads: 12` and 24 live threads both hosts answered `503 no_slot (12/12
slots)` while nothing was in flight — 20 per host, verified with 12 concurrent
requests (6/6, no 503).

**Eviction instead of refusal (added 14.11.2026).** Raising `max_threads` only
moves the wall; a fleet that has been used for a while always ends up with slots
held by finished conversations, and a finished conversation has no prefix cache
worth protecting. When no host has a free slot but at least one has an *idle*
thread, RIPPLE now drops the least recently used one and answers the request
(`x-ripple-reason: slot_evict`, counter `threads_evicted`) instead of `503`. A
thread with a request in flight is never a candidate — that request would lose
its prefix cache mid-stream. Eviction is the last resort, not the first choice: a
host with a free slot always wins, even if it means a cold load on the other node,
so FA-12 keeps its meaning. Covered by
`test_fa19_when_every_slot_is_taken_an_idle_thread_gives_way` and
`test_fa19_a_thread_with_a_request_in_flight_never_gives_up_its_slot`; verified
live on `:8010` with `max_threads: 2` per host — a sequential request came back
`200 slot_evict` on the M5 with `threads_evicted: 1`, and the 5-request burst
before it answered `503` only because all four slots belonged to *running*
requests at that moment (the guard doing its job).

**FA-16 (warmup)** is optional and **not implemented**: NZ-4 forbids RIPPLE to
load or unload models, so a `warmup` list would contradict the design. The key is
still accepted — but a non-empty list produces a visible warning instead of
silently doing nothing.

**FA-13 (`/healthz`)** — spot check against the live instance: per host `healthy`,
`status`, `weight`, `target_share`, `address`, `identity_ok`, `guard_enabled`,
`ceiling_bytes`, `used_bytes`, `used_ratio`, `inflight`/`max_inflight`,
`thread_slots`/`max_threads`, `target_threads`, `rtt_ms`, `version`,
`models_loaded`, `models_available`, `last_error`; globally `threads.active`,
`threads.target_share`, `threads.actual_share`, `capabilities`, `warnings`.
Measured tok/s appears under `hosts[].metrics[<model>].tg_tps` after the first
response (spot check: 100,7 tok/s for the 9B on the M5).

**FA-22 (dashboard)** — checked in a real browser, not only by HTTP status: four
sections in the required order, 2 fleet cards, 30 model rows grouped by model
with `est./act.`, context, running requests, slots and tok/s, the distribution as
bars with target 60 % / 40 % against the actual share, and the last decisions with
time, model, host, reason, prompt size, duration and status. No banners (no
warnings), no JavaScript errors, no layout breakage.

A fifth section, **Parameters**, shows the *effective* configuration — the values
in force after defaults, `${VAR}` expansion and validation — via `GET /config`:
listen address, config file, log target, placement strategy and hysteresis,
affinity (header, TTL, slot threshold, prefix match, ephemeral window), discovery,
proxy behaviour and per host weight → share, inflight/thread caps, spill and hard
threshold, allow-list and whether a key is set. Read-only by design: a weight that
changed under a running thread would pull a conversation off its prefix cache, so
changing a value means editing the config and restarting (FA-3/FA-22). Secrets
never appear — the key is reduced to `set`/`none`, and the AS-10 test now searches
`/config` as well.

The dashboard distinguishes what `/healthz` reports: of the 23 models the M5
offers, 3 are loaded and only 2 may be routed to. The other 21 carry a
`not routable` tag — otherwise "offered by oMLX" and "may be used here" look
identical, and the allow-list is the contract.

Two defects were found by this suite and fixed: the per-host allow-list was not
enforced during placement, and the placement reason `model_pinned` was
overwritten with `new_thread`. A third came out of writing the AS-4b test: the
slot counters included threads whose TTL had passed, so for up to 30 s (the purge
interval) the deficit rule kept routing around an idle thread.

---

# Loading state, flap guard, calibration — acceptance, 15.11.2026

Specification: `docs/loading-state.md` §12. Evidence: the stub suite plus the
live run in `docs/loading-state-live-2026-11-15.md`.
`pytest -q` → **261 passed**, no oMLX, no network, no model needed.

| # | Expectation (§12) | Covered by | Status |
|---|---|---|---|
| 1 | A request sent immediately after a cold load completes is served, not refused | `test_loading_state.py::test_loading_clears_when_the_request_returns` (second request → 200, reason `cold_load`/`model_pinned`, never 503); live: 10/10 after a 24,5 s cold load | **live + automated** |
| 2 | During a cold load `/healthz` reports `loading` with model, size and ETA, and no probe failure is counted | `test_loading_state.py::test_cold_load_suspends_the_probe_and_keeps_the_host_in_the_pool`, `…_probes_are_suspended_for_an_external_load`; live: `state=loading eta=32s probes_skipped=2`, `healthy` stayed true | **live + automated** |
| 3 | One or two missed probes keep the host usable and report `degraded`; three in a row make it `down` | `test_flap_guard.py::test_one_soft_failure_degrades_the_host_but_keeps_it_usable`, `…test_two_soft_failures_stay_degraded`, `…test_three_soft_failures_take_the_host_out_of_the_pool`, `…test_a_refused_connection_drops_the_host_at_once` | automated |
| 4 | An uncalibrated node uses the M3-Ultra defaults and reports `source: default` | `test_calibration.py::test_a_missing_file_means_defaults`, `test_calibration_api.py::test_plan_is_a_dry_run_available_before_any_job` (asserts `source: "default"` on the GET); live: the same GET on M3-beta before the job | **live + automated** |
| 5 | After calibration the stored samples are used and the ETA changes accordingly | `test_calibration_api.py::test_end_to_end_writes_the_measured_curve_to_the_calibration_file` (the host's estimate is now computed from the stored curve, not just different), `test_calibration.py::test_estimate_interpolates_between_two_samples`; live: 3 samples (6,54 / 111,61 / 453,64 GB) stored under the `node_id`, estimate for 453,6 GB moved 170,3 s → 142,2 s | **live + automated** |
| 6 | The button is admin-key protected and refuses on a bound thread without `force` | `test_calibration_api.py::test_12a_both_endpoints_require_the_admin_key`, `…test_9_a_bound_thread_refuses_and_force_overrides`, `test_dashboard_loading.py::test_calibrate_is_disabled_while_the_node_holds_a_thread` | automated |

## Scenarios that existed before this change and must not regress

| ID | Expectation | Covered by | Status |
|---|---|---|---|
| AS-5 | M3 stopped → 10/10 on M5, `/healthz` shows `down` | `test_routing.py::test_fa11_as5_fallback_when_one_host_is_down` | automated, unchanged |
| AS-5b | cold load goes to the host that does not evict | `test_eviction.py::…`, live run above | **live + automated** |
| AS-5c | both nodes hold DeepSeek → 503 with conflict details | `test_eviction.py::…`, live run above | **live + automated** |

## What the live run added to the record

Four defects that the stub suite could not see were found and fixed (details in
`docs/loading-state-live-2026-11-15.md` §4): oMLX's `loading_started_at` is a
monotonic value and not an epoch; `/load` blocks until the model is resident; a
drafter in the allow-list cannot be loaded alone and must be skipped rather than
abort the job; and the "entry host model" preference could remove the only small
model and make a 3× calibration impossible.

The adversarial review (W5, `docs/loading-state-review.md`) found one defect the
live run could not show — the supervision could be dropped by a probe tick that
landed before oMLX had registered the load — plus a set of smaller ones. They are
fixed and covered: `test_loading_signals.py::test_a_load_the_node_has_not_started_yet_keeps_its_supervision`
(that race), `…test_a_host_without_an_eviction_guard_still_reports_loading`,
`test_loading_state.py::test_ending_a_load_does_not_revive_a_host_that_was_down`,
`test_calibration_api.py::test_an_unhealthy_node_is_not_calibrated`,
`…test_the_curve_follows_the_node_id_not_the_host_name` (§10 case 13).

The first production cold load (10:44, `reason=evict_idle`) found a third one:
the dispatch hook only supervised the `cold_load` tier, so a node that evicts an
idle model first — the normal case on a node with a big resident model — went
unsupervised for one probe interval (14 s of a 28 s load). Fixed and covered by
`test_eviction.py::test_an_evict_idle_load_is_supervised_like_a_cold_one`; the
request itself was served (28,2 s, then 0,4 s, no 503).

It also answered the open question of spec §3: the fixed load cost is **positive
and about 23 s**, not the −10 s a two-point linear fit suggested — measured with a
6,5 GB model that took longer to load than a 111,6 GB one (32,5 s vs 26,4 s).
