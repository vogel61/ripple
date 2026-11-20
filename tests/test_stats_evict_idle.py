"""`/stats` counts `evict_idle` as the subset of `cold_load` (issue #112).

The counter used to sit behind an `elif` that could never fire, because the
`evict_idle` tier is already a cold load — so it read 0 for the lifetime of the
process. What an operator sees on `/stats` is what these tests assert.
"""

from __future__ import annotations

from tests.support import (DEEPSEEK_SIZE, QWEN_SIZE, StubModel, chat_body,
                           host_config, make_config, prompt_for)

DEEPSEEK = "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"
QWEN = "Qwen3.8-Flash-Next-oQ4e-mtp"


async def test_a_load_without_an_eviction_moves_only_cold_load(
        stub_factory, ripple_factory):
    """Nothing resident, so nothing to evict: one cold load, no eviction."""
    a = stub_factory(models=[StubModel(id="small", estimated_size=1_000_000_000)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    resp = await harness.ok(chat_body("small", prompt_for(0), turns=2))

    assert resp.headers["x-ripple-reason"] == "cold_load"
    stats = await harness.stats()
    assert stats["cold_load"] == 1
    assert stats["evict_idle"] == 0


async def test_a_load_that_evicts_an_idle_model_moves_both_counters(
        stub_factory, ripple_factory):
    """DeepSeek (422.5 GB) is resident, ceiling 487.7 GB, target 463.3 GB; Qwen
    (103.9 GB) pushes the forecast to 526.4 GB, so oMLX would evict the idle
    DeepSeek first — tier `evict_idle`.

    It is still a load RIPPLE supervised, so `cold_load` counts it too; that is
    what makes `evict_idle` a subset rather than a separate class.
    """
    a = stub_factory(models=[
        StubModel(id=DEEPSEEK, loaded=True, estimated_size=DEEPSEEK_SIZE,
                  last_access=1.0),
        StubModel(id=QWEN, loaded=False, estimated_size=QWEN_SIZE),
    ])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    resp = await harness.ok(chat_body(QWEN, prompt_for(0), turns=2))

    assert resp.headers["x-ripple-reason"] == "evict_idle"
    stats = await harness.stats()
    assert stats["cold_load"] == 1        # the load itself
    assert stats["evict_idle"] == 1       # ... and the eviction it needed
