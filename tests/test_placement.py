"""The optimizer's decision (issue #80): relative load picks the source, Δtotal
picks the destination.

The two halves of the operator's question are answered by two different numbers,
and this file pins both:

* **who gives** — the host with the highest `relative_load(model)` that still
  holds an idle thread. Not the requesting thread (it is busy by definition),
  not "the other host".
* **who takes** — the host with the highest `Δtotal`, computed from the measured
  concurrency curves of the two hosts, on the model the thread carries. With
  three nodes the pairwise answer is simply wrong.

`Δtotal` is the fleet's aggregate decode rate after the move minus before it, so
batching is inside the number: a host that lost per-stream speed while gaining in
aggregate is loaded correctly and the optimizer stays quiet. That is the
counter-case below, and it is the reason the ratio alone never decides.

Numbers are the ones measured on 18.11.2026 (issue #80): M5 delivers 60.2 tok/s
on one stream and 18.3 per stream on five (91.5 in aggregate, 1.52× the
single-stream rate), M3 43.5 alone.
"""

from __future__ import annotations

import time

from ripple.backends import ModelInfo
from ripple.config import (AffinityConfig, Config, HostConfig, MigrateConfig,
                           PlacementConfig, RebindConfig)
from ripple.hosts import CURVE_MIN_BUCKETS, HostRuntime
from ripple.placement import (MigrateFleet, MigrateHost, ThreadState, _delta_total,
                              decide_migration, fleet_view, plan_rebind)
from ripple.registry import Thread, ThreadRegistry

from tests.support import make_config

MODEL = "m1"

# Measured 18.11.2026. The M5 batches: five streams deliver 91.5 in aggregate,
# 1.52× what one stream gets, although each stream is three times slower.
M5_CURVE = {1: 60.2, 2: 38.0, 3: 28.0, 4: 22.0, 5: 18.3}
M3_CURVE = {1: 43.5, 2: 31.6, 3: 24.0}


class _FakeFleet:
    def __init__(self, hosts: list[HostRuntime]) -> None:
        self.hosts = hosts

    def get(self, name: str) -> HostRuntime | None:
        for h in self.hosts:
            if h.cfg.name == name:
                return h
        return None


def seed_curve(host: HostRuntime, points: dict[int, float], *, model: str = MODEL,
               now: float | None = None) -> None:
    """Give the host a measured curve: `CURVE_MIN_BUCKETS` clean buckets per level.

    One bucket is an anecdote and `curve()` refuses it (that rule is the whole
    point of the minimum), so a test that wants a point has to write as many as
    production would.
    """
    base = time.time() if now is None else now
    # One sample per bucket: a bucket holds *one* concurrency level, so three
    # buckets at the same level need three different 5 s slots.
    step = 0
    for n, rate in sorted(points.items()):
        for _ in range(CURVE_MIN_BUCKETS):
            host.series_ring.add(model, float(n), rate, now=base - 600 + step * 5)
            step += 1


def host(name: str, *, curve: dict[int, float] | None = None, tg: float | None = None,
         pp: float = 1000.0, loaded: bool = True, inflight: int = 0,
         spill: int = 4, max_threads: int = 20) -> HostRuntime:
    cfg = HostConfig(name=name, addresses=[f"http://{name}.test:8000"], weight=1.0,
                     pp_tps=pp, tg_tps=tg, spill_threshold=spill,
                     max_threads=max_threads)
    h = HostRuntime(cfg=cfg, client=None)      # no HTTP on the decision path
    h.healthy = True
    h.status = "healthy"
    h.identity_ok = True
    h.inflight = inflight
    if loaded:
        h.models[MODEL] = ModelInfo(id=MODEL, available=True, loaded=True)
    if curve:
        seed_curve(h, curve)
        # The current per-stream rate: what the node is delivering *now*, which is
        # the numerator of the relative load.
        h.metrics[MODEL] = {"tg_tps": tg if tg is not None else curve[min(curve)],
                            "tg_samples": 9}
    return h


