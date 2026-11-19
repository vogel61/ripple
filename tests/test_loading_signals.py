"""Signal 2 of docs/loading-state.md §5 — loads RIPPLE did not cause.

The state machine of W1 covers the load RIPPLE causes itself (signal 1, the
placement tier). A load started in the oMLX UI or by a client talking to `:8000`
directly is invisible to it — and that load produces exactly the defect of
14.11.2026: a health probe landing inside it. oMLX reports `is_loading` and
`loading_started_at` on `/v1/models/status`, so the state can be entered from the
authoritative source as well.
"""

from __future__ import annotations

import logging
import time

import pytest

from ripple.config import PlacementConfig
from ripple.hosts import DEFAULT_LOAD_SECONDS_PER_GB

from tests.support import (StubModel, chat_body, host_config, host_row, make_config,
                           prompt_for, stub_model_loaded)

QWEN_BYTES = 111_600_000_000          # the 111,6 GB model of the incident
BIG_BYTES = 453_600_000_000           # the 453,6 GB load of the second cold-load test


async def test_a_load_that_omlx_started_without_us_enters_the_state(
        stub_factory, ripple_factory):
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    a.state.start_load("big", started_at=time.time() - 5)
    await host.refresh()

    assert host.loading is not None
    assert host.loading["model"] == "big"
    assert host.loading["source"] == "omlx"
    assert host.loading["size_gb"] == 111.6
    # The load is not a health verdict: the probe that ran was fine, and the host
    # must keep its verdict and its pool membership.
    assert host.healthy is True
    assert host.in_pool is True
    assert host.status == "loading"


async def test_the_eta_of_an_external_load_is_what_is_left(stub_factory, ripple_factory):
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    full = 111.6 * DEFAULT_LOAD_SECONDS_PER_GB * 1.3

    a.state.start_load("big", started_at=time.time() - 20)
    await host.refresh()

    assert host.loading is not None
    assert 0 < host.loading["eta_seconds"] < full
    # and the ceiling counts from oMLX's start, not from our observation
    assert host.loading["started_at"] <= time.time() - 19


async def test_probes_are_suspended_for_an_external_load(stub_factory, ripple_factory):
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    a.state.start_load("big")
    await host.refresh()

    health_calls = a.state.count("health")
    await host.refresh()

    # no /health probe at all, and the skip is counted so the host cannot hide
    assert a.state.count("health") == health_calls
    assert harness.ripple.stats.probes_skipped >= 1


async def test_an_external_load_ends_when_omlx_reports_it_done(
        stub_factory, ripple_factory):
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    a.state.start_load("big")
    await host.refresh()
    assert host.loading is not None

    a.state.finish_load("big")
    await host.refresh()

    assert host.loading is None
    assert host.status == "healthy"


