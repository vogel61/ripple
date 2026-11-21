"""The HTTP startup must not wait for the nodes (issue #117).

Measured on 20.11.2026: with oMLX on the M5 switched off, the synchronous host
resolution held uvicorn's startup for 30 s — the dashboard is served by the very
app whose lifespan was blocked, so the page appeared only once a reachable node
existed. These tests pin the three properties that fix it:

* the app answers `/` and `/healthz` while an address is still being probed,
* a host that never answers ends up `down`, not `unknown`, by itself,
* the address list costs one probe timeout, not one per entry.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ripple.backends import CONNECT_TIMEOUT_SECONDS, OmlxBackend
from ripple.config import Config
from ripple.hosts import Fleet, HostRuntime
from ripple.proxy import Ripple
from ripple.stats import Stats

from tests.support import closed_port, host_config, host_row, make_config


class SilentAddress:
    """A TCP port that accepts connections and never answers.

    The stand-in for a blackholed address: the connect succeeds, so the probe
    sits in its *read* timeout — exactly the shape of the live incident, where
    an unreachable LAN address cost the whole probe timeout per entry.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = int(self._sock.getsockname()[1])
        self._accepted: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_forever,
                                        name="silent-address", daemon=True)
        self._thread.start()

    def _accept_forever(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            # Held open and never written to: the client waits for a response.
            self._accepted.append(conn)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        for conn in self._accepted:
            try:
                conn.close()
            except OSError:
                pass
        self._sock.close()


@pytest.fixture
def silent() -> Any:
    addresses = [SilentAddress(), SilentAddress()]
    yield addresses
    for address in addresses:
        address.close()


def _fleet(cfg: Config) -> Fleet:
    return Fleet(cfg, Stats())


def test_startup_serves_the_dashboard_while_a_host_is_still_unanswered(silent):
    """Startup < 3 s, `/` and `/healthz` answer, the host is not blocking them."""
    cfg = make_config([host_config("alpha", [silent[0].url], role="entry",
                                   node_id="11111111-1111-4111-8111-111111111111")])
    app = Ripple(cfg).app

    started = time.monotonic()
    with TestClient(app) as client:
        startup = time.monotonic() - started
        assert startup < 3.0, f"startup waited {startup:.2f} s for a silent host"

        page = client.get("/")
        assert page.status_code == 200
        assert "RIPPLE" in page.text

        row = host_row(client.get("/healthz").json(), "alpha")
        # The probe is still in its read timeout: the page answered *first*.
        assert row["status"] == "unknown"
        assert row["state"] == "unknown"
        assert row["in_pool"] is False

        # And it resolves to `down` on its own, without a second start.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            row = host_row(client.get("/healthz").json(), "alpha")
            if row["state"] == "down":
                break
            time.sleep(0.1)
        assert row["state"] == "down"
        assert row["last_error"]
        # Still serving after the verdict — the dashboard never went away.
        assert client.get("/").status_code == 200


async def test_a_refused_address_reads_down_after_start():
    """A closed port is a hard failure: `down` on the first pass, no re-resolve."""
    cfg = make_config([host_config("alpha", [f"http://127.0.0.1:{closed_port()}"])])
    fleet = _fleet(cfg)
    try:
        await fleet.start(warmup=False)
        host = fleet.get("alpha")
        assert host is not None
        assert host.status == "down"
        assert host.in_pool is False
        assert host.to_dict()["state"] == "down"
    finally:
        await fleet.stop()


async def test_the_address_list_costs_one_timeout_not_one_per_entry(silent, monkeypatch):
    """The identity probes run concurrently; the order still decides the winner.

    A sequential walk would pay the read timeout once per address. The seam
    shortens the timeouts so the test stays fast, and the ratio is what is
    asserted — not the absolute number.
    """
    original = HostRuntime._make_backend

    def fast(self: HostRuntime, address: str):  # type: ignore[no-untyped-def]
        backend = original(self, address)
        backend.timeout = 0.5
        backend.connect_timeout = 0.3
        return backend

    monkeypatch.setattr(HostRuntime, "_make_backend", fast)
    cfg = make_config([host_config("alpha", [a.url for a in silent])])
    fleet = _fleet(cfg)
    try:
        started = time.monotonic()
        assert await fleet.get("alpha").resolve() is False     # type: ignore[union-attr]
        elapsed = time.monotonic() - started
        # Two phases (identity, health) over two addresses at 0.5 s each:
        # concurrent is ~1.0 s, a sequential walk needs >= 2.0 s.
        assert elapsed < 1.4, f"resolution walked the list sequentially ({elapsed:.2f} s)"
    finally:
        await fleet.stop()


def test_the_connect_timeout_is_capped_for_probes():
    """The connect is the phase an unreachable address burns (issue #117)."""
    assert CONNECT_TIMEOUT_SECONDS <= 2.0
    import httpx

    client = httpx.AsyncClient()
    try:
        backend = OmlxBackend("http://192.0.2.1:8000", "", client=client)
        assert backend.timeout == 5.0
        assert backend.connect_timeout == CONNECT_TIMEOUT_SECONDS
        # A caller may not raise the cap above the probe timeout.
        assert OmlxBackend("http://x:8000", "", client=client,
                           timeout=0.5).connect_timeout == 0.5
    finally:
        import asyncio

        asyncio.run(client.aclose())
