# Open-source landscape

**Date:** Nov 20th, 2026
**Scope:** open-source projects that could replace or compete with RIPPLE — an OpenAI-compatible proxy for a small, heterogeneous fleet of Apple-Silicon inference nodes (oMLX/MLX), where models are huge and resident (300–450 GB) and the hard problems are thread affinity, weighted distribution (3:2), residency-aware placement and eviction protection.
**Question:** Which open-source project already solves that problem, and what exactly is missing from each of the ones that come close?

Every star count, licence and date below was read from the GitHub REST API on that day (see §1 and §11).

---

## 1. Method

**Tooling.** No third-party software was installed and no inference node was touched. Data came from
`gh api` (authenticated as `vogel61`), which returns the same fields the repo page shows:
`stargazers_count`, `license.spdx_id`, `language`, `pushed_at`, `archived`, `releases/latest`,
`commits?per_page=1`. READMEs were pulled raw (`repos/{owner}/{repo}/readme`,
`Accept: application/vnd.github.raw`) and searched locally for the feature words that matter here
(`sticky`, `weight`, `evict`, `unload`, `resident`, `prefix cache`, `mlx`, `dashboard`). Documentation
pages were read only where a README made a claim that needed checking (SMG routing docs, LocalAI
distributed mode, LocalAI P2P).

**Search queries (24, `sort=stars`, 30 results each).**

| # | Query | # | Query |
|---|---|---|---|
| 1 | `openai-compatible proxy load balancer` | 13 | `omlx` |
| 2 | `llm inference gateway` | 14 | `mlx cluster inference` |
| 3 | `llm gateway proxy` | 15 | `ollama multi node` |
| 4 | `model router llm` | 16 | `model residency routing` |
| 5 | `mlx distributed` | 17 | `llm proxy sticky session` |
| 6 | `distributed inference cluster` | 18 | `apple silicon inference cluster` |
| 7 | `ollama load balancer` | 19 | `openai compatible router weights` |
| 8 | `kv cache aware routing` | 20 | `llm eviction model swap` |
| 9 | `inference scheduler` | 21 | `gpu pool llm router` |
| 10 | `llm serving router` | 22 | `distributed ollama` |
| 11 | `openai proxy` | 23 | `local llm cluster proxy` |
| 12 | `multi-model server apple silicon` | 24 | `inference gateway mlx` |

**Volume.** 435 unique repositories screened from the 24 searches, plus 47 repositories opened
individually (`/repos/{owner}/{repo}`) for exact release and commit dates. 30 READMEs fetched and
searched for the feature words above. 4 documentation pages read in full.

**Inclusion criteria.**

1. A separate process/service that accepts OpenAI-shaped HTTP and forwards it to one or more
   *other* inference servers (an engine that only serves its own models is not a candidate — it can
   only ever be a backend).
2. More than one backend supported, documented, and configurable without writing code.
3. OSI-approved licence and a public repository.
4. Maintained: a commit within the last 12 months. Anything older appears only in §10, with the
   reason.
5. Relevance to at least one of: small non-Kubernetes fleet, Apple-Silicon/MLX backends, thread
   stickiness, residency/eviction semantics, weighted distribution.

**Strictness rules used in the table.**

- **Residency-aware** = the router reads *which model is currently loaded in which backend's memory*
  and uses that as a routing input. Prefix-cache affinity is **not** residency: a prefix hit says
  nothing about whether the model is loaded, only about KV blocks.
- **Eviction guard** = the router refuses, queues or moves a request rather than let a backend unload
  a model that another client is using.
- **Weighted distribution** = a configurable weight across backends that serve *the same* model.
  Weights across API keys or providers are marked ⚑ and do not count.
- "Not documented" is written as such. No feature is inferred from a project's ambition.

**No benchmarks were run** and no performance figure appears below unless it is a quote with a
source.

*Fact-checked against primary sources on Nov 20th, 2026; corrections applied — see `docs/comparisons/factcheck.md`.*

---

## 2. Group A — local / multi-host inference proxies (non-Kubernetes, laptop/desktop, OpenAI-compatible)

This is RIPPLE's own group: one process you start next to your models, pointed at a handful of
machines over LAN.

**thushan/olla** — 338★, Apache-2.0, Go, v0.1.0 (2026-11-01). The only project in this group that
advertises oMLX as a first-class backend ("oMLX-native" badge in the README) and ships a read-only
fleet dashboard at `/internal/ui/`. Its sticky sessions are the strongest single feature
(`X-Olla-Session-ID`, prefix hash fallback). Already measured against both Macs on 14.11.2026:
3:2 is not representable, routing is by availability not residency, no eviction protection, +45.4 ms
overhead, path prefix `/olla/omlx/v1`. Full report: [`../olla-evaluation.md`](../olla-evaluation.md).
Note that the README's claim "KV-cache-aware affinity routing" is prefix affinity, not residency.

**smg-project/smg (Shepherd Model Gateway)** — 734★, Apache-2.0, Rust, v1.15.0 (2026-11-10),
commit 2026-11-20. Successor of the SGLang Model Gateway, now engine-agnostic, and the most complete
standalone multi-worker router in this group: `smg launch --worker-urls http://gpu1:8000
http://gpu2:8000 --policy cache_aware`. Ten policies, real conversation stickiness
(`--routing-key-override`, `rid` lineage, TTL, bounded spill), MLX listed as a supported backend, no
dashboard (Prometheus/OTel only). No weight field anywhere in the policy set. Deep dive in §8.1.

**vllm-project/router** — 527★, Apache-2.0, Rust, v0.1.17 (2026-11-04), commit 2026-11-20. Fork of
the same SGLang gateway, narrowed to vLLM: five policies (`round_robin`, `random`,
`consistent_hash`, `power_of_two`, `cache_aware`), session affinity through `consistent_hash`
(on a header) and through `cache_aware`'s prefix locality. HTTP workers are "transparent OpenAI
reverse proxies". No weights, no residency, no dashboard, no MLX.

