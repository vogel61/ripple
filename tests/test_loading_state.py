"""The loading state machine (docs/loading-state.md §4/§5, contract §3/§4/§8).

While RIPPLE causes a cold load it must not let that load decide the health of
the host: the probes are suspended, `healthy` keeps the verdict of the last real
probe, and the host stays in the pool. Three signals end the state — the request
that caused the load returns, oMLX reports the load done, or the ceiling expires.
Each of them is driven here directly, because the timing is the whole point: the
defect of 14.11.2026 was a probe that landed *inside* a load.
"""

from __future__ import annotations

import asyncio

import pytest

from ripple.config import LoadingConfig, PlacementConfig
from ripple.hosts import HostRuntime

from tests.support import (StubModel, chat_body, host_config, host_row,
                           make_config, prompt_for, stub_model_loaded, wait_until)

QWEN_BYTES = 111_600_000_000          # the 111,6 GB model of the incident


def _bare_host(placement: PlacementConfig | None = None) -> HostRuntime:
    """A host without HTTP — enough for the estimate and the state transitions."""
    return HostRuntime(cfg=host_config("A", "http://127.0.0.1:1"), client=None,
                       placement=placement or PlacementConfig())


# ---------------------------------------------------------------------------
# Estimate seam (placeholder until the calibration engine, wave W3)
# ---------------------------------------------------------------------------
def test_estimate_load_seconds_is_the_placeholder_curve():
    """Worst measured slope (0.311 s/GB) × safety factor, clamped to [10, max].

    W3 replaces the slope with the node's own calibration; the clamp and the
    factor are already final, because an estimate that is too short would let a
    probe land inside the load.
    """
    host = _bare_host(PlacementConfig(
        loading=LoadingConfig(safety_factor=1.3, max_seconds=900.0)))

    assert host.estimate_load_seconds(100.0) == pytest.approx(100 * 0.311 * 1.3)
    assert host.estimate_load_seconds(453.6) == pytest.approx(453.6 * 0.311 * 1.3)
    # floor: a small model must not get an ETA that a real load always beats
    assert host.estimate_load_seconds(1.0) == pytest.approx(10.0)
    assert host.estimate_load_seconds(None) == pytest.approx(10.0)
    # ceiling: never longer than placement.loading.max_seconds
    assert host.estimate_load_seconds(10_000.0) == pytest.approx(900.0)


def test_begin_loading_is_a_no_op_when_the_feature_is_off():
    host = _bare_host(PlacementConfig(loading=LoadingConfig(enabled=False)))

    host.begin_loading("m1", size_gb=111.6, eta_seconds=40.0, source="dispatch")

    assert host.loading is None
    assert host.status == "unknown"


# ---------------------------------------------------------------------------
# 1. A cold load suspends the probe and keeps the host in the pool
# ---------------------------------------------------------------------------
async def test_cold_load_suspends_the_probe_and_keeps_the_host_in_the_pool(
        stub_factory, ripple_factory):
    a = stub_factory(models=[StubModel(id="m1", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id),
    ]))
    host = harness.host("A")
    assert host.status == "healthy" and host.healthy is True

    a.state.delay = 0.2                       # keep the request in flight
    request = asyncio.create_task(harness.chat(chat_body("m1", prompt_for(0), turns=2)))
    await wait_until(lambda: host.loading is not None)

    # The load is visible, but it is not a health verdict.
    assert host.status == "loading"
    assert host.healthy is True
    assert host.in_pool is True
    assert host.loading is not None
    assert host.loading["model"] == "m1"
    assert host.loading["source"] == "dispatch"
    assert host.loading["size_gb"] == pytest.approx(111.6)
    assert host.loading["eta_seconds"] >= 10.0
    assert host.loading["started_at"] > 0

    # /healthz reports `state` under its reporting name; `status` stays.
    row = host_row(await harness.healthz(), "A")
    assert row["state"] == "loading"
    assert row["status"] == "loading"
    assert row["healthy"] is True
    assert row["loading"]["model"] == "m1"
    assert row["loading"]["eta_seconds"] >= 10.0

    # A probe now must not touch /health at all — the stub reports `is_loading`
    # as unknown, so the state has to survive the poll.
    probes_before = a.state.count("health")
    await host.refresh()
    assert a.state.count("health") == probes_before
    assert host.loading is not None
    assert host.status == "loading"

    stats = await harness.stats()
    assert stats["probes_skipped"] == 1
    assert stats["loads_supervised"] == 1

    resp = await request
    assert resp.status_code == 200
    assert resp.headers["x-ripple-reason"] == "cold_load"


