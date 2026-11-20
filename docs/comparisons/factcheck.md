# Adversarial fact-check — `litellm.md` and `landscape.md`

**Date:** Nov 20th, 2026
**Scope:** adversarial verification of [`./litellm.md`](./litellm.md) and [`./landscape.md`](./landscape.md)
against primary sources only — GitHub REST/GraphQL API, `raw.githubusercontent.com` at `main`,
`docs.litellm.ai`, `localai.io`, `lightseek.org/smg`, `theagentrouter.ai`.
**Question:** Which claims in the two documents are wrong, overstated, or quoted from a page that no
longer exists?

**Checked:** 2026-11-20, 08:00–09:05 UTC (09:00–10:05 CET).
**Method:** primary sources only — GitHub REST/GraphQL API as `vogel61`, `raw.githubusercontent.com`
at `main`, `docs.litellm.ai` (live HTML + the 10.2 MB `llms-full.txt` corpus, which contains every
doc page verbatim), `localai.io`, `lightseek.org/smg`, `theagentrouter.ai`. Where a docs page was
cited, the live page was fetched and searched; where a repo claim was made, the README at `main` was
fetched and quoted.
**The two source documents were not edited during the check.** Corrections are text, below;
every one of them was applied to [`./litellm.md`](./litellm.md) and [`./landscape.md`](./landscape.md) on Nov 20th, 2026, before publication.

**Headline:** both documents are in far better shape than a 45-minute AI pass usually produces. All
35 star counts, licences, languages, latest releases and last-commit dates in `landscape.md` §7
match the API to within +1/+2 stars of drift. Two things will get the author mocked on Hacker News,
and both are the same class of error — **a quote that is real but sourced to a page that no longer
exists** (`litellm.md` §1) — plus one wrong commit hash and one wrong quote path.

---

## A. `litellm.md`

