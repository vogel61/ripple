"""The operator's install path, walked once, end to end (issue #160).

Six defects reached the operator on one morning and every one of them had green
unit tests: #151 (a port a just-closed listener left in `TIME_WAIT` read as
taken), #152 (the comma list `init` itself proposed was refused), #153 (an empty
`${VAR}` was silent), #154 (`--force` overwrote without a backup), #158 (the
admin key was never shown), #159 (the oMLX key went to `defaults.api_key` only).
Each part was correct; the path between the parts was not. So this module does
not test a part — it performs the walk:

    stop the proxy → `init --force --yes` → look at what it says
    → `init --force` again (the machine is already configured)
    → `ripple start` → read the log

Everything runs against a `ThreadingHTTPServer` stub on 127.0.0.1 and a
`tmp_path` home. Nothing here binds 8000 or 8010 (the operator's proxy lives
there), touches `~/.ripple`, or contacts a real oMLX.

Isolation is load-bearing, and it is learned the hard way: `init` writes
`log_file: ~/Library/Logs/ripple.log` into the configuration it generates, so a
test that starts the proxy with that file untouched appends its own start-up
warnings to the operator's log and then reads them back — the "no warnings"
assertion then passes on a polluted file or fails on an unrelated line. Every
store (`stats.file`, `placement.model_policy_file`, `placement.maintenance_file`,
`placement.loading.calibration_file`, `fleet.file`, `auth.keys_file`) is moved
into `tmp_path` and the test asserts it, plus that `~/.ripple` did not change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ripple import init as init_mod
from ripple.config import load_config
from ripple.keys import KeyStore

#: The stub node's two models: one chat model, one virtual `markitdown` engine
#: (oMLX's word for a converter that carries no weights — `ripple/hosts.py`).
CHAT_MODEL = "Qwen3.5-9B-oQ4e-mtp"
INFRA_MODEL = "infra-markitdown"
NODE_ID = "11111111-1111-4111-8111-111111111111"
#: A fake oMLX key. It goes into `defaults.api_key` *and* `replication.admin_key`
#: (issue #159); the stub demands it on every model and admin call.
OMLX_KEY = "sk-smoke-omlx-key-0123456789"

#: The two networks the machine "has", as `init` would derive them. The default
#: it offers is the comma-separated line — the shape issue #152 refused.
CIDR_A = "192.0.2.0/24"
CIDR_B = "172.16.60.0/24"

#: What a healthy start must not write into its log. The first entry is the
#: sentence `admin_key_gap` logs (issue #159); the second is
#: `config.REBIND_NO_PREFILL_RATE`; the rest are the per-host and per-request
#: failures an install that lost its keys produces.
FORBIDDEN_IN_LOG = (
    "host_stats: no admin key",
    "no destination has a prefill rate",
    "models_unreadable",
    "admin_denied",
    "auth_denied",
)

#: `~/.ripple` files the *live* instance rewrites on its own clock. Their content
#: is not this test's to compare — the operator's proxy flushes its ledger every
#: 30 s — so the unchanged-check skips them by name and the config assertions
#: below prove that this test's paths are not theirs.
LIVE_FILES = frozenset({"host_stats.json", "host_stats.json.tmp"})

#: The checkout this module lives in — the `ripple start` subprocess runs from
#: here so it imports this tree's package and not an installed copy.
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The stub node
# ---------------------------------------------------------------------------
class _StubHandler(BaseHTTPRequestHandler):
    """The three endpoints `init` and the running proxy actually ask for.

    `/v1/models` and every admin call demand `Bearer OMLX_KEY` and answer 401
    without it — that is how a real node behaves, and it is what makes the
    install's `defaults.api_key` and `replication.admin_key` observable at all.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # keep pytest quiet
        return

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _model(self, model_id: str, engine: str) -> dict[str, Any]:
        return {"id": model_id, "object": "model", "model_type": engine,
                "engine_type": engine}

    def do_GET(self) -> None:  # noqa: N802 — http.server's spelling
        path = urlparse(self.path).path
        supplied = self.headers.get("authorization") or ""
        key_ok = supplied == f"Bearer {OMLX_KEY}"

        if path == "/health":
            self._send(200, {"status": "healthy", "engine_pool": {
                "final_ceiling": 487_700_000_000,
                "current_model_memory": 103_900_000_000,
                "loaded_count": 1, "model_count": 2}})
            return

        # Identity answers without a key — that is what lets `init` find the
        # node before it has asked for one.
        if path == "/api/cluster/node_id":
            self._send(200, {"version": "0.8.0", "cluster_name": "omlx",
                             "node_id": NODE_ID})
            return

        if path == "/v1/models":
            if not key_ok:
                self._send(401, {"detail": "Unauthorized"})
                return
            self._send(200, {"object": "list", "data": [
                self._model(CHAT_MODEL, "llm"),
                self._model(INFRA_MODEL, "markitdown"),
            ]})
            return

        if path == "/v1/models/status":
            if not key_ok:
                self._send(401, {"detail": "Unauthorized"})
                return
            self._send(200, {"models": [
                {"id": CHAT_MODEL, "loaded": True, "pinned": False,
                 "estimated_size": 103_900_000_000,
                 "actual_size": 103_900_000_000,
                 "max_context_window": 32768, "engine_type": "llm"},
                {"id": INFRA_MODEL, "loaded": False, "pinned": False,
                 "estimated_size": 0, "actual_size": 0,
                 "max_context_window": 32768, "engine_type": "markitdown"},
            ]})
            return

        if path == "/admin/api/models":
            if not key_ok:
                self._send(401, {"detail": "Unauthorized"})
                return
            self._send(200, {"models": [{"id": CHAT_MODEL}, {"id": INFRA_MODEL}]})
            return

        # The per-host state sampler (issue #33). 401 without the admin key is
        # what makes a missing `replication.admin_key` visible as a log line
        # instead of an empty curve (issue #159).
        if path == "/admin/api/activity":
            if not key_ok:
                self._send(401, {"detail": "Unauthorized"})
                return
            self._send(200, {"active_models": {"models": [
                {"id": CHAT_MODEL, "prefilling": [], "generating": [],
                 "is_loading": False},
            ]}})
            return

        if path == "/admin/api/usage":
            if not key_ok:
                self._send(401, {"detail": "Unauthorized"})
                return
            self._send(200, {"range": "today", "totals": {
                "requests": 0, "prefill_seconds": 0.0,
                "generation_seconds": 0.0}})
            return

        self._send(404, {"detail": "Not Found"})


