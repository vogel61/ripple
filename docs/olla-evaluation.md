# Olla evaluation against two oMLX instances

**Date:** 14 November 2026
**Test object:** `thushan/olla` v0.1.0 and v0.0.29 (Apache-2.0, Go)
**Setup:** Olla as a local process (127.0.0.1) in front of M5-alpha (`localhost:8000`) and
M3-beta (`192.0.2.12:8000`), both with `type: omlx`.
**Question:** Does Olla meet the requirements specification — in particular the weighted distribution 3:2?

---

## 1. Verdict

**Olla is well crafted, but it does not meet the core requirement.**

The required distribution 3:2 (FA-2, deficit rule) is **not representable** with Olla.
There is no weight field, and the three existing balancers know only two states:
*everything on one endpoint* or *evenly distributed*. The intermediate step is missing
by construction, not out of configuration ignorance.

Second, Olla routes by **availability**, not by **residency**. A request for
a model that is not loaded is sent without hesitation to an endpoint whose
memory thereby evicts another, running model. That happened unintentionally and
measurably during the tests (section 5).

**Recommendation:** in-house build as specified in the requirements specification. From Olla we adopt
four things that are well solved there (section 8).

---

## 2. Procedure

All measurements against a local Olla process on its own ports. No change to
the oMLX installations, no model switches on M5 (the own session was running there),
no system installation — Olla ran as an unpacked release binary in the project directory.

Test models exclusively the models resident on both hosts (DeepSeek-V4.1,
Qwen3.5-9B), `max_tokens` small, in order not to provoke any model switches.

Reproduction: five small scripts — `battery.py` (the test battery: one JSON per
case), `route_test.py` (N requests, counted per endpoint), `cold_test.py` (a model
that is not resident anywhere), `details.py` (residency knowledge, auth
pass-through, stats endpoints), `overhead.py` (direct oMLX against the same request
through Olla) — plus the raw JSON they wrote. They lived in the project directory
and are **not** part of this repository: they drive a third-party binary that is
not shipped here, so without it they would be dead code. The figures, the
configurations and the verdict are in this report; section 10 says what a rebuild
needs.

---

## 3. Blocker: the release artefact is incomplete

Both release zips (`olla_v0.1.0_macos_arm64.zip`, `olla_v0.0.29_macos_arm64.zip`)
contain **no `config/profiles/` directory**. Start therefore fails:

```
failed to load endpoints from config: invalid endpoint config for "M5-alpha":
unsupported endpoint type: omlx, supported types: [llamacpp lm-studio ollama auto]
```

The documentation says "Since Olla v0.0.28" — the profile exists in the source code
(`config/profiles/omlx.yaml`) but is not shipped. After pulling the
twelve profile files from the tag it runs immediately:

```
Profile factory created successfully, profiles: [docker-model-runner, lemonade,
litellm, llamacpp, lm-studio, lmdeploy, ollama, omlx, sglang, vllm, vllm-mlx]
Registering provider routes from profiles, count=12
```

**Additional finding:** `olla --validate-config` reports `Result: PASS - all configuration files are valid`
for exactly this configuration. The tool does not check
endpoint types. A green validation result is therefore no
promise of a successful start — for a production environment a serious defect.

---

## 4. Core finding: 3:2 does not exist

### 4.1 No weight field

`weight: 3` / `weight: 2` on the endpoints is not read:

```
Ignored keys in config (2) - not read by Olla, no effect on the result:
         - discovery.static.endpoints[0].weight
         - discovery.static.endpoints[1].weight
```

### 4.2 Three balancers, two results

The binary symbols name exactly three selectors: `LeastConnectionsSelector`,
`PrioritySelector`, `RoundRobinSelector`. Measured (DeepSeek, 1 token per request):

| Balancer | Priorities | Requests | Distribution |
|---|---|---|---|
| `least-connections` | 100 / 100 | 20 | **9 / 11** |
| `least-connections` | 100 / 100 | 4 parallel | **2 / 2** |
| `least-connections` | 100 / 100 | 8 parallel | **4 / 4** |
| `least-connections` | 100 / **50** | 10 | **5 / 5** |
| `priority` | **3 / 2** | 40 | **40 / 0** |
| `priority` | 100 / 50 | 10 | **10 / 0** |
| `round-robin` | 100 / 100 | 10 | 6 / 4 |

Three conclusions:

1. **`least-connections` ignores `priority` completely.** 100 against 50 still
   yields 5/5. In the default configuration the priority is pure decoration.
2. **`priority` is a hard stage, not a weight.** 3 against 2 yields 40/0, not
   24/16. The priority decides *who is served at all*, not *how often*.
3. That leaves only **100/0** and **~50/50**. For 3:2 the balancer would have to know the
   fraction — it does not know it.

The documentation speaks of "weighted random selection within the same priority".
Whatever is weighted there (assumption: internal metrics such as model count
or latency), it is **not configurable** and in no measurement produces a
deviation from 50/50.

### 4.3 Also in v0.1.0

Counter-check with the newer v0.1.0 (same priorities): **3 / 7** with 10 requests.
Same picture, no weighting.

---

## 5. Residency: Olla routes by availability, not by loading

The oMLX profile knows the endpoint `/v1/models/status` — but it is only
passed through (HTTP 200, 4911 bytes), **not used as a routing criterion**.
The routing argument is called `model_found` in every response header: the model is
in the model list, so it is routed.

The test that makes this expensive: a request for `Qwen3.6-35B-A3B-oQ4e-mtp`
(21,13 GB, only on M5, not loaded) was correctly routed to M5 and after
16,5 s cold start served with HTTP 200. The M5 log says about it:

```
10:49:20  Unloaded model: DeepSeek-V4.1-Flash-1017-oQ4e-mtp,
          freed=403.65GB (expected>=401.36GB), active_memory: 3.73MB
10:49:31  Evicting 'Qwen3.6-35B-A3B-oQ4e-mtp' to fit 'DeepSeek-V4.1'
          under the admission soft target (464.75GB > 457.64GB)
```

A single test request evicted the 403 GB model — **the model of the
running agent session**. oMLX loaded it again by itself on the next access, the
session survived it. But exactly that is the state that
FA-18 (eviction protection) is supposed to prevent, and Olla neither prevented it nor
reported it: the client saw HTTP 200.

The reverse case, by contrast, works cleanly: a model that exists on only one
host is routed exclusively there (model filter).

---

## 6. What works

| Function | Result |
|---|---|
| Sticky per header `X-Olla-Session-ID` | 5/5 same endpoint, `key_source: session_header` |
| Sticky per prefix hash (without header) | 5/5 same endpoint, `key_source: prefix_hash` |
| Model unification | 22 models across 2 endpoints (M5: 23, M3: 6) |
| Model filter (only one host has it) | 100 % to this host, cold start 16,5 s, HTTP 200 |
| Failover (endpoint dead) | endpoint `offline`, 10/10 to the healthy one, 0 errors |
| Streaming | 8 SSE chunks, 905 ms (`write_timeout: 0s` needed) |
| Sticky statistics | 35 sessions, 8 hits, 0 evictions |
| Dashboard | 3 tabs (Overview, Endpoints, Models), runs offline without CDN |

The sticky mechanism is the strongest feature and covers FA-6 in substance
to a large extent — with one restriction: the `prefix_hash` runs over the first
512 bytes of the `messages` field and does **not contain the model**. Two threads with
the same prompt but a different model land on the same endpoint.

---

## 7. Overhead and client adaptation

**Overhead on the chat path** (DeepSeek, 8 tokens each, n=6, median):

```
direct   : 226,5 ms
via Olla : 271,9 ms
overhead : +45,4 ms (+20,0 %)
```

Clearly more than the documented 0,3 ms — noticeable with short requests, with
long streaming responses negligible.

**Path prefix:** Olla does not serve `/v1/...` but `/olla/omlx/v1/...`. Every
client must be adapted (`base_url: http://host:40114/olla/omlx/v1`). No
drop-in replacement.

**Auth:** With `auth.token_file` set on the endpoint, Olla passes the key through
itself — a client without a key received HTTP 200. Convenient, but Olla is therefore
no auth gateway (NZ-6 confirmed): whoever reaches the port reaches the models.
Loopback binding is mandatory.

**Arbitrary paths are passed through:** `/olla/omlx/admin/api/models` answers
with 200. A balancer of our own could therefore query the residency via Olla — only
Olla itself does not use it.

---

## 8. Comparison with the requirements specification

