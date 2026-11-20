"""`ripple init`: from an empty directory to a configuration the proxy can use.

Every test runs against a `tmp_path` home, so nothing here reads or writes the
real `~/.ripple`, `~/.omlx` or `~/Library`. The stub upstream is a real HTTP
server on an ephemeral port, so the last test exercises the actual request path
(`--check` plus one completion) instead of a description of it.

Two properties are load-bearing:

* the files must be `0600` and must load through the *real* `Config` and
  `KeyStore` readers — a writer tested only against itself proves nothing;
* `--yes` and the conversation must ask the same questions in the same order,
  because the cold-start test drives `--yes` and would otherwise test something
  other than what a human gets.
"""

from __future__ import annotations

import dataclasses
import os
import socket
import stat
import sys
import types
from pathlib import Path

import pytest
import yaml

from ripple.config import load_config
from ripple.init import (AutoPrompter, ConsolePrompter, Options, main, probe_port,
                         run)
from ripple.keys import KeyStore
from tests.support import StubModel, StubState, StubUpstream, closed_port


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


def _keys_path(tmp_path: Path) -> Path:
    return tmp_path / ".ripple" / "keys.yaml"


def _argv(tmp_path: Path, port: int, *extra: str) -> list[str]:
    return ["--yes", "--port", str(port), "--home-dir", str(tmp_path),
            "--config", str(_config_path(tmp_path)), *extra]


def _fake_discovery(monkeypatch: pytest.MonkeyPatch, *,
                    models: tuple = ("m1", "m2")) -> None:
    """Install a stand-in `ripple.discover` (issue #61 is written in parallel).

    Shadowing the module in `sys.modules` keeps these tests valid after the real
    one is merged: the lazy import inside `ripple.init` picks up whichever
    module is there.
    """
    module = types.ModuleType("ripple.discover")
    found_type = dataclasses.make_dataclass(
        "Found",
        [("address", str), ("node_id", object), ("models", tuple),
         ("latency_ms", float), ("name", str), ("admin", bool)],
        frozen=True)

    async def discover(addresses, *, timeout: float = 1.0, api_key=None):
        return [found_type(address=a, node_id="node-id-1", models=models,
                           latency_ms=0.4, name="stub-node", admin=True)
                for a in addresses]

    module.Found = found_type        # type: ignore[attr-defined]
    module.discover = discover       # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ripple.discover", module)


# -- the happy path ---------------------------------------------------------
def test_yes_run_writes_config_and_keys(tmp_path, capsys):
    port = closed_port()
    code = main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--no-check"))

    assert code == 0
    config, keys = _config_path(tmp_path), _keys_path(tmp_path)
    assert config.is_file() and keys.is_file()
    assert _mode(config) == 0o600
    assert _mode(keys) == 0o600

    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    admin_key = raw["auth"]["admin_key"]
    assert len(admin_key) == 32
    assert all(c in "0123456789abcdef" for c in admin_key)

    # both files load through the readers the proxy itself uses
    store = KeyStore(keys)
    assert [k.name for k in store.list()] == ["first-client"]
    cfg = load_config(config)
    assert cfg.listen_port == port
    assert [h.name for h in cfg.hosts] == ["node-1"]
    assert cfg.hosts[0].addresses == ["http://127.0.0.1:1"]
    assert cfg.auth.enabled is True
    assert cfg.auth.admin_key == admin_key
    assert Path(cfg.auth.keys_file).resolve() == keys.resolve()
    # every state file lives under the given home, never under the real one
    assert Path(cfg.stats.file).resolve() == (tmp_path / ".ripple" /
                                              "host_stats.json").resolve()

    # the client key is shown exactly once, in clear text
    out = capsys.readouterr().out
    client_key = store.list()[0].key
    assert out.count(client_key) == 1


def test_run_without_omlx_key_writes_no_api_key(tmp_path):
    port = closed_port()
    assert main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--no-check")) == 0

    raw = yaml.safe_load(_config_path(tmp_path).read_text(encoding="utf-8"))
    assert "api_key" not in raw["defaults"]
    cfg = load_config(_config_path(tmp_path))
    assert cfg.hosts[0].api_key == ""


