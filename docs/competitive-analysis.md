# RIPPLE in the field — what exists, what does not, and when to use what

**Date:** Nov 20th, 2026
**Scope:** 435 open-source repositories screened, 16 profiled, 12 commercial products,
1 previously measured competitor (Olla). Four detailed profiles in
[`docs/comparisons/`](comparisons/), each with its own sources.
**Question:** Does a tool already do what RIPPLE does — and if so, why does RIPPLE exist?

---

## 1. Verdict

1. **The surrounding platform already exists, and it is better than what RIPPLE has.** Keys, budgets,
   spend tracking, 100+ provider adapters, guardrails, observability, SSO: LiteLLM does all of it, in
   production, with a community. RIPPLE deliberately does none of it (NZ-6). If those are your
   requirements, this document tells you to use LiteLLM.
2. **The placement layer does not exist — not as a product, not as a project.** No open-source and no
   commercial tool in this survey combines (a) the conversation as the unit of placement, (b) a
   weighted target share that holds at *every* sample size, and (c) knowledge of which model is
   *loaded* in which machine's memory, including a refusal instead of an eviction. The compound claim
   survived an adversarial check that was specifically trying to falsify it.
3. **The closest three each fail on a different axis.** SMG (734★, Rust) has the best thread keys in
   the field and no residency concept. LocalAI has the only written-down eviction ladder in the
   landscape and cannot front oMLX at all. Olla was measured against these two Macs and cannot express
   3:2 (see [`olla-evaluation.md`](olla-evaluation.md)).
4. **For most people, the honest answer is: do not build this.** If you rent capacity, the problem
   RIPPLE solves does not exist — the vendor absorbs residency, eviction and cache warmth. RIPPLE is
   only the right build if you have decided to own the memory.
5. **What RIPPLE should take from the field is concrete and listed in §10** — 14 items, from SMG's
   routing-key grammar to LocalAI's zero-in-flight eviction predicate. A project that reads its
   competitors badly does not produce a list like that.

---

## 2. The question behind the question

The fair version of the criticism is not "this is vibecoded". It is: *"you wrote 20,000 lines of
Python for something a proxy config could do."* That deserves a precise answer, not a defence.

**Where the criticism is right.** A proxy in front of two OpenAI-compatible servers is not a hard
problem, and it has been solved many times: sticky routing, health checks, failover, a model list,
streaming. Every candidate in §6 has that, most of them in Go or Rust, with fewer moving parts than a
Python venv. RIPPLE did not invent any of it. Seven things in RIPPLE were adopted from competitors
rather than designed — the sticky key chain, the traceability headers, the dashboard layout and the
model filter come from Olla; hysteresis, atomic reservation and prefix-similarity-as-a-ratio come from
`vllm-project/router` ([`requirements.md`](requirements.md) §13.5-13.6).

**Where the criticism is wrong.** The load is not uniform, and the backends are not interchangeable.
In this fleet a model is 300-450 GB of unified memory, loading one costs 65-135 seconds measured, and
loading one *unloads another* — which is how a single test request evicted the 403 GB model of a
running agent session when Olla was tried (measured, [`olla-evaluation.md`](olla-evaluation.md) §5).
Under those conditions "least connections" is not a routing policy, it is a way to destroy other
people's warm state. That is the problem RIPPLE was written for, and §4 shows nobody else models it.

**What would change our mind.** A project that has properties 1-5 of §4. The survey in
[`comparisons/landscape.md`](comparisons/landscape.md) looked for exactly that, twice: once by
searching, once by trying to falsify the result. If it appears tomorrow, this document gets a new
row and RIPPLE gets a link.

---

## 3. Two different problems, routinely confused

Almost every "LiteLLM already does that" reply compares two different jobs.

| | **Problem A — placement on my own machines** | **Problem B — routing across vendors** |
|---|---|---|
| Backends | 2-10 machines you own, heterogeneous, each with fixed memory | 80+ providers, elastic, interchangeable by contract |
| Scarce resource | unified memory holding a *specific* model | nothing — capacity is rented per token |
| Failure mode | a load evicts a model another conversation is using | a provider is slow, expensive or rate-limited |
| Unit of interest | the conversation (it must stay put) | the request (each one may go anywhere) |
| Optimise for | cache warmth, residency, fairness of a *share* | price, latency, quality, uptime |
| Products | RIPPLE, SMG, Olla, LocalAI, llm-d, Kong, APIM | LiteLLM, OpenRouter, Portkey, Bifrost, Cloudflare, Bedrock IPR |