| Requirement | Status with Olla |
|---|---|
| FA-2 distribution 3:2 (deficit rule) | **not satisfiable** |
| FA-5 capacity / spillover | partially: `max_concurrent_requests: 4` per endpoint, no spillover |
| FA-6 thread detection | partially: sticky yes, model not in the hash, no thread concept |
| FA-18 eviction protection | **not present** (empirically confirmed, section 5) |
| FA-19 slots per model | no: counting per endpoint, not per model |
| FA-21 host identity via `node_id` | no: identification only via URL |
| FA-22 dashboard with active models | partially: host column yes, residency no |
| AS-10 no credentials in logs | fulfilled |
| NZ-6 no auth gateway | fulfilled (key is passed through) |

**On the dashboard (FA-22):** The Models tab shows `MODEL | PARAMS | QUANT | SIZE |
ENDPOINTS | LAST SEEN`. The host column is present (`M3-beta / M5-alpha`), but
`PARAMS`, `QUANT` and `SIZE` are consistently set to `—`, and there is **no
display of which models are currently loaded**. Exactly that is the "Active Models"
section of the oMLX dashboard that should be rebuilt. Olla shows
availability, not residency.

---

## 9. Recommendation

1. **In-house build remains right.** The core of Richard's model — threads stay where
   they are, new threads are distributed by deficit until 3:2 is reached — is
   not representable in Olla. Not a configuration problem, a design gap.
2. **Adopt from Olla:**
   - sticky key sources `session_header` → `prefix_hash` as a fallback chain
   - header names `X-Olla-*` as a model for traceability in operation
     (`X-Olla-Endpoint`, `X-Olla-Routing-Reason`, `X-Olla-Sticky-Key-Source`)
   - dashboard layout: three tabs, endpoint table with latency/requests/status
   - model filter via the model list of the endpoints — works and is cheap
3. **Olla as an interim solution** is usable for failover and model filter, but
   without 3:2 and without eviction protection. If a proxy is to run in the
   transition period: only with loopback binding and deliberately without weighting.
4. **Upstream PR** would be conceivable (weight field + residency criterion), but the
   author has explicitly ruled out lifecycle control. To put two features of this
   size against a project's design decision is not a
   reliable plan.
5. **Before every use of Olla:** pull `config/profiles/` from the tag and
   verify the start — `--validate-config` is not enough.

---

## 10. Appendix: measurement environment

- Olla v0.1.0 and v0.0.29, macOS arm64, ports 40114–40121, loopback
- M5-alpha: Mac Studio M5 Ultra, 512 GB, `localhost:8000`, ceiling 487,7 GB
- M3-beta: M3 Ultra, 512 GB, `192.0.2.12:8000`, ceiling 491,3 GB
- Residency during the measurements: DeepSeek-V4.1 + Qwen3.5-9B + MarkItDown on both
- Initial state restored after the tests (M5: DeepSeek + 9B + MarkItDown,
  M3: unchanged), all test processes terminated

**Files created outside the repo:**

- `~/.omlx/api_key.txt` (mode 600) — copy of the oMLX API key so that Olla can read it via
  `auth.token_file` without it standing in a project file.
  Deletable when Olla is no longer operated.
- A working directory outside the repo, with both Olla binaries
  (v0.1.0 and v0.0.29), the twelve profile files and the five measurement scripts.
  Nothing of it is in the repository: the scripts only make sense next to those
  binaries, and the raw JSON they wrote is summarised in the sections above.
  Deletable — this report is the record.

**Reproduction in short form** (a rebuilt setup, not a script in the repo):

```bash
work=~/Projects/olla && mkdir -p $work && cd $work
curl -sL -o olla.zip https://github.com/thushan/olla/releases/download/v0.1.0/olla_v0.1.0_macos_arm64.zip
unzip -q olla.zip -d bin
# IMPORTANT: profiles are missing from the release and must be pulled from the tag
mkdir -p config/profiles
for p in llamacpp lmstudio ollama omlx sglang vllm vllm-mlx litellm lmdeploy \
         lemonade dmr openai-compatible; do
  curl -sfL -o config/profiles/$p.yaml \
    "https://raw.githubusercontent.com/thushan/olla/v0.1.0/config/profiles/$p.yaml"
done
./bin/olla -c config.yaml     # the two-endpoint setup of section 2
```

Then one request loop per question: which endpoint takes N requests (distribution),
whether a model that is resident on neither host is routed at all, and the same
request timed directly against oMLX and through Olla.
