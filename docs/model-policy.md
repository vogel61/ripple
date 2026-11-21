# Model policy — capability, rule, lock, residency

The operator wants to make a model unavailable **without touching oMLX**. Two
different wishes hide behind that sentence, and they are two levels of one
operator state:

| level | dispatch to this host | appears in `/v1/models` |
|---|---|---|
| `disabled` | no | yes, as long as another host offers the model |
| `hidden` | no | no — once **every** host that offers it hides it |

`hidden` implies `disabled`. Hiding a model but still routing to it would answer
a client that knows the id while telling everyone else the model does not exist
— the one combination nobody can reason about.

The qualifier on `disabled` is not decoration: when it is the only host that
offers the model, nothing can serve it any more, and the model leaves the list
as well. A list entry that every request answers with a 503 is worse than no
entry — Hermes and opencode would keep choosing it.

Nothing about oMLX changes: no model is loaded, unloaded or patched, exactly as
with a maintenance window (`ripple/maintenance.py`). This is routing state, and
it is the smaller exception of the two.

The **lock** is a third operator decision and sits on another axis: it says
nothing about where a model may be routed and everything about whether its
memory may be taken away (issue #21). It has its own section further down.

Before any of the three: four statements about "a model on a host" are in play at
the same time, and they are easy to read as one another. #116 was exactly that
mistake — a listed-but-cold model read as servable. The next section names all
four, what each combination means and where each one lives; the rest of this file
is the operator's two of them, the rule and the lock.

## The four statements about a model

Each axis answers a different question, lives in a different place, and implies
nothing about the other three.

| Axis | Where it lives | The question it answers | Code |
|---|---|---|---|
| **capability** | `hosts[].models` in `config.yaml` — the allow-list | may this host serve this model at all? | `ripple/config.py:165` (`HostConfig.models`), `ripple/config.py:178` (`model_allowed`), gate 1 in `ripple/hosts.py:1447` |
| **rule** | `ModelPolicy.rules`, keyed `(host, model)` | does RIPPLE route this model to this host, or does it not? | `ripple/model_policy.py:72` (`Rule`), `ripple/model_policy.py:149` (`blocks`), gate 2 in `ripple/hosts.py:1449` |
| **lock** | `ModelPolicy.locks`, keyed by model id | may this model be unloaded? | `ripple/model_policy.py:103` (`Lock`), `ripple/model_policy.py:167` (`locked`), `ripple/hosts.py:1639` (`locked_loaded`) |
| **residency** | `loaded` on `ModelInfo` | are the weights resident right now? | `ripple/backends.py:47`, read by `ripple/hosts.py:1463` (`model_routable`) and `ripple/proxy.py:951` (`_lock_hosts`) |

Three properties of the list matter when reading the table:

* An empty `hosts[].models` means "everything this host offers"
  (`ripple/config.py:179`). Capability is therefore **open by default** and the
  axis is invisible until an operator closes it.
* Capability is configuration: `config.yaml`, read at start-up. The rule and the
  lock are operator state, written by admin endpoints into
  `~/.ripple/models.yaml` (`ripple/model_policy.py:266`). Residency is oMLX's
  answer, and RIPPLE only reads it.
* None of the four is per-thread or per-client. A key's own model scope
  (`ripple/keys.py`) is a fifth, client-facing statement and is not one of these
  — see *What this is not* at the end.

**The two predicates are not the same question (issue #116).**
`HostRuntime.model_available()` (`ripple/hosts.py:1453`) answers "would RIPPLE
send a request here": allow-list, policy, and the model being in the node's list.
A cold load counts, so a listed-but-cold model is available.
`HostRuntime.model_routable()` (`ripple/hosts.py:1463`) answers "can this node
serve it right now": the same predicate plus `loaded`, and it is what the page's
availability column and the `not routable` badge show. Reading the first as the
second is #116 — a several-hundred-GB model that would have to be loaded first
read as servable. The lock is deliberately **not** an input to `model_routable()`
(`ripple/hosts.py:1481`): a lock is a veto on an unload, never evidence that
something is loaded.

### The combinations

The interesting part is not the four axes but their products. "Page" is the Models
tab, fed by `/healthz → models[]` (`ripple/proxy.py:3650`); "code" is the
predicate that decides.

| capability | rule | lock | residency | what the code does | what the page shows |
|---|---|---|---|---|---|
| permitted | routes | locked | loaded | dispatch works; a request that would evict it is dropped from the candidate list before ranking (`ripple/placement.py:153`), so `503 model_locked` only when no host is left (`ripple/placement.py:248`); calibration and replication on that node answer `409` (`ripple/calibration_api.py:645`, `ripple/proxy.py:1188`) | padlock closed and red, `lock_reason` names the hosts that hold it, loaded dot on, `hosts[].lock` true there |
| permitted | routes | locked | cold | dispatchable, so the first request pays the cold load; the lock refuses nothing (`ripple/proxy.py:1025`); the preload loads it at the next healthy transition (`ripple/hosts.py:2039`) or records a `failed` with the numbers (`ripple/hosts.py:2060`) | padlock closed, `loaded_on: []`, `lock_reason: null`, tooltip "not loaded anywhere right now — the lock refuses nothing", the row collapsed under `available, not loaded` |
| permitted | routes | locked | cold, and **cannot fit anywhere** | the lock is stored and logged (`model_lock`, `model_lock_infeasible`, `ripple/proxy.py:2009`), and the arithmetic says the promise cannot be kept: every host that offers the model refuses it (`preload_forecast`, `ripple/hosts.py:1516`), so the next healthy transition would record `does not fit: … is locked and cannot be evicted`. Nothing is evicted, unlocked or pinned to make room — the forecast only reads (`_lock_feasibility`, `ripple/proxy.py:1041`). `GET /admin/models` carries the verdict *before* the click: `feasible: false`, `feasible_host`/`feasible_reason` from the first refusing host in sorted order, `hosts[].fits: false` with the same sentence | padlock closed plus the warning state: the lock is kept and the row says which host cannot hold it and with which numbers — a stored intent, not a broken promise discovered at the next preload |
| permitted | does not route here — and this is the only host that holds it | locked | loaded | **the drain pattern**: no new thread is placed (`model_permitted` false → `model_available` false, `ripple/hosts.py:1449`), the resident copy is still protected from eviction (`ripple/placement.py:157`), and `lock_reason` is `null` because no request is aimed at it (`ripple/proxy.py:1022`). `listed: false`, so it leaves `/v1/models` | padlock closed, `lock_reason: null`, `hosts[].lock` true on the node that holds it, policy select `disabled`. Legitimate: keep the model warm, stop sending new threads. With a second host that still routes it, this row is the first one — the refusal is in force for the copy that is reachable |
| not permitted anywhere | — | locked | loaded | no request is aimed at the model anywhere, so nothing is refused for its sake (`lock_reason: null`); the resident copy still blocks every request for *another* model on that host that would evict it (`ripple/placement.py:157`) — a lock that costs memory and protects nothing the fleet serves | padlock closed, `loaded_on` names the host, `lock_reason: null`, tooltip "the lock refuses nothing", model not in `/v1/models` |
| permitted | routes | unlocked | loaded | the normal case: it is served, and evicted when another request needs the memory and it is neither pinned nor holding a thread (`ripple/placement.py:165`) | padlock open in the neutral tone, `lock_reason: null`, dot on |
| permitted | routes | unlocked | cold | a cold load on the first request, reason `cold_load` | padlock open, `routable: false`, the `not routable` badge, the row dimmed |
| — (no allow-list names it) | — | always | resident, no weights | an oMLX **builtin**: never dispatched to, never locked by an entry, never unlockable (`409 lock_builtin`) | padlock closed and inert, its own tooltip, no policy select |

`locked` + a model no host lists at all — locked ahead of an oMLX start, or while
a node's model list is unreadable — is the fourth row with nothing resident: the
lock is stored, `offered: false`, `loaded_on: []`, `lock_reason: null`, and
nothing is refused until the model is resident somewhere a request can reach. The
admin view reports such a row on purpose (`ripple/proxy.py:1036`).

**`locked` + cannot fit anywhere is a warning, not a refusal (issue #126).** On
the M5 the two big models together exceed the ceiling: DeepSeek is 422.5 GB
resident against a 487.7 GB ceiling and GLM-5.2-oQ4 needs 418.1 GB, so a lock on
GLM is a promise the arithmetic cannot keep. The write is still stored (200), and
the operator used to learn the verdict only at the next healthy transition, from
the preload's `does not fit: … is locked and cannot be evicted`. The lock view now
says it before the click, from the preload's own arithmetic and nothing else
(`_lock_feasibility`, `ripple/proxy.py:1041`, calling
`HostRuntime.preload_forecast`):

* `feasible: false` only when at least one host offers the model and **every**
  offering host refuses it; `feasible_host` and `feasible_reason` then name the
  first refusing host in sorted order and its sentence;
* `null` when no host offers the model, or when no offering host can forecast at
  all — `([], None)` from the forecast means "no forecast" (unknown size, no
  ceiling, guard off), never "does not fit";
* `true` otherwise, including when one host would have to evict an unlocked model
  to hold it.

`hosts[].fits` carries the same answer per host: `null` where the host does not
offer the model, `false` with the forecast's sentence where it refuses, `true`
otherwise. A builtin is never infeasible (it has no weights), and a model that is
merely not routed here does not warn — only memory infeasibility does. The
forecast only reads: no eviction, no unlock and no pin happens to make room. The
state lives in `GET /admin/models`; `/healthz` keeps its fields, so a reader that
only wants the refusal sentence does not have to learn a prognosis.

**`locked` + permitted nowhere is the row to read twice.** The lock is a promise
about memory, and memory no request will ever ask for is not being protected. The
view says so: `lock: true` (the operator's entry is still set), `loaded_on` names
the host, and `lock_reason: null` — the sentence that would name a `503` is not
produced, because `_dispatch_hosts()` is empty (`ripple/proxy.py:1022`). The
tooltip is the operator-facing half of that: `lock.nowhere`, "the lock refuses
nothing". Two facts in one icon on purpose — the padlock is the entry, the tooltip
is its effect.

One wording gap, documented as it is rather than papered over: the tooltip picks
its sentence on `lock_reason` alone (`ripple/static/dashboard.js:3409`), so the
two rows where the model **is** resident and refuses nothing because nothing is
dispatched show "not loaded anywhere right now". The verdict is right, the reason
is not; that is a sentence to fix in the page, not in this file.

### Two intents, two places

"The model must not be routed" and "the model must not be on this machine" are
different intents with different homes. The operator's rule (2026-11-20) is that
they never share one field:

| the operator means | the place | what changes | the way back |
|---|---|---|---|
| the model exists on this host's SSD but must not be routed | the **allow-list**, `hosts[].models` (`ripple/config.py:165`) | `model_available()` is false on this host (`ripple/hosts.py:1447`): no dispatch, no admission, no rebind target. The weights stay where they are and the node keeps reporting them | delete the line, restart. Visible in the config, per host, and reversible by the same edit |
| the model should not exist on this machine at all | **oMLX** — remove it from the model directory | the node stops listing it; RIPPLE observes the result (`ripple/backends.py:44`) and nothing else | oMLX's own undo: copy it back |

RIPPLE has no route that deletes weights and none that edits the allow-list of a
host already in the fleet. The only endpoint that can create an allow-list is the
one that declares a whole node (`POST /admin/nodes`, `ripple/proxy.py:1495`); the
two operator endpoints write `~/.ripple/models.yaml` and nothing else
(`ripple/proxy.py:1884`, `ripple/proxy.py:1919`).

Why the separation is worth a rule: written into one field, the second intent
looks exactly like the first. A model that is "not routed" is still on the SSD and
— until something evicts it — still resident, so weeks later nobody can say
whether it was meant to be quiet or gone, and the machine is the only place the
answer still exists. It is also why the allow-list is a list of what a host **may**
carry and not a list of what it has: the difference between the two is the whole
of this section.

### One edit, one axis

Editing one axis must never silently change another. The code has three answers to
that requirement, and none of them is "mutate something else":

* **Refuse.** An oMLX builtin cannot be locked or unlocked (`409 lock_builtin`,
  `ripple/proxy.py:1882`, `ripple/proxy.py:1917`); a calibration or a replication
  that would unload a locked model is refused before it starts
  (`ripple/calibration_api.py:645`, `ripple/proxy.py:1188`); a request whose
  only host would evict a locked model is refused with `503 model_locked`
  (`ripple/placement.py:248`).
* **Show the resulting state.** Every lock write answers with the whole fleet-wide
  view (`ripple/proxy.py:1895`, `ripple/proxy.py:1923`), and the policy `GET`
  reports the four separate answers `offered` / `allowed` / `listed` / `dispatch`
  on every row (`ripple/proxy.py:944`).
* **Warn, once.** Locking a model that no host lists is accepted and logged
  (`model_lock_unknown_model`, `ripple/proxy.py:1890`); a hand-edited policy file
  naming a level this version does not know is logged and ignored rather than
  silently re-enabling the model (`ripple/model_policy.py:237`).

Nothing here writes `config.yaml`, nothing deletes a model from a node, and the
lock never turns into a routing rule (`ripple/model_policy.py:19`).

## Persistence

`~/.ripple/models.yaml` (config key `placement.model_policy_file`), mode `0600`,
written atomically. Loaded at start-up, before the fleet is built, so a model
switched off for a reason is off from the first request after a deploy — which
is precisely when an operator restarts RIPPLE.

Only the admin endpoints write the file. The balancer only reads it, so nothing
can appear on disk behind the operator's back.

```yaml
version: 1
models:
  M5:
    DeepSeek-V4.1-Flash-1017-oQ4e-mtp:
      level: disabled
      since: '2026-11-15T14:02:11+01:00'
      by: dashboard
      note: broken quantisation
```

A missing or corrupt file means "nothing is switched off". The expensive
direction — starting with every model hidden because one YAML line broke — is
the one that must never happen. The locks share this file, one level up; see
*The store* in the lock section below.

## Endpoints

The shape of the maintenance routes, one level deeper (admin key required, same
`_require_admin()` gate and the same per-address rate limit):

```
GET    /admin/nodes/{node}/models                  # every model with its decision
POST   /admin/nodes/{node}/models/{model}?level=disabled|hidden&note=…
DELETE /admin/nodes/{node}/models/{model}          # back into service
```

`{model}` is a path parameter, so an id with a slash (`org/model`) is
addressable. `level` and `note` may also arrive in a JSON body — a dashboard
toggle posts one.

The `GET` row answers four different questions, because "why is it not there"
has four answers:

| key | meaning |
|---|---|
| `offered` | oMLX lists the model on this node |
| `allowed` | the config's allow-list (`hosts[].models`) permits it |
| `listed` | it appears in `/v1/models` — a fleet answer, not this node's |
| `dispatch` | RIPPLE would send a request here |

`/healthz` carries `policy` and `dispatch` on every `models[].hosts[]` row, so
the dashboard and the soak report see the same decision.

## Behaviour

* **Placement.** `HostRuntime.model_available()` is the one predicate that
  placement, admission and rebind already ask; the policy is a gate inside it,
  next to the allow-list. A `disabled` host is never chosen, never admitted and
  never a rebind target.
* **A bound conversation moves.** A thread bound to a host where the model is
  now `disabled` falls back to a host that still offers it (reason `fallback`),
  like the maintenance path — the session survives the switch.
* **`/v1/models`** drops a model when no host in the pool could serve it: a
  `hidden` model is dropped for every host that hides it, a `disabled` one stays
  listed while another host still offers it. The entry-host fallback (the raw
  oMLX list used when nothing is discovered yet) is filtered too, and it no
  longer fires when the hosts do know their models but everything was filtered
  out — an empty answer is the honest one there.
* **A request for a model that is switched off everywhere** is a `503` with
  reason `model_disabled` or `model_hidden` and a message naming the hosts,
  their levels and the `DELETE` that undoes it. Not a `404`: for a `disabled`
  model the model is still in `/v1/models`, and `404` is what the aggregation
  answers for a model nobody offers.

## The lock — keeping a model resident

`disabled` and `hidden` answer "may RIPPLE route to this model". The lock answers
a different question: "may RIPPLE take this model's memory away". It is keyed by
the model id alone, not by (host, model), because what the operator protects is
the availability of the model wherever it is resident — and the eviction that
actually hurts is the one a request for *another* model causes on exactly the
host that holds it. One lock therefore covers every node the model is loaded on.

It is deliberately not a policy level:

| the lock is not | because |
|---|---|
| `disabled` or `hidden` | routing is untouched: the model stays in `/v1/models` and is served on the host that holds it |
| a veto on eviction in general | only the eviction of *that* model is refused; a thread giving up its slot, and the eviction of an unlocked model, work exactly as before |
| a client-visible setting | it is fleet state and needs the admin key, like the policy |

### The refusal

A request whose only way onto a host would evict a locked model is answered
`503` with reason `model_locked`. Here for `Qwen3.8-Flash-Next-oQ4e-mtp` while
`beta` is in maintenance, so `M5` is the only host left:

```
model 'Qwen3.8-Flash-Next-oQ4e-mtp' needs memory that is held by a locked model
(DeepSeek-V4.1-Flash-1017-oQ4e-mtp on M5); no other host can take it without
evicting one. Unlock it with DELETE
/admin/models/DeepSeek-V4.1-Flash-1017-oQ4e-mtp/lock
```

The other host is always tried first. A host that could only serve the request by
evicting a locked model drops out of the candidate list before the remaining
hosts are ranked, so the 503 is the last word and not the first: a cold load
elsewhere beats breaking the promise, and that request is served with
`x-ripple-reason: cold_load`. The lock turns a servable request into an error in
exactly one case — no host can take it without the memory.

**It refuses only while the model is resident.** A locked model that is loaded
nowhere reports `lock: true`, `lock_reason: null` and blocks nothing. That is the
right call: the lock protects memory that is in use, and a model nobody has
loaded costs nothing to keep. Refusing there would fail requests for a reason
that no longer exists — and would leave the fleet wedged after oMLX unloaded the
model on its own, with a lock whose effect nobody can see.

`lock_reason` has a second `null` case, and it is not about residency: a model
that is resident but that no host would dispatch to — `disabled` on every host
that holds it, or outside every allow-list — refuses nothing either, because no
request is ever aimed at it (`ripple/proxy.py:1022`). The combinations are in
*The combinations* above; the row to read twice is `locked` + permitted nowhere.

### The promise is delivered, not just defended (issue #119)

A lock that only refuses evictions defends residency on a node that already has
it. After an oMLX restart — or after a maintenance window — the node is healthy
and empty, and the first thread paid the cold load (65,8 s on the M5, ~2 min on
the M3) although the operator's intent was "this model is always there".

So a locked model is **loaded as soon as its host becomes healthy**, on every host
whose allow-list contains it and whose policy does not switch it off
(`ripple/hosts.py:2052`): at start-up and after every maintenance window. The
trigger is `preload_due`, set next to `relearn_due` on the transition into a
healthy verdict (`ripple/hosts.py:628`), and the end of a maintenance window calls
`Fleet.request_preload()` directly (`ripple/proxy.py:677`) because the node was
healthy throughout the window. The pass is a named background task per host
(`ripple/hosts.py:1926`), never awaited by the probe tick. The loading state shows
it (`source: preload`, `ripple/hosts.py:2095`), the dashboard answers while it
runs, and the load never blocks the startup. Two consequences the operator will
see:

* **The lock wins an eviction.** If the preload displaces an unlocked model, that
  is intended. What it displaced is logged (`preload_evict`,
  `ripple/hosts.py:2114`) and stays visible in the model view
  (`models[].hosts[].evicted_by`, `ripple/proxy.py:3636`) — a model that vanishes
  without a word is a mystery, not a decision.
* **What does not fit fails loudly, once.** Two 400 GB models do not fit on one
  512 GB host, and a locked model is never evicted for another one. The attempt
  ends as `failed` with the sentence `does not fit: 400.0 GB needed, 40.8 GB
  free; … is locked and cannot be evicted` (`ripple/hosts.py:1562`), it is
  reported in `hosts[].preloads[]` (`ripple/hosts.py:1717`), and it is not
  retried — the next attempt is the next healthy transition.

A cold model can be locked (issue #118), but locking it does **not** load it on
the spot: the promise is delivered on the next healthy transition or restart.
The rules are in `docs/loading-state.md` §5a.

### Calibration and replication

Both can take a node's memory away, so both refuse while a locked model is
loaded on it:

* **Calibration.** `POST /admin/nodes/{node}/calibrate` answers `409` with a
  message naming the locked model and the `DELETE` that releases it. The check
  sits after the health and slot checks and before the plan, so nothing is
  unloaded first — a job whose first effect is the thing the lock prevents is
  never started. `force=1` does not override it: that flag is about bound
  threads, i.e. about a prefix cache, and this is about a model's residency.
* **Replication.** The reload at the end of a job unloads every model on the
  target, so `GET /admin/replication/plan` answers `ok: false` with the same
  sentence and the `POST` answers `409` without starting a job. A copy to a node
  that does not hold the locked model is allowed.

### The store

The locks live in the same file as the policy — `~/.ripple/models.yaml`, config
key `placement.model_policy_file`, mode `0600`, written atomically, loaded at
start-up before the fleet is built — one level up, because they are keyed by the
model and not by a host:

```yaml
version: 1
locks:
  DeepSeek-V4.1-Flash-1017-oQ4e-mtp:
    model: DeepSeek-V4.1-Flash-1017-oQ4e-mtp
    since: '2026-11-15T22:41:07+01:00'
    by: dashboard
    note: kept warm for the operator's own session
```

The key is written only when there is at least one lock, so a fleet that never
uses the feature keeps the file it always had; `locks: {m: true}` reads as
"locked, nothing else known". A lock survives a restart of RIPPLE with its
`since`, `by` and note intact, and the first request after the restart is refused
again — a restart is not an operator decision to release it. Only the admin
endpoints write the file; the balancer only reads it.

### Builtins — always locked, never unlockable (issue #118)

MarkItDown is not a model: it is an oMLX virtual engine (`engine_type:
markitdown`) that is resident everywhere, occupies no weights, is in no
allow-list, and is routed to by nothing. RIPPLE derives it from what the node
reports, never from a list of ids in this repository: `ModelInfo.engine_type`
(`ripple/backends.py:57`) is matched against `VIRTUAL_ENGINES`
(`ripple/hosts.py:59`) by `Host.model_builtin()` (`ripple/hosts.py:1493`).

Three consequences:

* **It reads as locked without an entry in the policy file.** `_locked()` is the
  operator's entry *or* a derived builtin (`ripple/proxy.py:986`), so `/healthz`,
  `GET /admin/models` and the refusal sentence cannot disagree about whether a
  model reads as locked. `builtin: true` and `lockable: false` say where the
  answer came from (`ripple/proxy.py:1068`).
* **It cannot be locked or unlocked.** Both writes answer `409 lock_builtin` and
  store nothing (`ripple/proxy.py:1882`, `ripple/proxy.py:1917`). A stale entry
  written before #118 is left in the file and stays inert — the derived state wins
  on every read.
* **Nothing loads or dispatches it.** The preload skips it
  (`ripple/hosts.py:2047`) because it has no weights, and `model_available()` is
  false on every host because no allow-list names it (`ripple/hosts.py:1447`).

An empty `engine_type` is "the node did not say", so a node whose model list could
not be read reports nothing as a builtin (`ripple/hosts.py:1507`). The page draws
it as an inert padlock with its own tooltip; the visual rules are in
`docs/dashboard-design.md`, *Das Schloss in der Modellzeile*.

### Endpoints and reporting

```
GET    /admin/models                            # every known model with its lock
POST   /admin/models/{model:path}/lock?note=…   # lock (idempotent, refreshes stamp and note)
DELETE /admin/models/{model:path}/lock          # unlock
```

`{model:path}` makes an id with a slash (`org/model`) addressable, and `note` may
also arrive in a JSON body. Both writes answer with the `GET` view, which is
fleet-wide on purpose: the lock is per model, so a per-node list would repeat one
flag per node and could disagree with itself. A row is reported for a locked
model that no host currently lists (`offered: false`), otherwise a stale lock
would be invisible exactly when someone asks why a request is refused.

| row key | meaning |
|---|---|
| `lock` | the model is locked — the operator's entry **or** a builtin's derived state |
| `builtin` | it is an oMLX virtual engine, locked by derivation |
| `lockable` | `false` for a builtin: no write can change it |
| `since`, `by`, `note` | who locked it, when, and why — `null` for a builtin, which has no entry |
| `loaded_on` | hosts that have it resident right now |
| `lock_reason` | the sentence `/healthz` shows — only while the refusal is in force, i.e. locked, resident somewhere, and dispatched to somewhere |
| `feasible` | could the lock be delivered at all: `false` when every host that offers the model refuses it, `null` when no host offers it or none can forecast, `true` otherwise (issue #126) |
| `feasible_reason`, `feasible_host` | the forecast's sentence and the first refusing host (sorted) — `null` unless `feasible` is `false` |
| `listed` | it appears in `/v1/models` |
| `hosts[].lock` | locked **and** resident on that host |
| `hosts[].offered`, `hosts[].loaded`, `hosts[].policy`, `hosts[].dispatch`, `hosts[].routable` | as on the policy rows; `routable` is the availability column (issue #116) |
| `hosts[].fits`, `hosts[].fit_reason` | whether that host could hold it: `null` where it does not offer the model, `false` with the forecast's sentence where it refuses, `true` otherwise — including where there is no forecast to make |

`/healthz` carries the lock on these keys:

| key | meaning |
|---|---|
| `locks` | every locked model id, sorted — stored entries **and** builtins |
| `models[].lock` | the model is locked |
| `models[].builtin` | it is an oMLX virtual engine |
| `models[].lock_reason` | why requests are refused for its sake; `null` while the lock is not in force |
| `models[].hosts[].lock` | locked **and** loaded on that host — the per-node flag |
| `hosts[].preloads[]`, `hosts[].displaced`, `models[].hosts[].evicted_by` | the preload of a locked model (issue #119); their shape is in `docs/loading-state.md` §5a |

`/stats` counts the refusals as `requests_model_locked`.

One inconsistency is deliberate and documented as it is: the two write routes
(`POST`/`DELETE …/lock`) answer `403` for a missing or wrong admin key, while
`GET /admin/models` and the rest of `/admin` answer `401`. The operator's
decision is that this state is *forbidden* to everyone but them, not that a
credential is missing. It is one argument (`_require_admin(status=…)`) away from
401 everywhere if that ever reads wrong.

### Undoing it, and what the lock does not outlive

`DELETE /admin/models/{model}/lock` is the whole undo; it is idempotent and
leaves the policy rules in the file alone. For a builtin there is nothing to undo
and the write answers `409 lock_builtin` (`ripple/proxy.py:1917`). When the
locked model is unloaded anyway — oMLX evicted it under pressure from a load
RIPPLE did not place, an
operator unloaded it, the node restarted — the lock stays in the file and simply
refuses nothing until the model is resident again. Eviction of *unlocked* models
is unchanged throughout: the lock is a promise about one model, not a general
brake on the eviction logic.

## What this is not

Not a client-visible knob for a single key: a key's own model scope
(`ripple/keys.py`) stays the per-client mechanism. This is fleet state, decided
by the operator, and it needs the admin key.
