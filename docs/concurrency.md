# How much concurrency each node should take

`max_inflight` and `spill_threshold` were set by hand in M2. The M5 — the
stronger node, weight 3, target share 60 % — got the *lower* cap (3, against 6 on
the M3) and `spill_threshold: 2`, so it counted as "full" for new threads from two
concurrent requests on. That contradicted the weights, and the reason given in the
config ("this host also serves the agent session") was a guess, not a measurement.

This is the measurement. Short answer: the guess was wrong in the direction that
hurts — under a burst the M5 was capped out while the slow node queued.

## Method

`tools/concurrency_series.py`, requests only (no model is loaded or unloaded,
NZ-4), against the live instance on `:8010`:

- model `DeepSeek-V4.1-Flash-1017-oQ4e-mtp`, already resident on both hosts
- 9,229 prompt tokens per request (the model reports the real count), 128
  completion tokens — a prefill-dominated request, which is what a subagent
  sending a large context actually does
- every request carries a unique marker at the front of the prompt, so each one is
  its own thread with its own prefix. A shared prefix would bind them all to one
  host through the affinity rule and measure nothing
- levels of 3, 6 and 9 concurrent requests, two configurations, five seconds
  between levels
- throughput per host is divided by the host's *own* span (its slowest request),
  not by the wall time of the level — otherwise a fast host looks slow because a
  slow host is still busy

| configuration | alpha cap / spill / threads | beta cap / spill / threads |
|---|---|---|
| A (M2, hand-set) | 3 / 2 / 12 | 6 / 3 / 12 |
| B (measured) | 6 / 4 / 20 | 6 / 3 / 20 |

Raw data: `tools/concurrency/results/concurrency-20261114-141627-m5cap3-spill2.json`,
`…-141945-m5cap6-spill4.json`, `…-142153-m5cap6-spill4.json` (12 concurrent, slot
exhaustion), `…-142414-m5cap6-threads20.json` (12 concurrent, clean).
Reproduce with `tools/concurrency_series.py --label <name>` and compare with
`tools/concurrency_report.py tools/concurrency/results/*.json`.

## Results

Wall time for the whole burst, split of requests, and prefill throughput per host
(tokens per second against the host's own span):

| concurrent | configuration | wall | split M5 / M3 | M5 tok/s | M3 tok/s | M5 latency |
|---|---|---|---|---|---|---|
| 3 | A | 17.9 s | 2 / 1 | 2233 | 523 | 8.2 s |
| 3 | B | 11.8 s | 2 / 1 | 2212 | 796 | 8.1 s |
| 6 | A | 34.0 s | 3 / 3 | 2359 | 825 | 11.7 s |
| 6 | B | 34.4 s | 3 / 3 | 2458 | 815 | 11.1 s |
| 9 | A | 66.5 s | 3 / 6 | 2458 | 844 | 11.3 s |
| 9 | B | **45.2 s** | **5 / 4** | 2633 | 828 | 17.5 s |
| 12 | B (threads 20) | 66.8 s | 6 / 6 | 2727 | 853 | 20.3 s |

The 3-concurrent row differs only in the M3's single request (17.9 s against
11.8 s) — same split, same M5 latency. That is variance on the M3, not an effect
of the configuration; single-run cells carry ±10–20 % there.

## What the numbers say

**1. The old `spill_threshold: 2` inverted the distribution.** At 9 concurrent,
configuration A put 6 requests on the M3 and 3 on the M5 — the exact opposite of
the 3:2 target — and took 66.5 s. B, with `spill_threshold: 4`, put 5 on the M5 and
4 on the M3, close to the 3:2 target, and finished in 45.2 s: **32 % less wall time
for the same work**. The M5 was not slow, it was refused work.

**2. The M5 saturates at about 2.5 k tok/s and stays there.** 3 concurrent
requests → 2.46 k tok/s, 5 → 2.63 k, 6 → 2.73 k. Prefill throughput is nearly flat
while per-request latency grows from 11.4 s to 20.3 s. Extra slots above ~3 are
queue depth, not capacity — they buy latency for the individual request and
nothing for the fleet. That is why 6 and not 12: the M5's own numbers stop
improving.

**3. The M3 saturates with a single request.** 800 tok/s at 1 concurrent, 815 at 3,
841 at 6. Its six slots are pure queue depth as well. The prefill ratio M5:M3 is
therefore about **3.2:1** — much wider than the 1.38:1 decode ratio we
measured. The weights 3:2 (1.5:1) sit between the two: right for a decode-heavy
conversation, generous to the M3 for prefill-heavy ones. Changing the weights per
workload is not worth it; a 3.2:1 weighting would starve the M3 the moment a
conversation starts generating.

**4. A thread holds its slot until the TTL expires, not until its request ends.**
This is by design (FA-19: a warm prefix cache is worth a reservation), but it has
an operational consequence that the first 12-concurrent run exposed: 24 threads
from earlier levels were still live, both hosts sat at 12/12 slots with `inflight
0`, and nine of twelve new requests were refused with `503 no_slot (12/12 slots)`.
With `max_threads: 20` and a fresh registry the same 12 concurrent requests ran
through as 6/6 without a single 503.

`max_threads` is therefore not a concurrency limit — that is `max_inflight` — but
the number of *conversations* a host keeps warm. 12 was too few for a day of
subagents: after twelve distinct conversations within 30 minutes, new ones are
refused until the oldest thread expires. 20 is the documented default and gives
headroom for a burst.

## The other half: decode under concurrency