A Problem-B tool can be pointed at your machines (most accept a custom `base_url`), and then it does
Problem A *badly*: it sees URLs, not memory. A Problem-A tool is useless for Problem B. The rest of
this document keeps the two apart; every "no" in §5-§7 means "no for Problem A".

---

## 4. What "the same" would have to mean — five properties

The comparison only means something if the bar is stated first. These are the five properties, and
who has them:

| # | Property | Why it is hard | Who has it |
|---|---|---|---|
| 1 | **The conversation is the unit of placement** — bound on the first request, kept for every turn and tool round, released on idle TTL, with short one-shot requests holding no slot at all | Requires a thread registry with identity that survives context compression, and a lifecycle that is not "per request" | RIPPLE (header → fingerprint → prefix similarity, `registry.py:111/522/583`); SMG (`x-smg-routing-key` / `rid` lineage, best in field); Olla (prefix hash, measured 5/5); Kong, APIM (hash only, no lifecycle) |
| 2 | **A weighted share as state, not as probability** — 3:2 must hold at 5 threads as well as at 50 | Needs a deficit accumulator over *new* threads; a weight field gives 3:2 only in expectation (5 threads → 3:2 or 4:1) | RIPPLE only (`placement.py:166`). Everyone else: static weights per request, or nothing |
| 3 | **Residency awareness** — the router knows which model is *loaded* on which node and prefers it | Requires a backend API that reports loaded models plus sizes; no gateway consumes one | RIPPLE (`placement.py:107`), LocalAI SmartRouter (own gRPC workers only) |
| 4 | **Eviction protection** — refuse (or wait) instead of letting a load unload a model a live thread holds | Requires forecasting the backend's own admission arithmetic (`Σ estimated_size + new` vs `hard_threshold × ceiling`) and naming the blocking model | RIPPLE (503 with model/node/blocking model), LocalAI (LRU with zero in-flight, then wait) |
| 5 | **Identity is not an address** — a reused DHCP address must not inherit another node's slots | Requires a node identity the backend reports and the proxy verifies | RIPPLE (`/api/cluster/node_id`, `identity_mismatch`). Nobody else in this survey |

Everything else — OpenAI-compatible entry, streaming, failover, model list, request log, dashboard —
is table stakes and is **not** a reason to prefer any tool. Properties 1-5 are the whole argument.

---

## 5. LiteLLM vs RIPPLE

Full profile: [`comparisons/litellm.md`](comparisons/litellm.md) (v1.110.2, 63,589★, MIT core with a
paid enterprise directory).

**Verdict: LiteLLM is the wrong tool for the placement problem and an excellent tool for everything
around it.** It would take over auth, keys, spend and observability — and it would still hand the
eviction decision to the backend, because it has no concept of a loaded model. The reason is
structural, not a missing config flag: LiteLLM routes **requests** between **interchangeable
deployments**; RIPPLE places **threads** between **non-interchangeable nodes whose memory is the
scarce resource**.

