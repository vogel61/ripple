# Requirements specification — RIPPLE (Lastenheft)

**Project:** RIPPLE — Router Inference Policy Proxy for LLM Engines
**Client:** the operator
**Prepared by:** Oliver Stein
**As of:** 14.11.2026 · Version 0.10 (draft for acceptance; Olla measured,
vllm-router/SGLang architecture harvested, adapter contract added)

> **Change 0.9 → 0.10:** New **§6.2 Backend adapter contract** with six
> capability flags (`availability`, `residency`, `capacity`, `lifecycle`,
> `identity`, `prefix_cache`) and the rule "no silent degradation":
> if a capability is missing, the requirement that depends on it switches
> off visibly. New **FA-23**. Remote providers (OpenRouter et al.) are
> delimited as expansion stage **M6** — policy routing, no balancing, default off.

> **Change 0.1 → 0.2:** The placement unit is now the **thread**, not the
> request (§6.0, FA-2, FA-5, FA-6, FA-7). The distribution arises from
> assigning new threads by deficit, not from weighted round-robin per
> request. Scenarios AS-1/AS-2/AS-4 adjusted, risk R-9 added.

---

## 1. Purpose

The balancer distributes OpenAI-compatible requests from a single entry
point across two oMLX machines (M5 : M3 = 3 : 2) by weight, without
splitting the models. It replaces the oMLX cluster mode, which is
disadvantageous in practice, for daily operation and leaves it untouched for
the one legitimate case (a model that fits on no single machine).

## 2. Starting situation (measured, 14.11.2026)

| Characteristic | M5 (Mac Studio M5 Ultra) | M3 (Mac Studio M3 Ultra) |
|---|---|---|
| RAM | 512 GB | 512 GB |
| Role | Entry point, weight 3, fallback | Second node, weight 2 |
| oMLX | 0.8.0rc1, port 8000 | port 8000 |
| Model inventory | 22 models | 6 models |
| currently loaded | DeepSeek-V4.1 (402,6 GB) + Qwen3.5-9B (6,5 GB) | nothing |

**Present on both and therefore in scope:**

| Model | Resident size | max_model_len | Use |
|---|---|---|---|
| `DeepSeek-V4.1-Flash-1017-oQ4e-mtp` | 402,6 GB | 524 288 | Main model (agent, long contexts) |
| `Qwen3.5-9B-oQ4e-mtp` | 6,5 GB | 262 144 | Vision |

Further measured facts that shape the requirements:

- **One API key is valid on both nodes** (with the key from `~/.omlx/settings.json`
  the M3 answers HTTP 200) — the balancer therefore does not have to swap keys.
- **Load time of a 400 GB model: 60–70 s.** A request that lands on a
  cold node waits that long.
- **DeepSeek on the M5:** prefill 611,7 tok/s, decode 200,5 tok/s (MTP) at
  204 k prompt tokens (measurement 25.10.2026).
- **Both nodes can hold exactly one 400 GB model.** DeepSeek and the 9B
  coexist; two large models do not.
- The OpenAI API is stateless — **there is no field that identifies a
  conversation** (see FA-6).

### 2.1 Transport paths to the M3 (measured, 14.11.2026)

The M3 is reachable via three paths; the choice is not a matter of taste:

| Path | Address | RTT avg | Jitter | HTTP `/health` |
|---|---|---|---|---|
| Thunderbolt 5 (point-to-point) | `203.0.113.2` | 0,60 ms | 0,16 ms | 6 ms |
| LAN wired (M3: 2,5 GbE, M5: 10 GbE) | `192.0.2.12` | 0,71 ms | 0,06 ms | 5 ms |
| LAN via Wi-Fi | `192.0.2.216` | **38,3 ms** | **67,9 ms** | 15–41 ms |

**Conclusion:** For an HTTP proxy, Thunderbolt 5 is **not required** —
the wired LAN path at 0,71 ms is practically as fast as TB5 at
0,60 ms, and both lie in the noise against a prefill time of 335 s.
The Wi-Fi path, by contrast, is unusable for a compute node: 38 ms
average with peaks above 170 ms means streams that break off.

TB5 keeps two real jobs: **model copies** (402 GB in minutes instead of
~21 min over 2,5 GbE) and the **oMLX cluster mode** (JACCL), which does not
work at all without TB.

## 3. Goals

- **Z-1** Both machines are utilised in normal operation without any
  client having to know about two endpoints.
- **Z-2** The distribution follows the ratio 3 : 2 (M5 : M3) across the
  shared models.
- **Z-3** Long conversations do not lose their prefix cache because of the
  distribution.
- **Z-4** If the M3 fails, everything continues on the M5 without intervention.
- **Z-5** The balancer is realisable without intervening in oMLX (pure proxy).

## 4. Non-goals

- **NZ-1** No model splitting across machines (that remains reserved for the
  oMLX cluster).
- **NZ-2** No models outside the shared two (DeepSeek-V4.1,
  Qwen3.5-9B). Other models are passed through, but not distributed.
- **NZ-3** No copying/distributing of model files, no model management **as part of
  routing or on RIPPLE's own initiative**. The one exception is the
  operator-initiated replication job of the FA-22 third amendment: one model, one
  target, behind a confirmation dialog (`docs/replication.md`). Nothing is copied
  because a model is missing somewhere — only because an operator asked for it.
- **NZ-4** No change to oMLX itself, no patches in the app bundle. A calibration
  load is an API call to oMLX (`POST /v1/models/{id}/load`), not a change to it:
  operator-initiated, admin-key protected, and the only write the balancer ever
  makes (see the FA-22 amendment and `docs/loading-state.md` §6.1).
  **RIPPLE does not change oMLX, but it may place model files for the operator —
  never on its own, never as part of routing.** That is the replication job
  (FA-22 third amendment, `docs/replication.md`): it writes a model directory onto
  another machine and then calls `POST /admin/api/reload` there, both on the
  operator's explicit instruction and both inside a maintenance window it opened
  itself.
- **NZ-5** No request rewriting (prompt manipulation, rephrasing).
- **NZ-6** No replacement for authentication: the balancer is not an
  auth gateway, it passes keys through.
- **NZ-7** **No WAN/cross-site operation in version 1.** The
  balancer assumes a LAN with single-digit millisecond RTT. Globally
  distributed nodes are technically possible (it is HTTP), but they need
  things that are deliberately not built here: an encrypted tunnel
  (oMLX itself offers no TLS), a placement weighted by RTT and site,
  a site failure concept and one model copy per site.
  That is expansion stage M5, not part of acceptance.
- **NZ-8** **Remote providers are expansion stage M6.** OpenRouter and comparable
  services are not a load-balancing target (§6.2, FA-23): no fixed capacity, no
  prefix cache, no memory to protect. They come into play exclusively via
  explicit policy (allow-list, overflow threshold, cost budget),
  **off** by default, and never as a silent fallback.

## 5. Actors and interfaces

| Actor | Interface | Note |
|---|---|---|
| Hermes (main session) | `POST /v1/chat/completions`, streaming | long-lived, 100 k+ context |
| Hermes (subagents) | ditto | short-lived, new conversation |
| opencode | ditto | streaming, tools |
| Scripts / benchmarks | ditto | often parallel, identical prompts |
| oMLX M5 / M3 | `GET /v1/models`, `GET /health`, `GET /admin/api/models` | discovery |

To the outside the balancer offers **exactly** the oMLX surface:
`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`.
In addition two endpoints of its own: `/healthz`, `/stats`.

## 5.1 Data model: what a host stores

A host record has three layers with different lifetimes. The
separation matters: **static configuration is maintained by humans,
live state is read from the host, measurements are produced by the balancer.**
None of it is mixed, otherwise an update overwrites the
configuration.

### Layer A — Configuration (persisted, hand-maintained)