**NVIDIA-NeMo/Switchyard** — 3,659★, Apache-2.0, Rust, v0.3.0 (2026-09-22). Looks like a router,
is a *model-tier* router: it decides whether an agent's next call needs the strong or the efficient
model (`llm_classifier`, `stage_router`, `composite`). Placement across hosts is out of scope; it
embeds as `libsy` in someone else's gateway. See §6.

**autonomous-ai/autonomous-grid (AI Grid)** — 279★, MIT, Python, v0.3.63 (2026-11-03). The closest
product *idea* to RIPPLE's deployment reality — "one OpenAI-compatible endpoint over the computers you
own", installs engines (`grid engine install llama.cpp`), supports MLX on Apple Silicon. But routing
is by model name only: "routed to whichever computer serves it". No stickiness, no weights, no
residency, no eviction. It is an installer plus a name-based dispatcher, not a placement engine.

**mudler/LocalAI** — 50,460★, MIT, Go, v5.0.0 (2026-11-18). A single-host model server with three
multi-machine modes bolted on: P2P federated (least-connections, experimental), P2P worker (shards
one llama.cpp model), and **distributed mode** (PostgreSQL + NATS + gRPC workers) whose SmartRouter is
the only scheduler in this entire landscape that is explicitly residency-aware and has an eviction
rule. It cannot front oMLX — its workers are LocalAI-managed gRPC processes. Deep dive in §8.2.

---

## 3. Group B — Kubernetes inference gateways and schedulers

Architecturally rich, operationally wrong for two Macs on a desk: they assume a control plane, a
service registry and pods that can be created and destroyed.

**llm-d/llm-d** — 5,317★, Apache-2.0, Python (manifests/guides), v1.0.0 (2026-10-20), commit
2026-11-18. "Optimized for production deployments on Kubernetes"; CNCF Sandbox since 2026-03; the
routing brain is the Endpoint Picker. Prefix-cache- and load-aware balancing, tiered KV offload,
predicted-latency scheduling. No weights, no residency, no eviction guard, no Apple Silicon.

**llm-d/llm-d-router** — 472★, Apache-2.0, Go, v1.0.0 (2026-10-16), commit 2026-11-20. New home of
the EPP, `InferenceObjective` and `InferenceModelRewrite` APIs, moved out of the Kubernetes SIG repo
in November 2026. Two deployment modes, both Gateway API based.

**kubernetes-sigs/gateway-api-inference-extension** — 806★, Apache-2.0, Go, v1.6.2 (2026-09-17).
Defines `InferencePool` and the ext-proc contract an inference gateway implements. As of this cutoff
it hosts the lightweight EPP (LWEPP), the `InferencePool` API and the conformance tests; the full EPP,
`InferenceObjective`/`InferenceModelRewrite` and the body-based router have moved to `llm-d-router`
and `llm-d-inference-payload-processor`, and the moved packages "will be archived soon". Routing
signal is "kv-cache and request cost aware" endpoint scoring.

**theagentrouter/agent-router (formerly Envoy AI Gateway)** — 2,423★, Apache-2.0, Go, v1.3.0
(2026-11-18). Renamed in 2026 (now an Agentic AI Foundation project), CRDs unchanged
(`AIGatewayRoute`, `AIServiceBackend`). Runs standalone on a laptop (`aigw run`, listens on
`:1975`), but its centre of gravity is provider credentials, quotas and failover; the "Tier Two
Gateway" defers inference-aware picking to an endpoint picker. 22 provider integrations in the
README, and per-backend weights on `AIGatewayRouteRuleBackendRef` — the only real ones in this
group — but zero local-fleet residency features.

**NVIDIA Dynamo** — 8,535★, Rust, v1.6.2 (2026-11-05), licence not auto-detected by GitHub
(`NOASSERTION`). "Datacenter-scale inference stack" above vLLM/SGLang/TensorRT-LLM. Its KV-aware
router "routes requests based on worker load and KV cache overlap" — prefix/KV locality, not model
residency. GPU-only by construction; the companion `ai-dynamo/grove` (298★) is gang scheduling for
Kubernetes.

**kserve/kserve** — 6,250★, Apache-2.0, Go, v0.21.0 (2026-09-25). Standard K8s model-serving CRDs,
OpenAI-compatible protocol, KV-cache offload. Pod-per-model, so "residency" is "is the pod ready".

**Also present, not tabled:** `vllm-project/aibrix` (5,194★, K8s control plane, prefix-cache and
engine-state routing, ModelClaim lifecycle), `kubeai-project/kubeai` (1,294★, prefix-aware proxy,
scale-from-zero), `vllm-project/production-stack` (2,730★, Helm chart around the vLLM router).

**Why the whole group is a category error for RIPPLE:** every one of them derives placement from
*pod* state. RIPPLE's unit is a *thread*, its backends are two machines that are always up, and its
scarce resource is 400 GB of resident weights that a single wrong request can evict. Gateway API
`HTTPRoute` does carry a `backendRef.weight`, but it is per-route and stateless — it cannot express
"24 of the next 40 new threads go to M5", because that requires an accumulator, not a probability.

---

## 4. Group C — multi-provider AI gateways

Built for the opposite problem: many *providers*, small models, no residency, and the customer's
problem is cost and keys.

**BerriAI/litellm** — 63,589★, Python, v1.110.2 (2026-11-20). The default answer to "OpenAI-compatible
gateway"; virtual keys, spend tracking, budgets, admin UI. Provider/deployment load balancing, no
thread affinity, no notion of a loaded model. Mentioned once here; not evaluated.

