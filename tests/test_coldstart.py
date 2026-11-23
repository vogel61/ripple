"""The cold start, in the suite (issue #64).

`tools/coldstart.sh` proves the real thing on a real machine; this file proves
the same sequence without an oMLX: a fresh home, `ripple init --yes` against a
stub node, a proxy built from exactly that configuration, `ripple doctor` against
it, and one real completion with the client key `init` issued.

It is deliberately the *same* code path as the script — `init.main`, the real
`load_config`, the real `Ripple`, the real `doctor.main` — and not a second
implementation that could agree with itself while the script fails.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest
import yaml

from ripple import doctor as doctor_module
from ripple import init as init_module
from ripple.config import load_config

from tests.support import StubModel


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _client_key(home) -> str:
    data = yaml.safe_load((home / ".ripple" / "keys.yaml").read_text()) or {}
    keys = data.get("keys") or []
    if isinstance(keys, dict):
        keys = list(keys.values())
    for entry in keys:
        if isinstance(entry, dict) and entry.get("key"):
            return entry["key"]
    raise AssertionError("init wrote no client key")


@pytest.fixture
def cold_home(tmp_path):
    """An empty home, the way a stranger's machine looks."""
    home = tmp_path / "home"
    home.mkdir()
    return home


def _init(cold_home, url, *extra):
    argv = ["--yes", "--port", str(_free_port()), "--host", url,
            "--config", str(cold_home / "config.yaml"),
            "--home-dir", str(cold_home), *extra]
    return init_module.main(argv)


def test_the_cold_start_runs_through_against_a_stub_node(
        cold_home, stub_factory, live_proxy_factory, capsys):
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)],
                        load_simulates=True)
    assert _init(cold_home, stub.url) == 0
    out = capsys.readouterr().out
    # init does its own self-test: a real request through the new config
    assert "test request: 200" in out

    # Both files exist and only the owner may read them.
    for name in ("config.yaml", ".ripple/keys.yaml"):
        mode = (cold_home / name).stat().st_mode & 0o777
        assert mode == 0o600, f"{name} is {oct(mode)}"

    cfg = load_config(cold_home / "config.yaml")
    assert cfg.hosts[0].models == ["m1"]        # the chat-capable default (#157), not "everything"
    assert cfg.hosts[0].node_id is None or isinstance(cfg.hosts[0].node_id, str)

    url = live_proxy_factory(cfg)
    assert doctor_module.main(["--config", str(cold_home / "config.yaml"),
                               "--url", url, "--timeout", "5"]) == 0

    # The completion, with the key init issued — the last step of the protocol.
    key = _client_key(cold_home)
    resp = httpx.post(f"{url}/v1/chat/completions",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": "m1", "max_tokens": 8,
                            "messages": [{"role": "user", "content": "hi"}]},
                      timeout=30.0)
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"]


def test_a_second_run_without_force_refuses_and_names_the_file(cold_home, stub_factory,
                                                             capsys):
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)])
    assert _init(cold_home, stub.url) == 0
    before = (cold_home / "config.yaml").read_text()

    assert _init(cold_home, stub.url) != 0
    err = capsys.readouterr()
    assert str(cold_home / "config.yaml") in (err.err + err.out)
    # and it did not touch what was there
    assert (cold_home / "config.yaml").read_text() == before


def test_without_a_key_the_node_is_read_limited_and_doctor_still_passes(
        cold_home, stub_factory, live_proxy_factory, capsys, monkeypatch):
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)])
    assert _init(cold_home, stub.url, "--no-check") == 0
    cfg = load_config(cold_home / "config.yaml")
    assert not cfg.hosts[0].api_key

    url = live_proxy_factory(cfg)
    # A read-limited node is a warning, not a failure: exit 0 either way.
    assert doctor_module.main(["--config", str(cold_home / "config.yaml"),
                               "--url", url, "--timeout", "5"]) == 0
    out = capsys.readouterr().out
    assert "0 FAIL" in out


def test_the_doctor_report_is_json_when_asked(cold_home, stub_factory,
                                              live_proxy_factory, capsys):
    stub = stub_factory(models=[StubModel(id="m1", loaded=True)])
    assert _init(cold_home, stub.url) == 0
    cfg = load_config(cold_home / "config.yaml")
    url = live_proxy_factory(cfg)
    capsys.readouterr()

    assert doctor_module.main(["--config", str(cold_home / "config.yaml"),
                               "--url", url, "--json", "--timeout", "5"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["summary"]["fail"] == 0
    assert payload["checks"]
    assert any(c["check"].startswith("completion") for c in payload["checks"])
