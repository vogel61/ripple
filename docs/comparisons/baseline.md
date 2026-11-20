# RIPPLE — capability inventory

**Date:** Nov 20th, 2026
**Scope:** RIPPLE at commit `531c345` (2026-11-19, version `0.1.0`) — the reference column the other three documents are compared against: functional and non-functional requirements, non-goals, operational surface, what is distinctive, and the known limits.
**Question:** What does RIPPLE actually implement, with `file:line` evidence, and where is the evidence thin or absent?

Every `yes` below is backed by a `file:line` in this repository (commit `531c345`, 2026-11-19, version `0.1.0`) or by a config key or test name. Where the evidence was a docstring claim rather than a test or a measurement, the maturity column says so.

Two notes on reading this table. First, some evidence lives in internal working documents that are **not published** — the acceptance report (`docs/acceptance.md`), the live-run protocols (`docs/as5-live-*`, `docs/loading-state-live-*`). Those references are kept for traceability; the files are not part of this tree, so the paths do not resolve here. Second, this inventory describes 0.1.0. Two entries changed in 0.1.1: `prefer_resident` is now honoured (it was parsed and never read) and `/stats` counts `evict_idle` (the branch was unreachable). See the CHANGELOG.

## 1. Positioning

RIPPLE is an OpenAI-compatible HTTP proxy that sits in front of two or more **oMLX**
instances on one LAN and decides *which machine answers which conversation*, not which
machine answers which request. The unit of placement is the thread: a conversation is
bound to a node on its first turn and stays there, so the prefix cache stays warm, and
the configured weight ratio (3:2 in the reference deployment) emerges from placing *new*
threads on whichever node is furthest below its target share. Before dispatch it also
refuses to choose a node whose memory would evict a model another live thread is using.
It is built for one operator with a handful of Macs, a handful of agent clients
(Hermes, opencode, scripts) and models large enough that a wrong placement costs a
70-second reload. It is explicitly **not** an auth gateway, not a scheduler with
budgets or quotas, not a multi-provider router, not a WAN/multi-site control plane, and
not a model manager: it speaks HTTP, needs no Thunderbolt, patches nothing, and passes
client keys through rather than owning identity (`docs/manual-en.md` §1, `docs/requirements.md` §4).

## 2. Functional capability matrix

`NFA-1..NFA-8` are the non-functional requirements (§7). The `NZ-1..NZ-8` identifiers in
§4 are non-goals, not requirements — they are quoted verbatim in §3 instead of being
scored here.

