"""The arriving turn in the request path (issue #95, §6.4) — no instance needed.

`tests/test_migrate_acceptance.py` measures the decision; this file checks the
two things the request path adds: that the reason an operator reads is the
*decision's*, not the blanket `thread`, and that the prefill price a destination
is charged comes from **cold** requests only.

The measured hazard (`stats.UsageLedger` rows, live server 19.11.2026): a
700-token prompt on a 702-token cache hit reports `prompt_tokens_per_second`
14.46, the same prompt cold 1254 — the server prices only the uncached
remainder. A rolling mean over both would make every move look free, so
`Ripple.cold_pp_tps` refuses a row that states a cache hit. These tests pin that
rule down with the two numbers from the measurement, and pin the fallback: a
node with no cold row keeps its configured `pp_tps`.
"""

from __future__ import annotations

import logging
import time

from ripple.config import MigrateConfig, PlacementConfig, RebindConfig
from ripple.placement import MigrationDecision
from ripple.proxy import _migration_note

from tests.support import (chat_body, host_config, make_config, prompt_for,
                           stub_model_loaded)


def request_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.name == "ripple.request"]


def _placement(*, enabled: bool = True, pp_tps: float = 0.0) -> PlacementConfig:
    """`placement` with migration on or off; `rebind` off so only #95 can speak."""
    return PlacementConfig(
        rebind=RebindConfig(enabled=False, default_pp_tps=pp_tps),
        migrate=MigrateConfig(enabled=enabled),
    )


async def _two_hosts(stub_factory, ripple_factory, *, enabled: bool, **placement):
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ], placement=_placement(enabled=enabled, **placement)))
    return harness, a, b


async def test_an_unpriceable_fleet_keeps_todays_reason_and_says_why(
        stub_factory, ripple_factory, caplog):
    """`no_curve` is not a reason about the turn, so `thread` stays — the note tells.

    This fleet has no measured curve and no `pp_tps`, so the calculus cannot run
    at all. The dispatch still went to the bound thread's host, `reason=thread`
    is still true, and the `decision=` note on the same line names the obstacle —
    an operator reads *why* without the routing reason becoming a verdict the
    decision never reached.
    """
    caplog.set_level(logging.INFO)
    harness, _a, _b = await _two_hosts(stub_factory, ripple_factory, enabled=True)
    prompt = prompt_for(0)

    first = await harness.ok(chat_body("m1", prompt, turns=2))
    bound = first.headers["x-ripple-host"]
    assert first.headers["x-ripple-reason"] == "new_thread"

    second = await harness.ok(chat_body("m1", prompt, turns=3))
    assert second.headers["x-ripple-host"] == bound, "a refusal must not move the thread"
    assert second.headers["x-ripple-reason"] == "thread"

    line = request_lines(caplog)[-1]
    assert f"host={bound}" in line
    assert "reason=thread" in line
    assert "decision='migrate no_curve" in line, line


def _seed_curve(host, model: str, rate: float, *, now: float) -> None:
    """Three clean `n = 1` buckets — what `curve()` needs to report `rate_h(1)`."""
    for age in (15.0, 10.0, 5.0):
        host.series_ring.add(model, 1.0, rate, now=now - age)


async def test_a_priced_refusal_reaches_the_log_with_its_numbers(
        stub_factory, ripple_factory, caplog):
    """A refusal the calculus *did* reach is the reason on the line.

    Both hosts have a curve at `n = 1` (40 tok/s on A, 60 on B) and a configured
    `pp_tps`, so the decision prices the move: B is faster, and the gain per
    400-token answer is `400 × (1/40 − 1/60) = 3.33 s`. The thread has two turns
    of history, so the benefit is 2 × 3.33 = 6.67 s; the move would cost the
    20 k-token prompt at A's 3680 tok/s = 5.43 s, and `cost × safety_factor`
    (1.5) is 8.15 s. 6.67 < 8.15 — `no_repayment`, and that is what the operator
    must read instead of `thread`.
    """
    caplog.set_level(logging.INFO)
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3, pp_tps=3680.0),
        host_config("B", b.url, node_id=b.state.node_id, weight=2, pp_tps=3680.0),
    ], placement=_placement(enabled=True)))
    _seed_curve(harness.ripple.fleet.get("A"), "m1", 40.0, now=time.time())
    _seed_curve(harness.ripple.fleet.get("B"), "m1", 60.0, now=time.time())

    prompt = ("a long agent turn. " * 4800)  # ~20 k tokens at 4 chars each
    first = await harness.ok(chat_body("m1", prompt, turns=2))
    assert first.headers["x-ripple-host"] == "A"
    second = await harness.ok(chat_body("m1", prompt, turns=3))

    assert second.headers["x-ripple-host"] == "A", "the refusal must not move it"
    assert second.headers["x-ripple-reason"] == "no_repayment"

    line = request_lines(caplog)[-1]
    assert "reason=no_repayment" in line
    assert "decision='migrate no_repayment" in line, line
    assert "dst=B" in line and "cost=" in line and "benefit=" in line, line


async def test_disabled_keeps_todays_reason_byte_for_byte(
        stub_factory, ripple_factory, caplog):
    """`placement.migrate.enabled: false` is the world before #95.

    Same fleet, same two requests: the reason is `thread` and the placement
    note is empty — nothing is priced, nothing is claimed.
    """
    caplog.set_level(logging.INFO)
    harness, _a, _b = await _two_hosts(stub_factory, ripple_factory, enabled=False)
    prompt = prompt_for(0)

    await harness.ok(chat_body("m1", prompt, turns=2))
    second = await harness.ok(chat_body("m1", prompt, turns=3))

    assert second.headers["x-ripple-reason"] == "thread"
    line = request_lines(caplog)[-1]
    assert "reason=thread" in line
    assert "migrate" not in line


