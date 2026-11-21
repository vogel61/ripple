"""Streaming: FA-8 — SSE is passed through chunk by chunk, never buffered.

httpx.ASGITransport buffers the whole response body, so these tests run RIPPLE
behind a real uvicorn server on an ephemeral port.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from tests.support import (chat_body, host_config, host_row, make_config, prompt_for,
                           stub_model_loaded)


@pytest.fixture
def proxy_url(stub_factory, live_proxy_factory):
    a = stub_factory(models=[stub_model_loaded("m1")],
                     stream_chunks=3, stream_chunk_delay=0.3)
    b = stub_factory(models=[stub_model_loaded("m1")])
    url = live_proxy_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]), wait_healthy=True)
    return url, a, b


async def test_fa8_streaming_arrives_chunk_by_chunk(proxy_url):
    url, a, _b = proxy_url
    body = chat_body("m1", prompt_for(0), turns=2, stream=True)

    chunks: list[bytes] = []
    arrivals: list[float] = []
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=url, timeout=30.0) as client:
        async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["x-ripple-host"] == "A"
            async for chunk in resp.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
                    arrivals.append(time.perf_counter() - started)

    payload = b"".join(chunks)
    assert len(chunks) > 1, "the response was buffered instead of forwarded"
    assert payload.count(b"data: ") >= 3
    assert b"[DONE]" in payload
    # the first chunk must reach the client long before the upstream is done:
    # the stub waits 0.3 s between its chunks
    assert arrivals[0] < arrivals[-1] - 0.2


async def test_fa8_usage_chunk_is_requested_and_stripped(stub_factory, ripple_factory):
    """oMLX reports its own measurements only when asked, and the client must not
    see the extra chunk. This is where the decode rate for /healthz comes from —
    counting deltas would be off by ~7.5x (measured 14.11.2026)."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    cfg = make_config([host_config("A", stub.url, node_id=stub.state.node_id)])
    harness = await ripple_factory(cfg)

    async with harness.client.stream("POST", "/v1/chat/completions",
                                     json=chat_body("m1", "hi", turns=2, stream=True)) as r:
        body = b"".join([chunk async for chunk in r.aiter_bytes()])

    assert b"[DONE]" in body
    assert b'"usage"' not in body                  # stripped before the client
    assert stub.state.usage_chunks_served == 1     # but it was asked for
    health = await harness.healthz()
    assert host_row(health, "A")["metrics"]["m1"]["tg_tps"] == 51.5


async def test_fa8_client_requested_usage_passes_through(stub_factory, ripple_factory):
    """A client that asks for usage itself gets it — RIPPLE only strips what it
    injected."""
    stub = stub_factory(models=[stub_model_loaded("m1")])
    cfg = make_config([host_config("A", stub.url, node_id=stub.state.node_id)])
    harness = await ripple_factory(cfg)

    body_in = chat_body("m1", "hi", turns=2, stream=True,
                        stream_options={"include_usage": True})
    async with harness.client.stream("POST", "/v1/chat/completions", json=body_in) as r:
        body = b"".join([chunk async for chunk in r.aiter_bytes()])

    assert b'"usage"' in body
    assert stub.state.usage_chunks_served == 1


async def test_fa8_streaming_request_is_a_single_upstream_call(proxy_url):
    url, a, b = proxy_url
    body = chat_body("m1", prompt_for(0), turns=2, stream=True)

    async with httpx.AsyncClient(base_url=url, timeout=30.0) as client:
        resp = await client.post("/v1/chat/completions", json=body)

    assert resp.status_code == 200
    assert b"[DONE]" in resp.content
    assert a.state.chat_calls == 1
    assert b.state.chat_calls == 0
    assert a.state.chat_bodies[0]["stream"] is True


# -- the first token, and why no test ever saw one (issue #24) --------------
def test_the_detector_does_not_depend_on_a_serialisers_spacing():
    """Both spellings of a non-empty delta count, the empty one does not.

    oMLX writes the compact form; `json.dumps` writes the spaced one. A client
    that only understood one of them was silently blind to the other — which is
    how the whole suite came to exercise the prefill path of every stream and
    never the first token.
    """
    from ripple.proxy import _CONTENT_RE

    assert _CONTENT_RE.findall(b'{"delta":{"content":"Hallo"}}')
    assert _CONTENT_RE.findall(b'{"delta": {"content": "Hallo"}}')
    assert _CONTENT_RE.findall(b'{"delta": {"reasoning_content": "denk"}}')
    # an empty delta is not a token
    assert not _CONTENT_RE.findall(b'{"delta": {"content": ""}}')
    assert not _CONTENT_RE.findall(b'{"delta": {}}')


async def test_the_shared_stub_reaches_the_first_token(stub_factory, ripple_factory):
    """The shared stub's stream must produce a first token, not just a prefill.

    `tests/support.py` serialises its SSE chunks with `json.dumps` defaults, so
    the delta arrives as `"content": "tok0 "` — with a space. While the detector
    required the compact form, this stub produced no first token at all: the phase
    stayed `pp` for the entire stream (issue #18 had to build its own stub to see
    the transition), `first_token_ms` was never set and the delta counter stayed
    at 0. The observable is `/healthz`, the endpoint the page reads.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")], stream_chunks=4,
                        stream_chunk_delay=0.3)
    harness = await ripple_factory(make_config([
        host_config("A", stub.url, node_id=stub.state.node_id)]))
    request = asyncio.create_task(harness.chat(chat_body("m1", "hi", turns=2,
                                                         stream=True)))
    try:
        deadline = time.monotonic() + 10.0
        seen: Any = "never read"
        while time.monotonic() < deadline:
            health = await harness.healthz()
            seen = next(x for m in health["models"] if m["id"] == "m1"
                        for x in m["hosts"] if x["host"] == "A")["phase"]
            if seen == "tg":
                break
            await asyncio.sleep(0.01)
        assert seen == "tg", ("the shared stub's stream never showed a first token "
                              f"(the last phase read was {seen!r})")
        assert host_row(health, "A")["phase"] == "tg"
    finally:
        resp = await request

    assert resp.status_code == 200
    health = await harness.healthz()
    assert host_row(health, "A")["phase"] is None
    # idle -> pp, pp -> tg, tg -> idle
    assert host_row(health, "A")["phase_switches"] == 3
