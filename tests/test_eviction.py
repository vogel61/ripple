"""Eviction guard and capabilities: FA-18 (incl. the 503 conflict), FA-23."""

from __future__ import annotations

import asyncio
import time

from tests.support import (CEILING, DEEPSEEK_SIZE, QWEN_SIZE, StubModel, bearer,
                           chat_body, host_config, host_row, keyed_config,
                           make_config, prompt_for, stub_model_loaded, wait_until)

DEEPSEEK = "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"
QWEN = "Qwen3.8-Flash-Next-oQ4e-mtp"


def _deepseek(*, loaded: bool, pinned: bool = False,
              last_access: float = 1_000.0) -> StubModel:
    return StubModel(id=DEEPSEEK, loaded=loaded, pinned=pinned,
                     estimated_size=DEEPSEEK_SIZE, last_access=last_access)


def _qwen() -> StubModel:
    return StubModel(id=QWEN, loaded=False, estimated_size=QWEN_SIZE)


async def test_fa18_cold_load_goes_to_the_host_that_does_not_evict(
        stub_factory, ripple_factory):
    """DeepSeek (422.5 GB) is resident on A, ceiling 487.7 GB, target 463.3 GB.
    Qwen (103.9 GB) would push A to 526.4 GB, so oMLX would evict DeepSeek —
    the thread must go to B instead, with the reason `cold_load`."""
    a = stub_factory(models=[_deepseek(loaded=True), _qwen()])
    b = stub_factory(models=[_deepseek(loaded=False), _qwen()])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    resp = await harness.ok(chat_body(QWEN, prompt_for(0), turns=2))

    assert resp.headers["x-ripple-host"] == "B"
    assert resp.headers["x-ripple-reason"] == "cold_load"
    # nothing was ever sent to A, and the balancer never loads or unloads
    assert a.state.chat_calls == 0
    assert a.state.load_calls == [] and a.state.unload_calls == []

    health = await harness.healthz()
    row_a = host_row(health, "A")
    assert row_a["guard_enabled"] is True
    assert row_a["ceiling_bytes"] == CEILING
    assert health["capabilities"]["eviction_guard"] is True
    assert (await harness.stats())["cold_load"] == 1


