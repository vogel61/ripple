"""The tachograph's ring (issue #83): 5 s buckets, bounded, honest gaps.

The buffer is per (host, model), lives in memory only, and is filled by the
refresh tick that already holds the two numbers (`n` from the phase bookkeeping,
`rate` from `tg_tps`) — no second loop, no extra request to the node. What the
tests below pin down is the contract, not the implementation: samples inside one
bucket are averaged, the oldest bucket falls out when the ring is full, a bucket
without a sample is `null`, and an instance that has not run anything serves an
empty list rather than `null`.
"""

from __future__ import annotations

import time

import httpx
import pytest

from ripple.config import DiscoveryConfig
from ripple.hosts import HostRuntime, SeriesRing
from ripple.stats import BUCKET_SECONDS, SERIES_BUCKETS, SERIES_WINDOW_SECONDS

from tests.support import host_config, host_row, make_config, stub_model_loaded


def _host(client: httpx.AsyncClient) -> HostRuntime:
    return HostRuntime(cfg=host_config("A", "http://127.0.0.1:9"), client=client)


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------
def test_two_samples_in_the_same_bucket_are_averaged_not_overwritten():
    """At a 2 s poll and a 5 s bucket, two samples share a bucket by design."""
    ring = SeriesRing()
    ring.add("m", 1, 30.0, now=1000.0)
    ring.add("m", 3, 40.0, now=1002.0)          # same bucket (200)

    entry = ring.digest(window_s=5, now=1002.0)[0]
    assert entry["n"] == [2]                     # not 1, and not the last 3
    assert entry["per_thread"] == [35.0]         # not 30, not 40
    assert entry["total"] == [70.0]              # 2 × 35


def test_a_later_bucket_is_a_new_point_oldest_first():
    ring = SeriesRing()
    ring.add("m", 1, 30.0, now=1000.0)           # bucket 200
    ring.add("m", 2, 20.0, now=1005.0)           # bucket 201

    entry = ring.digest(window_s=10, now=1005.0)[0]
    assert entry["n"] == [1, 2]
    assert entry["per_thread"] == [30.0, 20.0]
    assert entry["total"] == [30.0, 40.0]


# ---------------------------------------------------------------------------
# Bound
# ---------------------------------------------------------------------------
def test_after_a_full_ring_the_oldest_bucket_falls_out_and_the_length_stays():
    """Length, not shape: 720 buckets, one more sample, still 720."""
    ring = SeriesRing()
    for i in range(SERIES_BUCKETS + 1):
        ring.add("m", 1, float(i + 1), now=i * BUCKET_SECONDS)

    held = ring._by_model["m"]
    assert len(held) == SERIES_BUCKETS
    assert held[0].index == 1                    # bucket 0 was dropped

    entry = ring.digest(window_s=SERIES_BUCKETS * BUCKET_SECONDS,
                        now=SERIES_BUCKETS * BUCKET_SECONDS)[0]
    assert len(entry["n"]) == len(entry["total"]) == len(entry["per_thread"]) == SERIES_BUCKETS
    assert entry["per_thread"][0] == 2.0         # the first surviving sample
    assert entry["per_thread"][-1] == float(SERIES_BUCKETS + 1)


def test_a_model_outside_the_served_window_is_not_served_at_all():
    """An all-`null` line is noise: the entry disappears with its data."""
    ring = SeriesRing()
    ring.add("m", 1, 30.0, now=0.0)

    assert ring.digest(window_s=900, now=10_000.0) == []


def test_the_ring_prunes_a_model_that_left_the_horizon():
    ring = SeriesRing()
    ring.add("m", 1, 30.0, now=0.0)
    ring.digest(window_s=900, now=(SERIES_BUCKETS + 10) * BUCKET_SECONDS)

    assert ring.models() == []