async def test_cold_pp_tps_refuses_a_cache_hit(stub_factory, ripple_factory):
    """1254 tok/s cold, 14.46 on a 700/702 hit: only the first is a price."""
    harness, _a, _b = await _two_hosts(stub_factory, ripple_factory, enabled=True)
    ledger = harness.ripple.usage

    ledger.record(host="A", usage={"prompt_tokens": 700, "cached_tokens": 0,
                                   "prompt_tokens_per_second": 1254.0})
    ledger.record(host="A", usage={"prompt_tokens": 702, "cached_tokens": 688,
                                   "prompt_tokens_per_second": 14.46})
    assert harness.ripple.cold_pp_tps("A") == 1254.0

    # A second cold measurement is averaged; the hit stays out of the mean.
    ledger.record(host="A", usage={"prompt_tokens": 24000, "cached_tokens": 0,
                                   "prompt_tokens_per_second": 1200.0})
    assert harness.ripple.cold_pp_tps("A") == 1227.0

    # A prompt too short to be a prefill rate is not a measurement either: its
    # time is mostly fixed overhead, and it would drag the price down.
    ledger.record(host="A", usage={"prompt_tokens": 8, "cached_tokens": 0,
                                   "prompt_tokens_per_second": 3.0})
    assert harness.ripple.cold_pp_tps("A") == 1227.0

    # A node the ledger has never seen cold has no measurement at all — the
    # caller keeps the configured rate instead of inventing one.
    assert harness.ripple.cold_pp_tps("B") is None


async def test_the_view_charges_the_measured_rate_when_there_is_one(
        stub_factory, ripple_factory):
    """`migration_view` is `fleet_view` plus the measured cold prefill rate."""
    harness, _a, _b = await _two_hosts(stub_factory, ripple_factory, enabled=True,
                                       pp_tps=3680.0)
    before = harness.ripple.migration_view("m1")
    assert before.hosts["A"].pp_tps == 3680.0, "the configured rate when unmeasured"

    harness.ripple.usage.record(host="A", usage={
        "prompt_tokens": 700, "cached_tokens": 0,
        "prompt_tokens_per_second": 1254.0})
    after = harness.ripple.migration_view("m1")
    assert after.hosts["A"].pp_tps == 1254.0, "the measurement wins"
    assert after.hosts["B"].pp_tps == 3680.0, "B was never measured cold"
    # The band's samples travel with the view, unchanged.
    harness.ripple._delta_history.extend([1.0, 2.0, 3.0])
    assert harness.ripple.migration_view("m1").delta_history == (1.0, 2.0, 3.0)


async def test_a_move_is_written_back_onto_the_thread(stub_factory, ripple_factory):
    """A `moved` verdict lands on the thread: host, origin, count, pin.

    This is the half `decide_migration` deliberately does not do — it is pure —
    so the request path has to apply it, and the pin the decision priced has to
    be the pin the thread carries.
    """
    harness, _a, _b = await _two_hosts(stub_factory, ripple_factory, enabled=True)
    registry = harness.ripple.registry
    thread = registry.register(key="sid:move", model="m1", host="A", text="x",
                               holds_slot=True, source="header")
    now = 1_000.0

    first = MigrationDecision(host="B", reason="moved", priced={"pin_until": now + 60.0})
    harness.ripple._note_migration(thread, "B", first, now=now)
    assert thread.host == "B"
    assert thread.moved_from == "A"
    assert thread.moved_at == now
    assert thread.move_count == 1
    assert thread.pinned_until == now + 60.0
    assert thread.last_rebind == now, "the sweep's cooldown sees the same move"
    assert registry.rebinds_last_hour(now) == 1, "the fleet budget sees it too"

    # A second move inside the window counts up; one outside it starts over.
    harness.ripple._note_migration(thread, "A", MigrationDecision(host="A", reason="moved"),
                                   now=now + 10.0)
    assert thread.move_count == 2
    assert thread.pinned_until == 0.0, "no pin priced, no pin written"
    harness.ripple._note_migration(thread, "B", MigrationDecision(host="B", reason="moved"),
                                   now=now + 7200.0)
    assert thread.move_count == 1, "the window closed, so the count restarted"


def test_the_note_carries_the_numbers_the_decision_used():
    """§6.4: a move and a refusal both show their arithmetic on the line."""
    moved = MigrationDecision(
        host="m5-alpha", reason="moved",
        priced={"dst": "m5-alpha", "cost_s": 3.0, "gain_per_turn_s": 4.286,
                "benefit_s": 8.57, "threshold_s": 4.5, "turns_left": 2.0,
                "delta_total": 12.0, "band_s": 0.0, "band": "iqr"},
        detail="m3-beta->m5-alpha: 2 turns × 4.29s = 8.6s > 4.5s")
    note = _migration_note(moved)
    for fragment in ("migrate moved", "dst=m5-alpha", "cost=3s", "gain=4.286s/turn",
                     "benefit=8.57s", "threshold=4.5s", "turns_left=2",
                     "delta_total=+12.0", "band=0s/iqr"):
        assert fragment in note, note

    refused = MigrationDecision(host="m3-beta", reason="no_repayment",
                                priced={"dst": "m5-alpha", "cost_s": 3.0,
                                        "gain_per_turn_s": 4.286},
                                detail="1 turns × 4.29s = 4.3s < 4.5s")
    note = _migration_note(refused)
    assert note.startswith("migrate no_repayment")
    assert "dst=m5-alpha" in note and "cost=3s" in note
    # Nothing was computed beyond the price, and nothing is invented.
    assert "benefit" not in note and "delta_total" not in note
