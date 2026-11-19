"""The flap guard: one missed probe is not a dead node (contract §3.4, spec §4).

A host becomes `down` only after `placement.flap_guard_failures` consecutive
*failed* probes; one or two leave it `degraded` and usable, and the decision that
uses it says `degraded_host` so the fallback is never silent. A *hard* failure —
a refused TCP connect, the node really gone — still drops the host at once, which
is what AS-5 needs.

The distinction is not cosmetic. The incident of 14.11.2026 was a read timeout
that landed inside a 111 GB cold load; treating that one probe as fatal made the
next request fail with 503 although the host was fully back.
"""

from __future__ import annotations

from ripple.config import Config, PlacementConfig

from tests.support import (chat_body, host_config, make_config, prompt_for,
                           stub_model_loaded)


def _fleet(a, b, **placement) -> Config:
    """Two identical hosts; `least_threads` makes the first request land on A.

    Without a deterministic choice the assertions below would depend on the
    deficit arithmetic, which is not what this file is about.
    """
    return make_config(
        [host_config("A", a.url, node_id=a.state.node_id),
         host_config("B", b.url, node_id=b.state.node_id)],
        placement=PlacementConfig(binding_strategy="least_threads", **placement))


# ---------------------------------------------------------------------------
# 3. One soft failure: degraded, usable, and visible as the reason
# ---------------------------------------------------------------------------
async def test_one_soft_failure_degrades_the_host_but_keeps_it_usable(
        stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_fleet(a, b))
    host = harness.host("A")

    a.state.health_status = "error"     # the node answers, but not with "healthy"
    await host.refresh()

    assert host.consecutive_failures == 1
    assert host.status == "degraded"
    assert host.healthy is False        # the verdict of the last probe
    assert host.in_pool is True         # ... which is not yet a pool decision

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert resp.headers["x-ripple-host"] == "A"
    assert resp.headers["x-ripple-reason"] == "degraded_host"

    stats = await harness.stats()
    assert stats["degraded_pool_decisions"] == 1


# ---------------------------------------------------------------------------
# 4. Two soft failures: still degraded
# ---------------------------------------------------------------------------
async def test_two_soft_failures_stay_degraded(stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_fleet(a, b))
    host = harness.host("A")

    a.state.health_status = "error"
    await host.refresh()
    await host.refresh()

    assert host.consecutive_failures == 2
    assert host.status == "degraded"
    assert host.in_pool is True

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert resp.headers["x-ripple-host"] == "A"
    assert resp.headers["x-ripple-reason"] == "degraded_host"


# ---------------------------------------------------------------------------
# 5. Three soft failures: down, out of the pool
# ---------------------------------------------------------------------------
async def test_three_soft_failures_take_the_host_out_of_the_pool(
        stub_factory, ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_fleet(a, b))
    host = harness.host("A")

    a.state.health_status = "error"
    for _ in range(3):
        await host.refresh()

    assert host.consecutive_failures >= 3
    assert host.status == "down"
    assert host.in_pool is False

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert resp.headers["x-ripple-host"] == "B"
    assert b.state.chat_calls == 1


# ---------------------------------------------------------------------------
# 6. flap_guard_failures: 1 — the behaviour before this wave
# ---------------------------------------------------------------------------
async def test_flap_guard_of_one_restores_the_old_behaviour(stub_factory,
                                                            ripple_factory):
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_fleet(a, b, flap_guard_failures=1))
    host = harness.host("A")

    a.state.health_status = "error"
    await host.refresh()                # one failure is enough again

    assert host.status == "down"
    assert host.in_pool is False

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert resp.headers["x-ripple-host"] == "B"


# ---------------------------------------------------------------------------
# A. A hard failure drops the host at once (AS-5)
# ---------------------------------------------------------------------------
async def test_a_refused_connection_drops_the_host_at_once(stub_factory,
                                                           ripple_factory):
    """A stopped node must lose its traffic on the first probe.

    A refused TCP connect is not a load artefact, so the flap guard does not
    soften it — AS-5 (one node stopped, 10/10 on the other) depends on that.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_fleet(a, b))
    a.stop()
    host = harness.host("A")

    await host.refresh()

    assert host.consecutive_failures == 1
    assert host.status == "down"
    assert host.in_pool is False
    assert host.last_error

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert resp.headers["x-ripple-host"] == "B"
    assert a.state.chat_calls == 0


# ---------------------------------------------------------------------------
# B. A degraded host still serves — including a thread that is bound to it
# ---------------------------------------------------------------------------
async def test_a_degraded_bound_host_serves_its_thread_instead_of_a_503(
        stub_factory, ripple_factory):
    """`admit()` and the bound-thread branch both use `in_pool`.

    One missed probe must neither answer 503 nor push a running conversation to
    the other node — moving it would cost the prefix cache for a probe artefact.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_fleet(a, b))

    first = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert first.headers["x-ripple-host"] == "A"

    a.state.health_status = "error"
    await harness.host("A").refresh()
    assert harness.host("A").status == "degraded"

    second = await harness.ok(chat_body("m1", prompt_for(0), turns=2))

    assert second.status_code == 200
    assert second.headers["x-ripple-host"] == "A"
    assert second.headers["x-ripple-reason"] == "thread"
    assert b.state.chat_calls == 0
