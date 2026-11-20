"""Binding and admission (FA-2, FA-5, FA-12, FA-18, FA-19; §6.1).

Two decisions are kept apart on purpose:

* **Binding** — *where to*: which node gets a new thread. Deficit rule,
  consistency hash or least threads. Happens once per thread.
* **Admission** — *whether at all*: may this node take it right now. Slots,
  capacity, residency, health. Re-checked before every dispatch, each gate with
  a veto.

Both use `HostRuntime.in_pool` as the only health predicate: a host that missed
one or two probes (`degraded`) is still usable, and the decision then carries the
reason `degraded_host` so the fallback is never silent. Only `down` excludes.

A veto does not abort the request, it moves on to the next candidate; only when
every candidate vetoes is the request rejected. A thread that is already bound
is never rebound because of a veto — moving it would cost the prefix cache,
which is exactly what this architecture exists to prevent.

One veto is not negotiable: a **locked model** (issue #21) is never evicted, so a
host that would have to drop one is out of the candidate set entirely, and the
request is only refused when no host is left — with the reason `model_locked`,
which names the model and the host it protects.

A third decision lives here since issue #80: `plan_rebind` is the **optimizer's**
verdict about moving an *idle* thread between hosts. It is not taken in the
request path — the thread a request is being admitted for is busy by definition,
so a move there either throws its work away or blocks it. It is asked on a timer
(`placement.optimizer`) and decides on measured numbers: the host with the highest
relative load gives, the host with the highest `Δtotal` takes, and no curve means
no move.

A fourth decision lives here since issue #95: `decide_migration` is asked at the
**arrival** of a turn, before the prompt is prefilled anywhere. The sweep only
ever sees idle threads; the arriving turn is the one moment a move can still be
paid for, so the same two numbers are weighed against the turns that will follow
(`turns_left × gain_per_turn > cost × safety_factor + band`). It is a pure
function — no clock, no I/O, no logging — and the three hysteresis mechanisms
that keep it from thrashing (an observed band, a return factor, a per-thread
pin) are all inside it, each with the reason it exists written where it lives.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from .config import Config
from .hosts import Fleet, HostRuntime
from .registry import Thread, ThreadRegistry

# Admission tiers (FA-18)
TIER_RESIDENT = 1
TIER_COLD_LOAD = 2
TIER_EVICT_IDLE = 3
TIER_CONFLICT = 4
TIER_NO_SLOT = 5
TIER_FULL = 6

TIER_REASON = {
    TIER_RESIDENT: "resident",
    TIER_COLD_LOAD: "cold_load",
    TIER_EVICT_IDLE: "evict_idle",
    TIER_CONFLICT: "conflict",
    TIER_NO_SLOT: "no_slot",
    TIER_FULL: "max_inflight",
}


@dataclass
class Candidate:
    host: HostRuntime
    tier: int
    evicts: list[str] = field(default_factory=list)
    note: str | None = None
    # Models this placement would evict that the operator locked (issue #21).
    # Non-empty means the host is *out*, not that it is a last resort: a locked
    # model is never evicted, so the tier is `conflict` whatever else is true.
    locked: list[str] = field(default_factory=list)
    # A finished conversation on this host that may give up its slot for the
    # request in hand. Never a thread with a request in flight.
    evict_thread: Thread | None = None

    @property
    def reason(self) -> str:
        return TIER_REASON.get(self.tier, "unknown")


@dataclass
class Decision:
    host: HostRuntime | None
    reason: str
    candidates: list[Candidate] = field(default_factory=list)
    error_status: int | None = None
    error_message: str | None = None
    note: str | None = None
    evict_thread: Thread | None = None
    # The prefill bias actually applied in this decision, per host, in slots
    # (only hosts that were penalised appear). None = no bias was in play.
    bias: dict[str, float] | None = None


# --------------------------------------------------------------------------
# Residency and eviction forecast (FA-12, FA-18)
# --------------------------------------------------------------------------
def residency_tier(host: HostRuntime, model: str, cfg: Config,
                   registry: ThreadRegistry) -> Candidate:
    """Forecast what oMLX would do, using its own arithmetic — not the measured
    usage: `estimated_size` is what oMLX itself calculates with.

    A resident model is tier `resident` only while the host's FA-12 preference
    is on. With `hosts[].prefer_resident: false` the host keeps taking part, but
    as an ordinary cold-load candidate: being warm is then not a reason to send
    the request there, and the deficit decides. The model is already loaded, so
    no bytes have to be found for it and no eviction is forecast — the guard
    below stays exactly as it is for every other host.
    """
    info = host.models.get(model)
    resident = info is not None and info.loaded
    if resident and host.cfg.prefer_resident:
        return Candidate(host=host, tier=TIER_RESIDENT)
    if resident:
        return Candidate(host=host, tier=TIER_COLD_LOAD,
                         note="resident_not_preferred")

    # Without residency/capacity signals there is no forecast — say so by
    # staying on the availability tier instead of pretending.
    if not host.has("residency") or not host.guard_enabled:
        return Candidate(host=host, tier=TIER_COLD_LOAD,
                         note="no_eviction_forecast")
    if info is None or info.estimated_size is None:
        return Candidate(host=host, tier=TIER_COLD_LOAD, note="size_unknown")
    if not host.ceiling_bytes:
        return Candidate(host=host, tier=TIER_COLD_LOAD, note="no_ceiling")

    loaded = [m for m in host.models.values() if m.loaded]
    target = host.cfg.hard_threshold * host.ceiling_bytes
    forecast = sum(m.estimated_size or 0 for m in loaded) + info.estimated_size
    if forecast <= target:
        return Candidate(host=host, tier=TIER_COLD_LOAD)

    # Which models would oMLX evict? LRU order, until the forecast fits.
    ordered = sorted(loaded, key=lambda m: m.last_access or 0.0)
    evicts: list[str] = []
    remaining = forecast
    for m in ordered:
        if remaining <= target:
            break
        evicts.append(m.id)
        remaining -= m.estimated_size or 0

    # The lock (issue #21) comes before every other veto: a locked model is not
    # a preference that another candidate can outbid, it is a promise that
    # RIPPLE will not take that memory away. The host is therefore unusable for
    # *this* model, and `choose()` only reports it if no host is left.
    locked = [m.id for m in ordered
              if m.id in evicts and host.policy is not None
              and host.policy.locked(m.id)]
    if locked:
        return Candidate(host=host, tier=TIER_CONFLICT, evicts=evicts,
                         locked=locked,
                         note=f"would evict locked {', '.join(locked)}")

    for m in ordered:
        if m.id not in evicts:
            continue
        busy = registry.slots(host.cfg.name, m.id) > 0
        if m.pinned or busy:
            return Candidate(host=host, tier=TIER_CONFLICT, evicts=evicts,
                             note=f"would evict {'pinned' if m.pinned else 'active'} {m.id}")
    return Candidate(host=host, tier=TIER_EVICT_IDLE, evicts=evicts)


# --------------------------------------------------------------------------
# Binding
# --------------------------------------------------------------------------
def _deficit(cand: Candidate, cfg: Config, registry: ThreadRegistry,
             model: str, fleet: Fleet) -> float:
    """How far below its target share a host is, in slots — with running work
    counted for as long as it runs (issue #14).

    Two corrections to the plain thread count:

    * **Running work is load.** `inflight` and not only the slot/ephemeral
      count: an ephemeral request leaves the 60 s window while it is still
      generating, and a 225 s generation on the weak node then looked like an
      idle host — so the *next* request went there too, and the weak node
      collected conversations it could not serve in time. `max` and not `+`:
      a bound thread with a request in flight is already 1.0 in
      `weighted_slots`, and the same work must not be counted twice. `inflight`
      is per host, not per model: a host that is busy with another model is
      busy for this one too — the GPU is the shared resource.
    * **One decision places one thread.** A host that is many threads behind
      may claim at most one thread's worth of deficit
      (`on_node + 1.0`): one placement closes one thread, and a host that is
      far behind must not outbid a faster idle one by more than that. With the
      two hosts of this fleet the cap never changes the outcome — the algebra
      shows a host can only exceed one slot of deficit when the other host's
      score is already negative — but the share term must not be able to grow
      without bound as threads accumulate on the other node.
    """
    host = cand.host
    decay = cfg.placement.thread_decay_seconds
    active = 0.0
    on_node = 0.0
    for h in fleet.hosts:
        # Idle threads fade out (weighted_slots): a conversation that has been
        # silent for minutes must not keep pulling new threads onto the other
        # host. Ephemeral short requests count fully inside their 60 s window.
        slots = (registry.weighted_slots(h.cfg.name, model, decay)
                 + registry.ephemeral_count(h.cfg.name, model))
        slots = max(slots, float(h.inflight))
        active += slots
        if h.cfg.name == host.cfg.name:
            on_node = slots
    share = cfg.target_share(host.cfg)
    claim = min(share * (active + 1), on_node + 1.0)
    return claim - on_node


def _ring_position(key: str) -> int:
    return int(hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:8], 16)


def _policy_decision(model: str, blocked: list[HostRuntime]) -> Decision:
    """503 for a model the operator switched off on every host that offers it.

    Not a 404: the model exists and `/v1/models` may still list it (a `disabled`
    model is deliberately still offered). Not `no_healthy_host` either — every
    host here is healthy; the operator said no. The message has to be one a
    human can act on, so it names the hosts, their levels and the endpoint that
    undoes it.
    """
    detail = ", ".join(
        f"{h.cfg.name}={(h.policy.level(h.cfg.name, model) if h.policy else None) or 'disabled'}"
        for h in blocked)
    hidden_everywhere = all(h.policy is not None and h.policy.hides(h.cfg.name, model)
                            for h in blocked)
    reason = "model_hidden" if hidden_everywhere else "model_disabled"
    return Decision(
        host=None, reason=reason, error_status=503,
        error_message=(f"model {model!r} is switched off by the operator on every host "
                       f"that offers it ({detail}); re-enable it with "
                       f"DELETE /admin/nodes/<node>/models/{model}"))


def _locked_decision(model: str, cands: list[Candidate]) -> Decision:
    """503 for a request whose only hosts would have to evict a locked model.

    Not `conflict`: the operator did not ask for this request to fail, they asked
    for one model to stay resident, and the message has to say which model on
    which host is holding the memory — plus the endpoint that releases it. This
    is the only path that can produce `model_locked`, and it is only reached
    after every other host was tried (`choose()` drops a locked host before it
    ranks the rest).
    """
    detail = "; ".join(
        f"{', '.join(c.locked)} on {c.host.cfg.name}" for c in cands if c.locked)
    locked_names = sorted({name for c in cands for name in c.locked})
    undo = ", ".join(f"DELETE /admin/models/{name}/lock" for name in locked_names)
    return Decision(
        host=None, reason="model_locked", candidates=cands, error_status=503,
        error_message=(f"model {model!r} needs memory that is held by a locked model "
                       f"({detail}); no other host can take it without evicting one. "
                       f"Unlock it with {undo}"))


def _prefill_bias(cand: Candidate, considered: list[Candidate], cfg: Config,
                  prompt_tokens: int) -> float:
    """What the cold prefill of *this* prompt would cost on `cand`, in slots.

    Decode is 1,3 : 1 between the two Macs and already sits in the weights;
    prefill is 3,6–4,0 : 1 and sits nowhere. So a long context placed on the
    weaker node pays minutes of prefill that the deficit never saw:

        t_h     = prompt_tokens / pp_tps_h
        bias_h  = −min(max_slots, max(0, t_h − t_fastest) / slot_seconds)

    Only the fastest candidate of the set actually under consideration gets 0.0.
    The bias is never positive (no host is ever rewarded for being fast), never
    applies with fewer than two known prefill rates (a single known rate has
    nothing to be relative to), and is 0.0 for a host without `pp_tps` — unknown
    is not the same as fast. Measured rates: `docs/measurements.md`.
    """
    pb = cfg.placement.prefill_bias
    if not pb.enabled or prompt_tokens < pb.min_prompt_tokens:
        return 0.0
    if pb.slot_seconds <= 0:
        return 0.0
    own = cand.host.cfg.pp_tps
    rates = [c.host.cfg.pp_tps for c in considered if c.host.cfg.pp_tps]
    if not own or len(rates) < 2:
        return 0.0
    fastest = min(prompt_tokens / float(r) for r in rates if r)
    extra = prompt_tokens / own - fastest
    if extra <= 0:
        return 0.0
    return -min(pb.max_slots, extra / pb.slot_seconds)


def _consistent_hash_choice(cands: list[Candidate], thread_key: str) -> Candidate:
    ring: list[tuple[int, int]] = []
    for idx, c in enumerate(cands):
        for v in range(160):
            h = hashlib.sha256(f"{c.host.cfg.name}:{v}".encode()).hexdigest()[:8]
            ring.append((int(h, 16), idx))
    if not ring:
        return cands[0]
    ring.sort()
    pos = _ring_position(thread_key)
    for point, idx in ring:
        if point >= pos:
            return cands[idx]
    return cands[ring[0][1]]


def imbalance(cands: list[Candidate], registry: ThreadRegistry, cfg: Config,
              model: str) -> dict:
    """Hysteresis check (FA-2): two thresholds, both must be exceeded.

    In v1 this is a *reported* measure, not a gate: binding only ever places
    **new** threads, and the deficit rule must run for every new thread or the
    distribution would freeze at the start state (5 threads → 3:2 only if every
    new thread is placed by deficit). Re-placement of bound threads — what the
    hysteresis from vllm-router actually protects — does not exist here.
    """
    norm = []
    for c in cands:
        h = c.host
        load = (registry.slots(h.cfg.name, model)
                + registry.ephemeral_count(h.cfg.name, model))
        weight = h.cfg.weight or 1.0
        norm.append(load / weight)
    if len(norm) < 2:
        return {"imbalanced": False, "spread": 0.0, "ratio": 1.0}
    lo, hi = min(norm), max(norm)
    spread = hi - lo
    ratio = (hi / lo) if lo > 0 else float("inf")
    imbalanced = (spread > cfg.placement.balance_abs_threshold
                  and ratio > cfg.placement.balance_rel_threshold)
    return {"imbalanced": imbalanced, "spread": round(spread, 3),
            "ratio": (round(ratio, 3) if ratio != float("inf") else None)}


def _admin_key_hint(hosts: list[HostRuntime]) -> str:
    """The one operator fix behind an unreadable list that answered 401/403.

    Only appended for an auth failure: a timeout or a broken body has a
    different cause, and pointing at the key there would send the operator to
    the wrong place.
    """
    if any((h.models_error or "").startswith(("401", "403")) for h in hosts):
        return "; check replication.admin_key"
    return ""


def choose(fleet: Fleet, cfg: Config, registry: ThreadRegistry, model: str, *,
           thread_key: str = "", exclude: frozenset[str] = frozenset(),
           kind: str | None = None, holds_slot: bool = True,
           prompt_tokens: int = 0) -> Decision:
    """Pick a host for a new thread (binding) or a request (admission).

    `kind` is the traffic class of the calling key and only changes *whom* an
    eviction takes: an interactive request (a person waiting) drops a finished
    batch thread before it drops another conversation. `holds_slot=False` (a
    one-shot request) skips the slot gate entirely — such a request never takes a
    slot, so a full host is not full for it and nothing has to be evicted.

    `prompt_tokens` is the size of *this* request's prompt (estimated by the
    caller). It only feeds the prefill bias of the deficit rule: a long context
    is charged the extra cold prefill it would need on a slower host. 0 — the
    default — means "size unknown" and changes nothing.
    """
    # The allow-list is part of the contract, not decoration (§5.1): a host may
    # only serve the models it is configured for, otherwise the eviction
    # forecast from FA-18 is built on a model it was never meant to hold.
    offered = [h for h in fleet.hosts if h.cfg.enabled and h.model_available(model)]
    if not offered:
        # The operator switched the model off (issue #11). That is neither a
        # health problem nor an unknown model: the model exists, it is on
        # purpose not being served. `hidden` and `disabled` both reach here
        # because `model_available()` is the one predicate for both — the
        # difference is only what `/v1/models` shows and which reason the
        # client gets.
        switched_off = [h for h in fleet.hosts
                        if h.cfg.enabled and h.cfg.model_allowed(model)
                        and model in h.models and not h.model_available(model)]
        if switched_off:
            return _policy_decision(model, switched_off)
        unhealthy = [h for h in fleet.hosts
                     if not h.in_pool or not h.identity_ok]
        if unhealthy:
            detail = ", ".join(f"{h.cfg.name}={h.status}"
                               + ("" if h.identity_ok else "/identity_mismatch")
                               for h in unhealthy)
            return Decision(host=None, reason="no_healthy_host", error_status=503,
                            error_message=f"model {model!r} is not available: {detail}")
        # No host offers it — but that is only a fact about the model where a
        # host actually read its list (issue #53). A node that could not tell us
        # what it carries (a 401 from a missing admin key, a timeout) is not a
        # node that lacks the model, and a 404 would tell the client the model
        # does not exist: a permanent answer to an operator problem, so clients
        # give up instead of retrying after the key is fixed.
        unreadable = [h for h in fleet.hosts
                      if h.cfg.enabled and h.model_permitted(model)
                      and h.model_list_unreadable]
        if unreadable:
            detail = "; ".join(
                f"{h.cfg.name}'s model list is unreadable "
                f"({h.models_error or 'never read'})" for h in unreadable)
            return Decision(host=None, reason="model_list_unavailable",
                            error_status=503,
                            error_message=f"model {model!r} cannot be placed: {detail}"
                                          + _admin_key_hint(unreadable))
        return Decision(host=None, reason="unknown_model", error_status=404,
                        error_message=f"model {model!r} is not offered by any host")

    pool = [h for h in offered if h.in_pool and h.cfg.name not in exclude]
    if not pool:
        if exclude:
            return Decision(host=None, reason="no_candidate", error_status=503,
                            error_message="no further host available for retry")
        return Decision(host=None, reason="no_healthy_host", error_status=503,
                        error_message=f"no healthy host offers {model!r}")

    cands: list[Candidate] = []
    for h in pool:
        cand = residency_tier(h, model, cfg, registry)
        if cand.tier < TIER_CONFLICT:
            # A one-shot request (`holds_slot=False`) reserves nothing: the slot
            # limit does not apply to it and nothing is dropped for it. Only
            # max_inflight can still say no.
            if holds_slot and registry.slots_on_host(h.cfg.name) >= h.cfg.max_threads:
                # All slots taken — by whom? A conversation that finished minutes
                # ago is worth less than the request in hand: it gives up its
                # slot instead of the request being refused. A thread with a
                # request in flight is never a candidate, so no running request
                # loses its prefix cache. An interactive request prefers a
                # finished batch thread over another conversation (FA-18/FA-19).
                spare = registry.evictable(
                    h.cfg.name, model,
                    prefer_kind="batch" if kind == "interactive" else None)
                if spare is None:
                    cand = Candidate(host=h, tier=TIER_NO_SLOT,
                                     note=f"{registry.slots_on_host(h.cfg.name)}/{h.cfg.max_threads} slots")
                else:
                    cand = Candidate(host=h, tier=cand.tier, evicts=cand.evicts,
                                     evict_thread=spare,
                                     note=(f"slots full ({registry.slots_on_host(h.cfg.name)}"
                                           f"/{h.cfg.max_threads}), evicts idle {spare.label}"))
            elif h.inflight >= h.cfg.max_inflight:
                cand = Candidate(host=h, tier=TIER_FULL,
                                 note=f"{h.inflight}/{h.cfg.max_inflight} inflight")
        cands.append(cand)

    usable = [c for c in cands if c.tier < TIER_CONFLICT]
    # Eviction is the last resort, not the first choice: a host that can take the
    # thread without dropping anything wins, even if that means a cold load on
    # another node (FA-12 keeps its meaning — a warm node with a free slot beats a
    # heavier cold one). Only when *every* candidate would have to drop a thread
    # is one dropped, instead of answering 503.
    free = [c for c in usable if c.evict_thread is None]
    if free:
        usable = free
    if not usable:
        # A locked model is the one refusal that is not a "no host can take it
        # right now": the operator ruled the eviction out. Reported by its own
        # reason, before the generic conflict, so the client and `/stats` can
        # tell "everything is busy" from "a promise is in the way".
        locked_hits = [c for c in cands if c.locked]
        if locked_hits:
            return _locked_decision(model, locked_hits)
        detail = "; ".join(
            f"{c.host.cfg.name}: {c.reason}"
            + (f" ({c.note})" if c.note else "")
            + (f" would evict {', '.join(c.evicts)}" if c.evicts else "")
            for c in cands)
        status = 503
        return Decision(host=None, reason="conflict", candidates=cands,
                        error_status=status,
                        error_message=f"no host can take {model!r} without evicting a running "
                                      f"thread — {detail}")

    # FA-5: prefer hosts below the spill threshold; if none is, take the one
    # with the largest relative headroom (inflight+1)/weight.
    open_cands = [c for c in usable if c.host.inflight < c.host.cfg.spill_threshold]
    spill = False
    if open_cands:
        considered = open_cands
    else:
        spill = True
        best = min(usable, key=lambda c: (c.host.inflight + 1) / (c.host.cfg.weight or 1.0))
        considered = [best]

    # Admission tiers stay authoritative: a resident node beats a cold one.
    best_tier = min(c.tier for c in considered)
    considered = [c for c in considered if c.tier == best_tier]

    strategy = cfg.placement.binding_strategy
    biases: dict[str, float] = {}
    if strategy == "consistent_hash" and thread_key:
        chosen = _consistent_hash_choice(considered, thread_key)
    elif strategy == "least_threads":
        chosen = min(considered, key=lambda c: (
            registry.slots(c.host.cfg.name, model)
            + registry.ephemeral_count(c.host.cfg.name, model),
            -c.host.cfg.weight, c.host.inflight))
    else:
        scores = {c.host.cfg.name: _deficit(c, cfg, registry, model, fleet)
                  for c in considered}
        # The prefill bias belongs to the deficit only: consistent_hash and
        # least_threads do not weigh the deficit at all, so a penalty in slots
        # would mean nothing to them.
        biases = {c.host.cfg.name: _prefill_bias(c, considered, cfg, prompt_tokens)
                  for c in considered}
        for name, penalty in biases.items():
            scores[name] += penalty
        chosen = max(considered, key=lambda c: (
            scores[c.host.cfg.name], c.host.cfg.weight, -c.host.inflight))

    note = None
    if spill:
        note = "all hosts at spill_threshold"
    if chosen.tier == TIER_EVICT_IDLE:
        note = (note + "; " if note else "") + f"evicts idle {', '.join(chosen.evicts)}"
    applied = {name: round(penalty, 2) for name, penalty in biases.items() if penalty}
    if applied:
        # FA-14: a placement that was decided by prefill cost must say so.
        note = (note + "; " if note else "") + "prefill bias " + ", ".join(
            f"{name} {penalty:.2f}" for name, penalty in sorted(applied.items()))
    reason = chosen.reason
    if chosen.evict_thread is not None:
        # FA-14: the operator must see that a conversation was dropped for this.
        reason = "slot_evict"
    if len(offered) == 1 and chosen.tier == TIER_RESIDENT and chosen.evict_thread is None:
        # FA-4/AS-8: a model only one host offers is pinned there by definition
        reason = "model_pinned"
    if (chosen.host.status == "degraded" and not chosen.host.healthy
            and reason not in ("slot_evict", "model_pinned")):
        # The host missed one or two probes but is still in the pool. Serving
        # from it is the point of the flap guard, so the fallback must not be
        # silent: `slot_evict`/`model_pinned` win because they say something
        # stronger about *this* request. A host whose only degradation is the
        # switched-off eviction guard (`healthy` true) is not this case — that is
        # a reporting note, not a failed probe.
        reason = "degraded_host"
        fleet.stats.degraded_pool_decisions += 1
    return Decision(host=chosen.host, reason=reason, candidates=cands, note=note,
                    evict_thread=chosen.evict_thread, bias=applied or None)


# --------------------------------------------------------------------------
# A weight change and the threads already running (issue #78)
# --------------------------------------------------------------------------
def _live_threads(registry: ThreadRegistry) -> list[Thread]:
    """The threads a restart or a re-placement would actually disturb.

    Expired threads are skipped because they are about to be purged anyway;
    `lookup` removes them on the next request, so counting them here would name
    conversations that no longer exist.
    """
    return [t for t in list(registry.threads.values())
            if t.host and not registry.expired(t)]


def _selection(fleet: Fleet, cfg: Config, registry: ThreadRegistry,
               threads: list[Thread]) -> dict[str, str | None]:
    """Where the selection function would place each of these conversations now.

    One call of `choose` per thread, with the thread's own key so a
    `consistent_hash` fleet answers with the same host it would have given the
    conversation in the first place. `None` means "no host could take it".
    """
    out: dict[str, str | None] = {}
    for t in threads:
        decision = choose(fleet, cfg, registry, t.model, thread_key=t.key,
                          kind=t.kind, holds_slot=t.holds_slot,
                          prompt_tokens=t.last_prompt_tokens)
        out[t.key] = decision.host.cfg.name if decision.host is not None else None
    return out


def threads_that_would_move(fleet: Fleet, cfg: Config, registry: ThreadRegistry,
                            weights: dict[str, float]) -> int:
    """How many live conversations a weight change would place differently.

    The issue's own argument for asking first: a weight that shifts under a
    running thread pulls it away from its prefix cache. Existing threads are
    never moved by a weight change — they keep their host until they end — so
    the honest number to show before the click is the counterfactual: *if the
    new split were applied to the conversations running now, how many would
    land somewhere else?*

    `weights` maps host name to the weight it would have; every other host keeps
    the weight in force. The comparison is `choose` under the current weights
    against `choose` under the new ones, for the same load — the deficit maths,
    the strategy, the tiers and the gates are the selection function's, not a
    second copy of them here. A thread counts only when both answers exist and
    differ: a conversation no host could take is not moved by a weight.

    The weights are swapped on the live `HostConfig`s for the duration of one
    synchronous pass and restored in a `finally`. `choose` is synchronous and
    the event loop does not interleave it, so no request ever observes the
    intermediate state.
    """
    threads = _live_threads(registry)
    if not threads:
        return 0
    hosts = {h.cfg.name: h.cfg for h in fleet.hosts}
    before = _selection(fleet, cfg, registry, threads)
    # `choose` counts a degraded pick on the fleet's stats; a preview must not.
    degraded = fleet.stats.degraded_pool_decisions
    previous: dict[str, float] = {}
    try:
        for name, weight in weights.items():
            host = hosts.get(name)
            if host is not None:
                previous[name] = host.weight
                host.weight = float(weight)
        after = _selection(fleet, cfg, registry, threads)
    finally:
        for name, weight in previous.items():
            hosts[name].weight = weight
        fleet.stats.degraded_pool_decisions = degraded
    return sum(1 for key, host in after.items()
               if host is not None and before.get(key) is not None and host != before[key])


# --------------------------------------------------------------------------
# The optimizer's decision (issue #80)
# --------------------------------------------------------------------------
@dataclass
class RebindPlan:
    """Which idle thread moves where, what it buys, and what it costs.

    `delta_total` is the fleet-wide verdict in tok/s (`None` when the move could
    not be evaluated — no curve, no price), `gain_seconds`/`cost_seconds` are
    the per-thread seconds that the guards compare. `reason` is the decision's
    word; `ok` means a move.
    """

    src: str | None = None
    dst: str | None = None
    thread_key: str | None = None
    model: str | None = None
    delta_total: float | None = None
    gain_seconds: float = 0.0
    cost_seconds: float = 0.0
    reason: str = "no_target"
    detail: str | None = None
    # The thread itself, so the caller can move it without a second lookup.
    thread: Thread | None = None

    @property
    def moves(self) -> bool:
        return self.dst is not None and self.thread_key is not None


def _curve_rates(curve: dict[float, float],
                 levels: tuple[int, ...]) -> tuple[dict[int, float] | None, int | None]:
    """The curve's rates at exactly these levels, or `None` and the missing one.

    No interpolation and no extrapolation: `rate_h(n)` exists where it was
    measured, and a level without a point makes the move unevaluable rather than
    approximate (issue #80).
    """
    rates: dict[int, float] = {}
    for level in levels:
        value = curve.get(float(level))
        if value is None:
            return None, level
        rates[level] = float(value)
    return rates, None


def _delta_total(src_curve: dict[float, float], dst_curve: dict[float, float],
                 n: int, m: int) -> tuple[float | None, str | None]:
    """`[(m+1)·rate_dst(m+1) + (n−1)·rate_src(n−1)] − [m·rate_dst(m) + n·rate_src(n)]`.

    Both sides are the fleet's **aggregate** decode rate before and after one
    thread moves, so batching is in the number: a host that loses per-stream
    speed while gaining in aggregate is not overloaded, and the formula says so.

    `n` is the source's streams including the thread that would leave, `m` the
    destination's before it arrives. Returns `(None, "no_curve <level>")` when a
    needed point is missing — never a guessed rate.
    """
    # A level at which **no** stream runs contributes `0 · rate` and needs no
    # point: `m = 0` is an idle destination, not a missing measurement, and the
    # same holds for `n = 1` (the host empties). Every level at which a stream
    # *does* run after the move must have been measured — `m + 1` always, `n − 1`
    # only when it is ≥ 1.
    src_levels = (n, n - 1) if n > 1 else (n,)
    src_rates, missing_src = _curve_rates(src_curve, src_levels)
    if src_rates is None:
        return None, f"no_curve src n={missing_src}"
    dst_levels = (m, m + 1) if m > 0 else (m + 1,)
    dst_rates, missing_dst = _curve_rates(dst_curve, dst_levels)
    if dst_rates is None:
        return None, f"no_curve dst m={missing_dst}"
    before = (m * dst_rates[m] if m > 0 else 0.0) + n * src_rates[n]
    after = ((m + 1) * dst_rates[m + 1]
             + ((n - 1) * src_rates[n - 1] if n > 1 else 0.0))
    return round(after - before, 1), None


def plan_rebind(fleet: Fleet, cfg: Config, registry: ThreadRegistry, *,
                now: float | None = None) -> RebindPlan:
    """Which idle thread should move, and where to — the fleet-wide question.

    Not "may *this* thread move" any more: the requesting thread is busy by
    definition, so a decision in the request path either threw its work away or
    blocked it. This runs on a timer (`placement.optimizer`) and answers the two
    halves of the operator's question:

    * **source** — the host with the highest `relative_load(model)` that still
      holds idle threads. Scored on the model of its *cheapest* idle thread,
      because that is the thread it would give away first; a host whose model has
      no curve cannot be scored and is not a source.
    * **destination** — the host with the highest `Δtotal`, not "the other one".
      With three nodes a pairwise rule picks the wrong machine, and the marginal
      question is what the number answers.

    The candidate is the cheapest idle thread first (`registry.idle_threads`
    orders by prompt size, then turns): a move costs one full re-prefill, so the
    cheap one is tried first and an expensive one only when the cheap ones cannot
    move the fleet forward.
    A thread whose prompt size was never recorded is not moved (`price_unknown`)
    — a price of 0 would look free.

    Every guard stays: per-thread cooldown and hourly budget from
    `placement.rebind`, `min_gain_seconds`, `safety_factor`, no cold load on the
    destination, `inflight < spill_threshold`, free slots, and a priceable
    `pp_tps` on the destination (#81: a host without one is never a destination).
    `Δtotal ≤ 0` is a decision too, and the reason names the number.
    """
    rb = cfg.placement.rebind
    opt = cfg.placement.optimizer
    now = now if now is not None else time.time()
    if not opt.enabled:
        return RebindPlan(reason="disabled", detail="placement.optimizer.enabled is false")
    if not rb.enabled:
        return RebindPlan(reason="disabled", detail="placement.rebind.enabled is false")
    used = registry.rebinds_last_hour(now)
    if rb.max_per_hour and used >= rb.max_per_hour:
        return RebindPlan(reason="budget", detail=f"{used}/{rb.max_per_hour} per hour")

    cooldown = rb.per_thread_cooldown_seconds
    min_idle = (rb.min_idle_seconds if rb.min_idle_seconds is not None
                else cfg.placement.thread_decay_seconds)

    # -- the source: the most relatively loaded host that has something to give.
    scored: list[tuple[float, HostRuntime, list[Thread]]] = []
    for host in fleet.hosts:
        if not host.cfg.enabled or not host.in_pool:
            continue
        idle = registry.idle_threads(host.cfg.name, now, cooldown_seconds=cooldown)
        if not idle:
            continue
        load = host.relative_load(idle[0].model)
        if load is None:
            continue
        scored.append((load, host, idle))
    if not scored:
        # Either nothing is idle anywhere, or no host that has an idle thread has
        # a measured curve. Both are "no move", and the reason says which.
        idle_anywhere = any(
            registry.idle_threads(h.cfg.name, now, cooldown_seconds=cooldown)
            for h in fleet.hosts if h.cfg.enabled and h.in_pool)
        if idle_anywhere:
            return RebindPlan(reason="no_curve",
                              detail="no host with idle threads has a measured curve")
        # Nothing movable, but why: a batch thread is throughput without anyone
        # waiting for its prefix cache, and `rebind` is off for it everywhere.
        batch = [t for t in registry.threads.values()
                 if t.holds_slot and t.kind == "batch" and t.active == 0
                 and not registry.expired(t, now)]
        if batch:
            return RebindPlan(reason="batch_no_rebind",
                              detail="only batch threads are idle")
        return RebindPlan(reason="no_target", detail="no idle thread anywhere")

    # Highest load wins; fleet order breaks a tie, so two scans of the same fleet
    # decide the same way.
    scored.sort(key=lambda row: -row[0])
    _load, src, candidates = scored[0]

    reason = "no_target"
    detail: str | None = None
    # The last Δtotal that was actually computed, so a refusal can name its
    # number instead of staying silent (`Δtotal ≤ 0` is a measurement).
    verdict: float | None = None
    for thread in candidates:
        model = thread.model
        if thread.kind == "batch":
            reason, detail = "batch_no_rebind", "batch TTL too short to amortise a prefill"
            continue
        if thread.active > 0:
            reason, detail = "in_flight", "a request of this thread is running"
            continue
        idle = now - thread.last_seen
        if idle < min_idle:
            reason, detail = "too_recent", f"idle {idle:.0f}s < {min_idle:.0f}s"
            continue
        if thread.last_rebind and now - thread.last_rebind < cooldown:
            reason, detail = "cooldown", f"moved {now - thread.last_rebind:.0f}s ago"
            continue
        tokens = int(thread.last_prompt_tokens or 0)
        if tokens <= 0:
            reason, detail = "price_unknown", "no recorded prompt size"
            continue

        src_curve = src.curve(model=model)
        n = registry.slots(src.cfg.name, model)
        remaining = registry.ttl_of(thread) - idle

        best: tuple[float, HostRuntime, float, float] | None = None
        for host in fleet.hosts:
            if host.cfg.name == src.cfg.name or not host.cfg.enabled:
                continue
            if not host.in_pool or not host.identity_ok:
                continue
            if not host.model_available(model):
                continue
            info = host.models.get(model)
            if info is None or not info.loaded:
                # A cold load on top of the prefill never pays — and #81's rule
                # holds unchanged: only a resident model is a destination.
                continue
            if host.inflight >= host.cfg.spill_threshold:
                continue
            if registry.slots_on_host(host.cfg.name) >= host.cfg.max_threads:
                continue
            pp_tps = host.cfg.pp_tps or rb.default_pp_tps
            if not pp_tps:
                # No price model, no move: an unmeasured prefill cannot be
                # repaid, and "0 tok/s" would read as infinite cost.
                continue
            cost = tokens / float(pp_tps)
            if cost > remaining:
                reason = "cost_exceeds_ttl"
                detail = f"cost {cost:.1f}s > {remaining:.0f}s left on the slot"
                continue
            m = registry.slots(host.cfg.name, model)
            delta, why = _delta_total(src_curve, host.curve(model=model), n, m)
            if delta is None:
                reason, detail = "no_curve", why
                continue
            if verdict is None:
                verdict = delta
            if delta <= 0:
                reason = "delta_non_positive"
                detail = f"delta_total {delta:+.1f} tok/s"
                continue
            # What the move buys *this* thread's next answer: its own rate on
            # both machines, at the concurrency each would run.
            src_rates, _ = _curve_rates(src_curve, (n,))
            dst_rates, _ = _curve_rates(host.curve(model=model), (m + 1,))
            assert src_rates is not None and dst_rates is not None  # Δ was evaluable
            gain = rb.assumed_completion_tokens * (
                1.0 / src_rates[n] - 1.0 / dst_rates[m + 1])
            if gain < rb.min_gain_seconds:
                reason = "min_gain"
                detail = f"gain {gain:.1f}s < {rb.min_gain_seconds:.1f}s"
                continue
            if gain < cost * rb.safety_factor:
                reason = "cost_not_repaid"
                detail = f"gain {gain:.1f}s < {cost:.1f}s × {rb.safety_factor:g}"
                continue
            if best is None or delta > best[0]:
                best = (delta, host, gain, cost)

        if best is None:
            continue
        delta, dst, gain, cost = best
        return RebindPlan(src=src.cfg.name, dst=dst.cfg.name, thread_key=thread.key,
                          model=model, delta_total=delta,
                          gain_seconds=round(gain, 1), cost_seconds=round(cost, 1),
                          reason="ok", detail=f"{src.cfg.name}->{dst.cfg.name}",
                          thread=thread)

    return RebindPlan(reason=reason, detail=detail, delta_total=verdict)



def admit(fleet: Fleet, host: HostRuntime, model: str) -> tuple[bool, str | None]:
    """Admission for an already bound thread (§6.1): health and hard capacity.
    No rebinding — a veto here means 503 or fallback, never a move."""
    if not host.cfg.enabled:
        return False, "host disabled"
    if not host.in_pool:
        return False, "host not in pool"
    if not host.model_available(model):
        # The policy (issue #11) is part of `model_available()`; say which one it
        # was, because "not offered" reads like an oMLX fact and a disabled model
        # is an operator decision.
        level = host.policy.level(host.cfg.name, model) if host.policy else None
        if level:
            return False, f"model {model!r} is {level} on {host.cfg.name}"
        return False, f"model not offered by {host.cfg.name}"
    if host.inflight >= host.cfg.max_inflight:
        return False, f"{host.cfg.name} at max_inflight ({host.inflight}/{host.cfg.max_inflight})"
    return True, None


# --------------------------------------------------------------------------
# The arriving turn (issue #95, §6.4)
# --------------------------------------------------------------------------
# The sweep above asks a fleet-wide question on a timer and only about threads
# nobody is waiting for. The turn that is arriving now is the other half of the
# question, and the only moment its answer can still be acted on: the prompt is
# about to be prefilled in full, wherever it lands, and `prompt_tokens` is in
# hand. So the same two numbers are asked here — `_delta_total` for the fleet
# verdict, `prompt_tokens / pp_tps(dst)` for the prefill — and the prefill is
# amortised over the turns that will follow:
#
#     turns_left × gain_per_turn > cost × safety_factor + band
#
# Without the multiplication every short turn looks like a win and the fleet
# re-prefills 40 k-token contexts for one answer each. With it the shape of the
# conversation decides, which is why `turns_left` is the number to distrust.
#
# Everything below is pure: the caller passes the clock (`now_s`) and plain
# values, and nothing here reads a curve, a file, a registry or a log.


@dataclass(frozen=True)
class MigrateHost:
    """One node as the arriving-turn decision sees it — numbers, not objects.

    A pure decision cannot ask a live host for its curve or a registry for its
    stream count, so those answers are collected once, by `fleet_view`, and the
    decision reads nothing else. `threads` is `m` in the sweep's arithmetic (the
    streams of **this model** already on the node), `on_host` the node's whole
    thread count, which is what `hosts[].max_threads` limits.
    """

    name: str
    pp_tps: float = 0.0            # prefill price; 0 = not measured, never infinite
    tg_tps: float = 0.0            # what the node delivers now, for the log line
    curve: dict[float, float] = field(default_factory=dict)   # rate_h(n), this model
    threads: int = 0               # `m`: streams of this model already here
    on_host: int = 0               # every thread on the node, any model
    capacity: int = 0              # `hosts[].max_threads`; 0 = no limit known
    inflight: int = 0
    spill_threshold: int = 0       # 0 = no spill limit
    in_pool: bool = True           # healthy, identity verified, model offered
    resident: bool = True          # the model is loaded here: a cold load never pays


@dataclass(frozen=True)
class MigrateFleet:
    """The fleet, as values, plus the `Δtotal` samples the band is measured on.

    `delta_history` is the recent history of the same `Δtotal` the sweep
    computes — the fleet-wide verdict in tok/s, one sample per scan, oldest
    first. It is an argument and not a store on purpose: the band is a property
    of the measurements, and a pure function cannot go looking for them.
    """

    hosts: dict[str, MigrateHost] = field(default_factory=dict)
    delta_history: tuple[float, ...] = ()


@dataclass(frozen=True)
class ThreadState:
    """The conversation, as values: where it is, how long it has run, what it has
    already spent on moves.

    `last_turn_at` is the arrival time of the previous turn when the caller has
    it; `created` is the fallback that makes a mean interval computable at all.
    `ttl_remaining_s` is `registry.ttl_of(thread)` minus the time already idle —
    the slot's remaining life, which is the horizon `turns_left` is bounded by.
    """

    host: str
    turns: int = 1
    created: float | None = None
    last_turn_at: float | None = None
    ttl_remaining_s: float | None = None
    moved_from: str | None = None
    moved_at: float = 0.0
    pinned_until: float = 0.0
    move_count: int = 0
    kind: str = "interactive"


@dataclass(frozen=True)
class MigrationDecision:
    """`host` is where the arriving turn should go: the destination when the
    reason is `moved`, the current host in every other case (a refusal does not
    move anything). `priced` carries the numbers the decision used, so a log
    line and a test can both show the arithmetic instead of the verdict alone.
    """

    host: str
    reason: str
    priced: dict[str, float | str | None] = field(default_factory=dict)
    detail: str | None = None

    @property
    def moves(self) -> bool:
        return self.reason == "moved"


#: How informative a refusal is when several candidates refused for different
#: reasons. The first one that got furthest into the arithmetic is reported: a
#: candidate that failed on price says more than one that had no curve at all.
_REFUSAL_RANK = {
    "cost_exceeds_ttl": 0,
    "no_repayment": 1,
    "cooldown": 2,
    "gain_below_band": 3,
    "delta_non_positive": 4,
    "no_curve": 5,
}


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile of a small sample (no numpy here).

    `p` in [0, 1]; position `(n−1)·p` between the sorted neighbours, which is
    the definition every statistics package uses by default and the only one
    that needs no assumption about the distribution.
    """
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _band_seconds(history: tuple[float, ...], band_samples: int,
                  fallback_s: float) -> tuple[float, str]:
    """The deadband in seconds, and where it came from — mechanism (a).

    *Why it exists.* `Δtotal` is a difference of two **fitted** curves at
    neighbouring levels: measured, not exact, and no constant is right for every
    model pair. A move must beat `cost × safety_factor` by more than the width
    of that noise, or the decision is reading the fit, not the fleet.

    *Why the interquartile range and not 2σ.* The samples are a difference of
    fits, so a single bad scan is ordinary; a standard deviation squares it, so
    one outlier widens the band until nothing ever moves — the failure mode of a
    guard that is supposed to be sensitive. The IQR discards the tails and
    reports the width of the ordinary noise, which is exactly the quantity the
    band wants. With `band_samples: 5` both are defined; only one survives an
    outlier.

    *Why it cannot speak early.* Before `band_samples` samples exist there is no
    spread to measure, and inventing one would be worse than a constant: the
    decision falls back to `min_gain_seconds` and says `band="unavailable"` in
    `priced`, so a log line never claims a measured band it did not have.
    """
    samples = list(history or ())
    if band_samples > 0:
        samples = samples[-band_samples:]
    if band_samples <= 0 or len(samples) < band_samples or len(samples) < 2:
        return float(fallback_s), "unavailable"
    iqr = _percentile(samples, 0.75) - _percentile(samples, 0.25)
    return max(0.0, round(iqr, 2)), "iqr"


def _turns_left(state: ThreadState, now_s: float) -> tuple[int, float | None]:
    """How many turns a move can still repay — **the weakest number here**.

    It is an estimate, and the only one in the decision that is not measured, so
    it is bounded three ways and the smallest bound wins:

    1. **the thread's history** — `turns` turns have happened. Assuming more
       remain than have happened is invention, and this bound alone is what
       makes a first or second turn almost never worth a move.
    2. **the pace** — `(now − created) / turns` is the mean interval between
       turns (the wait for the first turn included, so it errs long, which errs
       *towards* moving). The slot can only hold `ttl_remaining / interval`
       more turns at that pace.
    3. **the TTL remainder** — the horizon bound 2 divides by. A slot that
       expires in 90 s cannot repay ten turns at 60 s each, whatever the history
       says; a slot that outlives the conversation does not raise bound 1.

    The floor is 1: the arriving turn is the one that is certain, and a move
    that cannot repay even that is refused by `no_repayment` — a named number —
    rather than by a count of zero.
    """
    turns = max(int(state.turns), 1)
    start = state.created if state.created is not None else state.last_turn_at
    interval: float | None = None
    if start is not None and now_s > start:
        interval = (now_s - start) / turns
    bounds = [turns]
    if interval and interval > 0 and state.ttl_remaining_s is not None:
        bounds.append(int(max(0.0, state.ttl_remaining_s) / interval))
    return max(1, min(bounds)), interval


def _price_move(*, src: MigrateHost, dst: MigrateHost, tokens: int, turns_left: int,
                band_s: float, safety_factor: float, return_factor: float,
                ttl_remaining: float | None,
                assumed_completion_tokens: int) -> tuple[str | None, str | None, dict]:
    """Price one destination. `(None, None, priced)` means the move passes.

    The arithmetic is the sweep's, unchanged: `cost = prompt_tokens / pp_tps(dst)`
    and `gain_per_turn = 1/rate_src(n) − 1/rate_dst(m+1)` at
    `assumed_completion_tokens`. What is new is the multiplier — the benefit is
    earned on every turn that follows, so it is `turns_left × gain_per_turn`
    against a threshold that carries the band and, for a return, the factor.
    """
    priced: dict[str, float | str | None] = {"dst": dst.name}
    if dst.pp_tps <= 0:
        # No price model, no move: an unmeasured prefill cannot be repaid, and
        # "0 tok/s" would read as infinite cost.
        return "no_curve", f"no pp_tps on {dst.name}", priced
    cost = tokens / float(dst.pp_tps)
    priced["cost_s"] = round(cost, 2)
    if ttl_remaining is not None and cost > ttl_remaining:
        return ("cost_exceeds_ttl",
                f"cost {cost:.1f}s > {ttl_remaining:.0f}s left on the slot", priced)

    n = max(int(src.threads), 1)     # streams on the source, this thread included
    m = max(int(dst.threads), 0)     # streams on the destination before it arrives
    delta, why = _delta_total(src.curve, dst.curve, n, m)
    if delta is None:
        return "no_curve", why, priced
    priced["delta_total"] = delta
    if delta <= 0:
        # The fleet verdict is not a gain for the fleet, whatever this one thread
        # would gain: batching is inside `Δtotal` and it says so.
        return "delta_non_positive", f"delta_total {delta:+.1f} tok/s", priced

    src_rates, _ = _curve_rates(src.curve, (n,))
    dst_rates, _ = _curve_rates(dst.curve, (m + 1,))
    assert src_rates is not None and dst_rates is not None   # Δtotal was evaluable
    gain = assumed_completion_tokens * (1.0 / src_rates[n] - 1.0 / dst_rates[m + 1])
    priced["gain_per_turn_s"] = round(gain, 3)
    if gain <= 0:
        return "no_repayment", f"no gain: {gain:+.2f}s per answer", priced

    benefit = turns_left * gain
    base = cost * safety_factor + band_s
    threshold = base * return_factor
    priced.update({"benefit_s": round(benefit, 2), "threshold_s": round(threshold, 2),
                   "band_s": round(band_s, 2), "turns_left": float(turns_left),
                   "return_factor": return_factor})
    if benefit <= cost * safety_factor:
        return ("no_repayment",
                f"{turns_left} turns × {gain:.2f}s = {benefit:.1f}s < "
                f"{cost:.1f}s × {safety_factor:g}", priced)
    if benefit <= threshold:
        if return_factor > 1.0:
            # Mechanism (b) refused it: the bar was raised because this is a
            # move back to the host the thread just left. The vocabulary has no
            # `return_factor` reason, and `cooldown` is what the raised bar is:
            # the same move inside the cooldown window, priced higher.
            return ("cooldown",
                    f"back to {dst.name}: {benefit:.1f}s < {base:.1f}s × "
                    f"{return_factor:g}", priced)
        # Mechanism (a) refused it: it clears the cost, not the noise.
        return ("gain_below_band",
                f"{benefit:.1f}s < {cost:.1f}s × {safety_factor:g} + band "
                f"{band_s:.1f}s", priced)
    return None, None, priced


def decide_migration(*, cfg: Config, thread_state: ThreadState, fleet: MigrateFleet,
                     prompt_tokens: int, now_s: float,
                     assumed_completion_tokens: int = 400,
                     moves_last_hour: int = 0,
                     max_moves_per_hour: int = 0) -> MigrationDecision:
    """Should the arriving turn stay on `thread_state.host`, or move — and where?

    Pure: `now_s` is an argument, `fleet` and `thread_state` are values, nothing
    is written and nothing is logged. The same inputs decide the same way twice,
    which is what makes the numbers in `priced` testable.

    The guards run in the order that lets the reason name the real obstacle:

    1. `disabled` / `batch_no_rebind` / `budget` — the decision is not allowed to
       run at all (the fleet budget is passed in: `rebind.max_per_hour` bounds
       the fleet, the pin below bounds the conversation).
    2. `pinned` — mechanism (c): this conversation has already spent its moves.
    3. per candidate: `no_curve` → `cost_exceeds_ttl` → `no_curve` (Δtotal) →
       `delta_non_positive` → `no_repayment` → `cooldown`/`gain_below_band`.
    4. `moved` — the highest `Δtotal` among those that cleared every gate.

    `assumed_completion_tokens` is the default 400 from the contract; a caller
    that knows the request's `max_tokens` may pass it, as the sweep does.
    """
    mc = cfg.placement.migrate
    rb = cfg.placement.rebind
    state = thread_state
    tokens = max(0, int(prompt_tokens or 0))

    if not mc.enabled:
        return MigrationDecision(host=state.host, reason="disabled",
                                 detail="placement.migrate.enabled is false")
    if state.kind == "batch":
        # A batch TTL is too short to amortise a prefill, and nobody waits for
        # its prefix cache — the same rule the sweep applies.
        return MigrationDecision(host=state.host, reason="batch_no_rebind",
                                 detail="batch TTL too short to amortise a prefill")
    if max_moves_per_hour and moves_last_hour >= max_moves_per_hour:
        return MigrationDecision(host=state.host, reason="budget",
                                 priced={"moves_last_hour": float(moves_last_hour)},
                                 detail=f"{moves_last_hour}/{max_moves_per_hour} per hour")

    window = float(mc.window_seconds)
    # Mechanism (c): the pin. `pinned_until` is the explicit answer and wins; a
    # thread that has spent `max_moves_per_thread` moves inside a window that is
    # still running is pinned for the rest of *that* window, which the caller
    # then writes back (`priced["pin_until"]`). The count resets with the
    # window: two moves last Tuesday do not pin today.
    if now_s < float(state.pinned_until or 0.0):
        return MigrationDecision(host=state.host, reason="pinned",
                                 priced={"pinned_until": float(state.pinned_until)},
                                 detail=f"pinned for {state.pinned_until - now_s:.0f}s more")
    count = (int(state.move_count)
             if state.moved_at > 0 and (now_s - state.moved_at) < window else 0)
    if count >= mc.max_moves_per_thread:
        derived = state.moved_at + window
        return MigrationDecision(host=state.host, reason="pinned",
                                 priced={"pinned_until": derived,
                                         "move_count": float(count)},
                                 detail=f"{count} moves inside {window:.0f}s")

    src = fleet.hosts.get(state.host)
    if src is None:
        return MigrationDecision(host=state.host, reason="no_curve",
                                 detail=f"host {state.host!r} not in the fleet view")

    turns_left, interval = _turns_left(state, now_s)
    band_s, band_note = _band_seconds(tuple(fleet.delta_history or ()),
                                      mc.band_samples, rb.min_gain_seconds)
    cooldown = float(rb.per_thread_cooldown_seconds)
    # Mechanism (b), the first half: was there a move recently, and does this
    # candidate undo it? Only a return is priced up — a move to a *third* host
    # inside the cooldown is refused outright, because it has no direction the
    # factor could weigh.
    recently_moved = state.moved_at > 0 and (now_s - state.moved_at) < cooldown

    shared: dict[str, float | str | None] = {
        "turns_left": float(turns_left),
        "interval_s": None if interval is None else round(interval, 1),
        "band": band_note,
        "band_s": round(band_s, 2),
        "safety_factor": rb.safety_factor,
        "samples": float(len(fleet.delta_history or ())),
    }

    best: tuple[float, str, dict] | None = None
    refusal: tuple[int, str, str | None, dict] | None = None
    for name, host in sorted(fleet.hosts.items()):
        if name == state.host:
            continue
        if not host.in_pool or not host.resident:
            continue
        if host.spill_threshold and host.inflight >= host.spill_threshold:
            continue
        if host.capacity and host.on_host >= host.capacity:
            continue
        if recently_moved and name != state.moved_from:
            reason, detail, priced = ("cooldown",
                                      f"moved {now_s - state.moved_at:.0f}s ago; "
                                      f"{name} is not where it came from", {})
        else:
            returning = bool(recently_moved and name == state.moved_from)
            reason, detail, priced = _price_move(
                src=src, dst=host, tokens=tokens, turns_left=turns_left,
                band_s=band_s, safety_factor=rb.safety_factor,
                return_factor=(mc.return_factor if returning else 1.0),
                ttl_remaining=state.ttl_remaining_s,
                assumed_completion_tokens=assumed_completion_tokens)
        if reason is None:
            if best is None or priced["delta_total"] > best[0]:
                best = (float(priced["delta_total"]), name, priced)
            continue
        rank = _REFUSAL_RANK.get(reason, 99)
        if refusal is None or rank < refusal[0]:
            refusal = (rank, reason, detail, priced)

    if best is not None:
        delta, name, priced = best
        priced = {**shared, **priced}
        # The move that spends the last of the budget pins the thread for the
        # rest of the window; the caller writes `pin_until` back onto the thread.
        if count + 1 >= mc.max_moves_per_thread:
            priced["pin_until"] = now_s + window
        return MigrationDecision(
            host=name, reason="moved", priced=priced,
            detail=(f"{state.host}->{name}: {turns_left} turns × "
                    f"{priced['gain_per_turn_s']:.2f}s = {priced['benefit_s']:.1f}s > "
                    f"{priced['threshold_s']:.1f}s (Δtotal {delta:+.1f} tok/s)"))

    if refusal is not None:
        _rank, reason, detail, priced = refusal
        return MigrationDecision(host=state.host, reason=reason,
                                 priced={**shared, **priced}, detail=detail)
    return MigrationDecision(host=state.host, reason="no_target",
                             priced=shared,
                             detail="no other host can take the thread")


def fleet_view(fleet: Fleet, cfg: Config, registry: ThreadRegistry, model: str, *,
               delta_history: tuple[float, ...] = ()) -> MigrateFleet:
    """Collect the numbers `decide_migration` needs — the decision's only seam.

    No clock is read and nothing is written: a live `Fleet` and a
    `ThreadRegistry` are read once, into plain values, so the decision never
    holds an object that could change under it between two lines. The gates a
    destination must pass to be a destination at all are `plan_rebind`'s — in
    the pool with identity verified, the model offered and **resident** (a cold
    load on top of a prefill never pays), a free slot, no spill — and a host
    that fails one stays in the view with `in_pool`/`resident` false, so the
    decision can name it instead of pretending it does not exist.
    """
    rb = cfg.placement.rebind
    hosts: dict[str, MigrateHost] = {}
    for host in fleet.hosts:
        info = host.models.get(model)
        hosts[host.cfg.name] = MigrateHost(
            name=host.cfg.name,
            pp_tps=float(host.cfg.pp_tps or rb.default_pp_tps or 0.0),
            tg_tps=float((host.metrics.get(model) or {}).get("tg_tps") or 0.0),
            curve=dict(host.curve(model=model)),
            threads=registry.slots(host.cfg.name, model),
            on_host=registry.slots_on_host(host.cfg.name),
            capacity=int(host.cfg.max_threads or 0),
            inflight=int(host.inflight or 0),
            spill_threshold=int(host.cfg.spill_threshold or 0),
            in_pool=bool(host.cfg.enabled and host.in_pool and host.identity_ok
                         and host.model_available(model)),
            resident=bool(info is not None and info.loaded),
        )
    return MigrateFleet(hosts=hosts,
                        delta_history=tuple(float(x) for x in delta_history))
