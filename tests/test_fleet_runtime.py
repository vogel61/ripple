"""The fleet changes while it runs (issue #70).

A private setup is frozen at start-up: a new machine means editing `config.yaml`
and restarting, and a removal kills every request in flight on the node it
removes. A pool changes while it runs. This file is the contract of that change:

* `POST /admin/nodes` resolves the address, reads `node_id`, models and memory,
  and inserts the host into the running fleet — no restart, and `/healthz` says
  `source: overlay`;
* `DELETE /admin/nodes/{name}` stops routing, waits for `inflight == 0` with a
  deadline, releases the node's thread slots and only then removes it. A drain
  that runs out of time says what is still running and leaves the node disabled
  rather than half-removed;
* the runtime overlay lives in `~/.ripple/runtime.yaml` (atomic write) and is
  merged over `config.yaml` at start-up. `config.yaml` is the baseline: a node
  from it may be disabled, never deleted; a node added in the overlay may be
  deleted;
* since #78 the same file also carries the routing policy (`affinity.*`,
  `placement.*`), set through `PATCH`/`DELETE /admin/config/{section}` and
  validated by the loader's own parser.

No test touches the network: the nodes are the stub upstreams from
`tests/support.py`, and no test writes `~/.ripple/runtime.yaml`.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from ripple.config import (ConfigError, FleetConfig, FleetOverlay, HostConfig,
                           apply_overlay, host_block, load_config, load_overlay,
                           save_overlay)
from ripple.placement import threads_that_would_move
from ripple.registry import Thread

from tests.support import (StubModel, bearer, chat_body, host_config, host_row,
                           keyed_config, make_config, wait_until)

ADMIN = bearer("test-admin-key")
MODEL = "m1"
OTHER = "m2"


def _models(*ids: str) -> list[StubModel]:
    return [StubModel(id=m, estimated_size=100_000_000_000) for m in ids]


def _cfg(tmp_path, hosts, *, persist: bool = False, drain_seconds: float = 5.0,
         auth: bool = True):
    """A config whose fleet overlay lives in `tmp_path`, never in `~/.ripple`."""
    cfg = (keyed_config(hosts, str(tmp_path / "keys.yaml")) if auth
           else make_config(hosts))
    cfg.fleet = FleetConfig(file=str(tmp_path / "runtime.yaml"), persist=persist,
                            drain_seconds=drain_seconds, drain_poll_seconds=0.01)
    # A fleet key, as `load_config` would take it from `defaults` (issue #182):
    # a node added at the page inherits it, and without one the merge warns.
    cfg.fleet_api_key = "stub-fleet-key"
    return cfg


def _two_hosts(stub_factory):
    a = stub_factory(models=_models(MODEL))
    b = stub_factory(models=_models(MODEL, OTHER))
    return a, b


async def _add(harness, address: str, name: str = "B", **extra):
    body = {"address": address, "name": name, **extra}
    return await harness.client.post("/admin/nodes", headers=ADMIN, json=body)


async def _chat(harness, body: dict):
    """One completion, with a client key — these configs have auth on.

    The admin key unlocks `/admin/*` and nothing else: a request on `/v1/*` still
    needs a key of its own, which is exactly the separation FA-23 asks for.
    """
    key = getattr(harness, "_test_key", None)
    if key is None:
        key = harness.ripple.keys.create(name="test", kind="interactive").key
        harness._test_key = key
    return await harness.chat(body, headers=bearer(key))


# ---------------------------------------------------------------------------
# The guard: a test must never read or write the operator's fleet
# ---------------------------------------------------------------------------
def test_a_test_config_never_points_at_the_operator_fleet():
    """Same rule the ledger and the maintenance windows have.

    `~/.ripple/runtime.yaml` is what the operator set at the page. A test that
    wrote it would make the next start of the live service build a fleet that no
    `config.yaml` declares — and a test that read it would inherit machines that
    are not its own.
    """
    cfg = make_config([host_config("alpha", "http://192.0.2.11:8000")])
    assert not str(cfg.fleet.file).startswith(str(Path.home()))
    assert str(cfg.fleet.file).startswith(tempfile.gettempdir())
    # …and nothing is read or written at all unless a test asks for the file.
    assert cfg.fleet.persist is False
    # The rename of #78 did not quietly move the default back into the home
    # directory, and the test path is the new name, not the old one.
    assert FleetConfig().file == "~/.ripple/runtime.yaml"
    assert Path(FleetConfig().file).expanduser() != Path(cfg.fleet.file)
    assert Path(cfg.fleet.file).name.endswith("runtime.yaml")


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------
async def test_a_node_added_at_the_page_joins_the_fleet_and_gets_traffic(
        stub_factory, ripple_factory, tmp_path):
    """The acceptance case: real traffic within a tick, no restart.

    The added node is the only one carrying `m2`, so a request for it can only be
    served by the new host — "it appears in /healthz" alone would not prove that
    placement ever sends anything there.
    """
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    resp = await _add(harness, b.url)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "overlay"
    # The identity was read from the node, not invented: that is what slots and
    # affinity hang off.
    assert body["node_id"] == b.state.node_id
    assert body["models"] == 2
    assert body["node"]["source"] == "overlay"

    health = await harness.healthz()
    row = host_row(health, "B")
    assert row["source"] == "overlay"
    assert row["address"] == b.url
    assert row["node_id"] == b.state.node_id
    assert host_row(health, "A")["source"] == "config"

    # Real traffic, on the very next request.
    answer = await _chat(harness, chat_body(OTHER, "hello there"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["model"] == OTHER
    assert b.state.chat_calls == 1
    assert a.state.chat_calls == 0


async def test_the_added_node_is_a_full_member_of_the_weight_accounting(
        stub_factory, ripple_factory, tmp_path):
    """Placement divides by `cfg.hosts`, so the new host has to be in both lists."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url, weight=1.0)]))
    assert (await _add(harness, b.url, weight=3.0)).status_code == 201

    health = await harness.healthz()
    assert health["threads"]["target_share"]["B"] == pytest.approx(0.75)
    assert host_row(health, "B")["weight"] == 3.0


async def test_a_node_that_does_not_answer_is_not_half_added(
        stub_factory, ripple_factory, tmp_path):
    """Resolve first, insert after: a failure leaves the fleet exactly as it was."""
    from tests.support import closed_port
    a, _ = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    resp = await _add(harness, f"http://127.0.0.1:{closed_port()}")
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "unresolved"
    assert "last_error" in resp.json()["error"]
    assert harness.ripple.fleet.get("B") is None
    assert harness.ripple.cfg.host("B") is None
    assert [h.cfg.name for h in harness.ripple.fleet.hosts] == ["A"]


