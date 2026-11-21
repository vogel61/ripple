"""Fixtures: stub upstreams, RIPPLE instances wired to them, a live proxy.

The app lifespan is not run by httpx.ASGITransport, so the fleet is started
manually here and torn down explicitly.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
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


def _wait_for_hosts(url: str, names: list[str], timeout: float = 15.0) -> None:
    """Block until every configured host has been probed healthy.

    `Fleet.start(blocking=False)` (issue #117) resolves the hosts in a
    background task, so `server.started` only means the socket is open. A
    request that arrives before the first probe is answered with `503` — no
    healthy host to place it on. A test that needs *routing* therefore has to
    wait for the verdict, not for the socket; a test that wants the unresolved
    state must not pass `wait_healthy`.

    Measured 20.11.2026: `test_fa8_streaming_request_is_a_single_upstream_call`
    failed in 3 of 6 full-suite runs with `503` and never in isolation — the
    only thing that changed between the runs was how busy the machine was.
    """
    deadline = time.monotonic() + timeout
    last: Any = "no answer"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as resp:
                payload = json.loads(resp.read())
            last = {h.get("name"): h.get("status")
                    for h in payload.get("hosts", [])}
            if all(last.get(name) == "healthy" for name in names):
                return
        except Exception as exc:                       # noqa: BLE001 — reported below
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.02)
    raise RuntimeError(
        f"live ripple proxy never reported {names} healthy within {timeout} s "
        f"(last seen: {last})")


@pytest.fixture
def live_proxy_factory() -> Callable[..., str]:
    """Serve a Ripple app with uvicorn on an ephemeral port.

    Needed where httpx.ASGITransport would hide the transport behaviour: it
    buffers the whole response body, so chunked streaming (FA-8) cannot be
    observed through it.

    `wait_healthy=True` additionally waits until every configured host has been
    probed healthy — the difference between "the app answers" and "the app can
    route", which stopped being the same thing with #117.
    """
    created: list[tuple[uvicorn.Server, threading.Thread]] = []

    def make(cfg: Config, *, wait_healthy: bool = False) -> str:
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
        url = f"http://127.0.0.1:{port}"
        if wait_healthy:
            _wait_for_hosts(url, [h.name for h in cfg.hosts])
        return url

    yield make

    for server, thread in created:
        server.should_exit = True
        thread.join(timeout=10)