| RIPPLE requirement | LiteLLM mechanism | Gap |
|---|---|---|
| Thread as unit of placement, sticky for all turns and tool rounds | `session_affinity` pin map, TTL 3600 s, opt-in | A cache hint applied *after* routing, not a placement; skipped under `usage-based-routing` v1, `priority` groups, sync SDK calls and custom strategies; two bugs show it not holding (#50029 open, #47482) |
| Deficit rule to a weighted target share (3:2) | `weight: 3` / `weight: 2` in `simple-shuffle` | Weight is a per-request probability; there is no active-thread counter, so no controller can converge to a share |
| Capacity before weighting; spill when a node is full | `max_parallel_requests` → immediate 429, no queue, no spill — and counted **per proxy worker**, not per deployment | Opposite semantics: reject instead of redirect |
| Largest relative headroom among full nodes | — | No such rule; `least-busy` is absolute in-flight with a known undercount bug (#48994) |
| Model filter from the backend's model list | static `model_list` per deployment | If the model is on neither node, LiteLLM still sends it |
| Residency preference | — | No backend-state introspection at all |
| Eviction guard: 503 naming model/node/blocking model | — | No equivalent; the failure is invisible (the backend answers 200) |
| Slot counting per model | `max_parallel_requests` per deployment | Per deployment, not per model |
| Node identity via expected `/api/cluster/node_id` | `api_base` + `model_info.id` | The URL is the identity; DHCP reuse unaddressed |
| Streaming, no own timeout, retry only before the first byte | streaming yes; `timeout: 30` in the docs' own example; retries/fallbacks can move mid-thread | Different contract; a 65-135 s cold load needs explicit timeout surgery |
| Health via a cheap probe | health = a **real completion**, 16 tokens | On a 400 GB model the health check itself triggers a cold load and a possible eviction |
| Key pass-through, no auth gateway | virtual keys by default, master key | Deliberate difference: RIPPLE's non-goal is LiteLLM's core feature |

**What LiteLLM genuinely contributes**, if the two were combined rather than compared: virtual keys
with per-user/team budgets and spend attribution; 100+ provider adapters including `/v1/messages` and
`/v1/responses`; observability callbacks (Langfuse, LangSmith, GCS, Prometheus) and guardrails with
PII masking; a mature admin UI with SSO and audit; an enormous, fast-moving community.

**What adopting it here would cost**, honestly: PostgreSQL plus Redis for multi-replica correctness on
a two-Mac fleet; a Python process carrying an open 4-7× latency regression (#46276); a 30 s default
timeout in the documented example; health checks that issue real completions; and a routing model
whose central assumption — deployments are interchangeable — is the one assumption this fleet
violates. Adding residency would mean forking the router or writing a custom strategy, and a custom
strategy is documented to lose `session_affinity` unless it selects through `router.select(..., kind="healthy")`.

**Division of labour, if one is wanted:** RIPPLE as the placement layer; LiteLLM in front of it only
when keys, spend or provider breadth become requirements — with RIPPLE as the single "deployment"
LiteLLM sees, so LiteLLM's request-level routing never gets a chance to break thread affinity. Each
system then does the thing it was built for.

**When to just use LiteLLM:** one Mac or one server (nothing to place), any need for multi-tenancy or
budgets, any need for cloud providers next to your own models, or any team that cannot maintain
Python. That is a large share of real users, and they should not install RIPPLE.

---

## 6. The open-source field

Full scan with method, 16-project table and evidence footnotes:
[`comparisons/landscape.md`](comparisons/landscape.md). Stars, licences and dates are GitHub API
values read on Nov 20th, 2026.

| Project | ★ | Licence | Multi-host | Sticky threads | Weighted share | Residency-aware | Eviction guard | Verdict for a RIPPLE user |
|---|---|---|---|---|---|---|---|---|
| **RIPPLE** | — | MIT | yes | yes | **yes (deficit)** | **yes** | **yes (503)** | — |
| smg-project/smg | 734 | Apache-2.0 | yes | **yes (best)** | no | no | no | Closest transport twin; will route a thread into an eviction |
| thushan/olla | 338 | Apache-2.0 | yes | yes (measured) | no (measured) | no (measured) | no (measured) | Measured and rejected; four things adopted |
| mudler/LocalAI | 50,460 | MIT | yes | partial | no | **yes** | **partial** | Only project with the right eviction ladder; cannot front oMLX |
| exo-explore/exo | 48,500 | Apache-2.0 | yes | no | no | yes (own runtime) | no | Same hardware, wrong layer: replaces oMLX, shards one model |
| vllm-project/router | 527 | Apache-2.0 | yes | partial | no | no | no | Homogeneous workers; architecture harvested |
| llm-d / GAIE | 5,317 / 806 | Apache-2.0 | K8s only | partial | no | partial (prefix) | no | Most powerful, wrong environment |
| Kong AI Gateway | — | commercial | yes | yes (hash) | static | no | no | Closest commercial match |
| LiteLLM | 63,589 | MIT + enterprise | yes | pin (fragile) | probability | no | no | Use it — for Problem B |

**The three closest, and why a RIPPLE user would not switch:**

- **SMG** nails the transport and the thread keys (`conv_t2_r1` → `conv`, TTL, bounded spill) and
  routes by expected wait. It will happily place a thread on a node that then evicts someone else's
  400 GB model, and 3:2 is not representable among its ten policies.
- **LocalAI distributed** wrote down the eviction policy RIPPLE needs — *evict the LRU model with zero
  in-flight requests, otherwise wait, then fail* — and cannot be used here: its workers are its own
  gRPC processes, with PostgreSQL and NATS. Two Macs running oMLX cannot be its workers.
- **exo** is the only project that treats "several Macs with 512 GB" as normal, with RDMA over
  Thunderbolt and a dashboard that shows which model sits on which Mac. It is a runtime, not a proxy:
  adopting it means giving up oMLX, and it shards one model instead of keeping several resident.

**What looks similar and is not** (see landscape §10 for the full list with reasons): Ollama
(`OLLAMA_MAX_LOADED_MODELS` unloads the oldest model — exactly what the eviction guard prevents),
llama.cpp RPC and MLX distributed (split one model, no routing), Ray Serve and KServe (K8s control
planes), RouteLLM / semantic-router / NotDiamond (choose *which model*, not *which machine*).

---

## 7. Commercial products

Full profile with prices and dates: [`comparisons/commercial.md`](comparisons/commercial.md).

| Product | Can front my 2 Macs | Thread affinity | Weighted 3:2 | Residency-aware | Eviction guard | Price | Verdict |
|---|---|---|---|---|---|---|---|
| Kong AI Gateway | yes (`upstream_url`) | yes (header hash) | static | no | no | $25/mo + $200 per extra 1M req, $100/model/mo | Feature-closest; treats a model as an always-available URL |
| Azure API Management | yes ("self-hosted models") | partial ("session-aware") | static | no | no | $0.042/10k ops (Consumption) … $686.72/mo (Standard) | Only one with a real session mode; enterprise topology |
| Portkey | yes (`customHost`) | no | static | no | no | $49/mo | Governance you may not need, plus a WAN hop |
| Cloudflare AI Gateway | partial (public HTTPS only) | no | no | no | no | free core, 5% on credits | Good in front of public providers, wrong topology for a LAN |
| OpenRouter | partial (Enterprise private endpoints, catalog model only) | no | no | no | no | 5.5% (8% Business), BYOK 5% | Solves Problem B; your fleet becomes one provider among 80 |
| Bedrock IPR / Foundry / Vertex | no | no | no | no | no | per 1k requests / not public | Routing *between that vendor's models* |

**The honest economic counter-argument.** Two 512 GB Mac Studios cost roughly $44,600 (assumption:
MacRumors pricing, 28.10.2026). Amortised over 36 months plus an electricity ceiling (2 × 480 W at
37.0 ct/kWh) that is about **$1,542/month**. The same 400 GB-class model rented costs $29-108/month
for a solo developer and $108-1,566/month at 300M-4.5B input tokens. The build only wins above roughly
**4.3 billion input tokens per month uncached** — and with 90% prefix-cache hits the cloud bill drops
by about 4×, because vendors already sell the cache warmth RIPPLE exists to protect. RIPPLE is a
decision to own the memory, not a way to save money.

**Where a commercial product is genuinely better:** any need for frontier models (OpenRouter/Requesty,
5-5.5%); ten or more people with an audit requirement (APIM or Portkey — SSO, budgets, PII masking, a
signed DPA); bursty or mostly-idle load (rent); a contractual SLA or multi-region (nothing that runs on
your own hardware has one); edge caching for a public API (Cloudflare's free core).

**Why none of them can be "RIPPLE but hosted":** every commercial product models a backend as a
stateless URL that is always available. RIPPLE models a backend as a fixed amount of memory that
currently holds a specific model. In a Problem-B gateway no request can unload a model, so an eviction
guard has nothing to guard. Hosting eliminates the problem instead of solving it.

---

## 8. The requirement matrix

Only the distinguishing requirements, because the rest is table stakes (§4). `n/d` = not established
in this survey — not "no".

| Req. | Requirement (short) | RIPPLE | LiteLLM | SMG | Olla | LocalAI | Kong/APIM |
|---|---|---|---|---|---|---|---|
| FA-1 | OpenAI-compatible entry point | yes | yes | yes | yes | yes | yes |
| FA-2 | Deficit placement to a weighted share | **yes** | no | no | no (measured) | no | static only |
| FA-3 | Weights configurable without code change | yes | yes (probability) | no | no | no | yes (static) |
| FA-4 | Route only to nodes that offer the model | yes | static list | partial | yes | yes | partial |
| FA-5 | Capacity cap + spill to the node with most headroom | yes | 429, no queue | yes (load-based) | partial | yes | partial |
| FA-6 | Thread detection that survives compression | yes | pin, fragile | yes | yes | no | hash only |
| FA-7 | One-shot requests hold no slot | yes | no | no | no | no | no |
| FA-8 | Streaming without buffering | yes | yes | yes | yes | yes | yes |
| FA-9 | No own request timeout | yes | 30 s default | n/d | yes | n/d | n/d |
| FA-10 | Retry only before the first byte | yes | retries mid-thread | n/d | n/d | n/d | n/d |
| FA-11 | Fallback to the surviving node | yes | yes | yes | yes (measured) | yes | yes |
| FA-12 | Prefer the node that already holds the model | **yes** | no | no | no (measured) | yes | no |
| FA-13 | `/healthz` per host + `/stats` | yes | partial | metrics only | partial | partial | partial |
| FA-14 | One log line per request with the reason | yes | yes | metrics | yes | yes | yes |
| FA-15 | Key pass-through, no auth gateway | yes | no (virtual keys) | yes | yes | partial | no |
| FA-16 | Warmup at startup | **no** (inert) | no | no | no | n/d | n/a |
| FA-18 | Eviction guard: 503 instead of evicting a live thread | **yes** | no | no | no (measured) | partial (wait, then evict) | no |
| FA-19 | Slots counted per model | yes | no | no | no | no | no |
| FA-21 | Node identity verified, not an address | **yes** | no | no | no | partial | no |
| FA-22 | Read-only dashboard with active models | yes | yes (no residency) | no | yes (no residency) | yes | yes (no residency) |
| FA-23 | Backend capabilities probed, visible degradation | yes | no | partial | no | no | no |

FA-16 is listed as **no** on purpose: `warmup` is parsed and refused with a warning
(`ripple/config.py:1902`), because NZ-4 forbids RIPPLE to load models on its own. The README used to
advertise it as working; that sentence is corrected in the same change as this document.

---

## 9. Decision guide

**Use RIPPLE if** all of these hold:
- you own two or more machines with large unified memory and want several 300-450 GB models *resident*;
- your work arrives as long conversations (agents, coding sessions) that must keep their cache warm;
- the machines are not identical, so "spread evenly" is the wrong answer and a share is the right one;
- you accept a LAN-only, single-point-of-failure proxy with no auth gateway (NZ-6/7);
- you can live with a Python venv and no HA.

**Use LiteLLM if** you need keys, budgets, spend, 100+ providers, or multi-tenancy — or if you have
one machine. Add RIPPLE *behind* it only if you also need placement.

**Use llm-d / Gateway API Inference Extension if** you are on Kubernetes and want prefix-cache-aware
routing with a control plane. Not for two Macs on a desk.

**Use LocalAI if** you want its distributed mode with its own workers — i.e. you are not attached to
oMLX. It has the best eviction policy in the landscape.

**Use a commercial router if** any part of the workload needs frontier models, or you need an SLA.

**Do not use RIPPLE if** you rent capacity, run one node, need WAN/cross-site, need per-user budgets,
or cannot accept that a restart drops every thread binding (no persistence, by design).

---

## 10. What RIPPLE should adopt from the field

Fourteen concrete items, in the order they would pay off. This list is the part of the analysis that
is not about marketing.

**From SMG** — routing-key lineage grammar (`X-Ripple-Thread`, then body `rid`, strip one `_t<N>` and
one `_r<N>` suffix, log the key source in a header); bounded pin spill (a third concurrent request for
the same thread is re-placed instead of stacked); idle pin TTL with a periodic sweep and a metric;
decision counters per branch (`thread_hit`, `deficit`, `residency`, `spillover`, `eviction_refused`) so
the 3:2 rule can be proven in production without reading logs; an explicit replica caveat in the docs.

**From LocalAI** — the placement ladder written down as a numbered list in code order (loaded → fits →
idle → least-loaded → evict LRU **with zero in-flight** → wait → 503), with a log line naming the step;
the stricter eviction predicate ("zero in-flight requests on that model, cluster-wide"); wait-then-503
with `Retry-After` instead of a flat refusal; a disk-headroom check before routing a cold model;
heartbeat-based staleness instead of URL identity.

**From exo** — a route-preview endpoint (`GET /admin/route-preview?thread=…&model=…`) returning the
chosen node, the rule that fired, both nodes' residency and the projected effect; a dashboard whose
first panel is *loaded models per host* with eviction risk; the memory plan as a first-class computed
object.

**From Olla** (measured, see [`olla-evaluation.md`](olla-evaluation.md) §9) — the sticky key fallback
chain, `X-*` traceability headers, the three-tab layout, model filtering by endpoint model list. Not
its balancers.

**What not to adopt from anyone:** probabilistic weights without a deficit accumulator. A weight field
alone gives 3:2 only in expectation; with five new threads you get 3:2 or 4:1.

---

## 11. What this document does not establish

- **No competitor was benchmarked here.** The comparison is on documented mechanism and source code,
  plus one measured case (Olla, 14.11.2026). Latency, throughput and overhead of LiteLLM, SMG, LocalAI
  or Kong against these Macs are **not** measured by us.
- **Stars and licences drift.** All values were read from the GitHub API on Nov 20th, 2026 and are
  re-checkable; the audit trail is [`comparisons/factcheck.md`](comparisons/factcheck.md), which lists
  40 verified claims and the corrections applied after the first draft (10 items, including two that
  weakened our own argument).
- **RIPPLE's own headline numbers are partly unmeasured.** TTFB overhead (NFA-1) and RSS (NFA-2) have
  no measurement in this repository. The eviction guard's most important acceptance scenario (AS-5b)
  is stub-tested, not run live. A restart drops all thread bindings. These are stated here because a
  comparison that hides them is not worth publishing — see
  [`comparisons/baseline.md`](comparisons/baseline.md) §6.
- **The commercial prices are list prices** read on Nov 20th, 2026, not quotes, and the
  rent-vs-build break-even rests on stated assumptions (hardware price, 36 months, 37.0 ct/kWh).
- **Coverage is bounded.** 435 repositories were screened with 24 GitHub queries; a project that does
  not describe itself with the vocabulary of routing, proxying or load balancing would have been
  missed. The falsification attempt in `comparisons/factcheck.md` B12 is the mitigation, not a proof.

---

## 12. Sources and audit trail

- [`comparisons/litellm.md`](comparisons/litellm.md) — LiteLLM technical profile, 10 sections, sources per claim
- [`comparisons/landscape.md`](comparisons/landscape.md) — 435 repos screened, 16 profiled, evidence footnotes
- [`comparisons/commercial.md`](comparisons/commercial.md) — 12 commercial products, list prices with dates
- [`comparisons/baseline.md`](comparisons/baseline.md) — RIPPLE's own capability inventory, FA-1…FA-23 with `file:line`
- [`comparisons/factcheck.md`](comparisons/factcheck.md) — adversarial verification of the four above
- [`olla-evaluation.md`](olla-evaluation.md) — the one competitor measured against these two nodes (14.11.2026)
- [`requirements.md`](requirements.md) §13 — the original make-or-buy check and what was adopted from `vllm-project/router`

All sources were accessed on Nov 20th, 2026. This document is MIT, like the project. If you find a
tool that satisfies properties 1-5 of §4, open an issue — the honest outcome of a comparison is a
better link in this file.
