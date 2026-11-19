"""Maintenance windows: taking a node out of service (FA-22, second amendment).

A maintenance window is the operator's "this machine is mine for the next hour".
It must not touch oMLX, must not change a health verdict, must keep running
requests alive, and must survive the RIPPLE restart that a deploy causes — which
is precisely when an operator sets it.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ripple.config import PlacementConfig
from ripple.maintenance import Maintenance, Window, load, save

from tests.support import (StubModel, bearer, chat_body, host_config, host_row,
                           keyed_config,
                           make_config, wait_until)

ADMIN = bearer("test-admin-key")
MODEL = "m1"


def test_a_test_config_never_points_at_the_operator_s_state_files():
    """#15: the maintenance window is the one piece of state that takes a real
    node out of the pool, so `make_config` must redirect it to a temporary path
    exactly like the calibration curve and the model policy. Without this a test
    that sets a window writes `~/.ripple/maintenance.yaml` and the next start of
    the service keeps a production node out of the pool."""
    cfg = make_config([host_config("M5-alpha", "http://192.0.2.11:8000")])
    stores = {
        "calibration_file": cfg.placement.loading.calibration_file,
        "model_policy_file": cfg.placement.model_policy_file,
        "maintenance_file": cfg.placement.maintenance_file,
    }
    for name, path in stores.items():
        assert not str(path).startswith(str(Path.home())), (name, path)
        assert str(path).startswith(tempfile.gettempdir()), (name, path)


def _cfg(tmp_path, hosts):
    cfg = keyed_config(hosts, str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(maintenance_file=str(tmp_path / "maintenance.yaml"),
                                    model_policy_file=str(tmp_path / "models.yaml"))
    return cfg


def _models() -> list[StubModel]:
    return [StubModel(id=MODEL, estimated_size=100_000_000_000)]


async def _out(harness, name: str = "A", **params):
    return await harness.client.post(f"/admin/nodes/{name}/maintenance",
                                     headers=ADMIN, params=params)


async def _in(harness, name: str = "A"):
    return await harness.client.delete(f"/admin/nodes/{name}/maintenance", headers=ADMIN)


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------
async def test_a_node_in_maintenance_leaves_the_pool_but_keeps_its_health(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    assert harness.host("A").in_pool

    resp = await _out(harness)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["in_pool"] is False
    assert body["status"] == "healthy"          # the probe verdict is untouched
    assert body["state"] == "maintenance"       # the reporting state is not
    assert body["maintenance"]["node"] == "A"

    # /healthz tells the same story, so the dashboard and the soak report see it.
    row = next(h for h in (await harness.healthz())["hosts"] if h["name"] == "A")
    assert row["state"] == "maintenance"
    assert row["status"] == "healthy"
    assert row["maintenance"]["by"] == "dashboard"

    await _in(harness)
    assert harness.host("A").in_pool
    row = next(h for h in (await harness.healthz())["hosts"] if h["name"] == "A")
    assert row["state"] == "healthy" and row["maintenance"] is None


async def test_new_threads_go_to_the_other_host(stub_factory, ripple_factory, tmp_path):
    a, b = stub_factory(models=_models()), stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _out(harness, "A")
    for _ in range(3):
        resp = await harness.ok(chat_body(MODEL, "a fresh question " * 20, turns=2),
                                headers=client)
        assert resp.headers["x-ripple-host"] == "B"


async def test_a_bound_thread_falls_back_when_its_host_leaves(
        stub_factory, ripple_factory, tmp_path):
    """The point of the whole feature: a session survives the update."""
    a, b = stub_factory(models=_models()), stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    # B is out first, so the thread has to start on A.
    await _out(harness, "B")
    first = await harness.ok(chat_body(MODEL, "opening question " * 20, turns=2),
                             headers=client)
    assert first.headers["x-ripple-host"] == "A"
    thread = first.headers["x-ripple-thread"]

    # Now the roles swap: A goes out for its update, B comes back.
    await _in(harness, "B")
    await _out(harness, "A")

    second = await harness.ok(chat_body(MODEL, "opening question " * 20, turns=3),
                              headers=client)
    assert second.headers["x-ripple-host"] == "B"
    assert second.headers["x-ripple-thread"] == thread     # same session
    # FA-11's fallback is the path that moved it — not a new thread.
    assert second.headers["x-ripple-reason"] == "fallback"
    assert (await harness.stats())["fallbacks"] >= 1


async def test_the_last_host_out_is_a_503_and_reaches_nobody(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)
    await _out(harness)

    resp = await harness.chat(chat_body(MODEL, "hello", turns=2), headers=client)
    assert resp.status_code == 503
    assert a.state.chat_bodies == []            # nothing was sent to the node


# ---------------------------------------------------------------------------
# Persistence and expiry
# ---------------------------------------------------------------------------
async def test_the_window_survives_a_restart(stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models())
    cfg = _cfg(tmp_path, [host_config("A", a.url)])
    harness = await ripple_factory(cfg)
    await _out(harness, "A", minutes=60)

    # A deploy restarts RIPPLE while the node is out — the node must stay out.
    again = await ripple_factory(cfg)
    assert again.host("A").maintenance is not None
    assert again.host("A").in_pool is False
    assert again.host("A").maintenance["minutes_left"] > 50


async def test_minutes_end_the_window_by_themselves(stub_factory, ripple_factory,
                                                    tmp_path):
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    await _out(harness, "A", minutes=30)
    assert harness.host("A").maintenance["minutes_left"] == pytest.approx(30, abs=1)

    # Rather than waiting half an hour: age the window and let the purge run.
    harness.ripple.maintenance.windows["A"].until = (
        datetime.now().astimezone() - timedelta(seconds=1))
    assert harness.ripple.maintenance.prune() == ["A"]
    harness.ripple._apply_windows()
    assert harness.host("A").in_pool
    assert harness.host("A").maintenance is None


def test_a_calibration_window_is_never_written_to_disk(tmp_path):
    """A job window exists only while the job runs; a crash must not keep it."""
    path = tmp_path / "maintenance.yaml"
    store = Maintenance(path=path)
    store.set("A", by="dashboard", note="update")
    store.set("B", by="calibration", note="job 123")
    save(store)

    text = path.read_text(encoding="utf-8")
    assert "A" in text and "B" not in text
    assert set(load(path).windows) == {"A"}


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
async def test_the_endpoints_need_the_admin_key_and_know_their_nodes(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    url = "/admin/nodes/A/maintenance"

    assert (await harness.client.post(url)).status_code == 401
    assert (await harness.client.delete(url)).status_code == 401
    assert (await harness.client.get(url)).status_code == 401
    assert harness.host("A").maintenance is None      # a denied call changed nothing

    assert (await harness.client.get(url, headers=bearer("wrong"))).status_code == 401
    assert (await harness.client.get("/admin/nodes/NOPE/maintenance",
                                     headers=ADMIN)).status_code == 404
    assert (await harness.client.post("/admin/nodes/NOPE/maintenance",
                                      headers=ADMIN)).status_code == 404


def test_a_window_without_minutes_has_no_end(tmp_path):
    store = load(tmp_path / "nothing.yaml")
    win = store.set("A", note="until I say otherwise")
    assert win.until is None and win.minutes_left() is None
    assert store.is_out("A")
    assert Window(node="A").active() is True


# ---------------------------------------------------------------------------
# One state, two endpoints (issue #90)
# ---------------------------------------------------------------------------
async def _agreed_state(harness, name: str = "A") -> str:
    """The `state` both endpoints report about one node — and the comparison.

    `/healthz → hosts[].state` and `GET /admin/nodes/{name}/maintenance → state`
    describe the same moment. The assertion is that they agree; the caller checks
    the word once, so the rule is not written down a second time here either.
    """
    health = host_row(await harness.healthz(), name)["state"]
    resp = await harness.client.get(f"/admin/nodes/{name}/maintenance", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    admin = resp.json()["state"]
    assert admin == health, f"two answers for {name}: /healthz {health!r}, admin {admin!r}"
    return health


async def test_the_two_endpoints_never_disagree_about_one_node(
        stub_factory, ripple_factory, tmp_path):
    """The four states the page can show, read from both paths and compared.

    The measured bug: `_maintenance_view` re-derived the word on its own and knew
    neither the drain nor the disable, so during a `disable` drain `/healthz`
    said `draining` while the admin answer about the same node said `healthy`.
    """
    a = stub_factory(models=_models())
    a.state.delay = 0.5                     # long enough to observe the drain
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)

    # 1. nothing done: the probe verdict, on both paths
    assert await _agreed_state(harness) == "healthy"

    # 2. a maintenance window
    assert (await _out(harness, "A", minutes=30)).status_code == 200
    assert await _agreed_state(harness) == "maintenance"
    assert (await _in(harness, "A")).status_code == 200

    # 3. a disable that drains: the request is still running, so the node is on
    #    its way out — this is where the two answers used to split.
    running = asyncio.create_task(
        harness.ok(chat_body(MODEL, "a slow answer " * 20, turns=2), headers=client))
    await wait_until(lambda: harness.host("A").inflight == 1)
    disabling = asyncio.create_task(
        harness.client.post("/admin/nodes/A/disable", headers=ADMIN,
                            params={"timeout": 10}))
    await wait_until(lambda: harness.host("A").draining)
    assert await _agreed_state(harness) == "draining"

    # ... and `draining` means the drain, not "still busy": the busy nuance has a
    # name of its own, so one field cannot mean two things on two paths.
    view = (await harness.client.get("/admin/nodes/A/maintenance", headers=ADMIN)).json()
    assert view["draining"] is True
    assert view["work_in_progress"] is True

    assert (await disabling).status_code == 200
    assert (await running).status_code == 200

    # 4. the drain is over and the node stays out: disabled on both paths
    assert await _agreed_state(harness) == "disabled"
    view = (await harness.client.get("/admin/nodes/A/maintenance", headers=ADMIN)).json()
    assert view["draining"] is False and view["work_in_progress"] is False
