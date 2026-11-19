# Model policy — switching a (host, model) pair off

Richard wants to make a model unavailable **without touching oMLX**. Two
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

A third operator decision, the **lock**, sits on the other axis: it says nothing
about where a model may be routed and everything about whether its memory may be
taken away. It has its own section at the end of this file (issue #21).

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
`M3-beta` is in maintenance, so `M5` is the only host left:

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
| `lock` | the model is locked |
| `since`, `by`, `note` | who locked it, when, and why |
| `loaded_on` | hosts that have it resident right now |
| `lock_reason` | the sentence `/healthz` shows — only while the refusal is in force |
| `listed` | it appears in `/v1/models` |
| `hosts[].lock` | locked **and** resident on that host |
| `hosts[].offered`, `hosts[].loaded`, `hosts[].policy`, `hosts[].dispatch` | as on the policy rows |

`/healthz` carries the lock on four keys:

| key | meaning |
|---|---|
| `locks` | every locked model id, sorted |
| `models[].lock` | the model is locked |
| `models[].lock_reason` | why requests are refused for its sake; `null` while the lock is not in force |
| `models[].hosts[].lock` | locked **and** loaded on that host — the per-node flag |

`/stats` counts the refusals as `requests_model_locked`.

One inconsistency is deliberate and documented as it is: the two write routes
(`POST`/`DELETE …/lock`) answer `403` for a missing or wrong admin key, while
`GET /admin/models` and the rest of `/admin` answer `401`. The operator's
decision is that this state is *forbidden* to everyone but them, not that a
credential is missing. It is one argument (`_require_admin(status=…)`) away from
401 everywhere if that ever reads wrong.

### Undoing it, and what the lock does not outlive

`DELETE /admin/models/{model}/lock` is the whole undo; it is idempotent and
leaves the policy rules in the file alone. When the locked model is unloaded
anyway — oMLX evicted it under pressure from a load RIPPLE did not place, an
operator unloaded it, the node restarted — the lock stays in the file and simply
refuses nothing until the model is resident again. Eviction of *unlocked* models
is unchanged throughout: the lock is a promise about one model, not a general
brake on the eviction logic.

## What this is not

Not a client-visible knob for a single key: a key's own model scope
(`ripple/keys.py`) stays the per-client mechanism. This is fleet state, decided
by the operator, and it needs the admin key.