| Field | Type | Purpose | Why not omittable |
|---|---|---|---|
| `name` | string | stable identity | key for thread slots, affinity and logs |
| `node_id` | uuid | identity check | prevents routing to the wrong machine (RB-9) |
| `addresses[]` | list | probe order | site change without config change (FA-21) |
| `weight` | float | target share | 3 : 2 |
| `enabled` | bool | drain switch | take the host out of rotation without changing addresses (maintenance) |
| `role` | enum | `entry` / `worker` / `fallback` | makes the special status of the M5 explicit instead of implicit |
| `site` | string | site id from the top-level `sites:` block (issue #67) | says where the machine stands; the claim is checked against the address RIPPLE really uses, so a wrong label is a `doctor` warning instead of a footnote |
| `api_key` | string | upstream auth | both nodes accept the same key (measured) |
| `max_inflight` | int | hard upper bound | prevents overload (FA-5) |
| `spill_threshold` | int | from when full | spillover decision (FA-5) |
| `models[]` | list | **allow-list** | this host may only serve these models |
| `warmup[]` | list | **accepted, inert** — FA-16 is not implemented; a non-empty list warns at startup | none; the key exists so an old config still parses |

The allow-list is the most important item in this layer: without it a
request for a 400 GB model can land on a host that is not supposed to hold
it at all, and the eviction forecast from FA-18 becomes waste paper.

`site` names a block, it does not carry a label: the top level of the
configuration holds `sites:` — one entry per named location with `id`, `name`
and `networks[]` (CIDR, checked by `ipaddress`). The networks are what makes the
declaration checkable: the address a node is actually reached through is matched
against them, and a contradiction is one `ripple doctor` line naming both
locations. A site is a location and never a link: which address is used follows
from reachability and the configured order, not from where the machine stands.
See `docs/sites.md`.

### Layer B — Live state (read from the host, never persisted)

| Field | Source | Purpose |
|---|---|---|
| `resolved_address` | resolution | active address (FA-21) |
| `healthy` | health check | candidate filter |
| `omlx_version` | `/api/cluster/node_id` | **drift detection** between the nodes |
| `guard_enabled` | `/health` → `final_ceiling > 0` | arms or disarms the eviction forecast (FA-18) |
| `hard_threshold` | host settings | admission target for FA-18 |
| `ceiling_bytes` | `/health` | headroom calculation |
| `used_bytes` | `/health` | current utilisation |
| `models[id].estimated_size` | `/admin/api/models` | **the** number for the eviction forecast |
| `models[id].loaded` / `pinned` | `/admin/api/models` | residency, eviction protection |
| `models[id].context_len` | `/v1/models` | context window per host |
| `models[id].vision` | `/admin/api/models` (`engine_type: vlm`) | image requests only to capable hosts |
| `consecutive_failures` | counter | triggers re-resolution (FA-21) |
| `last_seen` / `last_error` | counter | diagnostics |

`guard_enabled` is not decoration: if the memory guard is off (`final_ceiling: 0`),
**no** admission and no eviction check takes place — then the
forecast from FA-18 is meaningless and the real risk is called Jetsam kill instead of
eviction. The balancer must know this state and report it in the log.

### Layer C — Measurements (produced by the balancer, per host **and model**)

| Field | Purpose |
|---|---|
| `models[id].tg_tps` | decode speed → justifies the weights (AS-6) |
| `models[id].pp_tps` | prefill speed → latency of long-context threads |
| `models[id].load_seconds` | cold load time → is a spill worth it at all? |
| `models[id].measured_at` | age of the measurement |
| `rtt_ms` | transport latency of the active address |
| `errors_total`, `http_507_total` | error profile per host |

**The core point:** `tg_tps` is **not** a host property. A host is not
"fast", it is fast *for a particular model at a particular context
length*. The measured values diverge: Qwen3.6-35B ran on the same
machine at 172 tok/s with 80 tokens of context and 128 tok/s at 10 k context
(measurement 15.10.2026). A single host value would flatten both and set
the weights wrongly. Therefore: measurements always on the pair (host, model), with
context length and timestamp.

### What a host does **not** store

- **`inflight` and occupied thread slots** — purely volatile, in memory; a
  persisted number would be a lie after a restart.
- **Derived metrics** such as "utilisation in percent" — it follows from
  `inflight`, `max_inflight` and `used_bytes`; stored separately it drifts
  apart.
- **Model lists as truth** — the model list is read, not
  maintained; only the allow-list is configuration.

## 6. Functional requirements

Priority: **M** = Must, **S** = Should, **K** = Can.

### 6.0 Placement model (basis of all requirements)

**The placement unit is the thread, not the request.** A thread is a
conversation: on its first request it is assigned to exactly one node and
stays there for its entire lifetime — all follow-up turns, all tool rounds.
Within a thread the balancer makes **no** routing decision any more.

The 3 : 2 distribution arises from placing **new threads** on the node that is
furthest below its target share relative to that share
(deficit rule, FA-2). This yields exactly 3 : 2 at every multiple of 5 active
threads:

| active threads | M5 | M3 |
|---|---|---|
| 1 | 1 | 0 |
| 2 | 1 | 1 |
| 3 | 2 | 1 |
| 4 | 2 | 2 |
| 5 | **3** | **2** |
| 10 | **6** | **4** |

A thread releases its slot when it has not made a request for
`thread_ttl_seconds` (default 1800). Short-lived single requests (vision,
classification) occupy a slot only for the duration of the response and are
released immediately afterwards — for them the deficit rule therefore acts like
a pure 3:2 distribution per request.

### 6.1 Two separate decisions: binding and admission

The design in §6.0 mixes two questions that must be kept apart. The model is
the `Program` layer of `vllm-router`, which makes exactly this
separation explicit (`binding_strategy` on the one hand, `RequestPool` admission
on the other):

| | **Binding** — *where to* | **Admission** — *whether at all* |
|---|---|---|
| Question | Which node gets the new thread? | May this node take it now? |
| Basis | deficit, consistent hash, thread count | slots, capacity, residency, health |
| Result | desired node | yes / no / fall back to the next candidate |
| When | once per thread | before every dispatch |
| Changeable | no (affinity) | yes, continuously |

**Binding strategies** (`binding_strategy`, configurable):

- `deficit` (default) — the rule from FA-2. Yields exactly 3:2.
- `consistent_hash` — thread identifier onto a ring with 160 virtual nodes
  per node. Stateless and survives a restart of the balancer, but yields
  no quota.
- `least_threads` — simplest candidate, for comparison in tests.

**Admission gates**, in this order, each with veto power:

1. **Model present** (FA-4) — otherwise the node is not a candidate.
2. **Health** (FA-11) — otherwise not a candidate.
3. **Slots** (FA-19) — `threads_on_node < max_threads`.
4. **Residency and eviction protection** (FA-18) — otherwise HTTP 503 with conflict details.

A veto does **not** abort but moves to the next candidate; only when
all candidates have a veto is the request rejected. The binding is
unaffected: a thread whose node later gets a veto is not
rebound but gets 503 — moving would cost the prefix cache and
is exactly what this architecture is meant to prevent.

**Why the separation is worth money:** in the mixed version the
deficit rule would also have decided admission — a node with a free slot but
without a resident model would have received a thread and run it into an eviction
on the first request. The separation turns "where to" into a pure
distribution question and "whether" into a pure safety question.

### 6.2 Backend adapter contract

The balancer does not talk to oMLX directly but to an **adapter per
backend type**. Reason: the core mechanics (thread placement, 3:2, hysteresis,
binding/admission) are backend-agnostic — the signals for residency and
capacity are not.

**Capability flags.** Every adapter declares what it can really do. The check
happens at start against the running backend, not against the configuration:

| Flag | Meaning | `omlx` | `openai_compatible` |
|---|---|---|---|
| `availability` | model list via `/v1/models` | ✓ | ✓ |
| `residency` | loaded models including size | ✓ | – |
| `capacity` | `final_ceiling`, used, `hard_threshold` | ✓ | – |
| `lifecycle` | load and unload models | ✓ | – |
| `identity` | stable `node_id` independent of the address | ✓ | – |
| `prefix_cache` | affinity pays off measurably | ✓ | – |

**No silent degradation.** If a capability is missing, that must be visible —
otherwise the operator believes in a protection that does not exist:

| missing | consequence | visibility |
|---|---|---|
| `residency` | FA-18 inactive, availability routing only | `WARN` at start, `eviction_guard: false` in `/healthz`, banner in the dashboard |
| `capacity` | no eviction forecast, slots only | `capacity: false` in `/healthz` |
| `identity` | URL counts as identity — RB-9 violated | `WARN` at start, note in the dashboard |
| `lifecycle` | dashboard read-only | no warning needed |

**Interface** (Python, `async`, all methods may return `None`):

```python
class Backend:
    name: str
    capabilities: frozenset[str]

    async def health()   -> Health                 # always
    async def models()   -> list[ModelInfo]        # always: id, availability
    async def residency() -> dict[str, Residency] | None
    async def capacity()  -> Capacity | None
    async def identity()  -> str | None
    async def load(model_id: str)   -> bool        # only with lifecycle
    async def unload(model_id: str) -> bool        # only with lifecycle
```

Adapters in version 1: `OmlxBackend` (all six capabilities) and
`OpenAICompatibleBackend` (only `availability`). The second does not exist in order
to be used, but to test the boundary: it proves that the balancer
runs without oMLX signals too — and that it then says so.

**Remote providers (expansion stage M6).** A backend of class `remote`
(OpenRouter, Anthropic, others) is **not a load-balancing target**. There is
no fixed capacity to distribute, no prefix cache to hit and no
memory to protect. Therefore it:

- **never** takes part in the deficit rule
- gets **no** thread affinity across the class boundary
- is reachable only via explicit policy: `model_allowlist`,
  `overflow_after_seconds`, `cost_budget_per_day`; default **off**
- privacy: `remote_allowed` is set **per model**, never as a
  convenience fallback

That is policy routing, not balancing — and therefore a milestone of its own,
not a configuration switch.

### 6.3 The unit of placement for parallel work (#92)

**The problem.** The affinity key is the *session*. Parallel subagents are
started inside one session and inherit its key, so four subagents are four
concurrent streams of one thread — and a thread lives on one node. Measured in
the request log: `sid:local-cd86633198a05e` with 132 requests on alpha and 87
on beta, a second thread with 81. The 87 on M3 are a fallback episode, not a
balance. RIPPLE can therefore only balance **between** sessions; within a
session parallelism is invisible to the placement, and with one operator and one
agent session the fleet delivers the throughput of a single machine while the
other idles.

**What a finer key would cost (measured, 18.11.2026).** The only thing a spawned
request can lose when it moves is the prefix it shares with the main turn.
oMLX's scheduler log (`~/.omlx/logs/server.log`) states that per request:

| Quantity | Value | Source |
|---|---|---|
| cache granularity | `block_size=8192` — reuse is counted in 8192-token blocks, a shorter shared prefix yields **nothing** | line 185933 |
| shared prefix, same conversation | `common_prefix` = the whole previous prompt, cp/prompt median **0.990** | 2 758 probes, 07:52–19:43 |
| shared prefix, **different context on the same host** | `common_prefix=8/90128 tokens (~0 blocks of 8192)` — a 90 128-token request against the 253 998-token main turn matches **8 tokens** | line 19873 |
| the same subset, all probes | 307 of 2 758 (11,1 %) have `common_prefix < 8192`; median **8 tokens**, i.e. **match_ratio ≈ 0.0001** | derived from the above |
| what such a request does reuse | its **own** earlier turn: `served/prompt` median **0.946**, for prompts ≥ 20 k median **0.997** | same log |

So the shared prefix between a spawned request and the main turn is **0 blocks of
8192 — 8 tokens**. The 99 % cache hits everywhere are *self*-reuse, not shared
reuse: each context keeps its own warm chain on the host it lives on, and
contexts on one host share nothing that a block could carry. A cold first turn is
therefore the normal case and not a regression — 75 requests of the day were
served with `cached_tokens=0`, 13 of them with a prompt ≥ 20 k.

**Decision: option 1 first — a spawned unit gets its own thread id.** The cost of
the finer key is exactly the prefix it destroys, and that is measured at zero
blocks. What the subagent does pay is one cold prefill of *its own* context on
whichever node it lands on — and it pays that today as well, on the parent's
node, because its context shares nothing with the parent. At the measured cold
prefill rates (FA-2: M5 3 680, M3 970 tok/s) a 40 k context costs 11 s on the M5
and 41 s on the M3, once, and is cached from the second turn on. Against that
stands a second machine's worth of throughput. With a single-node fleet the
decision costs nothing at all: the key changes, the candidate set does not.

**Option 2 (splitting one thread's streams) is not built as a mechanism.** It
needs per-stream pinning, so that a response returns to the right caller, plus a
price for the split (`prompt_tokens / pp_tps` on the receiving node) — that is a
cost model, and it belongs in the optimizer's calculus (#72), which already
decides whether a move pays (FA-2, `rebind`). Once option 1 exists, a subagent's
streams are placed independently anyway; option 2 would only help the *one*
remaining stream, the main turn's own overflow. It stays open as an optimizer
question, not as a placement mechanism.

**What follows for the code** (not in this change):

- `ripple/registry.py` — the key granularity. Today the thread key is the session
  id. It must accept a **work-unit id** as a second, finer level, with the
  session id kept as the parent. Precedence: explicit work-unit id, then session
  id, then fingerprint (FA-6, unchanged). A client that sends nothing keeps
  today's behaviour exactly.
- `ripple/proxy.py` — the source of the key. The work-unit id comes from a
  request header set by the agent runtime, which is the only party that knows
  where one spawned unit ends and the next begins. It is read before the
  fingerprint, passed through untouched, and logged next to the session
  (`thread_source` names which level decided), so the dashboard still shows one
  session and the log still shows which unit a request belonged to.

**The honest limit.** This decision does not balance *within* a stream: the main
turn of a session still lives on one node, and if that single stream is the
bottleneck the other node stays idle. It moves nothing that is already running.
**#80** (moving idle threads between sessions) is necessary but not sufficient —
it shifts whole threads over time, it does not split one thread's concurrent
streams. And it depends on the agent runtime actually sending the id: if it does
not, nothing changes, and the log says so. The measurement above is M5-only —
the M3's `server.log` was not readable from the measuring host (SSH timeout), so
the sample is one node; block size and cache mechanics are per-host properties
and should carry, but they were not double-checked on the M3.

**Acceptance.** A session that spawns four subagents puts those four units on the
node the deficit rule picks for each of them, not on the parent's node; the
request log names the unit and the session separately; a client that sends no
work-unit id reproduces today's placement byte for byte; and no request loses a
prefix block it would have had — the main turn's chain stays intact on its node.

### 6.4 The optimizer prices the arriving turn (#95)

**The problem.** The calculus is well gated. `plan_rebind`
(`ripple/placement.py:610`) prices a move as
`cost = tokens / pp_tps` (`:742`) against `gain = assumed_completion_tokens × Δ`
(`:763`), with `_delta_total` (`:579`) the fleet verdict in tok/s, refusing on
`safety_factor` 1.5, `min_gain_seconds` 2.0 and `cost_exceeds_ttl`; it runs every
30 s from one call site (`ripple/proxy.py:2056`), on idle threads only
(`placement.py:660`). A bound thread is never priced: dispatch reads
`thread.host`, admits or refuses, and follows it with `reason=thread`
(`proxy.py:2292`), and `choose()` (`placement.py:337`) has no migration term. The
one moment at which a turn's cost could be weighed against its benefit is
therefore the one the optimizer never asks about: the arrival of a prompt that
will be prefilled in full wherever it lands.

**The measured consequence (19.11.2026, one session — one observation, not a
benchmark).** Three parallel tasks of one session ran as one thread
(`turns=100`, `active=3`) on beta while alpha — weight 3, role entry, same
model resident — sat at `inflight 0`. The fleet delivered one machine's throughput
and no gate fired: from the sweep's side nothing was idle.

**Decision: the same calculus in the binding path, at the turn's arrival.**
Before a bound thread's turn is dispatched to `thread.host`, price **stay**
against **move** with the same two numbers the sweep uses — `_delta_total` for the
fleet verdict, `prompt_tokens / pp_tps` for the prefill — and move only if the
priced benefit beats the priced cost plus a hysteresis band. Nothing new needs
measuring: `prompt_tokens` is in hand at arrival, the curves are already sampled,
`pp_tps` is configured per host. The sweep asks whether any thread is cheap to
move; the binding path asks whether *this* turn is cheaper elsewhere — while the
answer can still be acted on.

**The amortisation rule: over the expected remaining turns.** A move pays the
whole prompt once and earns the decode delta on every turn after it, so it is
compared against the turns that will follow: `turns_left × gain_per_turn >
cost × safety_factor + band`, with `cost = prompt_tokens / pp_tps(dst)` in
seconds, `gain_per_turn = 1/rate_src(n) − 1/rate_dst(m+1)` seconds per answer at
`assumed_completion_tokens` (400 by default), and `turns_left` the expected
remaining turns. Without the multiplication the feature is a **prefill storm**:
every short turn looks like a win, and the fleet re-prefills 40 k-token contexts
for one answer each. With it the shape of the conversation decides — a single
short turn almost never repays a move, a long one often does. `turns_left` is an
estimate, bounded by the thread's history (`thread.turns`, its interval between
turns) and the TTL remainder; it is the weakest number in the decision. Each
decision carries one reason in the log line the sweep already writes: `moved`,
`no_repayment` (benefit below cost), `cost_exceeds_ttl` (unrepayable before the
slot expires), `gain_below_band` (clears cost, not the noise), plus the existing
`no_curve`, `delta_non_positive`, `cooldown`, `budget`, `pinned`, `disabled`.

**The hysteresis: three mechanisms, each blind where the others see.**
*(a) A deadband tied to the observed spread.* `delta_total` is a difference of two
fitted curves at neighbouring levels — measured, not exact — and no constant is
right for every model pair. So the band is the **observed** spread of
`delta_total` over the last `band_samples` scans (default 5) — interquartile range
or 2σ — and a move must beat `cost × safety_factor` by more than that spread. It
cannot speak early: before enough scans exist the decision falls back to
`min_gain_seconds` (`band=unavailable`). The return factor cannot replace it
(blind to magnitude), nor the pin (it counts moves, not noise).

*(b) Directional memory: a return factor.* A thread that moved A→B within
`per_thread_cooldown_seconds` (600 s) needs a strictly higher bar to move back —
the required gain is multiplied by `return_factor` (default 2.0) — and a reverse
move resets the cooldown, so A→B→A→B cannot happen at the ordinary interval. The
worst oscillation undoes a move: two prefills, ending where it started. The band
cannot replace it, being symmetric: a large genuine swing the other way is what a
symmetric band lets through, and one reversal never trips a move count.

*(c) A per-thread move budget: pinning.* The fleet-wide `max_per_hour` (4) bounds
the fleet, not a conversation; one long conversation can spend the whole budget. A
thread that moved `max_moves_per_thread` times (default 2) inside `window_seconds`
(default 3600) is **pinned** for the rest of the window: `pinned_until` on the
thread, visible in `/healthz → threads.registry[]` beside `turns` and
`idle_seconds`, with `reason=pinned` in the log. Pinning admits the calculus was
unsure; the thread keeps its cache, which is what this architecture exists to
protect. Only the pin counts decisions already taken.

**Config sketch** — a proposal, names may change.

```yaml
placement:
  migrate:            # the arriving turn (#95); placement.rebind stays the sweep
    enabled: true
    band_samples: 5
    return_factor: 2.0
    max_moves_per_thread: 2
    window_seconds: 3600
```

**Acceptance.** Three traces; the third makes the first two mean something.

| Trace | Expected |
|---|---|
| two nodes, equal curves, one thread, 20 turns | 0 moves — `delta_total` is zero, the band is never beaten |
| one node 30 % slower, one long conversation, migration enabled | at most one move per cooldown window, and a **lower** total wall-clock than the same trace with `placement.migrate.enabled: false` |
| the same trace, band 0 and `return_factor` 1.0 | **more** moves than trace 2 — if not, the trace was too quiet to prove anything |

Row 2 is the only row that measures the feature: a move that happened is not a
win, a lower total time is. Row 3 shows the hysteresis suppressed the moves, not
that there was no opportunity.

**What is measured.** The trace exists since 19.11.: `tools/trace_migrate.py` —
20 turns, two nodes, one 30 % slower in decode, a deterministic model that reads
no clock and never sleeps — and `tests/test_migrate_acceptance.py`, which plugs
the shipped `placement.decide_migration` into it. Measured with
`pytest -q tests/test_migrate_acceptance.py`:

| Row | Trace | Moves | Wall clock |
|---|---|---|---|
| 1 | `equal_curves` | **0** | **202.500 s**, identical to the `stay` baseline |
| 2 | `one_slow_node` | **1** | **209.786 s** against **288.214 s** with `enabled: false` (78.43 s saved) |
| 3a | `one_slow_node`, band 0 / `return_factor` 1.0 | **1** | 209.786 s — the same single move |
| 3b | `flapping_fleet`, band 0 / `return_factor` 1.0 | **10** | 199.2 s against **4** moves / 196.0 s with the measured band |

Row 3a is the honest negative the table asked for: after the one move on
`one_slow_node` the slow node is the only alternative left, `delta_total` stays
≤ 0, and no decider — however neutral its hysteresis — finds a second move. Row
3b supplies what row 3 needs: two equally fast nodes whose per-turn rates cross
(±25 %), with short prompts (400 tokens, +50 a turn) so the price of a move stays
low enough for the unguarded calculus to say yes. It says yes ten times — the
prefill storm this section warns about — and the band plus the return factor cut
that to four. `flapping_fleet` is a fixture in the test module, not a trace of
the CLI.

The table was an intention from #95 until 19.11.; it is asserted now. What the
numbers are not: a production measurement. The curves are round numbers (800
`pp_tps`, 40 and 28 `tg_tps`), chosen so the difference can be checked on paper,
not a claim about any node in the fleet.

**What is not measured.** The shape of `delta_total`'s noise for a real model
pair (`band_samples: 5` is plausible, not measured), the accuracy of
`turns_left`, and the price of a move on a fleet whose `pp_tps` was never
calibrated — the trace prices prefill from a configured rate, so it shows what
the calculus does with a number, not whether the number is right.

**Dependency and relation.** This depends on §6.3 / #92: until a spawned unit is
its own thread the calculus has nothing to choose between — the arriving turn
belongs to the parent's thread, and moving it moves the conversation, not the
parallel work. **#80** (idle threads) is necessary and not sufficient: it supplies
the curves, `_delta_total` and the price model reused here, but acts only where
nobody waits. **#72** (a cross-site pool) makes this the central question: across
sites a move is also a network hop, so the price stops being only prefill — but
the shape of the decision does not change.

*What this means for a user — when a move pays, and why one conversation never
uses a second machine — is §9 of `docs/manual-en.md`; the trace numbers in the
acceptance table are asserted in `tests/test_migrate_acceptance.py`.*

### FA-1 Entry point (M)
The balancer is the only externally reachable endpoint
(`0.0.0.0:8010`). It accepts `POST /v1/chat/completions` with and without
`stream: true` and answers protocol-compliant.
**Acceptance:** An unmodified Hermes/opencode client works against
`:8010` without configuration changes other than the `base_url`.

### FA-2 Thread placement by deficit (M)
Every **new** thread is assigned to the node that has the largest deficit
relative to its target share:

```
deficit(node) = target_share(node) × (active_threads + 1) − threads_on_node
target_share  = weight(node) / sum(weights)
```

On a tie the higher weight decides, then the node with fewer
running requests. The rule applies only among candidates that
offer the model, are healthy and have capacity (FA-5).

**Idle threads fade out (amended 14.11.2026, measured).** The slot count that
enters the deficit is weighted by idleness, not raw:

```
w(thread) = 1.0                                  while a request is in flight
          = (1 − idle / thread_decay_seconds)²    otherwise, clamped at 0
```

`thread_decay_seconds` defaults to 300. Reason: the entry node permanently
carries the operator's own agent sessions. Counted at full weight for the whole
`ttl_seconds` (1800) they kept the entry node looking loaded while nothing was
running anywhere, and every new conversation went to the weaker node
(`M5 −0.2` against `M3 +1.2`). Quadratic, not linear: the first minutes after an
answer keep most of their pull (10 % of the window still weighs 0.81, half of it
0.25), so a short pause does not rebalance, while the silent tail does not count.
A thread with a request in flight always weighs 1.0 — real work is never
invisible. The binding itself is untouched: the thread stays assigned and counts
fully again on its next request. The hard slot limit (`max_threads`) stays raw.
`0` restores the old behaviour (a thread counts fully until the TTL).

**Moving a bound thread (rebind, amended 14.11.2026).** A thread stays where it
was born — unless the picture has changed enough that one prefill is cheaper than
staying. The load picture moves: a long conversation ends on the strong node
while a colleague's thread keeps the weak one busy; a context grows on the weak
node while the strong one idles. So every request of a bound thread is judged
again before dispatch, in seconds:

```
gain = completion_tokens × (1/tg_own − 1/tg_target)
cost = prompt_tokens / pp_target
move ⟺ gain ≥ min_gain ∧ gain ≥ cost × safety_factor
```

`tg` is the host's **measured idle decode rate** (`hosts[].tg_tps`, AS-6: M5 60.2,
M3 43.5 tok/s) — the capability, not the moment: a live rate follows the current
load, and the entry host looks slow exactly while it carries the interactive
session (measured: a contended M5 reported 8.7 tok/s and a thread migrated away
for it). `pp` is the measured cold prefill rate (`hosts[].pp_tps`, 14.11.2026:
M5 3.680, M3 970 tok/s). Busyness is judged by `inflight` and the slot count, not
by a rate. Vetoes, all
of them hard: no request of the thread in flight; the destination must already
hold the model (a cold load on top of the prefill never pays), be below its
`spill_threshold` and have a free slot; per-thread cooldown (600 s) and an hourly
budget (`max_per_hour: 4`) against flapping. A move is logged with reason
`rebind`, counted as `threads_rebound` and shown in the dashboard — never silent.
The old host keeps its KV cache until oMLX evicts it, so a move is a copy, not a
transfer.

