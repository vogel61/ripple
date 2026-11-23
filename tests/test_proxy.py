"""`POST /admin/discover` against key-protected nodes (issue #155).

The sweep from the page is `ripple discover --scan` (manual §5.6.2, callout
7.2). Measured on a live instance it reported both nodes as model-less and
without admin rights, because `ripple/proxy.py` called `scan_network` without the
oMLX key: every `/v1/models` probe answered 401, so `models: []` and
`admin: false` were true of *every* build, not of the nodes.

The node here is the keyed stdlib stub from `tests/test_discover.py` — the one
stub in the suite that answers 401 without its key. No new server, and no test
touches a real node.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, List
from urllib.parse import urlparse

import httpx
import pytest
import yaml

from ripple.config import (FleetConfig, host_from_block, load_config,
                           load_overlay)
from ripple.init import Node, config_document, secrets_document
from ripple.proxy import Ripple
from tests.support import bearer, closed_port, host_config, keyed_config
from tests.test_discover import NodeConfig, StubNode

ADMIN = bearer("test-admin-key")
KEY = "secret"
MODELS = ["DeepSeek-V4.1-Flash-1017-oQ4e-mtp", "Qwen3.5-9B-oQ4e-mtp"]


@pytest.fixture
def keyed_node() -> Any:
    """A stub oMLX node that answers `/v1/models` and `/admin/api/models` only
    with `Bearer secret` — identity stays free."""
    stub = StubNode(NodeConfig(models=list(MODELS), key=KEY, admin=True)).start()
    yield stub
    stub.stop()


def _cfg(tmp_path: Path, hosts: List[Any]) -> Any:
    """The test's own overlay file, never `~/.ripple/runtime.yaml`."""
    cfg = keyed_config(hosts, str(tmp_path / "keys.yaml"))
    cfg.fleet = FleetConfig(file=str(tmp_path / "runtime.yaml"), persist=False,
                            drain_seconds=5.0, drain_poll_seconds=0.01)
    return cfg


def _port(url: str) -> int:
    port = urlparse(url).port
    assert port is not None
    return port


async def _scan(harness: Any, stub: StubNode) -> Any:
    return await harness.client.post("/admin/discover", headers=ADMIN,
                                     json={"cidr": "127.0.0.1/32",
                                           "port": _port(stub.url), "timeout": 1.0})


async def test_the_sweep_reports_the_real_model_count_of_a_keyed_node(
        keyed_node: StubNode, ripple_factory: Callable[..., Any],
        tmp_path: Path) -> None:
    """The fleet's key is the sweep's key: `model_count` means what the manual says.

    The configured host carries the key (`defaults.api_key` lands on it at load),
    so the candidate it finds on the same network must be read with that key —
    the page offered these nodes as model-less and nobody would add one.
    """
    harness = await ripple_factory(
        _cfg(tmp_path, [host_config("A", keyed_node.url, api_key=KEY)]))

    resp = await _scan(harness, keyed_node)

    assert resp.status_code == 200, resp.text
    found = resp.json()["found"]
    assert len(found) == 1
    assert found[0]["model_count"] == len(MODELS), found[0]
    assert found[0]["models"] == sorted(MODELS)
    assert found[0]["admin"] is True
    assert found[0]["reason"] is None


async def test_without_a_key_the_sweep_still_runs_and_says_why_it_is_empty(
        keyed_node: StubNode, ripple_factory: Callable[..., Any],
        tmp_path: Path) -> None:
    """No key configured: identity needs none, so the node is still found — and
    the row names the 401 instead of reading as a node that carries nothing."""
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", keyed_node.url)]))

    resp = await _scan(harness, keyed_node)

    assert resp.status_code == 200, resp.text
    found = resp.json()["found"]
    assert len(found) == 1
    assert found[0]["model_count"] == 0
    assert found[0]["admin"] is False
    assert "401" in (found[0]["reason"] or ""), found[0]


# ---------------------------------------------------------------------------
# The sampler's key is `replication.admin_key` (issue #159)
# ---------------------------------------------------------------------------
SAMPLING = "no admin key for the per-host state sampling"
OMLX_KEY = "sk-omlx-sampling-test-key"


class _Unreachable:
    """A node that is never asked: any call here means the sampler went on blind."""

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("no route to the node")