| ID | Requirement (short) | Implemented? | Evidence | Maturity |
|---|---|---|---|---|
| FA-1 | Single external entry point; `POST /v1/chat/completions` with and without `stream` | yes | `ripple/proxy.py:444` `PROXY_PATHS = ("v1/chat/completions", "v1/completions", "v1/embeddings")`; routes registered `proxy.py:2159-2160`; `listen_host`/`listen_port` in `config.example.yaml:11-12` | tested indirectly by every routing test (`tests/conftest.py` starts a real proxy against stubs); live run in `docs/acceptance.md` ("A real subagent … answered") |
| FA-2 | New thread → node with largest deficit; idle threads fade quadratically; atomic reservation; AND-hysteresis | yes | `ripple/placement.py:166` `_deficit()` (decay via `registry.weighted_slots`, `max(slots, inflight)`); hysteresis `placement.py:306` `imbalance()`; reservation under lock `proxy.py:2594` `async with self._lock` | tested: `test_fa2_atomic_reservation_never_overshoots_one_host`, `test_fa2_idle_threads_fade_quadratically`, `test_fa2_hysteresis_needs_both_thresholds`, `test_as1/as2_*`; AS-1 verified live |
| FA-3 | Weights, `max_inflight`, `spill_threshold`, `thread_ttl_seconds` adjustable in YAML, no code change | yes | `config.example.yaml:42-84` (`hosts[].weight`, `max_inflight`, `spill_threshold`), `:86-99` (`affinity.ttl_seconds`) | tested: `test_fa3_weight_change_shifts_the_distribution` |
| FA-4 | Route only to nodes that offer the model; unknown model → 404 | yes | model map refreshed per host in `ripple/hosts.py` probe loop; single-offer pinning `placement.py:524-526` (`model_pinned`); 404 path `proxy.py:3292` `_error()` | tested: `test_as8_fa4_model_offered_by_one_host_is_pinned_there`, `test_fa4_unknown_model_is_rejected_with_404` |
| FA-5 | `max_inflight` hard cap, `spill_threshold` soft cap; when all full, largest relative headroom instead of rejecting | yes | `placement.py:440` (`inflight >= max_inflight` → ineligible), `:475-482` (`open_cands`, else `min((inflight+1)/weight)`), `:512` note `all hosts at spill_threshold` | tested: `test_fa5_spill_threshold_moves_new_thread_to_the_other_host` |
| FA-6 | Thread detection: header → fingerprint → prefix similarity; must survive context compression | yes, with a documented deviation | `proxy.py:3054` `_identify()`; header `registry.py:583` `key_for_family()`; fingerprint `registry.py:403`; similarity `registry.py:522` `best_prefix_match()` + `registry.py:111` `block_chain()` (issue #99: shared blocks instead of a ratio on the header path) | tested: `test_fa6_compressed_conversation_keeps_its_binding`, `test_fa6_session_header_beats_the_fingerprint`, `test_fa6_unrelated_prompt_opens_a_new_thread` |
| FA-7 | Single short user message (< `affinity.min_prompt_tokens`) holds no slot, releases immediately | yes | `proxy.py:3083` `_is_ephemeral()` (judged on the *whole* prompt, issue #14); window `affinity.ephemeral_window_seconds` in `config.example.yaml:96` | tested: `test_fa7_short_single_message_holds_no_slot_but_a_long_one_does`, `test_as4_twenty_short_requests_split_twelve_to_eight` |
| FA-8 | SSE passed chunk by chunk, no buffering | yes | `proxy.py:3117` `_stream()` yields each chunk; `proxy.py:2901` sets `x-ripple-*` headers before the body | tested: `test_fa8_streaming_arrives_chunk_by_chunk`, `test_fa8_streaming_request_is_a_single_upstream_call`; live AS-7 (6 `data:` chunks, no buffering) |
| FA-9 | No read timeout on running generations; only connect/write bounded | yes | `proxy.py:586-590` `httpx.Timeout(connect=…, read=None, write=…)`; key `proxy.connect_timeout_seconds` (`config.example.yaml:191`) | coded and commented (`# FA-9: no read timeout`); `test_as7_a_slow_stream_is_not_cut_off` covers the stream side, no 15-min test exists |
| FA-10 | Retry only before the first byte, max one attempt per node | yes | `proxy.py:2851-2880` (`tried` set, `len(tried) >= len(self.fleet.hosts)`, `log.warning("retry_other_node …")`) | tested: `test_fa10_retry_on_5xx_before_the_first_byte`, `test_fa10_no_retry_after_the_response_started`, `test_fa10_no_retry_when_both_hosts_fail` |
| FA-11 | If the fallback node is down, everything goes to the remaining node | yes | `proxy.py:2885` `reason = "fallback"`; only in-pool hosts become candidates `placement.py:395` (`pool = [h for h in offered if h.in_pool …]`), else `no_healthy_host` 503 | tested: `test_fa11_as5_fallback_when_one_host_is_down`; AS-5 live |
| FA-12 | Prefer a node where the model is already loaded | yes | `placement.py:107` `residency_tier()` returns `TIER_RESIDENT = 1`, tiers sorted `placement.py:485-487`; key `hosts[].prefer_resident` (`config.py:167`) | tested: `test_fa12_a_warm_node_beats_a_heavier_cold_one` |
| FA-13 | `/healthz` per host (health, models, inflight, weight, slots, last error) + global view; `/stats` counters | yes | `proxy.py:3443` `healthz()`; `proxy.py:3614` `stats_payload()`; `proxy.py:1200/1208` routes | exercised by most integration tests and by `docs/acceptance.md`; no FA-13-named test, no schema test |
| FA-14 | Exactly one log line per request with reason, no prompt content, no keys | yes | `ripple/stats.py:538` `log_request()` (docstring: "Exactly one line per request (FA-14)"); called at `proxy.py:2908/2935/3312` | **thin**: no dedicated format test; the line is asserted only indirectly (`tests/test_client_identity.py`, `test_the_error_line_carries_the_client_too`) |
| FA-15 | Per-upstream key from config (`${VAR}` expanded), otherwise client key passed through; never logged | yes | `proxy.py:3105-3106` (`api_key = host.cfg.api_key or _client_key(request)`); key store `ripple/keys.py:1` (`hmac.compare_digest`, never `==`) | tested: `test_fa15_as10_keys_never_appear_in_stats_healthz_or_logs`, `test_fa15_client_key_is_passed_through_without_an_upstream_key`; AS-10 live |
| FA-16 | `warmup_models` preloads models at start | **no** | `config.py:1902-1907`: the `warmup:` key is parsed, then rejected with a warning — *"warmup … is not implemented"*; `__main__.py:100` and `:179` call `fleet.start(warmup=False)` | declared-but-refused: the key is **inert** (`ripple/config.py:1902`). RIPPLE issues no load command; it dispatches requests, the backend loads on demand, and RIPPLE forecasts and reports the load (`reason=cold_load`, `loading_start`/`loading_end`) and refuses only loads that would evict a locked, pinned or running model. README and the example config now say so (corrected 20.11.2026) |
| FA-18 | Never choose a node whose load would evict a model a live thread uses; 4-tier hierarchy, 503 on conflict | yes | `placement.py:107-160` `residency_tier()` — forecast `sum(estimated_size of loaded) + estimated_size(new)` vs `hard_threshold × ceiling`, LRU order, `TIER_CONFLICT = 4` on pinned/busy/locked; 503 with model+node+blocking model `placement.py:463-471` | tested: `test_fa18_cold_load_goes_to_the_host_that_does_not_evict`, `test_fa18_pinned_model_blocks_the_host_with_503`, `test_fa18_active_thread_blocks_the_host_with_503`; AS-5b deliberately **not** run live (`docs/acceptance.md`) |
| FA-19 | Slots and deficit counted per model, not globally | yes | `registry.py:868` `slots(self, host, model)`; used in `_deficit` and `imbalance`; `hosts[].max_threads` per host | tested: `test_fa19_when_every_slot_is_taken_an_idle_thread_gives_way`, `test_fa19_a_thread_with_a_request_in_flight_never_gives_up_its_slot` |
| FA-20 | `--check` validates config, probes upstreams, prints routing table, serves nothing | yes | `__main__.py:81` `run_check()`, flag `__main__.py:150` `--check` | tested: `tests/test_cli.py` (`test_the_three_first_run_words_are_wired`, `test_each_word_reaches_its_module_with_the_rest_of_argv`) |
| FA-21 | 1:n host→address probing, same-subnet first, `node_id` identity check, re-resolve on demand | yes | `hosts.py:428` `resolve()`; identity mismatch `hosts.py:441-446` (`identity_mismatch`); switch log `hosts.py:471-472` (`addr_switch`); `--reinit` / `POST /reinit` `proxy.py:2152` | tested: `test_as11_fa21_unreachable_first_address_falls_back_to_the_second`, `test_as12_fa21_foreign_node_id_is_rejected`, `test_as13_reinit_keeps_thread_bindings` |
| FA-22 | Read-only dashboard at `/`, no build step, no CDN, reload every 2 s | yes (with 3 audited write exceptions) | `proxy.py:1196` route; shell `ripple/dashboard.py:163-193` (six tabs); assets `ripple/static/dashboard.js` (227 kB), `dashboard.css` (83 kB), fonts shipped locally, 16 i18n catalogues in `ripple/static/i18n/` | tested structurally only: `tests/test_dashboard.py` + `dashboard_dom_stub.js` assert DOM contracts (`test_the_page_carries_no_external_url`, `test_the_shell_carries_six_tabs_with_one_panel_each`), plus a generated snapshot `docs/dashboard-mockup.html`. No browser/visual test |
| FA-23 | Backend adapter; capabilities probed against the live backend, missing ones visible, `residency` absent → eviction guard off | yes | `ripple/backends.py:18` `ALL_CAPABILITIES`, `:261` `probe_capabilities()`, `:335` `merge_capabilities()` (fleet = intersection); surfaced as `eviction_guard` in `/healthz` | tested: `test_fa23_openai_compatible_backend_switches_the_guard_off`, `test_fa23_mixed_fleet_reports_the_intersection_of_capabilities` |
| NFA-1 | TTFB overhead p50 < 5 ms, p95 < 20 ms over 100 requests | **no evidence** | no measurement file records p50/p95 TTFB for RIPPLE itself; `docs/measurements.md` measures decode/prefill and fleet load, not proxy latency. `docs/olla-evaluation.md:182` measures *Olla's* overhead (+45.4 ms), not RIPPLE's | unmeasured. Any competitor comparison on overhead is currently unsupported by this repo |
| NFA-2 | < 300 MB RSS under load | **no evidence** | no `ps`/RSS figure anywhere in `docs/`; only Olla's "< 50 MB RAM" is quoted (`requirements.md:1178`) | unmeasured |
| NFA-3 | No build step; start by command and by double-click | yes | `deploy/start-ripple.command`, `deploy/stop-ripple.command`, `deploy/verify-ripple.command`; `pyproject.toml:46-47` console script `ripple` | the cold-start report (`docs/coldstart-report.md`) is an end-to-end transcript on a clean second Mac, macOS system Python 3.9.6, no Homebrew — the strongest operational evidence in the repo |
| NFA-4 | Runs as a launchd service, starts after reboot | yes | `deploy/ai.ripple.plist`, `deploy/install-launchd.command`; supervision detection in `proxy.py:216-332` (`supervision_state`, `_launchd_owns_pid`) | coded + install script; no automated test of the plist |
| NFA-5 | Standard deps only (FastAPI, httpx, PyYAML, uvicorn) | yes | `requirements.txt` (4 runtime deps); `pyproject.toml` `[project]` | verified by `tests/test_packaging.py` and `tests/test_py39.py` |
| NFA-6 | Tests run without oMLX and without a model | yes | `tests/conftest.py:1` "stub upstreams, RIPPLE instances wired to them"; `README.md:39-40`: 1292 collected, 1290 passed, 2 skipped, 229 s (19.11.2026) | measured once, with a date on it; not a CI badge |
| NFA-7 | No access from outside the LAN; bind address configurable | partial | `listen_host: 0.0.0.0` is the **default** (`config.example.yaml:11`), overridable by `--host` (`__main__.py:154`) | "configurable" yes, "no access from outside" is a deployment promise, not enforced by code. Auth can be switched off entirely (`auth.enabled: false` = every request accepted) |
| NFA-8 | Log rotation, no unbounded log | yes | `__main__.py:68` `RotatingFileHandler(maxBytes=10 MB, backupCount=5)`; duplicate-handler suppression under launchd `__main__.py:37-78` | tested: `tests/test_logging.py` (5 tests, incl. `test_single_handler_when_stderr_is_the_log_file`) |

## 3. Non-goals — verbatim from §4, with the consequence

Quoted from `docs/requirements.md:92-127`. The table after the quotes is this
inventory's addition: what the operator therefore has to bring themselves.

> - **NZ-1** No model splitting across machines (that remains reserved for the oMLX cluster).
> - **NZ-2** No models outside the shared two (DeepSeek-V4.1, Qwen3.5-9B). Other models are passed through, but not distributed.
> - **NZ-3** No copying/distributing of model files, no model management **as part of routing or on RIPPLE's own initiative**. The one exception is the operator-initiated replication job of the FA-22 third amendment: one model, one target, behind a confirmation dialog (`docs/replication.md`). Nothing is copied because a model is missing somewhere — only because an operator asked for it.
> - **NZ-4** No change to oMLX itself, no patches in the app bundle. A calibration load is an API call to oMLX (`POST /v1/models/{id}/load`), not a change to it: operator-initiated, admin-key protected, and the only write the balancer ever makes (see the FA-22 amendment and `docs/loading-state.md` §6.1). **RIPPLE does not change oMLX, but it may place model files for the operator — never on its own, never as part of routing.** That is the replication job (FA-22 third amendment, `docs/replication.md`): it writes a model directory onto another machine and then calls `POST /admin/api/reload` there, both on the operator's explicit instruction and both inside a maintenance window it opened itself.
> - **NZ-5** No request rewriting (prompt manipulation, rephrasing).
> - **NZ-6** No replacement for authentication: the balancer is not an auth gateway, it passes keys through.
> - **NZ-7** **No WAN/cross-site operation in version 1.** The balancer assumes a LAN with single-digit millisecond RTT. Globally distributed nodes are technically possible (it is HTTP), but they need things that are deliberately not built here: an encrypted tunnel (oMLX itself offers no TLS), a placement weighted by RTT and site, a site failure concept and one model copy per site. That is expansion stage M5, not part of acceptance.
> - **NZ-8** **Remote providers are expansion stage M6.** OpenRouter and comparable services are not a load-balancing target (§6.2, FA-23): no fixed capacity, no prefix cache, no memory to protect. They come into play exclusively via explicit policy (allow-list, overflow threshold, cost budget), **off** by default, and never as a silent fallback.

| Non-goal | What the user must bring themselves |
|---|---|
| NZ-1 | If one model does not fit a machine, this tool is the wrong layer; the oMLX cluster (JACCL/TB5) or another inference server is. RIPPLE distributes *requests*, never tensors. |
| NZ-2 | Only models offered by ≥2 nodes get a ratio at all. A single-offer model is pinned to its node (`model_pinned`), so "3:2" is a property of the shared subset only. |
| NZ-3 | Model provisioning stays manual (or `ripple init`-time). The one copy path is operator-triggered `rsync`/SSH replication, disabled by default (`replication.enabled: false`). |
| NZ-4 | Anything oMLX-side — settings, profiles, patches, a real load/unload UI — is out of scope. Calibration and replication are the only writes, both admin-key gated and maintenance-windowed. |
| NZ-5 | Prompt shaping, caching tricks, context pruning, guardrails: not here. RIPPLE forwards the body (minus hop-by-hop headers, `proxy.py:445-447`). |
| NZ-6 | Identity, tenancy, spend limits, per-team quotas: none. `auth:` is an optional static key store (`ripple/keys.py`) with per-key model allow-lists, `hide_foreign_models`, and a loopback exemption — not OIDC, not RBAC, no budgets. |
| NZ-7 | Cross-site/WAN operation: you supply the tunnel, the RTT-aware policy and the site-failure model. RIPPLE assumes single-digit-ms LAN RTT (`requirements.md:117`) and binds `0.0.0.0` by default. |
| NZ-8 | Cloud/OpenRouter overflow, cost budgets, allow-listed remote providers: not implemented. There is no second backend class for remote providers — `OpenAICompatibleBackend` (`backends.py:234`) is a local-API adapter that reports *fewer* capabilities, not a billing-aware remote target. |

## 4. Operational surface

### Endpoints (`proxy.py:1196-2160`)

| Path | Method | Purpose |
|---|---|---|
| `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` | POST | proxied with routing (`proxy.py:444`) |
| `/v1/models` | GET | merged list of healthy hosts, filtered by client key (`proxy.py:2145`) |
| `/healthz` | GET | fleet, per-host state, series, capabilities, threads (`proxy.py:3443`) |
| `/stats` | GET | counters + thread snapshot + host ledger + decision ring (`proxy.py:3614`) |
| `/config` | GET | effective config, secrets reduced to `set`/`none` (`proxy.py:2138`) |
| `/` | GET | dashboard shell (`proxy.py:1196`) |
| `/keys` | GET | key admin page (`proxy.py:1278`) |
| `POST /reinit` | POST | re-resolve all host addresses (`proxy.py:2152`) |
| `/admin/stats/reset` | POST | reset the per-host ledger |
| `/admin/threads/{key}` | DELETE | drop one thread binding |
| `/admin/keys`, `/admin/keys/{name}/{action}` | GET/POST | key CRUD + enable/disable/rotate |
| `/admin/nodes`, `/admin/nodes/{name}` | POST/DELETE | add a node (resolve + read `node_id`), drain-then-remove |
| `/admin/nodes/{name}/disable`, `/enable` | POST | drain out of / back into the pool |
| `/admin/nodes/{name}/maintenance` | GET/POST/DELETE | maintenance windows |
| `/admin/nodes/{name}/models[/{model}]` | GET/POST/DELETE | per-(host, model) policy: `disabled` / `hidden` / lock |
| `/admin/models`, `/admin/models/{model}/lock` | GET/POST/DELETE | model locks (never evict) |
| `/admin/discover` | POST | probe a candidate node before adding |
| `/admin/restart` | GET/POST | supervised restart, refused while `inflight > 0` |
| `/admin/replication/plan`, `/admin/nodes/{name}/replicate`, `…/replication` | GET/POST/DELETE | model copy over `rsync`+SSH, then `POST /admin/api/reload` |
| `/admin/config/{section}` | PATCH/DELETE | runtime policy edits (overlay), never identity |
| `/admin/nodes/{name}/calibrate` | POST | measure load rate (admin key, node must be idle; `ripple/calibration_api.py:603`, mounted as a router) |

### Headers

- **In:** `X-Session-Id` (name configurable via `affinity.header`) — optional, clients without it still work (RB-5); `Authorization: Bearer *** — client key when `auth.enabled`, otherwise passed upstream; `X-Forwarded-For` — believed only from `proxy.trusted_proxies` (`proxy.py:2955-2985`).
- **Out:** `X-Ripple-Host`, `X-Ripple-Reason`, `X-Ripple-Thread` (`proxy.py:2901-2903`), reason ∈ `new_thread`, `thread`, `ephemeral`, `cold_load`, `evict_idle`, `model_pinned`, `fallback`, `rebind`, `slot_evict`, `degraded_host`.

### Config keys (`config.example.yaml`, parsed in `ripple/config.py`)

`listen_host`, `listen_port`, `log_level`, `log_file`; `sites[]` (`id`, `name`, `networks[]`); `hosts[]` (`name`, `node_id`, `role`, `site`, `weight`, `addresses[]`, `models[]`, `api_key`, `max_inflight`, `max_threads`, `spill_threshold`, `hard_threshold`, `prefer_resident`, `tg_tps`, `pp_tps`, `replication.*`); `affinity` (`header`, `ttl_seconds`, `batch_ttl_seconds`, `prefix_chars`, `match_threshold`, `min_prompt_tokens`, `ephemeral_window_seconds`); `placement` (`binding_strategy`, `balance_abs_threshold`, `balance_rel_threshold`, `thread_decay_seconds`, `rebind.*`, `prefill_bias.*`, `flap_guard_failures`, `maintenance_file`, `model_policy_file`, `loading.*`, `optimizer.*`); `auth` (`enabled`, `keys_file`, `admin_key`, `allow_loopback`, `hide_foreign_models`); `replication` (`enabled`, `admin_key`, `ssh_command`, `rsync_command`, `ssh_user`, `model_roots`, `free_space_factor`, `copy_timeout_seconds`, `settings_exclude`, `verify_settings`); `discovery` (`interval_seconds`, `timeout_seconds`, `unhealthy_after`); `proxy` (`connect_timeout_seconds`, `retry_other_node`, `request_log`, `trusted_proxies`); `stats` (`file`, `persist`, `flush_seconds`); `fleet` (`file`, `persist`, `drain_seconds`, `drain_poll_seconds`).

Runtime state lives under `~/.ripple/` (`maintenance.yaml`, `models.yaml`, `keys.yaml`, `fleet.yaml`, `calibration.yaml`, `host_stats.json`) — never in `config.yaml`, which stays the hand-written baseline (`config.example.yaml:213-229`). Note: the `placement.optimizer` block (periodic sweep, its own hourly budget) is parsed in `config.py:434` but has no commented example in `config.example.yaml` — it is documented in `docs/manual-en.md` §4.4 only.

### Dashboard (`ripple/dashboard.py`, `ripple/static/`)

Six tabs, hash-routed (`#fleet`, `#models`, `#threads`, `#history`, `#ops`, `#nodes`):
**Fleet** (card per host: status, oMLX version, memory bar vs ceiling, inflight, slots vs target, RTT, tok/s, 15-min tachograph of `n·rate(n)` vs `rate(n)`), **Models** (one row per (model, host), est./actual size, context, running requests, slots, tok/s, per-model policy and locks), **Threads** (registry), **History** (ring buffer of the last 200 routing decisions, filterable by thread/reason/host), **Operations** (maintenance, calibration, replication, restart, keys), **Nodes** (runtime fleet: add/disable/remove). Assets are local — no CDN, no framework, no build step; UI text from 16 shipped catalogues (`ripple/static/i18n/*.json`: cs, de, el, en, es, fr, hr, hu, it, pl, pt, ru, sk, sl, tr, zh).

### CLI

- `ripple` (or `python -m ripple`) — run the proxy; flags `--config/-c`, `--check`, `--reinit`, `--host`, `--port`, `--log-level`, `--version` (`__main__.py:148-157`).
- `ripple init` — first-run wizard: writes `config.yaml`, `keys.yaml`, generates a 32-hex admin key (`ripple/init.py`).
- `ripple discover` — prints address, `node_id`, model count, latency for candidate nodes (`ripple/discover.py`).
- `ripple doctor` — one line per check, `OK`/`WARN`/`FAIL`/`SKIP`, exit code for scripts; port free, key-store mode, real completion (`ripple/doctor.py:1-20`).
- Console script `ripple = "ripple.__main__:main"` (`pyproject.toml:46-47`); launchd via `deploy/install-launchd.command`.

### Logging

One line per request through `log_request()` (`stats.py:538`): time, model, host, reason, prompt size, duration, status, thread + thread source (`sid`/`fp`/`sid+fp`), client address, never prompt content or keys. Rotating file handler 10 MB × 5 (`__main__.py:68`), `access_log=False` (`__main__.py:206`), duplicate-suppression when launchd already redirects stderr into the same file.

## 5. What is genuinely distinctive

1. **Residency-aware admission that reproduces the backend's own arithmetic.** `placement.py:107-160` computes `forecast = Σ estimated_size(loaded) + estimated_size(new)` against `hard_threshold × ceiling`, walks oMLX's LRU order to name the exact models that would be evicted, and returns `TIER_CONFLICT` (HTTP 503 naming model, node and blocking model) rather than evicting a live thread. Nothing else in this class does it by *estimate* rather than by observed usage — and the estimate is the number oMLX itself uses (422.5 GB vs 402.6 GB actual for DeepSeek, `requirements.md:849`). A generic "max loaded models" or "least connections" knob cannot express it; the same measurement in `docs/olla-evaluation.md` §5 caught Olla evicting a running model.
2. **Thread identity that survives context compression.** Three-tier detection (header → fingerprint over model + first user message → prefix similarity, `proxy.py:3054`), plus `block_chain()` (`registry.py:111`) counting shared blocks on the header path because a character ratio over a 4096-char slice cannot localise a divergence in a 200 k-char prompt. Evidence that this is not cosmetic: `test_fa6_compressed_conversation_keeps_its_binding`, and the denominator had to be changed from the spec's literal form (`registry.py:447-455`) for the 0.5 threshold to be reachable at all.
3. **Idle-decay weighted deficit (quadratic, not linear).** `_deficit()` (`placement.py:166`) weights a thread `(1 − idle/thread_decay_seconds)²`, clamped, with in-flight work always at 1.0 and `max(slots, inflight)` so a 225-second generation is never invisible. This was derived from a measured failure — the entry node's own agent sessions kept it looking loaded and every new conversation went to the weaker node (`requirements.md:618-638`) — and `test_fa2_idle_threads_fade_quadratically` pins the curve. A plain `least_connections` or `round_robin` has no state to decay.
4. **Rebind priced in seconds against measured prefill and decode rates.** `plan_rebind()` (`placement.py:696`) and `decide_migration()` (`placement.py:1157`) compare `gain = completion_tokens × (1/tg_own − 1/tg_target)` with `cost = prompt_tokens / pp_target`, gated by per-thread cooldown (600 s), a global hourly budget (`max_per_hour: 4`) and a measured interquartile "band" from the last five scans (`docs/manual-en.md` §4.1-4.2). The rates are per node and per model, measured on that node's own storage path (`ripple/calibration.py:1-17`), because a hard-coded rate "would be wrong for every setup but one".
5. **Capability probing that disarms requirements visibly instead of pretending.** `backends.py:261` probes the running backend; `merge_capabilities()` (`backends.py:335`) takes the *intersection* across the fleet; a missing `residency` turns FA-18 off and says so in `/healthz` (`eviction_guard: false`), a `WARN` line and a dashboard banner. `test_fa23_openai_compatible_backend_switches_the_guard_off` asserts the degradation path, not the happy path. Config-flag-based feature toggles elsewhere degrade silently.
6. **Address is transport, `node_id` is identity.** `hosts.py:428-475` probes addresses in order (same-subnet first, regardless of configured order), requires `GET /api/cluster/node_id` to match the configured UUID, rejects a mismatch as `identity_mismatch`, logs every switch as `addr_switch`, and re-probes on demand. Slots and affinity hang on the host *name*, so a DHCP reshuffle or a `--reinit` keeps every thread binding (`test_as12_fa21_foreign_node_id_is_rejected`, `test_as13_reinit_keeps_thread_bindings`). Most proxies treat a URL as the identity.
7. **Per-model accounting and per-(host, model) operator policy.** Slots, deficit and hysteresis are all keyed by `(host, model)` (`registry.py:868`, `placement.py:306`), so a node that cannot serve a model does not accumulate an unpayable deficit. On top of that sit `disabled`/`hidden` policies, model locks and maintenance windows, all persisted to `~/.ripple/*.yaml` and applied without a restart — routing state RIPPLE owns, never written into oMLX.
8. **A no-build, offline, 16-language dashboard whose DOM contract is tested.** `dashboard.py:163-193` + `static/` (227 kB JS, 83 kB CSS, shipped fonts, no external URL — `test_the_page_carries_no_external_url`), 16 i18n catalogues, structural tests over a DOM stub plus a generated snapshot (`docs/dashboard-mockup.html`). Read-only except three named, admin-key-gated, confirmation-dialogued writes (calibrate, policy/maintenance, replicate), each with its own doc.

## 6. Known limits and rough edges

**From the docs' own admissions**

- **"What the weighting cannot do"** (`README.md:97-107`): single-offer models go 100 % to their node, so 3:2 applies only to the shared subset; the weaker node must keep shared models warm or every spill pays a load; the eviction guard needs `estimated_size` and a ceiling and switches itself off without them.
- **"Where it does not help"** (`docs/manual-en.md:295-312`): one client with one thread cannot be spread; a very long context is rarely worth moving (`cost = whole prompt`); a node without a measured curve is invisible as source *and* destination; a fleet with no idle thread has nothing to move; **"it does not make a single request faster."**
- **Single point of failure, by design** (RB-2, `requirements.md:1063`). The documented emergency path is pointing the client at `:8000` directly. No HA, no health-checked pair, no shared state.
- **No persistence of routing state.** A restart drops every thread binding and every in-flight stream (AS-9, `docs/acceptance.md` "What is not automated"). The 15-min tachograph series is in-memory, one hour deep, cleared on restart (`requirements.md:1015-1016`).
- **Warmup is advertised but inert.** `ripple/config.py:1902` warns `warmup … is not implemented` and `fleet.start(warmup=False)` is called unconditionally, while `README.md` and the config table now say so (corrected Nov 20th, 2026). What RIPPLE does instead of preloading: it issues no load command, dispatches the request, lets the backend load on demand, and forecasts and reports that load (`reason=cold_load`, `loading_start`/`loading_end`).
- **The `match_ratio` denominator deviates from the spec.** The spec divides by the length of the incoming request; the code divides by the shorter side (`registry.py:447-455`) because the literal form could never reach 0.5 for a 200 k-char prompt. Defensible, but it means the shipped behaviour is not the specified behaviour, and the header path now uses a different mechanism (`block_chain`) entirely.
- **"Read-only dashboard" is a claim with three exceptions**, all documented (calibrate, maintenance/model policy, replication). The third writes to another machine's filesystem over SSH and triggers a reload that unloads every model there.
- **Auth is optional and off by default.** `auth.enabled: false` means every request is accepted (`keys.py:1-9` records that this was measured against the live service on 14.11.2026: a request without an auth header answered `200` in front of a backend that answers `401`). `admin_key: "CHANGE-ME"` is the example value.
- **The eviction guard's most important acceptance scenario was never run live.** AS-5b (both nodes holding DeepSeek, request for a third model) is stub-tested only, because a mistake would evict the running session (`docs/acceptance.md`).
- **RB-6**: the 3:2 weights are a specification, not a measurement, and AS-6's clean measurement was taken while the agent session was running on the M5, so the decode numbers are a lower bound (`docs/measurements.md`).
- **LAN-only, single-digit-ms RTT assumed** (NZ-7). No TLS anywhere — oMLX offers none, so an operator who needs encryption must add a tunnel RIPPLE does not know about.
- **`0.0.0.0` is the default bind.** NFA-7 is satisfied by a config field, not by an enforcement mechanism.

**Rough edges visible in the code**

- 24 modules / ~19.9 k LOC of production Python (of which `proxy.py` is 3 679 lines and `config.py` 1 952) — the routing core is `placement.py` (1 327) + `registry.py` (926); everything else is operator surface (dashboard, keys, replication, calibration, fleet overlay, doctor). The surface-to-core ratio is high for a 0.1.0.
- Many behaviours are driven by hand-tuned constants with measurement provenance: `balance_abs_threshold: 1` (with an explicit note that vllm-router's 32 would never trigger for two Macs, `requirements.md:686-694`), `thread_decay_seconds: 300`, `safety_factor: 1.5`, `min_gain_seconds: 2.0`, `slot_seconds: 60.0`, `flap_guard_failures: 3`. Each is justified in a comment, none is auto-tuned.
- Two hourly budgets and two cooldowns (`placement.rebind.max_per_hour` vs `placement.optimizer.max_moves_per_hour`) exist side by side and are deliberately separate (`proxy.py:598-605`) — correct, but an operator has two knobs to reason about for one behaviour.
- German comments and docstrings are mixed with English throughout the source and in `dashboard.py`; the docs are English-first but not English-only.
- Test coverage is broad (64 files, 1 225 `def test_` functions, 1 292 collected) but uneven per requirement: FA-2/FA-6/FA-10/FA-18/FA-21 are tightly pinned by name; FA-9, FA-13 and FA-14 have no dedicated test; FA-22 is tested as DOM structure, not as a rendered page; NFA-1 and NFA-2 have no test and no measurement.

## 7. Maturity signals

| Signal | Value | Source |
|---|---|---|
| Version | `0.1.0` | `ripple/__init__.py:6`; `CHANGELOG.md` `[0.1.0] - 2026-11-19` |
| Last commit | `531c345` — "packaging: the distribution names its public home (#65)", 2026-11-19 | `git log -1` |
| History | 406 commits, no tags | `git log --oneline \| wc -l`, `git tag` (empty) |
| Tests | 1 292 collected — 1 290 passed, 2 skipped, 229 s (19.11.2026); 1 225 `def test_` in 64 files | `README.md:39-40`; `tests/*.py` |
| Test independence | stub upstreams, no oMLX, no model, no network | `tests/conftest.py:1-36` |
| Licence | MIT | `LICENSE`; `pyproject.toml` |
| Distribution | `ripple` (package) + `ripple-proxy` (PyInstaller build), console script `ripple` | `pyproject.toml:37-47`, `ripple/build.py` |
| Measured reports | `docs/measurements.md` (per-node decode/prefill 14.-16.11.2026 + multi-day fleet run 14.11.2026), `docs/coldstart-report.md` (end-to-end install on a clean second Mac, macOS 27.2.1, system Python 3.9.6, no Homebrew/uv/pipx), `docs/olla-evaluation.md` (Olla v0.1.0/v0.0.29 measured against the same two nodes; 3:2 not representable, +45.4 ms overhead, residency ignored), `docs/acceptance.md` (AS-1..AS-13 mapped to test names, AS-1/3/7/10 also live), `docs/as5-live-*.md` (3 live fallback rehearsals), `docs/loading-state-live-2026-11-15.md` | `docs/` |
| Benchmarks in tree | `tools/as6_bench.py` (decode), `tools/prefill_bench.py` (cold prefill), `tools/load_report.py` (fleet), raw JSON in `tools/as6/results/` | `docs/measurements.md:13-15` |
| Documentation | ~40 files in `docs/` incl. a full operator manual (`docs/manual-en.md`, 9 chapters + troubleshooting), install (EN/DE), dashboard design, replication, model policy, sites, loading-state contract, 8 handover documents | `docs/` |
| CI | none in the repository (no `.github/workflows`) | `ls .github` → absent |

---

Part of the competitive analysis: [docs/competitive-analysis.md](../competitive-analysis.md). Licence of this text: MIT, like the project.