The decode measurement (TG) took one request at a time. That is not how a long
conversation meets the fleet: it shares the host with whatever else is running.
Same tool, direct against each oMLX (`--base http://localhost:8000/v1` and
`http://192.0.2.12:8000/v1`), 400-token prompt, 500 completion tokens, unique
marker per request, so prefill is ~2–5 % of the wall time and the numbers are
decode:

| concurrent | M5 per stream | M5 aggregate | M3 per stream | M3 aggregate |
|---|---|---|---|---|
| 1 | 62.5 tok/s | 62.5 | 27.8 | 27.8 |
| 3 | 20.6 | 61 | 21.7 | 64 |
| 6 | 16.9 | 99 | 11.9 | 70 |

What that means for a long session:

- **Per-stream decode collapses with every extra stream** — M5 62 → 17 tok/s at
  six concurrent, the M3 28 → 12. A conversation that was fast alone becomes four
  times slower because five other conversations share the same host.
- **Aggregate decode barely grows**: the M5 goes from 62 to 99 tok/s with six
  streams (+60 %), the M3 from 28 to 70. Batching buys a little, not a lot; the
  memory bandwidth is the ceiling either way.
- **The M5's advantage is real but not large**: 62 against 28 per stream alone
  (2.2:1), 99 against 70 aggregate at six (1.4:1). The 3:2 weights (1.5:1) sit in
  the right place for decode-dominated work, which is what a long conversation is.
- **The decisive variable is not the weight but the number of long streams on one
  host.** RIPPLE cannot see how long a conversation will run, so it cannot pack
  long sessions onto separate hosts — but the numbers say the question is worth
  asking: with `max_inflight: 6` and six long conversations on the M5, each of them
  gets a quarter of the speed it would have alone.

The 1-concurrent M3 value (27.8 tok/s) is well below the 43.5 tok/s our decode
measurement found with an 8k prompt and 256 completion tokens. Different prompt
length, different output length, and the M3 may have been busy; the ratios above
are therefore more trustworthy than the absolute numbers.

## Decision

| host | max_inflight | spill_threshold | max_threads |
|---|---|---|---|
| alpha (weight 3) | 6 | 4 | 20 |
| beta (weight 2) | 6 | 3 | 20 |

The cost is honest to state: when five or six requests run on the M5 at once, one
of them takes 17–20 s instead of 11 s, and an interactive session on the same GPU
feels that. RIPPLE does not route the interactive session, so this only happens
when subagents burst. If that becomes annoying, the knob is `spill_threshold` on
the M5 — raising it pushes more new threads onto the M3, which is slower but does
not touch the GPU the agent session is using.

## Idle threads must not count as load (14.11.2026)

Observed live: a new conversation almost always started on the M3 although both
hosts reported `inflight 0`. Not a fault in the deficit rule — a fault in what it
counted.

At 15:28:20 the registry held two threads on the M5 (the operator's own agent
session, 12 turns, idle 580 s; an older one with 1 turn, idle 1402 s) and none on
the M3. The rule then computed

```
M5:  0.6 × (2 + 1) − 2 = −0.2        M3:  0.4 × (2 + 1) − 0 = +1.2   → M3
```

Both hosts were idle in the only sense that matters to a user, but a thread keeps
its slot for `ttl_seconds: 1800` and counted at full weight the whole time. Since
the entry node permanently carries the interactive session, it looked
permanently loaded, and the stronger machine (60.2 against 43.5 tok/s in our
decode measurement) got none of the new conversations. Measured share over the
soak window: 53/47 instead of 60/40.

**Fix:** the slot count that enters the deficit is weighted by idleness,
`(1 − idle / 300 s)²`, clamped at 0; a thread with a request in flight always
weighs 1.0. Quadratic on purpose: 30 s of silence still weighs 0.81, 150 s only
0.25, 300 s nothing — a short pause does not rebalance, a stale tail does not
pull. Binding and the hard `max_threads` limit are untouched; the thread stays
where it is and counts fully again on its next request.

The residual risk is stated openly: a "silent" thread could still be doing work
the balancer cannot see (a client that bypasses RIPPLE, or a request that has not
started yet). An in-flight request through RIPPLE is covered — `active > 0` pins
the weight at 1.0.

## Prefill is a different machine (measured 14.11.2026)

The 3:2 weights came from decode measurements (60.2 against 43.5 tok/s =
1.38:1). Prefill was never measured — and it is not the same ratio. Cold prefill,
unique text per request so no prefix cache could answer:

| context | alpha | beta | ratio |
|---|---|---|---|
| 8.4k tokens | 2.10 s (4.011 tok/s) | 15.60 s (541 tok/s) | 7.4 : 1 |
| 24.5k tokens | 6.66 s (3.680 tok/s) | 25.23 s (970 tok/s) | 3.8 : 1 |

The M3 is 4–7× slower at prefill and only 1.4× slower at decode. Consequences:

1. `hosts[].pp_tps` is now configuration, because a rebind has to price its
   prefill (see `placement.rebind`).
2. `tg_tps` in `/healthz` was wrong before: it divided completion tokens by the
   whole request duration, so a long prompt made the M5 look like 15.3 tok/s. It
   is now corrected by `prompt_tokens / pp_tps` and smoothed exponentially
   (`tg_samples`), so it matches the decode measurement.
3. Open: for prompt-heavy agent work (long tool outputs, big contexts) 3:2
   understates the difference. A workload-dependent weight — short contexts to
   the M3, long ones to the M5 — is the obvious next question, not yet decided.