async def test_an_identity_mismatch_is_reported_not_swallowed(
        stub_factory, ripple_factory, tmp_path):
    """A node that answers with another `node_id` is not the machine we were told
    to add — the refusal names the two identities."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    resp = await _add(harness, b.url, node_id="node-somebody-else")
    assert resp.status_code == 502, resp.text
    assert "identity_mismatch" in resp.json()["error"]["message"]
    assert harness.ripple.fleet.get("B") is None


async def test_adding_a_name_that_already_exists_is_refused(
        stub_factory, ripple_factory, tmp_path):
    """Two declarations of one machine would be two slot accounts for it."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _add(harness, b.url, name="A")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "duplicate_node"
    assert "config" in resp.json()["error"]["message"]


async def test_adding_without_an_address_is_a_400(ripple_factory, stub_factory, tmp_path):
    a, _ = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await harness.client.post("/admin/nodes", headers=ADMIN, json={"name": "B"})
    assert resp.status_code == 400
    assert "address" in resp.json()["error"]["message"]


async def test_an_invalid_host_block_is_a_400(ripple_factory, stub_factory, tmp_path):
    """The page is not a way around the validation the file gets."""
    a, _ = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _add(harness, "192.0.2.40:8000")       # no scheme
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_host"


async def test_the_add_is_behind_the_admin_key(stub_factory, ripple_factory, tmp_path):
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await harness.client.post("/admin/nodes", json={"address": b.url})
    assert resp.status_code in (401, 403), resp.text
    assert harness.ripple.fleet.get("B") is None


# ---------------------------------------------------------------------------
# Remove, with a drain
# ---------------------------------------------------------------------------
async def test_a_removal_waits_for_the_request_in_flight_and_it_completes(
        stub_factory, ripple_factory, tmp_path):
    """The defect this replaces: a removal used to kill what was running."""
    a, b = _two_hosts(stub_factory)
    b.state.delay = 0.4
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    assert (await _add(harness, b.url)).status_code == 201

    running = asyncio.create_task(_chat(harness, chat_body(OTHER, "slow answer")))
    await wait_until(lambda: harness.host("B").inflight == 1)
    resp = await harness.client.delete("/admin/nodes/B", headers=ADMIN,
                                       params={"timeout": 10})
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] is True
    assert resp.json()["inflight"] == 0

    answer = await running
    assert answer.status_code == 200, answer.text
    assert b.state.chat_calls == 1
    assert harness.ripple.fleet.get("B") is None
    assert "B" not in [row["name"] for row in (await harness.healthz())["hosts"]]


async def test_the_node_leaves_the_pool_at_once_while_it_drains(
        stub_factory, ripple_factory, tmp_path):
    """No new work on a draining node — that is what makes the wait finite."""
    a, b = _two_hosts(stub_factory)
    b.state.delay = 0.4
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    assert (await _add(harness, b.url)).status_code == 201

    running = asyncio.create_task(_chat(harness, chat_body(OTHER, "slow answer")))
    await wait_until(lambda: harness.host("B").inflight == 1)
    removing = asyncio.create_task(
        harness.client.delete("/admin/nodes/B", headers=ADMIN, params={"timeout": 10}))
    await wait_until(lambda: harness.host("B").draining)

    row = host_row(await harness.healthz(), "B")
    assert row["in_pool"] is False
    assert row["draining"] is True
    assert row["state"] == "draining"
    assert row["drain_remaining_seconds"] is not None
    # …and a request for the only model it carries is placed on A or refused,
    # never on the node that is leaving.
    assert "B" not in [h.cfg.name for h in harness.ripple.fleet.healthy_hosts()]

    await removing
    assert (await running).status_code == 200


async def test_a_drain_that_runs_out_of_time_says_what_is_running(
        stub_factory, ripple_factory, tmp_path):
    """The acceptance case for a request that never ends.

    Nothing is removed. The answer names the count and the model, and the node is
    left *disabled* — out of the pool, still in the fleet — instead of being
    pulled out from under the request.
    """
    a, b = _two_hosts(stub_factory)
    b.state.delay = 30.0
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    assert (await _add(harness, b.url)).status_code == 201

    stuck = asyncio.create_task(_chat(harness, chat_body(OTHER, "never ends")))
    await wait_until(lambda: harness.host("B").inflight == 1)

    resp = await harness.client.delete("/admin/nodes/B", headers=ADMIN,
                                       params={"timeout": 0.1})
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "drain_timeout"
    assert error["inflight"] == 1
    assert error["models"] == [{"model": OTHER, "inflight": 1,
                                "prefill": 1, "decode": 0}]
    assert "still has 1 request" in error["message"]

    host = harness.host("B")
    assert host.cfg.enabled is False        # disabled, not half-removed
    assert host.in_pool is False
    assert harness.ripple.fleet.get("B") is not None
    row = host_row(await harness.healthz(), "B")
    assert row["enabled"] is False
    assert row["state"] == "draining"

    stuck.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stuck


async def test_removing_a_node_from_the_config_is_refused_with_a_reason(
        stub_factory, ripple_factory, tmp_path):
    """The file is the baseline: a click may disable a machine, not delete it."""
    a, _ = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await harness.client.delete("/admin/nodes/A", headers=ADMIN)
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "config_host"
    assert "/admin/nodes/A/disable" in error["message"]
    assert harness.host("A").in_pool is True
    assert harness.ripple.overlay.hosts == {}
    assert harness.ripple.overlay.overrides == {}