def registry_with(*threads: Thread) -> ThreadRegistry:
    reg = ThreadRegistry(AffinityConfig())
    for t in threads:
        reg.threads[t.key] = t
    return reg


def idle_thread(key: str, on: str, *, tokens: int = 4_000, turns: int = 3,
                idle: float = 60.0, model: str = MODEL, kind: str = "interactive",
                active: int = 0, last_rebind: float = 0.0) -> Thread:
    now = time.time()
    return Thread(key=key, model=model, host=on, prefix="p", source="fingerprint",
                  created=now - 3600, last_seen=now - idle, turns=turns,
                  holds_slot=True, kind=kind, active=active,
                  last_prompt_tokens=tokens, last_rebind=last_rebind)


def cfg(**rebind) -> Config:
    return make_config([], placement=PlacementConfig(
        thread_decay_seconds=300.0, rebind=RebindConfig(**rebind)))


# ---------------------------------------------------------------------------
# 1. The source is the relatively loaded host, and an *idle* thread leaves it
# ---------------------------------------------------------------------------
def test_five_requests_on_one_host_and_the_other_idle_moves_an_idle_thread() -> None:
    """The case from the issue: M5 at five streams, M3 idle.

    Δtotal = [1·rate_M3(1) + 4·rate_M5(4)] − [5·rate_M5(5)]
           = [43.5 + 4·22.0] − [5·18.3] = 131.5 − 91.5 = +40.0 tok/s

    The fleet gains 40 tok/s in aggregate, so one thread goes — the idle one, and
    the report carries the number.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)      # 18.3 of 60.2 → load 0.70
    m3 = host("M3", curve=M3_CURVE, tg=43.5)      # idle: 43.5 of 43.5 → load 0.0
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)])

    plan = plan_rebind(_FakeFleet([m5, m3]), cfg(), reg)

    assert plan.moves, plan.reason
    assert plan.src == "M5", "the relatively loaded host is the source"
    assert plan.dst == "M3"
    assert plan.delta_total == 40.0
    assert plan.reason == "ok"
    moved = plan.thread
    assert moved is not None and moved.host == "M5" and moved.active == 0
    assert "40.0" in (plan.detail or "") or plan.delta_total == 40.0


def test_the_source_is_the_host_with_the_higher_relative_load() -> None:
    """Two hosts hold idle threads; the more relatively loaded one gives.

    M5 delivers 18.3 of its 60.2 single-stream rate (load 0.70), M3 its full 43.5
    (load 0.0) — so M5 is the source even though M3 is the faster machine alone.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)],
                        idle_thread("on_m3", "M3"))

    plan = plan_rebind(_FakeFleet([m5, m3]), cfg(), reg)

    assert plan.src == "M5", "0.70 > 0.00"
    assert plan.moves, plan.reason
    # n = 5, m = 1 → [2·31.6 + 4·22.0] − [1·43.5 + 5·18.3] = 151.2 − 135.0
    assert plan.delta_total == 16.2
    assert plan.dst == "M3"


def test_a_thread_with_a_request_in_flight_never_moves() -> None:
    """The candidate list is idle threads: a running request is work, not a price."""
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(idle_thread("busy", "M5", active=1))

    plan = plan_rebind(_FakeFleet([m5, m3]), cfg(), reg)

    assert not plan.moves
    assert plan.reason == "no_target", "the busy thread is not even a candidate"


