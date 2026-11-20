"""Host discovery (issue #61) against stub oMLX nodes on 127.0.0.1.

Every stub is a `ThreadingHTTPServer` on an ephemeral port: discovery is plain
HTTP with no SSE, so the stdlib server is enough here and keeps the test free of
event-loop plumbing. No test contacts a real node, and no test sweeps a network —
`scan_network` is pointed at 127.0.0.1/32.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, List, Optional
from urllib.parse import urlparse

import pytest

from ripple import discover as discover_mod
from ripple.discover import discover, scan_network


@dataclass
class NodeConfig:
    node_id: Optional[str] = "11111111-1111-4111-8111-111111111111"
    models: List[str] = field(default_factory=lambda: ["Qwen3.5-9B-oQ4e-mtp"])
    # The key the stub demands. None means the node answers without one.
    key: Optional[str] = None
    admin: bool = True
    identity_status: int = 200
    models_status: int = 200


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # keep pytest output clean
        return

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 — http.server's spelling
        cfg: NodeConfig = self.server.cfg  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        supplied = self.headers.get("authorization") or ""
        key_ok = cfg.key is not None and supplied == f"Bearer {cfg.key}"

        if path == "/api/cluster/node_id":
            if cfg.identity_status != 200:
                self._send(cfg.identity_status, {"detail": "Not Found"})
                return
            body = {"version": "0.8.0", "cluster_name": "omlx"}
            if cfg.node_id:
                body["node_id"] = cfg.node_id
            self._send(200, body)
            return

        if path == "/v1/models":
            if cfg.models_status != 200:
                self._send(cfg.models_status, {"detail": "boom"})
                return
            if cfg.key is not None and not key_ok:
                self._send(401, {"detail": "Unauthorized"})
                return
            self._send(200, {"object": "list",
                             "data": [{"id": m, "object": "model"} for m in cfg.models]})
            return

        if path == "/admin/api/models":
            if not cfg.admin:
                self._send(404, {"detail": "Not Found"})
                return
            if cfg.key is not None and not key_ok:
                self._send(401, {"detail": "Unauthorized"})
                return
            self._send(200, {"models": [{"id": m} for m in cfg.models]})
            return

        self._send(404, {"detail": "Not Found"})


class StubNode:
    """A stub oMLX node on 127.0.0.1:<ephemeral>."""

    def __init__(self, cfg: NodeConfig) -> None:
        self.cfg = cfg
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.cfg = cfg  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        name="ripple-stub-node", daemon=True)

    def start(self) -> "StubNode":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5.0)


@pytest.fixture
def node_factory() -> Callable[..., StubNode]:
    created: List[StubNode] = []

    def make(**kwargs: Any) -> StubNode:
        stub = StubNode(NodeConfig(**kwargs)).start()
        created.append(stub)
        return stub

    yield make
    for stub in created:
        stub.stop()


@pytest.fixture
def silent_port() -> int:
    """A port that accepts connections and then says nothing.

    `listen()` puts the handshake in the backlog, so the client's connect
    succeeds and its read has to time out — the case a sweep hits on every
    firewalled host.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    yield port
    sock.close()