**Atomic reservation (mandatory).** The deficit is computed under a lock
and the slot is reserved **before** the dispatch. Two threads arriving
almost simultaneously must not both choose the same node and
overshoot it to 4/1. Model: `vllm-router`/`sgl-model-gateway` perform the
placement explicitly "within the pool's admission critical section".

**Hysteresis.** The deficit rule is not applied for every trifle,
but only when the imbalance exceeds two thresholds **simultaneously**
(AND, not OR — otherwise one of the two never takes effect):

```
rebalance    ⟺  (max_deficit − min_deficit) > balance_abs_threshold
             ∧   max_deficit > min_deficit × balance_rel_threshold
```

Default `balance_abs_threshold: 1`, `balance_rel_threshold: 1.5`. The 1.5
corresponds to the target ratio 3:2 — the rule therefore takes effect exactly when
the distribution drifts further apart than 3:2.

**Careful with the calibration:** `vllm-router` uses `balance_abs_threshold: 32`
and `balance_rel_threshold: 1.1`. Those numbers are right for machine parks with
hundreds of workers; for two Macs with five to ten threads, 32 would
**never** be reached — the hysteresis would never trigger and the distribution would stay
at its initial state. With us the absolute value must be small (1).

**Acceptance:** Five threads opened one after another (one request each, then idle)
yield 3 threads on the M5 and 2 on the M3. With ten threads: 6 / 4.
After all threads are released (TTL) the distribution starts again at 1 / 0.
In addition: four threads opened simultaneously yield 3/1 or 2/2 — never 4/0.