# ---------------------------------------------------------------------------
# 2. No curve, no move — never an invented rate
# ---------------------------------------------------------------------------
def test_a_destination_without_a_curve_is_no_curve() -> None:
    """M3 has no measured concurrency curve: the move cannot be priced.

    The rate is *not* guessed from `tg_tps` (the old rule did exactly that, and
    migrated a thread onto an idle M3 that then delivered 3.9 tok/s).
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=None, tg=43.5)
    reg = registry_with(idle_thread("t0", "M5"))

    plan = plan_rebind(_FakeFleet([m5, m3]), cfg(), reg)

    assert not plan.moves
    assert plan.reason == "no_curve"
    assert plan.delta_total is None
    assert "1" in (plan.detail or ""), "the missing level is named"


def test_a_source_without_a_curve_is_no_curve() -> None:
    m5 = host("M5", curve=None, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(idle_thread("t0", "M5"))

    plan = plan_rebind(_FakeFleet([m5, m3]), cfg(), reg)

    assert not plan.moves
    assert plan.reason == "no_curve"


def test_a_missing_m_plus_one_point_is_no_curve_not_an_extrapolation() -> None:
    """M3's curve stops at n = 1; the move would need `rate_M3(2)`.

    Extrapolating past the measured levels is exactly what the contract forbids —
    the destination's second stream is the one that is not measured.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve={1: 43.5}, tg=43.5)
    # One thread already bound on M3 → m = 1 → the move needs rate_M3(2).
    reg = registry_with(idle_thread("t0", "M5"), idle_thread("on_m3", "M3"))

    plan = plan_rebind(_FakeFleet([m5, m3]), cfg(), reg)

    assert not plan.moves
    assert plan.reason == "no_curve"
    assert "m=2" in (plan.detail or "")


# ---------------------------------------------------------------------------
# 3. Δtotal ≤ 0 → no move, and the reason names the number
# ---------------------------------------------------------------------------
def test_delta_non_positive_keeps_a_correctly_loaded_host_working() -> None:
    """The counter-case the issue demands: no move even though the split looks
    lopsided.

    M5 at five streams delivers 91.5 tok/s in aggregate; its fifth stream still
    *adds* 91.5 − 4·19.0 = 15.5 tok/s to the fleet, because the weights are read
    once for all five streams. An idle node whose first stream gets 10.0 tok/s
    cannot replace that:

    Δtotal = [1·10.0 + 4·19.0] − [5·18.3] = 86.0 − 91.5 = −5.5 tok/s

    The ratio (0.70) proposed M5 as the source — that is all it is allowed to do.
    The number decides, and it decides nothing.
    """
    m5 = host("M5", curve={1: 60.2, 4: 19.0, 5: 18.3}, tg=18.3)
    weak = host("M3", curve={1: 10.0, 2: 11.0}, tg=10.0)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)])

    plan = plan_rebind(_FakeFleet([m5, weak]), cfg(), reg)

    assert not plan.moves
    assert plan.reason == "delta_non_positive"
    assert plan.delta_total == -5.5
    assert "-5.5" in (plan.detail or ""), "the report names the number"


# ---------------------------------------------------------------------------
# 4. The cheapest candidate first
# ---------------------------------------------------------------------------
def test_the_cheap_thread_moves_before_the_expensive_one() -> None:
    """A 3-turn thread with a small prompt beats a 500-turn thread with a huge one.

    Turns are not the price; `last_prompt_tokens` is. The 500-turn conversation
    went through compression and still carries 200k tokens — moving it costs
    200 s of prefill on the destination, the 3-turn one 4 s.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5, pp=1000.0)
    cheap = idle_thread("cheap", "M5", tokens=4_000, turns=3)
    dear = idle_thread("dear", "M5", tokens=200_000, turns=500)
    # The source is genuinely loaded: three more streams, priced above the cheap one.
    rest = [idle_thread(f"t{i}", "M5", tokens=6_000, turns=40) for i in range(3)]
    reg = registry_with(dear, cheap, *rest)      # insertion order must not matter

    plan = plan_rebind(_FakeFleet([m5, m3]), cfg(), reg)

    assert plan.moves
    assert plan.thread_key == "cheap"
    assert plan.cost_seconds == 4.0       # 4 000 / 1 000 tok/s


def test_a_thread_without_a_recorded_prompt_size_is_not_moved() -> None:
    """`last_prompt_tokens == 0` = price unknown. A price of 0 would look free."""
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(idle_thread("unknown", "M5", tokens=0))

    plan = plan_rebind(_FakeFleet([m5, m3]), cfg(), reg)

    assert not plan.moves
    assert plan.reason == "price_unknown"


# ---------------------------------------------------------------------------
# 5. Three hosts: the destination is the highest Δtotal, not the first other
# ---------------------------------------------------------------------------
def test_three_hosts_pick_the_best_delta_not_the_first_other() -> None:
    """A weak idle node sits before the strong one in the fleet list.

    Both can take the thread; the weak one gains 1·25.0, the strong one 1·43.5
    against the source's 4·22.0. The strong node wins on Δtotal even though it is
    the *second* other host.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    weak = host("M3", curve={1: 25.0, 2: 26.0}, tg=25.0)
    strong = host("M2", curve={1: 50.0, 2: 34.0}, tg=50.0)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)])

    plan = plan_rebind(_FakeFleet([m5, weak, strong]), cfg(), reg)

    assert plan.moves, plan.reason
    assert plan.src == "M5"
    assert plan.dst == "M2", "the highest Δtotal, not the first other host"
    # [1·50.0 + 4·22.0] − [5·18.3] = 138.0 − 91.5 = +46.5
    assert plan.delta_total == 46.5