@pytest.fixture
def closed_port() -> int:
    """A port nothing listens on (bound and closed again)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# -- discover() ------------------------------------------------------------
async def test_full_node(node_factory) -> None:
    stub = node_factory(node_id="11111111-1111-4111-8111-111111111111",
                        models=["DeepSeek-V4.1-Flash-1017-oQ4e-mtp",
                                "Qwen3.5-9B-oQ4e-mtp"],
                        key="secret")
    found = await discover([stub.url], timeout=2.0, api_key="secret")

    assert len(found) == 1
    node = found[0]
    assert node.address == stub.url
    assert node.node_id == "11111111-1111-4111-8111-111111111111"
    assert node.models == ("DeepSeek-V4.1-Flash-1017-oQ4e-mtp", "Qwen3.5-9B-oQ4e-mtp")
    assert node.latency_ms > 0.0
    assert node.name == "local"
    assert node.admin is True


async def test_address_is_normalised(node_factory) -> None:
    stub = node_factory()
    found = await discover([stub.url + "/"], timeout=2.0)

    assert [n.address for n in found] == [stub.url]


async def test_address_without_scheme(node_factory) -> None:
    stub = node_factory()
    found = await discover([f"127.0.0.1:{stub.port}"], timeout=2.0)

    assert [n.address for n in found] == [stub.url]


async def test_node_without_node_id(node_factory) -> None:
    stub = node_factory(node_id=None, models=["Qwen3.5-9B-oQ4e-mtp"])
    found = await discover([stub.url], timeout=2.0)

    assert len(found) == 1
    assert found[0].node_id is None
    assert found[0].models == ("Qwen3.5-9B-oQ4e-mtp",)
    # Without a node_id the name cannot be derived from it either, but loopback
    # is still "local".
    assert found[0].name == "local"


async def test_silent_address_is_bounded(silent_port: int) -> None:
    timeout = 0.3
    started = time.perf_counter()
    found = await discover([f"http://127.0.0.1:{silent_port}"], timeout=timeout)
    elapsed = time.perf_counter() - started

    print(f"\nsilent address: timeout={timeout}s, measured wall time={elapsed:.3f}s")
    assert found == []
    assert elapsed < 3 * timeout


async def test_closed_port_is_fast(closed_port: int) -> None:
    started = time.perf_counter()
    found = await discover([f"http://127.0.0.1:{closed_port}"], timeout=1.0)
    elapsed = time.perf_counter() - started

    print(f"\nclosed port: measured wall time={elapsed:.3f}s")
    assert found == []
    assert elapsed < 1.0


async def test_silent_address_does_not_abort_the_run(node_factory, silent_port: int) -> None:
    stub = node_factory(models=["Qwen3.5-9B-oQ4e-mtp"])
    found = await discover([f"http://127.0.0.1:{silent_port}", stub.url], timeout=0.4)

    assert [n.address for n in found] == [stub.url]


async def test_no_key_still_finds_the_node(node_factory) -> None:
    """The stranger's first run: no key, 401 on /v1/models, node still reported."""
    stub = node_factory(models=["Qwen3.5-9B-oQ4e-mtp"], key="secret")
    found = await discover([stub.url], timeout=2.0)

    assert len(found) == 1
    node = found[0]
    assert node.models == ()
    assert node.admin is False
    assert node.node_id == "11111111-1111-4111-8111-111111111111"

    # ... and with the key the same node tells us everything.
    with_key = await discover([stub.url], timeout=2.0, api_key="secret")
    assert with_key[0].models == ("Qwen3.5-9B-oQ4e-mtp",)
    assert with_key[0].admin is True


async def test_admin_404_is_not_admin_not_failure(node_factory) -> None:
    stub = node_factory(admin=False, models=["Qwen3.5-9B-oQ4e-mtp"])
    found = await discover([stub.url], timeout=2.0)

    assert len(found) == 1
    assert found[0].admin is False
    assert found[0].models == ("Qwen3.5-9B-oQ4e-mtp",)


async def test_plain_web_server_is_not_a_node(node_factory) -> None:
    """A 404 on every endpoint is not an oMLX node, however well it connects."""
    stub = node_factory(identity_status=404, models_status=404)
    assert await discover([stub.url], timeout=1.0) == []


async def test_identity_missing_but_models_401_is_evidence(node_factory) -> None:
    """An older build without the identity endpoint: the 401 on /v1/models is
    still an oMLX node answering, so it is reported (with node_id None)."""
    stub = node_factory(identity_status=404, key="secret")
    found = await discover([stub.url], timeout=1.0)

    assert len(found) == 1
    assert found[0].node_id is None
    assert found[0].models == ()
    assert found[0].admin is False


async def test_same_node_on_two_addresses_is_reported_once(node_factory) -> None:
    """`localhost` and `127.0.0.1` are one node — the node_id says so."""
    stub = node_factory(node_id="abc-123-456", models=["m"])
    found = await discover([stub.url, f"http://localhost:{stub.port}"], timeout=2.0)

    assert len(found) == 1
    assert found[0].address == stub.url


async def test_two_distinct_nodes_are_both_reported(node_factory) -> None:
    first = node_factory(node_id="aaa-111", models=["m1"])
    second = node_factory(node_id="bbb-222", models=["m2"])
    found = await discover([first.url, second.url], timeout=2.0)

    assert [n.node_id for n in found] == ["aaa-111", "bbb-222"]


