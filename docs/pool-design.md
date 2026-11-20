# Pool design — feasibility of a two-house node pool

**Issue:** #72. **State:** study, no code. Nothing here is built.
**Question:** can machines in different places join a pool over a VPN, serve each
other's requests, and settle the exchange in a measured quantity instead of a
currency — and what would have to be true before the first foreign request is
routed?

**Answer in one line:** yes, but only as a *compute-sharing* scheme between two
or three houses that already trust each other, and only for work that is
decode-heavy on a model that is *already resident* at the far end. The network
is not the limit; the prefill asymmetry and the absence of a shared prefix cache
are. The numbers below say so, and they are measured on this fleet, not assumed.

---

## 1. The goal

Two or more machines in different places join a pool over a VPN, and what a node
gives is what it gets back: a node that serves requests for others accrues
measured work, and its owner spends that work later on a bigger model or on more
concurrent agents. RIPPLE stays a balancer: it does not grow a tunnel, a
currency, or a public identity system. The concrete case that motivates it is a
family member's Mac Mini M5 Pro that cannot hold the large models but keeps a
Qwen 27B resident — useful to the pool precisely because it is always loaded —
against the operator's link of 1 Gbit down / 300 Mbit up, with WireGuard or
Tailscale and no port opened to the internet. The pool therefore exists to trade
*availability of resident models*, not to buy speed from a distant machine.

---

## 2. Why WAN only pays in a narrow band

### 2.1 The link is not the bottleneck — the bytes are trivial

The OpenAI API is stateless: every turn re-sends the whole conversation, because
there is no field that identifies a conversation (`docs/requirements.md` §2,
FA-6). That is the one property that makes a long-distance hop look expensive.
It is not, in bytes. At roughly 4 bytes per token:

| prompt | payload | upload at 300 Mbit/s | upload at 30 Mbit/s |
|---|---|---|---|
| 8 000 tok | 0.03 MB | 0.001 s | 0.009 s |
| 24 500 tok | 0.10 MB | 0.003 s | 0.026 s |
| 200 000 tok | 0.80 MB | 0.021 s | 0.213 s |
| 1 000 000 tok | 4.00 MB | 0.107 s | 1.067 s |

Even a 1M-token context costs a tenth of a second on the operator's uplink. The
download side is not a constraint either: 60 tok/s of streamed JSON is a few
kilobytes per second. **Bandwidth is not what makes WAN hard.**

### 2.2 What the link does cost, and what it does not

The only measured high-latency path in this repository is the Wi-Fi route to the
second node (`docs/requirements.md` §2.1):

| path | RTT avg | jitter | HTTP `/health` |
|---|---|---|---|
| Thunderbolt 5 | 0.60 ms | 0.16 ms | 6 ms |
| LAN wired | 0.71 ms | 0.06 ms | 5 ms |
| LAN via Wi-Fi | **38.3 ms** | **67.9 ms** | 15–41 ms |

Two facts come out of that table, and they point in opposite directions:

1. **Mean RTT is negligible against the work.** 38 ms is 0.29 % of the 13.31 s a
   real request takes on the M5 (§2.3). At the 80 ms upper end of the design
   range it is 0.6 %. A VPN adds a round trip; a round trip is not what a
   prefill-dominated request is made of.
