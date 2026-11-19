# One thread id across all platforms

Issue #12. RIPPLE already accepts a client-supplied thread id, but no client
sends it, so every platform derives its own id and the same conversation can end
up as two threads — on two hosts, with two prefix caches, counted twice in the
3 : 2 split. This document is what a client needs in order to stop that, and
what RIPPLE actually does with the value.

The rule is one sentence: **the client names the conversation, RIPPLE only
routes it.** Everything below is the consequence.

## The header

| | |
|---|---|
| Name | `X-Session-Id` — configurable via `affinity.header` (`ripple/config.py:126`, parsed at `ripple/config.py:452`) |
| Value | any non-empty string. RIPPLE does not parse, validate, normalise or length-limit it — it strips the surrounding whitespace and uses the rest verbatim (`ripple/registry.py:212-217`) |
| Scope | **one conversation, not one request.** Every turn of one conversation sends the same value; two conversations send two different values |
| Persistence | none needed. The registry is in memory (`ripple/registry.py:11-12`), so a RIPPLE restart forgets every thread anyway — a value that survives the client's own restart buys nothing |

Header names are compared case-insensitively (`k.lower() == wanted`,
`ripple/registry.py:213-215`), so `X-Session-Id` and `x-session-id` are the same
header. If the header appears twice, the first one wins.

What the value must satisfy:

1. **Stable for the life of the conversation**, across every turn — including
   turns where the client compacts or rewrites its own history. A value that
   changes on compaction loses the thread exactly when the cache is worth most.
2. **Distinct per conversation.** Two conversations with the same value are one
   thread to RIPPLE; nothing detects the collision.
3. **Opaque.** Nothing reads it. It appears (truncated) in the log and in
   `/stats`, so do not put anything in it that must not be read by whoever can
   read the log.

A random UUID per conversation satisfies all three. A client-side session id
does too, as long as it does not change when the conversation is resumed in a
new process.

```bash
curl -s http://localhost:8010/v1/chat/completions \
  -H "Authorization: Bearer $RIPPLE_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-Id: 7f3c1a9e-2b4d-4f60-9c1e-8a5d2b6f0c33" \
  -d '{"model":"DeepSeek-V4.1-Flash-1017-oQ4e-mtp",
       "messages":[{"role":"user","content":"hallo"}]}'
```

## What RIPPLE does with it

### Identification

A request is attributed to a thread in this order (`ripple/registry.py:1-13`,
`Ripple._identify` in `ripple/proxy.py:999-1014`):

1. **Header** — exact, preferred. The thread key becomes `sid:<value>`
   (`ripple/registry.py:216`).
2. **Fingerprint** — `sha256(model + "\0" + first user message)[:32]`
   (`ripple/registry.py:166-174`). The system prompt is deliberately excluded:
   it carries timestamps and tool lists and would change every turn. A
   fingerprint key has no prefix of its own.
3. **Prefix similarity** — the longest common prefix against the first
   `affinity.prefix_chars` (4096) characters stored per thread, accepted at or
   above `affinity.match_threshold` (0.5). This is what rescues a conversation
   whose text was rewritten by compaction (`ripple/registry.py:185-199`).

The header is not merged with the other two: when it is present, it *is* the
identity. A client that sends it never has to care what its first user message
looks like.

### Binding, prefix cache, TTL

The first request of a new thread is placed on a host by the placement rules;
every later request of that thread goes back to **that** host
(`reason=thread`, `ripple/proxy.py:644-667`) so oMLX's prefix cache for it stays
warm. The thread moves only when the bound host is gone or a rebind is worth its
prefill (`ripple/proxy.py:668-689`, `ripple/registry.py:294-304`).

* **TTL** — a thread that has been idle longer than `affinity.ttl_seconds`
  (1800 s, `ripple/config.py:127`) is dropped, its slot released; the purge loop
  runs every 30 s (`ripple/proxy.py:529-530`). A thread opened by a key of kind
  `batch` carries the shorter `affinity.batch_ttl_seconds` (120 s,
  `ripple/config.py:134`) and is promoted to interactive — TTL back to 1800 s —
  the first time an interactive request adopts it (`ripple/registry.py:277-292`).