# ---------------------------------------------------------------------------
# The formula itself
# ---------------------------------------------------------------------------
def test_delta_total_matches_the_contract_formula() -> None:
    """`[(m+1)·rate_dst(m+1) + (n−1)·rate_src(n−1)] − [m·rate_dst(m) + n·rate_src(n)]`."""
    src = {1.0: 60.0, 2.0: 40.0, 3.0: 30.0, 4.0: 24.0}
    dst = {1.0: 45.0, 2.0: 32.0, 3.0: 25.0}
    # n = 4, m = 1 → after = 2·32.0 + 3·30.0 = 154.0 ; before = 1·45.0 + 4·24.0 = 141.0
    assert _delta_total(src, dst, 4, 1) == (13.0, None)


def test_delta_total_needs_no_point_at_zero_streams() -> None:
    """`m = 0` is an idle destination: `0 · rate` contributes nothing, so no
    `rate(0)` is invented or required."""
    src = {1.0: 60.0, 2.0: 40.0}
    dst = {1.0: 45.0}
    # n = 2, m = 0 → after = 1·45.0 + 1·60.0 = 105.0 ; before = 2·40.0 = 80.0
    assert _delta_total(src, dst, 2, 0) == (25.0, None)


def test_delta_total_is_none_when_a_needed_level_is_missing() -> None:
    assert _delta_total({1.0: 60.0}, {1.0: 45.0, 2.0: 30.0}, 3, 1) == (None, "no_curve src n=3")
    assert _delta_total({1.0: 60.0, 2.0: 40.0}, {1.0: 45.0}, 2, 1) == (None, "no_curve dst m=2")


# ---------------------------------------------------------------------------
# 6. The arriving turn (issue #95): price stay against move, then amortise it
# ---------------------------------------------------------------------------
# The same two measured numbers as the sweep, asked at the one moment a move can
# still be paid for: the prompt is about to be prefilled in full. The decision
# is `turns_left × gain_per_turn > cost × safety_factor + band`, and every test
# below is pure — a fixed `NOW`, plain values, no sleeps, no clock.
NOW = 1_000_000.0

# M3 alone 43.5 tok/s (the level the source runs at in most cases below); the
# deeper levels exist so a source with streams can be priced too.
M3_CURVE = {1: 43.5, 2: 31.6, 3: 24.0, 4: 19.0}

# Δtotal has not moved for five scans: the band is 0 s, so the arithmetic below
# is the contract's, with nothing hidden in a deadband.
STEADY = (16.7, 16.7, 16.7, 16.7, 16.7)


def migrate_cfg(**migrate) -> Config:
    return make_config([], placement=PlacementConfig(
        rebind=RebindConfig(safety_factor=1.5, min_gain_seconds=2.0,
                            per_thread_cooldown_seconds=600.0,
                            assumed_completion_tokens=400),
        migrate=MigrateConfig(**migrate)))