# ---------------------------------------------------------------------------
# 2. The three ways a load ends
# ---------------------------------------------------------------------------
async def test_loading_clears_when_the_request_returns(stub_factory, ripple_factory):
    a = stub_factory(models=[StubModel(id="m1", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id),
    ]))
    host = harness.host("A")

    a.state.delay = 0.2
    request = asyncio.create_task(harness.chat(chat_body("m1", prompt_for(0), turns=2)))
    await wait_until(lambda: host.loading is not None)

    resp = await request
    assert resp.status_code == 200
    assert host.loading is None
    assert host.status == "healthy"
    assert host.healthy is True

    # ... and the probes are back: the next refresh talks to /health again.
    probes_before = a.state.count("health")
    await host.refresh()
    assert a.state.count("health") == probes_before + 1
    assert host.status == "healthy"

    # §12.1: the next request is *served*. On 14.11.2026 it answered
    # `503 no_healthy_host` one millisecond after the load ended, because the load
    # had been taken as the health verdict. The reason may be cold_load (this
    # stub never keeps the model) — what matters is 200 and not 503.
    second = await harness.chat(chat_body("m1", prompt_for(1), turns=2))
    assert second.status_code == 200
    assert second.headers["x-ripple-reason"] in ("cold_load", "model_pinned")


async def test_loading_clears_when_omlx_reports_the_load_done(stub_factory,
                                                              ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id),
    ]))
    host = harness.host("A")

    a.state.start_load("m1")                  # oMLX's own signal: loading, not loaded
    host.begin_loading("m1", size_gb=111.6, eta_seconds=40.0, source="omlx")
    assert host.status == "loading"

    probes_before = a.state.count("health")
    await host.refresh()
    assert a.state.count("health") == probes_before
    assert host.loading is not None           # `is_loading` is still true

    a.state.finish_load("m1")
    await host.refresh()
    assert host.loading is None
    assert host.status == "healthy"
    assert host.healthy is True
    assert (await harness.stats())["probes_skipped"] == 2


async def test_loading_ends_at_the_ceiling(stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    cfg = make_config([host_config("A", a.url, node_id=a.state.node_id)],
                      placement=PlacementConfig(
                          loading=LoadingConfig(max_seconds=5.0)))
    harness = await ripple_factory(cfg)
    host = harness.host("A")

    host.begin_loading("m1", size_gb=10.0, eta_seconds=4.0, source="dispatch")
    assert host.loading is not None
    started = host.loading["started_at"]
    assert host.loading_expired(now=started + 1.0) is False
    assert host.loading_expired(now=started + 6.0) is True

    # The stub reports no `is_loading`, so only the ceiling can end the state.
    host.loading["started_at"] = started - 6.0
    await host.refresh()

    assert host.loading is None
    assert host.status == "healthy"
    assert host.healthy is True


async def test_loading_ends_when_the_model_disappears_from_the_node(
        stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id),
    ]))
    host = harness.host("A")

    host.begin_loading("m1", size_gb=10.0, eta_seconds=20.0, source="dispatch")
    a.state.models = []                       # the node no longer lists the model
    await host.refresh()

    assert host.loading is None
    assert host.status == "healthy"


async def test_ending_a_load_does_not_revive_a_host_that_was_down(
        stub_factory, ripple_factory):
    """A load is not a health verdict, and neither is its end.

    `begin_loading` leaves a `down` host `down`; `end_loading` used to recompute
    the status from `healthy` and could put it back into the pool without a
    single successful probe in between.
    """
    a = stub_factory(models=[StubModel(id="big", estimated_size=QWEN_BYTES)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    host.status = "down"
    host.healthy = False
    host.consecutive_failures = 5

    host.begin_loading("big", size_gb=111.6, eta_seconds=32.0, source="dispatch")
    assert host.status == "down"

    host.end_loading("request_returned")
    assert host.status == "down"
    assert host.in_pool is False