class _StubNode:
    """A stub oMLX node on 127.0.0.1:<ephemeral>, the shape of `StubNode`."""

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        name="ripple-install-smoke-stub",
                                        daemon=True)

    def start(self) -> "_StubNode":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# The operator's port: one connection, both ends closed
# ---------------------------------------------------------------------------
def _port_left_in_time_wait() -> int:
    """Bind, accept one connection, close both ends — then close the listener.

    The server side closes first, so the 4-tuple is left in `TIME_WAIT` with
    nobody listening. That is the state a stopped proxy leaves behind, and the
    one that made `init` report "an unidentified process" and move to 8011
    (issue #151). A plain `bind` on the port really does fail (errno 48); a
    `SO_REUSEADDR` bind — what uvicorn does — does not.
    """
    while True:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        if port not in (8000, 8010):        # never the operator's proxy
            break
        listener.close()

    listener.listen(1)
    client = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        accepted, _ = listener.accept()
        accepted.close()                    # the server closes first
        assert client.recv(1) == b""        # wait for the FIN, so it is TIME_WAIT
    finally:
        client.close()
        listener.close()
    return port


# ---------------------------------------------------------------------------
# Isolation helpers
# ---------------------------------------------------------------------------
def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _ripple_home_state() -> dict[str, str]:
    """Content hashes of `~/.ripple`, so "the test wrote nothing" is checkable."""
    root = Path.home() / ".ripple"
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in LIVE_FILES:
            continue
        try:
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
        except OSError as exc:                  # unreadable is still a state
            out[str(path.relative_to(root))] = f"unreadable: {exc}"
    return out