# -- scan_network() --------------------------------------------------------
async def test_scan_network_finds_the_stub(node_factory) -> None:
    stub = node_factory(models=["Qwen3.5-9B-oQ4e-mtp"])

    found = await scan_network("127.0.0.1/32", port=stub.port, timeout=1.0)

    assert [n.address for n in found] == [stub.url]
    assert found[0].models == ("Qwen3.5-9B-oQ4e-mtp",)


async def test_scan_network_empty_range_finds_nothing(closed_port: int) -> None:
    assert await scan_network("127.0.0.1/32", port=closed_port, timeout=0.3) == []


def test_scan_network_rejects_oversized_range() -> None:
    with pytest.raises(ValueError, match="largest range"):
        discover_mod._scan_addresses("10.0.0.0/8", 8000)


# -- main() ----------------------------------------------------------------
def test_main_reports_a_node_as_json(node_factory, capsys) -> None:
    stub = node_factory(models=["Qwen3.5-9B-oQ4e-mtp"], key="secret")

    rc = discover_mod.main(["--address", stub.url, "--omlx-key", "secret",
                            "--json", "--timeout", "2"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["address"] == stub.url
    assert payload[0]["node_id"] == "11111111-1111-4111-8111-111111111111"
    assert payload[0]["models"] == ["Qwen3.5-9B-oQ4e-mtp"]
    assert payload[0]["admin"] is True
    assert payload[0]["name"] == "local"
    assert payload[0]["reason"] is None


def test_main_reports_missing_key_as_reason(node_factory, capsys) -> None:
    stub = node_factory(models=["Qwen3.5-9B-oQ4e-mtp"], key="secret")

    rc = discover_mod.main(["--address", stub.url, "--json", "--timeout", "2"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["models"] == []
    assert payload[0]["admin"] is False
    assert "401" in payload[0]["reason"]


def test_main_human_output_one_line_per_node(node_factory, capsys) -> None:
    stub = node_factory(models=["a", "b"])

    rc = discover_mod.main(["--address", stub.url, "--timeout", "2"])

    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert stub.url in lines[0]
    assert "node_id=11111111" in lines[0]
    assert "models=2" in lines[0]
    assert "latency=" in lines[0]
    assert "admin=yes" in lines[0]
    assert "name=local" in lines[0]


def test_main_exits_1_when_nothing_answers(closed_port: int, capsys) -> None:
    rc = discover_mod.main(["--address", f"http://127.0.0.1:{closed_port}"])

    assert rc == 1
    assert "no oMLX node answered" in capsys.readouterr().err


def test_main_does_not_scan_without_the_flag(closed_port: int, monkeypatch,
                                            capsys) -> None:
    calls: List[str] = []

    async def fake_scan(cidr: str, **kwargs: Any) -> List[Any]:
        calls.append(cidr)
        return []

    monkeypatch.setattr(discover_mod, "scan_network", fake_scan)
    rc = discover_mod.main(["--address", f"http://127.0.0.1:{closed_port}"])

    assert calls == []
    assert rc == 1


def test_main_scans_only_with_the_flag(monkeypatch, capsys) -> None:
    calls: List[str] = []

    async def fake_scan(cidr: str, **kwargs: Any) -> List[Any]:
        calls.append(cidr)
        return [discover_mod.Found(address="http://198.51.100.139:8000",
                                   node_id="22222222-2222-4222-8222-222222222222",
                                   models=("m",), latency_ms=1.5,
                                   name="node-22222222", admin=True)]

    monkeypatch.setattr(discover_mod, "scan_network", fake_scan)
    rc = discover_mod.main(["--address", "http://127.0.0.1:1", "--scan",
                            "198.51.100.139/32"])

    assert calls == ["198.51.100.139/32"]
    assert rc == 0
    out = capsys.readouterr().out
    assert "http://198.51.100.139:8000" in out
    assert "node-22222222" in out


def test_module_is_runnable_standalone() -> None:
    """`python -m ripple.discover --help` must work without any wiring."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(discover_mod.__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "-m", "ripple.discover", "--help"],
                          cwd=str(root), capture_output=True, text=True, timeout=60)

    assert proc.returncode == 0, proc.stderr
    assert "--scan" in proc.stdout
    assert "--omlx-key" in proc.stdout