async def test_fa18_pinned_model_blocks_the_host_with_503(stub_factory,
                                                          ripple_factory):
    """If the model that would be evicted is pinned and no other host offers the
    requested model, the answer is 503 naming host and blocking model."""
    a = stub_factory(models=[_deepseek(loaded=True, pinned=True), _qwen()])
    b = stub_factory(models=[_deepseek(loaded=True)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    resp = await harness.chat(chat_body(QWEN, prompt_for(0), turns=2))

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["reason"] == "conflict"
    message = error["message"]
    assert "deepseek" in message.lower()
    assert "pinned" in message.lower()
    assert "A" in message
    assert a.state.chat_calls == 0 and b.state.chat_calls == 0


async def test_fa18_active_thread_blocks_the_host_with_503(stub_factory,
                                                           ripple_factory):
    """A model held by an active thread counts as in use, not as idle."""
    a = stub_factory(models=[_deepseek(loaded=True), _qwen()])
    b = stub_factory(models=[_deepseek(loaded=True)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    # a running conversation on DeepSeek pins it to A (B does not offer it)
    first = await harness.ok(chat_body(DEEPSEEK, prompt_for(0), turns=2))
    assert first.headers["x-ripple-host"] == "A"

    resp = await harness.chat(chat_body(QWEN, prompt_for(1), turns=2))

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["reason"] == "conflict"
    assert "active deepseek" in error["message"].lower()
    assert a.state.chat_calls == 1  # only the DeepSeek request was forwarded


async def test_fa18_without_a_ceiling_there_is_no_forecast(stub_factory,
                                                           ripple_factory):
    """`final_ceiling: 0` switches the guard off visibly (FA-18 + FA-23): no
    forecast, `eviction_guard: false`, and the request is still served."""
    a = stub_factory(models=[_deepseek(loaded=True), _qwen()], final_ceiling=0)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
    ]))

    health = await harness.healthz()
    assert health["capabilities"]["eviction_guard"] is False
    assert health["capabilities"]["residency"] is True
    assert health["capabilities"]["capacity"] is False
    assert health["warnings"]
    assert any("capacity missing" in w for w in health["warnings"])
    row = host_row(health, "A")
    assert row["guard_enabled"] is False
    assert row["used_ratio"] is None

    resp = await harness.ok(chat_body(QWEN, prompt_for(0), turns=2))

    assert resp.headers["x-ripple-host"] == "A"
    assert resp.headers["x-ripple-reason"] == "cold_load"


async def test_fa23_openai_compatible_backend_switches_the_guard_off(
        stub_factory, ripple_factory):
    """A backend without residency signals must say so — and still serve."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, backend="openai_compatible"),
    ]))

    health = await harness.healthz()
    capabilities = health["capabilities"]
    assert capabilities["availability"] is True
    assert capabilities["residency"] is False
    assert capabilities["capacity"] is False
    assert capabilities["identity"] is False
    assert capabilities["eviction_guard"] is False
    assert health["warnings"]
    assert any("residency missing" in w for w in health["warnings"])

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert resp.headers["x-ripple-host"] == "A"
    assert a.state.chat_calls == 1


async def test_fa23_mixed_fleet_reports_the_intersection_of_capabilities(
        stub_factory, ripple_factory):
    """One host without residency switches FA-18 off for the whole fleet: a
    protection that covers only half the hosts is not a protection."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2,
                    backend="openai_compatible"),
    ]))

    health = await harness.healthz()
    assert health["capabilities"]["availability"] is True
    assert health["capabilities"]["eviction_guard"] is False
    assert host_row(health, "A")["guard_enabled"] is True
    assert host_row(health, "B")["guard_enabled"] is False

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    assert resp.status_code == 200


async def test_fa12_a_warm_node_beats_a_heavier_cold_one(stub_factory, ripple_factory):
    """Residency preference (FA-12): a loaded model wins over a heavier weight.

    The cold node carries weight 3 against 1 and still must not see a request
    while the warm node has a free slot — otherwise every new thread pays 70 s of
    load time for nothing. Only when the warm node is full does the cold one take
    over, and then it says so (`cold_load`).
    """
    warm = stub_factory(models=[stub_model_loaded("big")])
    cold = stub_factory(models=[StubModel(id="big", loaded=False,
                                          estimated_size=DEEPSEEK_SIZE)])
    harness = await ripple_factory(make_config([
        host_config("warm", warm.url, node_id=warm.state.node_id, weight=1,
                    max_threads=2),
        host_config("cold", cold.url, node_id=cold.state.node_id, weight=3,
                    max_threads=20),
    ]))

    for i in range(2):
        resp = await harness.ok(chat_body("big", prompt_for(i), turns=2))
        assert resp.headers["x-ripple-host"] == "warm"

    assert cold.state.chat_calls == 0

    # the warm node has no slot left, so the cold one has to load
    resp = await harness.ok(chat_body("big", prompt_for(9), turns=2))
    assert resp.headers["x-ripple-host"] == "cold"
    assert resp.headers["x-ripple-reason"] == "cold_load"
    assert warm.state.chat_calls == 2 and cold.state.chat_calls == 1


