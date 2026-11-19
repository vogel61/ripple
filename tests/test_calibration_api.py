"""The calibration admin endpoints and the job behind them (spec §10 tests 9-12).

The engine (`ripple/calibration.py`) is tested on its own; what is tested here is
the conversation with the node: who may start a job, which models it picks, where
the measured seconds come from, and that the node is left as it was found.

Everything runs against stub oMLX instances. `StubState.load_simulates` is the
opt-in that makes a `/load` actually change residency — off by default, so no
other test in the suite changes behaviour.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from ripple import calibration as engine
from ripple.config import LoadingConfig

from tests.support import (StubModel, bearer, host_config, keyed_config,
                           stub_model_loaded, wait_until)

ADMIN = bearer("test-admin-key")
CEILING_GB = 487.7                     # the stub's `final_ceiling`, in GB

# Sizes chosen so the plan has a real range: 10× between the extremes, and a
# geometric mean (126 GB) that differs from the arithmetic one (220 GB).
SMALL = 40_000_000_000
MID = 126_000_000_000
BIG = 400_000_000_000


def _cfg(tmp_path, hosts, *, samples: int = 2, max_seconds: float = 30.0):
    """Admin key on, calibration file and sample count under `tmp_path`.

    `replace` and not a fresh `PlacementConfig`: the latter would silently undo
    the `/tmp` redirects `make_config` applies to the operator's real stores
    (maintenance, model policy). Measured on 16.11.2026 — the maintenance window
    the operator had set on `M3-beta` in `~/.ripple/maintenance.yaml` leaked into
    this file and made `test_the_curve_follows_the_node_id_not_the_host_name`
    answer 409.
    """
    cfg = keyed_config(hosts, str(tmp_path / "keys.yaml"))
    cfg.placement = replace(cfg.placement, loading=LoadingConfig(
        calibration_file=str(tmp_path / "calibration.yaml"),
        samples=samples, max_seconds=max_seconds))
    return cfg


def test_the_harness_never_reads_the_operators_stores(tmp_path):
    """Guard for the leak above: every store this file's config touches is ours.

    A test that reads `~/.ripple/maintenance.yaml` does not fail in isolation —
    it fails on the day the operator takes a node out of the pool, and then it
    fails for a reason that has nothing to do with the code under test.
    """
    cfg = _cfg(tmp_path, [])
    home = str(Path.home())
    assert not cfg.placement.maintenance_file.startswith(home), \
        cfg.placement.maintenance_file
    assert not cfg.placement.model_policy_file.startswith(home), \
        cfg.placement.model_policy_file
    assert not cfg.placement.loading.calibration_file.startswith(home), \
        cfg.placement.loading.calibration_file
    assert not cfg.auth.keys_file.startswith(home), cfg.auth.keys_file


def _three_models() -> list[StubModel]:
    return [StubModel(id="small", estimated_size=SMALL),
            StubModel(id="mid", estimated_size=MID),
            StubModel(id="big", estimated_size=BIG)]


def _node_id(stub) -> str:
    return stub.state.node_id


async def _get(harness, name: str = "A") -> dict:
    resp = await harness.client.get(f"/admin/nodes/{name}/calibration", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _post(harness, name: str = "A", *, force: bool = False):
    url = f"/admin/nodes/{name}/calibrate" + ("?force=1" if force else "")
    return await harness.client.post(url, headers=ADMIN)


async def _wait_done(harness, name: str = "A", *, timeout: float = 20.0) -> dict:
    """Poll the GET until the job settles — `wait_until` does the waiting."""
    seen: dict[str, dict | None] = {"job": None}
    done = asyncio.Event()

    async def poll() -> None:
        while not done.is_set():
            seen["job"] = (await _get(harness, name))["job"]
            if seen["job"] is not None and seen["job"]["state"] != "running":
                done.set()
                return
            await asyncio.sleep(0.05)

    task = asyncio.create_task(poll())
    try:
        await wait_until(lambda: done.is_set(), timeout=timeout)
    finally:
        done.set()
        await task
    assert seen["job"] is not None
    return seen["job"]


# ---------------------------------------------------------------------------
# Test 12 — the admin key, and an unknown node
# ---------------------------------------------------------------------------
async def test_12a_both_endpoints_require_the_admin_key(stub_factory, ripple_factory,
                                                        tmp_path):
    a = stub_factory(models=_three_models())
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    assert (await harness.client.post("/admin/nodes/A/calibrate")).status_code == 401
    assert (await harness.client.get("/admin/nodes/A/calibration")).status_code == 401
    # A wrong key is not a key either.
    wrong = bearer("not-the-admin-key")
    assert (await harness.client.get("/admin/nodes/A/calibration",
                                     headers=wrong)).status_code == 401
    # With the key both answer — and nothing was started by the denied calls.
    assert (await _get(harness))["job"] is None
    assert harness.ripple.stats.calibrations_started == 0


async def test_12b_an_unknown_node_is_a_404(stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_three_models())
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    assert (await harness.client.get("/admin/nodes/nope/calibration",
                                     headers=ADMIN)).status_code == 404
    assert (await harness.client.post("/admin/nodes/nope/calibrate",
                                      headers=ADMIN)).status_code == 404


# ---------------------------------------------------------------------------
# Test 9 — a bound thread refuses, `force` overrides
# ---------------------------------------------------------------------------
async def test_9_a_bound_thread_refuses_and_force_overrides(stub_factory, ripple_factory,
                                                            tmp_path):
    a = stub_factory(models=_three_models(), load_simulates=True, load_delay=0.05)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))
    # The operator's session, bound to this node: calibrating would cycle models
    # under it and could cost it its prefix cache.
    harness.ripple.registry.register(key="session", model="small", host="A",
                                     text="a conversation", holds_slot=True,
                                     source="header")

    refused = await _post(harness)
    assert refused.status_code == 409, refused.text
    message = refused.json()["error"]["message"]
    assert "bound thread" in message and "force=1" in message
    assert harness.ripple.stats.calibrations_started == 0
    assert a.state.load_calls == []

    forced = await _post(harness, force=True)
    assert forced.status_code == 202, forced.text
    assert harness.ripple.stats.calibrations_forced == 1
    assert harness.ripple.stats.calibrations_started == 1

    job = await _wait_done(harness)
    assert job["state"] == "done", job


# ---------------------------------------------------------------------------
# Test 10 — measured from `loading_started_at`, not from the request
# ---------------------------------------------------------------------------
async def test_10_the_sample_comes_from_omlx_loading_started_at(stub_factory,
                                                               ripple_factory, tmp_path):
    # oMLX's own clock says the load began 8 s ago; the load call returns after
    # 0.6 s. The two differ by more than a factor of ten, so the stored sample can
    # only match one of them.
    a = stub_factory(models=[StubModel(id="small", estimated_size=SMALL),
                             StubModel(id="big", estimated_size=BIG)],
                     load_simulates=True, load_delay=0.6,
                     load_started_at=time.time() - 8.0)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))], max_seconds=60.0))

    started = await _post(harness)
    assert started.status_code == 202, started.text
    job = await _wait_done(harness)
    assert job["state"] == "done", job

    samples = (await _get(harness))["samples"]
    assert len(samples) == 2
    request_seconds = 0.6 * len(samples)      # how long the calls took, at most
    for sample in samples:
        # oMLX's delta is ~8 s plus the run time, never the 0.6 s of the call.
        assert sample["seconds"] >= 7.0, sample
        assert abs(sample["seconds"] - 8.0) < abs(sample["seconds"] - request_seconds)


# ---------------------------------------------------------------------------
# Test 11 — the models are unloaded again, and evictions are reported
# ---------------------------------------------------------------------------
async def test_11_the_job_unloads_its_models_and_reports_evictions(
        stub_factory, ripple_factory, tmp_path):
    # Two models are resident before the job. The forecast predicts the big one
    # (`victim`, 200 GB) has to go; `surprise` (10 GB) fits. oMLX drops both.
    a = stub_factory(models=[
        StubModel(id="victim", estimated_size=200_000_000_000, loaded=True,
                  last_access=1.0),
        StubModel(id="surprise", estimated_size=10_000_000_000, loaded=True,
                  last_access=2.0),
        StubModel(id="small", estimated_size=SMALL),
        StubModel(id="big", estimated_size=BIG),
    ], load_simulates=True, load_delay=0.3)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    started = await _post(harness)
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["evicts"] == ["victim"], body

    # oMLX drops a second model while the load runs — exactly the case the
    # forecast cannot see, and the reason the job reports what it observed.
    await wait_until(lambda: bool(a.state.load_calls))
    a.state.model("surprise").loaded = False

    job = await _wait_done(harness)
    assert job["state"] == "done", job

    # every calibration model is gone again, and we asked for it
    for model in body["models"]:
        assert a.state.model(model).loaded is False, model
        assert model in a.state.unload_calls
    # `surprise` was never in the forecast, and it is gone — reported, not hidden.
    assert job["evicted_observed"] == ["surprise"], job
    assert "surprise" not in job["evicts"]


# ---------------------------------------------------------------------------
# 202 shape, and one job per node
# ---------------------------------------------------------------------------
async def test_post_202_names_the_job_models_and_evictions(stub_factory,
                                                           ripple_factory, tmp_path):
    a = stub_factory(models=_three_models(), load_simulates=True, load_delay=0.4)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))], samples=2))

    started = await _post(harness)
    assert started.status_code == 202, started.text
    body = started.json()
    assert set(body) == {"job", "node", "node_id", "models", "unloads", "evicts"}
    assert body["node"] == "A"
    assert body["node_id"] == a.state.node_id
    uuid.UUID(body["job"])                       # a real id, not a counter
    assert body["models"] == ["big", "small"]    # largest first, then smallest
    assert body["unloads"] == []                 # nothing resident, nothing to unload
    assert body["evicts"] == []
    assert harness.ripple.stats.calibrations_started == 1

    second = await _post(harness)
    assert second.status_code == 409, second.text
    assert "already running" in second.json()["error"]["message"]
    assert harness.ripple.stats.calibrations_started == 1

    job = await _wait_done(harness)
    assert job["state"] == "done", job


# ---------------------------------------------------------------------------
# End to end: the curve is written and the estimate changes
# ---------------------------------------------------------------------------
async def test_end_to_end_writes_the_measured_curve_to_the_calibration_file(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_three_models(), load_simulates=True, load_delay=0.05,
                     load_started_at=time.time() - 1.5)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))], samples=3))
    host = harness.host("A")
    path = tmp_path / "calibration.yaml"

    before = await _get(harness)
    assert before["source"] == "default"
    assert before["job"] is None
    default_estimate = host.estimate_load_seconds(100.0)
    assert default_estimate == pytest.approx(27.4, abs=0.3)   # the AS-5 curve

    started = await _post(harness)
    assert started.status_code == 202, started.text
    assert len(started.json()["models"]) == 3                  # small, big, mid
    job = await _wait_done(harness)
    assert job["state"] == "done", job
    assert harness.ripple.stats.calibrations_ok == 1

    # the file, keyed by node_id (FA-21), not by host name
    stored = engine.load(path)
    entry = stored.nodes[a.state.node_id]
    assert entry["source"] == "measured"
    assert entry["calibrated_by"] == "dashboard"
    # ISO-8601 with an offset, so a stored curve says when it was taken
    when = datetime.fromisoformat(entry["calibrated_at"])
    assert when.tzinfo is not None and when.year >= 2026
    assert len(entry["samples"]) == 3
    assert sorted(entry["models"]) == ["big", "mid", "small"]
    assert entry["samples"] == sorted(entry["samples"])
    assert engine.source_for(stored, a.state.node_id) == "measured"

    # the estimate now comes from this node's own curve
    after = await _get(harness)
    assert after["source"] == "measured"
    assert after["calibrated_by"] == "dashboard"
    assert [s["model"] for s in after["samples"]] == ["small", "mid", "big"]
    assert after["fit"]["points"] == 3
    measured_estimate = host.estimate_load_seconds(100.0)
    assert measured_estimate != pytest.approx(default_estimate)
    # The stub's loads are instantaneous, so the floor hides the arithmetic here.
    # What matters is that the host reads the curve this job stored, with this
    # configuration's safety factor — not that the number happens to differ.
    ld = harness.ripple.cfg.placement.loading
    assert measured_estimate == engine.estimate(
        stored, a.state.node_id, 100.0,
        safety_factor=ld.safety_factor, max_seconds=ld.max_seconds)


# ---------------------------------------------------------------------------
# The plan: a dry run the dialog shows before anything is loaded
# ---------------------------------------------------------------------------
async def test_plan_is_a_dry_run_available_before_any_job(stub_factory,
                                                          ripple_factory, tmp_path):
    a = stub_factory(models=[
        StubModel(id="resident", estimated_size=200_000_000_000, loaded=True,
                  last_access=1.0),
        StubModel(id="small", estimated_size=SMALL),
        StubModel(id="big", estimated_size=BIG),
    ])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))], samples=2))

    plan = (await _get(harness))["plan"]
    assert plan["error"] is None
    assert plan["models"] == ["big", "small"]
    # 200 + 400 + 40 GB does not fit in 0.95 × 487,7 GB; the LRU model goes.
    assert plan["evicts"] == ["resident"]
    # read-only: nothing was loaded, nothing unloaded, no job started
    assert a.state.load_calls == []
    assert a.state.unload_calls == []
    assert (await _get(harness))["job"] is None


async def test_plan_error_with_fewer_than_two_candidate_models(stub_factory,
                                                               ripple_factory, tmp_path):
    a = stub_factory(models=[StubModel(id="only", estimated_size=BIG)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    plan = (await _get(harness))["plan"]
    assert plan["models"] == []
    assert plan["error"] and "at least two" in plan["error"]

    refused = await _post(harness)
    assert refused.status_code == 409, refused.text
    assert "at least two" in refused.json()["error"]["message"]
    assert a.state.load_calls == []


async def test_a_plan_needs_a_size_ratio_of_three(stub_factory, ripple_factory,
                                                  tmp_path):
    # 100 GB and 200 GB say nothing about the slope: two points that close are
    # one point with noise.
    a = stub_factory(models=[StubModel(id="a", estimated_size=100_000_000_000),
                             StubModel(id="b", estimated_size=200_000_000_000)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    plan = (await _get(harness))["plan"]
    assert plan["error"] and "3×" in plan["error"]
    assert (await _post(harness)).status_code == 409


async def test_a_resident_model_stays_untouched_while_fresh_ones_span_the_range(
        stub_factory, ripple_factory, tmp_path):
    # A resident model has to be unloaded before it can be measured, so it is
    # only taken when the node offers nothing else. Here two models that are not
    # loaded already span 10×, and the job leaves the resident one alone.
    a = stub_factory(models=[stub_model_loaded("resident", estimated_size=MID),
                             StubModel(id="small", estimated_size=SMALL),
                             StubModel(id="big", estimated_size=BIG)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    plan = (await _get(harness))["plan"]
    assert plan["error"] is None, plan
    assert plan["models"] == ["big", "small"]
    assert plan["unloads"] == []


async def test_a_node_out_of_the_pool_refuses(stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_three_models())
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))
    harness.host("A").status = "down"

    refused = await _post(harness)
    assert refused.status_code == 409
    assert "not in the pool" in refused.json()["error"]["message"]
    assert harness.ripple.stats.calibrations_started == 0


# ---------------------------------------------------------------------------
# The live finding of 15.11.2026: `loading_started_at` is not an epoch
# ---------------------------------------------------------------------------
async def test_a_monotonic_loading_started_at_falls_back_to_the_load_call(
        stub_factory, ripple_factory, tmp_path):
    # Measured live on M3-beta: oMLX reports a monotonic clock value there
    # (158021.3 s after boot) and keeps it constant while the load runs. Read as
    # epoch it is ~57 years in the past, so a naive measurement stores ~1.8e9
    # seconds and the whole curve becomes worthless. `/load` blocks until the
    # model is resident, so the wall time of the call is the honest fallback.
    a = stub_factory(models=[StubModel(id="small", estimated_size=SMALL),
                             StubModel(id="big", estimated_size=BIG)],
                     load_simulates=True, load_delay=0.4,
                     load_started_at=158_021.299)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))], max_seconds=60.0))

    started = await _post(harness)
    assert started.status_code == 202, started.text
    job = await _wait_done(harness)
    assert job["state"] == "done", job

    samples = (await _get(harness))["samples"]
    assert len(samples) == 2
    for sample in samples:
        assert 0 < sample["seconds"] < 30, sample      # never 1.8 billion


async def test_a_model_the_node_cannot_load_is_skipped_not_fatal(
        stub_factory, ripple_factory, tmp_path):
    # Live finding, 15.11.2026: `Qwen3.8-Flash-Next-DFlash2` is a drafter and oMLX
    # answers 409 ("81 parameters not in model"). Aborting the job would throw away
    # the loads already paid for — and on that node it would have made calibration
    # impossible, because the drafter is the only very small model. Skip it, keep
    # the sizes that do load, and say so.
    a = stub_factory(models=[
        StubModel(id="small", estimated_size=SMALL),
        StubModel(id="mid", estimated_size=MID),
        StubModel(id="big", estimated_size=BIG, load_fails=True),
    ], load_simulates=True, load_delay=0.05)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))], samples=3))

    started = await _post(harness)
    assert started.status_code == 202, started.text
    job = await _wait_done(harness)

    assert job["state"] == "done", job
    assert [s["model"] for s in job["skipped"]] == ["big"]
    samples = (await _get(harness))["samples"]
    assert [s["model"] for s in samples] == ["small", "mid"]
    assert all(s["seconds"] > 0 for s in samples)


async def test_the_entry_model_preference_must_not_destroy_the_spread(
        stub_factory, ripple_factory, tmp_path):
    # Live finding, 15.11.2026: on M3-beta the only small model (6,5 GB) was
    # resident on the entry host, so "do not load the operator's model" dropped it
    # and left 111,6 vs 159,2 GB — a 1,4× range, which says nothing about the
    # slope and made calibration impossible. The preference applies only while a
    # 3× spread survives without those models.
    tiny = StubModel(id="tiny", estimated_size=6_500_000_000)
    mid = StubModel(id="mid", estimated_size=111_600_000_000)
    big = StubModel(id="big", estimated_size=159_200_000_000)
    a = stub_factory(models=[tiny, mid, big])
    b = stub_factory(models=[StubModel(id="tiny", estimated_size=6_500_000_000,
                                      loaded=True)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a)),
        host_config("B", b.url, node_id=_node_id(b), role="entry")]))

    plan = (await _get(harness, "A"))["plan"]

    assert plan["error"] is None, plan
    assert "tiny" in plan["models"], plan


# ---------------------------------------------------------------------------
# Who the curve belongs to, and when it must not be taken at all
# ---------------------------------------------------------------------------
async def test_the_curve_follows_the_node_id_not_the_host_name(
        stub_factory, ripple_factory, tmp_path):
    """Spec §10 case 13: a host may be renamed in `config.yaml` at any time.

    The store is keyed by `node_id`, so the curve survives the rename — and the
    GET of the renamed host must still report `measured`, not `default`.
    """
    a = stub_factory(models=[StubModel(id="small", estimated_size=SMALL),
                             StubModel(id="big", estimated_size=BIG)],
                     load_simulates=True, load_delay=0.05)
    first = await ripple_factory(_cfg(tmp_path, [
        host_config("M3-beta", a.url, node_id=a.state.node_id)]))
    assert (await _post(first, "M3-beta")).status_code == 202
    job = await _wait_done(first, "M3-beta")
    assert job["state"] == "done", job

    # the same machine under a new name: nothing else in the config changed
    second = await ripple_factory(_cfg(tmp_path, [
        host_config("M3-renamed", a.url, node_id=a.state.node_id)]))
    got = await _get(second, "M3-renamed")

    assert got["node_id"] == a.state.node_id
    assert got["source"] == "measured"
    assert [s["size_gb"] for s in got["samples"]] == [40.0, 400.0]
    assert second.host("M3-renamed").estimate_load_seconds(400.0) == \
        engine.estimate(engine.load(tmp_path / "calibration.yaml"), a.state.node_id, 400.0)


async def test_an_unhealthy_node_is_not_calibrated(stub_factory, ripple_factory,
                                                   tmp_path):
    """§6.3.1: a node that cannot answer is not a measurement.

    `force` overrides the thread check and nothing else, so it must not open this
    door either.
    """
    a = stub_factory(models=[StubModel(id="small", estimated_size=SMALL),
                             StubModel(id="big", estimated_size=BIG)])
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    host.healthy = False
    host.status = "degraded"
    host.last_error = "read timeout"

    for query in ("", "?force=1"):
        resp = await harness.client.post(f"/admin/nodes/A/calibrate{query}", headers=ADMIN)
        assert resp.status_code == 409, resp.text
        assert "not healthy" in resp.json()["error"]["message"]
    assert a.state.load_calls == []
    assert (await _get(harness))["job"] is None


# ---------------------------------------------------------------------------
# Issue #4 — a fully resident node is calibrated, not refused
# ---------------------------------------------------------------------------
async def test_a_fully_resident_node_is_calibrated_and_left_as_it_was(
        stub_factory, ripple_factory, tmp_path):
    """Live finding, 15.11.2026, `docs/loading-state-live-2026-11-15.md` §10.

    The M5's allow-list holds exactly two models and both are normally resident,
    so the plan had nothing to load and answered
    `409 ... offers 0 usable model(s) that are not loaded`. The operator had to
    unload both by hand and reload them afterwards. The job can do that itself:
    unload, measure the load, load it back.
    """
    a = stub_factory(models=[
        StubModel(id="small", estimated_size=SMALL, loaded=True),
        StubModel(id="big", estimated_size=BIG, loaded=True),
    ], load_simulates=True, load_delay=0.1)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    plan = (await _get(harness))["plan"]
    assert plan["error"] is None, plan
    assert plan["models"] == ["big", "small"]
    assert plan["unloads"] == ["big", "small"]
    # the forecast must not count them twice: they leave before the first load
    assert plan["evicts"] == []

    started = await _post(harness)
    assert started.status_code == 202, started.text
    assert started.json()["unloads"] == ["big", "small"]
    job = await _wait_done(harness)
    assert job["state"] == "done", job
    assert job["restore_failed"] == []
    assert job["evicted_observed"] == []

    # two real loads were measured — a resident model would have given zero
    samples = (await _get(harness))["samples"]
    assert [s["model"] for s in samples] == ["small", "big"]
    assert all(s["seconds"] > 0 for s in samples), samples
    # ...and the residency is what it was before the job: loaded again, by us
    assert a.state.model("small").loaded is True
    assert a.state.model("big").loaded is True
    assert a.state.load_calls.count("big") == 2      # once to measure, once back
    assert a.state.load_calls.count("small") == 2


async def test_a_model_that_cannot_be_reloaded_is_reported(stub_factory,
                                                           ripple_factory, tmp_path):
    """The acceptance for the restore: it is reported, not swallowed.

    The measurement succeeds, so the curve is stored — but the node is missing a
    model it had before, and that has to be visible in `restore_failed` and in
    the job's `error`.
    """
    a = stub_factory(models=[
        StubModel(id="small", estimated_size=SMALL, loaded=True),
        StubModel(id="big", estimated_size=BIG, loaded=True),
    ], load_simulates=True, load_delay=0.05)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    # The measurement works, the restore afterwards does not — the shape of a
    # model whose file went away, or a node that refuses it on the second try.
    host = harness.host("A")
    real_load = host.backend.load
    seen: list[str] = []

    async def flaky(model_id: str, *, timeout: float = 600.0) -> bool:
        seen.append(model_id)
        if model_id == "small" and seen.count("small") > 1:
            return False
        return await real_load(model_id, timeout=timeout)

    host.backend.load = flaky        # type: ignore[method-assign]

    started = await _post(harness)
    assert started.status_code == 202, started.text
    job = await _wait_done(harness)

    assert job["state"] == "done", job          # the measurement itself was fine
    assert job["restore_failed"] == ["small"], job
    assert "small" in (job["error"] or ""), job
    # the curve is written anyway: the sample is valid, the node is not
    assert len((await _get(harness))["samples"]) == 2
    assert a.state.model("big").loaded is True
    assert a.state.model("small").loaded is False


async def test_a_failed_job_still_puts_the_residency_back(stub_factory,
                                                          ripple_factory, tmp_path):
    """The restore is not the success path's privilege.

    `big` cannot be loaded at all: the job unloads it, the load is refused, it is
    skipped, and with one sample the job fails. Both models were resident before
    it started, so both have to be again — and the one that will not come back is
    named.
    """
    a = stub_factory(models=[
        StubModel(id="small", estimated_size=SMALL, loaded=True),
        StubModel(id="big", estimated_size=BIG, loaded=True, load_fails=True),
    ], load_simulates=True, load_delay=0.05)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    assert (await _post(harness)).status_code == 202
    job = await _wait_done(harness)

    assert job["state"] == "failed", job
    assert "only 1 load" in job["error"], job          # the job's own reason
    assert job["restore_failed"] == ["big"], job       # ...and what would not return
    assert "could not reload: big" in job["error"], job
    assert a.state.model("small").loaded is True       # untouched by the failure
    assert a.state.model("big").loaded is False        # reported, not hidden


async def test_a_single_resident_model_still_refuses(stub_factory, ripple_factory,
                                                     tmp_path):
    """The 409 stays for the real case: fewer than two models that could be
    measured at all — resident or not, there is no curve to be had from one."""
    a = stub_factory(models=[StubModel(id="only", estimated_size=BIG, loaded=True)],
                     load_simulates=True)
    harness = await ripple_factory(_cfg(tmp_path, [
        host_config("A", a.url, node_id=_node_id(a))]))

    plan = (await _get(harness))["plan"]
    assert plan["models"] == []
    assert plan["unloads"] == []
    assert plan["error"] and "at least two" in plan["error"]

    refused = await _post(harness)
    assert refused.status_code == 409, refused.text
    assert a.state.unload_calls == []
    assert a.state.model("only").loaded is True