| # | Claim (short) | Verdict | Evidence | Correction text to apply |
|---|---|---|---|---|
| 1 | `v1.110.2` is current stable; PyPI upload 04:05:22Z, release 04:06:19Z | **CONFIRMED** | `gh api repos/BerriAI/litellm/releases?per_page=6` → `v1.110.2 \| 2026-11-20T04:06:19Z \| prerelease=false`. PyPI JSON: `version 1.110.2`, `1.110.2 uploads [('2026-11-20T04:05:22.369806Z', ...)]` | none |
| 2 | Stars 63,589 | **CONFIRMED (drift +2)** | `stargazers_count: 63591` at 08:00Z. Forks `13439` ✓, subscribers `238` ✓, open issues `5300` (doc says 5,299 — same drift) | optionally "63.6k" |
| 3 | Licence: MIT core, `NOASSERTION` on the API | **CONFIRMED**, quote **WRONG** | API `license: NOASSERTION` ✓. But `LICENSE` at `main` reads: *"All content that resides under the **"litellm/enterprise/"** directory … licensed under the license defined in **"litellm/enterprise/LICENSE"**"*. The document quotes it as `enterprise/` and `enterprise/LICENSE` | see C-3 |
| 4 | Berri AI, YC W23, `ishaan@berri.ai` / `krrish@berri.ai`; enterprise = licence key on the same image; SOC 2 Type II; AWS+Azure Marketplace; 30-day trial; no public pricing; no funding round found | **CONFIRMED** | README L23 badge `Y Combinator W23`; L751 `ishaan@berri.ai / krrish@berri.ai`. `/docs/enterprise`: *"Enterprise is a license key on the same Gateway image you may already run"*, *"SOC 2 Type II audited"*, *"Procurement is available through AWS and Azure Marketplace"*, *"Pricing depends on your deployment size"*, *"SSO is free for up to 5 users"*, *"1 hour for Sev 0 and 72 hours for security patches"*, 7 enterprise guardrails listed verbatim (`llmguard_moderations` … `aporia_prompt_injection`) | none |
| 5 | Cosign key "pinned by commit `0112e53`" | **WRONG** | README L668: *"Releases are signed with the key introduced in commit **`e82a3c6`**"*; L676 fetches `cosign.pub` from that commit. `0112e53` appears nowhere in the README | see C-2 |
| 6 | GitHub description says "Rust core with Python SDK" | **CONFIRMED** | `gh api repos/BerriAI/litellm` → `description: "The fastest, litest AI Gateway. Rust core with Python SDK. Call 100+ LLM APIs…"` | none |
| 7 | The Rust quote: "the Python proxy serves only two routes on Rust: `/ocr` … `/audio/transcriptions` for `bedrock` … the per-model `rust: true` flag described below has no effect" (Documented: `/docs/proxy/rust_gateway`) | **WRONG (citation) — quote real, page deleted** | `https://docs.litellm.ai/docs/proxy/rust_gateway` → **404** (a meta-refresh stub to `/rust-migration`). `docs.litellm.ai/llms-full.txt` (all 10.2 MB of docs) contains **no** occurrence of "two routes on Rust", "`rust: true` … has no effect", or `audio/transcriptions` + bedrock together. The sentence **is** verbatim in `BerriAI/litellm-docs` at `docs/proxy/rust_gateway.md` **commit `f9d4a4d2` (2026-10-30)**, and that file was **deleted** by `fabf8478` (2026-11-10) — *"docs: remove outdated Rust gateway page in favor of /rust-migration"* | see C-1 |
| 8 | `session_affinity` is a pre-call check; `deployment_affinity_ttl_seconds` default 3600; `session_affinity_repin_on_failover` default false | **CONFIRMED** | `/docs/routing` §Session Affinity: *"It is a router pre-call check: it runs before the routing strategy picks a deployment and narrows the candidates to the pinned one."* Config block: `deployment_affinity_ttl_seconds : 3600 # optional, default 3600`. *"The option defaults to false."* | none |
| 9 | Affinity skipped under usage-based-routing v1, priority groups, sync SDK methods, custom strategies | **CONFIRMED** | `/docs/routing`: *"It works when the top-level routing_strategy is simple-shuffle, least-busy, usage-based-routing-v2, latency-based-routing, or cost-based-routing … also works under lar1, which only supports async calls."* / *"A top-level routing_strategy of usage-based-routing (the legacy v1 strategy) also skips it on every call"* / *"Inside a priority routing group, session_affinity and deployment_affinity never pin"* / *"The sync Router methods, such as completion, responses, and embedding, skip it, so the same session id can land on a different deployment on every call."* / *"unless its async method picks from what router.select(model, request_kwargs, kind=\"healthy\") returns"* | none |
| 10 | Quoted: "LiteLLM provides session affinity; it does not inspect the contents of each pod's cache or guarantee a cache hit." | **CONFIRMED verbatim** | `/docs/proxy/cache_aware_routing`: *"The inference server must support and enable prompt or prefix caching for cache reuse. LiteLLM provides session affinity; it does not inspect the contents of each pod's cache or guarantee a cache hit."* | none |
| 11 | `kubernetes_pod_discovery`: rendezvous hashing, refresh 5 s, idle eviction 300 s | **CONFIRMED** | Same page: *"LiteLLM uses rendezvous hashing to rank pods for a session."* `KUBERNETES_POD_DISCOVERY_REFRESH_INTERVAL_SECONDS` default `5`; `KUBERNETES_POD_DISCOVERY_IDLE_EVICTION_SECONDS` default `300` | none |
| 12 | `model_management` is CRUD only; no `/model/load`; source has 2 GET, 1 PATCH, 8 POST | **CONFIRMED** | `/docs/proxy/model_management` documents only `POST /model/new`, `GET /model/info`, `PATCH /model/{model_id}/update`, `POST /model/update`, `POST /model/delete`. At `main`, `grep -c '^@router.get\|^@router.post\|^@router.patch' model_management_endpoints.py` → **2 / 8 / 1**; the 11 paths are `GET /model/{id}/kubernetes_pods`, `PATCH /model/{id}/update`, `POST /model/{block,unblock,delete,new,update}`, `POST /model_group/make_public`, `POST /model_hub/update_useful_links`, `GET|POST /auto_router/classifier/default_prompt`. No `model/load`, `model/unload`, `evict` or `pin` route | none (add: "the file also has `/model/block` and `/model/unblock`, which are DB visibility flags, not backend lifecycle") |
| 13 | Health checks send real completions; `health_check_max_tokens` default 16 | **CONFIRMED** | `/docs/proxy/health`: *"Unless background health checks are on, every call sends a real test request to each of those deployments, so it costs a few tokens per deployment."* Tuning table: `health_check_max_tokens \| 16 (unset for wildcard routes) \| max_tokens on the probe request` | none |
| 14 | `max_parallel_requests` → immediate 429, no wait queue | **CONFIRMED verbatim**, but the framing of *whose* limit it is is **OVERSTATED** | `/docs/routing`: *"There is no wait queue and nothing to configure for one: requests never sit in the proxy waiting for a slot, they either run or get the 429."* Same paragraph: *"**The cap is counted in process, so it is per proxy worker and is not shared across workers or pods**"* | see C-4 |
| 15 | `weight` is a probability, not a share ("pick this 90% of the time") | **CONFIRMED** | `/docs/routing`: `"weight" : 9 , # pick this 90% of the time`; *"Set weight on a deployment to pick one deployment more often than others."* | none |
| 16 | `enable_weighted_failover` re-picks within the group; with `order`, order filter runs first | **CONFIRMED** | `/docs/routing`: *"With enable_weighted_failover, the router first retries inside the same model group by re-picking a different deployment using the existing weights"*; *"If the same group also uses order, the order filter runs before the weighted pick."* | none |
| 17 | `tag_based_routing` is a 404; no tag strategy found | **CONFIRMED** | `curl -o /dev/null -w %{http_code}` → **404**; sitemap has no tag-routing page | none |
| 18 | Vendor benchmarks: 13.5 B TPM; p50 30.581 / p95 54.029 / p99 91.645 ms; 1,170 RPS @ 8 ms p95; 1,035.7 RPS @ 12/29/43 ms; 1,207 RPS @ 67 ms; callbacks 1,133 → 1,137/1,135 @ 140 → 138/132 ms | **CONFIRMED** | `/docs/benchmarks`, every figure present: `13.5 billion tokens per minute`, `30.581 ms`, `54.029 ms`, `91.645 ms`, `1,170 requests per second with 8 ms p95`, `1,035.7` / `12 / 29 / 43`, `1,207 requests per second` / `67 ms`, `1,133.2 → 1,137.3 / 1,135` and `140 → 138 / 132 ms`; methodology `33 gateway pods; 4 workers, 4 vCPUs, and 16 GiB` ✓ | none |
| 19 | `timeout: 30` in the docs' own load-balancing example | **CONFIRMED** | `/docs/proxy/load_balancing`, `router_settings` block: `timeout : 30 # 30 seconds` | none |
| 20 | TTFT "553 ms → 35 ms … local 440k-token benchmark" | **CONFIRMED quote, URL WRONG** | Real page is `https://docs.litellm.ai/blog/long-prompt-latency` (200): *"Removing that unnecessary work took median time to first byte from 553 ms to 35 ms in our local 440k-token benchmark"*. The cited `/docs/blog/long-prompt-latency` returns **404** | see C-5 |
| 21 | Issues #50029 open 11-04, #47482 closed 10-20, #46276 open 10-13, #48994 open 10-29, #38193 closed 08-25, #50874 closed 11-10, #36926 open 08-14, #19755, #47421 | **CONFIRMED states/titles, dates are `created_at`** | API returns `created_at` matching every date given, but close dates differ: **#47482 closed 2026-11-10**, **#38193 closed 2026-09-03**, **#50874 closed 2026-11-11** | see C-8 |
| 22 | "five releases published on 2026-11-20 alone" | **OVERSTATED** | Six releases that day: `v1.111.0-rc.3` 05:55, `v1.110.2` 04:06, `v1.109.3` 03:59, `v1.108.13` 04:00, `v1.106.12` 05:52, **`v1.112.0-dev.1` 00:03** | see C-9 |
| 23 | 968 PyPI releases; ~1,960 contributors; LTS `1.108.13` | **CONFIRMED** | PyPI `n_releases 968`; contributors `Link: rel="last" … page=1960`; release name `1.108.13 (LTS)` | none |
| 24 | Support policy "four most recent stable minor lines", "paused during the LTS pilot" | **CONFIRMED** | `llms-full.txt`: *"LiteLLM supports the four most recent stable minor lines … This policy is paused during the LTS pilot."* | none |
| 25 | usage-based-routing v1 is "bad for perf" | **CONFIRMED** | Docs table literally: `**usage-based-routing** (bad for perf)` | none |

