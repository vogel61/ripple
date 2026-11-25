"""A node that was updated and came back: the facts are read again (issue #86).

The version used to be read in exactly one place, `resolve()`. A node that is
updated and restarted cleanly is back within seconds — too short for the flap
guard to ever call it `down` — so `resolve()` never ran again and `/healthz`, the
fleet card and `docs/measurements.md` kept reporting the old engine (`0.8.0rc1`
while the node ran `0.8.0`).

What this file measures is the pair of properties that make the fix right rather
than merely louder:

- **the transition** — `down`/`degraded`/`loading` → `healthy` re-reads what the
  node says (version, model list, capabilities, memory), so the change is visible
  within one discovery interval, without restarting RIPPLE;
- **not the poll** — a node that stays healthy is *not* re-read, because that
  would be a request per node per interval for data that changes when an operator
  updates a machine. The counts below are the whole point of the second half.
"""

from __future__ import annotations

import logging

from ripple.config import HostConfig, PlacementConfig
from ripple.hosts import CURVE_MIN_BUCKETS, HostRuntime

from tests.support import (closed_port, host_config, host_row, make_config,
                           stub_model_loaded)

OLD = "0.8.0rc1"
NEW = "0.8.0"


def _one_host(stub, **placement) -> object:
    return make_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        placement=PlacementConfig(**placement) if placement else None)


# ---------------------------------------------------------------------------
# 1. The transition that never reaches `down` (the reported case)
# ---------------------------------------------------------------------------
async def test_a_clean_restart_updates_the_version_without_a_resolve(
        stub_factory, ripple_factory):
    """One failed probe, then healthy again: `degraded` → `healthy`, no `down`.

    This is the shape of a node update — the service is gone for a few seconds,
    so the flap guard never fires and `resolve()` never runs. The version must
    follow anyway, in `/healthz` (which is what the fleet card renders).
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")

    assert host.version == OLD
    assert host_row(await harness.healthz(), "A")["version"] == OLD

    a.state.health_status = "error"           # the node goes away ...
    await host.refresh()
    assert host.status == "degraded"          # ... but not far enough to be `down`
    assert host.version == OLD

    a.state.version = NEW                     # ... and comes back updated
    a.state.health_status = "healthy"
    await host.refresh()

    assert host.status == "healthy"
    assert host.version == NEW
    assert host_row(await harness.healthz(), "A")["version"] == NEW


# ---------------------------------------------------------------------------
# 2. The other path: `down` → healthy, so `resolve()` and the transition agree
# ---------------------------------------------------------------------------
async def test_a_node_that_was_down_reports_the_version_it_came_back_with(
        stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a, flap_guard_failures=2))
    host = harness.host("A")
    assert host.version == OLD

    a.state.health_status = "error"
    await host.refresh()
    await host.refresh()                      # the flap guard has now said `down`
    assert host.status == "down"

    a.state.version = NEW
    a.state.health_status = "healthy"
    await host.refresh()

    assert host.status == "healthy"
    assert host.version == NEW
    assert host_row(await harness.healthz(), "A")["version"] == NEW


# ---------------------------------------------------------------------------
# 3. A change is one log line, both values, once
# ---------------------------------------------------------------------------
async def test_a_version_change_is_logged_once_with_both_values(
        stub_factory, ripple_factory, caplog):
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")

    a.state.health_status = "error"
    await host.refresh()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ripple.hosts"):
        a.state.version = NEW
        a.state.health_status = "healthy"
        await host.refresh()
        # A node that stays on the new version must not repeat it.
        await host.refresh()

    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("version host=")]
    assert lines == [f"version host=A old={OLD} new={NEW}"]


# ---------------------------------------------------------------------------
# 4. The other half: a node that stays healthy is not re-read
# ---------------------------------------------------------------------------
async def test_a_node_that_stays_healthy_is_not_re_read(stub_factory, ripple_factory):
    """Three intervals, no transition: the counts must not move.

    `/api/cluster/node_id` (where `version()` reads from) and `/v1/models` are the
    two requests the re-read costs. Over three intervals of a node that stays
    healthy, neither may grow — and `/v1/models/status`, which the probe loop
    reads on every tick anyway, may grow by exactly one per tick, so a re-read
    hidden inside the steady path would still show up here.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")

    before = {name: a.state.count(name) for name in ("identity", "models", "status")}
    for _ in range(3):
        await host.refresh()
    after = {name: a.state.count(name) for name in ("identity", "models", "status")}

    assert after["identity"] == before["identity"]      # no version request
    assert after["models"] == before["models"]          # no /v1/models request
    assert after["status"] == before["status"] + 3      # exactly one per tick
    assert host.version == OLD


