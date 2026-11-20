# Commercial landscape

**Date:** Nov 20th, 2026
**Scope:** the proprietary and hosted half of the market that [`./landscape.md`](./landscape.md) and [`./litellm.md`](./litellm.md) deliberately exclude — managed AI gateways, routers sold as a product, hyperscaler model routing, and the "rent capacity instead of building a fleet" alternative.
**Question:** Does any commercial product model a backend as memory that currently holds a specific model — or do they all model it as a stateless URL that is always available?

Every price below was read from the vendor's own pricing page on that date; where a price is not public the text says **not public** rather than guessing. No account was created, no trial started, no payment data entered.

---

## 1. Method and scope

**What counts as commercial here.** A product with a public price list (or an explicit "contact
sales") that is sold as a service or a licensed control plane, where the routing/balancing logic is
not something the user can read and fork. That is the boundary against [`./landscape.md`](./landscape.md): Portkey's
open-source gateway is in that document, Portkey the hosted product is here; LiteLLM is excluded
from both and is not re-covered here.

**Inclusion test (three questions, answered per product):**

1. Can it accept an OpenAI-shaped request and forward it to an endpoint I run myself?
   (Note: this is a *different* capability from "it has a `base_url` field". A `base_url` field means
   one upstream per configured provider. Balancing across two Macs with affinity is a different
   feature, and §7 marks the difference explicitly.)
2. Does it have a placement *state model* — thread stickiness, weights, residency, eviction — or only
   a per-request scoring model (price/latency/quality)?
3. What does it cost at the scale of one person and two Macs?

**Excluded, with reason:**

| Excluded | Why |
|---|---|
| LiteLLM (hosted or self-hosted) | Covered by [`./litellm.md`](./litellm.md); it is the open-source control in that comparison, not a commercial product. |
| TrueFoundry, Cloudsmith-style "AI gateway" vendors with no public price | Nothing to cite; mentioning them would be a list, not a comparison. |
| NVIDIA NIM / NVIDIA AI Enterprise | An inference *runtime* licence. It would replace oMLX, not RIPPLE. |
| AWS SageMaker endpoints, Azure ML managed online endpoints | Hosting, not routing across machines I own. |
| Closed desktop apps (LM Studio) | Single host, no fleet. |
| "AI observability" tools without a data plane (LangSmith, Braintrust, Arize) | They do not sit in the request path as a balancer. |
| Everything in [`./landscape.md`](./landscape.md) | Already covered there. |

Prices are point-in-time and volatile; the date is attached to every figure.

**A note on what I could and could not read.** Vendor pricing pages are increasingly JS-rendered.
Where a number came from a rendered page I say "read on the pricing page". Where a row existed only
inside a collapsed table, I say so. Two figures in this document (Bedrock's routing fee, Mac Studio's
512 GB configuration) are flagged as such rather than presented as clean reads.

---

## 2. Two different problems people confuse

Everything in this market is sold as "AI gateway" or "model router", and the two words hide a fork in
the road.

**Problem A — "route my requests to the best of MY OWN machines."**
The endpoints are mine, they are few, they are heterogeneous, they are *stateful in a way that costs
money to change*: a 300–450 GB model that is resident in a Mac's unified memory takes ~70 s to
reload, and loading a second one evicts the first. Routing decisions therefore have a cost that
outlives the request. Capacity is fixed and known. The scarce resource is **memory residency**, not
tokens or dollars. This is RIPPLE's problem.

**Problem B — "route my requests to the best of MANY VENDORS' APIs."**
The endpoints belong to other people, there are hundreds, they are stateless from my point of view,
capacity is elastic, and the price per token differs per vendor. The scarce resources are **money,
latency and quality**. Every commercial product in this document solves Problem B. A few of them can
be *pointed at* Problem A's endpoints (a custom base URL), but none of them models it.

The distinction matters because it predicts which features exist. A Problem-B router has a scorer
over (price, latency, error rate, predicted quality). A Problem-A router needs a state machine over
(which model is loaded where, how much headroom, which threads are pinned to which node, what happens
to the other tenants of that node if I place this request here). No commercial product in this
document has the second thing, and §9 explains why that is structural rather than an oversight.

**Who solves what:**

| Product | Problem solved | Can be pointed at my own endpoint? |
|---|---|---|
| Portkey | B (+ governance) | Yes — "Local/Privately hosted provider", `customHost` |
| Cloudflare AI Gateway | B (+ edge caching/logging) | Yes — Custom Providers, `base_url` (HTTPS only) |
| Kong AI Gateway | B (+ API management) | Yes — `upstream_url` on the OpenAI provider |
| Helicone | B (+ observability) | Partly — gateway is provider-key oriented, see §3 |
| Databricks Unity Gateway | B (inside Databricks) | Not documented |
| Azure API Management | B (+ full API governance) | Yes — self-hosted models are an explicit backend class |
| OpenRouter | B | Only via Enterprise "Private Endpoints", catalog-model-shaped |
| Requesty | B | "Custom model hosting" listed on the Enterprise tier |
| NotDiamond | B, as a *recommender* | It is explicitly not a gateway |
| Bedrock IPR / Foundry model router / Google API Gateway routing | B, inside one vendor | No |
| Together / Fireworks / Groq / DeepInfra / Ollama Cloud | Neither — they *are* the capacity | n/a |

---

## 3. Managed AI gateways

### 3.1 Portkey

- **What it is.** A hosted gateway in front of ~1,600 models with virtual keys, fallbacks, load
  balancing, guardrails, prompt management and logging. The docs now brand it "Portkey (Prisma AIRS
  AI Gateway)", i.e. it has been folded into Palo Alto Networks' AI security line — the pricing page's
  "Upgrade Now" button points at `paloaltonetworks.com/ai-security/ai-gateway`.