async def test_a_removal_releases_the_thread_slots_of_the_node(
        stub_factory, ripple_factory, tmp_path):
    """A bound thread is a slot; a removed node must not keep counting them."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    assert (await _add(harness, b.url)).status_code == 201

    # Two turns: the thread holds a slot on B (FA-6/FA-19).
    assert (await _chat(harness, chat_body(OTHER, "conversation about slots",
                                           turns=2))).status_code == 200
    assert (await harness.slots())["B"] == 1

    resp = await harness.client.delete("/admin/nodes/B", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["threads_released"] == 1
    assert harness.ripple.registry.slots_on_host("B") == 0
    assert "B" not in (await harness.slots())


async def test_a_drain_does_not_wait_on_a_bound_thread_with_no_request(
        stub_factory, ripple_factory, tmp_path):
    """A thread slot is not in-flight work: the drain waits on requests only."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    assert (await _add(harness, b.url)).status_code == 201
    assert (await _chat(harness, chat_body(OTHER, "idle conversation",
                                           turns=2))).status_code == 200
    assert (await harness.slots())["B"] == 1

    resp = await harness.client.delete("/admin/nodes/B", headers=ADMIN,
                                       params={"timeout": 0.2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["waited_seconds"] < 0.2


# ---------------------------------------------------------------------------
# Disable / enable
# ---------------------------------------------------------------------------
async def test_disabling_a_config_node_takes_it_out_and_keeps_the_entry(
        stub_factory, ripple_factory, tmp_path):
    """The open decision: disabling drains like a removal, but keeps the entry."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))

    resp = await harness.client.post("/admin/nodes/B/disable", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["disabled"] is True
    assert resp.json()["drained"] is True

    host = harness.host("B")
    assert host.cfg.enabled is False
    assert host.in_pool is False
    assert host.status == "healthy"          # the probe verdict is untouched
    row = host_row(await harness.healthz(), "B")
    assert row["state"] == "disabled"
    assert row["source"] == "config"
    # The node stays in the fleet and in the file's world — only out of the pool.
    assert harness.ripple.fleet.get("B") is not None
    assert harness.ripple.overlay.overrides == {"B": {"enabled": False}}

    # Nothing is placed there any more.
    for _ in range(3):
        assert (await _chat(harness, chat_body(MODEL, "hello"))).status_code == 200
    assert b.state.chat_calls == 0


async def test_enabling_brings_the_node_back(stub_factory, ripple_factory, tmp_path):
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    assert (await harness.client.post("/admin/nodes/B/disable", headers=ADMIN)).status_code == 200

    resp = await harness.client.post("/admin/nodes/B/enable", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert harness.host("B").in_pool is True
    assert harness.ripple.overlay.overrides == {}
    assert host_row(await harness.healthz(), "B")["state"] == "healthy"


async def test_disabling_waits_for_the_request_in_flight(
        stub_factory, ripple_factory, tmp_path):
    a, b = _two_hosts(stub_factory)
    b.state.delay = 0.3
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    running = asyncio.create_task(_chat(harness, chat_body(OTHER, "slow")))
    await wait_until(lambda: harness.host("B").inflight == 1)

    resp = await harness.client.post("/admin/nodes/B/disable", headers=ADMIN,
                                     params={"timeout": 5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["drained"] is True
    assert (await running).status_code == 200
    assert harness.host("B").cfg.enabled is False


# ---------------------------------------------------------------------------
# Persistence: the overlay file
# ---------------------------------------------------------------------------
async def test_the_added_node_survives_a_restart_and_the_config_is_untouched(
        stub_factory, ripple_factory, tmp_path):
    """The acceptance case: read back from the overlay, `config.yaml` unchanged."""
    a, b = _two_hosts(stub_factory)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "listen_host": "127.0.0.1",
        "listen_port": 1,
        "hosts": [{"name": "A", "addresses": [a.url], "node_id": a.state.node_id}],
        "discovery": {"interval_seconds": 3600.0, "timeout_seconds": 2.0},
        "placement": {
            "maintenance_file": str(tmp_path / "maintenance.yaml"),
            "model_policy_file": str(tmp_path / "models.yaml"),
            "loading": {"calibration_file": str(tmp_path / "calibration.yaml")},
        },
        "stats": {"file": str(tmp_path / "host_stats.json"), "persist": False},
        "fleet": {"file": str(tmp_path / "runtime.yaml")},
        "auth": {"enabled": True, "admin_key": "test-admin-key",
                 "keys_file": str(tmp_path / "keys.yaml")},
    }), encoding="utf-8")
    before = config_path.read_bytes()

    first = await ripple_factory(load_config(config_path))
    assert (await _add(first, b.url, weight=2.0)).status_code == 201
    assert (tmp_path / "runtime.yaml").is_file()

    # A restart: a new Config from the same file, a new Ripple, a new fleet.
    cfg = load_config(config_path)
    assert cfg.fleet.file == str(tmp_path / "runtime.yaml")
    from ripple.proxy import Ripple
    second = Ripple(cfg)
    try:
        host = second.fleet.get("B")
        assert host is not None, "the node added at the page is gone after the restart"
        assert host.cfg.source == "overlay"
        assert host.cfg.weight == 2.0
        assert host.cfg.node_id == b.state.node_id
        assert host.cfg.addresses == [b.url]
        # The baseline is still exactly the file the operator wrote.
        assert config_path.read_bytes() == before
    finally:
        await second.fleet.stop()
        await second.proxy_client.aclose()


async def test_a_disabled_config_node_stays_disabled_across_a_restart(
        stub_factory, ripple_factory, tmp_path):
    a, b = _two_hosts(stub_factory)
    cfg = _cfg(tmp_path, [host_config("A", a.url), host_config("B", b.url)],
               persist=True)
    harness = await ripple_factory(cfg)
    assert (await harness.client.post("/admin/nodes/B/disable", headers=ADMIN)).status_code == 200

    overlay = load_overlay(tmp_path / "runtime.yaml")
    assert overlay.overrides == {"B": {"enabled": False}}

    # A restart reads the overlay before the fleet is built: B is out of it.
    from ripple.proxy import Ripple
    fresh = make_config([host_config("A", a.url), host_config("B", b.url)])
    fresh.fleet = FleetConfig(file=str(tmp_path / "runtime.yaml"), persist=True)
    second = Ripple(fresh)
    try:
        assert second.cfg.host("B").enabled is False
        assert second.fleet.get("B") is None
        assert second.cfg.host("A").enabled is True
    finally:
        await second.fleet.stop()
        await second.proxy_client.aclose()


def test_the_overlay_is_written_atomically_and_privately(tmp_path):
    """`.tmp` + fsync + rename, mode 0600 — an overlay host may carry a key."""
    path = tmp_path / "runtime.yaml"
    overlay = FleetOverlay(path=path)
    overlay.add("B", {"addresses": ["http://192.0.2.40:8000"], "api_key": "secret"})
    overlay.override("A", enabled=False)
    save_overlay(overlay)

    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not (tmp_path / "runtime.yaml.tmp").exists()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    # The #78 shape: the fleet's two maps live under `hosts`, the routing policy
    # under `policy`. Both keys are always written, so the file has one shape.
    assert raw["hosts"]["nodes"]["B"]["addresses"] == ["http://192.0.2.40:8000"]
    assert raw["hosts"]["overrides"] == {"A": {"enabled": False}}
    assert raw["policy"] == {}


def test_a_broken_overlay_does_not_stop_the_start(tmp_path, caplog):
    """A corrupt file is "nothing added" plus one warning — never a crash."""
    path = tmp_path / "runtime.yaml"
    path.write_text("{not: valid: yaml:", encoding="utf-8")
    overlay = load_overlay(path)
    assert overlay.hosts == {} and overlay.overrides == {}
    assert overlay.error is not None

    missing = load_overlay(tmp_path / "nope.yaml")
    assert missing.hosts == {} and missing.error is None


# ---------------------------------------------------------------------------
# The merge rule: config ↔ overlay
# ---------------------------------------------------------------------------
def test_a_config_node_can_only_be_disabled_never_deleted(tmp_path):
    """The rule of issue #70, at the merge: an override cannot remove a host."""
    cfg = make_config([host_config("A", "http://192.0.2.11:8000", weight=1.0)])
    overlay = FleetOverlay(path=tmp_path / "runtime.yaml")
    overlay.override("A", enabled=False, weight=2.0)
    warnings = apply_overlay(cfg, overlay)

    assert warnings == []
    assert [h.name for h in cfg.hosts] == ["A"]      # still declared
    assert cfg.host("A").enabled is False
    assert cfg.host("A").weight == 2.0
    assert cfg.host("A").source == "config"


def test_an_override_may_not_touch_a_config_hosts_identity(tmp_path):
    """`addresses` is the machine, `weight` is the operator: only the latter moves."""
    with pytest.raises(ConfigError):
        FleetOverlay().override("A", addresses=["http://10.0.0.1:8000"])


def test_an_override_for_an_unknown_host_is_a_warning_not_a_host(tmp_path):
    cfg = make_config([host_config("A", "http://192.0.2.11:8000")])
    overlay = FleetOverlay()
    overlay.override("typo", enabled=True)
    warnings = apply_overlay(cfg, overlay)
    assert [h.name for h in cfg.hosts] == ["A"]
    assert any("cannot add a host" in w for w in warnings)


def test_an_overlay_host_is_added_whole_and_marked_as_such():
    # A fleet key, because a node added at the page has none of its own (#182);
    # without one the merge warns, which `test_config.py` covers.
    cfg = make_config([host_config("A", "http://192.0.2.11:8000", api_key="k-fleet")])
    cfg.fleet_api_key = "k-fleet"          # as `load_config` reads it from defaults
    overlay = FleetOverlay()
    overlay.add("B", {"addresses": ["http://192.0.2.40:8000"], "node_id": "node-b",
                      "weight": 2.0, "site": "home"})
    assert apply_overlay(cfg, overlay) == []

    host = cfg.host("B")
    assert host is not None
    assert host.source == "overlay"
    assert host.node_id == "node-b"
    assert host.weight == 2.0
    assert host.api_key == "k-fleet"       # inherited, the overlay has no key field
    assert cfg.host("A").source == "config"


def test_a_name_in_both_keeps_the_config_identity_and_the_overlay_decisions():
    """The rule stated in the issue, in one place: identity from the file,
    weight/site/enabled from the page."""
    cfg = make_config([host_config("A", "http://192.0.2.11:8000", node_id="node-a")])
    overlay = FleetOverlay()
    # A hand-written overlay block for a host the config declares: ignored.
    overlay.add("A", {"addresses": ["http://10.9.9.9:8000"], "node_id": "node-other"})
    overlay.override("A", weight=3.0, site="home")
    warnings = apply_overlay(cfg, overlay)

    assert len(cfg.hosts) == 1
    assert cfg.host("A").addresses == ["http://192.0.2.11:8000"]
    assert cfg.host("A").node_id == "node-a"
    assert cfg.host("A").weight == 3.0
    assert cfg.host("A").site == "home"
    assert any("also declared in config.yaml" in w for w in warnings)


def test_the_overlay_round_trips_through_the_file(tmp_path):
    cfg = make_config([host_config("A", "http://192.0.2.11:8000", api_key="k")])
    overlay = FleetOverlay(path=tmp_path / "runtime.yaml")
    host = HostConfig(name="B", addresses=["http://192.0.2.40:8000"], node_id="node-b",
                      api_key="secret", weight=2.5, site="home", max_threads=12)
    overlay.add("B", host_block(host))
    overlay.override("A", enabled=False)
    save_overlay(overlay)

    read = load_overlay(tmp_path / "runtime.yaml")
    assert read.overrides == {"A": {"enabled": False}}
    assert read.hosts["B"]["api_key"] == "secret"
    assert apply_overlay(cfg, read) == []

    added = cfg.host("B")
    assert added.source == "overlay"
    assert added.api_key == "secret"
    assert added.max_threads == 12
    assert added.node_id == "node-b"
    assert cfg.host("A").enabled is False


def test_an_unusable_overlay_host_is_left_out_with_a_warning():
    """The overlay is not a way around the validation `config.yaml` gets."""
    cfg = make_config([host_config("A", "http://192.0.2.11:8000")])
    overlay = FleetOverlay()
    overlay.add("B", {"addresses": ["192.0.2.40:8000"]})     # no scheme
    warnings = apply_overlay(cfg, overlay)
    assert cfg.host("B") is None
    assert any("unusable" in w for w in warnings)


def test_the_overlay_never_writes_into_the_config_file(tmp_path):
    """`save_overlay` touches its own file and nothing else."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("hosts:\n- name: A\n  addresses: [http://10.0.0.1:8000]\n",
                           encoding="utf-8")
    before = config_path.read_bytes()
    cfg = make_config([host_config("A", "http://10.0.0.1:8000")])
    overlay = FleetOverlay(path=tmp_path / "runtime.yaml")
    overlay.add("B", {"addresses": ["http://10.0.0.2:8000"]})
    apply_overlay(cfg, overlay)
    save_overlay(overlay)
    assert config_path.read_bytes() == before


# ---------------------------------------------------------------------------
# /healthz says where a host came from
# ---------------------------------------------------------------------------
async def test_healthz_marks_every_host_with_its_source_and_the_overlay_state(
        stub_factory, ripple_factory, tmp_path):
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    assert (await _add(harness, b.url)).status_code == 201

    health = await harness.healthz()
    assert {row["name"]: row["source"] for row in health["hosts"]} == {
        "A": "config", "B": "overlay"}
    fleet = health["fleet"]
    assert fleet["overlay_hosts"] == ["B"]
    assert fleet["overlay_error"] is None
    assert fleet["drain_seconds"] == 5.0


# ---------------------------------------------------------------------------
# The fleet's intersection follows a node (issue #88)
# ---------------------------------------------------------------------------
async def test_the_intersection_follows_a_node_that_changes_without_a_restart(
        stub_factory, ripple_factory, tmp_path):
    """One node loses `residency`, the fleet loses it — and gets it back.

    `Fleet.capabilities` is the intersection every routing decision is checked
    against, and it used to be computed in `start()` and `add()` only: a node
    that came back from an update without its admin key left the fleet saying
    `residency: on` until RIPPLE restarted. Both directions are held here, and
    on two nodes, because the point is the *intersection*: A still reports the
    capability while B does not, and the fleet must not.
    """
    a, b = _two_hosts(stub_factory)
    cfg = _cfg(tmp_path, [host_config("A", a.url, node_id=a.state.node_id),
                          host_config("B", b.url, node_id=b.state.node_id)])
    harness = await ripple_factory(cfg)
    fleet = harness.ripple.fleet
    assert "residency" in fleet.capabilities
    assert (await harness.healthz())["capabilities"]["residency"] is True

    host_b = harness.host("B")
    b.state.models_status_code = 401        # the update came back without the key
    b.state.health_status = "error"
    await host_b.refresh()
    b.state.health_status = "healthy"
    await host_b.refresh()                  # one transition, no restart

    assert "residency" not in fleet.capabilities
    assert (await harness.healthz())["capabilities"]["residency"] is False
    assert "residency" in harness.host("A").capabilities   # A was not the one

    b.state.models_status_code = 200        # and back again
    b.state.health_status = "error"
    await host_b.refresh()
    b.state.health_status = "healthy"
    await host_b.refresh()

    assert "residency" in fleet.capabilities
    assert (await harness.healthz())["capabilities"]["residency"] is True


# ---------------------------------------------------------------------------
# The preview (issue #71): read a node, write nothing
# ---------------------------------------------------------------------------
# `POST /admin/nodes?dry_run=true` is the read half of the add: the node is
# asked the same four questions the write path asks (identity, model list,
# memory, round trip) on a `HostRuntime` that never joins the fleet. The page
# shows that answer, the operator confirms weight, site and role, and only then
# does the real `POST` follow. The tests below are about the one property that
# makes the confirmation honest: a preview changes nothing.
async def _preview(harness, address: str, name: str = "B", **extra):
    return await harness.client.post("/admin/nodes?dry_run=true", headers=ADMIN,
                                     json={"address": address, "name": name, **extra})


def _sites():
    """Two declared locations, one of which contains the stub's address."""
    from ripple.sites import SiteConfig
    return [SiteConfig(id="local", name="Local", networks=["127.0.0.1/32"]),
            SiteConfig(id="sa", name="site-a", networks=["192.0.2.0/24"])]


async def test_the_preview_answers_the_four_facts_and_writes_nothing(
        stub_factory, ripple_factory, tmp_path):
    """node_id, models, memory and the measured round trip — and no side effect.

    "Writes nothing" is the part that matters: the fleet, `/healthz` and the
    overlay file are the same after the call as before it. A preview that
    quietly added the node would make the confirmation a lie.
    """
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    before = await harness.healthz()

    resp = await _preview(harness, b.url)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["node_id"] == b.state.node_id
    assert body["models"] == sorted([MODEL, OTHER])
    assert body["model_count"] == 2
    assert body["ceiling_bytes"] == b.state.final_ceiling
    assert body["used_bytes"] is not None
    assert isinstance(body["rtt_ms"], float) and body["rtt_ms"] >= 0.0
    assert body["address"] == b.url
    # The three defaults the page offers in the preview.
    assert body["weight"] == 1.0
    assert body["role"] == "worker"
    assert body["step"] is None

    after = await harness.healthz()
    assert [h["name"] for h in after["hosts"]] == [h["name"] for h in before["hosts"]]
    assert harness.ripple.fleet.get("B") is None
    assert harness.ripple.overlay.hosts == {}
    assert (after["fleet"] or {}).get("overlay_hosts") == []
    assert not (tmp_path / "runtime.yaml").exists()


async def test_the_preview_is_the_read_half_and_the_add_is_the_write(
        stub_factory, ripple_factory, tmp_path):
    """The same answer, then the write — and only the write changes the fleet.

    The preview's `node_id` must be the identity the fleet stores, otherwise
    slots and affinity would hang off a value the page invented.
    """
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)], persist=True))
    preview = (await _preview(harness, b.url)).json()
    assert harness.ripple.fleet.get("B") is None
    # A preview does not write the overlay file, even when one is configured.
    assert not (tmp_path / "runtime.yaml").exists()

    resp = await _add(harness, b.url)
    assert resp.status_code == 201, resp.text
    host = harness.host("B")
    assert host.cfg.node_id == preview["node_id"]
    assert sorted(host.models) == preview["models"]
    assert (tmp_path / "runtime.yaml").exists()


