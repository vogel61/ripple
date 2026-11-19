"""Fixtures: stub upstreams, RIPPLE instances wired to them, a live proxy.

The app lifespan is not run by httpx.ASGITransport, so the fleet is started
manually here and torn down explicitly.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable

import httpx
import pytest
import uvicorn

from ripple.config import Config
from ripple.proxy import Ripple

from tests.support import Harness, StubState, StubUpstream


@pytest.fixture
def stub_factory() -> Callable[..., StubUpstream]:
    """Create stub oMLX instances; they are stopped at the end of the test."""
    created: list[StubUpstream] = []

    def make(**state_kwargs: Any) -> StubUpstream:
        stub = StubUpstream(StubState(**state_kwargs)).start()
        created.append(stub)
        return stub

    yield make
    for stub in created:
        stub.stop()


@pytest.fixture
async def ripple_factory() -> Callable[[Config], Any]:
    """Build a Ripple instance for a config and hand back a client for its app."""
    created: list[Harness] = []

    async def make(cfg: Config) -> Harness:
        ripple = Ripple(cfg)
        await ripple.fleet.start(warmup=False)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=ripple.app),
                                   base_url="http://ripple", timeout=30.0)
        harness = Harness(ripple, client)
        created.append(harness)
        return harness

    yield make

    for harness in created:
        await harness.client.aclose()
        await harness.ripple.fleet.stop()
        await harness.ripple.proxy_client.aclose()
    # let the cancelled refresh tasks observe their cancellation
    await asyncio.sleep(0.01)


@pytest.fixture
def live_proxy_factory() -> Callable[[Config], str]:
    """Serve a Ripple app with uvicorn on an ephemeral port.

    Needed where httpx.ASGITransport would hide the transport behaviour: it
    buffers the whole response body, so chunked streaming (FA-8) cannot be
    observed through it.
    """
    created: list[tuple[uvicorn.Server, threading.Thread]] = []

    def make(cfg: Config) -> str:
        ripple = Ripple(cfg)
        server = uvicorn.Server(uvicorn.Config(
            ripple.app, host="127.0.0.1", port=0, log_config=None,
            access_log=False, lifespan="on", timeout_graceful_shutdown=1,
            # as in `__main__`: uvicorn must not rewrite the peer address from
            # `X-Forwarded-For`, that is `proxy.trusted_proxies`' job
            proxy_headers=False))
        thread = threading.Thread(target=server.run, name="ripple-live",
                                  daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("live ripple proxy failed to start")
            time.sleep(0.005)
        created.append((server, thread))
        port = server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    yield make

    for server, thread in created:
        server.should_exit = True
        thread.join(timeout=10)