- **Pricing (read 20.11.2026, https://portkey.ai/pricing).** Free Forever: 10k recorded logs/month,
  3-day log retention, gateway included. Production: **$49/month**, 100k logs, **+$9 per additional
  100k requests**, 30-day logs. Enterprise: custom.
- **Can it front my two Macs?** Yes, in the narrow sense. Portkey documents a "Local/Privately hosted
  provider" toggle and a `customHost` parameter, with the provider type set to `openai` because the
  server speaks the OpenAI schema (https://portkey.ai/docs/integrations/llms/vllm). That makes each
  Mac a *provider*.
- **Balancing.** `{"strategy": {"mode": "loadbalance"}, "targets": [{"provider": "@a", "weight":
  0.7}, {"provider": "@b", "weight": 0.3}]}` — weighted, per request, on the provider list
  (https://portkey.ai/docs/product/ai-gateway/load-balancing).
- **Residency/eviction.** None. There is no concept of a model being loaded, of memory headroom, or
  of a request that must not be placed because it would evict something else. Weights are static
  fractions applied per request; there is no deficit accumulator, so 3:2 holds only in expectation.

### 3.2 Cloudflare AI Gateway

- **What it is.** A gateway that sits on Cloudflare's edge in front of provider APIs, adding
  analytics, caching, rate limiting, DLP and guardrails.
- **Pricing (read 20.11.2026, https://developers.cloudflare.com/ai-gateway/reference/pricing/).**
  Core features — dashboard analytics, caching, rate limiting — are **free on all plans**. Log
  retention follows the Workers plan (100k logs on Workers Free, 10M per gateway on Workers Paid).
  Unified Billing credits carry a **5% fee**; inference is passed through with no markup. DLP is free;
  guardrails are billed as Workers AI tokens.
- **Can it front my two Macs?** Yes — "Custom Providers" takes a `base_url` that "must start with
  `https://`" and is designed for "Internal AI models… Route requests to your own AI infrastructure"
  (https://developers.cloudflare.com/ai-gateway/configuration/custom-providers). This is the single
  hardest constraint for a LAN fleet: the gateway is at the edge, so the Macs must be reachable over
  HTTPS from the public internet. That is a tunnel, a certificate and an exposure decision, not a
  config line.
- **Balancing.** Fallbacks and rate limiting are documented; weighted distribution across two custom
  providers is not. The Universal Endpoint that used to do multi-provider fallback is marked
  **deprecated** (https://developers.cloudflare.com/ai-gateway/usage/universal).
- **Residency/eviction.** None.

### 3.3 Kong AI Gateway

The most feature-complete of the managed gateways for this specific question, and the one whose
marketing language comes closest to RIPPLE's vocabulary.

- **What it is.** The AI plugins of Kong Gateway, sold through Konnect as "AI Management".
- **Pricing (read 20.11.2026, https://konghq.com/pricing).** Plus: **from $25/month plus usage**.
  Serverless AI gateway control plane: **$25/month per control plane**, 1M requests included,
  **$200 per additional 1M requests/month**, max 10M requests/month, **$100/month per model** with a
  5-model limit on Plus. Dedicated Cloud Gateways: **$500/month per control plane + $0.15/GB**.
  Hybrid: **$200/month per control plane**. Fully self-hosted AI gateway: custom (Gateway Enterprise).
- **Can it front my two Macs?** Yes: "You only need to configure a URL if you're using a self-hosted
  or OpenAI-compatible endpoint, in which case set the `upstream_url` option in your AI Model
  configuration" (https://developer.konghq.com/ai-gateway/ai-providers/openai).
- **Balancing (https://developer.konghq.com/ai-gateway/load-balancing/).** Four documented
  algorithms: **weighted round-robin**, **consistent-hashing** on a configurable header (default
  `X-Kong-LLM-Request-ID`) described as "enabling sticky sessions", **least-connections**, and
  **lowest-usage** by token count. This is genuinely the closest commercial analogue to RIPPLE's
  weight + affinity pair.
- **Residency/eviction.** None. Consistent hashing is *prefix-cache* affinity in intent ("sticky
  sessions for maintaining context"), not residency: the docs never claim the gateway knows whether a
  model is loaded. Nothing refuses a request to protect a loaded model, because for Kong a model is a
  URL that is always available.
- **The pricing trap for a two-Mac setup.** The model-proxy limit on Plus is 5 models at $100/model/
  month, and the request allowance is 1M/month. RIPPLE's reference deployment serves two models to a
  handful of agent clients; the request volume would sit far under 1M, so the cost would be
  ~$25–$125/month to reproduce weighted round-robin plus header stickiness — i.e. the two features
  RIPPLE already has — while adding a hop off-LAN.

### 3.4 Helicone

- **What it is.** Observability-first: logging, sessions, cost tracking, prompt management, with a
  gateway bolted on (`https://ai-gateway.helicone.ai`).
- **Pricing (read 20.11.2026, https://www.helicone.ai/pricing).** Hobby free (10k requests, 7-day
  retention); **Pro $79/month**; **Team $799/month**; Enterprise custom (on-prem deployment listed).
  Gateway features listed on the plan table: caching, rate limits, automatic fallbacks. Credits carry
  **0% markup** (https://docs.helicone.ai/gateway/overview).
- **Can it front my two Macs?** The gateway is built around provider keys and a model registry ("we
  manage provider API keys for you"), and its documented routing is "automatic routing and fallbacks"
  across providers. A custom OpenAI-compatible upstream is not part of the gateway overview I read.
  Treat this as **not documented** rather than "no".
- **Residency/eviction.** None.
- **Note.** The pricing page carries a banner: "Helicone is no longer part of Mintlify. Details to
  come soon." That is a product-status risk worth knowing before adopting it as a control plane.

### 3.5 Databricks Unity Gateway

- **What it is.** Databricks' AI gateway: model access, budgets, rate limits, guardrails, tracing, and
  "smart routing" that "automatically route[s] requests to the best-fit model and harness for cost,
  speed, and quality" across "Claude, GPT, Gemini, Grok, Kimi and GLM" plus open models
  (https://www.databricks.com/product/artificial-intelligence/unity-gateway).
- **Pricing.** **Not public** as a per-request or per-seat figure. Databricks bills in DBUs at
  per-second granularity and the Unity Gateway page defers to a quote
  (https://www.databricks.com/product/pricing). Anything quoted to you as "the Unity Gateway price"
  is a negotiated number.
- **Can it front my two Macs?** Not documented. The gateway's backends are Unity Catalog model
  services and external model providers. A pair of Macs on a LAN is not one of those classes.
- **Residency/eviction.** None. Its "traffic management" is "load balance traffic across models and
  providers, with automatic fallbacks" — a Problem-B feature.
- **Verdict.** Only relevant if you are already a Databricks shop. For a two-Mac operator it is a
  platform purchase to solve a problem you do not have.

### 3.6 Azure API Management (AI gateway)

The most interesting commercial product in this section, because it is the only one whose load
balancer is documented with a *session* mode and whose backend list explicitly includes self-hosted
models.

- **What it is.** APIM's AI capabilities: token limits, semantic caching, content safety, and a
  backend load balancer, applied to LLM APIs.
- **Pricing (read 20.11.2026, https://azure.microsoft.com/en-us/pricing/details/api-management/).**
  **Consumption**: 0–1M API operations per subscription included, then **$0.042 per 10,000
  operations** — and the footnote states it is "Free for up to 100,000 requests when created as an AI
  Gateway in Azure AI Foundry". **Developer $48.04/month**, **Basic $147.17**, **Standard $686.72**,
  **Premium $2,795.17** (per unit; incremental units at 50%).
- **Can it front my two Macs?** Yes, explicitly: the capability list includes "**Self-hosted** models
  and endpoints" among the backend classes it manages
  (https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities).
- **Balancing.** "The backend load balancer supports round-robin, weighted, priority-based, and
  **session-aware** load balancing." Session-aware here means the APIM backend-pool feature that keeps
  a caller on one backend (by cookie or header, depending on configuration); it is a stickiness
  primitive, not a residency model. Weights are static per backend.
- **Residency/eviction.** None. APIM has no idea that loading model B on Mac 1 unloads model A.
- **Cost shape for this use case.** Consumption tier at $0.042/10k operations is ~$4.20 per million
  requests, which is cheap; but the request path now leaves the LAN and comes back through an Azure
  region, and the Macs must be reachable from it. For a LAN-latency design (`requirements.md`: single-
  digit-millisecond RTT) that is a category error, not a price problem.

---

## 4. Routers as a product

### 4.1 OpenRouter

- **What it is.** A marketplace and router over 600+ models from 80+ providers, with a 5.5% platform
  fee on pay-as-you-go credit purchases (read 20.11.2026, https://openrouter.ai/pricing; Business 8%).
- **Routing criteria.** Price, latency, throughput and uptime per provider, plus "auto-routing,
  preferred vendor selections", data-policy routing and EU/US in-region routing — all listed on the
  pricing page's feature table.
- **Can it route to my own endpoints?** Two mechanisms, and both have a shape that excludes a private
  fleet:
  1. **BYOK** — bring provider API keys. The fee becomes "5% of what the same model/provider would
     cost normally on OpenRouter", with a plan-dependent allowance measured in list-price inference
     ($25,000/month on pay-as-you-go) (https://openrouter.ai/docs/use-cases/byok). BYOK means *keys to
     providers OpenRouter already supports*, not arbitrary base URLs.
  2. **Private Endpoints** — an **Enterprise add-on** that lets you enter "the HTTPS base URL of your
     endpoint and the upstream model or deployment ID it serves". The constraints are decisive: the
     endpoint "must be hosted by a provider that OpenRouter supports for BYOK" and "must serve a model
     that already exists in the OpenRouter catalog, with the same request and response shape"
     (https://openrouter.ai/docs/guides/routing/private-endpoints). So a Mac running a catalog model
     can be *added to that model's provider list* — one endpoint per model, preferred when it has
     access, no weights, no thread affinity, no residency.
- **Residency/eviction.** None, and it cannot exist: OpenRouter's providers are assumed to have the
  model available.

### 4.2 NotDiamond

- **What it is.** A routing *recommender*, and it says so: "Not Diamond is not a gateway. Rather, we
  integrate with your existing gateway infrastructure"
  (https://www.notdiamond.ai/pricing).
- **Pricing (read 20.11.2026).** **$0.05 per million tokens routed**, pay-as-you-go; Enterprise
  custom. Their FAQ quotes "average added per request latency of our router is 100–150ms" — a quoted
  vendor number, and 20–40× the latency budget RIPPLE sets for itself (NFA-1: p95 < 20 ms).
- **Routing criteria.** Predicted quality/cost from a learned model over prompt semantics, "KV cache
  state", session outcomes and harness signals.
- **Can it route to my own endpoints?** Not directly — it recommends a model from a pool, and your
  gateway executes. In principle the pool could include a self-hosted model, but the product's value
  is choosing between *frontier vendor models*; choosing between two identical Macs is not a decision
  it is trained for.
- **Residency/eviction.** None.

### 4.3 Requesty

- **What it is.** A gateway with routing policies, caching, fallbacks, latency routing and "custom
  routing rules" (read 20.11.2026, https://www.requesty.ai/pricing).
- **Pricing.** **5% markup** on model spend, no per-seat fee, free tier 200 requests/day, BYOK on the
  pay-as-you-go tier, Enterprise custom.
- **Can it route to my own endpoints?** "Custom model hosting" appears in the plan comparison — on the
  Enterprise tier. Not documented at the level of detail needed to say whether two LAN Macs can be
  pooled with weights.
- **Residency/eviction.** None. "EU data residency" on the pricing page is about where Requesty
  processes data, not about which machine holds a model — an easy phrase to misread.

### 4.4 Martian and Unify — two routers that stopped being routers

Both are worth one paragraph because they are still cited in comparison articles as live options.

- **Martian** (`https://withmartian.com/`, read 20.11.2026) now presents itself as a research company
  ("We're a team of researchers who've left the big labs to focus on understanding machine
  intelligence"). There is no router product page and no public pricing on the site I read.
- **Unify** (`https://unify.ai/`, read 20.11.2026) now sells "Continual-ARC — continual learning for
  AI teams" and presents itself as a model-training product. No router pricing on the site.

Neither publishes a price, so neither can be recommended. If you are reading a 2025-era comparison
table that lists them as routing products, it is out of date.

---

## 5. Hyperscaler model routing

The word "routing" here means something narrower than anywhere else in this document: choosing
between **that vendor's own model deployments**, by predicting which one answers a given prompt well
enough at a lower price.

**AWS Bedrock — Intelligent Prompt Routing.**
"Routes prompts to different foundational models **within a model family**", choosing "any two models
from the same family" (Anthropic Haiku/Sonnet, Meta Llama, Amazon Nova)
(https://aws.amazon.com/bedrock/intelligent-prompt-routing/). Vendor claim: "can reduce costs by up
to 30% without compromising on accuracy" — quoted, not verified. Price: AWS lists **$1 per 1,000
requests** for Intelligent Prompt Routing in the on-demand table of
https://aws.amazon.com/bedrock/pricing/. That row sits inside a collapsed table and did not render in
my extraction; I am quoting the pricing page's own indexed row rather than a rendered read, and
flagging it as such. Two models, same family, both on Bedrock: nothing here can be pointed at a Mac,
and the "router" is a classifier, not a balancer.

**Microsoft Foundry — model router.**
"Model router is a trained language model that intelligently routes your prompts in real time to the
most suitable large language model… You deploy model router like any other Foundry model"
(https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-router). It is billed as a
model deployment; the per-token price of the router deployment itself is **not public** on the concept
page I read. Backends are Foundry model deployments (plus Claude, which must be deployed separately).
No self-hosted endpoints, no weights, no affinity.

**Google Cloud — API Gateway model routing.**
Explicitly positioned as "a managed alternative to client-side proxies such as LiteLLM": it "accepts
OpenAI-compatible prompt requests, transcodes them in-flight, and routes them to specific Gemini
Enterprise Agent Platform models", i.e. "Gemini, Anthropic Claude, or OpenAI GPT family models"
(https://docs.cloud.google.com/api-gateway/docs/model-routing-overview). Pricing is API Gateway
pricing, **not public** as a per-request routing fee on that page. This is the closest a hyperscaler
comes to "put an OpenAI-shaped front door on my models" — and it still only reaches models in Model
Garden.

**The common structure.** In all three, the router's candidate set is a catalogue the vendor owns,
every entry is always available, and the decision variable is price × predicted quality. There is no
memory, no eviction, no per-machine state — because the vendor's capacity problem is solved by
someone else's scheduler.

---

## 6. The "rent instead of build" alternative

This is the comparison that actually decides the question, and it is uncomfortable for the build.

### 6.1 Assumptions (all stated, all checkable)

| Assumption | Value | Source |
|---|---|---|
| Hardware | 2 × Mac Studio M5 Ultra, 512 GB unified memory | RIPPLE's reference fleet shape |
| Capex per node | **$22,299** minimum (M5 Ultra 36c/80c at $6,799 + $15,500 for 512 GB) | MacRumors, 28.10.2026, quoting Apple's store: https://www.macrumors.com/2026/10/28/mac-studio-m5-ultra-512gb-ram/ ; Apple's own configurator prices the 512 GB option only after configuration, which is why a named third party is cited for the delta |
| Capex for the fleet | **$44,598** | 2 × $22,299 |
| Amortisation | 36 months (and 60 months shown for sensitivity) | assumption |
| Power ceiling per node | **480 W** ("Maximum continuous power") | Apple, Mac Studio (2025) tech specs: https://support.apple.com/en-us/122211 |
| Electricity ceiling | 2 × 0.48 kW × 730 h = **700.8 kWh/month** → **€259.30/month** at €0.370/kWh | BDEW Strompreisanalyse, 2026 average household price 37.0 ct/kWh: https://www.bdew.de/service/daten-und-grafiken/bdew-strompreisanalyse |
| FX | €1 = **$1.1681** (ECB reference rate) | https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/eurofxref-graph-usd.en.html |
| Model priced | DeepSeek-V4.1-Flash (the model class RIPPLE actually serves) | Together / Ollama Cloud price lists |
| Input:output ratio | 20:1 (long agent contexts, short answers) | assumption; stated so you can re-run it |

The electricity figure is a **ceiling**: it assumes both Macs draw maximum continuous power 24/7.
Real inference load is bursty and idle draw is far below 480 W, so the true electricity bill is
lower. Using the ceiling biases the comparison *in favour of the build*, which is the direction an
honest comparison should err.

### 6.2 Rented prices for the same model (read 20.11.2026)

| Vendor | Model | Input /1M | Cached input /1M | Output /1M | Source |
|---|---|---|---|---|---|
| Together | DeepSeek V4.1 Flash | $0.30 | $0.006 | $1.20 | https://www.together.ai/pricing |
| DeepInfra | DeepSeek-V4-Flash | $0.09 | $0.018 | $0.18 | https://deepinfra.com/pricing |
| Ollama Cloud | deepseek-v4.1-flash | $0.30 (off-peak $0.15) | $0.006 | $1.20 (off-peak $0.60) | https://ollama.com/pricing |
| DeepInfra | Kimi-K3 (frontier-class) | $2.85 | $0.285 | $14.25 | https://deepinfra.com/pricing |
| Groq | gpt-oss-120b | $0.15 | — | $0.60 | https://console.groq.com/docs/models |
| Groq | Llama 3.3 70B, Muse Spark Open, MiniMax M2.7 | **not public** ("Contact Sales") | — | — | same page |

Groq belongs in the list for completeness, not as a candidate: every model on its public list that
carries a price is ≤120B parameters, and the larger entries are quote-only. A 300–450 GB model is not
something you rent from an LPU vendor.

### 6.3 Monthly cost, three volumes

Volumes are **input tokens per month** with output at 1/20 of that. "Cache 90%" assumes 90% of input
tokens hit a provider prefix cache — the normal shape of an agent loop resending the same context,
and the reason RIPPLE cares about prefix affinity at all.

| Volume profile | Tokens/month | Together, no cache | Together, 90% cache | DeepInfra, no cache | Ollama Cloud off-peak, no cache |
|---|---|---|---|---|---|
| P1 one developer, chat + light agent | 300M in / 15M out | **$108** | $29 | $30 | $54 |
| P2 one developer, heavy agentic | 1.5B in / 60M out | **$522** | $125 | $146 | $261 |
| P3 small team (3 heavy users) | 4.5B in / 180M out | **$1,566** | $375 | $437 | $783 |

Build cost for the same month: **$1,542** at 36-month amortisation ($1,238.83 capex + $302.88
electricity ceiling), or **$1,046** at 60 months.

### 6.4 What the numbers say

- At **P1 and P2, renting is 3–14× cheaper than owning**, on the same model family, before counting
  the operator's time.
- The build only wins at roughly **4.3 billion input tokens/month** uncached (≈143M/day, ≈P3 and
  beyond) — and only against Together's list price. Against DeepInfra's V4-Flash price the break-even
  moves past **15.5 billion input tokens/month**, which is a small company's volume, not a person's.
- With realistic prefix-cache hit rates the break-even moves out again by roughly 4×, because cached
  input is priced at 1/50th of fresh input ($0.006 vs $0.30). **Cloud vendors already sell the thing
  RIPPLE exists to protect.** That is the single most important sentence in this document.
- The counter-argument is not price. It is: data never leaves the LAN; no rate limits; no vendor can
  deprecate the model; no per-token meter to watch; and the fleet works when the internet does not.
  Those are real, and none of them appear on a price list.
- If the Macs are **already bought**, the marginal cost of using them is the electricity (~€259/month
  ceiling, likely much less), and the comparison flips: at P2 the fleet's marginal cost is about 60% of
  Together's uncached price and comparable to Ollama Cloud's off-peak price. The honest
  statement is therefore conditional: *renting wins the purchase decision, owning wins the sunk-cost
  decision.*
- **Dedicated GPU rental** as a third option: Fireworks lists on-demand H200 at **$0.134/minute =
  $8.00/hour** (https://fireworks.ai/pricing). 400 GB of weights ÷ 141 GB per H200 = at least 3 cards,
  so ≥$24/hour ≈ **$17,520/month** for always-on dedicated capacity — an order of magnitude above both
  alternatives, and only rational if you need guaranteed throughput that serverless will not give you.

---

## 7. Table

Legend: **Yes** = documented and configurable. **Partial** = possible but with a constraint that
matters. **No** = not documented. "Weighted 3:2" means a *deficit-corrected* 3:2 over new threads;
a static weight is marked **static**.

| Product | Model | Can front my own 2 Macs? | Thread affinity | Weighted 3:2 | Residency-aware | Eviction guard | Price model | Verdict for a RIPPLE user |
|---|---|---|---|---|---|---|---|---|
| **Portkey** | Managed gateway | Yes (`customHost`, "Local/Privately hosted provider") | No | Static weights per request | No | No | Free (10k logs) / $49/mo / custom | Governance and observability you do not need; adds a WAN hop |
| **Cloudflare AI Gateway** | Edge gateway | Partial (HTTPS `base_url` only — needs a public tunnel) | No | No | No | No | Free core; 5% on credits; logs on Workers plan | Good for caching/logging in front of public providers; wrong topology for a LAN fleet |
| **Kong AI Gateway** | Gateway + API mgmt | Yes (`upstream_url`) | **Yes** — consistent hashing on a header | **Static** weighted RR | No | No | $25/mo + $200 per extra 1M req; $100/model/mo (5 models) | Closest commercial feature match; still no residency, and priced per model |
| **Helicone** | Observability + gateway | Not documented | No | No | No | No | $0 / $79 / $799 / custom | Observability product; gateway is a side feature |
| **Databricks Unity Gateway** | Platform gateway | Not documented | No | No | No | No | **Not public** (DBU/quote) | Only if already on Databricks |
| **Azure API Management** | Enterprise gateway | **Yes** — "self-hosted models and endpoints" | **Partial** — "session-aware" LB | **Static** weighted | No | No | Consumption $0.042/10k ops (100k free as AI Gateway in Foundry); Dev $48.04, Basic $147.17, Std $686.72, Prem $2,795.17/mo | The only one with a real session mode; enterprise price and topology |
| **OpenRouter** | Router/marketplace | **Partial** — Enterprise "Private Endpoints", must serve a catalog model | No | No | No | No | 5.5% platform fee (8% Business); BYOK 5% of list | Solves the *other* problem; the fleet becomes one provider among 80 |
| **Requesty** | Router/gateway | Partial ("custom model hosting", Enterprise) | No | No | No | No | 5% markup; free 200 req/day | Same as OpenRouter, smaller |
| **NotDiamond** | Routing recommender | No (not a gateway) | No | No | No | No | $0.05 per 1M tokens routed | Recommends models; 100–150 ms added latency per their FAQ |
| **Martian** | — | — | — | — | — | — | **Not public** — no router product on site | Not currently a purchasable router |
| **Unify** | — | — | — | — | — | — | **Not public** — pivoted to continual learning | Not currently a purchasable router |
| **Bedrock IPR** | Hyperscaler routing | No | No | No | No | No | $1 per 1,000 requests (see §5 caveat) | Two models, one family, on Bedrock |
| **Foundry model router** | Hyperscaler routing | No | No | No | No | No | Billed as a model deployment; price not public | Foundry-only |
| **Google API Gateway routing** | Hyperscaler routing | No | No | No | No | No | API Gateway pricing; routing fee not public | Model Garden-only |
| **Together / Fireworks / DeepInfra / Ollama Cloud** | Capacity rental | n/a — they *are* the backend | n/a | n/a | n/a | n/a | Per token (see §6.2) | Cheaper than owning below ~4B input tokens/month |
| **RIPPLE** (reference) | LAN proxy | n/a | **Yes** (thread key → node, TTL, spill cap) | **Yes** (deficit accumulator over new threads) | **Yes** (residency tier before dispatch) | **Yes** (503 rather than evict a live thread's model) | Self-hosted, MIT | The only row that answers Problem A |

---

## 8. Where a commercial product is genuinely the better choice

Be concrete, because "it depends" is not an answer.

1. **You need frontier models at all.** If any part of the workload wants Claude, GPT or Gemini, no
   amount of Mac hardware substitutes. OpenRouter or Requesty at 5–5.5% is the cheapest way to have
   one endpoint for everything, and Requesty's EU data residency option is a real differentiator for
   a European shop. RIPPLE explicitly does not do this (NZ-8).
2. **You are a team of ten or more with an audit requirement.** SSO, per-team budgets, PII masking,
   prompt logging with retention, and a signed DPA are things RIPPLE deliberately refuses to be
   (NZ-6). Azure APIM (Standard $686.72/month) or Portkey Production ($49/month, $9 per extra 100k
   requests) is the correct buy. The price is trivial next to the compliance work you would otherwise
   do yourself.
3. **Your load is bursty and mostly idle.** A solo developer whose agents run a few hours a day, or a
   product with a spiky launch profile, should rent. §6.3: P1 costs $29–108/month against $1,542/month
   of amortised hardware. Paying for idle unified memory is the most expensive way to buy tokens.
4. **You need a contractual SLA or multi-region.** Two Macs in one room are one power cut from zero.
   Bedrock/Foundry/Vertex sell 99.9%+ and multi-region; nothing in [`./landscape.md`](./landscape.md)
   or this document that runs on your own hardware does.
5. **You want prompt/response caching at the edge for a public API.** Cloudflare AI Gateway's free
   core (caching, rate limiting, analytics) is genuinely good value if your endpoints are already
   public, and the 5% Unified Billing fee with zero inference markup is honest pricing.
6. **You want weighted distribution plus header stickiness and nothing else, and you already run
   Kong.** Kong AI Gateway's weighted round-robin + consistent-hashing pair is the closest commercial
   match to RIPPLE's core loop, and at $25/month plus usage it is not expensive — as long as you
   accept that it treats a model as an always-available URL.

---

## 9. Where none of them can help, and why

The structural reason, stated once and precisely:

**Every commercial product here models a backend as a stateless URL that is always available. RIPPLE
models a backend as a fixed amount of memory that currently holds a specific model.**

From that one difference, everything else follows:

1. **There is nothing to protect.** An eviction guard exists because placing a request can destroy
   another client's warm state. In a Problem-B gateway, placing a request on provider X cannot harm
   provider Y, and no request can unload a model. A gateway has no eviction concept because its world
   has no evictions. You cannot configure a feature whose precondition does not exist in the data
   model.
2. **Weights are per request, not per thread.** Portkey, Kong and APIM all distribute *requests* by
   static weight. That gives 3:2 in expectation and drifts in practice: 5 new threads can land 4:1,
   and 40 land 24:16 ± noise. RIPPLE's deficit accumulator places *new threads* on whichever node is
   furthest below target, so the ratio holds at every sample size. This is not a tuning difference; it
   is a different unit of accounting, and none of the commercial products has a thread as a first-
   class object with its own target share.
3. **Affinity without semantics is just a hash.** Consistent hashing (Kong) and session-aware LB
   (APIM) keep a caller on a backend. They do not know that the caller's thread has gone idle, that a
   node is at its thread cap, that a thread should spill after two in-flight requests, or that an
   ephemeral one-shot prompt should not hold a slot at all. RIPPLE's FA-5/FA-7/FA-19 behaviours have
   no commercial equivalent because they are properties of *conversations*, and no gateway in this
   document stores conversations.
4. **The economics invert.** These products are priced per request, per log, per model or per seat.
   For two Macs serving a handful of clients, the control plane costs more than the electricity of the
   fleet it is managing — Kong's $100/model/month alone exceeds the ~€259/month power ceiling once you
   register three models. A commercial gateway in front of a two-machine LAN fleet is a cost centre
   that duplicates a 4,000-line Python process.
5. **The topology is wrong.** The gateways live at an edge (Cloudflare) or in a cloud region (APIM,
   Databricks, Portkey). RIPPLE's design assumption is single-digit-millisecond LAN RTT. Putting a
   cloud hop in front of a LAN fleet converts a 1 ms decision into a 30–100 ms one and makes the fleet
   dependent on the internet it was bought to avoid.
6. **And the deepest one:** the rent alternative removes the problem instead of solving it. If you
   rent, there is no residency to reason about, no eviction to guard, no 3:2 to maintain — the vendor
   has absorbed all of it. RIPPLE is only the right build if you have decided to own the memory. No
   commercial product can be "RIPPLE but hosted", because hosting is precisely what eliminates the
   problem RIPPLE solves.

---

## 10. Sources

All accessed **Nov 20th, 2026**. Prices are point-in-time.

**Gateways**

- Portkey pricing — https://portkey.ai/pricing
- Portkey load balancing — https://portkey.ai/docs/product/ai-gateway/load-balancing
- Portkey self-hosted / OpenAI-compatible provider (`customHost`) — https://portkey.ai/docs/integrations/llms/vllm
- Cloudflare AI Gateway pricing — https://developers.cloudflare.com/ai-gateway/reference/pricing/
- Cloudflare AI Gateway custom providers — https://developers.cloudflare.com/ai-gateway/configuration/custom-providers
- Cloudflare AI Gateway Universal Endpoint (deprecated) — https://developers.cloudflare.com/ai-gateway/usage/universal
- Kong pricing — https://konghq.com/pricing
- Kong AI Gateway load balancing — https://developer.konghq.com/ai-gateway/load-balancing/
- Kong OpenAI provider (`upstream_url` for self-hosted) — https://developer.konghq.com/ai-gateway/ai-providers/openai
- Helicone pricing — https://www.helicone.ai/pricing
- Helicone AI Gateway overview — https://docs.helicone.ai/gateway/overview
- Databricks Unity Gateway — https://www.databricks.com/product/artificial-intelligence/unity-gateway
- Databricks pricing (DBU model) — https://www.databricks.com/product/pricing
- Azure API Management AI gateway capabilities (load balancer modes, self-hosted backends) — https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities
- Azure API Management pricing — https://azure.microsoft.com/en-us/pricing/details/api-management/

**Routers**

- OpenRouter pricing — https://openrouter.ai/pricing
- OpenRouter BYOK — https://openrouter.ai/docs/use-cases/byok
- OpenRouter Private Endpoints — https://openrouter.ai/docs/guides/routing/private-endpoints
- NotDiamond pricing — https://www.notdiamond.ai/pricing
- Requesty pricing — https://www.requesty.ai/pricing
- Martian (current site) — https://withmartian.com/
- Unify (current site) — https://unify.ai/

**Hyperscaler routing**

- Bedrock Intelligent Prompt Routing — https://aws.amazon.com/bedrock/intelligent-prompt-routing/
- Bedrock pricing (IPR line item; collapsed table, see §5 caveat) — https://aws.amazon.com/bedrock/pricing/
- Microsoft Foundry model router — https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-router
- Google Cloud API Gateway model routing — https://docs.cloud.google.com/api-gateway/docs/model-routing-overview

**Capacity rental and cost inputs**

- Together AI pricing — https://www.together.ai/pricing
- Fireworks pricing (serverless + on-demand GPU) — https://fireworks.ai/pricing
- DeepInfra pricing — https://deepinfra.com/pricing
- Groq supported models and prices — https://console.groq.com/docs/models
- Ollama Cloud pricing — https://ollama.com/pricing
- Apple Mac Studio (2025) tech specs, 480 W maximum continuous power — https://support.apple.com/en-us/122211
- MacRumors, 28.10.2026, 512 GB configuration at $22,299 — https://www.macrumors.com/2026/10/28/mac-studio-m5-ultra-512gb-ram/
- Apple Store Mac Studio (configuration page; memory priced only after configuration) — https://www.apple.com/shop/buy-mac/mac-studio
- BDEW Strompreisanalyse, 2026 household average 37.0 ct/kWh — https://www.bdew.de/service/daten-und-grafiken/bdew-strompreisanalyse
- ECB euro reference rate USD, 20.11.2026 (1.1681) — https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/eurofxref-graph-usd.en.html

**Companion documents**

- [`./landscape.md`](./landscape.md) — open-source landscape, 20.11.2026
- [`./baseline.md`](./baseline.md) — RIPPLE capability inventory, commit `531c345`

---

Part of the competitive analysis: [docs/competitive-analysis.md](../competitive-analysis.md). Licence of this text: MIT, like the project.