# ---------------------------------------------------------------------------
# 5. The model list follows the node too, not only on a `resolve()`
# ---------------------------------------------------------------------------
async def test_the_model_list_is_re_read_on_the_transition(stub_factory, ripple_factory):
    """A node that comes back with one model fewer reports the new list.

    `models_read_at` is asserted as well: the list has to be read on the
    transition even though it was read successfully before and never produced an
    error — that is the condition the old code keyed on.
    """
    a = stub_factory(models=[stub_model_loaded("m1"), stub_model_loaded("m2")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")
    assert set(host.models) == {"m1", "m2"}
    read_at = host.models_read_at

    a.state.health_status = "error"
    await host.refresh()

    a.state.models = [stub_model_loaded("m1")]     # the update dropped one
    a.state.health_status = "healthy"
    await host.refresh()

    assert set(host.models) == {"m1"}
    assert host.models_read_at > read_at
    assert host.model_available("m1") is True
    assert host.model_available("m2") is False
    row = host_row(await harness.healthz(), "A")
    assert row["models_available"] == 1
    assert row["models_loaded"] == ["m1"]


# ---------------------------------------------------------------------------
# 6. And the capabilities, which were only ever probed at start-up
# ---------------------------------------------------------------------------
async def test_the_capabilities_are_re_read_on_the_transition(stub_factory,
                                                             ripple_factory):
    """A node whose admin key arrives late gains `residency` on its next transition.

    Before issue #86 the capability set was probed once (`Fleet.start()`), so a
    node that was updated — or one that got its oMLX admin key after RIPPLE
    started — stayed "cannot report residency" for the lifetime of the process,
    and FA-18 stayed switched off fleet-wide.
    """
    a = stub_factory(models=[stub_model_loaded("m1")], models_status_code=401)
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")
    assert "residency" not in host.capabilities
    assert host.model_list_unreadable is True

    a.state.models_status_code = 200          # the key is there now
    a.state.health_status = "error"
    await host.refresh()
    a.state.health_status = "healthy"
    await host.refresh()

    assert "residency" in host.capabilities
    assert host.model_list_unreadable is False
    assert host_row(await harness.healthz(), "A")["capabilities"] == \
        sorted(host.capabilities)


# ---------------------------------------------------------------------------
# The concurrency curve per model (issue #80)
# ---------------------------------------------------------------------------
def _bare_host() -> HostRuntime:
    cfg = HostConfig(name="N", addresses=["http://n.test:8000"])
    h = HostRuntime(cfg=cfg, client=None)
    h.healthy = True
    h.status = "healthy"
    h.identity_ok = True
    return h


def _seed(h: HostRuntime, model: str, points: dict[int, float], *, base: float) -> None:
    step = 0
    for n, rate in sorted(points.items()):
        for _ in range(CURVE_MIN_BUCKETS):
            h.series_ring.add(model, float(n), rate, now=base - 600 + step * 5)
            step += 1


def test_the_curve_is_per_model_and_does_not_mix_two_speeds() -> None:
    """One box, two models, two very different rates.

    A 400 GB DeepSeek and a 9B Qwen on the same host do not share a curve: the
    mean over both describes neither, so the decision asks for the model it is
    about. Without a model the answer stays the node's own (the `/healthz` view),
    which is the old behaviour and must not change.
    """
    import time
    h = _bare_host()
    base = time.time()
    _seed(h, "deepseek", {1: 12.0, 2: 11.0}, base=base)
    _seed(h, "qwen", {1: 77.4, 2: 60.0}, base=base)

    assert h.curve(model="qwen") == {1.0: 77.4, 2.0: 60.0}
    assert h.curve(model="deepseek") == {1.0: 12.0, 2.0: 11.0}
    # Across all models: the mean of both, which is exactly the number nobody
    # should decide on.
    assert h.curve()[1.0] == 44.7


def test_relative_load_per_model_uses_that_models_own_rate() -> None:
    """`relative_load(model)` = 1 − that model's EMA / that model's capability.

    The M5 case from the issue: 77.4 tok/s alone, 64.7 now → 0.164. The other
    model on the same box is untouched by that, and its own load is its own.
    """
    import time
    h = _bare_host()
    base = time.time()
    _seed(h, "qwen", {1: 77.4}, base=base)
    _seed(h, "deepseek", {1: 12.0}, base=base)
    h.metrics["qwen"] = {"tg_tps": 64.7, "tg_samples": 5}
    h.metrics["deepseek"] = {"tg_tps": 6.0, "tg_samples": 5}

    assert h.relative_load("qwen") == 0.1641
    assert h.relative_load("deepseek") == 0.5
    assert h.capability_rate("qwen") == 77.4
    # Without a model the old behaviour is unchanged, and this is what it costs:
    # the denominator is the mean of both curves (44.7) and the numerator the
    # served model's EMA (deepseek, 6.0) — a ratio between two different models.
    # Nothing breaks because of it; the optimizer just must not decide on it.
    assert h.relative_load() == 0.8658


def test_a_model_without_a_curve_has_no_load_and_no_capability() -> None:
    """No measurement is `None`, never a guessed rate — the optimizer's rule."""
    import time
    h = _bare_host()
    _seed(h, "qwen", {2: 60.0}, base=time.time())   # no n = 1 point
    h.metrics["qwen"] = {"tg_tps": 30.0, "tg_samples": 5}

    assert h.capability_rate("qwen") is None
    assert h.relative_load("qwen") is None
    assert h.curve(model="deepseek") == {}


# ---------------------------------------------------------------------------
# 7. The fleet's intersection follows the node (issue #88)
#
# #86 taught a node to re-read its capabilities on the transition into
# `healthy`. The node's own view followed; the *fleet's* did not — `Fleet
# .capabilities` (the intersection every routing decision is checked against)
# was computed in `start()` and `add()` only, so a node that lost or gained
# `residency` on an update left the fleet on the old set until RIPPLE restarted.
# The two tests below hold both directions, and the third holds the price: the
# change is reported on the transition, not bought with a request per poll.
# ---------------------------------------------------------------------------
async def test_a_node_that_loses_residency_takes_it_out_of_the_fleet(
        stub_factory, ripple_factory, caplog):
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")
    assert "residency" in harness.ripple.fleet.capabilities
    assert (await harness.healthz())["capabilities"]["residency"] is True

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ripple.hosts"):
        a.state.models_status_code = 401      # the update came back without the key
        a.state.health_status = "error"
        await host.refresh()
        a.state.health_status = "healthy"
        await host.refresh()                  # the transition re-reads the node
        await host.refresh()                  # a steady tick must not repeat it

    assert "residency" not in host.capabilities
    assert "residency" not in harness.ripple.fleet.capabilities
    caps = (await harness.healthz())["capabilities"]
    assert caps["residency"] is False
    assert caps["eviction_guard"] is False
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("capabilities changed")]
    assert lines == ["capabilities changed host=A availability=off "
                     "prefix_cache=off residency=off"]


async def test_a_node_that_gains_residency_gives_it_back_to_the_fleet(
        stub_factory, ripple_factory, caplog):
    """The other direction: the key arrives late, the fleet gains the capability.

    Same shape as `test_the_capabilities_are_re_read_on_the_transition` above,
    one level up: not the host row but the intersection `/healthz` reports and
    placement checks.
    """
    a = stub_factory(models=[stub_model_loaded("m1")], models_status_code=401)
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")
    assert "residency" not in harness.ripple.fleet.capabilities

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ripple.hosts"):
        a.state.models_status_code = 200
        a.state.health_status = "error"
        await host.refresh()
        a.state.health_status = "healthy"
        await host.refresh()

    assert "residency" in harness.ripple.fleet.capabilities
    caps = (await harness.healthz())["capabilities"]
    assert caps["residency"] is True
    assert caps["eviction_guard"] is True
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("capabilities changed")]
    assert lines == ["capabilities changed host=A availability=on "
                     "prefix_cache=on residency=on"]


