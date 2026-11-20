"""FA-12: `hosts[].prefer_resident` is the switch behind the residency preference.

A resident model used to win unconditionally — `residency_tier()` returned
`TIER_RESIDENT` as soon as oMLX reported the model loaded, and no code read the
key at all. So `prefer_resident: false` parsed, validated, round-tripped through
the overlay and did nothing. These tests hold the key to its documented meaning:

* on (the default), a warm node beats a heavier cold one, because the request
  would otherwise pay the load time for nothing;
* off, the same fleet places by deficit/capacity — being warm is not a reason;
* off, the eviction guard is untouched: a host that would take memory away from
  a locked model is still a conflict, not a preference;
* the value set at the page survives the `runtime.yaml` round-trip and still
  changes where the request goes.
"""

from __future__ import annotations

from dataclasses import replace

from ripple.config import FleetOverlay, apply_overlay, load_overlay, save_overlay

from tests.support import (DEEPSEEK_SIZE, QWEN_SIZE, StubModel, chat_body,
                           host_config, make_config, prompt_for)

BIG = "big"
DEEPSEEK = "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"
QWEN = "Qwen3.8-Flash-Next-oQ4e-mtp"


def _warm(*, prefer_resident: bool = True, weight: float = 1.0, max_threads: int = 2):
    """The node that already holds `big`, with a fraction of the cold one's weight."""
    return {"prefer_resident": prefer_resident, "weight": weight,
            "max_threads": max_threads}


def _fleet(stub_factory, *, warm_prefer_resident: bool = True):
    """`warm` holds `big`; `cold` has weight 3 and 20 slots — the less loaded node.

    Both offer the same model. The cold node is *more* attractive on every other
    axis, so residency is the only thing that can put the request on `warm`.
    """
    warm = stub_factory(models=[StubModel(id=BIG, loaded=True,
                                          estimated_size=DEEPSEEK_SIZE)])
    cold = stub_factory(models=[StubModel(id=BIG, loaded=False,
                                          estimated_size=DEEPSEEK_SIZE)])
    hosts = [host_config("warm", warm.url, node_id=warm.state.node_id,
                         **_warm(prefer_resident=warm_prefer_resident)),
             host_config("cold", cold.url, node_id=cold.state.node_id,
                         **_warm(prefer_resident=True, weight=3.0, max_threads=20))]
    return warm, cold, hosts


async def test_fa12_the_default_prefers_the_warm_node(stub_factory, ripple_factory):
    """With the key at its default (`true`), residency still wins over weight."""
    warm, cold, hosts = _fleet(stub_factory, warm_prefer_resident=True)
    harness = await ripple_factory(make_config(hosts))

    resp = await harness.ok(chat_body(BIG, prompt_for(0), turns=2))

    assert resp.headers["x-ripple-host"] == "warm"
    # A first placement on a warm host is reported as `new_thread` on the wire
    # (proxy.py); what matters here is that nothing had to load.
    assert (await harness.stats())["cold_load"] == 0
    assert cold.state.chat_calls == 0
    assert cold.state.load_calls == []


async def test_fa12_switched_off_the_deficit_decides(stub_factory, ripple_factory):
    """`prefer_resident: false` on the warm node: the request goes to `cold`.

    Both hosts are cold-load candidates for `big`, so the tier no longer decides
    and the deficit does — the node with weight 3 and 20 free slots, not the warm
    one with weight 1. Nothing is loaded on `warm`, nothing is unloaded there.
    """
    warm, cold, hosts = _fleet(stub_factory, warm_prefer_resident=False)
    harness = await ripple_factory(make_config(hosts))

    resp = await harness.ok(chat_body(BIG, prompt_for(0), turns=2))

    assert resp.headers["x-ripple-host"] == "cold"
    assert resp.headers["x-ripple-reason"] == "cold_load"
    assert warm.state.chat_calls == 0
    assert warm.state.load_calls == [] and warm.state.unload_calls == []
    # the model is still resident on `warm` — not preferred, not touched
    assert warm.state.model(BIG).loaded is True


async def test_fa12_switched_off_still_refuses_to_evict_a_locked_model(
        stub_factory, ripple_factory, tmp_path):
    """The preference is a preference; the lock is a promise (issue #21).

    `A` holds the locked DeepSeek and would have to evict it for Qwen; `B` does
    not offer Qwen at all, so `A` is the only candidate. With the residency
    preference switched off the answer is still 503 `model_locked` — turning the
    preference off must not turn the guard off with it.
    """
    a = stub_factory(models=[StubModel(id=DEEPSEEK, loaded=True,
                                       estimated_size=DEEPSEEK_SIZE),
                             StubModel(id=QWEN, loaded=False,
                                       estimated_size=QWEN_SIZE)])
    b = stub_factory(models=[StubModel(id=DEEPSEEK, loaded=True,
                                       estimated_size=DEEPSEEK_SIZE)])
    cfg = make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3,
                    prefer_resident=False),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ])
    policy_file = tmp_path / "models.yaml"
    policy_file.write_text(f"locks:\n  {DEEPSEEK}: true\n", encoding="utf-8")
    cfg.placement = replace(cfg.placement, model_policy_file=str(policy_file))

    harness = await ripple_factory(cfg)
    resp = await harness.chat(chat_body(QWEN, prompt_for(0), turns=2))

    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["reason"] == "model_locked"
    assert DEEPSEEK in error["message"] and " on A" in error["message"]
    assert a.state.chat_calls == 0 and a.state.unload_calls == []
    assert a.state.model(DEEPSEEK).loaded is True


async def test_fa12_the_overlay_override_changes_where_the_request_goes(
        stub_factory, ripple_factory, tmp_path):
    """`hosts.warm.prefer_resident: false` set at the page survives the restart.

    The file still says `true`; the overlay says `false`. After the round-trip
    through `runtime.yaml` the fleet routes to `cold` — the key is policy, so it
    has to reach the router and not only the parser.
    """
    warm, cold, hosts = _fleet(stub_factory, warm_prefer_resident=True)
    overlay_path = tmp_path / "runtime.yaml"
    overlay = FleetOverlay(path=overlay_path)
    overlay.set_path("hosts.warm.prefer_resident", False)
    save_overlay(overlay)

    cfg = make_config(hosts)
    assert apply_overlay(cfg, load_overlay(overlay_path)) == []
    assert cfg.host("warm").prefer_resident is False

    harness = await ripple_factory(cfg)
    resp = await harness.ok(chat_body(BIG, prompt_for(0), turns=2))

    assert resp.headers["x-ripple-host"] == "cold"
    assert warm.state.chat_calls == 0
