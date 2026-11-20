# LiteLLM — technical profile

**Date:** Nov 20th, 2026
**Scope:** LiteLLM (`github.com/BerriAI/litellm`) at v1.110.2 and `main` — the proxy server, Router and SDK; every routing strategy, the three affinity mechanisms, residency and model lifecycle, operations, licence and commercial split.
**Question:** Can LiteLLM place *threads* on a two-node Apple-Silicon fleet where each node holds a different 400 GB model — and can it refuse a request that would evict one?

**No benchmarks were run for this document;** every number below is either quoted from a source
or marked as unverified.
**Version examined:** `litellm` **v1.110.2** (PyPI upload 2026-11-20T04:05:22Z, GitHub release
`v1.110.2` published 2026-11-20T04:06:19Z), plus `main` at 2026-11-20 for docs and source-code
claims. The LTS line `v1.108.13` and the release candidate `v1.111.0-rc.3` also exist as of the
same day.
**Repository:** `github.com/BerriAI/litellm`, default branch `main`, last push
2026-11-20T07:26:44Z.

**Evidence labels used throughout:**

| Label | Meaning |
|---|---|
| **Documented** | Stated on `docs.litellm.ai` for the version above. |
| **Source** | Read directly in the repository source at `main`. |
| **Vendor-measured** | A number published by LiteLLM/Berri AI about its own product. |
| **Third-party** | Measured by someone other than the vendor. |
| **Unverified** | Could not be confirmed from primary sources. Treated as absent, not as false. |

---

## 1. What it is and where it comes from (project, license, versions, stars, maintainers, funding/enterprise model)

LiteLLM is an OpenAI-format gateway and Python SDK for "100+ LLM providers". It has two shapes:
a **Python SDK** (`import litellm`) and a **proxy server / AI Gateway** (`litellm --config ...`),
which is the part relevant to a comparison with a reverse proxy (Documented: README).

| Fact | Value | Source |
|---|---|---|
| Repository created | 2023-07-27 | GitHub API |
| Stars | 63,589 | GitHub API, 20.11.2026 |
| Forks | 13,439 | GitHub API |
| Watchers (subscribers) | 238 | GitHub API |
| Open issues + PRs | 5,299 | GitHub API |
| Contributors | ~1,960 (pagination `per_page=1&anon=true` → 1,960 pages) | GitHub API |
| PyPI releases | 968 | PyPI JSON API |
| Current stable | v1.110.2 (2026-11-20) | GitHub releases, PyPI |
| LTS line | 1.108.13 ("LTS") | GitHub releases |
| Python requirement | `>=3.10, <3.16` | PyPI metadata |
| Company | Berri AI, YC W23 | README badge link |
| Founders' contact in README | `ishaan@berri.ai`, `krrish@berri.ai` | README |

**Funding / enterprise model.** Berri AI runs a commercial tier sold as a **license key on the same
image**: "Enterprise is a license key on the same Gateway image you may already run" (Documented:
`/docs/enterprise`). Pricing is not public — "Pricing depends on your deployment size", "Pricing is
based on usage", both pointing at a sales form. Procurement is offered through AWS and Azure
Marketplace. The company states SOC 2 Type II and publishes a Trust Center. There is no published
funding round in the material reviewed — **unverified**.

**Notable 2026 repositioning.** The GitHub description now reads "The fastest, litest AI Gateway.
**Rust core with Python SDK**". The docs contradicted that for the chat path, until the page was
deleted: as of 2026-10-30, `/docs/proxy/rust_gateway` said "the Python proxy serves only two routes
on Rust: `/ocr` for every provider and `/audio/transcriptions` for `bedrock` … Every other route,
including `/v1/messages` and `/chat/completions`, runs on Python, and the per-model `rust: true`
flag described below has no effect" (Source: `BerriAI/litellm-docs`, `docs/proxy/rust_gateway.md`
at commit `f9d4a4d2`; the page was removed on 2026-11-10 by commit `fabf8478` and the URL now
404s). Its replacement, `docs.litellm.ai/rust-migration`, publishes progress instead of per-route
coverage: 53 % of "Major APIs" built in `v1.111.0-rc.2`, with `/v1/messages` end-to-end on Rust a
**target for 2026-12-01**, not a shipped path. Treat "Rust core" as roadmap, not as the state of
the request path.

*Fact-checked against primary sources on Nov 20th, 2026; corrections applied — see `docs/comparisons/factcheck.md`.*

---

## 2. Architecture (proxy server, Router, SDK; what a request passes through; config format; state stores)

**Three layers** (Documented: `/docs/routing`, `/docs/proxy/load_balancing`):

1. **SDK** — `litellm.completion/acompletion(model="provider/model", ...)`. Provider translation,
   error mapping into `litellm.*Error` classes, cost calculation.
2. **Router** — `litellm.Router(model_list=[...], routing_strategy=...)`. Owns deployment selection,
   retries, fallbacks, cooldowns, pre-call checks.
3. **Proxy** — a FastAPI/uvicorn app exposing `/v1/chat/completions`, `/v1/responses`,
   `/v1/embeddings`, `/v1/messages`, `/v1/models`, `/health`, `/key/*`, `/model/*`, `/config/update`,
   plus pass-through relay routes (`/vllm`, `/azure`, `/azure_ai`, `/nvidia_nim`) and a dashboard.

