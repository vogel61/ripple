"""Host resolution: FA-21 / AS-11 (address fallback), AS-12 (identity mismatch)."""

from __future__ import annotations

from collections import Counter

from tests.support import (chat_body, closed_port, host_config, host_row,
                           make_config, prompt_for, stub_model_loaded)


async def test_as11_fa21_unreachable_first_address_falls_back_to_the_second(
        stub_factory, ripple_factory):
    """The address list is probed in order; the first answering address wins."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    dead_port = closed_port()
    harness = await ripple_factory(make_config([
        host_config("A", [f"http://127.0.0.1:{dead_port}", a.url],
                    node_id=a.state.node_id, weight=1),
    ]))

    host = harness.host("A")
    assert host.resolved_address == a.url
    assert host.healthy is True
    assert host.status == "healthy"

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert resp.headers["x-ripple-host"] == "A"
    assert a.state.chat_calls == 1


async def test_as11_requests_keep_working_while_the_first_address_stays_dead(
        stub_factory, ripple_factory):
    """The address is chosen once for the lifetime of the process (FA-21)."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    dead_port = closed_port()
    harness = await ripple_factory(make_config([
        host_config("A", [f"http://127.0.0.1:{dead_port}", a.url],
                    node_id=a.state.node_id, weight=1),
    ]))

    hosts = []
    for i in range(5):
        resp = await harness.ok(chat_body("m1", prompt_for(i), turns=2))
        hosts.append(resp.headers["x-ripple-host"])

    assert Counter(hosts) == {"A": 5}
    assert a.state.chat_calls == 5
    assert harness.host("A").resolved_address == a.url


async def test_as12_fa21_foreign_node_id_is_rejected(stub_factory, ripple_factory):
    """A host answering with the wrong node_id is not used at all — reachability
    alone is not an identity (RB-9)."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    foreign = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", foreign.url, node_id="expected-node-id", weight=2),
    ]))

    host_b = harness.host("B")
    assert host_b.resolved_address is None
    assert host_b.healthy is False
    assert host_b.identity_ok is False

    health = await harness.healthz()
    row_b = host_row(health, "B")
    assert row_b["status"] == "down"
    assert row_b["identity_ok"] is False
    assert row_b["node_id"] is None
    assert "identity_mismatch" in (row_b["last_error"] or "")
    assert foreign.state.node_id in (row_b["last_error"] or "")

    # not a single request is routed to the foreign instance
    hosts = []
    for i in range(5):
        resp = await harness.ok(chat_body("m1", prompt_for(i), turns=2))
        hosts.append(resp.headers["x-ripple-host"])
    assert Counter(hosts) == {"A": 5}
    assert foreign.state.chat_calls == 0
    assert a.state.chat_calls == 5


async def test_fa21_no_usable_host_never_reaches_the_foreign_instance(
        stub_factory, ripple_factory):
    """Nothing usable left: a clear error instead of a request to a stranger."""
    foreign = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("B", foreign.url, node_id="expected-node-id", weight=1),
    ]))

    resp = await harness.chat(chat_body("m1", prompt_for(0), turns=2))

    # No host is usable. RIPPLE answers 404 (`unknown_model`) because the
    # rejected host never got its model list; 503 would describe it better, but
    # either way the foreign instance must not see a request.
    assert resp.status_code in (404, 503)
    error = resp.json()["error"]
    assert error["reason"] in ("unknown_model", "no_healthy_host")
    assert "m1" in error["message"]
    assert foreign.state.chat_calls == 0
