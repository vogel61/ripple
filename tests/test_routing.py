"""Routing: FA-4/AS-8 (model availability), FA-10 (retry), FA-11/AS-5 (fallback)."""

from __future__ import annotations

from collections import Counter

import pytest

from ripple.placement import choose

from tests.support import (chat_body, host_config, host_row, make_config,
                           prompt_for, stub_model_loaded)


async def test_as8_fa4_model_offered_by_one_host_is_pinned_there(
        stub_factory, ripple_factory):
    """A model only host A offers goes to A 10/10 (AS-8, FA-4)."""
    a = stub_factory(models=[stub_model_loaded("m1"), stub_model_loaded("exclusive")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1),
    ]))

    responses = [await harness.ok(chat_body("exclusive", prompt_for(i), turns=2))
                 for i in range(10)]
    hosts = [r.headers["x-ripple-host"] for r in responses]

    assert Counter(hosts) == {"A": 10}
    assert b.state.chat_calls == 0
    assert a.state.chat_calls == 10

    decisions = (await harness.stats())["decisions"]
    assert len(decisions) == 10
    assert {d["host"] for d in decisions} == {"A"}


async def test_as8_fa4_placement_decides_model_pinned(stub_factory, ripple_factory):
    """The placement layer does produce the reason AS-8 asks for."""
    a = stub_factory(models=[stub_model_loaded("m1"), stub_model_loaded("exclusive")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1),
    ]))

    decision = choose(harness.ripple.fleet, harness.ripple.cfg,
                      harness.ripple.registry, "exclusive")

    assert decision.host is not None
    assert decision.host.cfg.name == "A"
    assert decision.reason == "model_pinned"


async def test_as8_fa4_reports_model_pinned_as_the_request_reason(
        stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1"), stub_model_loaded("exclusive")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1),
    ]))

    resp = await harness.ok(chat_body("exclusive", prompt_for(0), turns=2))

    assert resp.headers["x-ripple-reason"] == "model_pinned"


async def test_fa4_unknown_model_is_rejected_with_404(stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    resp = await harness.chat(chat_body("no-such-model", prompt_for(0), turns=2))

    assert resp.status_code == 404
    error = resp.json()["error"]
    assert "no-such-model" in error["message"]
    assert error["reason"] == "unknown_model"
    assert a.state.chat_calls == 0 and b.state.chat_calls == 0


async def test_fa10_retry_on_5xx_before_the_first_byte(stub_factory, ripple_factory):
    """A 5xx before any byte is retried on the other host — exactly once."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    a.state.chat_status = 500
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))

    assert resp.headers["x-ripple-host"] == "B"
    assert resp.headers["x-ripple-reason"] == "fallback"
    assert a.state.chat_calls == 1          # tried once, never again
    assert b.state.chat_calls == 1          # exactly one successful attempt
    stats = await harness.stats()
    assert stats["errors_total"] == 1
    assert stats["fallbacks"] == 1


async def test_fa10_no_retry_when_both_hosts_fail(stub_factory, ripple_factory):
    """At most one attempt per host: two failures mean 502, not a retry loop."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    a.state.chat_status = 500
    b.state.chat_status = 500
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    resp = await harness.chat(chat_body("m1", prompt_for(0), turns=2))

    assert resp.status_code == 502
    assert a.state.chat_calls == 1 and b.state.chat_calls == 1


async def test_fa10_no_retry_after_the_response_started(stub_factory,
                                                       ripple_factory):
    """Approximation of "no retry after the first byte": a response that has
    already started is never sent a second time, so the upstream is contacted
    exactly once and the other host never sees the request."""
    a = stub_factory(models=[stub_model_loaded("m1")], stream_chunks=2)
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2, stream=True))

    assert resp.headers["x-ripple-host"] == "A"
    assert b"data: " in resp.content
    assert a.state.chat_calls == 1
    assert b.state.chat_calls == 0


async def test_fa11_as5_fallback_when_one_host_is_down(stub_factory, ripple_factory):
    """Host B stopped: its bound thread falls back, everything else goes to A,
    and /healthz reports B as down."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    on_a = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    on_b = await harness.ok(chat_body("m1", prompt_for(1), turns=2))
    assert on_a.headers["x-ripple-host"] == "A"
    assert on_b.headers["x-ripple-host"] == "B"

    b.stop()
    # instead of waiting for the discovery loop, refresh three times so the
    # `unhealthy_after` threshold is crossed
    host_b = harness.host("B")
    for _ in range(3):
        await host_b.refresh()

    health = await harness.healthz()
    row = host_row(health, "B")
    assert row["status"] == "down"
    assert row["healthy"] is False
    assert row["last_error"]

    # the thread that was bound to B is moved, with the reason `fallback`
    fallen_back = await harness.ok(chat_body("m1", prompt_for(1), turns=3))
    assert fallen_back.headers["x-ripple-host"] == "A"
    assert fallen_back.headers["x-ripple-reason"] == "fallback"

    # ten fresh requests all land on A (AS-5: 10/10)
    hosts = []
    for i in range(2, 12):
        resp = await harness.ok(chat_body("m1", prompt_for(i), turns=2))
        hosts.append(resp.headers["x-ripple-host"])
    assert Counter(hosts) == {"A": 10}

    assert host_row(await harness.healthz(), "A")["status"] == "healthy"


async def test_allow_list_keeps_a_model_off_a_host_that_offers_it(
        stub_factory, ripple_factory):
    """§5.1: the allow-list is the most important field of layer A. A host must
    not serve a model it is not configured for, even when oMLX offers it — the
    eviction forecast of FA-18 would otherwise be built on the wrong host."""
    a = stub_factory(models=[stub_model_loaded("m1"), stub_model_loaded("big")])
    b = stub_factory(models=[stub_model_loaded("m1"), stub_model_loaded("big")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3, models=["m1"]),
        host_config("B", b.url, node_id=b.state.node_id, weight=2,
                    models=["m1", "big"]),
    ]))

    hosts = []
    for i in range(5):
        resp = await harness.ok(chat_body("big", prompt_for(i), turns=2))
        hosts.append(resp.headers["x-ripple-host"])

    assert Counter(hosts) == {"B": 5}
    assert a.state.chat_calls == 0

    # /v1/models does not advertise a model the host may not serve either
    listed = {m["id"] for m in (await harness.client.get("/v1/models")).json()["data"]}
    assert "big" in listed and "m1" in listed


async def test_model_outside_every_allow_list_is_rejected(stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1"), stub_model_loaded("blocked")])
    b = stub_factory(models=[stub_model_loaded("m1"), stub_model_loaded("blocked")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1, models=["m1"]),
        host_config("B", b.url, node_id=b.state.node_id, weight=1, models=["m1"]),
    ]))

    resp = await harness.chat(chat_body("blocked", prompt_for(0), turns=2))

    assert resp.status_code == 404
    assert a.state.chat_calls == 0 and b.state.chat_calls == 0