def mhost(name: str, *, curve: dict[int, float] | None = None, pp: float = 1000.0,
          threads: int = 0, on_host: int | None = None, capacity: int = 20,
          inflight: int = 0, spill: int = 4, in_pool: bool = True,
          resident: bool = True) -> MigrateHost:
    return MigrateHost(name=name, pp_tps=pp,
                       curve={float(k): v for k, v in (curve or {}).items()},
                       threads=threads,
                       on_host=threads if on_host is None else on_host,
                       capacity=capacity, inflight=inflight, spill_threshold=spill,
                       in_pool=in_pool, resident=resident)


def two_nodes(*, curve: dict[int, float] | None = None, **dst) -> MigrateFleet:
    """A one-stream M3 and an idle M5 that is 38 % faster on one stream."""
    return MigrateFleet(hosts={"M3": mhost("M3", curve=M3_CURVE, threads=1),
                               "M5": mhost("M5", curve=M5_CURVE if curve is None else curve,
                                           **dst)},
                        delta_history=STEADY)


def tstate(*, turns: int, host: str = "M3", interval: float = 60.0,
           ttl: float | None = 3600.0, **kw) -> ThreadState:
    """A conversation that has run `turns` turns, one every `interval` seconds."""
    return ThreadState(host=host, turns=turns, created=NOW - turns * interval,
                       ttl_remaining_s=ttl, **kw)


def decide(cfg: Config, state: ThreadState, fleet: MigrateFleet,
           tokens: int = 20_000, **kw):
    return decide_migration(cfg=cfg, thread_state=state, fleet=fleet,
                            prompt_tokens=tokens, now_s=NOW, **kw)


def test_the_arriving_turn_is_priced_with_hand_computed_numbers() -> None:
    """The arithmetic, spelled out — every number below is asserted.

    M3 delivers 43.5 tok/s on one stream, the idle M5 60.2, so one 400-token
    answer is `400·(1/43.5 − 1/60.2) = 2.551 s` faster on the M5. The prompt is
    20 000 tokens and the M5 prefills at 1 000 tok/s: `cost = 20 s`, and the
    threshold is `20 × 1.5 + 0 = 30 s`. Twelve turns repay `12 × 2.551 = 30.61 s`,
    which is over 30 and therefore a move; the band is 0 s because Δtotal has
    been steady for five scans, and Δtotal itself is `60.2 − 43.5 = +16.7 tok/s`.
    """
    d = decide(migrate_cfg(), tstate(turns=12), two_nodes())

    assert d.moves, d.detail
    assert d.host == "M5"
    assert d.priced["cost_s"] == 20.0
    assert d.priced["gain_per_turn_s"] == 2.551
    assert d.priced["turns_left"] == 12.0
    assert d.priced["threshold_s"] == 30.0
    assert d.priced["benefit_s"] == 30.61
    assert d.priced["delta_total"] == 16.7
    assert d.priced["band_s"] == 0.0 and d.priced["band"] == "iqr"


def test_a_short_turn_on_a_long_prompt_is_not_repaid() -> None:
    """The prefill storm: one turn, 40 k tokens, 40 s of prefill.

    The move buys 2.55 s once and costs 40 s — `no_repayment`, and the same
    answer for every single-turn request whatever the fleet says. This is the
    case the multiplication exists for.
    """
    d = decide(migrate_cfg(), tstate(turns=1), two_nodes(), tokens=40_000)

    assert not d.moves
    assert d.reason == "no_repayment"
    assert d.host == "M3"
    assert d.priced["cost_s"] == 40.0
    assert d.priced["turns_left"] == 1.0
    assert d.priced["benefit_s"] == 2.55


def test_the_boundary_between_a_move_and_no_repayment_is_one_turn() -> None:
    """Twelve turns repay 30.61 s > 30 s; eleven repay 28.06 s and do not.

    Same fleet, same prompt, same curves — only `turns` changes. This is the
    number the whole amortisation argument turns on, so it is pinned to a turn.
    """
    fleet = two_nodes()

    moved = decide(migrate_cfg(), tstate(turns=12), fleet)
    refused = decide(migrate_cfg(), tstate(turns=11), fleet)

    assert moved.moves and moved.priced["benefit_s"] == 30.61
    assert not refused.moves
    assert refused.reason == "no_repayment"
    assert refused.priced["benefit_s"] == 28.06
    assert refused.priced["threshold_s"] == 30.0