**Portkey-AI/gateway** — 13,420★, MIT, TypeScript, v1.15.2 (2026-01-12), last commit 2026-05-25.
README: "Distribute LLM requests across multiple API keys or AI providers **with weights**". Weights
are real ⚑ but they live on keys/providers; the routing target is a provider, not a machine holding a
400 GB model. Six months without a commit.

**maximhq/bifrost** — 9,795★, Apache-2.0, Go, helm-chart-v2.1.49 (2026-11-19), commit 2026-11-20.
23+ providers, web UI, "~10 ns to pick weighted API keys" ⚑. Same category, better maintained than
Portkey, same absence of placement semantics.

**higress-group/higress** — 9,798★, Apache-2.0, Go, v2.2.6 (2026-11-10). Envoy/Istio-based gateway with
an `ai-proxy` Wasm plugin covering mainstream providers; console included. No weighting documented for
backends of one model, no residency.

**apache/apisix** — 17,353★, Apache-2.0, Lua, 3.21.0 (2026-11-19). "Dynamic Load Balancing: Round-robin
load balancing with weight" and consistent-hash sessions — generic upstream features that predate LLM
serving. You could bolt oMLX behind it, and you would still have to write the residency logic.

**songquanpeng/one-api** (37,452★, MIT) and **QuantumNous/new-api** (52,256★, AGPL-3.0) — Chinese
key-pooling gateways with channel weights and a UI; one-api's last commit is 2025-02-21, new-api is
active. Both are account aggregators; nothing about models in memory.

**katanemo/plano** — 7,118★, Apache-2.0, Rust, 0.4.39 (2026-10-24). AI-native proxy/data plane for
agentic apps; routing by intent and model preference, not by residency.

---

## 5. Group D — distributed / aggregated inference runtimes

These make several machines behave like one bigger machine. That is a different product from making
several machines behave like a fleet.

**exo-explore/exo** — 48,500★, Apache-2.0, Python, app release v1.0.71 (2026-04-23), commit 2026-11-06.
The reference implementation for "cluster of Macs". MLX backend, MLX distributed for tensor
parallelism, RDMA over Thunderbolt (macOS 26.2+), automatic device discovery, a built-in dashboard
whose cluster view shows which models are loaded on which Mac, OpenAI/Claude/Responses/Ollama API
compatibility, and a placement planner with a preview endpoint (`/instance/previews`). Deep dive in
§8.3.

**b4rtaz/distributed-llama** — 3,141★, MIT, C++, v0.17.1 (2026-11-10). Root node loads and forwards
weights, workers process a slice of the network. One model, one cluster, no routing, no OpenAI server
beyond a thin one.

**ggml-org/llama.cpp RPC** — 135,798★, MIT, v1.0.0 (2026-11-13). `rpc-server` + `--rpc` splits one
model across hosts by layer. Explicitly not a fleet router; the model must fit the aggregate memory.

**ml-explore/mlx (MLX distributed)** — 29,220★, MIT, v0.32.4 (2026-10-14). `mlx.distributed` gives
ring all-reduce over Thunderbolt/IP; a library primitive, not a service. This is what exo and LocalAI's
MLX distributed mode are built on.

**ray-project/ray (Ray Serve LLM)** — 44,345★, Apache-2.0, ray-2.60.0 (2026-11-04). General
distributed serving with a real scheduler, replica autoscaling, and OpenAI-compatible ingress. Multi-
host yes; residency no — Ray schedules *replicas*, and a replica that is not running is a cold start,
not an eviction.

**sgl-project/sglang** — 38,866★, Apache-2.0, v0.5.23 (2026-11-02). Ships its own router (the ancestor
of SMG) and now lists Apple Silicon via Metal/MLX as a platform. The engine is the interesting part
here, not the router.

**mudler/LocalAI P2P** (see also §8.2) — federated mode assigns each incoming TCP connection to the
node with fewest active connections, keep-alive keeps it there; worker mode splits one llama.cpp model
proportional to memory. Its own docs note "Only a single model is supported currently."

**crowdllama/crowdllama** — 31★, MIT, Go, last commit 2025-11-27. Distributed Ollama across machines;
too small and too stale to matter, listed so nobody rediscovers it.

---

## 6. Group E — routers and classifiers, and why they are a different problem

A classifier answers *"which model should answer this prompt?"*. RIPPLE answers *"which machine already
holds this model, and where does this thread belong?"*. The first is a quality/cost decision made per
request from the prompt; the second is a placement decision made once per thread from cluster state.
They share the word "router" and nothing else.

- **lm-sys/RouteLLM** — 5,721★, Apache-2.0, last commit 2024-08-10, no release. Strong-vs-weak model
  choice by a trained classifier and a cost threshold. Dead for 27 months.
- **vllm-project/semantic-router** — 6,512★, Apache-2.0, Go, v0.4.0 (2026-09-27), commit 2026-11-20.
  The live successor to the idea: prompt classification, model selection, guardrails, a "decision
  layer for models and compute". Still chooses a model, not a host.
- **aurelio-labs/semantic-router** — 4,038★, MIT, v1.1.2 (2026-11-09). Embedding-based utterance
  routing inside an application. Library, not infrastructure.
- **NVIDIA-NeMo/Switchyard** — 3,659★, Apache-2.0, v0.3.0 (2026-09-22). Agent-aware tier routing with
  an evaluation loop; can be embedded in a gateway, has no concept of a backend.
- **NotDiamond, Martian, OpenRouter** — closed services. Same category: model choice by quality/cost.

Why this matters for the landscape: a RIPPLE reader who searches "LLM router" will land in this group
and conclude that the space is crowded. It is not crowded where RIPPLE stands — none of these projects
knows what a loaded model is.

---

## 7. Master table

Legend: **Yes** = documented and verifiable (evidence number in brackets, list below the table).
**Partial** = present in a narrower form, explained in the evidence. **No** = absent, and the absence
is itself evidenced. ⚑ = weights exist only across API keys/providers, not across backends of one
model. Star counts and dates: GitHub API, 20.11.2026.