# ---------------------------------------------------------------------------
# Gap and empty
# ---------------------------------------------------------------------------
def test_a_bucket_without_a_sample_is_null_and_the_arrays_stay_aligned():
    ring = SeriesRing()
    ring.add("m", 2, 30.0, now=0.0)              # bucket 0
    ring.add("m", 4, 20.0, now=15.0)             # bucket 3

    entry = ring.digest(window_s=20, now=15.0)[0]   # buckets 0..3
    assert entry["n"] == [2, None, None, 4]
    assert entry["total"] == [60.0, None, None, 80.0]
    assert entry["per_thread"] == [30.0, None, None, 20.0]
    assert (len(entry["n"]) == len(entry["total"])
            == len(entry["per_thread"]) == 4)


async def test_an_instance_with_no_samples_serves_an_empty_list_not_null():
    """A restart clears the ring — and that reads as \"no history\", not as a gap."""
    async with httpx.AsyncClient() as client:
        host = _host(client)
        assert host.series() == []
        assert host.series_ring.digest() == []


# ---------------------------------------------------------------------------
# Who fills it
# ---------------------------------------------------------------------------
async def test_the_host_samples_every_tick_but_only_what_has_a_rate():
    """`n = 0` is a point (the host is idle), a missing rate is not (it is unknown)."""
    async with httpx.AsyncClient() as client:
        host = _host(client)
        host._sample_series(now=1000.0)                  # nothing measured yet
        assert host.series_ring.digest(5, now=1000.0) == []

        host.metrics["m1"] = {"tg_tps": 30.0}            # measured once, now idle
        host._sample_series(now=1000.0)
        idle = host.series_ring.digest(5, now=1000.0)[0]
        assert idle["n"] == [0]
        assert idle["total"] == [0.0]
        # The rate belongs to the request that has *ended*: no stream, no rate.
        assert idle["per_thread"] == [None]

        host.begin_phase("m1")                           # dispatch …
        host.note_first_token("m1")                      # … first token: streaming
        host._sample_series(now=1005.0)
        host.metrics["m1"]["tg_tps"] = 40.0
        host._sample_series(now=1007.0)                  # same bucket, second sample

        entry = host.series_ring.digest(10, now=1007.0)[0]
        assert entry["n"] == [0, 1]                      # buckets 200 and 201
        # Both samples in bucket 201 had a stream running, so both rates count:
        # (30 + 40) / 2. The idle bucket above contributed none.
        assert entry["per_thread"] == [None, 35.0]
        assert entry["total"] == [0.0, 35.0]             # 0, 1 × 35


async def test_a_bucket_with_no_running_stream_never_carries_a_rate():
    """The #83 invariant where it broke: a stale `tg_tps` beside `n = 0`.

    Sampled exactly like the real run did — one measurement, then ticks with
    nothing running — the bucket must say "nothing ran" (`n = 0`, `total = 0.0`)
    and must not repeat the finished request's 55.5 tok/s as a per-thread rate.
    """
    async with httpx.AsyncClient() as client:
        host = _host(client)
        host.record_rate("m1", 55.5)                     # one stream, now over
        assert host.metrics["m1"]["tg_tps"] == 55.5

        host._sample_series(now=1000.0)
        host._sample_series(now=1002.0)                  # same bucket, still idle

        entry = host.series_ring.digest(5, now=1002.0)[0]
        assert entry["n"] == [0]
        assert entry["total"] == [0.0]
        assert entry["per_thread"] == [None]

        for i, count in enumerate(entry["n"]):
            if count == 0:
                assert entry["per_thread"][i] is None
            else:
                assert entry["total"][i] == round(count * entry["per_thread"][i], 1)