**Request path** (composed from the pages cited in §3–§6): client → auth (master key or virtual key,
optional JWT/SSO) → **pre-call checks** (`optional_pre_call_checks`, e.g. `session_affinity`,
`prompt_caching`, `enforce_model_rate_limits`, `encrypted_content_affinity`) → **routing strategy**
picks one deployment from the model group → provider adapter → response, with success/failure
callbacks for logging, spend and Prometheus. The response carries `x-litellm-model-id` (which
deployment served it) and, per the benchmark methodology, `x-litellm-overhead-duration-ms`
(Documented: `/docs/routing`, `/docs/benchmarks`).

**Config format** — one YAML with four top-level sections: `model_list` (deployments, each with
`model_name` = public alias, `litellm_params` = provider/model/api_base/api_key/rpm/tpm/weight,
`model_info` = id, region, capability flags), `router_settings` (strategy, retries, fallbacks,
cooldowns, Redis), `general_settings` (master key, database, health checks, `store_model_in_db`),
`litellm_settings` (callbacks, caching). Documented: `/docs/proxy/configs`, `/docs/proxy/load_balancing`.

**State stores:**

| Store | Holds | Source |
|---|---|---|
| **PostgreSQL** (`DATABASE_URL`) | virtual keys, users, teams, orgs, budgets, spend logs, models when `STORE_MODEL_IN_DB=True` | Documented: `/docs/proxy/virtual_keys`, `/docs/proxy/model_management` |
| **Redis** (`redis_host/port/password` in `router_settings`) | shared rpm/tpm counters, cooldown state, in-flight counters for `least-busy`, session-affinity pins, response cache | Documented: `/docs/proxy/load_balancing`, `/docs/routing` |
| **In-process** | everything above when Redis is absent — per-worker only | Documented (implied by "set this when using multiple litellm proxy deployments") |

Single-binary operation without Postgres and Redis is possible (config-only), but then virtual keys,
spend tracking, shared cooldowns and multi-replica affinity are unavailable. That is a materially
different product from the three-store deployment.

---

## 3. Routing capabilities (every strategy: simple-shuffle, least-busy, latency-based, usage/cost-based, tag-based, custom; weights; retries, fallbacks, cooldowns, context-window fallbacks)

The unit of routing is the **request**. Every strategy below picks a deployment per call; nothing in
the router carries a per-conversation placement decision unless a pre-call check (§4) adds one.

### 3.1 Strategies

| `routing_strategy` | Mechanism | Notes from the docs |
|---|---|---|
| `simple-shuffle` (**default**) | Weighted random. Uses `rpm`/`tpm` as pick weights if present, else `weight`, else uniform random. | "recommended ... Best performance with minimal latency overhead" |
| `least-busy` | Fewest active requests, in-flight counter in Redis when configured | "High concurrency scenarios" |
| `usage-based-routing` (v1) | Lowest current rpm/tpm usage | Explicitly "bad for perf"; "not recommended for production due to performance impacts" |
| `usage-based-routing-v2` | Async version, `redis.incr` + `redis.mget`, filters deployments over their tpm/rpm | Recommended variant of the usage family |
| `latency-based-routing` | Fastest responding deployment; `routing_strategy_args: {"ttl": N}` for the measurement window, `{"lowest_latency_buffer": 0.5}` for hysteresis | — |
| `cost-based-routing` | Lowest cost per token from the model cost map | — |
| `order` (priority tiers) | `order: 1` tried before `order: 2`; failures promote to the next tier | Not a weight: a hard stage |
| Routing groups | Named sub-groups inside one proxy, each with its own `routing_strategy` and `routing_strategy_args` | Added to allow mixed strategies without a second proxy |
| `lar1` | Listed among strategies that support session affinity; only async | **Unverified** what it computes |
| Custom | `router.set_custom_routing_strategy(Strategy())`, subclassing the router's strategy base | Documented, with a worked example |

Sources: `/docs/routing`, `/docs/proxy/load_balancing`.

### 3.2 Weights

`weight` is a **probability**, not a share controller. Docs: `weight: 9` / `weight: 1` → "pick this
90% of the time" (Documented: `/docs/routing`). Consequence that matters for RIPPLE: weights bias the
*next* pick; they do not observe how many conversations are currently parked on each deployment and
they do not correct a drift. A 3:2 weight yields a 3:2 *expectation over many independent picks*, not
a 3:2 *state*. With `simple-shuffle` the assignment is memoryless by construction.

`enable_weighted_failover: true` re-picks inside the same model group using the same weights before
escalating to cross-group `fallbacks`; with `order` also set, the order filter runs first and the
weighted re-pick only sees the current tier (Documented: `/docs/routing`).

### 3.3 Retries, fallbacks, cooldowns

- **Retries**: `num_retries` (ranked across four possible locations), `retry_after` for a minimum
  wait. Retries re-enter deployment selection.
- **Fallbacks**: `fallbacks: [{"a": ["b"]}]` — in-order, cross-model-group, after `num_retries` is
  exhausted. Three separate maps: `fallbacks`, `context_window_fallbacks` (on
  `ContextWindowExceededError`), `content_policy_fallbacks` (on `ContentPolicyViolationError`).
  `default_fallbacks` for misconfigured groups. Disable per request (`disable_fallbacks: true`) or
  per key (key metadata).