async def test_a_preview_of_an_unreachable_address_names_the_step(
        stub_factory, ripple_factory, tmp_path):
    """502 with `step: "resolve"` — a sentence the page can print, not a trace.

    `127.0.0.1:1` refuses at once, so the test costs no timeout.
    """
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _preview(harness, "http://127.0.0.1:1")
    assert resp.status_code == 502, resp.text
    err = resp.json()["error"]
    assert err["code"] == "unresolved"
    assert err["step"] == "resolve"
    assert "could not be resolved" in err["message"]
    assert harness.ripple.fleet.get("127.0.0.1") is None
    assert harness.ripple.overlay.hosts == {}


async def test_a_preview_with_the_wrong_identity_names_the_node_id_step(
        stub_factory, ripple_factory, tmp_path):
    """A machine answers, but it is not the one that was asked for.

    That is a different failure from "nothing answers", and the page has to be
    able to say so: `resolve` would send the operator to the cable, `node_id`
    sends them to the machine's identity.
    """
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _preview(harness, b.url, node_id="not-this-machine")
    assert resp.status_code == 502, resp.text
    err = resp.json()["error"]
    assert err["step"] == "node_id"
    assert "identity_mismatch" in (err["last_error"] or "")


async def test_a_preview_without_a_readable_model_list_still_answers(
        stub_factory, ripple_factory, tmp_path):
    """The partial case: the node is there, its model list is not readable.

    oMLX answers 401 on `/v1/models` without a key. The node may still be
    added — placement just cannot use it by name yet — so the preview is a
    warning with a step, not a refusal.
    """
    a = stub_factory(models=_models(MODEL))
    b = stub_factory(models=_models(OTHER), models_status_code=401)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _preview(harness, b.url)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["node_id"] == b.state.node_id
    assert body["model_count"] == 0
    assert body["step"] == "models"
    assert body["models_error"]
    assert body["warnings"]
    # …and the write it previews still goes through.
    assert (await _add(harness, b.url)).status_code == 201