def test_omlx_key_goes_into_defaults_and_is_never_echoed(tmp_path, capsys):
    port = closed_port()
    key = "sk-test-not-a-real-key"
    assert main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--omlx-key", key, "--no-check")) == 0

    raw = yaml.safe_load(_config_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["defaults"]["api_key"] == key
    assert load_config(_config_path(tmp_path)).hosts[0].api_key == key
    captured = capsys.readouterr()
    assert key not in captured.out
    assert key not in captured.err


# -- refusing to overwrite --------------------------------------------------
def test_refusal_happens_before_the_first_question(tmp_path, capsys):
    port = closed_port()
    argv = _argv(tmp_path, port, "--host", "http://127.0.0.1:1", "--no-check")
    assert main(argv) == 0
    capsys.readouterr()

    def _must_not_ask(_prompt: str) -> str:
        raise AssertionError("the refusal must not ask a single question")

    prompter = ConsolePrompter(input_fn=_must_not_ask, secret_fn=_must_not_ask)
    code = run(Options(port=port, home_dir=tmp_path,
                       config_path=_config_path(tmp_path), check=False),
               prompter, out=lambda _line: None)
    assert code == 1
    assert prompter.questions == []
    assert str(_config_path(tmp_path).resolve()) in capsys.readouterr().err


def test_name_overrides_a_single_node_and_is_refused_for_two(tmp_path, capsys):
    port = closed_port()
    assert main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--name", "beta", "--no-check")) == 0
    assert [h.name for h in load_config(_config_path(tmp_path)).hosts] == \
        ["beta"]

    other = tmp_path / "two"
    other.mkdir()
    code = main(_argv(other, port, "--host", "http://127.0.0.1:1",
                      "--host", "http://127.0.0.1:2", "--name", "x",
                      "--no-check"))
    assert code == 1
    assert "--name needs exactly one node" in capsys.readouterr().err
    assert not (other / "config.yaml").exists()


def test_second_run_refuses_and_names_the_path(tmp_path, capsys):
    port = closed_port()
    argv = _argv(tmp_path, port, "--host", "http://127.0.0.1:1", "--no-check")
    assert main(argv) == 0
    first_admin = yaml.safe_load(
        _config_path(tmp_path).read_text(encoding="utf-8"))["auth"]["admin_key"]
    capsys.readouterr()

    assert main(argv) == 1
    err = capsys.readouterr().err
    assert str(_config_path(tmp_path).resolve()) in err
    assert "--force" in err

    # with the config out of the way, the key store is the one that refuses —
    # and it is named by its own path, not by the config's
    _config_path(tmp_path).unlink()
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert str(_keys_path(tmp_path).resolve()) in err

    # --force replaces both files, and the admin key really is a new one
    assert main(argv + ["--force"]) == 0
    second_admin = yaml.safe_load(
        _config_path(tmp_path).read_text(encoding="utf-8"))["auth"]["admin_key"]
    assert second_admin != first_admin
    assert _mode(_keys_path(tmp_path)) == 0o600


# -- the port ---------------------------------------------------------------
def test_busy_port_moves_to_the_next_free_one(tmp_path):
    port = closed_port()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    try:
        state = probe_port(port)
        assert state.free is False
        # lsof names the holder when it is allowed to; the verdict is the bind
        assert state.pid in (None, os.getpid())

        code = main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                          "--no-check"))
        assert code == 0
        cfg = load_config(_config_path(tmp_path))
        assert cfg.listen_port != port
        assert cfg.listen_port > port
        assert probe_port(cfg.listen_port).free is True
    finally:
        listener.close()


# -- one flow, two front ends ----------------------------------------------
def _normalise(document: dict, home: Path) -> dict:
    """Replace the home prefix so two runs in two homes are comparable."""
    text = yaml.safe_dump(document, sort_keys=True)
    return yaml.safe_load(text.replace(str(home), "<home>"))