2. **Jitter is what breaks a stream.** The same document's own conclusion is
   that 38 ms average with 67.9 ms jitter and peaks above 170 ms "means streams
   that break off". A VPN over a public link has exactly that shape, and a
   broken stream has no resume (FA-8 streams without buffering, FA-10 retries
   only before the first byte, FA-9 no request timeout of RIPPLE's own). This is
   the failure mode to design against — not latency.

### 2.3 The worked example

Take the request the fleet is actually measured with: 24 500 prompt tokens,
400 completion tokens. Wall time is `prefill + decode + RTT`:

| where it runs | prefill | decode | RTT | total |
|---|---|---|---|---|
| M5, idle (`pp_tps` 3 680, `tg_tps` 60.2) | 6.66 s | 6.64 s | 0.001 s | **13.31 s** |
| M5, six streams (`tg` 16.9 per stream) | 6.66 s | 23.67 s | 0.001 s | **30.33 s** |
| WAN node, M5 class, 38 ms | 6.66 s | 6.64 s | 0.038 s | **13.34 s** |
| WAN node, M3 class (`pp_tps` 970, `tg_tps` 43.5), 38 ms | 25.26 s | 9.20 s | 0.038 s | **34.49 s** |
| WAN node, M3 class, 80 ms | 25.26 s | 9.20 s | 0.080 s | **34.54 s** |

Rates: `config.yaml` (`tg_tps`, `pp_tps`), measured per `docs/measurements.md`
(median 60.2 / 43.5 tok/s decode; 3 680 / 970 tok/s cold prefill at 24.5k).
Per-stream decode under six concurrent streams: `docs/concurrency.md`.

Read the last two rows against the first: **doubling the RTT from 38 to 80 ms
moves the total by 0.05 s. Running on a slower machine moves it by 21 s.** The
WAN penalty that matters is not the network, it is that a far-away node is
usually a weaker machine — and that it has no shared prefix cache.

### 2.4 The boundary

A cold hop pays only if it beats the local alternative:

```
WAN pays  ⟺  P/pp_w + C/tg_w + RTT  <  C/tg_local_eff
         ⟺  P < pp_w · (C/tg_local_eff − C/tg_w − RTT)
```

with `P` prompt tokens, `C` completion tokens (400, the configured
`placement.rebind.assumed_completion_tokens`), `pp_w`/`tg_w` the far node's
class, and `tg_local_eff` the local node's *per-stream* rate — what a client
actually gets when the local machine is shared, taken from
`docs/concurrency.md`. Solving for `P`:

| local alternative | WAN node | break-even prompt `P*` |
|---|---|---|
| M5 idle (60.2 tok/s) | M3 class, 38 ms | **never** (negative) |
| M5 idle | M5 class, 38 ms | **never** (negative) |
| M5 at 6 streams (16.9 tok/s) | M3 class, 38 ms | **14 000 tok** |
| M5 at 6 streams | M5 class, 38 ms | 62 500 tok |
| M3 at 6 streams (11.9 tok/s) | M3 class, 38 ms | 23 600 tok |
| M3 at 6 streams | M5 class, 38 ms | 99 100 tok |

**A cold hop never beats an idle local machine at any prompt length.** The
prefill asymmetry (3.6–4.1 : 1, `docs/measurements.md`) is larger than the
decode gain (1.25–1.31 : 1) and it is paid up front, so the far node starts
behind and stays behind. Against a *loaded* local node a hop pays — but only up
to ~14k tokens with a same-class-as-M3 far node. Above that the far node's
prefill dominates and the queuing local machine still wins. The bound is
conservative in the loaded cases, because it models local prefill at its idle
rate while `docs/concurrency.md` measures 11.1 s instead of 8.1 s at six
concurrent — so real `P*` is somewhat higher, not lower.

Now the other half, the one that does work: a thread that is **already bound**
to the far node has its prefix cached there, and pays decode only.

| 400 completion tokens | wall |
|---|---|
| local M5, six streams (16.9 tok/s) | 23.67 s |
| WAN node, M3 class, idle (43.5 tok/s) | 9.20 s |
| WAN node, M3 class, six streams (11.9 tok/s) | 33.61 s |

**Conclusion for the router: a WAN node pays for continuation, not for
arrival.** It is worth using when (a) the model is resident there and nowhere
local, (b) the thread is already bound there, or (c) the local node's
per-stream decode has collapsed under load. It is never a prefill accelerator,
and a rebind rule tuned on a LAN (`placement.rebind.safety_factor: 1.5`,
`min_gain_seconds: 2.0`, both written when the other node was 3.6× faster at
prefill) must not be allowed to move a thread across a site boundary: the
prefill advantage inverts there, and a rebind across sites costs a full cold
prefill — 48 s at 200k tokens on the M5, 191 s on the M3
(`docs/measurements.md`).

### 2.5 Weights over the link

Sharing a *model* is not sharing a *file*, and this is where the arithmetic
becomes blunt. At 300 Mbit/s up:

| model | size (repo) | 300 Mbit/s | 30 Mbit/s |
|---|---|---|---|
| `DeepSeek-V4.1-Flash-1017-oQ4e-mtp` | 402.6 GB (`docs/requirements.md` §2) | 2.98 h | 29.8 h |
| `DeepSeek-V4.1-Flash-1017-oQ4e-mtp` | 453.6 GB (`docs/measurements.md`) | 3.36 h | 33.6 h |
| `Qwen3.8-Flash-Next-oQ4e-mtp` | 111.6 GB (`docs/measurements.md`) | 0.83 h | 8.3 h |
| `Qwen3.5-9B-oQ4e-mtp` | 6.5 GB (`docs/requirements.md` §2) | 0.05 h | 0.5 h |

A pool therefore only ever trades models a node **already holds** — which is
also what NZ-3 requires (replication is operator-initiated, one model at a
time, never as part of routing). The 400 GB class is not shippable to a house
with a domestic uplink, and shipping it is a licence question before it is a
bandwidth question (§7).

---

## 3. Trust model

### 3.1 Identity is `node_id`, and it is already the right primitive

The fleet already treats reachability as insufficient evidence: `ripple/discover.py`
states that "reachability alone is not evidence that an oMLX node answers", and
`ripple/hosts.py` refuses a host whose `GET /api/cluster/node_id` does not match
the configured value (acceptance scenario AS-12, `docs/requirements.md` §9).
`ripple/calibration.py` keys its samples by `node_id` "never by host name",
because a name is a label an operator may change at any time.

**TOFU rule for the pool:** the first time a member is added, its `node_id` is
recorded and shown to the operator as a fingerprint — the first eight hex
characters, the same stub `discover.py` already derives for a suggested host
name. From then on, a node answering on that address with a different `node_id`
is not the member; it is refused, and the refusal names both fingerprints. The
fingerprint is displayed wherever the node is displayed, so that "is this still
a family member's machine" is answerable without reading a config file.

### 3.2 What a re-pointed address or DNS name does

| event | without pinning | with pinning |
|---|---|---|
| VPN address reassigned to another host | the pool routes to a stranger, silently | refused: `node_id` mismatch |
| DNS name re-pointed | same | same |
| the same machine re-addressed | the pool loses it | found again by `node_id` — the reason identity is not the address |

The gap that pinning does **not** close: `node_id` is a self-declared string,
not a key. A host that presents the same `node_id` from different hardware is
indistinguishable from the real one. That is why identity pinning and key
pinning are two different mechanisms and both are needed.

### 3.3 Keys per node, not per pool

Today one oMLX key is valid on both nodes (`docs/requirements.md` §2), and that
convenience is exactly what must not survive into a pool:

* **A member must not read another member's prompts.** Keys are per node. The
  entry node holds a member's key for *that member only*; there is no pool-wide
  key that opens every house.
* **The admin key of one house must not unlock another.** `auth.admin_key` and
  the replication `admin_key` are per instance (`config.yaml`). In a pool the
  rule is stronger than convention: a member's admin credential never leaves its
  own house, is never forwarded, and no pool endpoint accepts a remote admin
  key. The pool's own administrative surface is per member and cannot enumerate
  another member's models, addresses or keys.
* **`hide_foreign_models: true` is the model for this** (`config.yaml`): a key
  that asks for a model outside its list gets 404, not 403 — the pool should
  not even confirm what exists elsewhere.

### 3.4 What a compromised member can do

Stated plainly, because a threat model that flatters the design is useless:

| capability | bounded by |
|---|---|
| read every prompt it serves | **nothing** — the prompt is in its RAM. The only defence is not sending it work you would not show that house |
| inflate its own ledger | the two-meter rule (§4.4) |
| advertise models it cannot serve | admission probes and the resident-only rule (§5.1) |
| answer with wrong or malicious output | **nothing technical**. There is no attestation; the pool can verify *that* a node answered and *how much*, never *whether it computed honestly* |
| replay a captured key | per-node keys, rotation, and no key reuse across houses |
| learn the fleet topology | the fleet config is not shipped to members; a member sees its own neighbour entry only |

The honest summary: **a pool is a trust relationship with a measurement layer
on top, not a trustless system.** Anything that claims otherwise is selling
attestation it does not have.

---

## 4. The credit mechanism: reference node-seconds

### 4.1 The unit

Not a coin. A measured, auditable quantity derived from the ledger that already
exists (issue #33, `ripple/host_stats.py`): `prefill_seconds`,
`generation_seconds`, `requests`, plus `prompt_tokens` and `completion_tokens`.

```
credit(r) = prompt_tokens(r) / pp_ref + completion_tokens(r) / tg_ref
```

with `pp_ref` / `tg_ref` a **declared reference class** — the fleet's own
measured fast node, 3 680 tok/s prefill and 60.2 tok/s decode (`config.yaml`,
`docs/measurements.md`). The result is *reference node-seconds*: "how much
work of the reference class this request was worth".

| request | credit |
|---|---|
| 8 000 in, 400 out | 8.82 ref-s |
| 24 500 in, 400 out | 13.30 ref-s |
| 200 000 in, 1 600 out | 80.93 ref-s |

### 4.2 Why tokens and not the node's own seconds

The tempting formula is "the node's own seconds × its class factor", and it is
wrong in a way worth writing down. For the same 24 500 + 400 request:

| node | own wall time | credit under "own seconds" | credit under §4.1 |
|---|---|---|---|
| M5 class | 13.30 s | 13.30 | 13.30 |
| M3 class | 34.45 s | **34.45** | 13.30 |

Paying own seconds pays the slow machine 2.6× for the same delivered work: a
node is rewarded for being slow, and the pool's cheapest members become its
most expensive ones. Tokens delivered is the only quantity both sides can count
and neither can inflate cheaply.

**The honest consequence, which the intuitive phrasing hides:** under this unit
*every* class accrues about 3 600 ref-s per fully busy hour, because the unit is
work and an hour is an hour. A fast machine does not earn more per busy hour —
it earns the same credit in *fewer, shorter requests*, and it can absorb 2.6×
the traffic per hour at the same latency (`docs/concurrency.md`: 99 against
70 tok/s aggregate decode at six streams; 2.6k against 0.83k tok/s prefill).
Speed pays through throughput and latency, not through a multiplier. A
multiplier would be a subsidy, and a subsidy is a currency with extra steps.

### 4.3 Where `pp_tps` / `tg_tps` are actually used

1. **Conversion.** Two currencies (prefill tokens, decode tokens) become one
   number, and the weights come from a measurement rather than a guess.
2. **Admission.** A member's class decides how many concurrent foreign requests
   it may take (§5.2) — a slow node is not offered six streams it cannot hold.
3. **Sanity bound.** The class bounds what a member's self-report can plausibly
   claim (§4.4).
4. **Existing rules.** `placement.prefill_bias` and `placement.rebind` already
   consume these numbers; nothing new is invented for the pool.

### 4.4 Who measures, and who is believed

**The meter of record is the requester's side** — the entry node that forwarded
the request, because it counts what it sent and what it received, and its
interest runs *against* over-crediting the member. This is not a new idea in
this codebase: `ripple/host_stats.py` already keeps `client_seconds` (RIPPLE's
own wall clock) apart from the node-reported `prefill_seconds` /
`generation_seconds`, and counts a response without a usage block in
`requests_without_usage` rather than estimating it — "never estimated from
`prompt_chars` (a real run estimated 25 000 tokens against 82 510 actual)".
The node's own usage block is already treated as a **cross-check**, exactly as
the node's SQLite usage is.

Rules that follow:

* A request is credited at the **entry's** token count. The member's usage block
  must agree within a tolerance (2 % or 1 s, whichever is larger); outside it
  the request is a **dispute**, and a dispute credits **nothing** — the default
  is to withhold, never to grant.
* A member that reports no usage earns nothing for those requests. It cannot
  claim what it cannot document, and `requests_without_usage` is exported so the
  operator can see who is silent.
* Settlement is a **quota**, not a balance: "this member may use N ref-s this
  period". Nothing goes negative, nothing is transferable between members —
  transferability is what turns a unit into a currency, and a currency is out of
  scope (`docs/requirements.md` NZ-7 territory).

### 4.5 What stops self-dealing

| attack | why it fails |
|---|---|
| a node claims a faster class to earn more per token | the unit is tokens, not its claimed speed — a fast claim buys nothing |
| a node claims a faster class to be offered more work | the class is **measured by the pool**, with the `tools/as6_bench.py` / `tools/prefill_bench.py` protocol, fleet quiet — not declared by the member. An unmeasured node has no class and is not admitted |
| a node inflates completion tokens | the entry counts the bytes it received; the token count is in the stream it read |
| a node inflates prompt tokens | bounded by what the entry sent |
| a node credits itself for serving itself | self-requests have no entry-side meter, so they are not billable. Only a request that crossed the pool boundary earns |
| a node runs the clock | no clock is trusted: the entry timestamps dispatch and last byte, and the ledger's own split is the node's *reported* prefill/generation, cross-checked |

One residual hole is worth naming: a member could serve a request *badly* and
still be credited, because credit measures work, not quality. That is a
membership rule (§5), not a metering rule.

---

## 5. Membership rules

### 5.1 Which models may leave the house

Three lists already exist: the per-host allow-list (`hosts[].models`), the
policy levels `disabled` / `hidden`, and the lock (`docs/model-policy.md`). The
pool adds a fourth, and it is **default empty**:

* `pool.export` per host — the models this house is willing to serve to
  *others*, a subset of its allow-list. Nothing is exported because it happens
  to be loaded.
* A model is exported only if it is **already resident** on that node. The pool
  never asks a member to load (NZ-4: RIPPLE never loads or unloads on its own
  initiative), and a cold load is 60–70 s for a 400 GB model
  (`docs/requirements.md` §2).
* The lock wins: a locked model is never offered outward, for the same reason
  it is never evicted for an inward request.
* A model whose weights may not be redistributed is not exported. Serving a
  model you already hold is a different act from copying it (NZ-3), but the
  licence question is open (§7).

### 5.2 How many concurrent requests a member takes

Measured, not negotiated (`docs/concurrency.md`, per-stream decode):

| streams on the node | M5 per stream | M3 per stream | 400 tokens take |
|---|---|---|---|
| 1 | 62.5 tok/s | 27.8 tok/s | 6.4 s / 14.4 s |
| 3 | 20.6 | 21.7 | 19.4 s / 18.4 s |
| 6 | 16.9 | 11.9 | 23.7 s / 33.6 s |

Per-stream decode collapses with every extra stream while aggregate barely
grows (62 → 99 tok/s on the M5 at six). A member that offers all six of its
slots to the pool sells its own house a four-times-slower conversation.
**Rule: a member offers at most two of its six slots to the pool**
(`max_inflight: 6` is the measured value, `config.yaml`), and a member's own
house always outranks a foreign request — the pool's admission yields, the
house's does not.

### 5.3 When a member goes offline mid-stream

The protocol has no resume: FA-8 streams without buffering, FA-10 retries only
before the first byte, FA-9 sets no timeout of its own. A member that dies
mid-stream leaves the client with a stream that simply stops. The exposure is
not small — at 16.9 tok/s a 400-token answer takes 23.7 s, a 1 600-token answer
94.7 s, and every one of those seconds is a chance to lose the answer.

Rules:

1. The entry marks the thread broken and says so; it does **not** silently
   re-run the request elsewhere. A re-run pays a full cold prefill (up to 191 s
   at 200k) and would have to be charged twice or not at all.
2. The member is credited **only** for tokens the client actually received.
3. A member that breaks more than a set number of streams per hour is suspended
   automatically, and the suspension is visible in the operator's view — the
   same shape as `unhealthy_after: 3` in `discovery`, which already exists for
   a node that stops answering.
4. The client-visible signal is a truncated stream, which every OpenAI client
   already handles as an error. That is the one thing the pool gets for free.

### 5.4 Reciprocity instead of access

There is no "member list" that grants access. There is a rolling window and a
rule: **you may draw on the pool only while you contribute to it.** A member
whose contributed ref-s over the window is zero has a redemption quota of zero.
The operator's own house is a member under the same rule — the first thing the
rule has to survive is the operator being on the wrong side of it.

---

## 6. Phases

Each phase has one measurable acceptance and a stop criterion. The first phase
is deliberately *paper*: if the rule cannot be written down and measured
between two houses that trust each other, no amount of code makes it true.

### P0 — The written rule (no code)

Two sites, one shared model resident on both, VPN in place, `node_id` pinned,
per-node keys, one credit rule written down.

**Acceptance:** `ripple doctor` shows both nodes with a matching `node_id` and
a site that matches the address actually used (`docs/sites.md`); one real
request is measured end to end with its RTT, its prefill/decode split and its
wall time; the credit rule names every constant and its source.
**Stop if:** the two houses cannot agree on who measures (§4.4) — that is not a
technical gap and P1 does not fix it.

### P1 — Identity and keys made explicit (code)

TOFU record with a displayed fingerprint; per-node key files; a member's admin
key refused at the pool boundary.

**Acceptance:** a test that an address answering with a foreign `node_id` is
refused (the shape AS-12 already has), and a test that a member key cannot reach
another node's admin routes. **Stop if:** a per-node key makes ordinary
single-house operation worse — that regression is not worth the pool.

### P2 — The ledger as a pool meter (code)

Export reference node-seconds per node per period from the existing ledger;
report the two-meter disagreement instead of hiding it.

**Acceptance:** the exported sum equals the sum of `prefill_seconds` /
`generation_seconds` in `ripple/host_stats.py` to the digit, and a deliberately
corrupted usage block produces a dispute, not a credit.
**Stop if:** the export cannot be reconstructed from the ledger without new
measurement — then the ledger is not yet the meter it claims to be.

### P3 — One foreign request

One member, one model, one direction.

**Acceptance:** 100 real requests, p50/p95 wall time recorded, zero truncated
streams, credit booked by the rule and reconciled by hand against the ledger.
**Stop if:** the measured wall time is worse than the local alternative in more
than a small fraction of cases — that is §2.4 telling you the work is the wrong
work for a pool.

### P4 — Reciprocity with a second member

**Acceptance:** a member that stops contributing loses its quota within one
window, and the operator's own house is subject to the same test.

---

## 7. Open questions, and what we do not know

1. **Licensing of shared weights.** Not checked anywhere in this repository.
   Serving a model a member already holds is a different act from copying
   weights to it (NZ-3), and the pool is designed around the former — but that
   is a design choice, not a legal opinion. Which models may cross a house
   boundary is a question for a lawyer, not for `config.yaml`.
2. **Who is liable when a member's machine produces nonsense.** There is no
   attestation, so the pool cannot distinguish "computed badly" from "computed
   maliciously". Nothing in §4 detects it: credit measures work, not quality.
   This needs a written agreement before P3, and no phase after P3 can proceed
   without one.
3. **Privacy across houses.** A prompt that leaves the house is a disclosure to
   another party, and the far node's RAM, logs and dashboards all see it. There
   is no logging policy for a pool, and `docs/requirements.md` NZ-7 already says
   the balancer is not an auth gateway. Whether a member's prompts may be
   logged at the far end is undecided.
4. **The reference class is a moving target.** A faster machine joining makes
   every existing credit worth less. Nothing here says how a class change is
   grandfather-ordered, and the honest answer is that it has not been thought
   through.
5. **The 27B is unmeasured.** There is no `pp_tps`/`tg_tps` for a Qwen 27B
   anywhere in this repository, so its class is unknown and it cannot be
   admitted to a credit scheme until someone runs the decode protocol on it with
   the machine quiet. Until then the motivating case is a hypothesis, not a
   number.
6. **Revocation.** There is no expiry on `node_id` and no revocation list. A
   member removed from the pool keeps its keys until someone rotates them by
   hand.
7. **Fairness of the exchange itself.** The family member's electricity pays for the
   operator's model availability, and vice versa. Whether node-seconds are the
   right exchange rate between two households is an economics question that this
   document deliberately does not answer — it only makes the quantity
   measurable.
8. **No TLS in oMLX** (`docs/requirements.md` NZ-7): the VPN is the only
   confidentiality boundary. A member outside the tunnel has no channel at all,
   and nothing in RIPPLE would notice a downgrade.

---

## 8. Numbers and where they come from

| number | value | source |
|---|---|---|
| decode, M5 / M3 | 60.2 / 43.5 tok/s | `config.yaml` `tg_tps`; `docs/measurements.md` |
| cold prefill at 24.5k, M5 / M3 | 3 680 / 970 tok/s | `config.yaml` `pp_tps`; `docs/measurements.md` |
| prefill ratio | 3.6–4.1 : 1 | `docs/measurements.md` |
| decode ratio | 1.25–1.31 : 1 | `docs/measurements.md` |
| per-stream decode at 6 streams | 16.9 / 11.9 tok/s | `docs/concurrency.md` |
| aggregate decode at 6 streams | 99 / 70 tok/s | `docs/concurrency.md` |
| RTT, wired LAN / Wi-Fi | 0.71 / 38.3 ms | `docs/requirements.md` §2.1 |
| jitter, Wi-Fi | 67.9 ms, peaks >170 ms | `docs/requirements.md` §2.1 |
| `max_inflight` / `max_threads` | 6 / 20 | `config.yaml`, `docs/concurrency.md` |
| `assumed_completion_tokens` | 400 | `config.yaml` `placement.rebind` |
| 200k prompt prefill | 48 s / 191 s | `docs/measurements.md` |
| model sizes | 402.6 / 453.6 / 111.6 / 6.5 GB | `docs/requirements.md` §2, `docs/measurements.md` |
| 400 GB cold load | 60–70 s | `docs/requirements.md` §2 |
| ledger fields | `prefill_seconds`, `generation_seconds`, `requests`, `client_seconds`, `requests_without_usage` | `ripple/host_stats.py` |

The upload times, wall times, break-even prompts and credit values in this
document are arithmetic over those inputs, not measurements; where a number is
a forecast it is labelled as one.