### A8 — numbers in `litellm.md` with no visible source

All numeric claims are sourced except these three, none of which is fabricated:

1. **"five releases published on 2026-11-20"** — undercounts by one (see #22).
2. **"~1,960 contributors"** — sourced, but the method (`per_page=1&anon=true` → last page) counts
   *all* commit authors including anonymous ones, not GitHub's "Contributors" panel figure. State the
   method inline or drop the number.
3. **"$/token" cost claims** — none present; no unsourced pricing numbers found. Clean.

---

## B. `landscape.md`

| # | Claim (short) | Verdict | Evidence | Correction text to apply |
|---|---|---|---|---|
| 26 | §7 star counts, licences, languages, latest releases, last commits for all 35 projects | **CONFIRMED** | Full GraphQL re-read at 08:00Z. Every cell matched, max drift +2 stars: litellm 63,591 (+2), new-api 52,258 (+2), ollama 187,703 (+2), llama.cpp 135,799 (+1), vllm 97,246 (+1), jan 45,907 (+1). Licences all match including `NOASSERTION` for litellm/dynamo/jan, `AGPL-3.0` new-api, `BSD-3-Clause` triton, tensorzero `isArchived: true`, Portkey last commit `2026-05-25`, one-api last commit `2025-02-21`, RouteLLM `2024-08-10`, petals `2024-08-25` / release `2023-09-06`, crowdllama `2025-11-27` | none. Add one line to §1: *"Star counts re-verified 2026-11-20 08:00Z; expect ±0.01 % drift on any later re-read."* |
| 27 | SMG: 734★, `smg launch --worker-urls … --policy cache_aware`, MLX listed, ten policies, no weights | **CONFIRMED** | `734` stars; README L66 `smg launch --worker-urls http://gpu1:8000 http://gpu2:8000 --policy cache_aware`; L87 *"vLLM · SGLang · TokenSpeed · TensorRT-LLM · **MLX (Apple Silicon)** · any OpenAI-compatible server (e.g. Ollama)"*; load-balancing doc policy table lists exactly ten: `cache_aware, least_load, power_of_two, bucket, consistent_hashing, prefix_hash, manual, round_robin, random, passthrough`. "weight" occurs only as `kv_pressure_weight` / `--least-load-kv-pressure-weight` | none |
| 28 | SMG: `rid` lineage stripping (`conv_t2_r1` → `conv`), `--max-idle-secs` default 14400, bounded spill at 2 in-flight, `--routing-key-override` | **CONFIRMED** | sticky-sessions doc: *"The key comes from the body rid with turn and retry suffixes stripped, so conv_t1, conv_t2, and conv_t2_r1 all pin as conv."* / *"a key idle for longer than --max-idle-secs (default 14400, four hours)"* / *"once a key has 2 of its requests in flight on its pinned worker, the next concurrent request for the key is placed again (cap_respill)"* / *"With the override, keyed requests stick on every policy"* | none |
| 29 | SMG: load poll every 10 s, `/v1/loads`, "no dashboard" | **CONFIRMED (poll) / not falsified (dashboard)** | *"The gateway polls each worker's load every --load-monitor-interval seconds (default 10). HTTP workers are asked for /v1/loads … falling back to the engine's Prometheus /metrics"*; report carries *"waiting requests and KV-cache usage, plus queued tokens, generation throughput and resident context tokens"* ✓. No dashboard/UI appears in README or routing docs | none |
| 30 | LocalAI SmartRouter ladder incl. "evict the least-recently-used model with zero in-flight requests"; PostgreSQL + NATS + gRPC workers | **CONFIRMED verbatim** | `localai.io/docs/administration/distributed-mode/` §Scheduling Algorithm: *"If the model is already loaded on a node → use it (per-model gRPC address)"* … *"If no node has capacity → evict the least-recently-used model with zero in-flight requests to free a node"* … *"If all models are busy → wait (with timeout) for a model to become idle, then evict"* … *"worker starts a new gRPC process on a dynamic port"*. Prerequisites: *"requires authentication enabled with a PostgreSQL database - SQLite is not supported"*, NATS required, *"Each model gets its own gRPC backend process"* | none |
| 31 | LocalAI "cannot front oMLX" | **CONFIRMED (absence, well-evidenced)** | No BYO-OpenAI-server worker mode anywhere in the distributed-mode page; workers *"don't have a fixed backend type - the SmartRouter dynamically installs the required backend via NATS"*, and the install path ends in `LoadModel` over gRPC. Phrase it as the doc does — "no 'bring your own OpenAI server' mode is documented" — not as a capability that was tested | soften to "is not documented" |
| 32 | LocalAI P2P: "Only a single model is supported currently" | **CONFIRMED verbatim** | `localai.io/docs/administration/p2p-federated-inference/` L255: *"Only a single model is supported currently."* | none |
| 33 | exo: MLX backend, `/instance/previews`, dashboard cluster view "4 × M3 Ultra … DeepSeek v3.1 and Kimi-K2-Thinking", 1.8×/3.2× | **CONFIRMED** | README: *"The /instance/previews endpoint will preview all valid placements for your model"*; `"sharding": "Pipeline"`; img alt *"4 x M3 Ultra Mac Studio with DeepSeek v3.1 and Kimi-K2-Thinking loaded"*; *"up to 1.8x speedup on 2 devices and 3.2x speedup on 4 devices"*; *"Devices running different versions of exo don't join each other"* | none |
| 34 | olla: 338★, "oMLX-native" badge, `/internal/ui/`, sticky-session README quote, three selectors | **CONFIRMED** | `readme.md` (lowercase): badge alt `oMLX: Native Support` / `oMLX-native`; *"**📌 Sticky Sessions**: KV-cache-aware affinity routing that pins multi-turn conversations to the same backend"*; *"embedded in the binary at /internal/ui/"*; backend table `oMLX \| Integration \| Passthrough`. Three selectors corroborated by `docs/olla-evaluation.md` §4: *"The binary symbols name exactly three selectors: LeastConnectionsSelector, PrioritySelector, RoundRobinSelector."* | none |
| 35 | vllm-project/router: five policies, "transparent OpenAI reverse proxies" | **CONFIRMED**, but §2 prose **WRONG** | README policy table: `round_robin, random, consistent_hash, cache_aware, power_of_two`; *"transparent OpenAI reverse proxies"*; **`cache_aware` also has Session Affinity = Yes** — so "session affinity only through `consistent_hash` on a header" contradicts the document's own evidence [5] | see C-10 |
| 36 | GAIE moved EPP/InferenceObjective/BBR out; packages "will be archived soon"; CNCF Sandbox llm-d since 2026-03 | **CONFIRMED** | GAIE README banner: *"The Endpoint Picker (EPP), InferenceObjective and InferenceModelRewrite APIs, and Body Based Router (BBR) packages have moved to new repositories … No new code will be accepted to these packages in this repository, and **they will be archived soon**."* llm-d README: *"[2026-03] llm-d joins the CNCF as a Sandbox project"* | none |
| 37 | agent-router: formerly Envoy AI Gateway, CRDs unchanged, `aigw run` on `:1975` | **CONFIRMED** | README: *"An Agentic AI Foundation project. Formerly Envoy AI Gateway."* / *"CRDs and API group are unchanged"* / *"point any OpenAI-compatible client at http://localhost:1975/v1"* | none |
| 38 | agent-router: "No weights", "18 hosted providers" | **WRONG / OVERSTATED** | API reference: `AIGatewayRouteRuleBackendRef` — *"Each backend can have a **weight** that determines the traffic distribution … Default is 1"*, and backends may be `AIServiceBackend` resources pointing at any OpenAI-compatible endpoint. README provider block has **22** provider logos, not 18 | see C-6, C-7 |
| 39 | B12: "no project has both per-backend weights for the same model **and** residency awareness" | **SURVIVES — but only with the "same model" qualifier** | Falsification attempts: **agent-router** now has per-backend weights (above) but no residency (AIServiceBackend is a URL; the endpoint picker reads KV/queue metrics, not loaded models). **llm-d / GAIE** have HTTPRoute `backendRef.weight` but each `InferencePool` is one model, so those weights split across *models*, and residency is pod-readiness. **vLLM production-stack** has KV/load-aware scoring (a scorer weight, not a backend weight). **Bifrost / Portkey** weight `provider × model × key`. **Dynamo / KServe** have neither | see C-11 |
| 40 | Dynamo KV-aware router quote; "GPU-only"; grove 298★ | **CONFIRMED** | README: *"KV-Aware Routing — Routes requests based on worker load and KV cache overlap"*; grove `298` stars, `v0.2.1-beta1` | none |

---

## Mandatory corrections

Exact replacement text, ready to paste. Line references are to the current files.

### C-1 — `litellm.md` §1, the Rust paragraph (replace the whole paragraph)

> **Notable 2026 repositioning.** The GitHub description now reads "The fastest, litest AI Gateway.
> **Rust core with Python SDK**". The docs contradicted that for the chat path, until the page was
> deleted: as of 2026-10-30, `/docs/proxy/rust_gateway` said "the Python proxy serves only two routes
> on Rust: `/ocr` for every provider and `/audio/transcriptions` for `bedrock` … Every other route,
> including `/v1/messages` and `/chat/completions`, runs on Python, and the per-model `rust: true`
> flag described below has no effect" (Source: `BerriAI/litellm-docs`, `docs/proxy/rust_gateway.md`
> at commit `f9d4a4d2`; the page was removed on 2026-11-10 by commit `fabf8478` and the URL now
> 404s). Its replacement, `docs.litellm.ai/rust-migration`, publishes progress instead of per-route
> coverage: 53 % of "Major APIs" built in `v1.111.0-rc.2`, with `/v1/messages` end-to-end on Rust a
> **target for 2026-12-01**, not a shipped path. Treat "Rust core" as roadmap, not as the state of
> the request path.

