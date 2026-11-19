"""Rebind's guards (FA-2, §6.1) around the optimizer's decision (issue #80).

The move itself is decided by `plan_rebind` in `tests/test_placement.py`: relative
load picks the source, `Δtotal` picks the destination. This file is about the
vetoes — the reasons a move that looks good on paper still does not happen.

Two of them changed meaning with #80 and are worth stating:

* the old rule compared *capability* against *capability* (`if dst_tps <= src_tps:
  continue`), so it could only ever move work from the strong host to the weak
  one — and it never fired. `Δtotal` replaces it, and a destination that cannot
  improve the fleet's aggregate is refused with the number that said so;
* the decision left the request path. The requesting thread is busy by
  definition, so a move there threw its work away or blocked it. The optimizer
  asks on a timer, about *idle* threads only.
"""

from __future__ import annotations

import time

import pytest

from ripple.config import OptimizerConfig, PlacementConfig, RebindConfig

from tests.support import make_config
from tests.test_placement import (M3_CURVE, M5_CURVE, _FakeFleet, cfg, host,
                                  idle_thread, registry_with)


def _plan(hosts, reg, **rebind):
    from ripple.placement import plan_rebind
    return plan_rebind(_FakeFleet(hosts), cfg(**rebind), reg)


# ---------------------------------------------------------------------------
# Price and curve
# ---------------------------------------------------------------------------
def test_no_recorded_prompt_size_is_no_move() -> None:
    """`last_prompt_tokens == 0` means the price is unknown, not zero.

    The registry sorts such a thread last; the decider refuses it outright,
    because `0 / pp_tps = 0 s` would look like a free move.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(idle_thread("unknown", "M5", tokens=0))

    plan = _plan([m5, m3], reg)

    assert not plan.moves
    assert plan.reason == "price_unknown"


def test_a_move_that_costs_more_than_the_slot_has_left_is_refused() -> None:
    """Paying 200 s of prefill for a slot that expires in 30 s is a loss.

    The thread is idle for 1 770 s of its 1 800 s TTL, so 30 s are left; 200 000
    tokens at 1 000 tok/s cost 200 s. Both numbers are in the reason.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5, pp=1000.0)
    reg = registry_with(idle_thread("dying", "M5", tokens=200_000, idle=1770.0))

    plan = _plan([m5, m3], reg)

    assert not plan.moves
    assert plan.reason == "cost_exceeds_ttl"
    assert "200" in (plan.detail or "") and "30" in (plan.detail or "")


def test_a_destination_without_pp_tps_is_never_a_destination() -> None:
    """#81: `default_pp_tps: 0` means "not measured", not "infinitely fast".

    Without a prefill rate the cost cannot be computed, so the host is skipped —
    and with no other destination the reason is `no_target`.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5, pp=0.0)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)])

    plan = _plan([m5, m3], reg)

    assert not plan.moves
    assert plan.reason == "no_target"


def test_a_cold_load_is_not_a_destination() -> None:
    """A model that has to be loaded first costs the load *plus* the prefill."""
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5, loaded=False)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)])

    plan = _plan([m5, m3], reg)

    assert not plan.moves
    assert plan.reason == "no_target"


def test_a_busy_destination_is_not_a_destination() -> None:
    """`inflight >= spill_threshold` — the node is full from here on (FA-5)."""
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5, inflight=4, spill=4)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)])

    plan = _plan([m5, m3], reg)

    assert not plan.moves
    assert plan.reason == "no_target"


def test_a_destination_without_a_free_slot_is_not_a_destination() -> None:
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5, max_threads=1)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)],
                        idle_thread("filler", "M3"))

    plan = _plan([m5, m3], reg)

    assert not plan.moves
    assert plan.reason == "no_target"


# ---------------------------------------------------------------------------
# Cooldown, budget, batch, disabled
# ---------------------------------------------------------------------------
def test_a_thread_inside_its_cooldown_does_not_move_and_a_fresh_one_does() -> None:
    """`per_thread_cooldown_seconds` is per thread, not per host.

    The thread that paid a prefill 120 s ago stays where it is; the other idle
    thread on the same host may still move — the cooldown protects the one that
    just moved, it does not freeze the fleet.
    """
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    just_moved = idle_thread("just_moved", "M5", last_rebind=time.time() - 120)
    fresh = idle_thread("fresh", "M5", tokens=5_000)
    rest = [idle_thread(f"t{i}", "M5", tokens=6_000) for i in range(3)]
    reg = registry_with(just_moved, fresh, *rest)

    plan = _plan([m5, m3], reg, per_thread_cooldown_seconds=600.0)

    assert plan.moves
    assert plan.thread_key == "fresh", "the thread in its cooldown is not a candidate"


def test_only_a_thread_in_cooldown_means_no_move() -> None:
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(idle_thread("just_moved", "M5", last_rebind=time.time() - 120))

    plan = _plan([m5, m3], reg, per_thread_cooldown_seconds=600.0)

    assert not plan.moves
    assert plan.reason == "no_target"


def test_the_hourly_budget_stops_flapping() -> None:
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)])
    for i in range(4):                       # four moves in the last hour
        reg._rebinds.append(time.time() - 60 * i)

    plan = _plan([m5, m3], reg, max_per_hour=4)

    assert not plan.moves
    assert plan.reason == "budget"


def test_a_batch_thread_is_never_moved() -> None:
    """Its TTL is 120 s: a full prefill could not be repaid inside it."""
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(idle_thread("batch", "M5", kind="batch"))

    plan = _plan([m5, m3], reg)

    assert not plan.moves
    assert plan.reason == "batch_no_rebind"


def test_the_optimizer_switched_off_is_no_move() -> None:
    """`placement.optimizer.enabled: false` — silent, and for a stated reason."""
    m5 = host("M5", curve=M5_CURVE, tg=18.3)
    m3 = host("M3", curve=M3_CURVE, tg=43.5)
    reg = registry_with(*[idle_thread(f"t{i}", "M5") for i in range(5)])
    conf = make_config([], placement=PlacementConfig(
        rebind=RebindConfig(), optimizer=OptimizerConfig(enabled=False)))

    from ripple.placement import plan_rebind
    plan = plan_rebind(_FakeFleet([m5, m3]), conf, reg)

    assert not plan.moves
    assert plan.reason == "disabled"


def test_the_min_gain_and_the_safety_factor_still_hold() -> None:
    """A gain that does not beat the prefill by `safety_factor` is not a move.

    5 000 tokens at 1 000 tok/s cost 5.0 s; the safety factor 1.5 wants 7.5 s of
    gain, and the destination's rate is only marginally better than the source's.
    """
    m5 = host("M5", curve={1: 40.0, 2: 38.0, 3: 37.0}, tg=37.0)
    m3 = host("M3", curve={1: 41.0, 2: 39.0}, tg=41.0, pp=1000.0)
    reg = registry_with(*[idle_thread(f"t{i}", "M5", tokens=5_000) for i in range(3)])

    plan = _plan([m5, m3], reg)

    assert not plan.moves
    assert plan.reason in ("min_gain", "cost_not_repaid"), plan.reason


@pytest.mark.parametrize("reason", ["price_unknown", "no_curve", "no_target",
                                    "delta_non_positive", "batch_no_rebind"])
def test_the_reason_vocabulary_is_what_the_tests_speak(reason: str) -> None:
    """The words are part of the contract: `healthz`, logs and tests share them."""
    from ripple.placement import RebindPlan
    assert RebindPlan(reason=reason).reason == reason
