# Measurements

Two questions, one document. **Part 1** measures what a single node can do:
decode throughput, cold prefill, and what those numbers say about the configured
host weights. **Part 2** measures what the fleet actually did over a multi-day
run: per-host busy time, its hourly shape, and every thread movement.

**Hardware.** Two Macs running oMLX: `M5-alpha` (weight 3) and `M3-beta`
(weight 2). Models measured: `DeepSeek-V4.1-Flash-1017-oQ4e-mtp` (453.6 GB, the
model that carries the threads), `Qwen3.5-9B-oQ4e-mtp` (informative only) and
`Qwen3.8-Flash-Next-oQ4e-mtp` (111.6 GB, M3 only).

**Tools.** `tools/as6_bench.py` for decode throughput, `tools/prefill_bench.py`
for cold prefill, `tools/load_report.py` for the fleet report. The raw JSON of
every benchmark run is kept in `tools/as6/results/`.

**When.** Part 1 was measured 14.–16.11.2026, Part 2 covers the run of
14.11.2026. Both are point-in-time measurements of these two machines, not
guarantees about another one — see [Measuring your own
node](#measuring-your-own-node) at the end.

---

## Part 1 — What a node can do

**First measurement:** 14 November 2026
**Tool:** `tools/as6_bench.py` (raw data in `tools/as6/results/`)
**Question:** do the weights 3 : 2 hold, or does the measurement move them?

The weights are a specification, not a measurement (RB-6). This protocol
measures decode throughput per (host, model) and compares the ratio with the
configured 3 : 2.

### Method

- Identical prompt of ~8 000 tokens per run, one short marker per run so the
  prefix cache cannot make later runs cheaper.
- The prompt asks the model to **repeat the block verbatim**. Asking for a
  single word is not measurable: a well-behaved model answers with one token and
  the decode rate becomes noise. The first attempt did exactly that — the M3
  returned 1 completion token on all ten runs, which produced a meaningless
  "1 000 000 tok/s". Samples with fewer than 64 completion tokens are now marked
  invalid and excluded.
- `temperature: 0`, non-streaming, `total_time − time_to_first_token` from the
  oMLX usage block as the decode window.
- Measured **directly against the hosts**, not through RIPPLE — the point is to
  measure the machines, not the proxy.
- The tool refuses to run against a model that is not already loaded: loading a
  400 GB model costs 60–70 s and can evict a running session (FA-18).

### Result — DeepSeek-V4.1-Flash-1017-oQ4e-mtp (the model the weights are about)

5 runs per host, 8 058 prompt tokens, 256 completion tokens, all samples valid.

| Host | Decode median | min – max | TTFT median |
|---|---|---|---|
| M5-alpha | **60,2 tok/s** | 57,4 – 67,4 | 1,99 s |
| M3-beta | **43,5 tok/s** | 41,8 – 50,9 | 7,45 s |

Ratio **1,38 : 1**, configured **1,50 : 1** → deviation **8 % → weights confirmed**.

The absolute numbers are far below the 200,5 tok/s recorded for the M5 on
25.10.2026 because this measurement runs *while the main agent session is
generating on the M5*. That biases the M5 numbers downwards, i.e. the true idle
ratio is 1,38 or higher — which supports 3 : 2 rather than contradicting it.

### Result — Qwen3.5-9B-oQ4e-mtp (informative only)

10 runs per host, 8 040 prompt tokens, 400 completion tokens, all samples valid.

| Host | Decode median | min – max | TTFT median |
|---|---|---|---|
| M5-alpha | 136,3 tok/s | 95,0 – 152,1 | 1,94 s |
| M3-beta | 444,4 tok/s | 263,2 – 459,8 | 4,97 s |

Ratio 0,31 : 1 — the M3 is more than three times faster on the 9B. That is not a
statement about the machines: on the M5 the 9B shares the GPU with the resident
DeepSeek session, on the M3 it has the machine to itself. For the 9B the weights
are therefore **not** derived from this number.

The 9B is a vision/classification model with short requests (FA-7); it holds no
thread slot and is distributed by the deficit rule over the ephemeral window.
Its share follows the request pattern, not the decode rate.

### Consequence

1. **The weights stay at 3 : 2.** For the model that carries the threads
   (DeepSeek), the measurement confirms them within 8 %.
2. **The 9B numbers are documented, not used.** Anyone re-running this
   measurement must measure with the fleet idle, otherwise the M5 numbers are
   biased by the agent session that produced them.
3. **A deviation would not be a bug.** If a later measurement shows a ratio far
   from 1,5, `hosts[].weight` is a configuration value — no code change, one
   restart (FA-3).

### Reproducing

```bash
export OMLX_API_KEY=$(cat ~/.omlx/api_key.txt)
./.venv/bin/python tools/as6_bench.py --config config.yaml \
    --model DeepSeek-V4.1-Flash-1017-oQ4e-mtp --runs 5 --max-tokens 256
```

Raw JSON per run: `tools/as6/results/as6-<timestamp>.json` (prompt tokens,
TTFT, total time, decode window, tok/s and validity per sample).

**The engine version belongs to the number.** Every figure below was taken
against a specific oMLX build, and `/healthz → hosts[].version` is where that
build is named. RIPPLE re-reads it on every transition into `healthy`, so a node
that was updated reports the new version within one discovery interval instead of
keeping the one it was started with (issue #86). Before quoting a number, compare
that field with the version this section names: a measurement whose engine
version no longer matches the running one is a measurement of a different
engine.

### Re-measurement, 14.11.2026 17:55 — the idle numbers

The 14:00 numbers were taken while the agent session was streaming. This run was
made with the session quiet (the agent sat in a tool call, so no request was in
flight on the M5 during the samples). Same model, same 8k prompt, 400 completion
tokens, 10 runs per host:

| | M5-alpha | M3-beta | ratio |
|---|---|---|---|
| decode median | **55,9 tok/s** | **42,8 tok/s** | **1,31 : 1** |
| decode min / max | 34,6 / 61,8 | 41,1 / 48,4 | |
| TTFT (8 058-token prompt) | 1,99 s | 7,37 s | 1 : 3,7 |

Two M5 samples (34,6 and 43,1) are the agent's own turn; without them the median
is 56,3 — the ratio barely moves (1,32 : 1). So the honest statement is
**1,31 : 1 measured against 1,50 : 1 configured, 13 % deviation**, where the
earlier 1,38 : 1 was the optimistic end of the same measurement. The M3 is stable
to ±1 tok/s across both runs.

### Cold prefill, 14.11.2026 18:02 — `tools/prefill_bench.py`

Decode is only half of the cost model. `placement.rebind` compares
`gain = completion × (1/tg_own − 1/tg_target)` against
`cost = prompt / pp_target`, so the prefill rate per host decides whether a move
pays off. Measured with random-word prompts (no prefix cache can serve them),
`max_tokens: 1`, fixed overhead per host subtracted (0,11 s / 0,14 s), two
repetitions each:

| prompt | M5-alpha | M3-beta | ratio |
|---|---|---|---|
| ~2 000 tokens | 2 541 tok/s | 879 tok/s | 2,89 : 1 |
| ~8 000 tokens | 3 734 tok/s | 1 047 tok/s | 3,57 : 1 |
| ~24 000 tokens | 4 154 tok/s | 1 048 tok/s | 3,96 : 1 |

Three things follow:

1. **`hosts[].pp_tps` is confirmed.** The configuration says M5 3 680 and M3 970;
   measured at 8k and 24k: 3 734 / 4 154 and 1 047 / 1 048. The M3 saturates at
   ~1 050 tok/s, the M5 keeps climbing with size (fixed overhead amortises).
2. **Prefill is far more asymmetric than decode** (3,6–4,0 : 1 against 1,3 : 1).
   A 200k-token context costs the M3 ~191 s of prefill and the M5 ~48 s, while
   the same thread decodes only 1,3× slower on the M3. A placement rule that
   looks only at decode therefore systematically under-charges long contexts on
   the M3 — exactly the open question in §0.2 of the handover.
3. **The 2k ratio is not the 24k ratio.** Any context weight must use the size
   it is judging, not a single average rate.

Reproduce:

```bash
export OMLX_API_KEY=$(cat ~/.omlx/api_key.txt)
./.venv/bin/python tools/prefill_bench.py --config config.yaml \
    --model DeepSeek-V4.1-Flash-1017-oQ4e-mtp --sizes 2000 8000 24000 --reps 2
```

Raw JSON: `tools/as6/results/prefill-<timestamp>.json`.

### Consequence for choose() — the prefill bias (implemented 14.11.2026)

The deficit rule counted conversations, not the cost of starting one. A new
thread with a long context was placed by slots alone, although the same prompt
costs the M3 3,6–4,0× the prefill time of the M5. The rule now charges the extra
cold prefill to the host that would need it, in the currency the deficit already
speaks — slots:

```
t_h     = prompt_tokens / pp_tps_h              (only hosts with pp_tps > 0)
fastest = min(t_h) over the candidates compared
bias_h  = −min(max_slots, max(0, t_h − fastest) / slot_seconds)
score_h = deficit_h + bias_h
```

Only the fastest candidate keeps 0,0; every other host loses the extra prefill
seconds it would need, converted into slots. The bias is never positive (nobody
is rewarded for being fast), is not applied when fewer than two candidates have
a `pp_tps` (a single known rate has nothing to be relative to), is 0,0 for a
host without `pp_tps` (unknown is not fast), and it is compared over the
candidates actually under consideration at that point in `choose()` — not over
the whole fleet. It is added in the deficit branch only: `consistent_hash` and
`least_threads` do not weigh a deficit at all.

| parameter | default | meaning |
|---|---|---|
| `placement.prefill_bias.enabled` | `true` | off = exactly the rule before this change |
| `placement.prefill_bias.slot_seconds` | `60.0` | 1 slot == 1 minute of extra prefill (smaller = stricter) |
| `placement.prefill_bias.max_slots` | `3.0` | cap, so a 1M-token prompt cannot dominate |
| `placement.prefill_bias.min_prompt_tokens` | `4096` | below this: zero bias, behaviour as before |

Bias for the M3 against the M5, from the measured rates above (8k: 3 734 /
1 047 tok/s, from 24k on: 4 154 / 1 048):

| prompt tokens | M5 prefill | M3 prefill | bias M3 |
|---|---|---|---|
| 8 000 | 2,1 s | 7,6 s | **−0,09** |
| 24 000 | 5,8 s | 22,9 s | **−0,29** |
| 60 000 | 14,4 s | 57,3 s | **−0,71** |
| 100 000 | 24,1 s | 95,4 s | **−1,19** |
| 200 000 | 48,1 s | 190,8 s | **−2,38** |
| 1 000 000 | 240,7 s | 954,2 s | −3,00 (cap) |

The rounded `hosts[].pp_tps` from `config.yaml` (3 680 / 970) gives marginally
larger penalties: −0,10 / −0,30 / −0,76 / −1,27 / −2,53.

What it deliberately does **not** do: no decode term (the 1,31 : 1 is already in
the 3 : 2 weights), no KV-memory model, no influence on rebind (that has its own
seconds-based cost model), no effect on hosts without `pp_tps`, and nothing at
all below 4 096 tokens. Slot counting, `max_threads` and eviction are untouched —
the bias lives in the score of one decision, nowhere else. Observable as
`Decision.bias`, as a `prefill bias <host> <value>` note on the decision, and as
the `prefill_bias` counter in `/stats`; tests in `tests/test_prefill_bias.py`.

### Re-measurement, 15.11.2026 10:59 — after the loading-state work

Three runs of the same protocol (10 runs, ~8 058 prompt tokens, 400 completion
tokens, direct against the hosts, `--host` to keep one machine per run):

| host | run | decode tok/s | min–max | ttft |
|---|---|---|---|---|
| M5-alpha | 1 | **56,6** | 51,9–61,7 | 1,995 s |
| M5-alpha | 2 | **53,5** | 49,7–57,7 | 2,085 s |
| M3-beta | 1 | **45,2** | 44,0–50,8 | 7,275 s |

**Ratio M5 : M3 = 1,22 : 1** (55,05 / 45,2), against 1,38 : 1 measured on 14.11.
and 1,50 : 1 configured. The prefill ratio is unchanged and healthy: 7,275 / 2,04
= **3,57 : 1**, exactly what `pp_tps` 3 680 / 970 says.

Two caveats, both of which point the same way — these numbers are a floor for the
M5, not a verdict on the weights:

1. **M3's DeepSeek had been loaded 3 minutes earlier** (the fleet was restored
   after the cold-load tests), so its page cache was cold for the first runs. Its
   spread (44,0–50,8) is narrower than the M5's, which is the wrong direction for
   a cold start — the load itself had already read the file.
2. **The M5 was not quiet.** The operator's agent session runs through `:8010`
   against the M5's DeepSeek; it was idle during the runs (no requests in flight),
   but the model was resident alongside Qwen3.5-9B and MarkItDown, and the
   previous 40 minutes had seen several 100+ GB loads. 56,6 vs 60,2 on 14.11. is a
   6 % drop with a 10 % spread between two consecutive runs of the same host —
   that is measurement noise plus thermal state, not a new capability.

Consequence: the 3 : 2 weights are **not** re-derived from this. A run that could
justify them needs the fleet quiet (no agent session, both models warm, ten
minutes after the last load), which is an evening job like the M5 calibration.
What this run does confirm: the M5's decode advantage is real but smaller than
3 : 2 claims, and the prefill advantage (3,6 : 1) is untouched — the reason the
prefill bias exists.

#### Long-context variant, 15.11.2026 11:30 — 32k prompt, 1 600 completion tokens

Same tool, longer runs, because the operator's real traffic is a long conversation
and not a 400-token answer:

| host | decode tok/s | min–max | ttft | prompt |
|---|---|---|---|---|
| M5-alpha | **58,0** | 54,5–65,1 | 7,055 s | 32 024 |

Valid 10/10 — the window takes 32k in plus 1 600 out without truncation. The
decode rate is *higher* than at 8k (56,6 / 53,5), which is the expected shape: a
longer decode is a longer measurement, and the first tokens after a 32k prefill
are not the average. The prefill rate is 32 024 / 7,055 = **4 539 tok/s**, above
the configured `pp_tps` 3 680 — the configured value stays, because it is the
conservative side of the prefill bias.

M3-beta, same settings, ten minutes later:

| host | decode tok/s (median) | min–max | ttft | prompt |
|---|---|---|---|---|
| M3-beta | **44,8** (clean tail: 45,4) | 25,8–56,9 | 28,955 s | 32 024 |

The first three runs are visibly disturbed — ttft 45,7–50,6 s against 28,9 s for the
rest, decode 25,8–29,6 against 42,3–56,9. That was **not** the machine: the
operator's agent session had been routed to the M3 during the M5 calibration and
was still there, so three of my own requests ran inside the benchmark. Runs 4–10
are the clean measurement; the tool's median already sits at 44,8, and the clean
tail is 45,4.

#### What this says about the 3 : 2 weights

| | M5 | M3 | ratio |
|---|---|---|---|
| decode, 8k/400 | 56,6 | 45,2 | 1,25 |
| decode, 32k/1600 | 58,0 | 45,4 | **1,28** |
| prefill, 32k | 4 542 tok/s | 1 108 tok/s | **4,10** |
| wall clock per request, 8k/400 | 9,1 s | 16,1 s | 1,78 |
| wall clock per request, 32k/1600 | 34,6 s | 64,1 s | **1,85** |

The decode ratio is 1,25–1,28, below the configured 1,50 — but decode is not what
the requester waits for. Adding prefill, the M5 finishes the same request **1,8×
faster**, which is *more* than 3 : 2 claims. The weights are therefore not
generous to the M5; if anything they are generous to the M3, because the 4,1 : 1
prefill advantage dominates every prompt above a few thousand tokens and the
weights only see a 1,5 : 1 claim.

No change to `config.yaml`. The prefill bias already handles the 4,1 : 1 separately
and per request, which is the more precise instrument; raising the weights on top
of it would double-count the same advantage.

---

### Re-measurement, 16.11.2026 01:48 — Qwen3.8 on the M3 (issue #7)

Until now only DeepSeek had a measured basis. Qwen3.8 runs **only** on the M3 (the
M5's allow-list forbids it), so the 3 : 2 weights were a DeepSeek statement applied
to a model nobody had measured.

Fleet quiet: `:8010` idle, no other request on either node during the runs. The M3
was emptied first (DeepSeek unloaded, 453 GB) and Qwen3.8 loaded fresh
(111,6 GB), so the measurement ran against a model that had not served anything.
The tool talks to the node directly (`http://10.11.12.9:8000`), not through RIPPLE.

#### 8k / 400

| host | decode tok/s (median) | min–max | ttft (median) | prompt tokens | valid |
|---|---|---|---|---|---|
| M3-beta | **87,2** | 81,1–93,2 | 3,095 s | 8 080 | 10/10 |

Raw data: `tools/as6/results/as6-20261116-020240.json`.

The **first** run after the load reported 92,0 tok/s (82,8–98,8) with ttft 6,055 s
(`as6-20261116-014809.json`). The difference is not decode noise — it is the
engine's one-time warmup: all ten runs of that first batch carried ttft ≈ 6,05 s,
all ten of the second batch ≈ 3,10 s. Decode is measured from first token to last
and is unaffected, which is why the two batches agree on it and disagree on ttft.

#### 32k / 1600

| host | decode tok/s (median) | min–max | ttft (median) | prompt tokens | valid |
|---|---|---|---|---|---|
| M3-beta | **76,5** | 72,4–85,5 | 3,235 s | 32 046 | 10/10 |

Raw data: `tools/as6/results/as6-20261116-015936.json`.

**What the ttft column is and is not.** 32 046 prompt tokens cost 0,14 s more than
8 080 (3,235 against 3,095). A per-token prefill rate cannot be derived from that,
and I am not going to derive one: the tool sends the *same* prompt ten times, so
the prefix is cached after the first request, and what the median measures is a
cache hit plus a ~3 s per-request fixed cost. The prefill numbers in the earlier
sections of this document come from `tools/prefill_bench.py`, which varies the
prompt; they are not comparable with these. For the weights — decode throughput —
that does not matter, and decode is what the weights are about.

#### What this says about the weights

| | M3, DeepSeek | M3, Qwen3.8 | factor |
|---|---|---|---|
| decode, 8k/400 | 45,2 | 87,2 | **1,93** |
| decode, 32k/1600 | 45,4 | 76,5 | **1,69** |
| ttft, 32k (warm) | 28,955 s | 3,235 s | 8,95 |

Qwen3.8 decodes roughly **twice as fast** as DeepSeek on the same machine, for a
quarter of the weight footprint (111,6 GB against 453,6 GB) — a much smaller
active-parameter count, as the name says.

**No change to `config.yaml`, and the reason is structural, not numerical.** The
weights are one number per host. They arbitrate only when **two or more** hosts may
serve the same model. Qwen3.8 may be served by exactly one, so its placement is
decided by the allow-list before any weight is read — the `2.0` on the M3 is inert
for this model. "Does the M3 need a different weight for Qwen3.8?" therefore has no
answer that could change a decision, which is the honest form of "no change needed".

Two consequences worth keeping:

1. **The M3 is not the slow node for this model.** 87 tok/s is faster than anything
   measured on either node for DeepSeek. If Qwen3.8 is ever meant to carry real
   traffic, the lever is `hosts[].models` (open it to the M5), not the weights.
2. **If it is opened to both hosts, measure the M5 first.** The 1,25–1,28 decode
   ratio and the 4,1 : 1 prefill ratio are statements about a 453 GB model. A model
   that decodes 2× faster at a quarter of the size has a different compute and
   memory profile, so neither ratio may be transferred to it. The 3 : 2 weights
   would then be an untested assumption again — which is exactly the situation #7
   was opened to end.

---

## Part 2 — What the fleet did

How evenly are the two Macs loaded?

`tools/load_report.py` is a read-only analyser. It reads three logs and answers
three questions:

1. **Per host** — how many requests, how many seconds of prefill, how many of
   decode, how much of the window was the host busy at all, how much was idle.
2. **Over the day** — busy seconds per hour and per host, side by side, so
   "evenly loaded" is visible instead of inferred from a request count.
3. **Movements** — every rebind with from/to/gain/cost, the per-direction
   counts, the promotions from batch to interactive, and what RIPPLE itself saw
   (requests and mean duration per host).

It writes nothing but stdout. No network beyond one `ssh` per remote host, no
API call, no state.

```
./.venv/bin/python tools/load_report.py                    # last 24 h
./.venv/bin/python tools/load_report.py --since 7d
./.venv/bin/python tools/load_report.py --since 2026-11-14T13:00 --hosts M5-alpha
./.venv/bin/python tools/load_report.py --no-m3            # offline: local host only
./.venv/bin/python tools/load_report.py --json
```

`--since` takes `24h`, `48h`, `7d` (also `2w`) or an ISO timestamp; the default
is 24 h. `--config` (default `config.yaml`) supplies the host names, the
addresses that decide local vs remote, and `log_file`. `--ripple-log` overrides
the RIPPLE log path. Exit codes: **0** = something was parsed, **3** = nothing
could be parsed, **2** = the configuration or an argument is unusable.

### The line it parses

oMLX writes exactly one INFO line per completed request into
`~/.omlx/logs/server.log` (rotated daily as `server.log.YYYY-MM-DD`), on each
host:

```
2026-11-14 19:49:17,313 - omlx.server - INFO - [-] - Chat completion: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp, 15722 tokens in 102.81s (158.1 tok/s), prompt: 42644, finish_reason=stop, max_tokens=102400, request_max_tokens=None, stream_model_ttft=3.35s
```

From it: the **completion tokens** (15722), the **total seconds** (102.81), the
**prompt tokens** (42644) and **`stream_model_ttft`** (3.35).

* The timestamp is the **end** of the request, and the line has no request id.
  The interval is therefore `[end - total, end]`.
* **TTFT is wall time.** The prompt is read and the first token arrives after
  it, so prefill = Σ TTFT and decode = Σ (total − TTFT). oMLX's own tok/s in the
  parentheses is computed exactly that way: 15722 / (102.81 − 3.35) = 158.1. The
  report reproduces that as `TG tok/s` = Σ completion / Σ decode, which is why
  it lands on the measured capability (M5 ≈ 60, M3 ≈ 43 tok/s) instead of
  something inflated.
* **The prompt-token column is labelled nominal on purpose.** The prefix cache
  makes a prefill rate from it meaningless: 98.8 M prompt tokens for 797
  requests is not 98.8 M tokens of work, it is mostly cache hits. The column is
  there to show the shape of the traffic, never to divide by.
* The `(… tok/s …)` parentheses are skipped, not parsed: the diffusion models
  write extra numbers in there (`180.2 tok/s e2e, output=…, canvas=…`), and the
  figure is decode-only anyway.
* A line without `stream_model_ttft` (a non-streamed request) is still counted,
  with prefill 0 — there is no prefill measurement to add.
* The DEBUG line `Chat completion request received:` sits next to every one of
  these and is **not** matched.

### Busy is the union, idle is its complement

Requests overlap. Two conversations decoding at the same time occupy the host
once, not twice, so busy time is the measure of the **union** of the intervals
`[end − total, end]`, clipped to the window. Summing durations would double
count parallel requests and produce a busy share above 100 %.

`PP s` and `TG s` are per-request sums and can add up to more than `busy s` for
the same reason. That is intended: they answer "how much prefill work, how much
decode work", not "how long was the host occupied".

Idle is the complement: **`idle = window − busy`**, never
`window − (PP + TG)`. With a 24 h window the two add up to 100 %.

The hourly profile buckets the same union per clock hour, so a row cannot exceed
3600 s, and the sum of the rows equals `busy s` of the window.

### The M3 over SSH

The second Mac's log is not copied. `ssh -o BatchMode=yes -o ConnectTimeout=8
10.11.12.9 '<command>'` runs a `grep` on that machine and only the matching
lines come back — a 40 MB log stays a 40 MB log on the M3, and the report
prints the files it actually read (`READ` lines on the remote side's stderr).

Caveats:

* **BatchMode, no password.** If the key is not in place, the host is
  unreachable, not interactive.
* **An unreachable host is not an error.** The M5 part still prints, the M3 is
  marked `[unreachable: ssh exit 255: …]`, and the report says in one line that
  it is then *not* a fleet total. `--no-m3` does the same deliberately, for
  offline work.
* Which host is local is decided from the configuration: a host is local when
  one of its `addresses` is an address of this machine (loopback or one of its
  interface IPs). Everything else goes over SSH.
* Only the log files of the days the window touches are greped; a rotation that
  does not exist is skipped silently (the `exit 0` at the end of the remote
  command keeps a missing file from looking like a dead host).

### Duplicate lines in the RIPPLE log

Until the logging fix, launchd redirected stderr into the same file that
`log_file` named, so two handlers wrote every record twice (1476
`ripple.request:` lines for 740 requests). Rotated logs still contain those
pairs. The report drops byte-identical lines before counting and prints how many
it dropped, so the numbers stay honest for old logs and the counter goes to zero
for a clean one.

### Example

```
$ ./.venv/bin/python tools/load_report.py --since 6h
RIPPLE load report
window   2026-11-14 14:19 .. 2026-11-14 20:19  (6.0 h, local time)
sources
  M5-alpha     /Users/vogel/.omlx/logs/server.log
  M3-beta      10.11.12.9:/Users/vogel/.omlx/logs/server.log
  RIPPLE log   /Users/vogel/Library/Logs/ripple.log  (1270 lines, 1207 duplicates dropped)

per host (oMLX log)
  host           req     PP s     TG s   busy s  busy%  idle%  mean TTFT  TG tok/s    prompt tok  compl tok
  ---------------------------------------------------------------------------------------------------------
  M5-alpha       748    1,134   10,690   10,032  46.4%  53.6%      1.52s      50.2    81,138,615    536,863
  M3-beta        272    1,783    4,960    5,384  24.9%  75.1%      6.55s      32.2    16,396,697    159,862
  prompt tok is nominal (prefix-cache inflated) — not a prefill rate.
  PP and TG are per-request sums; concurrent requests overlap, so they
  can add up to more than busy. busy is the union of [end-total, end].
  M5-alpha: prefill 1,134s (5.2% of window), decode 10,690s (49.5%), idle 53.6%
  M3-beta: prefill 1,783s (8.3% of window), decode 4,960s (23.0%), idle 75.1%

hourly busy seconds (union per host, one row per clock hour)
  hour  M5-alpha                               M3-beta
  14:00  ###################             1,112s ###                               186s
  15:00  ########################        1,404s #################                 986s
  16:00  #############################   1,712s ###########                       632s
  17:00  #############################   1,723s #####                             312s
  18:00  ######################          1,293s ##############                    838s
  19:00  ##############################  1,788s ############################    1,683s
  20:00  #################                 999s #############                     747s
  2026-11-14 total                                  10,032s                                  5,384s
  bar: 30 chars = 1,788s

thread movements (RIPPLE log)
  18:28:04  26311548f7805f9c   M3-beta -> M5-alpha  gain 2.6s  cost 0.8s
  19:41:17  26311548f7805f9c   M3-beta -> M5-alpha  gain 2.6s  cost 0.6s
  direction M3-beta -> M5-alpha: 2
  rebinds 2, promotions (batch->interactive) 0
  RIPPLE requests per host (deduplicated):
    M3-beta        230 requests   2,628.2 ms mean
    M5-alpha       604 requests     581.7 ms mean
    unrouted         5 requests       0.2 ms mean

idle = window - busy. TTFT is wall time (prefill); decode = total - TTFT.
```

How to read that block: over these six hours the M5 was busy 46 % of the time
and the M3 25 %, and the hourly rows show *when* — the M3 carried real load in
15:00 and 19:00 and nothing in the morning. `TG tok/s` (50.2 / 32.2) sits below
the measured idle capability (60.2 / 43.5) because both hosts were working;
that is the expected direction, not a regression. The M3's mean TTFT (6.55 s) is
4× the M5's (1.52 s) — long prompts on the weaker prefill, visible per request.

### Tests

`tests/test_load_report.py` — verbatim log lines as fixtures, no network and no
ssh: union of overlapping intervals, clipping to the window, dedupe of
duplicated lines, per-host aggregation, hourly bucketing across an hour
boundary, `--since` filtering and parsing, the unreachable-host path (M5 still
printed, exit 0), the `--json` shape and exit code 3 when nothing parses.

---

## Measuring your own node

`tools/as6_bench.py` is the tool every decode number in Part 1 comes from, and it
runs against any oMLX host that RIPPLE's configuration knows about — the
operator's own fleet or a single machine.

### The call

```bash
export OMLX_API_KEY=$(cat ~/.omlx/api_key.txt)
./.venv/bin/python tools/as6_bench.py --config config.yaml \
    --model DeepSeek-V4.1-Flash-1017-oQ4e-mtp --runs 10 --max-tokens 256
```

Every flag has a default, so `./.venv/bin/python tools/as6_bench.py` alone
measures `Qwen3.5-9B-oQ4e-mtp` with 10 runs of ~8 000 prompt tokens and 256
completion tokens against all enabled oMLX hosts:

| flag | default | what it does |
|---|---|---|
| `-c`, `--config` | `config.yaml` | host names, addresses, weights and API keys come from here |
| `--model` | `Qwen3.5-9B-oQ4e-mtp` | the model to measure; it must already be loaded on every selected host |
| `--runs` | `10` | requests per host |
| `--prompt-tokens` | `8000` | approximate prompt size per run |
| `--max-tokens` | `256` | completion tokens per run |
| `--host` | all oMLX hosts | limit to one host name (repeatable, so one machine at a time) |
| `--dry-run` | off | one tiny request per host (200 prompt / 16 completion tokens) to check the wiring |

The tool talks to each host's own API (`hosts[].addresses[0]`), not through
RIPPLE — the proxy is not what is being measured. It refuses to load a model: if
the model is not offered or not resident, that host is reported as an error and
skipped, because a cold load costs 60–70 s and can evict a running session
(FA-18). Exit code is **0** if at least one host produced a number, **1** if none
did, **2** if no oMLX host was selected.

### What it prints

One block per host:

- the address it talked to and that host's configured `weight`;
- `decode: <median> tok/s (min <min>, max <max>)`, the median over the **valid**
  samples only;
- the median TTFT in seconds, the measured prompt size in tokens, and
  `valid <n>/<m>` — how many samples counted;
- `estimated_size`, the model size the host reports, in bytes.

Then, if exactly two hosts produced numbers, the comparison that the weights are
about: the measured ratio, the configured weight ratio, the deviation in percent
and a verdict — `confirmed` at 15 % or less, otherwise `DEVIATES — document it`.

Finally the path of the raw JSON, `tools/as6/results/as6-<timestamp>.json`. It
holds every sample with prompt tokens, TTFT, total time, the decode window,
tok/s, wall time and the validity flag, plus the per-host medians — so a number
in this document can be traced back to the runs that produced it.

### How many runs for a number you can quote

Ten per host is the protocol, and less is not worth reporting:

- A single sample is dominated by whatever else the machine was doing. Two
  consecutive batches on the same host differed by 10 % (56,6 and 53,5 tok/s);
  the same host measured 60,2 tok/s on one day and 55,9 on the next with a
  resident session.
- A sample only counts if it produced at least 64 completion tokens **and** a
  decode window of at least 0,2 s. Below that the rate is arithmetic on noise —
  the first version of this measurement asked for one word and got 1 completion
  token, i.e. a meaningless "1 000 000 tok/s". If `valid n/m` is not `m/m`, find
  out why before quoting the median.
- The median, not the mean: a single disturbed request (a competing session, a
  page-cache miss after a fresh load) moves a mean and not a median.
- For the ratio between two hosts, measure them one after the other in the same
  session, not hours apart — thermal state and what is resident change the
  number by several percent.

### What the number does not say

- **It was measured on a quiet node.** No other request was in flight, no agent
  session was generating, the model was already resident. Under load the same
  host reports less: 55,9 instead of 60,2 tok/s, and `TG tok/s` in the fleet
  report (Part 2) sits at 50,2 / 32,2 because both hosts were working. A decode
  rate from this tool is a ceiling for that machine in that state, not an average
  over a day.
- **It is decode only.** Prefill, queueing and wall clock per request are not in
  it. Wall clock is what a requester feels, and on these two hosts it favoured
  the M5 by 1,8× while decode favoured it by only 1,25–1,28×.
- **Its TTFT column is not a prefill rate.** The tool sends the *same* prompt on
  every run, so from the second run on the prefix is cached and the TTFT median
  measures a cache hit plus a fixed per-request cost. For a prefill rate use
  `tools/prefill_bench.py`, which varies the prompt.
- **It says nothing about the fleet.** One host, one request at a time, no
  concurrency, no rebind, no eviction. What the fleet did is Part 2, and the two
  numbers differ for the reasons listed above.
- **It does not justify a weight by itself.** The weights arbitrate only between
  two or more hosts that may serve the same model; if exactly one host may serve
  it, no weight is read and no measurement of the second host can change the
  placement.
- **It is one machine on one day.** A different model has a different compute and
  memory profile — the 1,25–1,28 decode and 4,1 : 1 prefill ratios are statements
  about a 453 GB model and may not be transferred to a 111 GB one.