def _healthz(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                    timeout=2.0) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def _wait_until_healthy(port: int, seconds: float = 20.0) -> dict[str, Any]:
    """`/healthz` until the node is probed healthy *and* its models are known.

    The app answers `/healthz` as soon as uvicorn binds; the fleet probes in the
    background on purpose (issue #117). Waiting for the state, not for the
    socket, is what makes this a test of the walk rather than of a race.
    """
    deadline = time.time() + seconds
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = _healthz(port)
        if last is not None:
            hosts = last.get("hosts") or []
            if (hosts and hosts[0].get("status") == "healthy"
                    and last.get("models")):
                return last
        time.sleep(0.05)
    raise AssertionError(f"/healthz on port {port} never reported the stub "
                         f"healthy with its models; last answer: {last}")


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------
def test_the_install_path_stop_init_start(tmp_path, capsys, monkeypatch):
    """One test, one sequence — a failure says "the install path is broken"."""
    home_before = _ripple_home_state()

    stub = _StubNode().start()
    try:
        port = _port_left_in_time_wait()
        config = tmp_path / "config.yaml"
        keys_path = tmp_path / ".ripple" / "keys.yaml"

        # The operator's shell exports the oMLX key; `init` reads it before the
        # key file, and asks the nodes again once it has one (the 401 → 200
        # path). `_interface_networks` is the machine's interface list: pinned
        # so the offered default is the same comma line on every machine.
        monkeypatch.setenv("OMLX_API_KEY", OMLX_KEY)
        monkeypatch.setattr(init_mod, "_interface_networks",
                            lambda: [CIDR_A, CIDR_B])

        argv = ["--yes", "--force", "--port", str(port), "--host", stub.url,
                "--home-dir", str(tmp_path), "--config", str(config),
                "--site-name", "Site A West", "--no-check"]

        # -- 1. the first run, on a machine that has nothing yet --------------
        assert init_mod.main(argv) == 0
        first = capsys.readouterr()
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))

        # the port the operator named survives: no silent move, no complaint
        assert raw["listen_port"] == port, (
            f"init moved the port to {raw['listen_port']} (asked for {port})")
        for phrase in ("already in use", "cannot be bound", "was not free"):
            assert phrase not in first.out, first.out
            assert phrase not in first.err, first.err

        # the comma default it proposed is written as a list of CIDRs (#152)
        assert len(raw["sites"]) == 1
        site = raw["sites"][0]
        assert site["id"] == "site-a-west"
        assert isinstance(site["networks"], list), site
        assert site["networks"] == [CIDR_A, CIDR_B], (
            "the comma-separated default did not become a list of CIDRs "
            f"(#152): {site['networks']!r}")
        assert "is not a CIDR network" not in first.err, first.err
        assert "no networks" not in first.out, first.out

        # the allow-list is the chat model, not the utility engine beside it
        # (#157): the stub's second model is `model_type: markitdown`, and an
        # empty answer used to route it as if it were a chat model
        assert raw["hosts"][0]["models"] == [CHAT_MODEL], raw["hosts"][0]
        assert any("allow-list = 1 chat-capable model(s)" in line
                   and f"{INFRA_MODEL} (markitdown)" in line
                   for line in first.out.splitlines()), first.out

        # the generated admin key is printed once and is the one written (#158);
        # it lives in the 0600 secret file, the config names it (#179)
        secrets = yaml.safe_load(
            (tmp_path / ".ripple" / "secrets.yaml").read_text(encoding="utf-8"))
        assert raw["auth"]["admin_key_file"].endswith("#admin")
        admin_key = secrets["admin"]
        assert re.fullmatch(r"[0-9a-f]{32}", admin_key), admin_key
        assert first.out.count(admin_key) == 1, first.out
        assert "RIPPLE admin key — shown once, store it now:" in first.out

        # one oMLX key, both names it is read under (#159) — as a reference
        # into the secret file, never as a literal (#179)
        assert raw["defaults"]["api_key_file"].endswith("#omlx.fleet")
        assert secrets["omlx"]["fleet"] == OMLX_KEY
        replication = raw.get("replication") or {}
        assert replication.get("admin_key_file", "").endswith("#omlx.fleet"), (
            "the oMLX key did not land in replication.admin_key, so the state "
            f"sampling is blind (#159): {replication!r}")
        assert "enabled" not in replication

        # nothing to back up on a machine that had nothing (#154)
        assert list(tmp_path.glob("config.yaml.bak*")) == []
        assert list(keys_path.parent.glob("keys.yaml.bak*")) == []

        # the client key is shown once, and the store the proxy reads has it
        store = KeyStore(keys_path)
        client = store.list()[0]
        assert first.out.count(client.key) == 1, first.out
        # The store the proxy reads carries the secret (mode 0600, below); the
        # only representation that may leave the process is the masked one.
        assert client.key in keys_path.read_text(encoding="utf-8")
        assert store.masked()[0]["key"] == client.masked_key()
        assert client.key not in yaml.safe_dump(store.masked())
        assert _mode(config) == 0o600 and _mode(keys_path) == 0o600

        first_config, first_keys = config.read_bytes(), keys_path.read_bytes()

        # -- 2. `--force` on a configured machine keeps what it replaces ------
        assert init_mod.main(argv) == 0
        second = capsys.readouterr()

        config_backups = sorted(tmp_path.glob("config.yaml.bak*"))
        keys_backups = sorted(keys_path.parent.glob("keys.yaml.bak*"))
        assert len(config_backups) == 1, config_backups
        assert len(keys_backups) == 1, keys_backups
        assert config_backups[0].read_bytes() == first_config
        assert keys_backups[0].read_bytes() == first_keys
        assert str(config_backups[0]) in second.out
        assert str(keys_backups[0]) in second.out

        second_admin = yaml.safe_load(
            (tmp_path / ".ripple" / "secrets.yaml").read_text(
                encoding="utf-8"))["admin"]
        assert second_admin != admin_key
        assert second.out.count(second_admin) == 1, second.out

        # -- 3. the configuration as `ripple start` will read it --------------
        # `init` wrote the log under this home already (a `--home-dir` install
        # never names `~/Library`), but the operator's real install names the
        # home log — so the test rewrites that one key explicitly, and the
        # update check goes off: this machine has no internet in a test.
        started = yaml.safe_load(config.read_text(encoding="utf-8"))
        started["log_file"] = str(tmp_path / "ripple.log")
        started["update"] = {"check": False}
        # Issue #153's probe, and a realistic one: a field written as `${VAR}`
        # with the variable unset in this environment. An empty expansion used
        # to load silently; the start-up log has to name the field and the
        # variable instead of leaving the operator with an unexplained "".
        started["sites"][0]["name"] = "${RIPPLE_SMOKE_SITE_NAME}"
        # A node whose cold prefill rate was measured. Without it `placement.
        # rebind` is on with nothing to price a move against and the #81 finding
        # ("no destination has a prefill rate") is logged on every fresh start —
        # by design, and not the install's fault. With it, that line's absence
        # is a statement about this install and not about the default.
        started["hosts"][0]["pp_tps"] = 1200.0
        config.write_text(yaml.safe_dump(started, sort_keys=False,
                                         allow_unicode=True), encoding="utf-8")
        monkeypatch.delenv("RIPPLE_SMOKE_SITE_NAME", raising=False)

        cfg = load_config(config)
        # every store this instance can write is under tmp_path — the reason the
        # test cannot take a node out of the operator's pool (21.11.2026)
        for path in (cfg.stats.file, cfg.placement.model_policy_file,
                     cfg.placement.maintenance_file,
                     cfg.placement.loading.calibration_file, cfg.fleet.file,
                     cfg.auth.keys_file, cfg.log_file):
            assert str(Path(path).resolve()).startswith(str(tmp_path.resolve())), path
        assert cfg.listen_port == port

        # -- 4. `ripple start`, as a real process ----------------------------
        # Its stdout and stderr go into the file the configuration names, which
        # is the arrangement the launchd job uses (both streams redirected into
        # `log_file`, see `service.py`): `ripple start` itself sets up no file
        # handler, so this *is* the log the operator reads afterwards.
        log = tmp_path / "ripple.log"
        env = {k: v for k, v in os.environ.items()
               if k not in ("RIPPLE_PORT", "RIPPLE_CONFIG", "OMLX_API_KEY",
                            "RIPPLE_SMOKE_SITE_NAME")}
        env["RIPPLE_CONFIG"] = str(config)
        # A label launchd does not know, so `ripple start` runs in the
        # foreground instead of reporting the operator's ai.ripple job.
        env["RIPPLE_LABEL"] = f"ai.ripple.install-smoke-{os.getpid()}"
        with log.open("wb") as sink:
            proc = subprocess.Popen(
                [sys.executable, "-m", "ripple", "start"], cwd=str(ROOT),
                env=env, stdout=sink, stderr=subprocess.STDOUT)
        try:
            payload = _wait_until_healthy(port)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:      # pragma: no cover
                proc.kill()
                proc.wait(timeout=5)

        assert proc.returncode in (0, -15), (
            log.read_text(encoding="utf-8", errors="replace"))

        # the node the install configured is the one /healthz reports
        hosts = payload["hosts"]
        assert len(hosts) == 1, hosts
        assert hosts[0]["name"] == started["hosts"][0]["name"]
        assert hosts[0]["status"] == "healthy", hosts[0]
        models = {row["id"]: row for row in payload["models"]}
        assert set(models) == {CHAT_MODEL, INFRA_MODEL}, models
        assert models[CHAT_MODEL]["routable"] is True

        # …and the log says nothing about the six ways this install used to lie
        text = log.read_text(encoding="utf-8", errors="replace")
        # A positive marker first: this log is *this* instance's, and the empty
        # `${VAR}` is named — field and variable, never the value. (The ledger
        # note is not a marker any more: a missing ledger on the first start is
        # normal and logs at INFO since issue #175, below this log level.)
        assert ("sites[0].name: ${RIPPLE_SMOKE_SITE_NAME} is empty"
                in text), text
        for phrase in FORBIDDEN_IN_LOG:
            assert phrase not in text, (
                f"a healthy start logged {phrase!r}:\n{text}")
    finally:
        stub.stop()

    # nothing of this test landed in the operator's stores
    assert _ripple_home_state() == home_before