- **Cooldowns**: `cooldown_time` (default 5 s per the health-check routing reference table),
  `allowed_fails`, and `allowed_fails_policy` with per-error-type thresholds
  (`AuthenticationErrorAllowedFails`, `TimeoutErrorAllowedFails`, `RateLimitErrorAllowedFails`,
  `BadRequestErrorAllowedFails`, `ContentPolicyViolationErrorAllowedFails`). Semantics are
  `updated_fails > allowed_fails`, i.e. `0` cools down on the first failure.
- **Health-driven routing**: `background_health_checks`, `health_check_interval`,
  `enable_health_check_routing`, `health_check_ignore_transient_errors`,
  `background_health_check_model_groups`. With no policy set, the first failed check removes the
  deployment; if *all* deployments are unhealthy the filter is bypassed and traffic keeps flowing.
- **Capacity**: `max_parallel_requests` per deployment — but counted **per worker process, not
  shared across workers or pods** ("The cap is counted in process, so it is per proxy worker and is
  not shared across workers or pods"). Over the limit the request fails **immediately with 429**
  naming the deployment — "There is no wait queue and nothing to configure for one: requests never
  sit in the proxy waiting for a slot, they either run or get the 429" (Documented: `/docs/routing`).
  This is a hard rejection, not a spill, and on an N-worker proxy the effective fleet-wide cap is
  N × `max_parallel_requests`.

Sources: `/docs/proxy/reliability`, `/docs/proxy/health_check_routing`, `/docs/routing`.

### 3.4 Tag-based routing

A page at `/docs/proxy/tag_based_routing` returns **404** as of 20.11.2026. The docs navigation
lists no tag-routing page. **Unverified / not found** — I could not confirm that a documented
tag-based strategy exists in v1.110.2. What does exist and is adjacent: `model_group_alias`
(alias → group), routing groups (named sub-groups), and `model_group_affinity_config` to enable
affinity per group.

---

## 4. Session affinity and cache awareness (what affinity exists — e.g. deployment affinity / sticky sessions, prefix-cache-aware routing if any — with the exact mechanism and its limits)

This is the most important section for RIPPLE, and LiteLLM has genuinely moved here in 2026. Three
distinct mechanisms exist. None of them is a thread-placement decision.

### 4.1 `session_affinity` — pin to the deployment that served the first request

**Mechanism (Documented: `/docs/routing#session-affinity-sticky-sessions`).** It is a **router
pre-call check**, not a strategy: it "runs before the routing strategy picks a deployment and narrows
the candidates to the pinned one". Enable with:

```yaml
router_settings:
  routing_strategy: simple-shuffle
  optional_pre_call_checks: ["session_affinity"]
  deployment_affinity_ttl_seconds: 3600   # idle TTL of a pin, default 3600
  session_affinity_repin_on_failover: false
```

Session id sources: `x-litellm-session-id` (interchangeable with `x-litellm-trace-id`), any
`x-<vendor>-session-id` (e.g. `x-claude-code-session-id`), `metadata.session_id` in the body, and
`litellm_session_id`. Which deployment served a request is visible in the `x-litellm-model-id`
response header. `deployment_affinity` is the coarser sibling: it pins by **caller** — on the proxy
that is the virtual key, or the authenticated user id when there is no key — and a session pin takes
priority over it.

**Limits, all documented:**

- It only works under `simple-shuffle`, `least-busy`, `usage-based-routing-v2`,
  `latency-based-routing`, `cost-based-routing`, and `lar1`. It is **skipped** under the legacy
  `usage-based-routing` (v1) on every call, inside `priority` groups ("never pin"), on the sync SDK
  methods (`completion`, `responses`, `embedding` — "the same session id can land on a different
  deployment on every call"), and under a custom strategy unless that strategy picks from
  `router.select(..., kind="healthy")`.
- The relay routes `/vllm`, `/azure`, `/azure_ai`, `/nvidia_nim` do not carry the session id, so only
  `deployment_affinity` applies there; Vertex pass-through uses neither.
- Pins expire after `deployment_affinity_ttl_seconds` (default 3600) of idleness.
- Failover: by default a replacement does **not** become the new pin (`session_affinity_repin_on_failover`
  defaults to `false`), so a session bounces back to the original deployment when it recovers.

**Known defects (GitHub issues, read on 20.11.2026):**

- **#50029 (open, 2026-11-04)** — "Sticky Session not working - Router always sends Request to a
  different Endpoint": three endpoints serving the same model, every request of one OpenCode session
  lands on a different endpoint; the reporter also notes `weight` "does NOT seem to have any
  influence".
- **#47482 (closed 2026-11-10; opened 2026-10-20)** — "session_affinity retries go back to the failed pinned
  deployment, so num_retries never fails over until it is cooled down": with two deployments,
  `session_affinity`, `num_retries: 2`, all three attempts hit the dead deployment; the healthy one
  is never tried.

### 4.2 `prompt_caching` — prefix-hash pinning for provider prompt caches

**Mechanism (Documented: tutorial `claude_code_prompt_cache_routing`).** `PromptCachingDeploymentCheck`,
enabled via `optional_pre_call_checks: ["prompt_caching"]`, remembers which deployment performed a
cache *write* and routes subsequent calls with the same cacheable prefix back to it, so Anthropic-style
`cache_control` blocks hit instead of being rewritten.

**Limits.** The pin is keyed on a hash of the **entire cacheable prefix**, so extending the prefix
changes the hash and routes elsewhere — filed as **#19755** ("Prompt Caching routes requests with
extended prefixes to different deployments"), which argues LiteLLM should do Anthropic-style
longest-prefix matching instead. Cost: the check counts tokens to test the provider minimum (often
1024), and Berri AI's own blog post measures the fix for that: "median time to first byte from 553 ms
to 35 ms in our local 440k-token benchmark" for the single-healthy-deployment case
(Vendor-measured: `/blog/long-prompt-latency`).

### 4.3 `kubernetes_pod_discovery` — pod-level affinity inside one deployment

**Mechanism (Documented: `/docs/proxy/cache_aware_routing`).** LiteLLM resolves a Kubernetes headless
service to pod IPs, then selects a pod by **rendezvous hashing** on the session id. Proxy workers
seeing the same pod set compute the same ranking without a shared mapping; retries walk down that
session's ranking and wrap around. Refresh interval `KUBERNETES_POD_DISCOVERY_REFRESH_INTERVAL_SECONDS`
(default 5), idle eviction 300 s. Set `kubernetes_pod_discovery: true` in `litellm_params`; keep the
existing `routing_strategy`.

**The vendor's own statement of the limit**, which is the single most useful sentence in the entire
doc set for this comparison:

> "The inference server must support and enable prompt or prefix caching for cache reuse. **LiteLLM
> provides session affinity; it does not inspect the contents of each pod's cache or guarantee a
> cache hit.**"

Also: "Affinity applies within the selected deployment. If a model group contains multiple
deployments, the routing strategy still chooses between those deployments first. Cooldowns and rate
limits remain associated with the configured deployment, rather than individual discovered pods."
And pod discovery is Kubernetes-specific — a two-Mac fleet has no headless service to discover.

### 4.4 What is missing at the session layer

- **No thread object.** Nothing in the router counts *conversations* per deployment, so no rule can
  be stated in terms of "active threads on this node". Affinity is a pin map keyed by an opaque
  string with a TTL.
- **No rebalancing.** A pinned session never migrates for load reasons; the only exits are TTL
  expiry, cooldown, removal, or an explicit `session_affinity_repin_on_failover`.
- **No cross-node prompt-cache inventory.** §4.3 in the vendor's own words.

---

## 5. Residency and lifecycle: can LiteLLM know which model is LOADED on which backend, prefer loaded models, or prevent eviction? (check load balancing docs, 'model management'/load-unload endpoints, oMLX/vLLM/Ollama-specific integration, health checks)

**Short answer: no, at every layer.** This is the clearest gap in the whole comparison, and it is a
gap of *concept*, not of configuration.

### 5.1 Model management is CRUD on the gateway's own model list

`/docs/proxy/model_management` covers exactly four operations on the *gateway's* records — and they
manage configuration, not backend state:

| Operation | Route |
|---|---|
| List | `GET /model/info` (also `/v1/model/info`, `/v1/model_group/info`) |
| Add | `POST /model/new` (requires `store_model_in_db`) |
| Update | `POST /model/update` |
| Delete | `POST /model/delete` (POST, no DELETE verb; admin only) |

"Delete Model" removes the model *from the gateway*; it does not unload anything on a backend.
There is no `load`, `unload`, `evict`, or `pin` route. Source check: in
`litellm/proxy/management_endpoints/model_management_endpoints.py` at `main`, the route decorators
are `@router.get` (×2), `@router.patch`, `@router.post` (×8) — and a grep for `model/load`,
`model/unload`, `/v1/model/load` returns **no match**. So: no load/unload endpoint exists in the
source I read, and none is documented.

### 5.2 No backend-state introspection

Deployment identity is configuration: `model_name`, `litellm_params.model`, `api_base`,
`model_info.id`. Nothing polls the backend for *what is resident*. Concretely, for the oMLX case:

- `/v1/models/status` (resident models + `estimated_size`) is **not** consumed anywhere in the routing
  docs. There is no oMLX provider in the provider list at all — an oMLX instance is configured as a
  generic `openai/`-compatible deployment with an `api_base`.
- vLLM is reached the same way (`openai/<served-model>` + `api_base`, per the cache-aware routing
  example) or via the `/vllm` relay route. No vLLM lifecycle API (`/sleep`, `/wake_up`, load/unload)
  is documented as integrated.
- Ollama has the only lifecycle-adjacent knob I found: `keep_alive` in `litellm_params`
  (`keep_alive: "8m"`, `-1` for forever), which is passed through to Ollama's own model lifetime
  handling (Documented: `/docs/providers/ollama`). That is a per-deployment hint to the backend, not
  a routing criterion.

### 5.3 Health checks actively work against residency

Health checks send **real inference requests**, not cheap probes:

> "Unless background health checks are on, every call sends a real test request to each of those
> deployments, so it costs a few tokens per deployment." (Documented: `/docs/proxy/health`)

`health_check_max_tokens` defaults to 16. On a 512 GB Mac holding a 400 GB model, a health check for a
model that is *not* resident is a cold load, and if memory is tight, an eviction of whatever is
resident. The health subsystem therefore treats "the model answers" as health — the exact semantic
that RIPPLE's eviction guard exists to prevent. `GET /health` is also expensive by default and
`/health/liveliness` checks nothing but the process ("No dependencies are checked").

### 5.4 What LiteLLM does instead of a residency rule

The nearest available substitutes, and why each falls short:

| Substitute | Why it is not residency |
|---|---|
| Model filter (a deployment only carries the models you configured) | Static config. If a model is configured on both nodes, both are candidates; if it is resident on neither, LiteLLM still sends it and the backend decides what to evict. |
| `max_parallel_requests` → 429 | Per deployment and per worker process, not per model; hard rejection with no spill and no queue. |
| Cooldowns | Reactive, error-driven, 5 s default. An eviction is not an error — the client sees HTTP 200. |
| `session_affinity` | Keeps a conversation on a node *after* the fact; it cannot refuse a request that would evict. |
| `weight` | Probability only. |

There is **no** 503-with-reason path, no notion of "the model is loaded here, prefer here", and no
guard that inspects what a new load would displace. **Unverified:** whether any hidden config key
does this — nothing in the docs or the model-management source suggests one.

---

## 6. The rest of the platform (virtual keys, teams, budgets, rate limits, spend tracking, guardrails, caching, logging/observability callbacks, admin UI, SSO, audit)

This is where LiteLLM is far ahead of any single-purpose router, and it is the honest reason people
choose it.

| Capability | Detail | Source |
|---|---|---|
| **Virtual keys** | `POST /key/generate` with `models`, `max_budget`, `tpm_limit`, `rpm_limit`, `budget_duration`, `metadata`; inheritance rules from owner (models, management routes, MCP entitlement, budgets) are documented as a table | `/docs/proxy/virtual_keys` |
| **Users / teams / orgs / projects** | `/user/new`, `/team/new`, `/team/info`, project-level limits; service accounts; SCIM provisioning; Terraform provider (`BerriAI/terraform-provider-litellm`) | `/docs/proxy/virtual_keys`, `/docs/proxy/model_management` |
| **Budgets** | `max_budget` per key/user/team, `budget_duration`, temporary budget increase, `apply_user_budget_to_team_keys`; router-side `router_budget_limiting` pre-call check | `/docs/proxy/virtual_keys`, `/docs/routing` |
| **Rate limits** | Per-deployment `rpm`/`tpm`/`rpd`/`tpd` with `daily_reset_time`; `enforce_model_rate_limits` turns them from routing hints into hard 429s with `retry-after`; `itpm`/`otpm` for split input/output limits | `/docs/proxy/load_balancing` |
| **Spend tracking** | `completion_cost()` against `model_prices_and_context_window.json`; per key/user/team/end-user via `/key/info`, `/user/info`, `/team/info`, `/end_user/info`; spend logs per request with the serving deployment | `/docs/proxy/virtual_keys` |
| **Guardrails** | OSS framework + custom guardrails + Presidio PII masking are OSS; `llmguard_moderations`, `llamaguard_moderations`, `hide_secrets`, `openai_moderations`, `google_text_moderation`, `lakera_prompt_injection`, `aporia_prompt_injection` require an Enterprise license | `/docs/enterprise` |
| **Caching** | Response cache with Redis/S3-style backends (`cache_params: {type: redis, max_connections: ...}`), plus the `prompt_caching` routing check (§4.2) | `/docs/proxy/load_balancing`, `/docs/routing` |
| **Logging / observability** | Callback integrations including GCS bucket and LangSmith (benchmarked by the vendor), Langfuse, Prometheus metrics; custom `CustomLogger` subclass with `log_success_event` / `log_failure_event` | `/docs/benchmarks`, `/docs/routing` |
| **Admin UI** | Model list, model detail with Test Connection, credentials, Health Status tab with "Run All Checks", request logs, Usage, Cost Optimization | `/docs/proxy/model_management`, `/docs/proxy/health` |
| **SSO / audit** | SSO/SAML free for up to 5 users, Enterprise beyond; audit logs and delegated admin roles are Enterprise; SOC 2 Type II | `/docs/enterprise` |
| **Beyond LLM routing** | MCP gateway, A2A agent gateway, auto-router (complexity classifier), LiteLLM Lens tracing, self-hosted coding-agent harnesses | README, docs nav |

For RIPPLE's stated non-goals this is mostly *surplus*: RIPPLE explicitly refuses to be an auth
gateway, to track spend, or to translate providers. The relevance is narrower — if the fleet ever
needs per-user keys or spend attribution, LiteLLM already has it and RIPPLE would have to grow it.

---

## 7. Operations (deployment, resources, latency overhead as documented/benchmarked publicly, scaling, failure modes, known complaints in issues)

**Deployment.** Docker image (`ghcr.io/berriai/litellm`, cosign-signed with a key pinned by commit
`e82a3c6`), Helm chart, `uv tool install 'litellm[proxy]'`, deploy buttons for Render/Railway/AWS/GCP.
Useful feature set requires PostgreSQL; multi-replica correctness requires Redis.

**Vendor benchmarks** (all `Vendor-measured`, mock backends, `/docs/benchmarks`):

| Scenario | Result |
|---|---|
| 3,000 RPS, 50K–100K-token prompts, 33 pods × 4 workers / 4 vCPU / 16 GiB | 13.5 B TPM; gateway latency p50 30.581 ms, p95 54.029 ms, p99 91.645 ms; 100 % HTTP 200 |
| Short prompts, 4 instances, 1,000 users | 1,170 RPS; gateway overhead p50 2 ms, p95 8 ms, p99 13 ms (the "8ms P95" in the README) |
| Short prompts, 2 instances | 1,035.7 RPS; overhead p50 12 ms, p95 29 ms, p99 43 ms |
| Realtime WebSockets, 4 instances | 1,207 RPS; end-to-end p95 67 ms |
| Logging callbacks (GCS, LangSmith) | 1,133 → 1,137 RPS; median latency 140 → 138/132 ms (i.e. no measurable cost) |

Two caveats a fair comparison must state: these are **vendor-run against mock models**, so they
measure gateway overhead, not inference; and the overhead numbers are per-instance with 4 workers on
8 GB, which is a heavier footprint than a single-purpose Go proxy needs.

**Independent latency measurement: unverified.** No third-party benchmark of LiteLLM gateway
overhead was found. Do not quote a number for this.

**Known complaints (GitHub issues, all read 20.11.2026):**

| Issue | State (opened) | Substance |
|---|---|---|
| **#46276** "perf: 4-7x latency regression from v1.101.0 to v1.105.0 under load (new middleware + auth prefetch + double Redis calls)" | open, 2026-10-13 | Reproducible 4–7× regression across CI runs, Locust 750 users, 4 uvicorn workers |
| **#38193** "LiteLLM memory keeps growing after OOM restart, with no apparent reclamation" | closed 2026-09-03 (opened 2026-08-25) | Process memory growth |
| **#50874** "How to handle Redis spend update queue backlog at ~300M TPM (20 pods / 80 workers)" | closed 2026-11-11 (opened 2026-11-10) | Spend pipeline backpressure at scale |
| **#48994** "least-busy undercounts a busy deployment after its in-flight counter key expires" | open, 2026-10-29 | In-flight counter TTL (3600 s) causes undercount; "the error gets worse on its own" |
| **#50029** sticky session not honored | open, 2026-11-04 | See §4.1 |
| **#47482** affinity retries never fail over | closed 2026-11-10 (opened 2026-10-20) | See §4.1 |
| **#36926** "False BudgetExceededError under sustained load" | open, 2026-08-14 | Spend accounting race |

**Release cadence and stability.** 968 PyPI releases; six releases published on 2026-11-20 alone
(`v1.112.0-dev.1`, `v1.106.12`, `v1.108.13` LTS, `v1.109.3`, `v1.110.2`, `v1.111.0-rc.3`). The docs state a support
policy of "the four most recent stable minor lines", currently "paused during the LTS pilot". 5,299
open issues+PRs against 63.6k stars is a real operational signal: the project moves fast and the
backlog is large.

**Failure modes worth naming for a small fleet:** default `timeout: 30` in the load-balancing example
(a 60–70 s cold load would be cut); retries and fallbacks can silently move a conversation to another
node mid-thread (documented as intended); health checks issue real completions; and every deployment
is assumed interchangeable, which is exactly false when each node holds a different 400 GB model.

---

## 8. License and commercial split (MIT core vs enterprise features, what is paywalled, pricing if public)

**License file (Source: `LICENSE` at `main`)** — a two-part statement:

> "All content that resides under the `litellm/enterprise/` directory of this repository, if that
> directory exists, is licensed under the license defined in `litellm/enterprise/LICENSE`. Content
> outside of the above mentioned directories or restrictions above is available under the MIT
> license."

MIT text follows, "Copyright (c) 2023 Berri AI". The GitHub API reports the license as
`NOASSERTION` / "Other" precisely because of this split — a fact worth stating plainly rather than
calling the project "MIT" without qualification.

**What is paywalled** (Documented: `/docs/enterprise`):

- SSO/SAML beyond 5 users ("SSO is free for up to 5 users; beyond that, an Enterprise license is
  required").
- Audit logs, delegated admin roles.
- The seven named guardrail integrations (`llmguard_moderations`, `llamaguard_moderations`,
  `hide_secrets`, `openai_moderations`, `google_text_moderation`, `lakera_prompt_injection`,
  `aporia_prompt_injection`).
- Professional support: shared Slack Connect channel, email, optional 24/7 SLA with 1 h for Sev 0 and
  72 h for security patches.

**What is not paywalled:** routing, retries, fallbacks, cooldowns, session affinity, prompt-caching
routing, virtual keys, budgets, spend tracking, caching, custom guardrails, Prometheus/callbacks, the
admin UI, the MCP/A2A gateways. The enterprise tier is a license key on the same image.

**Pricing: not public.** "Pricing depends on your deployment size"; "Pricing is based on usage";
both routes lead to a sales form. 30-day trial. Available via AWS and Azure Marketplace.

---

## 9. Honest assessment for RIPPLE's use case: a two-node heterogeneous Apple-Silicon fleet (M5 512GB + M3 512GB) where models are huge, resident, and evictable

**Verdict: LiteLLM is the wrong tool for the placement problem, and an excellent tool for everything
around it.** It would take over auth, keys, spend, observability and the dashboard — and it would
still hand the eviction decision to the backend, because it has no concept of a loaded model.

The reason is structural, not a missing config flag. LiteLLM routes **requests** between
**interchangeable deployments**. RIPPLE places **threads** between **non-interchangeable nodes whose
memory is the scarce resource**. Every LiteLLM mechanism that looks close turns out to be a different
thing:

| RIPPLE requirement | LiteLLM equivalent | Gap |
|---|---|---|
| Thread as unit of placement, sticky for all turns and tool rounds | `session_affinity` pin map, TTL 3600 s, opt-in | Pin is a cache hint applied *after* routing, not a placement; skipped under several strategies; two open/closed bugs show it not holding (#50029, #47482) |
| Deficit rule to a weighted target share (3:2) | `weight: 3` / `weight: 2` in `simple-shuffle` | Weight is a per-request probability. No active-thread counter exists, so no controller can converge to a share. 3:2 as *state* is not expressible |
| Capacity before weighting; spill when a node is full | `max_parallel_requests` → immediate 429, no queue, no spill | Opposite semantics: reject instead of redirect |
| Largest relative headroom among full nodes | — | No such rule; `least-busy` is absolute in-flight count with a known undercount bug (#48994) |
| Model filter from the backend's model list | Static `model_list` per deployment | If the model is on neither node, LiteLLM still sends it |
| Residency preference (prefer the loaded model) | — | No backend-state introspection at all (§5.2) |
| Eviction guard: 503 naming model/node/blocking model | — | No equivalent; the failure is invisible (HTTP 200 from the backend) |
| Slot counting per model | `max_parallel_requests` per deployment (per worker process) | Per deployment, not per model |
| Node identity via expected `/api/cluster/node_id` | `api_base` + `model_info.id` | URL is the identity; DHCP reuse unaddressed |
| Streaming pass-through, no own timeout, retry only before first byte | Streaming supported; `timeout: 30` in the docs' own example; retries/fallbacks can move mid-thread | Different contract; a 60–70 s cold load needs explicit timeout surgery |
| Health via cheap probe | Health = a real completion request, 16 tokens | Would itself trigger cold loads and possible evictions |
| Key pass-through, no auth gateway | Virtual keys by default; master key | Deliberate philosophical difference — RIPPLE's non-goal is LiteLLM's core feature |
| `X-Ripple-Host` / `X-Ripple-Reason` per response | `x-litellm-model-id` (+ `x-litellm-overhead-duration-ms`) | Which deployment, not why, and no eviction reasoning to report |

**What LiteLLM would genuinely contribute**, if the two were combined rather than compared:

1. Virtual keys, per-user/team budgets and spend attribution — RIPPLE refuses this by design, so it
   is only relevant if the fleet gets multi-tenant.
2. 100+ provider adapters and OpenAI-format translation, including `/v1/messages` and `/v1/responses`
   — useful if the fleet ever fronts a cloud model as overflow.
3. Observability callbacks (Langfuse/LangSmith/GCS/Prometheus) and a mature admin UI.
4. Guardrails and PII masking.
5. An enormous, fast-moving community and a published LTS line.

**Costs of adopting it here**, stated honestly: PostgreSQL (+ Redis for multi-replica correctness) on
a two-Mac fleet; a Python/FastAPI process with the 4–7× latency regression of #46276 still open; a
30 s default timeout in the documented example; health checks that issue real completions; and a
routing model whose central assumption — deployments are interchangeable — is the one assumption this
fleet violates. Adding residency logic would mean forking the router or writing a custom strategy
(`set_custom_routing_strategy`) — and a custom strategy is documented to lose `session_affinity`
unless it selects from `router.select(..., kind="healthy")`, so the two features would have to be
rebuilt together anyway.

**A defensible division of labour, if one is wanted:** RIPPLE as the placement layer (thread stickiness,
deficit share, residency, eviction guard, node identity), LiteLLM in front of it only if keys, spend or
provider breadth become requirements — with RIPPLE as the single "deployment" LiteLLM sees, so
LiteLLM's request-level routing never gets a chance to break thread affinity. That keeps each system
doing the thing it is actually built for.

---

## 10. Sources (every URL with access date 20.11.2026)

**Repository and release metadata**

1. https://github.com/BerriAI/litellm — repo landing, description ("Rust core with Python SDK"), stars/forks/license badge. Accessed 20.11.2026.
2. https://api.github.com/repos/BerriAI/litellm — stars 63,589, forks 13,439, open_issues 5,299, created 2023-07-27, pushed 2026-11-20T07:26:44Z, license `NOASSERTION`. Accessed 20.11.2026.
3. https://api.github.com/repos/BerriAI/litellm/releases?per_page=5 — v1.111.0-rc.3, v1.110.2, v1.109.3, v1.108.13 (LTS), v1.106.12, all 2026-11-20. Accessed 20.11.2026.
4. https://pypi.org/pypi/litellm/json — version 1.110.2, upload 2026-11-20T04:05:22Z, `requires_python <3.16,>=3.10`, 968 releases. Accessed 20.11.2026.
5. https://api.github.com/repos/BerriAI/litellm/contributors?per_page=1&anon=true — `Link: rel="last"` page 1960. Accessed 20.11.2026.
6. https://raw.githubusercontent.com/BerriAI/litellm/main/README.md — feature list, "8ms P95 latency at 1k RPS", YC W23, `litellm-core` distribution, cosign verification, enterprise scope. Accessed 20.11.2026.
7. https://raw.githubusercontent.com/BerriAI/litellm/main/LICENSE — MIT + `litellm/enterprise/` carve-out. Accessed 20.11.2026.
8. https://raw.githubusercontent.com/BerriAI/litellm/main/litellm/proxy/management_endpoints/model_management_endpoints.py — route decorators only (`get`/`patch`/`post`), no `model/load` route. Accessed 20.11.2026.

**Routing, reliability, capacity**

9. https://docs.litellm.ai/docs/routing — strategies, weights ("pick this 90% of the time"), `order`, routing groups, custom strategies, session affinity section (incl. `deployment_affinity_ttl_seconds`, `session_affinity_repin_on_failover`, `model_group_affinity_config`), `max_parallel_requests` 429-with-no-queue, `enable_weighted_failover`, cooldown semantics. Accessed 20.11.2026.
10. https://docs.litellm.ai/docs/proxy/load_balancing — default `simple-shuffle`, strategy table, `rpm`/`tpm`, `enforce_model_rate_limits`, Redis for shared state, `encrypted_content_affinity`. Accessed 20.11.2026.
11. https://docs.litellm.ai/docs/proxy/reliability — fallbacks, `context_window_fallbacks`, `content_policy_fallbacks`, `default_fallbacks`, `disable_fallbacks` per request/key. Accessed 20.11.2026.
12. https://docs.litellm.ai/docs/proxy/health_check_routing — `background_health_checks`, `enable_health_check_routing`, `allowed_fails_policy`, `cooldown_time` default 5, all-unhealthy bypass. Accessed 20.11.2026.
13. https://docs.litellm.ai/docs/proxy/health — probe endpoints, "every call sends a real test request", `health_check_max_tokens` default 16, `/health/liveliness` checks nothing. Accessed 20.11.2026.
14. https://docs.litellm.ai/docs/proxy/tag_based_routing — **404, no such page** as of 20.11.2026.

**Affinity and cache awareness**

15. https://docs.litellm.ai/docs/proxy/cache_aware_routing — `kubernetes_pod_discovery`, rendezvous hashing, session-id sources table, "does not inspect the contents of each pod's cache or guarantee a cache hit". Accessed 20.11.2026.
16. https://docs.litellm.ai/docs/tutorials/claude_code_prompt_cache_routing — `PromptCachingDeploymentCheck` / `optional_pre_call_checks: ["prompt_caching"]`. Accessed 20.11.2026.
17. https://docs.litellm.ai/blog/long-prompt-latency — TTFT 553 ms → 35 ms on a 440k-token prompt after skipping the prompt-cache eligibility count. Accessed 20.11.2026.

**Models, lifecycle, providers**

18. https://docs.litellm.ai/docs/proxy/model_management — `GET /model/info`, `POST /model/new|update|delete`, `store_model_in_db`, `LITELLM_SALT_KEY`, Terraform provider. Accessed 20.11.2026.
19. https://docs.litellm.ai/docs/providers/ollama — `keep_alive` in `litellm_params`, `ollama/` vs `ollama_chat/` prefixes. Accessed 20.11.2026.

**Platform surface, license, commercial**

20. https://docs.litellm.ai/docs/enterprise — license-key-on-same-image, SSO free ≤5 users, enterprise-only guardrails list, SOC 2 Type II, support SLAs, "pricing depends on your deployment size". Accessed 20.11.2026.
21. https://docs.litellm.ai/docs/proxy/virtual_keys — `/key/generate`, inheritance table, spend tracking via `completion_cost`, `/key/info`, `/user/info`, `/team/info`. Accessed 20.11.2026.
22. https://github.com/BerriAI/litellm-docs/blob/f9d4a4d2/docs/proxy/rust_gateway.md — the deleted
    Rust gateway page: only `/ocr` and `bedrock` `/audio/transcriptions` on Rust, `/chat/completions`
    on Python, `rust: true` inert. Accessed 20.11.2026; removed from `main` on 2026-11-10.
23. https://docs.litellm.ai/docs/benchmarks — 3,000 RPS / 13.5 B TPM, gateway p95 54.029 ms, short-prompt 8 ms p95 overhead on 4 instances, `x-litellm-overhead-duration-ms`, callback benchmark. Vendor-measured, mock backends. Accessed 20.11.2026.

**Issues (state as read on 20.11.2026)**

24. https://github.com/BerriAI/litellm/issues/50029 — sticky session not honored, open, 2026-11-04.
25. https://github.com/BerriAI/litellm/issues/47482 — session_affinity retries never fail over, closed 2026-11-10 (opened 2026-10-20).
26. https://github.com/BerriAI/litellm/issues/46276 — 4–7× latency regression v1.101.0 → v1.105.0, open, 2026-10-13.
27. https://github.com/BerriAI/litellm/issues/48994 — `least-busy` in-flight undercount after counter TTL, open, 2026-10-29.
28. https://github.com/BerriAI/litellm/issues/38193 — memory growth after OOM restart, closed 2026-09-03 (opened 2026-08-25).
29. https://github.com/BerriAI/litellm/issues/50874 — Redis spend-update backlog at ~300M TPM, closed 2026-11-11 (opened 2026-11-10).
30. https://github.com/BerriAI/litellm/issues/19755 — prompt-caching routes extended prefixes to different deployments.
31. https://github.com/BerriAI/litellm/issues/47421 — "Skip prompt_caching pre-call checks for single-deployment model groups", open, 2026-10-19.
32. https://github.com/BerriAI/litellm/issues/36926 — false BudgetExceededError under load, open, 2026-08-14.

**What was not established:** independent (non-vendor) latency measurements of LiteLLM's
gateway overhead; whether a documented tag-based routing strategy exists in v1.110.2; the exact
semantics of the `lar1` strategy; Berri AI's funding history. All four are marked unverified above
rather than filled in by inference.

---

Part of the competitive analysis: [docs/competitive-analysis.md](../competitive-analysis.md). Licence of this text: MIT, like the project.