def test_interactive_and_yes_produce_the_same_config(tmp_path, monkeypatch):
    _fake_discovery(monkeypatch)
    port = closed_port()

    yes_home = tmp_path / "yes"
    yes_home.mkdir()
    auto = AutoPrompter(out=lambda _line: None)
    assert run(Options(yes=True, port=port, home_dir=yes_home,
                       config_path=yes_home / "config.yaml", check=False),
               auto, out=lambda _line: None) == 0

    # the conversation, every answer the default (an empty line)
    con_home = tmp_path / "con"
    con_home.mkdir()
    prompter = ConsolePrompter(input_fn=lambda _prompt: "",
                               secret_fn=lambda _prompt: "")
    assert run(Options(port=port, home_dir=con_home,
                       config_path=con_home / "config.yaml", check=False),
               prompter, out=lambda _line: None) == 0

    # same questions, same order — the strongest statement that this is one flow
    assert prompter.questions == auto.questions
    assert prompter.questions            # …and that it actually asked something

    yes_doc = _normalise(yaml.safe_load(
        (yes_home / "config.yaml").read_text(encoding="utf-8")), yes_home)
    con_doc = _normalise(yaml.safe_load(
        (con_home / "config.yaml").read_text(encoding="utf-8")), con_home)
    # the admin key is generated, so it is the one field that must differ
    assert yes_doc["auth"].pop("admin_key") != con_doc["auth"].pop("admin_key")
    assert yes_doc == con_doc


def test_discovered_node_id_reaches_the_config_and_the_allow_list_stays_open(
        tmp_path, monkeypatch):
    """The node's identity is written; the model list is not frozen into the file.

    An empty `models:` means "everything this node offers". Writing tonight's 23
    models in would make RIPPLE hide every model the operator adds later — and
    say nothing about it.
    """
    _fake_discovery(monkeypatch, models=("m1", "m2"))
    port = closed_port()
    assert main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--no-check")) == 0

    cfg = load_config(_config_path(tmp_path))
    assert cfg.hosts[0].name == "stub-node"
    assert cfg.hosts[0].node_id == "node-id-1"
    assert cfg.hosts[0].models == []
    assert cfg.hosts[0].model_allowed("something-added-later") is True


# -- the final self-test ----------------------------------------------------
def test_check_and_one_request_through_the_new_config(tmp_path, capsys):
    stub = StubUpstream(StubState(models=[StubModel(id="m1", loaded=True)])).start()
    try:
        code = main(_argv(tmp_path, closed_port(), "--host", stub.url))
        out = capsys.readouterr().out
        assert code == 0, out
        assert "test request: 200" in out
        assert stub.state.chat_calls == 1
        # the read-limited state is stated, not hidden
        assert "no api_key" in out
    finally:
        stub.stop()


# -- the site question: what an empty answer does ---------------------------
def test_enter_at_the_site_question_writes_local(tmp_path, monkeypatch):
    """An empty line is the default `local`, not "no site" (issue #74).

    `ConsolePrompter.text` returns its default on an empty answer, so a prompt
    with `"local"` can never be answered "no site" by pressing Enter. The guide
    documents that behaviour; this test pins it through the real prompter, and
    asserts that the question's text says what Enter does, so code, docstring
    and guide cannot drift apart again.
    """
    from ripple import init as init_module
    from ripple.init import Node, _step_site

    monkeypatch.setattr(init_module, "_interface_networks",
                        lambda: ["192.0.2.0/24"])
    asked: list[str] = []

    def enter(prompt: str) -> str:
        asked.append(prompt)
        return ""                      # the operator pressed Enter

    prompter = ConsolePrompter(input_fn=enter, secret_fn=lambda _p: "")
    lines: list[str] = []
    site = _step_site(Options(), [Node(name="alpha", addresses=["192.0.2.5"])],
                      prompter, lines.append)

    # the block is written, with the derived network, and the node is inside it
    assert site is not None
    assert (site.id, site.name) == ("local", "local")
    assert site.networks == ["192.0.2.0/24"]
    assert lines == ["site local: local — 192.0.2.0/24",
                     "  nodes inside it: alpha"]
    # the question states what Enter does — and it is not the "no site" path
    assert len(asked) == 2
    assert "empty = local" in asked[0]


def test_a_blank_site_name_option_really_means_no_site(tmp_path, monkeypatch):
    """`--site-name " "` is the documented way to decline; it must not write " ".

    With the question's default being `local`, the option is the only way to
    say "no site" — so a value that strips to nothing has to be stripped before
    the emptiness check, or `init` writes a site whose name is a space.
    """
    from ripple import init as init_module

    monkeypatch.setattr(init_module, "_interface_networks",
                        lambda: ["192.0.2.0/24"])
    assert init_module.main(_argv(tmp_path, closed_port(),
                                  "--host", "http://192.0.2.5:1",
                                  "--site-name", " ", "--no-check")) == 0

    cfg = load_config(_config_path(tmp_path))
    assert cfg.sites == []
    assert cfg.hosts[0].site == ""