async def test_the_preview_refuses_a_duplicate_name_like_the_write_does(
        stub_factory, ripple_factory, tmp_path):
    """A name is an identity: two declarations would be two slot accounts."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _preview(harness, b.url, name="A")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "duplicate_node"


async def test_the_preview_shows_the_derived_site_and_the_mismatch(
        stub_factory, ripple_factory, tmp_path):
    """#67's comparison, in front of the write instead of after it.

    The address falls into `local`; the operator declares `sa`. The preview says
    so and the add still succeeds — a site is a location, not a routing rule.
    """
    a, b = _two_hosts(stub_factory)
    cfg = _cfg(tmp_path, [host_config("A", a.url)])
    cfg.sites = _sites()
    harness = await ripple_factory(cfg)

    resp = await _preview(harness, b.url, site="sa")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["site"] == "sa"
    assert body["site_derived"] == "local"
    assert body["site_mismatch"] is True
    assert any("site mismatch" in w for w in body["warnings"])

    assert (await _add(harness, b.url, site="sa")).status_code == 201
    row = host_row(await harness.healthz(), "B")
    assert row["site"] == "sa" and row["site_derived"] == "local"
    assert row["site_mismatch"] is True


async def test_the_preview_says_when_no_site_contains_the_address(
        stub_factory, ripple_factory, tmp_path):
    """Outside every declared site: a warning, not a refusal (acceptance #71)."""
    from ripple.sites import SiteConfig
    a, b = _two_hosts(stub_factory)
    cfg = _cfg(tmp_path, [host_config("A", a.url)])
    cfg.sites = [SiteConfig(id="sa", name="site-a", networks=["192.0.2.0/24"])]
    harness = await ripple_factory(cfg)
    resp = await _preview(harness, b.url)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["site_derived"] is None
    assert any("no declared site" in w for w in body["warnings"])
    assert (await _add(harness, b.url)).status_code == 201


async def test_the_preview_needs_the_admin_key(stub_factory, ripple_factory, tmp_path):
    """Reading a node is not a public service either — it is the same key."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await harness.client.post("/admin/nodes?dry_run=true", json={"address": b.url})
    assert resp.status_code == 401
    assert harness.ripple.fleet.get("B") is None


async def test_a_body_flag_is_the_same_as_the_query_flag(stub_factory, ripple_factory,
                                                         tmp_path):
    """`"dry_run": true` in the body, `?dry_run=true` in the query — one meaning."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await harness.client.post("/admin/nodes", headers=ADMIN,
                                     json={"address": b.url, "name": "B", "dry_run": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_run"] is True
    assert harness.ripple.fleet.get("B") is None


# ---------------------------------------------------------------------------
# Discovery (issue #71): the sweep the CLI runs, from the page
# ---------------------------------------------------------------------------
def _port(url: str) -> int:
    from urllib.parse import urlparse
    return urlparse(url).port


async def _scan(harness, cidr: str, port: int, **extra):
    return await harness.client.post("/admin/discover", headers=ADMIN,
                                     json={"cidr": cidr, "port": port, **extra})


async def test_discovery_finds_a_node_and_offers_it_with_its_preview(
        stub_factory, ripple_factory, tmp_path):
    """`ripple discover --scan 127.0.0.1/32` from the page: one candidate, read.

    The candidate carries the same four facts the address preview shows, so the
    operator can pick one and see the confirmation before anything is written.
    """
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _scan(harness, "127.0.0.1/32", _port(b.url), timeout=0.5)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cidr"] == "127.0.0.1/32"
    assert body["count"] == 1, body
    found = body["found"][0]
    assert found["address"] == b.url
    assert found["node_id"] == b.state.node_id
    assert found["models"] == sorted([MODEL, OTHER])
    assert found["latency_ms"] >= 0
    assert found["known"] is False
    # A sweep writes nothing either.
    assert harness.ripple.fleet.get("B") is None
    assert not (tmp_path / "runtime.yaml").exists()


async def test_discovery_marks_a_node_that_is_already_in_the_fleet(
        stub_factory, ripple_factory, tmp_path):
    """`known: true` — the page must not offer a machine twice.

    Two declarations of one node would be two slot accounts for it, which is
    what `POST /admin/nodes` refuses with 409.
    """
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _scan(harness, "127.0.0.1/32", _port(a.url), timeout=0.5)
    assert resp.status_code == 200, resp.text
    found = resp.json()["found"][0]
    assert found["known"] is True
    assert found["known_nodes"] == ["A"]


async def test_discovery_names_the_derived_site_of_each_candidate(
        stub_factory, ripple_factory, tmp_path):
    """The same derivation the preview uses — one rule, two places."""
    a, b = _two_hosts(stub_factory)
    cfg = _cfg(tmp_path, [host_config("A", a.url)])
    cfg.sites = _sites()
    harness = await ripple_factory(cfg)
    found = (await _scan(harness, "127.0.0.1/32", _port(b.url), timeout=0.5)).json()["found"]
    assert [f["site_derived"] for f in found] == ["local"]


async def test_discovery_of_an_empty_range_says_nothing_answered(
        stub_factory, ripple_factory, tmp_path):
    """Zero candidates is an answer, not an error: an empty range is normal."""
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _scan(harness, "127.0.0.1/32", 1, timeout=0.05)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] == [] and body["count"] == 0


async def test_discovery_refuses_a_range_wider_than_a_sixteenth(
        stub_factory, ripple_factory, tmp_path):
    """A /8 is 16 M addresses: refused with `step: "cidr"`, not waited out."""
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _scan(harness, "10.0.0.0/8", 8000)
    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == "bad_cidr"
    assert err["step"] == "cidr"


async def test_discovery_refuses_a_non_network(stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _scan(harness, "not-a-network", 8000)
    assert resp.status_code == 400
    assert resp.json()["error"]["step"] == "cidr"


async def test_discovery_needs_the_admin_key(stub_factory, ripple_factory, tmp_path):
    """A sweep is traffic on someone's network — it is not an anonymous call."""
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await harness.client.post("/admin/discover",
                                     json={"cidr": "127.0.0.1/32", "port": _port(a.url)})
    assert resp.status_code == 401


async def test_discovery_without_a_cidr_names_the_field(stub_factory, ripple_factory,
                                                        tmp_path):
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await harness.client.post("/admin/discover", headers=ADMIN, json={})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "cidr_required" and err["step"] == "cidr"


# ---------------------------------------------------------------------------
# The routing policy at the page (issue #78)
# ---------------------------------------------------------------------------
# One field per call on a dotted path, behind the admin key, validated by the
# loader's own parser. `config.yaml` stays the baseline: the value goes into the
# overlay, survives a restart, and the file the operator wrote is untouched.
def _policy_config(tmp_path, hosts, **placement) -> Path:
    """A real `config.yaml` with the overlay and the key store beside it.

    Needed where the acceptance case is about the *file*: `config.yaml` must be
    byte-identical after the click, and the value must come back after a restart
    that reads only the two files.
    """
    data = {
        "listen_host": "127.0.0.1",
        "listen_port": 1,
        "hosts": [{"name": name, "addresses": [stub.url],
                   "node_id": stub.state.node_id}
                  for name, stub in hosts],
        "discovery": {"interval_seconds": 3600.0, "timeout_seconds": 2.0},
        "placement": {
            "maintenance_file": str(tmp_path / "maintenance.yaml"),
            "model_policy_file": str(tmp_path / "models.yaml"),
            "loading": {"calibration_file": str(tmp_path / "calibration.yaml")},
            **placement,
        },
        "stats": {"file": str(tmp_path / "host_stats.json"), "persist": False},
        "fleet": {"file": str(tmp_path / "runtime.yaml")},
        "auth": {"enabled": True, "admin_key": "test-admin-key",
                 "keys_file": str(tmp_path / "keys.yaml")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


async def _patch_policy(harness, path: str, value, **extra):
    return await harness.client.patch(f"/admin/config/{path}", headers=ADMIN,
                                      json={"value": value, **extra})


async def _reset_policy(harness, path: str):
    return await harness.client.delete(f"/admin/config/{path}", headers=ADMIN)


def _bind(harness, key: str, host: str, model: str = MODEL) -> None:
    """Put a live thread on a host without a request — the state under test.

    A thread that is bound and idle is exactly what a weight change must not
    disturb, and seeding it directly is the only way to say where it sits
    instead of hoping the deficit rule put it there.
    """
    now = time.time()
    harness.ripple.registry.threads[key] = Thread(
        key=key, model=model, host=host, prefix="", source="header",
        created=now, last_seen=now)


async def test_a_policy_value_set_at_the_page_survives_a_restart(
        stub_factory, ripple_factory, tmp_path):
    """The acceptance case: in force, in the overlay, and `config.yaml` untouched."""
    a, b = _two_hosts(stub_factory)
    config_path = _policy_config(tmp_path, [("A", a), ("B", b)])
    before = config_path.read_bytes()
    harness = await ripple_factory(load_config(config_path))

    resp = await _patch_policy(harness, "affinity.ttl_seconds", 900)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"path": "affinity.ttl_seconds", "value": 900,
                           "source": "overlay", "effective": 900}

    # In force now — on the object the registry holds, not on a copy of it.
    assert harness.ripple.cfg.affinity.ttl_seconds == 900
    assert harness.ripple.registry.cfg.ttl_seconds == 900
    # In the overlay, and not in the file the operator wrote.
    raw = yaml.safe_load((tmp_path / "runtime.yaml").read_text(encoding="utf-8"))
    assert raw["policy"]["affinity"]["ttl_seconds"] == 900
    assert config_path.read_bytes() == before

    # `/config` says where the value comes from.
    view = (await harness.client.get("/config")).json()
    assert view["overrides"]["affinity.ttl_seconds"] == 900
    assert "affinity.ttl_seconds" in view["editable"]
    assert view["affinity"]["ttl_seconds"] == 900

    # A restart: both files, nothing else.
    from ripple.proxy import Ripple
    second = Ripple(load_config(config_path))
    try:
        assert second.cfg.affinity.ttl_seconds == 900
        assert second.cfg.overrides == {"affinity.ttl_seconds": 900}
    finally:
        await second.fleet.stop()
        await second.proxy_client.aclose()


async def test_an_invalid_value_is_refused_with_the_loaders_own_message(
        stub_factory, ripple_factory, tmp_path):
    """Same sentence the start-up would print, and nothing written."""
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                  host_config("B", b.url)]))

    resp = await _patch_policy(harness, "affinity.ttl_seconds", 0)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body == {"error": "invalid_value", "path": "affinity.ttl_seconds",
                    "message": "affinity.ttl_seconds must be >= 1, got 0"}
    assert harness.ripple.cfg.affinity.ttl_seconds == 1800
    assert not (tmp_path / "runtime.yaml").exists()

    # The cross-field check of the host parser comes along for free: a
    # spill_threshold above max_inflight is refused with the loader's words.
    resp = await _patch_policy(harness, "hosts.A.spill_threshold", 99)
    assert resp.status_code == 400
    assert "must not exceed max_inflight" in resp.json()["message"]