async def test_a_cold_load_on_a_degraded_host_is_still_counted_as_a_cold_load(
        stub_factory, ripple_factory):
    # The reason of such a request is `degraded_host` (the fallback must not be
    # silent), but the load it caused is a load RIPPLE supervised — the counter
    # follows the placement tier, not the reason.
    a = stub_factory(models=[StubModel(id="warm", loaded=True, estimated_size=1_000_000_000),
                             StubModel(id="cold", estimated_size=1_000_000_000)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    a.state.health_status = "error"           # one soft failure → degraded
    await host.refresh()
    assert host.status == "degraded"
    assert host.in_pool is True
    a.state.health_status = "healthy"         # the probe recovers, the count stays

    resp = await harness.chat(chat_body(model="cold"))

    assert resp.status_code == 200, resp.text
    assert resp.headers["x-ripple-reason"] == "degraded_host"
    assert harness.ripple.stats.degraded_pool_decisions == 1
    assert harness.ripple.stats.cold_load == 1
    assert harness.ripple.stats.loads_supervised == 1
    # the request returned, so the load is over for us
    assert host.loading is None


async def test_the_degraded_counter_counts_decisions_not_requests(
        stub_factory, ripple_factory):
    """`degraded_pool_decisions` is a decision count, and it says so.

    `choose()` runs once per placement attempt — also for the `retry_other_node`
    hop of a failed request — so one client request can produce two decisions on
    a degraded host. Counting *uses* would need the proxy to tell a hop from a
    fresh request; the name and the contract now say which number this is.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config(
        [host_config("A", a.url, node_id=a.state.node_id),
         host_config("B", b.url, node_id=b.state.node_id)],
        placement=PlacementConfig(binding_strategy="least_threads")))

    for name, stub in (("A", a), ("B", b)):
        stub.state.health_status = "error"          # one soft failure each
        await harness.host(name).refresh()
        assert harness.host(name).status == "degraded"
    a.stop()                                        # the first hop cannot connect

    resp = await harness.chat(chat_body("m1", prompt_for(0), turns=2))

    assert resp.status_code == 200, resp.text
    assert resp.headers["x-ripple-host"] == "B"     # the retry hop served it
    stats = await harness.stats()
    # Two decisions for one request: A (failed) and the retry on B.
    assert stats["degraded_pool_decisions"] == 2


async def test_a_monotonic_loading_started_at_is_not_read_as_an_epoch(
        stub_factory, ripple_factory):
    # M3-beta reports a monotonic clock value in `loading_started_at` (158021.3 s
    # after boot, constant during the load). Read as an epoch it says the load
    # began 57 years ago, which would clamp the ETA to the 10 s floor for a
    # 111 GB load — the operator would see "almost done" for minutes.
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    a.state.start_load("big", started_at=158_021.299)
    await host.refresh()

    assert host.loading is not None
    # A real estimate for 111,6 GB (the default curve says 24,7 s × 1,3 = 32,1 s),
    # and certainly not the 10 s floor a bogus 57-year elapsed time would produce.
    assert 20.0 < host.loading["eta_seconds"] <= 60.0
    assert host.loading["started_at"] == pytest.approx(time.time(), abs=5)


async def test_a_load_the_node_has_not_started_yet_keeps_its_supervision(
        stub_factory, ripple_factory):
    """The race that made the state useless: oMLX says `is_loading: false` for a
    model it has not begun to load yet.

    A refresh tick between our dispatch and oMLX registering the load used to end
    the state at once — and the probe that followed landed inside the load this
    whole feature exists to keep it out of. `false` only means "finished" after
    the node has said `true` at least once.
    """
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES,
                                       is_loading=False)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    # the dispatch tier has decided to load; oMLX has not started it yet
    host.begin_loading("big", size_gb=111.6, eta_seconds=32.0, source="dispatch")
    await host.refresh()

    assert host.loading is not None, "the supervision was dropped before the load began"
    assert host.loading["seen_loading"] is False
    assert host.status == "loading"

    # now the node really loads, then finishes
    a.state.models[0].is_loading = True
    await host.refresh()
    assert host.loading is not None
    assert host.loading["seen_loading"] is True

    a.state.finish_load("big")            # `false` after a `true` means done
    await host.refresh()
    assert host.loading is None
    assert host.status == "healthy"


async def test_a_load_that_ends_without_a_resident_model_is_a_failure(
        stub_factory, ripple_factory, caplog):
    """A load is not done because the node stopped reporting it.

    oMLX can end a load with nothing to show for it: `is_loading` flips to false
    and the model stays at `loaded: false` (a refused load, a full node, a broken
    file). The state used to end as an ordinary `loading_end`, so `/stats` said
    every supervised load had succeeded. The two statements are different: the
    load is over, *and* the model is not there.
    """
    caplog.set_level(logging.INFO)
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    a.state.start_load("big")
    await host.refresh()
    assert host.loading is not None and host.loading["seen_loading"] is True

    model = a.state.model("big")
    model.is_loading = False                 # the load is over ...
    assert model.loaded is False             # ... and no model is there
    await host.refresh()                     # one tick of doubt (`_apply_used_bytes`)
    assert host.loading is not None
    await host.refresh()                     # the node insists: nothing came of it

    assert host.loading is None
    assert host.status == "healthy"
    stats = await harness.stats()
    assert stats["loads_failed"] == 1
    # Once, not once per tick: the entry is gone after the first verdict.
    assert caplog.text.count("loading_failed host=A model=big") == 1
    assert "reason=omlx_idle" in caplog.text

    await host.refresh()                     # a later tick cannot count it again
    assert (await harness.stats())["loads_failed"] == 1


async def test_one_idle_tick_without_a_resident_model_is_not_yet_a_failure(
        stub_factory, ripple_factory):
    """The same one-tick tolerance `_apply_used_bytes()` gives a 0.

    `is_loading: false` with `loaded: false` can be the boundary between two
    fields of one answer. One tick of that is not evidence of a failed load —
    the next answer decides, and until then the probes stay suspended, which is
    the harmless direction.
    """
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    a.state.start_load("big")
    await host.refresh()
    model = a.state.model("big")
    model.is_loading = False

    await host.refresh()
    assert host.loading is not None, "one ambiguous tick ended the supervision"
    assert (await harness.stats())["loads_failed"] == 0

    model.loaded = True                      # it was the boundary after all
    await host.refresh()

    assert host.loading is None
    assert host.status == "healthy"
    assert (await harness.stats())["loads_failed"] == 0


async def test_a_model_that_disappears_while_loading_is_a_failed_load(
        stub_factory, ripple_factory):
    """A model the node no longer lists cannot have become resident."""
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    a.state.start_load("big")
    await host.refresh()
    a.state.models = []                      # the node does not list it at all
    await host.refresh()

    assert host.loading is None
    stats = await harness.stats()
    assert stats["loads_failed"] == 1
    # `loads_failed` covers every load the state supervised, so a load RIPPLE did
    # not start can fail without `loads_supervised` ever moving.
    assert stats["loads_supervised"] == 0


async def test_a_host_without_an_eviction_guard_still_reports_loading(
        stub_factory, ripple_factory):
    """`guard_enabled` is a warning about the forecast, not a state of its own.

    It used to be written into `status` at the end of every refresh — which, for
    a load discovered in that same tick, overwrote `loading` with `degraded` and
    hid the state from the dashboard while the probes were suspended.
    """
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)],
                     final_ceiling=0)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    assert host.guard_enabled is False

    a.state.start_load("big", started_at=time.time() - 5)
    await host.refresh()

    assert host.loading is not None
    assert host.status == "loading"
    # still a warning, just not one that hides the load
    await host.refresh()
    assert host.status == "loading"


async def test_a_zero_during_and_after_a_load_keeps_the_memory_bar(
        stub_factory, ripple_factory):
    """The second cold-load test: 453,6 GB loaded, `/healthz` said `used=0.0 GB`.

    oMLX answers `current_model_memory: 0` while a load runs and once right after
    it finishes; the correct 111,6 GB came a minute later. Placement never looked
    at the number (it uses `estimated_size` per model), but the dashboard's memory
    bar showed an empty node. That 0 is "no answer", not "empty".
    """
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES, loaded=True),
                             StubModel(id="huge", estimated_size=BIG_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    assert host_row(await harness.healthz(), "A")["used_bytes"] == QWEN_BYTES

    # The node begins the 453,6 GB load and its memory answer collapses to 0.
    a.state.start_load("huge")
    a.state.current_model_memory = 0
    await host.refresh()
    assert host.loading is not None
    assert host.used_bytes == QWEN_BYTES

    # While the load runs no probe is made at all, so the 0 cannot arrive —
    # the last known value simply stays.
    probes = a.state.count("health")
    await host.refresh()
    assert a.state.count("health") == probes
    assert host_row(await harness.healthz(), "A")["used_bytes"] == QWEN_BYTES

    # The load finishes and the first probe after it still gets the 0.
    a.state.finish_load("huge")
    await host.refresh()               # ends the state, no probe in this tick
    assert host.loading is None
    await host.refresh()               # the first /health after the load
    assert a.state.count("health") > probes
    assert host.used_bytes == QWEN_BYTES, "a single 0 is 'no answer', not 'empty'"
    assert host_row(await harness.healthz(), "A")["used_bytes"] == QWEN_BYTES


async def test_a_node_that_never_reported_memory_keeps_its_honest_zero(
        stub_factory, ripple_factory):
    """Nothing loaded is a real 0, and it must not be dressed up as an old value.

    The tolerance may only hold a value the node actually reported once.
    """
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    assert host.used_bytes == 0
    row = host_row(await harness.healthz(), "A")
    assert row["used_bytes"] == 0
    assert row["used_ratio"] == 0.0


async def test_a_second_zero_in_a_row_is_taken_as_the_truth(stub_factory, ripple_factory):
    """A node that keeps answering 0 really is empty (a model unloaded in the UI).

    One tick of doubt, then the operator sees the truth — otherwise a stale bar
    would outlive the model it showed.
    """
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES, loaded=True)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    assert host.used_bytes == QWEN_BYTES

    a.state.models[0].loaded = False        # unloaded in the oMLX UI
    await host.refresh()
    assert host.used_bytes == QWEN_BYTES, "one 0 is not enough to empty the bar"

    await host.refresh()
    assert host.used_bytes == 0
    assert host_row(await harness.healthz(), "A")["used_bytes"] == 0