async def test_a_down_host_stops_sampling_instead_of_drawing_a_zero():
    """An absent node must not look like an idle one."""
    async with httpx.AsyncClient() as client:
        host = _host(client)
        host.metrics["m1"] = {"tg_tps": 30.0}
        host._sample_series(now=1000.0)
        assert len(host.series_ring.digest(5, now=1000.0)) == 1

        host.status = "down"
        host._sample_series(now=1005.0)
        assert host.series_ring.digest(5, now=1005.0) == []


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------
async def test_healthz_carries_the_series_of_a_live_instance(stub_factory, ripple_factory):
    """The payload the page and the optimizer read: 180 points, gaps included."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config(
        [host_config("A", a.url, node_id=a.state.node_id, weight=1)],
        # No background tick inside the test: the two samples below are the only
        # ones, so the mean they produce is the mean that is asserted.
        discovery=DiscoveryConfig(interval_seconds=3600)))
    host = harness.host("A")

    # A freshly started instance has history for nothing — `[]`, not `null`.
    assert host_row(await harness.healthz(), "A")["series"] == []

    # One stream, two ticks inside the same bucket. Anchored at the start of the
    # current bucket so the read below lands in it too.
    anchor = (time.time() // BUCKET_SECONDS) * BUCKET_SECONDS
    host.begin_phase("m1")
    host.note_first_token("m1")
    host.metrics["m1"] = {"tg_tps": 30.0}
    host._sample_series(now=anchor)
    host.metrics["m1"]["tg_tps"] = 33.0
    host._sample_series(now=anchor + 1)

    series = host_row(await harness.healthz(), "A")["series"]
    assert len(series) == 1
    entry = series[0]
    assert entry["model"] == "m1"
    assert entry["bucket_s"] == BUCKET_SECONDS == 5
    assert entry["window_s"] == SERIES_WINDOW_SECONDS == 900
    assert (len(entry["n"]) == len(entry["total"])
            == len(entry["per_thread"]) == 180)

    # Exactly one point carries data, and it is the mean of the two samples.
    filled = [i for i, value in enumerate(entry["n"]) if value is not None]
    assert len(filled) == 1
    assert entry["n"][filled[0]] == 1
    assert entry["per_thread"][filled[0]] == 31.5
    assert entry["total"][filled[0]] == 31.5
    # Every other point is a gap in all three arrays — nothing was invented.
    assert all(value is None for i, value in enumerate(entry["total"]) if i != filled[0])

    # `?window=` serves a narrower window from the same ring.
    narrow_resp = await harness.client.get("/healthz", params={"window": 60})
    assert narrow_resp.status_code == 200, narrow_resp.text
    narrow = host_row(narrow_resp.json(), "A")["series"][0]
    assert narrow["window_s"] == 60
    assert len(narrow["n"]) == 12


# ---------------------------------------------------------------------------
# The concurrency curve and the relative load (issue #80)
# ---------------------------------------------------------------------------
def _anchor() -> float:
    """Start of the current 5 s bucket — so `curve()` reads what we seeded."""
    return (time.time() // BUCKET_SECONDS) * BUCKET_SECONDS


def _seed(host: HostRuntime, model: str, samples: list, *, anchor: float) -> None:
    """One entry per bucket, oldest first; each entry is its list of samples.

    Ascending time order on purpose: the ring drops a sample that arrives for a
    bucket older than its last one (the clock-back guard), so a descending seed
    would measure nothing at all.
    """
    for i, bucket in enumerate(samples):
        moment = anchor - (len(samples) - 1 - i) * BUCKET_SECONDS
        for n, rate in bucket:
            host.series_ring.add(model, n, rate, now=moment)


async def test_curve_is_the_mean_rate_per_clean_bucket_at_each_n():
    """Three buckets at `n = 1` are a point; two at `n = 2` are not yet."""
    async with httpx.AsyncClient() as client:
        host = _host(client)
        _seed(host, "m1", [[(1, 80.0)], [(1, 74.0)], [(1, 78.2)],
                           [(2, 40.0)], [(2, 40.0)]], anchor=_anchor())

        curve = host.curve()
        assert curve == {1.0: 77.4}              # (80 + 74 + 78.2) / 3
        assert 2.0 not in curve                  # two buckets are no measurement


async def test_curve_ignores_idle_and_mixed_buckets():
    """`n = 0` (nothing ran) and `n = 1.5` (idle + busy in one 5 s window) are
    artefacts of the bucket width, not concurrency levels the node ran at."""
    async with httpx.AsyncClient() as client:
        host = _host(client)
        _seed(host, "m1", [
            [(1, 70.0)], [(1, 70.0)], [(1, 70.0)],     # three clean buckets
            [(0, None)], [(0, None)], [(0, None)],     # idle: no rate, n = 0
            [(1, 70.0), (2, 70.0)],                    # mixed: n = 1.5
            [(1, 70.0), (2, 70.0)],
            [(1, 70.0), (2, 70.0)],
        ], anchor=_anchor())

        assert host.curve() == {1.0: 70.0}


async def test_a_mixed_bucket_alone_is_no_point_at_all():
    async with httpx.AsyncClient() as client:
        host = _host(client)
        _seed(host, "m1", [[(1, 70.0), (2, 70.0)] for _ in range(4)],
              anchor=_anchor())

        assert host.curve() == {}
        assert host.capability_rate() is None


async def test_capability_rate_is_the_single_stream_point_or_none():
    async with httpx.AsyncClient() as client:
        host = _host(client)
        assert host.capability_rate() is None

        _seed(host, "m1", [[(1, 77.4)] for _ in range(3)]
              + [[(2, 44.0)] for _ in range(3)], anchor=_anchor())
        assert host.capability_rate() == host.curve()[1.0] == 77.4


async def test_relative_load_is_the_served_models_rate_against_the_capability():
    """77.4 tok/s alone against 64.7 tok/s now: 1 − 64.7/77.4 = 0.1641."""
    async with httpx.AsyncClient() as client:
        host = _host(client)
        anchor = _anchor()
        _seed(host, "m1", [[(1, 77.4)] for _ in range(3)], anchor=anchor)
        host.metrics["m1"] = {"tg_tps": 64.7}
        # A second model with fewer buckets: not the one this host serves.
        _seed(host, "m2", [[(1, 77.4)]], anchor=anchor)
        host.metrics["m2"] = {"tg_tps": 20.0}

        assert host.relative_load() == pytest.approx(1 - 64.7 / 77.4, abs=1e-4)
        assert host.relative_load() == 0.1641


async def test_relative_load_is_none_without_a_curve_or_a_rate():
    async with httpx.AsyncClient() as client:
        host = _host(client)
        host.metrics["m1"] = {"tg_tps": 64.7}
        assert host.relative_load() is None          # no curve: nothing to divide by

        _seed(host, "m1", [[(1, 77.4)] for _ in range(3)], anchor=_anchor())
        host.metrics.clear()
        assert host.relative_load() is None          # no measured rate either


async def test_relative_load_is_clamped_when_the_ema_runs_hot():
    """An EMA can sit above the single-stream rate: never report a negative load."""
    async with httpx.AsyncClient() as client:
        host = _host(client)
        _seed(host, "m1", [[(1, 77.4)] for _ in range(3)], anchor=_anchor())
        host.metrics["m1"] = {"tg_tps": 90.0}

        assert host.relative_load() == 0.0


async def test_healthz_carries_capability_rate_and_relative_load(
        stub_factory, ripple_factory):
    """Both fields on `hosts[]`, `null` while nothing was measured."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config(
        [host_config("A", a.url, node_id=a.state.node_id, weight=1)],
        discovery=DiscoveryConfig(interval_seconds=3600)))
    host = harness.host("A")

    fresh = host_row(await harness.healthz(), "A")
    assert fresh["capability_rate"] is None
    assert fresh["relative_load"] is None

    _seed(host, "m1", [[(1, 77.4)] for _ in range(3)], anchor=_anchor())
    host.metrics["m1"] = {"tg_tps": 64.7}

    row = host_row(await harness.healthz(), "A")
    assert row["capability_rate"] == 77.4
    assert row["relative_load"] == 0.1641

    # The measurement has its own window: zooming the chart must not erase it.
    narrow = host_row((await harness.client.get("/healthz", params={"window": 60})).json(), "A")
    assert narrow["capability_rate"] == 77.4
    assert narrow["relative_load"] == 0.1641

    # `/admin/nodes` shares `_node_row()` — those answers must not grow with it.
    assert "capability_rate" not in harness.ripple._node_row(host)
    assert "relative_load" not in harness.ripple._node_row(host)
