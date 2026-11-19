"""Operating behaviour: AS-4b (thread TTL), AS-7 (long streams), AS-13 (reinit).

These three scenarios are about time and process lifetime rather than about
placement, which is why they live in their own file.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

from tests.support import (AffinityConfig, chat_body, host_config, make_config,
                           prompt_for, stub_model_loaded)


async def test_as4b_expired_threads_release_their_slots(stub_factory, ripple_factory):
    """After the TTL the distribution starts from zero again (AS-4b).

    Five threads take 3 : 2. Once the TTL has passed, no thread may hold a slot
    any more — otherwise the deficit rule would keep an idle conversation's
    capacity occupied for ever.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config(
        [host_config("A", a.url, node_id=a.state.node_id, weight=3),
         host_config("B", b.url, node_id=b.state.node_id, weight=2)],
        affinity=AffinityConfig(ttl_seconds=1),
    ))

    for i in range(5):
        await harness.ok(chat_body("m1", prompt_for(i), turns=2))
    assert sum((await harness.slots()).values()) == 5

    await asyncio.sleep(1.3)
    health = await harness.healthz()
    assert sum((await harness.slots()).values()) == 0
    assert health["threads"]["active"] == 0

    # a new set of threads starts the deficit rule from the beginning
    for i in range(5):
        await harness.ok(chat_body("m1", prompt_for(100 + i), turns=2))
    assert sum((await harness.slots()).values()) == 5


async def test_as7_a_slow_stream_is_not_cut_off(stub_factory, ripple_factory):
    """RIPPLE sets no read timeout of its own (FA-9/AS-7).

    The upstream answers after 1.5 s and then streams with 0.2 s between chunks.
    A proxy with its own short timeout would abort here; RIPPLE must pass the
    whole stream through.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    a.state.delay = 1.5
    a.state.stream_chunk_delay = 0.2
    a.state.stream_chunks = 5
    harness = await ripple_factory(make_config(
        [host_config("A", a.url, node_id=a.state.node_id, weight=1)]))

    started = time.monotonic()
    chunks = 0
    async with harness.client.stream("POST", "/v1/chat/completions",
                                     json=chat_body("m1", prompt_for(0), stream=True)) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:") and "[DONE]" not in line:
                chunks += 1
    elapsed = time.monotonic() - started

    assert chunks >= 5
    assert elapsed >= 1.5 + 4 * 0.2


async def test_as13_reinit_keeps_thread_bindings(stub_factory, ripple_factory):
    """`POST /reinit` re-resolves the addresses and keeps the bindings (AS-13).

    A site change re-resolves addresses; a thread that is already bound must not
    be moved by it, because its prefix cache lives on that host.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1),
    ]))

    first = await harness.ok(chat_body("m1", prompt_for(0), turns=2))
    bound = first.headers["x-ripple-host"]

    resp = await harness.client.post("/reinit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] == {}
    assert set(body["hosts"]) == {"A", "B"}

    hosts = []
    for turn in range(4):
        r = await harness.ok(chat_body("m1", prompt_for(0), turns=2 + turn))
        hosts.append(r.headers["x-ripple-host"])

    assert Counter(hosts) == {bound: 4}
