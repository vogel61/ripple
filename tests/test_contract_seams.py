"""The frozen surface of docs/loading-state-contract.md (wave W0).

These tests do not exercise the loading state machine — that is W1. They pin the
seams the later waves build on: the config keys, oMLX's loading signals through
the stub and the backend, and the mounted (still unimplemented) admin endpoints.
A later wave that breaks one of these breaks the contract, not just a test.
"""

from __future__ import annotations

import httpx
import pytest

from ripple.backends import OmlxBackend
from ripple.config import ConfigError, load_config

from tests.support import (bearer, host_config, keyed_config, make_config,
                           StubModel, StubUpstream)


MINIMAL = """
hosts:
  - name: alpha
    addresses: ["http://127.0.0.1:8000"]
    node_id: 22222222-2222-4222-8222-222222222222
"""


def _write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# -- config surface ---------------------------------------------------------
def test_loading_config_defaults_are_the_documented_ones(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    ld = cfg.placement.loading
    assert ld.enabled is True
    assert ld.safety_factor == pytest.approx(1.3)
    assert ld.max_seconds == pytest.approx(900.0)
    assert ld.calibration_file == "~/.ripple/calibration.yaml"
    assert ld.samples == 3
    # 3 = 30 s at discovery.interval_seconds 10 (docs/loading-state.md §7)
    assert cfg.placement.flap_guard_failures == 3


def test_loading_config_is_read_from_the_file(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL + """
placement:
  flap_guard_failures: 2
  loading:
    enabled: false
    safety_factor: 2.0
    max_seconds: 120
    calibration_file: /tmp/cal.yaml
    samples: 2
"""))
    ld = cfg.placement.loading
    assert ld.enabled is False
    assert ld.safety_factor == pytest.approx(2.0)
    assert ld.max_seconds == pytest.approx(120.0)
    assert ld.calibration_file == "/tmp/cal.yaml"
    assert ld.samples == 2
    assert cfg.placement.flap_guard_failures == 2


def test_flap_guard_failures_below_one_is_rejected(tmp_path):
    # 0 would mean "never down" — a host that is gone would stay in the pool for
    # ever. 1 is the documented way back to the old behaviour.
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, MINIMAL + """
placement:
  flap_guard_failures: 0
"""))


def test_loading_block_appears_in_the_effective_config(tmp_path):
    # /config and the dashboard's parameter table read Config.summary().
    cfg = load_config(_write(tmp_path, MINIMAL))
    summary = cfg.summary()
    assert summary["placement"]["flap_guard_failures"] == 3
    assert summary["placement"]["loading"]["max_seconds"] == pytest.approx(900.0)


# -- oMLX loading signals ---------------------------------------------------
async def test_backend_parses_the_loading_signals(stub_factory):
    stub = stub_factory(models=[StubModel(id="big", estimated_size=10**11)])
    state = stub.state
    state.start_load("big", started_at=1_700_000_000.0)

    async with httpx.AsyncClient() as client:
        backend = OmlxBackend(stub.url, client=client)
        residency = await backend.residency()

    assert residency is not None
    info = residency["big"]
    assert info.is_loading is True
    assert info.loading_started_at == pytest.approx(1_700_000_000.0)

    state.finish_load("big")
    async with httpx.AsyncClient() as client:
        residency = await (OmlxBackend(stub.url, client=client)).residency()
    assert residency is not None
    assert residency["big"].is_loading is False
    assert residency["big"].loaded is True
    assert residency["big"].loading_started_at is None


async def test_absent_loading_signals_stay_unknown_not_false(stub_factory):
    # A backend that does not report the field must not be read as "not
    # loading": that would end a running loading state on the first poll.
    stub = stub_factory(models=[StubModel(id="big", loaded=True)])
    async with httpx.AsyncClient() as client:
        residency = await (OmlxBackend(stub.url, client=client)).residency()
    assert residency is not None
    assert residency["big"].is_loading is None
    assert residency["big"].loading_started_at is None


# -- admin surface ----------------------------------------------------------
async def test_calibration_endpoints_are_mounted_and_need_the_admin_key(
        ripple_factory, stub_factory, tmp_path):
    stub = stub_factory(models=[StubModel(id="m1", loaded=True, estimated_size=10**10)])
    cfg = keyed_config([host_config("alpha", stub.url, node_id="nid-1")],
                       str(tmp_path / "keys.yaml"))
    harness = await ripple_factory(cfg)

    for method, path in (("get", "/admin/nodes/alpha/calibration"),
                         ("post", "/admin/nodes/alpha/calibrate")):
        anonymous = await getattr(harness.client, method)(path)
        assert anonymous.status_code == 401, anonymous.text

        allowed = await getattr(harness.client, method)(
            path, headers=bearer("test-admin-key"))
        # The stub reports a different node_id than the config, so the host is
        # unresolvable: the GET still reports the curve in force (200), the POST
        # refuses (409) — never 404, the node exists in the config.
        expected = 200 if method == "get" else 409
        assert allowed.status_code == expected, allowed.text

    unknown = await harness.client.get("/admin/nodes/nope/calibration",
                                       headers=bearer("test-admin-key"))
    assert unknown.status_code == 404, unknown.text