def test_the_band_is_the_observed_spread_and_a_gain_under_it_is_refused() -> None:
    """Mechanism (a): five Δtotal samples spanning 10…18 tok/s → IQR 4.0 s.

    The threshold becomes `20 × 1.5 + 4.0 = 34 s`. Twelve turns give 30.61 s:
    over the cost (30), under the noise (34) → `gain_below_band`. Fourteen turns
    give 35.71 s and clear both.
    """
    fleet = MigrateFleet(hosts=two_nodes().hosts,
                         delta_history=(10.0, 12.0, 14.0, 16.0, 18.0))

    below = decide(migrate_cfg(), tstate(turns=12), fleet)
    above = decide(migrate_cfg(), tstate(turns=14), fleet)

    assert below.priced["band_s"] == 4.0 and below.priced["band"] == "iqr"
    assert below.priced["threshold_s"] == 34.0
    assert not below.moves
    assert below.reason == "gain_below_band", "clears the cost, not the noise"
    assert above.moves and above.priced["benefit_s"] == 35.71


def test_two_samples_are_not_a_band_and_the_floor_stands_in() -> None:
    """The band cannot speak early: `band="unavailable"`, `min_gain_seconds` 2.0.

    Two scans give no spread worth trusting, so the decision says so in `priced`
    rather than inventing one, and the constant floor makes the threshold
    `20 × 1.5 + 2.0 = 32 s`. Twelve turns (30.61 s) then fall short — a refusal
    that would not have happened with a measured band of 0.
    """
    fleet = MigrateFleet(hosts=two_nodes().hosts, delta_history=(10.0, 18.0))

    d = decide(migrate_cfg(), tstate(turns=12), fleet)

    assert d.priced["band"] == "unavailable"
    assert d.priced["band_s"] == 2.0
    assert d.priced["threshold_s"] == 32.0
    assert not d.moves and d.reason == "gain_below_band"


def test_a_move_back_needs_twice_the_gain_and_the_factor_is_what_stops_it() -> None:
    """Mechanism (b): A→B, then B→A one minute later.

    The thread sits on a five-stream M5 (18.3 tok/s) having just come from the
    M3 (43.5 alone): three turns buy `3 × 12.663 = 37.99 s` against a cost of
    20 s × 1.5 = 30 s, so the plain calculus says move back. With
    `return_factor` 2.0 the bar is 60 s and it is refused (`cooldown`); with
    1.0 the same attempt moves — which is what makes the factor, not the
    arithmetic, the thing that stopped it.
    """
    fleet = MigrateFleet(hosts={"M5": mhost("M5", curve=M5_CURVE, threads=5),
                                "M3": mhost("M3", curve=M3_CURVE, threads=0)},
                         delta_history=STEADY)
    state = tstate(turns=3, host="M5", moved_from="M3", moved_at=NOW - 60)

    guarded = decide(migrate_cfg(), state, fleet)
    plain = decide(migrate_cfg(return_factor=1.0), state, fleet)

    assert guarded.priced["delta_total"] == 40.0
    assert guarded.priced["benefit_s"] == 37.99
    assert not guarded.moves and guarded.reason == "cooldown"
    assert guarded.priced["threshold_s"] == 60.0
    assert plain.moves, "the counter-test: the same attempt, no factor"
    assert plain.priced["threshold_s"] == 30.0
    assert plain.priced["return_factor"] == 1.0