| Project | Stars | License | Language | Last release / commit | Maintained? | Multi-host? | Sticky threads? | Weighted distr.? | Residency-aware? | Eviction guard? | Local Apple-Silicon backends? | Dashboard? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| thushan/olla | 338 | Apache-2.0 | Go | v0.1.0 · 2026-11-01 | yes (quiet) | Yes | **Yes** [1] | **No** [1] | **No** [2] | **No** [2] | **Yes** (oMLX profile) [1] | Yes [1] |
| smg-project/smg | 734 | Apache-2.0 | Rust | v1.15.0 · 2026-11-10 | yes | Yes | **Yes** [3] | No [4] | No [4] | No [4] | **Yes** (MLX listed) [4] | No (Prometheus) |
| vllm-project/router | 527 | Apache-2.0 | Rust | v0.1.17 · 2026-11-04 | yes | Yes | Partial [5] | No [5] | No [5] | No | No | No |
| mudler/LocalAI (distributed) | 50,460 | MIT | Go | v5.0.0 · 2026-11-18 | yes | Yes | Partial [6] | No | **Yes** [6] | **Partial** [6] | Yes (MLX modes) [7] | Yes [7] |
| exo-explore/exo | 48,500 | Apache-2.0 | Python | v1.0.71 · 2026-04-23 | yes | Yes | No | No | Yes (own runtime) [8] | No [8] | **Yes** (MLX native) [8] | **Yes** [8] |
| llm-d/llm-d | 5,317 | Apache-2.0 | Python | v1.0.0 · 2026-10-20 | yes | Yes (K8s) | Partial [9] | No | Partial [9] | No | No | No |
| llm-d/llm-d-router | 472 | Apache-2.0 | Go | v1.0.0 · 2026-10-16 | yes | Yes (K8s) | Partial [9] | No | Partial [9] | No | No | No |
| kubernetes-sigs/gateway-api-inference-extension | 806 | Apache-2.0 | Go | v1.6.2 · 2026-09-17 | yes | Yes (K8s) | No | No | Partial [10] | No | Partial (any HTTP engine) | No |
| theagentrouter/agent-router | 2,423 | Apache-2.0 | Go | v1.3.0 · 2026-11-18 | yes | Yes | No | **Yes** (HTTPRoute `backendRef.weight`) [11] | No [11] | No | Via custom provider | No |
| ai-dynamo/dynamo | 8,535 | NOASSERTION | Rust | v1.6.2 · 2026-11-05 | yes | Yes (K8s) | No | No | Partial (KV overlap) [12] | No | No | No |
| kserve/kserve | 6,250 | Apache-2.0 | Go | v0.21.0 · 2026-09-25 | yes | Yes (K8s) | No | No | No | No | No | No |
| BerriAI/litellm | 63,589 | NOASSERTION | Python | v1.110.2 · 2026-11-20 | yes | Yes | No | n/d | No | No | Via OpenAI endpoint | Yes |
| Portkey-AI/gateway | 13,420 | MIT | TypeScript | v1.15.2 · 2026-01-12 · c. 2026-05-25 | stale-ish | Yes | No | ⚑ Yes [13] | No | No | Via custom provider | Yes |
| maximhq/bifrost | 9,795 | Apache-2.0 | Go | helm-chart-v2.1.49 · 2026-11-19 | yes | Yes | No | ⚑ Partial [14] | No | No | Via Ollama/OpenAI | Yes |
| higress-group/higress | 9,798 | Apache-2.0 | Go | v2.2.6 · 2026-11-10 | yes | Yes | Partial (hash) | No | No | No | No | Yes |
| apache/apisix | 17,353 | Apache-2.0 | Lua | 3.21.0 · 2026-11-19 | yes | Yes | Partial (hash) [15] | Partial (upstream) [15] | No | No | No | Yes (separate repo) |
| autonomous-ai/autonomous-grid | 279 | MIT | Python | v0.3.63 · 2026-11-03 | yes | Yes | No | No | No [16] | No | Yes (MLX engine) | No (CLI) |

**Evidence**

1. olla README: "**Sticky Sessions**: KV-cache-aware affinity routing that pins multi-turn
   conversations to the same backend"; embedded read-only dashboard at `/internal/ui/`; backend table
   lists "oMLX | Integration | Passthrough". Measured on 14.11.2026: 5/5 sticky hits via
   `X-Olla-Session-ID`; no `weight` field read; three selectors only (`LeastConnections`,
   `Priority`, `RoundRobin`) → [`../olla-evaluation.md`](../olla-evaluation.md) §4, §6.
2. Same report §5: a request for a non-resident model was routed by `model_found` and evicted the
   403 GB resident model; "Olla neither prevented it nor reported it: the client saw HTTP 200".
   The README's "KV-cache-aware affinity" refers to prefix affinity, not loaded-model knowledge.
3. SMG docs, *Sticky Sessions and Routing Keys*: routing key from `x-smg-routing-key` or body `rid`
   with lineage suffixes stripped (`conv_t2_r1` → `conv`), pins on any policy, idle pins expire
   (`--max-idle-secs`, default 14400), bounded spill at 2 in-flight requests per key.
4. SMG docs, *Load Balancing*: ten policies listed — `cache_aware`, `least_load`, `power_of_two`,
   `bucket`, `consistent_hashing`, `prefix_hash`, `manual`, `round_robin`, `random`, `passthrough`.
   No weighted policy; "weight" appears only as `kv_pressure_weight` in the expected-wait score.
   README lists "MLX (Apple Silicon)" among self-hosted engines. Observability is Prometheus/OTel.
5. vllm-project/router README: policy table with `Session Affinity` column — `consistent_hash` = Yes
   ("Routes same session/user to same worker"), `cache_aware` = Yes; five policies total. HTTP workers
   described as "transparent OpenAI reverse proxies"; metrics via Prometheus only.