def _installed(tmp_path: Path, *, omlx_key: str = OMLX_KEY,
               keep_replication: bool = True) -> Any:
    """The configuration `ripple init` writes — or the same one without the key.

    `keep_replication=False` drops the `replication:` section: the shape of a
    pre-#159 install and of every hand-written configuration that fills only
    `defaults.api_key`.
    """
    nodes = [Node(name="A", addresses=["http://127.0.0.1:9"])]
    document = config_document(
        port=closed_port(), nodes=nodes,
        api_key=omlx_key, admin_key="test-admin-key", home=tmp_path)
    # …and the 0600 file the document points into (issue #179): a config with a
    # reference and no file behind it is a read-limited install, which is what
    # the third test in this group is about.
    secrets_path = tmp_path / ".ripple" / "secrets.yaml"
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(secrets_document(omlx_key, nodes, "test-admin-key"),
                            encoding="utf-8")
    if not keep_replication:
        document.pop("replication", None)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_config(path)


class _Host:
    """What `_activity_state` reads from a `HostRuntime`: the config and the address."""

    def __init__(self, host_cfg: Any) -> None:
        self.cfg = host_cfg
        self.resolved_address = host_cfg.addresses[0]


def _sampler(cfg: Any) -> Any:
    """A `Ripple` carrying only what `_activity_state` reads — no server, no port."""
    ripple = object.__new__(Ripple)
    ripple.cfg = cfg
    ripple._activity_warned = False
    ripple.proxy_client = _Unreachable()
    return ripple


async def test_the_installed_config_emits_no_admin_key_warning(tmp_path, caplog):
    """Issue #159: what `init` writes is enough for the per-host state sampling.

    Before this, the install filled `defaults.api_key` only, and the first start
    logged `host_stats: no oMLX admin key` while `ripple doctor` reported admin
    rights as OK on the same file.
    """
    cfg = _installed(tmp_path)
    assert cfg.replication.admin_key == cfg.hosts[0].api_key

    with caplog.at_level(logging.WARNING):
        assert await _sampler(cfg)._activity_state(_Host(cfg.hosts[0])) is None

    assert SAMPLING not in caplog.text


async def test_only_defaults_api_key_warns_once_and_names_both_fields(tmp_path,
                                                                     caplog):
    """The warning says which key is missing and that the client key is there."""
    cfg = _installed(tmp_path, keep_replication=False)
    assert cfg.hosts[0].api_key and not cfg.replication.admin_key
    ripple = _sampler(cfg)

    with caplog.at_level(logging.WARNING):
        await ripple._activity_state(_Host(cfg.hosts[0]))
        await ripple._activity_state(_Host(cfg.hosts[0]))       # one warning, not one per tick

    said = [r.getMessage() for r in caplog.records if SAMPLING in r.getMessage()]
    assert len(said) == 1, said
    assert "replication.admin_key is unset" in said[0]
    assert "defaults.api_key" in said[0]
    assert "state curves stay empty" in said[0]


async def test_without_either_key_the_warning_says_both_are_unset(tmp_path, caplog):
    """An install without any key is told so, in the same terms."""
    cfg = _installed(tmp_path, omlx_key="", keep_replication=False)

    with caplog.at_level(logging.WARNING):
        await _sampler(cfg)._activity_state(_Host(cfg.hosts[0]))

    said = [r.getMessage() for r in caplog.records if SAMPLING in r.getMessage()]
    assert len(said) == 1, said
    assert "defaults.api_key is unset too" in said[0]


# ---------------------------------------------------------------------------
# The node's own key on the way in (issue #161)
# ---------------------------------------------------------------------------
NODE_KEY = "sk-node-own-key"
NODE_MODELS = ["DeepSeek-V4.1-Flash-1017-oQ4e-mtp", "Qwen3.5-9B-oQ4e-mtp"]