async def test_fa19_when_every_slot_is_taken_an_idle_thread_gives_way(
        stub_factory, ripple_factory):
    """Slots are a reservation for a warm prefix cache (FA-19) — and a finished
    conversation holds that reservation for nothing. Measured live: 24 finished
    threads, `inflight 0`, both hosts at 12/12, every new conversation answered
    503 `no_slot`. When no host has a free slot, the oldest idle thread gives way
    instead; a thread with a request in flight never does.
    """
    from ripple.config import AffinityConfig  # noqa: PLC0415

    a = stub_factory(models=[stub_model_loaded("big")])
    b = stub_factory(models=[stub_model_loaded("big")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1, max_threads=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1, max_threads=1),
    ], affinity=AffinityConfig(min_prompt_tokens=1)))

    first = await harness.ok(chat_body("big", prompt_for(0), turns=2))
    second = await harness.ok(chat_body("big", prompt_for(1), turns=2))
    assert {first.headers["x-ripple-host"],
            second.headers["x-ripple-host"]} == {"A", "B"}

    # every slot is taken by an idle thread — drop the oldest instead of 503
    third = await harness.ok(chat_body("big", prompt_for(2), turns=2))
    assert third.headers["x-ripple-reason"] == "slot_evict"

    stats = (await harness.client.get("/stats")).json()
    assert stats["threads_evicted"] == 1
    assert stats["http_503_total"] == 0
    # two slots, not three: a conversation gave way, the request did not fail
    health = await harness.healthz()
    assert host_row(health, "A")["thread_slots"] + host_row(health, "B")["thread_slots"] == 2


def test_fa19_a_thread_with_a_request_in_flight_never_gives_up_its_slot():
    """The one thing eviction must never do: pull the slot from under a running
    request, which would cost that request its prefix cache mid-flight."""
    from ripple.config import AffinityConfig  # noqa: PLC0415
    from ripple.registry import ThreadRegistry  # noqa: PLC0415

    reg = ThreadRegistry(AffinityConfig(min_prompt_tokens=1))
    busy = reg.register(key="busy", model="m", host="A", text="alpha " * 40,
                        holds_slot=True, source="header")
    reg.enter(busy)
    # old, but inside the TTL — otherwise it would count as expired, not as idle
    busy.last_seen = time.time() - 300
    idle = reg.register(key="idle", model="m", host="A", text="zulu " * 40,
                        holds_slot=True, source="header")
    assert idle.key == "idle"

    assert reg.evictable("A").key == "idle"

    reg.leave(busy)
    busy.last_seen = time.time() - 300    # now merely old
    assert reg.evictable("A").key == "busy"


# ---------------------------------------------------------------------------
# Traffic classes: slot priority and the one-shot (kind, FA-18/FA-19)
# ---------------------------------------------------------------------------
async def test_interactive_request_evicts_a_finished_batch_thread_first(
        stub_factory, ripple_factory, tmp_path):
    """A person waiting (interactive key) takes a finished batch thread's slot
    before it takes another conversation's — even when the batch thread is the
    younger of the two.

    Age alone would drop the older conversation here. A batch thread has nobody
    waiting for its prefix cache and will not come back to it; the conversation
    may. Only when no batch thread is idle does the order fall back to pure age,
    which is why the control at the end must take the interactive one.
    """
    a = stub_factory(models=[stub_model_loaded("big")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", a.url, node_id=a.state.node_id, max_threads=2)],
        str(tmp_path / "keys.yaml")))
    human = harness.ripple.keys.create("hermes", "interactive")
    job = harness.ripple.keys.create("nightly", "batch")

    # the conversation is the *older* thread of the two
    first = await harness.ok(chat_body("big", prompt_for(0), turns=2),
                             headers={**bearer(human.key), "X-Session-Id": "human"})
    assert first.headers["x-ripple-thread"] == "sid:human"
    await asyncio.sleep(0.02)
    second = await harness.ok(chat_body("big", prompt_for(1), turns=2),
                              headers={**bearer(job.key), "X-Session-Id": "job"})
    assert second.headers["x-ripple-thread"] == "sid:job"
    assert await harness.slots() == {"A": 2}

    third = await harness.ok(chat_body("big", prompt_for(2), turns=2),
                             headers={**bearer(human.key), "X-Session-Id": "human2"})
    assert third.headers["x-ripple-reason"] == "slot_evict"
    stats = await harness.stats()
    assert stats["threads_evicted"] == 1
    assert stats["threads_evicted_batch"] == 1
    keys = [row["key"] for row in stats["threads"]]
    assert "sid:job" not in keys          # the batch thread gave way
    assert "sid:human" in keys            # the older conversation did not

    # control: with no batch thread left, the oldest conversation goes
    await asyncio.sleep(0.02)
    fourth = await harness.ok(chat_body("big", prompt_for(3), turns=2),
                              headers={**bearer(human.key), "X-Session-Id": "human3"})
    assert fourth.headers["x-ripple-reason"] == "slot_evict"
    stats = await harness.stats()
    assert stats["threads_evicted"] == 2
    assert stats["threads_evicted_batch"] == 1     # unchanged: nothing batch left
    assert [row["key"] for row in stats["threads"]].count("sid:human") == 0