6. LocalAI docs, *Distributed Mode* → "Scheduling Algorithm": "(1) If the model is already loaded on a
   node → use it … (5) If no node has capacity → **evict the least-recently-used model with zero
   in-flight requests** … (6) If all models are busy → wait (with timeout) for a model to become idle,
   then evict". Sticky = partial: no thread affinity; the federated mode only keeps an HTTP keep-alive
   connection on one node.
7. LocalAI docs, *P2P / Federated Inference*: three modes, one of them "MLX distributed — Apple Silicon
   clusters running MLX models over the MLX distributed runtime"; web UI and a fleet operations
   dashboard (release notes, 4.10.0).
8. exo README: "exo uses MLX as an inference backend and MLX distributed for distributed inference";
   built-in dashboard whose cluster view shows "4 × M3 Ultra Mac Studio with DeepSeek v3.1 and
   Kimi-K2-Thinking loaded"; `/instance/previews` "will preview all valid placements for your model".
   No documented rule that refuses a request to protect a loaded model — placement is planned by the
   cluster, and a new model load is a cluster decision, not a router decision.
9. llm-d README: "Intelligent Routing … prefix-cache and load-aware balancing, including experimental
   predicted latency"; deployment on Kubernetes only. Affinity is prefix-based, not a thread pin.
10. GAIE README: inference scheduler uses "Metrics and Capabilities … such as Prefix Cache status or
    LoRA Adapters availability". Engine-agnostic HTTP, so an MLX server could sit behind it, but there
    is no MLX-specific integration.
11. agent-router API reference, `AIGatewayRouteRuleBackendRef`: "Each backend can have a weight
    that determines the traffic distribution … Default is 1", inherited from Gateway API
    `BackendRef`. Backends are `AIServiceBackend` resources pointing at any OpenAI-compatible
    endpoint, so two self-hosted backends serving the same model can be weighted — this is the one
    project in the table with real per-backend weights. Residency is still absent: the Tier Two
    gateway defers picking to an endpoint picker that reads KV-cache and queue state, not which
    model is loaded. No eviction guard.
12. Dynamo README: "KV-Aware Routing — Routes requests based on worker load and KV cache overlap".
    That is prefix/KV locality across replicas of a model, not knowledge of which model is resident.
13. Portkey README: "Load Balancing: Distribute LLM requests across multiple API keys or AI providers
    **with weights**". Key/provider level only.
14. Bifrost README: "Fast Key Selection — ~10 ns to pick weighted API keys"; "Load Balancing —
    Intelligent request distribution across multiple API keys and providers".
15. APISIX README: "Dynamic Load Balancing: Round-robin load balancing with weight"; "Hash-based Load
    Balancing: Load balance with consistent hashing sessions". Generic upstream features.
16. AI Grid README: "Grid puts every computer you own behind one OpenAI-compatible endpoint, and sends
    each request to whichever one runs the right model"; the routing section describes model-name
    dispatch only (`X-Grid-Routed-Model` header names the winner).

---

## 8. The three closest alternatives, in depth

Olla is the fourth and is already covered by measurement in
[`../olla-evaluation.md`](../olla-evaluation.md); it is not repeated here. The three below are the ones a RIPPLE user would actually evaluate next, for three
different reasons: same shape (SMG), same semantics (LocalAI), same hardware (exo).

### 8.1 SMG — Shepherd Model Gateway (smg-project/smg)

**What it is.** The SGLang Model Gateway, detached from SGLang and generalised: a Rust binary that
sits in front of vLLM, SGLang, TensorRT-LLM, TokenSpeed, **MLX** or "any OpenAI-compatible server
(e.g. Ollama)". 734★, Apache-2.0, v1.15.0 (2026-11-10), commits on the day of access.

**What it does well.**