* **Slots** — `max_threads` per host (default 20, `ripple/config.py:109`). When
  a host is full, a new thread evicts the idlest finished thread
  (`ripple/placement.py:334-352`); if the thread is already bound to that host
  and the host is saturated, the request is **refused with 503
  `host saturated` and `Retry-After: 1`** rather than moved
  (`ripple/proxy.py:653-667`) — moving it would cost the prefill the binding
  exists to avoid. A client should retry, not switch ids.
* **Model is part of the identity.** `lookup()` returns `None` when the thread's
  model differs from the request's (`ripple/registry.py:234-243`), and
  `register()` overwrites the entry under the same key
  (`ripple/registry.py:269-271`). One sid per conversation is correct; one sid
  used for two models ping-pongs the entry and loses the binding each time.
* **Not every request holds a thread.** A request with exactly one user message
  and an estimated prompt below `affinity.min_prompt_tokens` (8192) is
  *ephemeral*: it holds no slot and is not registered, header or not
  (`ripple/proxy.py:1029-1042`). A `one-shot` client key never binds a thread at
  all (`ripple/proxy.py:604-612`). The header is read in both cases (so
  `consistent_hash` still spreads the requests), but no thread survives.
* **The visible label is truncated — at 24 characters.** Log, `/stats` and the
  `x-ripple-thread` response header carry `key[:24]` (`ripple/registry.py:32,67`)
  — that is `sid:` plus the first 20 characters of the value. A Hermes id is
  `YYYYMMDD_HHMMSS_<random>`, so the whole timestamp fits behind the prefix and
  two sessions of the same hour stay distinguishable (before issue #68 the cut
  was 16, i.e. `sid:` plus 12: `sid:20261117_150` was every session that hour).
  A client id whose first 20 characters are identical to another's will still
  look the same in the log; keep the distinguishing part early.

## What it does *not* do

* **It is not authentication.** Nothing in the header path touches the key
  store: the only credential RIPPLE compares is `Authorization` against the
  client keys (`ripple/keys.py:376-396`, `ripple/proxy.py:159-165`). A request
  with a header and no valid key is refused for the key, not for the header; a
  request with a valid key and no header is served normally (RB-5,
  `docs/requirements.md:779`). Never send a secret as `X-Session-Id` — it is
  logged, and the log is the one thing that survives a restart.
* **It is not isolation.** Anyone who sends another client's value joins that
  thread: same host, same prefix cache, counted as the same conversation. That
  is a feature for a client that reconnects and a hazard for a shared
  deployment. Treat the value as public.
* **It does not prevent collisions.** Two clients that both choose
  `default` are one thread, silently. The `sid:` prefix does not namespace by
  client — a sid never collides with a fingerprint, but it does collide with
  another sid. A whitespace-only value is not rejected either: it strips to the
  key `sid:`, which every such client then shares. Send a real value.
* **It guarantees nothing about the prompt cache.** The binding is a routing
  decision. oMLX may have dropped the cache (another model loaded, the slot
  evicted), and the first turn of a thread always prefills.
* **It is not durable.** No thread state is written to disk; after a restart
  the same sid opens a new thread. That is deliberate (`ripple/registry.py:11-12`)
  and the reason a client need not persist the value.

## How to verify a client got it right

The request log carries one line per request (`ripple/stats.py:143-176`):

```
model=<m> host=<h> reason=<r> client=<addr> thread=<label> thread_source=<sid|fp|sid+fp> prompt_chars=<n> prompt_tokens=<n> duration_ms=<x> status=<code>
```

`thread_source` is read from the key prefix *and* from how this request found
the thread: `sid` means the client said so, `fp` means RIPPLE guessed
(`ripple/proxy.py:425-443`). Two consecutive turns of one session must show the
same `thread=` **and** `thread_source=sid`.