@pytest.mark.parametrize("path", [
    "listen_port",
    "auth.admin_key",
    "hosts.A.api_key",
    "hosts.A.addresses",
    "affinity.header",
    "nonsense.path",
])
async def test_a_path_that_is_not_policy_answers_403_not_404(
        stub_factory, ripple_factory, tmp_path, path):
    """The field exists; it is simply not settable without a restart."""
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    resp = await _patch_policy(harness, path, 1)
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["error"] == "not_editable"
    assert body["path"] == path
    assert body["message"]
    # …and a DELETE says the same thing instead of quietly doing nothing.
    assert (await _reset_policy(harness, path)).status_code == 403


async def test_the_policy_endpoints_need_the_admin_key(stub_factory, ripple_factory,
                                                       tmp_path):
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await harness.client.patch("/admin/config/affinity.ttl_seconds",
                                      json={"value": 900})
    assert resp.status_code == 401
    resp = await harness.client.delete("/admin/config/affinity.ttl_seconds")
    assert resp.status_code == 401


async def test_a_reset_drops_the_override_and_the_file_value_returns(
        stub_factory, ripple_factory, tmp_path):
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                  host_config("B", b.url)],
                                       persist=True))
    assert (await _patch_policy(harness, "affinity.ttl_seconds", 900)).status_code == 200

    resp = await _reset_policy(harness, "affinity.ttl_seconds")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"path": "affinity.ttl_seconds", "value": 1800,
                           "source": "config", "effective": 1800}
    assert harness.ripple.cfg.affinity.ttl_seconds == 1800

    raw = yaml.safe_load((tmp_path / "runtime.yaml").read_text(encoding="utf-8"))
    assert raw["policy"] == {}
    view = (await harness.client.get("/config")).json()
    assert "affinity.ttl_seconds" not in view["overrides"]

    # Idempotent: a second reset is the state the operator asked for, not an error.
    assert (await _reset_policy(harness, "affinity.ttl_seconds")).status_code == 200