- *Deployment shape is RIPPLE's shape*: `smg launch --worker-urls http://gpu1:8000 http://gpu2:8000
  --policy cache_aware`. No Kubernetes, no control plane, one binary, `pip install smg` or a container.
- *Thread stickiness is better than Olla's* and better than anything else in the non-K8s group. The
  routing key comes from a header (`x-smg-routing-key`) or the request body's `rid`, and SMG strips
  turn/retry suffixes so that `conv_t1`, `conv_t2`, `conv_t2_r1` all resolve to `conv`. Pins survive
  any policy (`--routing-key-override`), expire after `--max-idle-secs` (default 4 h) and — the part
  worth stealing — a third concurrent request for the same key is **re-placed** instead of queuing
  behind the pin ("cap_respill"), so one hot conversation cannot stack up on one node.
- *Honest operational surface*: per-worker in-flight counters, load reports polled every 10 s
  (`/v1/loads`, falling back to the engine's Prometheus), a documented expected-wait score
  `(queued + running + in_flight tokens) / throughput + kv_pressure_weight · k/(1-k)`, and decision
  counters labelled by policy branch.
- *Multi-replica safety* is at least discussed: the docs say plainly that each replica keeps its own
  pin map, and that `consistent_hashing` is the replica-independent alternative.

**What it cannot do for RIPPLE's case.**

- **No weights.** The policy table is exhaustive — ten policies, none of them weighted — and the word
  "weight" appears only inside the KV-pressure term of the load score. 3:2 is not representable, and
  not because of a missing config key: the design routes on *expected wait* and *prefix locality*.
  Even a weight field would not be enough (see §9.1 on the deficit accumulator).
- **No residency knowledge.** A worker's load report carries waiting requests, KV usage, queued tokens,
  throughput, resident context tokens. It does not carry "which model is loaded". SMG assumes every
  worker can serve every model — true for a vLLM pool of identical replicas, false for two Macs with
  400 GB of resident weights and a model filter.
- **No eviction guard.** Nothing in the routing path can refuse a request because serving it would
  unload someone else's model; the concept does not exist in the code path that chooses a worker.
- **No dashboard.** 90+ Prometheus metrics and OTel traces, no UI. RIPPLE's FA-22 (active models per
  host) has no counterpart.
- **Cold start is invisible to it.** A request for a model that exists on disk but is not loaded is
  just a request to a worker that will take 16 s to answer.

**Verdict.** The closest thing to a drop-in replacement for RIPPLE's *transport* layer, and the best
source of ideas for thread keys and pin lifecycle — but it solves the homogeneous-pool problem. Put
two heterogeneous Macs behind it and it will happily route a thread to a node that then evicts the
model another thread is using.

### 8.2 LocalAI distributed mode — the only residency-aware scheduler in the landscape

**What it is.** LocalAI (50,460★, MIT, Go, v5.0.0 2026-11-18) with `--distributed`: stateless
frontends behind a load balancer, workers that self-register, PostgreSQL for state, NATS for lifecycle
events, and a **SmartRouter** that decides where a model gets loaded.

**What it does well.** Its documented scheduling ladder is, step for step, the problem RIPPLE exists to
solve:

1. if the model is already loaded on a node → use it (per-model gRPC address);
2. drop nodes without room to *store* the model;
3. otherwise prefer nodes with enough free VRAM;
4. then idle nodes, then least-loaded;
5. if nothing fits → **evict the least-recently-used model with zero in-flight requests**;
6. if every model is busy → **wait (with timeout) for a model to become idle**, then evict;
7. `backend.install` event, worker starts a per-model gRPC process, `LoadModel`, record in the DB.

Steps 1, 5 and 6 are exactly residency awareness plus an eviction guard — and step 6 is a genuinely
better answer than RIPPLE's flat 503: it distinguishes "no capacity now" from "no capacity, come back",
and it degrades into a bounded wait. LocalAI also derives the model-load deadline from the checkpoint
size on disk, stages files only when nodes do not share a models volume, and refuses nodes without disk
headroom *at scheduling time* rather than halfway through a transfer.

**What it cannot do for RIPPLE's case.**

- **It is not a proxy in front of oMLX.** Workers are LocalAI processes speaking gRPC; the frontend
  loads models through `LoadModel`. There is no "bring your own OpenAI server" mode. Two Macs running
  oMLX cannot be its workers, so none of the above applies to RIPPLE's actual backends.
- **Heavy prerequisites.** PostgreSQL with auth enabled, NATS, a registration token, shared storage
  (or per-node staging). For two machines on a desk this is more moving parts than the thing it
  schedules.
- **No weights, no thread affinity.** Placement is by model and load, not by conversation. A thread
  can bounce between workers that both hold the model.
- **Not a fleet of resident giants.** The design assumes models are loaded and unloaded on demand;
  RIPPLE assumes they stay resident and that eviction is the failure mode.

**Verdict.** Not adoptable, extremely worth reading. It is the only project in the landscape that
wrote down the eviction policy RIPPLE needs, and it wrote it more carefully (LRU **with zero in-flight
requests**, wait-then-evict) than most readers would.

### 8.3 exo — the same hardware, a different product

**What it is.** 48,500★, Apache-2.0, Python, app v1.0.71 (2026-04-23) with commits through 2026-11-06.
Connects Macs into one cluster: MLX backend, MLX distributed, RDMA over Thunderbolt, automatic device
discovery, a dashboard with a cluster view, and OpenAI/Claude/Responses/Ollama-compatible endpoints.

**What it does well.**

- *It is the only project here that treats "several Macs with 512 GB each" as the normal case.*
  Device discovery is automatic; the dashboard shows which models are loaded on which Mac; RDMA over
  Thunderbolt is a first-class transport.
- *Placement is explicit and inspectable.* `/instance/previews` returns "all valid placements for your
  model", including the sharding strategy (e.g. `"sharding": "Pipeline"`) — a dry-run API for "where
  would this go and why". RIPPLE has nothing comparable, and it is cheap to add.
- *Memory-aware by construction*: tensor parallelism up to 1.8× on 2 devices and 3.2× on 4 (their
  number, not measured here) exists because the planner fits shards into aggregate memory.

**What it cannot do for RIPPLE's case.**

- **It is the runtime, not a proxy over runtimes.** exo replaces oMLX rather than fronting it. Adopting
  exo means giving up oMLX's model management, its residency reporting, its quantisation formats and
  its eviction semantics — for a cluster that shards *one* model across devices, not one that keeps
  *several* 400 GB models resident and picks between them.
- **No thread affinity and no weights.** Requests are routed by model name to wherever the cluster put
  it; there is no conversation pin, no 3:2, and no per-node preference.
- **No eviction guard in RIPPLE's sense.** Load decisions belong to the cluster planner; there is no
  documented rule that refuses a request because a running thread holds a model.
- **Version coupling.** Devices must run the same build and wire version to join, which is fine for a
  homogeneous cluster and hostile to a fleet where one Mac is mid-upgrade.

**Verdict.** The wrong layer, the right hardware. Read it for the placement-preview API, the cluster
view and the memory planner; do not expect it to proxy oMLX.

---

## 9. What RIPPLE should adopt from each

Concrete, in the order they would pay off.

### 9.1 From SMG

1. **Routing-key lineage grammar.** Accept a thread key from `X-Ripple-Thread` first, then the body's
   `rid`, and strip one trailing `_r<N>` and one `_t<N>` suffix so `conv_t2_r1` and `conv_t2` are the
   same thread. This removes the most common cause of a "new" thread on every turn in agent clients
   that regenerate request ids. Log the key source in a response header
   (`X-Ripple-Thread-Key-Source: header|rid|prefix-hash`).
2. **Bounded pin spill.** Keep a per-thread in-flight counter; when a thread already has 2 requests
   running on its node, place the third by the normal rule instead of stacking it. Prevents one
   agent loop from turning a 3:2 fleet into a 1:0 fleet.
3. **Idle pin TTL with a sweep.** `--max-idle-secs`-style expiry (SMG defaults to 4 h) plus a
   periodic sweep, with a metric for pins dropped. RIPPLE's thread table otherwise only grows.
4. **Decision counters per branch.** SMG exports `smg_worker_selection_total{policy,branch}` and a
   counter per decision branch (`occupied_miss`, `cap_respill`, …). RIPPLE should count *why* each
   placement happened — `thread_hit`, `deficit`, `residency`, `spillover`, `eviction_refused` — because
   that is the only way to prove the 3:2 rule is working in production without reading logs.
5. **State the replica caveat in the docs.** SMG says outright that each replica pins independently
   and points at consistent hashing for replica-independent placement. RIPPLE's docs should say the
   same about its thread table, or the thread store must be shared.

### 9.2 From LocalAI distributed mode

6. **Write the placement ladder down as a numbered list, in code order.** Loaded → fits → idle →
   least-loaded → evict LRU **with zero in-flight** → wait → 503. A single function with those steps
   in that order, and a log line naming the step, makes the eviction guard auditable. LocalAI's doc is
   the template.
7. **Tighten the eviction-guard predicate to "zero in-flight requests on that model, cluster-wide".**
   RIPPLE's guard is defined per running thread; the stricter predicate (no request at all touching
   the model on any node) is what LocalAI uses before it unloads, and it is the difference between
   "probably safe" and "provably safe".
8. **Replace the flat 503 with wait-then-503.** If the only node that can serve a model is busy with a
   model that cannot be evicted, hold the request for a bounded window (`Retry-After` + a reason
   header) and answer 503 only when the window expires. The client sees a slow request instead of a
   failure, and the dashboard can show "queued on capacity" as a distinct state.
9. **Check disk headroom before routing a cold model.** LocalAI rejects nodes that cannot *store* the
   model at scheduling time. RIPPLE already knows the model list per node; adding "is the file present
   on that node's disk" turns a 16.5 s cold start into a routing decision instead of a surprise.
10. **Heartbeat + stale-node detection instead of URL identity.** LocalAI's node registry compares
    every reported field against the last write and flags stale nodes; RIPPLE's node identity check
    (`/api/cluster/node_id`) should be paired with a heartbeat so a restarted node that comes back with
    a different model set is not trusted until it re-reports.

### 9.3 From exo

11. **A route-preview endpoint.** `GET /admin/route-preview?thread=…&model=…` returning the chosen
    node, the rule that fired, the residency state of both nodes and the projected effect (would this
    load, would it evict, would it be refused). exo's `/instance/previews` is the same idea for
    placements; for RIPPLE it is the cheapest possible debugging tool and it makes the 3:2 deficit
    visible.
12. **A cluster view that shows loaded models per host, not availability.** exo's dashboard literally
    shows which models sit on which Mac. RIPPLE's dashboard should make "Active models" the first
    panel, with size and the eviction risk of each host, because that is the state the operator
    actually reasons about.
13. **Treat the memory plan as a first-class object.** exo returns placements with a sharding strategy;
    RIPPLE should compute, per node, "resident + candidate model vs ceiling" before routing, and expose
    that number. It is the same arithmetic the eviction guard needs, computed once.

### 9.4 From Olla (already measured; see [`../olla-evaluation.md`](../olla-evaluation.md) §9)

14. Sticky key fallback chain header → prefix hash; `X-Olla-*`-style traceability headers; the
    three-tab dashboard layout; model filtering by endpoint model list. Do not adopt its balancers.

**What not to adopt from anyone:** probabilistic weights without a deficit accumulator. A weight field
alone gives 3:2 only in expectation; with 5 new threads you get 3:2 or 4:1, and with 40 you get
24:16 ± noise. RIPPLE's deficit rule is the correct mechanism, and none of the projects above has it —
which is precisely why the 3:2 requirement is a differentiator rather than a configuration detail.

---

## 10. Repos that look similar but are not

| Project | Why it is not a RIPPLE alternative |
|---|---|
| ollama/ollama (187,701★, MIT, v0.40.16) | An inference server, not a proxy. Its multi-model handling is `OLLAMA_MAX_LOADED_MODELS` + unload-oldest — the exact behaviour RIPPLE's eviction guard exists to prevent, with no way to refuse a request. Single host. |
| ggml-org/llama.cpp RPC (135,798★, v1.0.0) | Splits one model across hosts by layer. No model choice, no routing, no residency. |
| ml-explore/mlx distributed (29,220★, v0.32.4) | A collective-communication primitive. Building on it is what exo and LocalAI do; it is not a service. |
| bigscience-workshop/petals (10,722★, MIT) | Dead: last commit 2024-08-25, last release 2023-09-06 (38 months). Also a sharded-model swarm, not a fleet proxy. |
| lm-sys/RouteLLM (5,721★, Apache-2.0) | Dead: last commit 2024-08-10, no releases. And a quality/cost classifier, not a placement engine (§6). |
| tensorzero/tensorzero (11,700★, Apache-2.0) | **Archived** (last commit 2026-06-04). Was an observability-first gateway; do not build on it. |
| songquanpeng/one-api (37,452★, MIT) | Last commit 2025-02-21, last release 2025-02-02. Key-pooling for hosted providers. |
| mudler/LocalAI **P2P/federated** mode | Experimental, "Only a single model is supported currently", least-connections assignment, no residency. (LocalAI's *distributed* mode is a different thing — §8.2.) |
| autonomous-ai/autonomous-grid (279★) | Routes by model name only; the product is the installer and the engine management. No stickiness, weights, residency or eviction. |
| crowdllama/crowdllama (31★, MIT) | Distributed Ollama, last commit 2025-11-27, too small and too stale. |
| b4rtaz/distributed-llama (3,141★, v0.17.1) | Root/worker sharding of one model. Complementary to a fleet, not a replacement for its router. |
| vllm-project/production-stack (2,730★) | A Helm chart for vLLM on Kubernetes; the routing is vllm-project/router. |
| kubeai-project/kubeai (1,294★), vllm-project/aibrix (5,194★), kserve/kserve (6,250★) | Kubernetes control planes: placement is pod placement. Nothing to run on two Macs. |
| triton-inference-server/server (11,156★, BSD-3-Clause, v2.75.0) | Single host, but the one project here with *explicit* model load/unload control (`model_management.md`, model control API). Worth reading for the control-plane shape of "load this model now"; useless as a multi-host proxy. |
| NVIDIA-NeMo/Switchyard (3,659★), vllm-project/semantic-router (6,512★), aurelio-labs/semantic-router (4,038★) | Model-selection classifiers (§6). They choose which model should answer; RIPPLE chooses which machine holds it. |
| janhq/jan (45,906★), LM Studio (closed) | Desktop apps with an embedded server. Single host, no fleet, no proxy. |
| vllm-project/vllm (97,245★), sgl-project/sglang (38,866★) | Inference engines. They are what RIPPLE's backends are made of (oMLX plays this role); their routers are covered as SMG / vllm-project/router. |
| OpenRouter, NotDiamond, Martian | Closed services. Cost/quality routing to hosted models. |

---

## 11. Sources

All GitHub pages accessed **Nov 20th, 2026** via the GitHub REST API (`gh api`), which returns the
values rendered on the repo page. Star counts are point-in-time.

**Repositories (page + README + releases + last commit)**

- https://github.com/thushan/olla — README, releases v0.1.0/v0.0.29
- https://github.com/smg-project/smg — README, releases v1.15.0
- https://github.com/vllm-project/router — README, releases v0.1.17
- https://github.com/mudler/LocalAI — README, releases v5.0.0
- https://github.com/exo-explore/exo — README, releases v1.0.71
- https://github.com/llm-d/llm-d — README, releases v1.0.0
- https://github.com/llm-d/llm-d-router — README, releases v1.0.0
- https://github.com/kubernetes-sigs/gateway-api-inference-extension — README, releases v1.6.2
- https://github.com/theagentrouter/agent-router (formerly https://github.com/envoyproxy/ai-gateway) — README, releases v1.3.0
- https://github.com/ai-dynamo/dynamo — README, releases v1.6.2
- https://github.com/ai-dynamo/grove — releases v0.2.1-beta1
- https://github.com/kserve/kserve — README, releases v0.21.0
- https://github.com/vllm-project/aibrix — README, releases v0.8.0
- https://github.com/kubeai-project/kubeai — README
- https://github.com/vllm-project/production-stack — README
- https://github.com/BerriAI/litellm — README, releases v1.110.2
- https://github.com/Portkey-AI/gateway — README, releases v1.15.2
- https://github.com/maximhq/bifrost — README, releases helm-chart-v2.1.49
- https://github.com/higress-group/higress — README, releases v2.2.6
- https://github.com/apache/apisix — README, releases 3.21.0
- https://github.com/songquanpeng/one-api — releases v0.6.10
- https://github.com/QuantumNous/new-api — releases v1.0.0-rc.51
- https://github.com/katanemo/plano — releases 0.4.39
- https://github.com/autonomous-ai/autonomous-grid — README, releases v0.3.63
- https://github.com/b4rtaz/distributed-llama — README, releases v0.17.1
- https://github.com/crowdllama/crowdllama — README
- https://github.com/bigscience-workshop/petals — README, releases v2.2.0
- https://github.com/ggml-org/llama.cpp — releases v1.0.0
- https://github.com/ml-explore/mlx — releases v0.32.4
- https://github.com/ray-project/ray — releases ray-2.60.0
- https://github.com/sgl-project/sglang — README, releases v0.5.23
- https://github.com/vllm-project/vllm — releases v0.34.0
- https://github.com/lm-sys/RouteLLM — README
- https://github.com/vllm-project/semantic-router — README, releases v0.4.0
- https://github.com/aurelio-labs/semantic-router — releases v1.1.2
- https://github.com/NVIDIA-NeMo/Switchyard — README, releases v0.3.0
- https://github.com/triton-inference-server/server — README, releases v2.75.0
- https://github.com/ollama/ollama — releases v0.40.16
- https://github.com/janhq/jan — releases v0.8.8
- https://github.com/tensorzero/tensorzero — README, archived flag, releases 2026.6.0
- https://github.com/Helicone/helicone — releases v2025.08.21-1

**Documentation pages**

- SMG — Load Balancing: https://lightseek.org/smg/v1.15.0/concepts/routing/load-balancing
- SMG — Sticky Sessions and Routing Keys: https://lightseek.org/smg/v1.15.0/concepts/routing/sticky-sessions
- LocalAI — Distributed Mode: https://localai.io/docs/administration/distributed-mode/
- LocalAI — P2P / Federated Inference: https://localai.io/docs/administration/p2p-federated-inference/
- Triton — Model management: https://github.com/triton-inference-server/server/blob/main/docs/user_guide/model_management.md

**Internal**

- RIPPLE — Olla evaluation, 14.11.2026: [`../olla-evaluation.md`](../olla-evaluation.md)

**Not covered, by design:** LiteLLM (one line only, see §4), commercial control planes (NVIDIA NIM,
Databricks, Together), hosted routers (OpenRouter, Martian, NotDiamond), and anything requiring
Kubernetes operators beyond the six projects listed in Group B.

---

Part of the competitive analysis: [docs/competitive-analysis.md](../competitive-analysis.md). Licence of this text: MIT, like the project.