async def test_one_shot_never_holds_a_slot_and_never_adopts_a_thread(
        stub_factory, ripple_factory, tmp_path):
    """A `one-shot` key is ephemeral by contract: no thread, no slot, no
    eviction — whatever the prompt size, and even with a session header that
    belongs to a live conversation.

    The slot of the running conversation is not dropped for it (nothing to gain:
    a one-shot will not come back) and its binding is not taken over (a thread
    kept warm for traffic that never returns is exactly what the slot rule
    prevents).
    """
    a = stub_factory(models=[stub_model_loaded("big")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", a.url, node_id=a.state.node_id, max_threads=1)],
        str(tmp_path / "keys.yaml")))
    human = harness.ripple.keys.create("hermes", "interactive")
    probe = harness.ripple.keys.create("probe", "one-shot")

    first = await harness.ok(chat_body("big", prompt_for(0), turns=2),
                             headers={**bearer(human.key), "X-Session-Id": "human"})
    assert await harness.slots() == {"A": 1}

    # far above min_prompt_tokens and with two user messages: any other kind
    # would open (or adopt) a thread for this
    long_prompt = "context " * 4200
    one = await harness.ok(chat_body("big", long_prompt, turns=2),
                           headers={**bearer(probe.key), "X-Session-Id": "human"})
    assert one.headers["x-ripple-reason"] == "one_shot"
    assert "x-ripple-thread" not in one.headers

    stats = await harness.stats()
    assert stats["requests_one_shot"] == 1
    assert stats["threads_new"] == 1          # only the conversation's own thread
    assert stats["thread_hits"] == 0          # the one-shot adopted nothing
    assert stats["threads_evicted"] == 0      # and nothing was dropped for it
    assert await harness.slots() == {"A": 1}
    assert [row["key"] for row in stats["threads"]] == ["sid:human"]

    # the conversation is untouched and still bound
    again = await harness.ok(chat_body("big", prompt_for(0), turns=3),
                             headers={**bearer(human.key), "X-Session-Id": "human"})
    assert again.headers["x-ripple-reason"] == "thread"
    assert again.headers["x-ripple-thread"] == first.headers["x-ripple-thread"]


async def test_an_evict_idle_load_is_supervised_like_a_cold_one(stub_factory,
                                                               ripple_factory):
    """A node that holds a big resident model never takes the `cold_load` tier.

    It evicts an idle model first and then loads the one it was asked for — tier
    `evict_idle`. That is still a load of 100+ GB, and its probes have to be
    suspended *before* the request goes out. Reading only `cold_load` left one
    probe interval unprotected: the first production cold load after deployment
    (15.11.2026, Qwen3.8 on M3-beta, 28,2 s) was an `evict_idle` and went
    unsupervised for ~14 s.
    """
    a = stub_factory(models=[_deepseek(loaded=True, last_access=1.0), _qwen()])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")
    a.state.delay = 0.3                 # hold the request open long enough to look

    request = asyncio.create_task(harness.chat(chat_body(QWEN, prompt_for(0), turns=2)))
    await wait_until(lambda: host.loading is not None, timeout=5.0)

    assert host.loading["model"] == QWEN
    assert host.loading["source"] == "dispatch"
    assert host.status == "loading"

    resp = await request
    assert resp.headers["x-ripple-reason"] == "evict_idle"
    stats = await harness.stats()
    assert stats["loads_supervised"] == 1
    assert host.loading is None         # the request returning ended it