async def test_a_weight_change_asks_first_and_names_how_many_threads_would_move(
        stub_factory, ripple_factory, tmp_path):
    """A weight applies to new threads only — so the click is a confirmation.

    Two threads sit on A and one on B with equal weights; dropping B's weight to
    0.2 makes the deficit rule prefer A for everything, which would pull B's one
    conversation off its prefix cache if it were applied to it. The 409 names
    that count; `confirm: true` applies the weight and leaves every thread where
    it is.
    """
    a = stub_factory(models=_models(MODEL))
    b = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                  host_config("B", b.url)],
                                       persist=True))
    _bind(harness, "sid:one", "A")
    _bind(harness, "sid:two", "A")
    _bind(harness, "sid:three", "B")
    before = {t.key: t.host for t in harness.ripple.registry.threads.values()}

    expected = threads_that_would_move(harness.ripple.fleet, harness.ripple.cfg,
                                       harness.ripple.registry, {"B": 0.2})
    assert expected > 0, "the scenario is chosen so that a thread would move"

    resp = await _patch_policy(harness, "hosts.B.weight", 0.2)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "confirm_required"
    assert body["path"] == "hosts.B.weight"
    assert body["threads_that_would_move"] == expected
    assert body["message"]
    # Nothing happened: no weight, no file.
    assert harness.ripple.cfg.host("B").weight == 1.0
    assert not (tmp_path / "runtime.yaml").exists()

    resp = await _patch_policy(harness, "hosts.B.weight", 0.2, confirm=True)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"path": "hosts.B.weight", "value": 0.2,
                           "source": "overlay", "effective": 0.2}
    assert harness.ripple.cfg.host("B").weight == 0.2
    # Existing threads keep their host until they end.
    assert {t.key: t.host for t in harness.ripple.registry.threads.values()} == before
    raw = yaml.safe_load((tmp_path / "runtime.yaml").read_text(encoding="utf-8"))
    assert raw["hosts"]["overrides"] == {"B": {"weight": 0.2}}