And in §10 sources, replace item 22:

> 22. https://github.com/BerriAI/litellm-docs/blob/f9d4a4d2/docs/proxy/rust_gateway.md — the deleted
> Rust gateway page: only `/ocr` and `bedrock` `/audio/transcriptions` on Rust, `/chat/completions`
> on Python, `rust: true` inert. Accessed 20.11.2026; removed from `main` on 2026-11-10.

### C-2 — `litellm.md` §7, first paragraph

> **Deployment.** Docker image (`ghcr.io/berriai/litellm`, cosign-signed with a key pinned by commit
> `e82a3c6`), Helm chart, `uv tool install 'litellm[proxy]'`, deploy buttons for
> Render/Railway/AWS/GCP.

### C-3 — `litellm.md` §8, licence block quote

> "All content that resides under the `litellm/enterprise/` directory of this repository, if that
> directory exists, is licensed under the license defined in `litellm/enterprise/LICENSE`. Content
> outside of the above mentioned directories or restrictions above is available under the MIT
> license."

### C-4 — `litellm.md` §3.3 "Capacity" bullet

> - **Capacity**: `max_parallel_requests` per deployment — but counted **per worker process, not
>   shared across workers or pods** ("The cap is counted in process, so it is per proxy worker and is
>   not shared across workers or pods"). Over the limit the request fails **immediately with 429**
>   naming the deployment — "There is no wait queue and nothing to configure for one: requests never
>   sit in the proxy waiting for a slot, they either run or get the 429" (Documented: `/docs/routing`).
>   This is a hard rejection, not a spill, and on an N-worker proxy the effective fleet-wide cap is
>   N × `max_parallel_requests`.

Also fix the two places that inherit the error:

- §5.4 table row: `| `max_parallel_requests` → 429 | Per deployment and per worker process, not per model; hard rejection with no spill and no queue. |`
- §9 table row: `| Slot counting per model | `max_parallel_requests` per deployment (per worker process) | Per deployment, not per model |`

### C-5 — `litellm.md` §10 source 17

> 17. https://docs.litellm.ai/blog/long-prompt-latency — TTFT 553 ms → 35 ms on a 440k-token prompt
> after skipping the prompt-cache eligibility count. Accessed 20.11.2026.

(and the same path fix wherever §4.2 cites it inline: `/blog/long-prompt-latency`, not `/docs/blog/…`)

### C-6 — `landscape.md` §7 table row for agent-router, "Weighted distr." cell

> | theagentrouter/agent-router | 2,423 | Apache-2.0 | Go | v1.3.0 · 2026-11-18 | yes | Yes | No | **Yes** (HTTPRoute `backendRef.weight`) [11] | No [11] | No | Via custom provider | No |

and rewrite evidence 11:

> 11. agent-router API reference, `AIGatewayRouteRuleBackendRef`: "Each backend can have a weight
>     that determines the traffic distribution … Default is 1", inherited from Gateway API
>     `BackendRef`. Backends are `AIServiceBackend` resources pointing at any OpenAI-compatible
>     endpoint, so two self-hosted backends serving the same model can be weighted — this is the one
>     project in the table with real per-backend weights. Residency is still absent: the Tier Two
>     gateway defers picking to an endpoint picker that reads KV-cache and queue state, not which
>     model is loaded. No eviction guard.

### C-7 — `landscape.md` §3, agent-router paragraph

> **theagentrouter/agent-router (formerly Envoy AI Gateway)** — 2,423★, Apache-2.0, Go, v1.3.0
> (2026-11-18). Renamed in 2026 (now an Agentic AI Foundation project), CRDs unchanged
> (`AIGatewayRoute`, `AIServiceBackend`). Runs standalone on a laptop (`aigw run`, listens on
> `:1975`), but its centre of gravity is provider credentials, quotas and failover; the "Tier Two
> Gateway" defers inference-aware picking to an endpoint picker. 22 provider integrations in the
> README, and per-backend weights on `AIGatewayRouteRuleBackendRef` — the only real ones in this
> group — but zero local-fleet residency features.

### C-8 — `litellm.md` §7, known-complaints table: relabel the date column

Change the header `| Issue | State | Substance |` to `| Issue | State (opened) | Substance |`, and
correct the three closed issues:

- `#47482` → **closed 2026-11-10** (opened 2026-10-20)
- `#38193` → **closed 2026-09-03** (opened 2026-08-25)
- `#50874` → **closed 2026-11-11** (opened 2026-11-10)

### C-9 — `litellm.md` §7, release cadence

> **Release cadence and stability.** 968 PyPI releases; six releases published on 2026-11-20 alone
> (`v1.112.0-dev.1`, `v1.106.12`, `v1.108.13` LTS, `v1.109.3`, `v1.110.2`, `v1.111.0-rc.3`).

### C-10 — `landscape.md` §2, vllm-project/router paragraph

> **vllm-project/router** — 527★, Apache-2.0, Rust, v0.1.17 (2026-11-04), commit 2026-11-20. Fork of
> the same SGLang gateway, narrowed to vLLM: five policies (`round_robin`, `random`,
> `consistent_hash`, `power_of_two`, `cache_aware`), session affinity through `consistent_hash`
> (on a header) and through `cache_aware`'s prefix locality. HTTP workers are "transparent OpenAI
> reverse proxies". No weights, no residency, no dashboard, no MLX.

### C-11 — `landscape.md` §7 or §9, the uniqueness claim

Wherever the "no project has both" claim appears, state it as:

> No project in this landscape combines **per-backend weights across backends serving the same
> model** with **model-residency awareness**. agent-router has the weights and no residency; LocalAI
> has residency and no weights; llm-d and GAIE have HTTPRoute weights, but each `InferencePool` is a
> single model, so those weights split across *models* and "residency" degrades to pod-readiness.

---

## What a hostile reader would actually catch

Ranked by damage, if the documents ship unchanged:

1. **The `rust_gateway` citation (A7).** Someone clicks `/docs/proxy/rust_gateway`, gets a 404, and
   concludes the quote was invented. It was not — but it is sourced to a page deleted ten days before
   publication. Fix the citation and the finding survives intact; leave it and every other quote in
   the document becomes suspect.
2. **`0112e53` (A5).** A commit hash is the one thing a reader can check in five seconds with a
   single click. It is wrong.
3. **agent-router "No weights" (B38).** This is the only substantive error in the landscape table,
   and it is the row a reader is most likely to know: Envoy AI Gateway's weight support is inherited
   from Gateway API and is documented in its API reference.
4. **The `max_parallel_requests` framing (A14).** Not a misquote — an over-reading. "Per deployment"
   sounds like a fleet-wide capacity limit; the docs say per worker process. Since the whole RIPPLE
   argument turns on capacity semantics, this one will be quoted back.

Everything else is polish. The two documents' central claims — that LiteLLM has no concept of a
loaded model, that its affinity is a pin map and not a placement decision, and that LocalAI's
distributed scheduler is the only written-down residency + eviction policy in the landscape — are
all confirmed against primary sources, in several cases verbatim.

---

Part of the competitive analysis: [docs/competitive-analysis.md](../competitive-analysis.md). Licence of this text: MIT, like the project.