class _NodeHandler(BaseHTTPRequestHandler):
    """An oMLX node that answers identity and `/health` for free.

    `/v1/models` and `/v1/models/status` only with `Bearer sk-node-own-key` —
    the shape of the real nodes (FA-23): `/health` is open, the model list is
    not, which is why a node added without its key arrives with `models=0`.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:      # noqa: A002
        return

    def do_GET(self) -> None:                                    # noqa: N802
        cfg: dict[str, Any] = self.server.cfg                     # type: ignore[attr-defined]
        path = urlparse(self.path).path
        keyed = self.headers.get("authorization") == f"Bearer {NODE_KEY}"
        status, body = 200, {}
        if path == "/health":
            body = {"status": "healthy", "engine_pool": {
                "final_ceiling": 525_360_213_856, "current_model_memory": 0,
                "model_count": len(cfg["models"]), "loaded_count": 0}}
        elif path == "/api/cluster/node_id":
            body = {"node_id": cfg["node_id"], "version": "0.8.0"}
        elif path == "/v1/models" and keyed:
            body = {"object": "list",
                    "data": [{"id": m, "object": "model"} for m in cfg["models"]]}
        elif path == "/v1/models/status" and keyed:
            body = {"models": [{"id": m, "loaded": False} for m in cfg["models"]]}
        elif path in ("/v1/models", "/v1/models/status"):
            status, body = 401, {"detail": "Unauthorized"}
        else:
            status, body = 404, {"detail": "Not Found"}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _KeyedNode:
    """The stub node above on 127.0.0.1:<ephemeral>."""

    def __init__(self, models: List[str], node_id: str = "node-161") -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _NodeHandler)
        self.server.daemon_threads = True
        self.server.cfg = {"models": list(models), "node_id": node_id}  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        name="ripple-stub-node-161", daemon=True)

    def __enter__(self) -> "_KeyedNode":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5.0)


def _add_cfg(tmp_path: Path) -> Any:
    """A fleet of one unreachable host and an overlay file this test owns.

    The node under test is *not* in it: it is the one `POST /admin/nodes` adds,
    which is the whole point — nothing about it may come from `defaults`.
    """
    cfg = keyed_config([host_config("A", "http://127.0.0.1:9")],
                       str(tmp_path / "keys.yaml"))
    cfg.fleet = FleetConfig(file=str(tmp_path / "runtime.yaml"), persist=True,
                            drain_seconds=5.0, drain_poll_seconds=0.01)
    return cfg


# ---------------------------------------------------------------------------
# A refused admin key says that it is a refused key (issue #156)
# ---------------------------------------------------------------------------
REFUSED = {"cidr": "127.0.0.1/32", "port": 9, "timeout": 0.5}


async def test_a_refused_admin_key_carries_the_admin_step_and_a_stable_code(
        ripple_factory: Callable[..., Any], tmp_path: Path, caplog) -> None:
    """#156: the 401 names the condition, so the page can name it too.

    Measured on a live instance after `ripple init --force`: four identical
    `admin_denied` lines and a page that said only "admin key required" — true,
    and useless, because the tab was holding the *previous* key. The marker is
    the fix: `step: "admin"` is what `nodeError()` maps to its own sentence,
    `code` is the stable half, and the message stays as it was.
    """
    harness = await ripple_factory(
        _cfg(tmp_path, [host_config("A", "http://127.0.0.1:9")]))

    with caplog.at_level(logging.WARNING):
        resp = await harness.client.post("/admin/discover",
                                         headers=bearer("the-previous-key"),
                                         json=REFUSED)

    assert resp.status_code == 401, resp.text
    err = resp.json()["error"]
    assert err["message"] == "admin key required"
    assert err["step"] == "admin", err
    assert err["code"] == "admin_key_refused", err
    # the key itself never reaches the log, and the refusal is still logged
    assert "admin_denied" in caplog.text
    assert "the-previous-key" not in caplog.text


async def test_the_model_lock_keeps_403_and_carries_the_same_marker(
        ripple_factory: Callable[..., Any], tmp_path: Path) -> None:
    """The 403 is the same condition, not a second one: same step, same code."""
    harness = await ripple_factory(
        _cfg(tmp_path, [host_config("A", "http://127.0.0.1:9")]))

    resp = await harness.client.post("/admin/models/Qwen3.5-9B-oQ4e-mtp/lock",
                                     headers=bearer("the-previous-key"))

    assert resp.status_code == 403, resp.text
    err = resp.json()["error"]
    assert err["message"] == "admin key required"
    assert err["step"] == "admin", err
    assert err["code"] == "admin_key_refused", err


async def test_the_rate_limited_answer_keeps_its_own_code(
        ripple_factory: Callable[..., Any], tmp_path: Path) -> None:
    """A blocked address is not a refused key: `429` names the wait instead."""
    harness = await ripple_factory(
        _cfg(tmp_path, [host_config("A", "http://127.0.0.1:9")]))

    for _ in range(harness.ripple.admin_limits.limit):
        first = await harness.client.post("/admin/discover",
                                          headers=bearer("wrong"), json=REFUSED)
        assert first.status_code == 401, first.text
    blocked = await harness.client.post("/admin/discover",
                                        headers=bearer("wrong"), json=REFUSED)

    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["error"]["code"] == "admin_rate_limited"
    assert "retry in" in blocked.json()["error"]["message"]


async def test_a_node_added_with_its_own_key_reads_its_model_list(
        ripple_factory: Callable[..., Any], tmp_path: Path) -> None:
    """#161: the key the flow asks for is the key the node is read with.

    Measured before this: `node_added name=192.0.2.9 … models=0` on a machine
    carrying 23 models, because a node inherits nothing from `defaults` and the
    flow never asked for the key. With it in the body the model list is read and
    the response says how many models the node carries.
    """
    with _KeyedNode(NODE_MODELS) as node:
        harness = await ripple_factory(_add_cfg(tmp_path))

        resp = await harness.client.post("/admin/nodes", headers=ADMIN,
                                         json={"address": node.url,
                                               "api_key": NODE_KEY})

        assert resp.status_code == 201, resp.text
        answer = resp.json()
        assert answer["models"] == len(NODE_MODELS), answer
        assert answer["models_error"] is None, answer
        host_cfg = harness.ripple.cfg.host(answer["node"]["name"])
        assert host_cfg is not None and host_cfg.api_key == NODE_KEY

        # The overlay keeps it, so a restart rebuilds the same host (#161):
        # `host_block()` writes the key, `load_overlay`/`host_from_block` read it.
        block = load_overlay(tmp_path / "runtime.yaml").hosts[answer["node"]["name"]]
        assert block["api_key"] == NODE_KEY, block
        rebuilt = host_from_block(block, {}, 0)
        assert rebuilt.api_key == NODE_KEY
        assert rebuilt.addresses == [node.url]


async def test_a_node_added_without_a_key_is_read_only_and_says_why(
        ripple_factory: Callable[..., Any], tmp_path: Path, caplog) -> None:
    """Empty is a valid answer — but `models=0` then means *unreadable*.

    The response and the `node_added` line both name the 401 instead of leaving
    "0 models" to read as "the machine carries nothing" (issue #161).
    """
    with _KeyedNode(NODE_MODELS) as node:
        harness = await ripple_factory(_add_cfg(tmp_path))

        with caplog.at_level(logging.WARNING):
            resp = await harness.client.post("/admin/nodes", headers=ADMIN,
                                             json={"address": node.url})

        assert resp.status_code == 201, resp.text
        answer = resp.json()
        assert answer["models"] == 0
        assert "401" in (answer["models_error"] or ""), answer
        said = [r.getMessage() for r in caplog.records
                if "node_added" in r.getMessage()]
        assert len(said) == 1, said
        assert "model list unreadable" in said[0], said[0]
        assert "401" in said[0], said[0]
        assert "keyed=no" in said[0], said[0]
        # read-only, not silently empty: the host is in the fleet, without a key
        host_cfg = harness.ripple.cfg.host(answer["node"]["name"])
        assert host_cfg is not None and not host_cfg.api_key


async def test_the_preview_reads_the_model_list_with_the_key_it_is_given(
        ripple_factory: Callable[..., Any], tmp_path: Path) -> None:
    """"23 models" before adding, not "0 models" after (#161).

    The preview is the same read as the add, so it must send the same key — and
    when it is given none it says so (`keyed: false`) instead of pretending the
    node is empty.
    """
    with _KeyedNode(NODE_MODELS) as node:
        harness = await ripple_factory(_add_cfg(tmp_path))

        keyed = await harness.client.post(
            "/admin/nodes?dry_run=true", headers=ADMIN,
            json={"address": node.url, "api_key": NODE_KEY})
        assert keyed.status_code == 200, keyed.text
        assert keyed.json()["model_count"] == len(NODE_MODELS), keyed.json()
        assert keyed.json()["keyed"] is True
        assert keyed.json()["step"] is None

        blind = await harness.client.post(
            "/admin/nodes?dry_run=true", headers=ADMIN, json={"address": node.url})
        assert blind.status_code == 200, blind.text
        assert blind.json()["model_count"] == 0
        assert blind.json()["keyed"] is False
        assert "401" in (blind.json()["models_error"] or ""), blind.json()
        # and nothing was written by either call
        assert harness.ripple.cfg.host("127.0.0.1") is None
        assert not (tmp_path / "runtime.yaml").exists()