def test_a_thread_that_spent_its_moves_is_pinned_until_the_window_passes() -> None:
    """Mechanism (c): two moves inside the hour pin the conversation.

    The same inputs that move an unpinned thread (`test_the_arriving_turn_…`)
    are refused with `pinned` while the count stands, and the pin the caller has
    to write back is named: the last move plus the window. Once the window has
    passed the count resets and the thread moves again — and the move that
    spends the last of the budget carries its own `pin_until`.
    """
    fleet = two_nodes()
    spent = tstate(turns=12, move_count=2, moved_at=NOW - 100)

    pinned = decide(migrate_cfg(), spent, fleet)
    after = decide(migrate_cfg(), tstate(turns=12, move_count=2, moved_at=NOW - 3700), fleet)
    # One move 700 s ago: outside the 600 s cooldown, inside the 3 600 s window.
    last = decide(migrate_cfg(), tstate(turns=12, move_count=1, moved_at=NOW - 700), fleet)
    explicit = decide(migrate_cfg(),
                      tstate(turns=12, pinned_until=NOW + 500), fleet)

    assert not pinned.moves and pinned.reason == "pinned"
    assert pinned.priced["pinned_until"] == NOW - 100 + 3600.0
    assert not explicit.moves and explicit.reason == "pinned"
    assert after.moves, "the window has passed: the count resets"
    assert last.moves and last.priced["pin_until"] == NOW + 3600.0


def test_disabled_never_moves() -> None:
    d = decide(migrate_cfg(enabled=False), tstate(turns=12), two_nodes())

    assert not d.moves
    assert d.reason == "disabled"
    assert d.host == "M3"


def test_every_other_reason_names_the_obstacle() -> None:
    """The rest of the vocabulary, each on the input that produces it."""
    # The slot expires before the prefill is done: 20 s of cost, 10 s of TTL.
    ttl = decide(migrate_cfg(), tstate(turns=12, ttl=10.0), two_nodes())
    # A destination without a curve cannot be priced.
    no_curve = decide(migrate_cfg(), tstate(turns=12),
                      two_nodes(curve={}))
    # Equal curves: the fleet gains nothing, whatever one thread would.
    flat = decide(migrate_cfg(), tstate(turns=12),
                  MigrateFleet(hosts={"M3": mhost("M3", curve=M3_CURVE, threads=1),
                                      "M5": mhost("M5", curve={1: 43.5})},
                               delta_history=STEADY))
    # The fleet's hourly budget, passed in from `placement.rebind`.
    budget = decide(migrate_cfg(), tstate(turns=12), two_nodes(),
                    moves_last_hour=4, max_moves_per_hour=4)
    # A batch TTL is too short to amortise a prefill.
    batch = decide(migrate_cfg(), tstate(turns=12, kind="batch"), two_nodes())
    # No other host at all.
    alone = decide(migrate_cfg(), tstate(turns=12),
                   MigrateFleet(hosts={"M3": mhost("M3", curve=M3_CURVE, threads=1)}))

    assert ttl.reason == "cost_exceeds_ttl"
    assert no_curve.reason == "no_curve"
    assert flat.reason == "delta_non_positive"
    assert budget.reason == "budget"
    assert batch.reason == "batch_no_rebind"
    assert alone.reason == "no_target"
    for d in (ttl, no_curve, flat, budget, batch, alone):
        assert not d.moves and d.host == "M3"


def test_the_view_collects_the_fleet_without_touching_the_decision() -> None:
    """`fleet_view` is the only seam: live objects in, plain values out.

    The stream count on each node comes from the registry, the curve from the
    host for **this** model, and the decision over the view answers exactly as
    it does over a hand-built one.
    """
    now = time.time()
    m3 = host("M3", curve=M3_CURVE, tg=43.5, pp=1000.0)
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    thread = Thread(key="t", model=MODEL, host="M3", prefix="p", source="fingerprint",
                    created=now - 720, last_seen=now - 1, turns=12,
                    last_prompt_tokens=20_000)
    reg = registry_with(thread)

    view = fleet_view(_FakeFleet([m3, m5]), migrate_cfg(), reg, MODEL,
                      delta_history=STEADY)
    d = decide_migration(cfg=migrate_cfg(),
                         thread_state=ThreadState(host="M3", turns=12,
                                                  created=now - 720,
                                                  ttl_remaining_s=3600.0),
                         fleet=view, prompt_tokens=20_000, now_s=now)

    assert view.hosts["M3"].threads == 1 and view.hosts["M5"].threads == 0
    assert view.hosts["M5"].curve[1.0] == 60.2
    assert view.hosts["M5"].pp_tps == 1000.0
    assert d.moves and d.host == "M5"