def test_the_change_line_names_the_capability_and_the_direction(caplog) -> None:
    """One line, with the capability and the direction (the format #88 asks for).

    `capabilities changed host=M5 residency=on` — not a diff, not a repeat. The
    first read of a node is not a change (there is nothing to compare it
    against), and a set that stays as it is says nothing.
    """
    h = HostRuntime(cfg=HostConfig(name="M5", addresses=["http://m5.test:8000"]),
                    client=None)
    with caplog.at_level(logging.INFO, logger="ripple.hosts"):
        h._note_capabilities(frozenset({"residency", "capacity"}))   # first read
        h._note_capabilities(frozenset({"residency", "capacity"}))   # unchanged
        h._note_capabilities(frozenset({"capacity"}))                # lost
        h._note_capabilities(frozenset({"capacity"}))                # still lost
        h._note_capabilities(frozenset({"capacity", "residency"}))   # gained

    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("capabilities changed")]
    assert lines == ["capabilities changed host=M5 residency=off",
                     "capabilities changed host=M5 residency=on"]


async def test_a_steady_state_does_not_probe_the_capabilities(
        stub_factory, ripple_factory):
    """Three ticks without a transition: not one request more than before #88.

    `/v1/models` is the request a capability probe costs (`probe_capabilities`
    reads the list to decide `availability`); it must not move. `/v1/models
    /status` is the poll's own read and moves by exactly one per tick.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")
    caps, fleet_caps = set(host.capabilities), set(harness.ripple.fleet.capabilities)

    before = {n: a.state.count(n) for n in ("identity", "models", "status")}
    for _ in range(3):
        await host.refresh()
    after = {n: a.state.count(n) for n in ("identity", "models", "status")}

    assert after["models"] == before["models"]           # no capability probe
    assert after["identity"] == before["identity"]       # no re-read of the version
    assert after["status"] == before["status"] + 3       # exactly one per tick
    assert set(host.capabilities) == caps
    assert set(harness.ripple.fleet.capabilities) == fleet_caps



# ---------------------------------------------------------------------------
# One reporting state (issue #90)
# ---------------------------------------------------------------------------
async def test_the_reporting_state_has_one_order(stub_factory, ripple_factory):
    """A drain outranks a disable, which outranks a window, which outranks the probe.

    `reporting_state()` is the only place this order is written down: `/healthz`
    reads it through `to_dict()` and the admin maintenance view calls it directly,
    so the two cannot drift apart. The probe verdict stays in `status`.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")
    assert host.reporting_state() == "healthy"

    host.maintenance = {"node": "A", "until": None}
    assert host.reporting_state() == "maintenance"

    host.cfg.enabled = False
    assert host.reporting_state() == "disabled"

    host.begin_drain(30)
    assert host.reporting_state() == "draining"
    assert host.status == "healthy"          # the probe verdict is never overwritten
    host.end_drain()


