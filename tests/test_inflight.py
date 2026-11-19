"""`inflight` and `thread.active` while a stream is running.

A streaming request keeps the upstream connection open for as long as the model
generates. The slot it took (`HostRuntime.acquire`) has to live exactly that
long: `_forward` returns when the *headers* arrive — that is the first token, not
the end. Releasing there made three things blind (`max_inflight`, the spill
threshold, the `(inflight+1)/weight` tie-break) and turned a running thread into
an eviction candidate, because `evictable()` looks for `active <= 0`.

These tests need a real socket: `httpx.ASGITransport` buffers the whole body, so
the mid-flight state is invisible through it — which is exactly why the suite
never noticed.
"""

from __future__ import annotations

import asyncio

import httpx

from ripple.config import PlacementConfig

from tests.support import StubModel, chat_body, host_config, make_config

MODEL = "m1"


def _cfg(tmp_path, hosts):
    cfg = make_config(hosts)
    cfg.placement = PlacementConfig(maintenance_file=str(tmp_path / "maintenance.yaml"),
                                    model_policy_file=str(tmp_path / "models.yaml"))
    return cfg


def _models() -> list[StubModel]:
    return [StubModel(id=MODEL, estimated_size=100_000_000_000)]


async def _inflight(url: str, client: httpx.AsyncClient) -> int:
    resp = await client.get(url + "/healthz")
    assert resp.status_code == 200, resp.text
    return next(h for h in resp.json()["hosts"] if h["name"] == "A")["inflight"]


async def test_a_stream_holds_its_slot_until_the_last_chunk(stub_factory,
                                                            live_proxy_factory, tmp_path):
    a = stub_factory(models=_models())
    a.state.stream_chunks = 4
    a.state.stream_chunk_delay = 0.12          # ~0,5 s of generation
    url = live_proxy_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    async with httpx.AsyncClient(timeout=15) as client:
        seen: list[int] = []
        async with client.stream("POST", url + "/v1/chat/completions",
                                 json=chat_body(MODEL, "hi", turns=2, stream=True)) as resp:
            assert resp.status_code == 200
            async for _chunk in resp.aiter_raw():
                seen.append(await _inflight(url, client))
        # The slot is taken as soon as the headers are there — before the fix it
        # was already free at this point — and it stays taken while the model
        # generates. It is released when the upstream is done, which can be a
        # few reads before the client has drained its socket buffer.
        assert seen[0] == 1, seen
        assert seen.count(1) >= 2, seen
        assert seen[-1] == 0, seen
        await asyncio.sleep(0.05)
        assert await _inflight(url, client) == 0


async def test_a_client_that_hangs_up_frees_the_slot(stub_factory, live_proxy_factory,
                                                     tmp_path):
    """An aborted stream must not leak the slot — otherwise the node stays "full"."""
    a = stub_factory(models=_models())
    a.state.stream_chunks = 40
    a.state.stream_chunk_delay = 0.05
    url = live_proxy_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    async with httpx.AsyncClient(timeout=15) as client:
        async with client.stream("POST", url + "/v1/chat/completions",
                                 json=chat_body(MODEL, "hi", turns=2, stream=True)) as resp:
            assert resp.status_code == 200
            async for _chunk in resp.aiter_raw():
                break                              # the client walks away mid-stream
        # Leaving the block closes the connection; the generator's finally runs.
        for _ in range(40):
            if await _inflight(url, client) == 0:
                break
            await asyncio.sleep(0.05)
        assert await _inflight(url, client) == 0


async def test_max_inflight_really_limits_concurrent_streams(stub_factory,
                                                             live_proxy_factory, tmp_path):
    a = stub_factory(models=_models())
    a.state.stream_chunks = 4
    a.state.stream_chunk_delay = 0.12
    url = live_proxy_factory(_cfg(tmp_path, [
        host_config("A", a.url, max_inflight=1)]))

    async with httpx.AsyncClient(timeout=15) as client:
        async with client.stream("POST", url + "/v1/chat/completions",
                                 json=chat_body(MODEL, "hi", turns=2, stream=True)) as first:
            assert first.status_code == 200
            await asyncio.sleep(0.05)          # the first stream is running now
            second = await client.post(url + "/v1/chat/completions",
                                       json=chat_body(MODEL, "hi again", turns=2,
                                                      stream=True))
            # The one slot is held by a stream that is still generating.
            assert second.status_code == 503, second.text
            assert "max_inflight" in second.text