async def test_a_weight_change_with_nothing_to_move_needs_no_confirmation(
        stub_factory, ripple_factory, tmp_path):
    """An empty fleet is not a redistribution: the value applies at once."""
    a = stub_factory(models=_models(MODEL))
    b = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                  host_config("B", b.url)],
                                       persist=True))
    resp = await _patch_policy(harness, "hosts.A.weight", 2.0)
    assert resp.status_code == 200, resp.text
    assert harness.ripple.cfg.host("A").weight == 2.0


async def test_a_per_host_policy_value_set_at_the_page_is_in_force_at_once(
        stub_factory, ripple_factory, tmp_path):
    """`spill_threshold` and `prefer_resident` are policy, and they land."""
    a = stub_factory(models=_models(MODEL))
    b = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                  host_config("B", b.url)],
                                       persist=True))

    assert (await _patch_policy(harness, "hosts.A.spill_threshold", 2)).status_code == 200
    assert (await _patch_policy(harness, "hosts.B.prefer_resident", False)).status_code == 200
    assert harness.ripple.cfg.host("A").spill_threshold == 2
    assert harness.ripple.cfg.host("B").prefer_resident is False
    # The fleet's own host objects read the same field — no restart, no copy.
    assert harness.ripple.fleet.get("A").cfg.spill_threshold == 2

    raw = yaml.safe_load((tmp_path / "runtime.yaml").read_text(encoding="utf-8"))
    assert raw["hosts"]["overrides"] == {"A": {"spill_threshold": 2},
                                         "B": {"prefer_resident": False}}


async def test_a_weight_on_a_node_added_at_the_page_survives_a_restart(
        stub_factory, ripple_factory, tmp_path):
    """The node lives in `hosts.nodes`, its weight in `hosts.overrides`.

    Both are the same file, and the merge has to read them in the right order:
    the node first (it is not in `config.yaml`), the override on top. Getting
    that wrong loses the weight on the next start and warns instead.
    """
    a, b = _two_hosts(stub_factory)
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)],
                                       persist=True))
    assert (await _add(harness, b.url, name="B")).status_code == 201

    resp = await _patch_policy(harness, "hosts.B.weight", 2.0)
    assert resp.status_code == 200, resp.text
    assert harness.ripple.cfg.host("B").weight == 2.0

    raw = yaml.safe_load((tmp_path / "runtime.yaml").read_text(encoding="utf-8"))
    assert "B" in raw["hosts"]["nodes"]
    assert raw["hosts"]["overrides"] == {"B": {"weight": 2.0}}

    from ripple.proxy import Ripple
    second = Ripple(_cfg(tmp_path, [host_config("A", a.url)], persist=True))
    try:
        assert second.cfg.warnings == [w for w in second.cfg.warnings
                                       if "fleet overlay" not in w]
        host = second.cfg.host("B")
        assert host is not None and host.source == "overlay"
        assert host.weight == 2.0
    finally:
        await second.fleet.stop()
        await second.proxy_client.aclose()


async def test_a_host_that_is_not_in_the_fleet_is_403_with_the_names_it_has(
        stub_factory, ripple_factory, tmp_path):
    a = stub_factory(models=_models(MODEL))
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    resp = await _patch_policy(harness, "hosts.alpha.weight", 2.0)
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["error"] == "not_editable"
    assert "alpha" in body["message"] and "A" in body["message"]