# ---------------------------------------------------------------------------
# 12. A capability note that does not change is not repeated (issue #170)
# ---------------------------------------------------------------------------
async def test_an_unchanged_capability_note_is_logged_once(
        stub_factory, ripple_factory, caplog, monkeypatch):
    """The same unreadable model list was logged on every refresh.

    Measured in an `init` check on 23.11.: two identical pairs of
    `capability host=… availability missing` / `residency missing` lines for one
    node, because `_read_capabilities` logged its notes unconditionally while
    `_note_model_list` had logged on the transition all along.
    """
    from ripple import hosts as hosts_module

    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")
    notes = ["availability missing: /v1/models unreadable (HTTPStatusError)",
             "residency missing: no loaded models with size readable"]

    async def fake_probe(backend, *, health=None):
        return frozenset(), list(notes), []

    monkeypatch.setattr(hosts_module, "probe_capabilities", fake_probe)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ripple.hosts"):
        await host._read_capabilities()
        await host._read_capabilities()
        await host._read_capabilities()

    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("capability host=")]
    assert len(lines) == len(notes), lines
    assert all(host.cfg.name in line for line in lines)


# ---------------------------------------------------------------------------
# 13. A node that only resolves after start-up joins the intersection (#190)
# ---------------------------------------------------------------------------
async def test_a_node_that_resolves_late_joins_the_fleet_intersection(
        stub_factory, ripple_factory, caplog):
    """The start-up read is a node's first read *and* the fleet's only chance.

    Measured on the live instance 24.11.2026: neither node was resolvable when
    the process started (oMLX was still coming up, `unresolved host=… node_id
    unreadable`), so `_resolve_all()` built an empty intersection. Both nodes
    resolved minutes later and reported all six capabilities — and the fleet
    stayed empty for ever, because the first read of a node was "not a change"
    and never told the fleet. `/healthz` then showed `capabilities.*: false`
    while every host row listed six: FA-18 off, RB-9 unverified, no restart
    that would fix it short of one that is not allowed to happen.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    cfg = make_config([host_config("A", f"http://127.0.0.1:{closed_port()}",
                                   node_id=a.state.node_id)])
    harness = await ripple_factory(cfg)
    host = harness.host("A")

    # At start-up there is nothing to intersect: the node never answered.
    assert host.backend is None
    assert harness.ripple.fleet.capabilities == frozenset()
    assert (await harness.healthz())["capabilities"]["residency"] is False

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ripple.hosts"):
        host.cfg.addresses[:] = [a.url]      # the node answers from now on
        await host.refresh()                 # no restart, one discovery tick

    assert host.status == "healthy"
    assert "residency" in host.capabilities
    assert set(harness.ripple.fleet.capabilities) == set(host.capabilities)
    caps = (await harness.healthz())["capabilities"]
    assert caps["residency"] is True
    assert caps["eviction_guard"] is True
    # The first read is still not a *change*: the fleet hears about it, the
    # operator does not get a "changed" line for a set that never changed.
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("capabilities changed")]
    assert lines == []


async def test_a_re_resolve_does_not_open_a_hole_in_the_intersection(
        stub_factory, ripple_factory, monkeypatch):
    """A second `resolve()` swaps in a backend that knows no capability yet.

    `Backend.capabilities` starts empty on a fresh object, so in the window
    between the swap and the re-read the host answers `frozenset()` — and any
    other node that recomputes in that window (a capability change, an `add()`,
    the `_resolve_all()` pass itself) pulls the fleet's intersection down to
    nothing and logs `fleet_capabilities lost=…` about a node that can do
    everything. The set the node last reported therefore travels into the new
    backend (issue #190).
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")
    fleet = harness.ripple.fleet
    before = set(fleet.capabilities)
    assert "residency" in before
    old = host.backend

    # Observe the window between the swap and the re-read: what a concurrent
    # recompute would have intersected, and what it would have left behind.
    real = host._read_capabilities
    seen: list[frozenset[str]] = []

    async def spy(health=None):
        seen.append(host.capabilities)
        fleet._recompute_capabilities()
        seen.append(fleet.capabilities)
        await real(health)

    monkeypatch.setattr(host, "_read_capabilities", spy)
    await host.reinit()                      # drop the resolution, probe again

    assert host.backend is not old
    assert seen[0] == frozenset(before)      # the fresh backend is not empty
    assert seen[1] == frozenset(before)      # … and the fleet keeps the set
    assert set(fleet.capabilities) == before


# ---------------------------------------------------------------------------
# 3. A profile is not a second engine (issue #195)
# ---------------------------------------------------------------------------
GB = 10**9


async def test_a_profile_row_does_not_add_memory_twice(
        stub_factory, ripple_factory):
    """One 112 GB engine with two profiles is 112 GB, not 336 GB.

    oMLX builds a profile row (`X:agent`, `X:think-1m`) as a copy of the source
    model's status, `actual_size` included, so summing every resident row
    charged the engine once per profile. The user saw 449.6 GB against a
    260.4 GB ceiling (172.7 %) while oMLX's own `current_model_memory` said
    40 %. The payload's `source_model_id` is the only marker: the row is a view
    of a model already counted.
    """
    a = stub_factory(
        models=[
            stub_model_loaded("X", estimated_size=112 * GB),
            stub_model_loaded("X:agent", estimated_size=112 * GB,
                              source_model_id="X"),
            stub_model_loaded("X:think-1m", estimated_size=112 * GB,
                              source_model_id="X"),
        ],
        final_ceiling=260_400_000_000,
    )
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")

    assert host.used_bytes_measured() == 112 * GB
    row = host_row(await harness.healthz(), "A")
    assert row["used_bytes_measured"] == 112 * GB
    # The card's percentage is measured against the ceiling; before the fix it
    # read 129 %, which is the number the user reported.
    assert row["used_bytes_measured"] / row["ceiling_bytes"] < 1.0


async def test_two_different_models_still_add_up(stub_factory, ripple_factory):
    """The guard against over-correcting: only a profile row is skipped.

    A second engine is a real resident model and must keep its `actual_size` in
    the sum, even when both rows are loaded side by side.
    """
    a = stub_factory(models=[
        stub_model_loaded("m1", estimated_size=112 * GB),
        stub_model_loaded("m2", estimated_size=8 * GB),
    ])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")

    assert host.used_bytes_measured() == 120 * GB