The third value, `sid+fp`, is the honest label for a request that reached a
`sid:` thread **without** the header — by prompt similarity after a compaction,
say. The id is the client's, the source this request used was not; calling it
`sid` made those requests uncountable as header traffic (issue #68). So:

* `thread=sid:… thread_source=sid` — the header was sent and matched;
* `thread=sid:… thread_source=sid+fp` — a sid thread, adopted by prefix match;
* `thread=<hash> thread_source=fp` — no header, fingerprint/prefix only.

`grep -c 'thread_source=sid '` therefore counts real header hits, not every
request that happens to sit on a sid thread.

The same pair is available without the log:

* `GET /stats` → `decisions[].thread` / `decisions[].thread_source`
  (`ripple/stats.py:83-103`), and `threads[]` from the registry snapshot with
  `source` (`ripple/proxy.py:1342-1345`, `ripple/registry.py:436-444`).
* `GET /healthz` → `threads.registry[]` (first 20), each with `key` (the label)
  and `source` (`ripple/proxy.py:1322`).
* Response headers `x-ripple-host`, `x-ripple-reason`, `x-ripple-thread`
  (`ripple/proxy.py:855-858`).

## Client steps

Both steps below change files outside this repository and are **operator
steps**, not part of this change. The Hermes half was executed and verified on
the wire on 17.11.2026 (below); the opencode half is still open.

### Hermes

Hermes has a first-class, opt-in mechanism for exactly this: a provider entry
may name the header, and Hermes then sends its per-conversation affinity key in
it.

* File: `~/.hermes/config.yaml`
* Key: `session_affinity_header: X-Session-Id` on the entry whose
  `base_url`/`api` resolves to `http://localhost:8010/v1` — a
  `custom_providers` list entry or a `providers.<name>` entry; the camelCase
  alias `sessionAffinityHeader` is accepted too (with a warning).
* **There is no such entry today.** The only custom entry in the file points at
  `http://localhost:8000/v1` (oMLX directly), while the active
  `model.base_url` is `:8010`; the header lookup is keyed by route, so an entry
  for `:8010` has to be added (or the key added to one) and the model pointed at
  it:

  ```yaml
  providers:
    ripple:
      api: http://localhost:8010/v1
      name: RIPPLE
      default_model: DeepSeek-V4.1-Flash-1017-oQ4e-mtp
      session_affinity_header: X-Session-Id
  ```

* Value sent: the conversation's rotation-stable affinity key, resolved per
  request (`agent/opencode_affinity.py:81-89,114-150`); the lookup of the
  configured header name is `hermes_cli/config_providers.py:515-528`. It is
  stable across compaction and across the turns of one session, which is
  exactly what the header needs. Default is off, so nothing is sent until the
  key is set.
* **No restart.** The lookup reads the config on every request and caches on
  `(mtime, size, inode, ctime, digest)` (`hermes_cli/config.py:1804`), so an
  edit takes effect on the next request of a running session. Measured
  17.11.2026: before the edit every line read `thread_source=fp`, a few seconds
  after it two consecutive turns of one session read
  `thread=sid:gwk_66695d98 thread_source=sid`.

Verified end to end on 17.11.2026 against the running service: the entry above
sends the affinity key on `:8010`, sends nothing on `:8000`, and an entry
without the key sends nothing at all. Two consecutive turns of one session show
`thread_source=sid` and the same thread both times.

Two traps, both measured:

* **The route identity is the URL string.** `http://127.0.0.1:8010/v1` does not
  match `http://localhost:8010/v1`. If `model.base_url` ever moves to the
  numeric form, the header silently disappears.
* **The log label is 24 characters.** The request log writes `key[:24]` —
  `sid:` plus twenty characters, which holds the whole `YYYYMMDD_HHMMSS` stamp
  of a Hermes id (`sid:20261117_150500_gwk_`), so two sessions of the same hour
  are distinct (issue #68; at 16 the log said `sid:20261117_150` for all of
  them). Ids that share their first 20 characters still collide in the log;
  the registry keeps the whole key.

### opencode

The config can carry a header, but only as a **constant**:

* File: `~/.config/opencode/opencode.jsonc`. The installed CLI is v2.1.4 and
  the file uses the V1 shape — `provider.<id>.options.headers`; V2 uses
  `providers.<id>.headers`. Either way the values are plain strings, and the
  only substitution opencode does is `{env:VAR}` and `{file:path}`
  (`opencode.ai/docs/config` §Variables, `opencode.ai/docs/providers`
  §Headers), so the value is one fixed string for every session.

That is the opposite of what the header needs: a constant sid puts every
opencode session into one thread, which is worse than the fingerprint it would
replace (one thread for all conversations, one slot, one host, one cache).
It is therefore **not configured**, and opencode keeps the fingerprint until it
can send a per-session value — either through a plugin that sets the header per
request, or through upstream support for a per-session placeholder. Until then
the log says `thread_source=fp` for opencode traffic, which is the honest state.

**Decided 18.11.2026:** the operator accepts this as the answer, so issue #12
closes on "Hermes sends a thread id, opencode does not by design". A constant
header stays refused — one thread for every opencode conversation would be
worse than the fingerprint, not better. If opencode ever grows a per-request
placeholder, the section above is where the change goes.

### Third-party clients that cannot send it

Nothing to do, and nothing to configure. RIPPLE falls back to the fingerprint
over `model + first user message` and, when that misses after a compaction, to
prefix similarity. The conversation is still routed consistently — it is just
not guaranteed to be the *same* thread as the same conversation on another
platform, which is the whole point of the header.

## Evidence

There is no test for this in the repository: the header path is exercised by the
running service. What the log shows today (2026-11-15, 18:35 CET) is the
fingerprint case, because no client sends the header yet:

```
$ grep -a "thread_source" ~/Library/Logs/ripple.log | tail -4
2026-11-15 18:35:36 INFO    ripple.request: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp host=M5-alpha reason=thread client=127.0.0.1 thread=730c1df2e2d76a3f thread_source=fp prompt_chars=224 prompt_tokens=67509 duration_ms=107.7 status=200 note=stream thread
2026-11-15 18:35:50 INFO    ripple.request: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp host=M3-beta reason=thread client=127.0.0.1 thread=53adb98e8d5d019d thread_source=fp prompt_chars=268 prompt_tokens=64789 duration_ms=242.1 status=200 note=stream thread
2026-11-15 18:35:52 INFO    ripple.request: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp host=M5-alpha reason=thread client=127.0.0.1 thread=730c1df2e2d76a3f thread_source=fp prompt_chars=224 prompt_tokens=67939 duration_ms=165.4 status=200 note=stream thread
2026-11-15 18:35:55 INFO    ripple.request: model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp host=M5-alpha reason=thread client=127.0.0.1 thread=730c1df2e2d76a3f thread_source=fp prompt_chars=224 prompt_tokens=68239 duration_ms=169.9 status=200 note=stream thread
```

Read what it says: two conversations (`prompt_chars` 224 and 268 — different
first user messages) on the same model, each recognised by fingerprint, each
bound to a host (`reason=thread` on every turn), and both coming from the same
address `client=127.0.0.1`. Nothing on the line says *which* client — that is
the gap the header closes.

The excerpt is a measured quote from before issue #68: the labels are 16
characters there because that was the cut then. Today the same lines carry 24
characters (`730c1df2e2d76a3f…` and so on) — the quotes are left as they were
logged.

```
$ grep -ac 'thread_source=sid' ~/Library/Logs/ripple.log
0
$ grep -ac 'thread_source=fp' ~/Library/Logs/ripple.log
508
```

Zero `sid` lines is the correct reading of the current state: no client sends
`X-Session-Id`, so every thread in the log was derived from a prompt. An excerpt
showing `thread_source=sid` will exist only after the Hermes step above has been
done and the service restarted — it is not quoted here because it does not
exist yet.

## Open items

1. **Hermes** — `session_affinity_header` in `~/.hermes/config.yaml` plus a
   gateway restart (operator step; ends the running session). Then verify
   `thread_source=sid` on two consecutive turns and add that excerpt here.
2. **opencode** — per-session header needs a per-request hook; the config alone
   can only send a constant. Open until that exists.
3. **Third-party clients** — nothing to do; the fingerprint stays, and since
   #3 the log says `thread_source=fp` so the two cases are distinguishable
   without guessing.