### FA-3 Configurability of the weights (M)
Weights, `max_inflight`, `spill_threshold` and `thread_ttl_seconds` live in
a YAML file and are adjustable without a code change. A restart suffices.
**Acceptance:** Changing `weight: 3 → 1` shifts the thread distribution
measurably (with 5 threads then 3 / 2 in favour of the M3).

### FA-4 Model availability map (M)
The balancer periodically reads `GET /v1/models` of both nodes and routes only to
nodes that offer the requested model. A model that only the M5 has
goes 100 % to the M5 (no error, no redirect).
**Acceptance:** A request for an M5-exclusive model lands 10/10 on the M5;
a request for an unknown model returns HTTP 404 with a meaningful message.

### FA-5 Capacity and spillover (M)
Every node has `max_inflight` (hard upper bound of concurrent requests) and
`spill_threshold` (from how many concurrent requests it counts as
full for new threads). A new thread is never placed on a node whose
`inflight >= spill_threshold` while another candidate has capacity.
If **no** candidate can take it any more, the request is not rejected: the thread
goes to the node with the largest relative headroom `(inflight+1)/weight`
and waits there.
**Acceptance:** With `spill_threshold: 1` and one running slow request on
the M5, the next new thread goes to the M3 although the deficit would
favour the M5.

### FA-6 Thread detection (M)
The balancer must recognise whether a request belongs to a known thread
or opens a new one. Since the OpenAI API is stateless, this happens in
this order:

1. **Explicit header** `X-Session-Id` (name configurable). Exact and
   preferred; Hermes and opencode can set it.
2. **Fingerprint** from `model` + first user message of the conversation.
   The system prompt does **not** enter the hash — it can contain timestamps,
   tool lists and dynamic hints and would change per turn.
3. **First-turn detection** as an additional criterion: exactly one user message and
   no assistant message before it = new thread.

**Prefix similarity instead of a fixed hash.** A hash over the first N bytes is
strict: a single change in the prefix yields a new thread and the
affinity tears. That hits us for real, because Hermes compresses the conversation
and rewrites the text in the process. Therefore, in addition to points 1 and 2, a
**similarity measure** is kept:

```
match_ratio(thread) = common_chars(prefix(thread), request)
                      / chars(request)
```

The balancer stores the first `affinity.prefix_chars` characters per thread
(default 4096). If the best `match_ratio` is above
`affinity.match_threshold` (default 0.5), the request counts as belonging to this thread.
That tolerates divergence: after a compression the beginning still
matches, even if the rest has been rephrased.

**Characters, not tokens.** The comparison runs on characters. Tokens would be more exact,
but require tokenisation per request in the router and depend on the
chat template — the effort is out of all proportion. `vllm-router` and
`sgl-model-gateway` make the same decision for the same reason
("we directly insert characters instead of token IDs, so we can save on
expensive tokenization time").

A hit on a known thread fixes the assignment; the node is
no longer changed (exception: FA-11 fallback when the node has failed).
**Acceptance:** Five turns of the same conversation land on the same node, even
if the other node is empty. Two different conversations are distributed per
FA-2. And: if the history is compressed between two turns, the
assignment is preserved.

### FA-7 Short-lived single requests (S)
Requests with exactly one user message that is shorter than
`affinity.min_prompt_tokens` (default 8192) do not occupy a permanent
thread slot: they are placed by deficit and release the slot immediately after
the response. That way vision and classification calls distribute
cleanly 3:2 without blocking thread slots.
**Acceptance:** 20 vision requests with different prompts yield 12 / 8,
no node holds an occupied slot afterwards.

### FA-8 Streaming without buffering (M)
SSE responses are passed through chunk by chunk. The balancer must not
collect the response before delivering it; the first byte must reach
the client as soon as it arrives from the upstream.
**Acceptance:** Time to first byte through the balancer deviates by < 50 ms from the
direct connection; a 10-min stream runs through completely.

### FA-9 No own request timeout (M)
For running generations **no** read timeout applies (contexts up to 524 k
tokens, runtimes up to 30 min). Only connection setup (`connect: 10 s`) and
writing the request are bounded.
**Acceptance:** A 15-min request is not aborted.

### FA-10 Retry only before the first byte (M)
If the upstream fails with a connection error or 5xx **before** the first
byte was sent, the next candidate is tried (max. one attempt per
node). After the first byte there is no retry.
**Acceptance:** A dead upstream leads to a request on the other node,
not to double token consumption.

### FA-11 Fallback M5 (M)
If the M3 is unreachable or unhealthy, all requests go to the M5.
The M5 is therefore functionally the fallback (weight 3 is independent of that).
**Acceptance:** M3 stopped → 10/10 requests successful on the M5; `/healthz`
reports M3 as `down`.

### FA-12 Residency preference (S)
The balancer reads `GET /admin/api/models` and prefers nodes where the
model is already loaded. If it is loaded nowhere, the node with the most
headroom wins. This prevents a request from accidentally paying 70 s of
load time.
**Acceptance:** With a cold M3 and a warm M5, all DeepSeek requests go to the
M5; only when the M5 is saturated does the M3 load.

### FA-13 Health and status endpoint (M)
`GET /healthz` returns per upstream: `healthy`, offered models, loaded
models, `inflight`, weight, **occupied thread slots** and last error.
In addition the global view: active threads total, target share and actual share
per node. `GET /stats` returns counters: requests, errors, thread reassignments,
spillovers, fallbacks, last measured tok/s.
**Acceptance:** After 5 opened threads `/healthz` shows 3 slots on the M5 and
2 on the M3; the counters match reality.

### FA-14 Request log (M)
Exactly one log line per request with: time, model, chosen node,
decision reason (`new_thread` / `thread` / `rebind` / `spillover` / `fallback` /
`model_pinned`), prompt size, duration, status. No prompt contents, no keys.
**Acceptance:** From the log it is reconstructable why a request landed where it did.

### FA-15 Key pass-through (M)
The balancer uses the configured key per upstream (`${VAR}` is
expanded) and otherwise passes the client key through. Keys never appear in the log
and never in `/stats`.
**Acceptance:** `grep` for the key in logs and endpoints: no hits.

### FA-16 Warmup (K)
`warmup_models` preloads configured models on all nodes at start
(`POST /v1/models/{id}/load`), so that the first spill costs no load time.

### FA-18 Eviction protection: never evict a running thread (M)
The balancer may only choose a node for a model whose loading there would
**evict** a model if the model to be evicted is not occupied by any
active thread and is not pinned.

**Calculation the balancer must reproduce for this** (oMLX's own
admission logic, not the actual utilisation):

```
target   = hard_threshold × ceiling          # 0.95 × 482,6 GB = 458,5 GB
forecast = sum(estimated_size of loaded) + estimated_size(new)
evicts   = forecast > target                 # oMLX then unloads the LRU model
```

The decisive figure is `estimated_size` from `/admin/api/models` — **not** the
measured consumption. oMLX itself computes with the estimate, and for
DeepSeek it is 422,5 GB against 402,6 GB actual.

**Measured example (14.11.2026, M5):** DeepSeek (422,5) + 9B (6,1) = 428,6 GB
used, target 458,5 GB, so 29,9 GB of air. A thread wants
`Qwen3.8-Flash-Next-oQ4e-mtp` (103,9 GB) → 532,5 GB > 458,5 GB. The M5 would
evict DeepSeek and thereby kill the running main session. **The M5 is locked
for this model, the thread goes to the M3.** On no node
do DeepSeek (422,5) and Qwen3.8-oQ4e (103,9) coexist: 526,4 GB > 486,7 GB
ceiling.

**Candidate hierarchy** (the first matching stage wins):

| Stage | Condition | Log reason |
|---|---|---|
| 1 | Model is **loaded** on the node | `resident` |
| 2 | No forecast possible (no `residency`, guard off, `size_unknown`, no ceiling) — load allowed **unchecked** | `cold_load`, note `no_eviction_forecast` / `size_unknown` / `no_ceiling` |
| 3 | Forecast ≤ target — loading evicts nothing | `cold_load` |
| 4 | Forecast over target, and the models that would go are idle, unpinned and unheld — they are unloaded, then the model loads | `evict_idle` |
| 5 | Only hosts that would evict a locked model | `model_locked` → 503 |
| 6 | Only hosts that would evict a pinned model or a model with a running thread | `conflict` → 503 |

Stages 2 and 4 are both loads: the client waits for a load either way. Stage 2 is the
one that carries no promise — it is reported with the same `cold_load` token as stage 3.

The refusal stages are mandatory: **where a forecast exists**, the balancer must never
silently evict a running session to serve a request. Without a forecast (stage 2) it
cannot tell, and it does not pretend to — the load is allowed and the absence of a
forecast is recorded in the decision note. The error message names the model, the
node and the blocking model including thread count.
**Acceptance:** With DeepSeek loaded on the M5, a Qwen3.8 request is routed 10/10
to the M3 (log reason `cold_load`), the M5 shows **no**
`Evicting` line in the log. If both nodes are occupied with DeepSeek, the balancer
answers with 503 and the conflict details.

### FA-19 Slot counting per model (M)
Thread slots and the deficit calculation (FA-2) are kept **per model**, not
globally across all models. Otherwise a node that cannot take a model
accumulates a deficit that it can never offset, and the distribution
tips permanently.
**Acceptance:** Five threads on DeepSeek yield 3/2; Qwen3.8 threads opened in
parallel land exclusively on the M3 without changing the DeepSeek
distribution.

### FA-21 Host and address resolution (M)
The configuration is two-stage 1:n — **one proxy to n hosts, one host to n
addresses**. The address list is not a routing decision but a
transport detection: it is probed in order at proxy start, and the
first responding address is used for the lifetime of the process.

```yaml
hosts:
  - name: alpha               # stable identity, key for slots + affinity
    weight: 3
    addresses:                # in probe order
      - http://192.0.2.11:8000
      - http://203.0.113.1:8000
  - name: beta
    weight: 2
    addresses:
      - http://192.0.2.12:8000     # SA, wired
      - http://203.0.113.2:8000    # TB5
      - http://198.51.100.49:8000 # SB
```

**A host only counts as resolved once its identity is confirmed.** Reachability
alone is not enough: `GET /api/cluster/node_id` answers without
authentication and returns a UUID unique per machine
(verified 14.11.2026: the M3 answers with `22222222-…` over all three addresses, the M5
with `11111111-…`). The proxy checks against the expected
`node_id` from the configuration. That way neither an address reused via DHCP
nor a swapped machine can be slipped in.

**Resolution rules**

1. **Same subnet first:** addresses that lie on an interface of the
   proxy itself are probed before the others — regardless of the
   configured order. A site change therefore needs no
   config change.
2. Then the configured order.
3. **Re-resolution only on demand:** if the health check of a resolved
   host fails `unhealthy_after` times in a row (default 3), the address list is
   tried again. Reason: at proxy start a machine may not yet be
   up; without this step it would stay dead for the process lifetime.
   Every switch is logged as `addr_switch` with old and new address.
4. **Deliberate switch:** `--reinit` (or `POST /reinit`) discards all
   resolutions and probes again — the intended path after a site change.
5. **A transition into `healthy` re-reads what the node can tell us:**
   the version (`/api/cluster/node_id`), the model list with `loaded` and
   the size of each model, the capabilities, the memory ceiling and what is
   in use. Not on every poll — that would be one request per node per
   interval for data that changes only when a machine is updated — but on
   the transition `down`/`loading`/`degraded` → `healthy`; `resolve()` does
   the same, so the two paths agree. A clean restart after an update is too
   short for rule 3 to fire: without this step the version would stay the
   one of the process that is gone. A changed version is logged once with
   both values (`version host=beta old=0.8.0rc1 new=0.8.0`) and is
   therefore visible in `/healthz` and on the fleet card within one
   discovery interval.

**Slot counting and affinity hang on the `name`, never on the address.** An
address change must not discard the thread bindings; only a `--reinit`
does that, and then deliberately.
**Acceptance:** (a) First address entry unreachable → the second is taken,
log reason `addr_fallback`. (b) A foreign oMLX answers on the address →
host is rejected with `identity_mismatch` and **not** used.
(c) Address change via `--reinit` → thread bindings are preserved.
(d) A node that was updated and restarted — a clean restart too short for
rule 3 — reports its new version and model list in `/healthz` within one
discovery interval, without restarting the proxy, and the change is logged
once with both values.

### FA-22 Dashboard (M)
At `GET /` the balancer serves a **read-only** overview page that
opens in the LAN without a build step and without internet access (no CDN, no framework)
and reloads every 2 s.

**Content, in this order:**

1. **Fleet** — one card per host: name, active address, status (healthy /
   degraded / down), oMLX version, memory guard on/off, memory bar
   (`used_bytes` against `ceiling_bytes`), `inflight`, occupied thread slots against
   target share, RTT, measured tok/s.
2. **Active models** — the section that the oMLX dashboard shows under "Active
   Models", here but with a host column. One row per **(model, host)**,
   grouped by model:

   | Model | Host | loaded | Size (est./act.) | Context | running req. | Thread slots | tok/s |
   |---|---|---|---|---|---|---|---|

   Models that only one host offers appear with one row; models on
   both hosts with two rows under the same group heading. Models that are not
   loaded but available are shown dimmed, so that
   "present" and "resident" are not confused.
3. **Distribution** — per model: target share (3 : 2) against actual share of the
   thread slots, as bars with numbers.
4. **Last decisions** — ring buffer of the last 200 routing decisions
   (time, model, thread, host, reason, prompt size, duration, status), newest on
   top and filterable by thread, reason and host (#101 widened this from 50 and
   added the thread column), so that it is visible *why* a request landed where
   it did.

**Explicitly not part of the dashboard:** load/unload actions, configur-
ation changes, anything writing. The balancer does not intervene in oMLX
(NZ-4); the dashboard is a window, not a control panel.

**Amendment, 15.11.2026 (`docs/loading-state.md` §6.1):** one exception, and only
one — a `Calibrate` button per node. It is an operator-initiated maintenance
action, not a general load/unload control: admin-key protected like
`/admin/keys`, disabled while the node holds an active thread, behind a
confirmation dialog that names the models to be loaded and what may be evicted.
It exists because the load rate is a property of the node's storage path, and a
hard-coded rate would be wrong for every setup but one. Everything else on the
page stays read-only.

**Second amendment (issues #10, #11):** the maintenance toggle and the per-(model,
node) policy field. Both are routing state inside RIPPLE — no model is loaded,
unloaded or patched — so they are a smaller exception than `Calibrate`, not a
bigger one.

**Third amendment, issue #13 (`docs/replication.md`):** a `Replicate` panel that
copies one model from one node to another — files over `rsync`/SSH, then
`POST /admin/api/reload` on the target, then the source's per-model settings.
This is the **third and last** exception, and the only one that writes to another
machine's filesystem. It is on the same terms as the first: operator-initiated,
admin key, one model at a time, behind a confirmation dialog that names source,
target, size and free space — and it opens a maintenance window on the target
itself, because the reload it needs unloads every model there. There will be no
fourth exception: anything beyond these three is a different tool, not this
dashboard.

**Amendment, issue #83:** the fleet card carries a **tachograph** — two lines over a
configurable window (default 15 min): total throughput `n · rate(n)` solid,
throughput per thread `rate(n)` dashed, distinguishable by dash pattern alone. The
data comes from a bounded in-memory ring of 5-second buckets per (host, model), one
hour deep, cleared on restart; `/healthz` serves the window as `hosts[].series`, one
entry per model with `bucket_s`, `window_s`, `n`, `total` and `per_thread`. A bucket
without a sample is `null` and the line breaks there — never interpolated. A bucket
with `n = 0` carries **no** rate: nothing was running, so there is no per-thread rate
to report. The series is the curve the optimizer (#80) reads; it is not persisted,
and it adds **no** exception to the read-only rule above.

**Acceptance:** Page loads in under 200 ms, works with the network cable to the internet
pulled, and the model table matches
`/admin/api/models` of both hosts (spot check: loaded models and
their sizes).

### FA-20 Configuration check (S)
`--check` validates the configuration, probes both upstreams and prints the
routing table (which model is available/loaded on which node),
without accepting requests.

### FA-23 Backend adapter with capability check (M)
Every backend is attached via an adapter (§6.2). At start the
balancer checks against the running backend which capabilities are actually
present — not what the configuration claims — and reports every missing
capability visibly: `WARN` in the log, its own field in `/healthz`, banner in the
dashboard. A backend without `residency` switches FA-18 **off** instead of
pretending it.
**Acceptance:** A stub configured as `openai_compatible` leads at start to
`eviction_guard: false` in `/healthz` and to a banner in the dashboard; an
`omlx` backend reports `true`. In both cases the balancer starts and accepts
requests — it does not refuse service, it only tells the truth about
its capabilities.

## 7. Non-functional requirements

| ID | Requirement | Acceptance |
|---|---|---|
| NFA-1 | Additional latency p50 < 5 ms, p95 < 20 ms (TTFB overhead) | measurement over 100 requests |
| NFA-2 | Memory footprint of the balancer < 300 MB RSS | `ps` under load |
| NFA-3 | Operation without a build step, start via command and via double-click | `deploy/start-balancer.command` |
| NFA-4 | Runs as a launchd service, starts automatically after reboot | `launchctl list` |
| NFA-5 | Python standard dependencies only (FastAPI, httpx, PyYAML, uvicorn) | `requirements.txt` |
| NFA-6 | Tests run without oMLX and without a model (stub upstreams) | `pytest -q` green |
| NFA-7 | No access from outside the LAN (bind to LAN interface configurable) | `lsof -iTCP:8010` |
| NFA-8 | Log rotation / no log file growing without bound | size after 24 h < 50 MB |

## 8. Constraints

- **RB-1** The M5 is the entry point. If the balancer fails, the M5 is
  reachable directly (`:8000`) — that is the documented emergency operation.
- **RB-2** The balancer is itself a single point of failure. That is
  accepted deliberately, because the emergency operation (RB-1) is one line.
- **RB-3** Both nodes can hold only one large model; the balancer
  does not resolve memory conflicts and must not force model changes.
- **RB-4** A 400 GB model needs 60–70 s to load. Every routing
  decision that chooses a cold node must make that recognisable as a deliberate
  choice (log reason `cold_load`).
- **RB-5** `X-Session-Id` is an addition, not a requirement: clients without the
  header must remain fully functional.
- **RB-6** The weights 3 : 2 are a specification, not a measurement. The
  actual capability of the M3 has not yet been measured (see AS-6).
- **RB-7** The balancer is **transport-agnostic**: it speaks HTTP and
  needs no Thunderbolt. The `base_url` per node is freely selectable; for the
  M3 use the wired path (`192.0.2.12`) or TB5 (`203.0.113.2`),
  **not** the Wi-Fi address (`192.0.2.216`, 38 ms RTT).
- **RB-8** Thunderbolt 5 is not used by the balancer and not required.
  It remains reserved for the oMLX cluster (JACCL) and for copying
  model weights.
- **RB-9** Addresses are **not** an identity feature. In the home lab they are
  assigned by DHCP; an address can go to another machine after a reboot.
  Identity is exclusively the `node_id` from
  `GET /api/cluster/node_id`. Every address in the configuration must therefore be
  stored with the expected `node_id`.
- **RB-10** The address list of a host is a **probe order**, not a
  load-balancing mechanism: only one address per host is ever used.

## 9. Acceptance scenarios

| ID | Scenario | Expectation |
|---|---|---|
| AS-1 | Open 5 threads one after another (1 request each, then idle) | 3 threads on M5, 2 on M3 |
| AS-2 | Open 10 threads one after another | 6 / 4 |
| AS-3 | 5 turns of one conversation (large context) | 5/5 same node, no switch |
| AS-4 | 20 vision requests (9B, short prompts) | 12 / 8, afterwards 0 occupied slots |
| AS-4b | Let the thread TTL expire (5 threads, wait 31 min) | distribution starts again at 1 / 0 |
| AS-5 | M3 stopped | 10/10 on M5, `/healthz` shows `down` |
| AS-5b | DeepSeek loaded on M5, request for Qwen3.8-oQ4e | 10/10 to the M3, **no** `Evicting` line in the M5 log |
| AS-5c | Both nodes hold DeepSeek, request for Qwen3.8-oQ4e | HTTP 503 with conflict details (model, node, blocking model) |
| AS-6 | **Measurement before fixing the weights:** 10 identical 8k prompts per node, decode tok/s | the ratio of tok/s justifies the weights; deviation from 3:2 is documented |
| AS-7 | 10-min streaming request | runs through, TTFB overhead < 50 ms |
| AS-8 | Request for an M5-exclusive model | 10/10 on M5, log reason `model_pinned` |
| AS-9 | Balancer restarted while streams are running | streams break off (documented), new requests run |
| AS-10 | Key search in logs and `/stats` | no hits |
| AS-11 | Proxy start with unreachable first address | second address is taken, log `addr_fallback` |
| AS-12 | Foreign oMLX answers on a configured address (`node_id` does not match) | host is rejected (`identity_mismatch`), no request to it |
| AS-13 | Site change: `--reinit` after address change | new address active, existing thread bindings preserved |

## 10. Risks and open decisions

| ID | Risk / question | Handling |
|---|---|---|
| R-1 | **Compression breaks the affinity** — after a compaction the first user message does not change, but the context does; with Hermes the beginning is mostly preserved, with heavily rewritten histories it is not | explicit header with Hermes (`extra_headers`), fingerprint only as fallback; node switch is logged |
| R-2 | **3 : 2 is unmeasured** | AS-6 before fixing it; weights remain configuration |
| R-3 | **Cold node costs 70 s** | FA-12 (residency) — placement prefers a resident node; FA-16 (warmup) is not implemented |
| R-4 | **Prefix cache vs. distribution** — every redirect costs prefill | affinity (FA-6), spillover only under real saturation |
| R-5 | The M5 additionally serves its own agent session; a spillover to the M5 can slow down that session | `max_inflight` of the M5 conservative (initial value 3) |
| R-6 | Memory guard/eviction on both nodes can evict models | FA-18: the balancer forecasts the eviction itself and locks the node instead of provoking it; the balancer issues no load command, but a request it places on a cold node makes oMLX load (and, at stage 4, evict) — which is why FA-18 forecasts it |
| R-7 | Balancer as SPOF | RB-1/RB-2, emergency operation documented |
| R-8 | **Open:** should the balancer run on the M5 or on a third host? | Proposal: M5, since it is the entry point anyway; direct access in case of failure |
| R-9 | **Headcount is not load.** The deficit rule counts threads, not work: a thread with 200 k context costs a multiple of a 5 k thread. With 3 large threads on the M5 and 2 small ones on the M3 the real load is not 3:2 | (a) `spill_threshold`/`max_inflight` bound the imbalance; (b) AS-6 measures the real tok/s; (c) **option:** weighting the thread slots by estimated prompt size (`thread_cost_mode: tokens`), as an extension after M3 |
| R-10 | **Thread detection without a header is heuristic.** Clients that rewrite the history (compression, reordering) can produce a new fingerprint — the thread then counts as new and occupies a second slot until the TTL applies | explicit header with Hermes/opencode; make the slot counter visible in `/healthz`; log reason `new_thread` on every first contact |

## 11. Scope of delivery

1. `docs/requirements.md` (this document)
2. `ripple/` — implementation (Python 3.11+, FastAPI/httpx)
3. `config.example.yaml` — commented example configuration
4. `tests/` — acceptance scenarios AS-1…AS-5, AS-8, AS-10 automated
5. `deploy/` — launchd plist, double-click start script
6. `README.md` — operating manual in German
7. Measurement report AS-6 (after commissioning, separate document)

**Milestones**

| M | Content | Done when |
|---|---|---|
| M1 | Requirements specification accepted | sign-off by the operator |
| M2 | Balancer with FA-1…FA-15, tests green | `pytest` green, AS-1…AS-5 evidenced |
| M3 | Commissioning on the M5, measurement AS-6 | weights confirmed or corrected by measurement |
| M4 | Continuous operation as a launchd service | 7 days without intervention, logs evaluated |
| M5 | **Expansion stage (not part of acceptance):** second site over WAN | only after a separate decision; presupposes tunnel, RTT weighting and model copy per site |
| M6 | **Expansion stage (not part of acceptance):** remote providers as a policy target (OpenRouter et al., §6.2, NZ-8) | allow-list, overflow threshold and cost budget take effect; demonstrably unreachable without policy |

---

## 12. Implementation language — recommendation

**Python 3.13 (FastAPI + httpx), no further runtime dependencies.**

Reasoning, with measurement: in a fresh venv (Python 3.13.9) `fastapi 0.162.7`
and `httpx 0.28.1` install without compilation and without conflicts
(checked 14.11.2026). Throughput is uncritical — the balancer moves
streams of a few MB/s with a handful of concurrent requests; the
bottleneck is always the model, never the proxy. What decides it is
changeability: the routing policy (affinity, thresholds, weights) will be
adjusted several times after the measurements from AS-6, and that is possible in Python
without a build step.

Alternatives, rejected: **Go** would be nicer operationally (one binary, no
venv), but the policy would be harder for the operator to read and every adjustment
would need a build. **Node** offers no advantage over Python.

---

## 13. Make-or-buy: does this already exist?

**Yes.** The question is not oMLX-specific, and mature
open-source proxies for it exist. This was initially not checked when this
requirements specification was written — the addendum is here because it
influences the decision.

### 13.1 Projects found

| Project | Language | What it solves | Relevant for us |
|---|---|---|---|
| **[thushan/olla](https://github.com/thushan/olla)** (Apache-2.0, 338 ★) | Go, one binary | proxy + LB for local LLM backends, **oMLX is a native backend type** (since v0.0.28), sticky sessions with prefix hash, priority/weighted/least-connections routing, circuit breaker, model discovery, **embedded dashboard**, < 50 MB RAM, 0,3 ms overhead | **highest match** |
| [vllm-project/router](https://github.com/vllm-project/router) (Apache-2.0, 511 ★) | Rust | **fork of SGLang Model Gateway.** Policies: round-robin, random, consistent-hash, power-of-two, cache-aware, kv-aware; plus its own `Program` layer (identity, slots, idle TTL, resume) | not as a solution (homogeneous workers), but **harvested architecturally** — see 13.6 |
| [llm-d](https://llm-d.ai) + Gateway API Inference Extension | Go/K8s | **prefix-cache-aware** routing with a global prefix index, endpoint picker | most powerful variant, but K8s-only |
| [LiteLLM Proxy](https://docs.litellm.ai/docs/routing) | Python | session affinity, `least-busy`, `latency-based`, weighted deployments, custom routing strategies | mature, but heavyweight |
| [SGLang Router](https://github.com/sgl-project/sglang) | Rust | `cache_aware` policy (radix tree), `po2`, `random` | tied to SGLang |

### 13.2 What Olla already covers

- OpenAI-compatible entry point with streaming (FA-1, FA-8)
- Sticky sessions with **the same key sources that were specified here**:
  `X-Olla-Session-ID`, **`prefix_hash`** (hash over the first bytes
  of the `messages` field — exactly the fingerprint from FA-6), `auth_header`, `ip`
- Health checks, circuit breaker, automatic failover (FA-11)
- Model discovery per endpoint (FA-4), including `/v1/models/status` for
  resident models
- Dashboard with fleet overview and model inventory (roughly FA-22)
- Several Macs behind one proxy — **this use case is explicitly described in
  Olla's oMLX documentation**

### 13.3 What Olla does not cover — and precisely the hard points

| Requirement | Status in Olla |
|---|---|
| **FA-18 Eviction protection** | Explicitly not supported: "Model Load/Unload Control — oMLX manages residency itself; Olla does not proxy lifecycle endpoints". Olla does not know `estimated_size` and `hard_threshold`. A Qwen3.8 request can therefore land on the M5 and evict DeepSeek. Olla only has a troubleshooting paragraph for that ("Eviction Thrashing"), no prevention. |
| **FA-2/FA-19 Thread placement 3 : 2** | **Not representable — measured on 14.11.2026.** There is no weight field (`weight` is discarded as an "Ignored key"). `least-connections` ignores `priority` completely (100 vs. 50 → 5/5). `priority` is a hard stage, not a weight: 3 vs. 2 yields 40/0, not 24/16. So only 100/0 and ~50/50 exist; the fraction 3:2 is missing by construction. Details: [`docs/olla-evaluation.md`](olla-evaluation.md), section 4. |
| **FA-21 Host identity** | No `node_id` concept. An address reused via DHCP is accepted as a valid endpoint. |
| **FA-12 Residency preference** | `/v1/models/status` is passed through as a path (HTTP 200), but **not used as a routing criterion** — the routing argument is `model_found` in every response, i.e. availability. Empirically confirmed: a request for a 21 GB model evicted the 403 GB model of the running session, the client saw HTTP 200. |
| **FA-22 Dashboard with active models** | The Models tab shows `MODEL / PARAMS / QUANT / SIZE / ENDPOINTS / LAST SEEN`. Host column present, **residency missing**; `PARAMS`, `QUANT`, `SIZE` are consistently set to `—`. Olla shows availability, not loading. |

### 13.4 Measurement of 14.11.2026 — result

Olla was set up against both hosts and measured (full report:
[`docs/olla-evaluation.md`](olla-evaluation.md)). The check from point 1 of the
earlier version is therefore done. Summary:

| Question | Answer |
|---|---|
| 3:2 distribution possible? | **no** — only 100/0 or ~50/50 (section 4) |
| Does Olla route by residency? | **no** — only by availability (section 5) |
| Does Olla protect against eviction? | **no** — eviction occurred in the test, client saw HTTP 200 |
| Sticky sessions usable? | **yes** — `session_header` and `prefix_hash`, 5/5 reproducible |
| Failover usable? | **yes** — dead endpoint becomes `offline`, 0 errors |
| Model filter usable? | **yes** — model only on M5 → 100 % M5, cold start 16,5 s |
| Overhead | +45,4 ms / +20 % on the chat path (not 0,3 ms) |
| Ready for immediate use? | **no** — release zip without `config/profiles/`, `--validate-config` still reports PASS |

**The assumption from the earlier version, that `priority` produces 60/40 on
average, was wrong.** Measured was 40/0 with priorities 3 against 2: the priority selects
*which host is served at all*, it does not weight. This correction is the
most important yield of the measurement — otherwise it would have led to a wrong decision.

### 13.5 Recommendation

1. **Build in-house, as specified in this requirements specification.** The core — threads stay
   where they are, new threads are distributed by deficit until 3:2 is reached —
   is not representable in Olla. That is not a configuration gap but a
   design decision of the project.
2. **Adopt from Olla** (demonstrably works there and is proven):
   - sticky key sources as a chain: `session_header` → `prefix_hash`
   - header names as a model for traceability: `X-Olla-Endpoint`,
     `X-Olla-Routing-Reason`, `X-Olla-Sticky-Key-Source`
   - dashboard structure: three tabs, endpoint table with status, latency, requests
   - model filter via the model list of the endpoints
3. **Olla as an interim solution** is usable for failover and model filter, but
   without 3:2 and without eviction protection — only with loopback binding and deliberately without
   weighting. The path prefix `/olla/omlx/v1` forces a client adjustment.
4. **Upstream contribution: not recommended.** Weighting and residency routing would be
   two feature requests against the author's explicit design decision
   not to proxy lifecycle control. Not a reliable plan.

**What this requirements specification is worth in any case:** it describes the requirements so
precisely that every option can be measured against it instead of deciding by gut feeling —
and it exposed the wrong assumption about `priority` before the build, where it would have been expensive.

### 13.6 What was adopted from `vllm-router` / `sgl-model-gateway`

`vllm-project/router` is a **fork of SGLang Model Gateway** (says so in the
README, with thanks to the original authors) — it is one codebase, not two.
Both presuppose homogeneous workers with one model and are out as a solution;
the architecture was nevertheless instructive. Three things were incorporated:

| Adopted | Where in the requirements specification | Source |
|---|---|---|
| Hysteresis over two simultaneously checked thresholds (absolute **and** relative) | FA-2 | `is_imbalanced()` in `src/policies/cache_aware.rs` |
| Atomic reservation before dispatch (admission critical section) | FA-2 | `select_prepared()` — placement in the admission section |
| Prefix similarity as a ratio instead of a fixed hash, on characters instead of tokens | FA-6 | `PrefixMatchResult { matched_char_count, input_char_count }` in `src/tree.rs` |
| Separation of binding ↔ admission, binding strategies as a menu | §6.1 | `binding_strategy` / `RequestPool` of the program layer |

**Not adopted, with reasons:**

- **Cost models** for prefill and decode (`prefill_cost_model`,
  `decode_throughput_model` with a quadratic term). With two nodes we measure
  `tg_tps` and `pp_tps` directly (AS-6) instead of modelling them.
- **KV event routing** — oMLX delivers no KV events.
- **Token capacity calculation** — our currency is `estimated_size` against
  `final_ceiling`, not KV tokens.
- **Radix tree as a data structure.** The tree is right there because it scales over
  millions of tokens and many workers. For a thread registry with
  a handful of entries a character comparison against stored
  prefixes suffices. What was adopted is the *measure* (match ratio), not the tree.

**The most valuable single finding:** the `Program` layer of `vllm-router`
independently describes the same model that the operator specified for this balancer
— a long-lived unit with identity, slots per target, idle TTL,
resumption on the previously bound target, migration only with sufficient
headroom (`cross_rank_headroom_ratio: 1.2`). That is the best available
confirmation that the design has the right shape.

**Not checked:** both routers were not run against oMLX. The result
would be predictable (no 3:2, no residency) and would cost half a day
— the measurement on Olla has already shown it.
