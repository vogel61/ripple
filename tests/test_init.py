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
import re
import socket
import stat
import sys
import types
from pathlib import Path

import pytest
import yaml

from ripple.config import load_config
from ripple.init import (AutoPrompter, ConsolePrompter, Options, PortState, main,
                         probe_port, run)
from ripple.keys import KeyStore
from tests.support import StubModel, StubState, StubUpstream, closed_port


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


def _keys_path(tmp_path: Path) -> Path:
    return tmp_path / ".ripple" / "keys.yaml"


def _secrets_path(tmp_path: Path) -> Path:
    return tmp_path / ".ripple" / "secrets.yaml"


def _secrets(tmp_path: Path) -> dict:
    """The 0600 file the configuration points into (issue #179)."""
    return yaml.safe_load(_secrets_path(tmp_path).read_text(encoding="utf-8"))


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
    # No secret in the configuration itself (issue #179): it names the place.
    assert raw["auth"]["admin_key_file"].endswith("#admin")
    assert "admin_key" not in raw["auth"]
    admin_key = _secrets(tmp_path)["admin"]
    assert len(admin_key) == 32
    assert _mode(_secrets_path(tmp_path)) == 0o600
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
    # Nothing to write under `replication` either: the second field carries the
    # same key, so an install without one has no section to fill (issue #159).
    assert "replication" not in raw
    cfg = load_config(_config_path(tmp_path))
    assert cfg.hosts[0].api_key == ""


def test_omlx_key_goes_into_defaults_and_is_never_echoed(tmp_path, capsys):
    port = closed_port()
    key = "sk-test-not-a-real-key"
    assert main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--omlx-key", key, "--no-check")) == 0

    raw = yaml.safe_load(_config_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["defaults"]["api_key_file"].endswith("#omlx.fleet")
    assert "api_key" not in raw["defaults"], "the key must not travel with the config"
    assert _secrets(tmp_path)["omlx"]["fleet"] == key
    assert load_config(_config_path(tmp_path)).hosts[0].api_key == key
    captured = capsys.readouterr()
    assert key not in captured.out
    assert key not in captured.err


def test_the_one_omlx_key_lands_in_both_fields(tmp_path):
    """Issue #159: one question, two fields — and the second one switches nothing on.

    `defaults.api_key` is the client key RIPPLE passes through; the per-host
    state sampling and the replication admin calls read `replication.admin_key`.
    An install that filled only the first was green in `doctor` and blind in the
    curves, so `init` writes the key under both names. `replication.enabled`
    keeps its default: the key alone must not turn replication on.
    """
    port = closed_port()
    key = "sk-omlx-159-test-key"
    assert main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--omlx-key", key, "--no-check")) == 0

    raw = yaml.safe_load(_config_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["defaults"]["api_key_file"].endswith("#omlx.fleet")
    assert raw["replication"]["admin_key_file"].endswith("#omlx.fleet")
    assert "admin_key" not in raw["replication"]
    assert "enabled" not in raw["replication"]
    assert _secrets(tmp_path)["omlx"]["fleet"] == key

    cfg = load_config(_config_path(tmp_path))
    assert cfg.hosts[0].api_key == key          # `defaults.api_key` lands on the host
    assert cfg.replication.admin_key == key
    assert cfg.replication.enabled is False
    # nothing warns about the section the install just wrote
    assert not [w for w in cfg.warnings if "admin_key" in w]


# -- the admin key: asked for, printed once when generated (issue #158) -----
def _admin_run(tmp_path: Path, port: int, *,
               typed: str = "") -> tuple[int, list[str], list[str]]:
    """One interactive run in `tmp_path`; `typed` answers the admin-key question.

    Returns `(exit code, every line init printed, every prompt it asked)`. The
    oMLX key is answered empty and the caller removes `$OMLX_API_KEY`, so the
    run cannot read the operator's real key.
    """
    lines: list[str] = []
    asked: list[str] = []

    def secret(prompt: str) -> str:
        asked.append(prompt)
        return typed if "admin key" in prompt else ""

    prompter = ConsolePrompter(input_fn=lambda _prompt: "", secret_fn=secret)
    code = run(Options(port=port, home_dir=tmp_path,
                       config_path=_config_path(tmp_path),
                       hosts=["http://127.0.0.1:1"], check=False),
               prompter, out=lines.append)
    return code, lines, asked


def test_a_generated_admin_key_is_printed_once_and_is_the_one_written(
        tmp_path, monkeypatch):
    """The install ends with the key the operator panels ask for in hand.

    Before #158 the transcript said "generated … not shown here", so the first
    thing a new operator did was open a mode-0600 file to copy a secret out of
    it — or conclude the install had not given them one.
    """
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    port = closed_port()
    code, lines, asked = _admin_run(tmp_path, port)
    assert code == 0

    admin_key = _secrets(tmp_path)["admin"]
    assert len(admin_key) == 32

    printed = "\n".join(lines)
    assert printed.count(admin_key) == 1
    assert f"  {admin_key}" in lines
    assert "RIPPLE admin key — shown once, store it now:" in lines
    # the question was asked, and an empty answer is what generates
    assert any("empty = generate a 32-hex one" in prompt for prompt in asked)


def test_a_typed_admin_key_is_used_verbatim_and_never_echoed(tmp_path, monkeypatch):
    """A short key is the operator's decision: reported, not judged, not shown.

    The running installation uses a 4-digit admin key on purpose, so `init` must
    not refuse one — and must not print it back, because the operator already
    knows it.
    """
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    port = closed_port()
    typed = "4711-ab"
    code, lines, _ = _admin_run(tmp_path, port, typed=typed)
    assert code == 0

    assert load_config(_config_path(tmp_path)).auth.admin_key == typed
    assert _secrets(tmp_path)["admin"] == typed
    assert "admin_key" not in yaml.safe_load(
        _config_path(tmp_path).read_text(encoding="utf-8"))["auth"]

    printed = "\n".join(lines)
    assert typed not in printed                      # never echoed back
    assert "7 character(s), used as given" in printed
    assert "shorter than the 32 hex" in printed      # one neutral note
    assert "shown once" in printed                   # …the client key still is


def test_yes_run_prints_one_generated_admin_key(tmp_path, capsys):
    """A scripted run generates and prints one too — no question, no silence."""
    port = closed_port()
    assert main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--no-check")) == 0
    out = capsys.readouterr().out

    admin_key = _secrets(tmp_path)["admin"]
    assert len(admin_key) == 32
    assert out.count(admin_key) == 1
    assert "RIPPLE admin key — shown once, store it now:" in out


def test_the_admin_key_lands_in_the_secret_file_and_nowhere_else(
        tmp_path, monkeypatch):
    """One file carries the secret, and it is the 0600 one (issue #179).

    The configuration is the file an operator pastes into a forum; the log is
    where a secret would outlive the terminal. Both must stay clean, and every
    other file under the home too.
    """
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    port = closed_port()
    code, _lines, _ = _admin_run(tmp_path, port)
    assert code == 0

    admin_key = _secrets(tmp_path)["admin"]
    log = tmp_path / "Library" / "Logs" / "ripple.log"
    assert not log.exists() or admin_key not in log.read_text(encoding="utf-8",
                                                             errors="replace")
    for path in tmp_path.rglob("*"):
        if not path.is_file() or path == _secrets_path(tmp_path):
            continue
        assert admin_key not in path.read_text(encoding="utf-8",
                                               errors="replace"), path

    # the view `/config` and `--json` show masks it, like the oMLX key
    assert "admin_key: set" in yaml.safe_dump(
        load_config(_config_path(tmp_path)).summary())


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
    first_admin = _secrets(tmp_path)["admin"]
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
    second_admin = _secrets(tmp_path)["admin"]
    assert second_admin != first_admin
    assert _mode(_keys_path(tmp_path)) == 0o600


# -- --force keeps what it replaces (issue #154) ----------------------------
def test_force_backs_up_the_config_and_the_key_store(tmp_path, capsys):
    """`--force` copies both files aside, prints both paths, keeps mode 0600.

    The operator runs `--force` when the machine is already in trouble — a port
    probe that lied, a key store that loaded empty. Replacing a 9 KB
    configuration with the 1.3 KB first-run document and the key store with a
    fresh one, with nothing left to restore, is the failure this pins down.
    """
    port = closed_port()
    argv = _argv(tmp_path, port, "--host", "http://127.0.0.1:1", "--no-check")
    assert main(argv) == 0
    config, keys = _config_path(tmp_path), _keys_path(tmp_path)
    first_config = config.read_bytes()
    first_keys = keys.read_bytes()
    # a fresh machine leaves no backup of a file that never existed
    assert list(tmp_path.glob("config.yaml.bak*")) == []
    assert list(keys.parent.glob("keys.yaml.bak*")) == []
    capsys.readouterr()

    assert main(argv + ["--force"]) == 0
    out = capsys.readouterr().out

    config_backups = sorted(tmp_path.glob("config.yaml.bak*"))
    keys_backups = sorted(keys.parent.glob("keys.yaml.bak*"))
    secrets_backups = sorted(keys.parent.glob("secrets.yaml.bak*"))
    assert len(config_backups) == 1
    assert len(keys_backups) == 1
    assert len(secrets_backups) == 1, "the secret file is backed up too (#179)"
    config_backup, keys_backup = config_backups[0], keys_backups[0]

    # byte for byte — a copy, not a re-render of the old file
    assert config_backup.read_bytes() == first_config
    assert keys_backup.read_bytes() == first_keys
    # …and a backup of secrets is as private as the original
    assert _mode(config_backup) == 0o600
    assert _mode(keys_backup) == 0o600
    assert _mode(secrets_backups[0]) == 0o600
    # the operator is told what to restore, one line each
    assert str(config_backup) in out
    assert str(keys_backup) in out

    # the name is the documented one, and `.gitignore` covers it
    assert re.fullmatch(r"config\.yaml\.bak-\d{8}-\d{6}", config_backup.name)
    assert re.fullmatch(r"keys\.yaml\.bak-\d{8}-\d{6}", keys_backup.name)

    # The configuration may now be byte-identical across two runs: nothing is
    # generated into it any more (issue #179) — the new admin key lands in the
    # secret file, which is therefore the file that must differ.
    assert _secrets(tmp_path)["admin"] != yaml.safe_load(
        secrets_backups[0].read_text(encoding="utf-8"))["admin"]
    assert keys.read_bytes() != first_keys
    assert [k.name for k in KeyStore(keys).list()] == ["first-client"]


def test_force_backs_up_whichever_file_exists(tmp_path, capsys):
    """The key store alone is backed up when the config is already gone.

    `--home-dir` moves the key store, so its backup has to land beside it and
    not beside the config — the path printed is the one the operator can use.
    """
    port = closed_port()
    argv = _argv(tmp_path, port, "--host", "http://127.0.0.1:1", "--no-check")
    assert main(argv) == 0
    keys = _keys_path(tmp_path)
    first_keys = keys.read_bytes()
    _config_path(tmp_path).unlink()
    capsys.readouterr()

    assert main(argv + ["--force"]) == 0
    out = capsys.readouterr().out

    assert list(tmp_path.glob("config.yaml.bak*")) == []
    keys_backups = sorted(keys.parent.glob("keys.yaml.bak*"))
    assert len(keys_backups) == 1
    assert keys_backups[0].parent == keys.parent
    assert keys_backups[0].read_bytes() == first_keys
    assert str(keys_backups[0]) in out


def test_force_never_collides_with_an_existing_backup(tmp_path):
    """A taken name is stepped past: two overwrites keep two copies.

    The stamp has one-second resolution, so two `--force` runs in the same
    second would otherwise copy onto the first backup — one copy for two
    overwrites, which is exactly the loss this issue is about.
    """
    from ripple.init import backup_path

    port = closed_port()
    argv = _argv(tmp_path, port, "--host", "http://127.0.0.1:1", "--no-check")
    assert main(argv) == 0
    config = _config_path(tmp_path)
    first = config.read_bytes()
    assert main(argv + ["--force"]) == 0
    second = config.read_bytes()

    # a backup with this second's stamp is already there
    taken = backup_path(config)
    taken.write_bytes(b"an earlier backup\n")

    assert main(argv + ["--force"]) == 0
    backups = list(tmp_path.glob("config.yaml.bak*"))
    assert len(backups) == 3            # first run's, the planted one, this run's
    assert taken.read_bytes() == b"an earlier backup\n"      # untouched
    bodies = [b.read_bytes() for b in backups]
    assert first in bodies and second in bodies


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


def _listener_that_took_a_connection(port: int) -> None:
    """Bind, listen, accept one connection, close both ends — then close the
    listener. The server side closes first, so the port is left in `TIME_WAIT`
    with nothing listening: exactly the state RIPPLE leaves behind when it is
    stopped while a client was connected (issue #151).

    `SO_REUSEADDR` on the listener is part of that state, not a convenience:
    uvicorn sets it on the socket it serves from, and the accepted socket
    inherits it. Linux only lets a new `SO_REUSEADDR` bind take a port in
    `TIME_WAIT` when the *old* socket carried the option too (the kernel
    compares both sides), so a listener without it leaves a port no server
    could take — a state RIPPLE cannot produce.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(("127.0.0.1", port))
        accepted, _ = listener.accept()
        accepted.close()                       # the server closes first
        client.close()
    finally:
        client.close()
        listener.close()


def test_a_just_freed_port_is_free(tmp_path):
    """Issue #151: a port a closed listener left in TIME_WAIT is not in use.

    `probe_port` said `free=False` here and `init` offered 8011 while launchd,
    the dashboard and every client still pointed at 8010. The plain bind really
    does fail (errno 48, 30-45 s measured) — that is why this case is worth a
    test of its own: `_can_bind` has to bind the way uvicorn does.
    """
    port = closed_port()
    _listener_that_took_a_connection(port)

    # The premise, asserted so this cannot pass vacuously: a plain bind — the
    # one that made `init` report "an unidentified process" — must still fail.
    with pytest.raises(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as plain:
            plain.bind(("127.0.0.1", port))

    state = probe_port(port)
    assert state.free is True, f"port {port} left in TIME_WAIT read as busy"
    assert state.pid is None                   # and nobody holds it

    # …and init keeps the port it was given instead of moving off it.
    assert main(_argv(tmp_path, port, "--host", "http://127.0.0.1:1",
                      "--no-check")) == 0
    assert load_config(_config_path(tmp_path)).listen_port == port


def test_a_moved_port_is_said_at_the_end_of_the_run(tmp_path):
    """Issue #151: the move is not only inside a question `--yes` answers.

    Under `--yes` the "Use port 8011 instead?" question is answered with its
    default, so the transcript used to read "RIPPLE is configured" with no sign
    that the number changed.
    """
    port = closed_port()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    lines: list[str] = []
    try:
        code = run(Options(yes=True, port=port, home_dir=tmp_path,
                           config_path=_config_path(tmp_path), check=False),
                   AutoPrompter(out=lines.append), out=lines.append)
    finally:
        listener.close()

    assert code == 0
    moved = load_config(_config_path(tmp_path)).listen_port
    assert moved != port
    assert any(f"listening on {moved}, not {port}" in line for line in lines)


def test_a_failed_bind_without_a_holder_does_not_name_a_process():
    """Issue #151: no `lsof` pid is not "an unidentified process".

    The wording is the second half of the bug — the operator read it as a
    running process holding 8010 while nothing was listening there at all.
    """
    text = PortState(port=8010, free=False).holder()
    assert "unidentified" not in text
    assert "no process this user can see" in text


# -- one flow, two front ends ----------------------------------------------
def _normalise(document: dict, home: Path) -> dict:
    """Replace the home prefix so two runs in two homes are comparable."""
    text = yaml.safe_dump(document, sort_keys=True)
    return yaml.safe_load(text.replace(str(home), "<home>"))


def test_interactive_and_yes_produce_the_same_config(tmp_path, monkeypatch):
    _fake_discovery(monkeypatch)
    # Inside 8010-8099: outside it the interactive run asks a confirmation the
    # `--yes` run does not ask, and the two question lists would differ (#172).
    port = _free_port_in_range()

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
    # The configuration is identical: the generated admin key lives in the
    # secret file now (issue #179), so nothing in this document can differ.
    assert yes_doc == con_doc
    assert _secrets(yes_home)["admin"] != _secrets(con_home)["admin"]


def test_discovered_node_id_reaches_the_config_and_the_allow_list_stays_open(
        tmp_path, monkeypatch):
    """The node's identity is written; an unreadable model list is not invented.

    `discover` reports ids only and this host answers nothing, so no
    `model_type` is known and the list stays empty — "everything this node
    offers" — with `init` saying so (issue #157). A concrete list written here
    would hide every model the operator adds later, silently.
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


# -- the models question: the default is the chat-capable ones (issue #157) --
CHAT = "Qwen3.5-9B-oQ4e-mtp"
UTILITY = [("MarkItDown", "markitdown"),
           ("Qwen3-Embedding-4B-4bit-DWQ", "embedding")]


def _node_offering(stub_models: list[StubModel]) -> StubUpstream:
    return StubUpstream(StubState(models=stub_models)).start()


def _chat_and_utility() -> list[StubModel]:
    return [StubModel(id=CHAT, model_type="llm", loaded=True)] + [
        StubModel(id=mid, model_type=kind) for mid, kind in UTILITY]


def test_the_default_allow_list_includes_the_utility_models(tmp_path):
    """An empty answer writes the node's whole list, embedding engine included.

    oMLX lists an embedding engine beside the chat models and answers
    `/v1/embeddings` for it (issue #193); a default that left it out made a
    fresh install refuse a call the node serves.
    """
    stub = _node_offering([StubModel(id="m1", model_type="llm", loaded=True),
                           StubModel(id="Qwen3-Embedding-4B-4bit-DWQ",
                                     model_type="embedding")])
    try:
        assert main(_argv(tmp_path, closed_port(), "--host", stub.url,
                          "--no-check")) == 0
    finally:
        stub.stop()

    raw = yaml.safe_load(_config_path(tmp_path).read_text(encoding="utf-8"))
    ids = ["m1", "Qwen3-Embedding-4B-4bit-DWQ"]
    assert raw["hosts"][0]["models"] == ids, raw["hosts"][0]


def test_the_line_names_the_utility_models_it_included(tmp_path, capsys):
    """The one line says how wide the list is and what it holds."""
    stub = _node_offering(_chat_and_utility())
    try:
        assert main(_argv(tmp_path, closed_port(), "--host", stub.url,
                          "--no-check")) == 0
    finally:
        stub.stop()

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "allow-list =" in line]
    assert len(lines) == 1, out
    line = lines[0]
    assert "all 3 offered model(s)" in line, line
    assert "including 2 that are not chat models" in line, line
    for mid, kind in UTILITY:
        assert f"{mid} ({kind})" in line, line
    assert "left out" not in line


def test_all_keeps_the_allow_list_empty_and_says_how_wide_it_is(tmp_path):
    """`all` is the documented way to route everything — and it is reported."""
    stub = _node_offering(_chat_and_utility())
    lines: list[str] = []
    try:
        prompter = ConsolePrompter(
            input_fn=lambda prompt: "all" if "models (comma" in prompt else "",
            secret_fn=lambda _prompt: "")
        assert run(Options(port=closed_port(), hosts=[stub.url],
                           home_dir=tmp_path, config_path=_config_path(tmp_path),
                           check=False), prompter, out=lines.append) == 0
    finally:
        stub.stop()

    raw = yaml.safe_load(_config_path(tmp_path).read_text(encoding="utf-8"))
    assert "models" not in raw["hosts"][0], raw["hosts"][0]

    line = next(line for line in lines if "allow-list empty" in line)
    assert "all 3 offered model(s)" in line, line
    assert "including 2 that are not chat models" in line, line
    for mid, kind in UTILITY:
        assert f"{mid} ({kind})" in line, line


def test_a_typed_list_is_written_exactly_as_typed(tmp_path):
    """Naming a utility model is the explicit opt-in; nothing is filtered."""
    stub = _node_offering(_chat_and_utility())
    try:
        prompter = ConsolePrompter(
            input_fn=lambda prompt: (f"MarkItDown, {CHAT}"
                                     if "models (comma" in prompt else ""),
            secret_fn=lambda _prompt: "")
        assert run(Options(port=closed_port(), hosts=[stub.url],
                           home_dir=tmp_path, config_path=_config_path(tmp_path),
                           check=False), prompter, out=lambda _l: None) == 0
    finally:
        stub.stop()

    raw = yaml.safe_load(_config_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["hosts"][0]["models"] == ["MarkItDown", CHAT]


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


# ---------------------------------------------------------------------------
# The port question (issue #166)
# ---------------------------------------------------------------------------
def test_the_port_answer_is_limited_to_what_a_normal_user_can_bind():
    """Below 1024 needs root, so the question must refuse it — not accept it and
    fail at start-up minutes later (measured: `80` passed, the start died with
    EACCES)."""
    from ripple.init import _parse_port

    assert _parse_port("8010") == 8010
    assert _parse_port(" 8099 ") == 8099
    assert _parse_port("1024") == 1024
    assert _parse_port("8080") == 8080          # unusual, but legitimate
    assert _parse_port("0") is None
    assert _parse_port("80") is None
    assert _parse_port("1023") is None
    assert _parse_port("65536") is None
    assert _parse_port("eight") is None


def test_each_way_to_answer_the_port_wrongly_gets_its_own_sentence():
    """One sentence for all three causes said nothing: `0` is a number."""
    from ripple.init import _port_complaint

    not_a_number = _port_complaint("eight")
    zero = _port_complaint("0")
    needs_root = _port_complaint("80")
    too_high = _port_complaint("70000")
    assert "not a number" in not_a_number and "8010-8099" in not_a_number
    assert "any free port" in zero and "8010-8099" in zero
    assert "needs root" in needs_root and "8010-8099" in needs_root
    assert "highest port" in too_high and "8010-8099" in too_high
    assert len({not_a_number, zero, needs_root, too_high}) == 4


def _free_port_in_range() -> int:
    """A free port inside 8010-8099, so no range question is asked."""
    from ripple.init import probe_port

    for candidate in range(8010, 8100):
        if probe_port(candidate).free:
            return candidate
    raise RuntimeError("no free port in 8010-8099")


def test_a_port_outside_the_documented_range_names_the_consequence():
    """9000 is legitimate — but the line says what it means (issue #172)."""
    from ripple.init import _outside_documented_range

    assert _outside_documented_range(8010) is None
    assert _outside_documented_range(8099) is None
    note = _outside_documented_range(9000)
    assert note and "outside 8010-8099" in note
    assert "follow this number" in note
    assert "written as asked" not in note


def test_a_port_outside_the_range_is_confirmed_and_a_no_asks_again():
    """A typo is the common cause, so the deviation is a decision (#172)."""
    from ripple.init import Options, _step_port

    free = _free_port_in_range()
    prompter = _ScriptedPrompter([str(free + 100), str(free)], [False])
    choice = _step_port(Options(), prompter, lambda _line: None)

    assert choice is not None and choice.port == free
    assert prompter.questions == ["Port", f"Use port {free + 100} anyway?", "Port"]


def test_the_port_flag_is_refused_with_the_same_sentence(tmp_path, capsys):
    """`--port 80` must not write a configuration that cannot bind."""
    from ripple import init as init_module

    assert init_module.main(["--yes", "--port", "80",
                             "--home-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "needs root" in err and "8010-8099" in err
    assert not (tmp_path / "config.yaml").exists()


# ---------------------------------------------------------------------------
# Typed addresses are probed, and the model count is not invented (#167, #168)
# ---------------------------------------------------------------------------
class _ScriptedPrompter:
    """Answers the questions in the order they are asked."""

    def __init__(self, texts, answers, secrets=()):
        self._texts = list(texts)
        self._answers = list(answers)
        self._secrets = list(secrets)
        self.questions = []

    def text(self, label, default=""):
        self.questions.append(label)
        return self._texts.pop(0) if self._texts else default

    def yes_no(self, label, default=True):
        self.questions.append(label)
        return self._answers.pop(0) if self._answers else default

    def secret(self, label, default=""):
        self.questions.append(label)
        return self._secrets.pop(0) if self._secrets else default


def test_the_key_is_asked_before_the_nodes_so_the_list_can_be_read(
        tmp_path, monkeypatch, capsys):
    """Issue #180: `model list not read (the key comes later)` was true for one
    step — the key was asked afterwards and the list read without a word.

    With the key answered first, the discovery line carries the count it really
    measured, and a key that does not fit is visible in that same line.
    """
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    stub = StubUpstream(StubState(models=[StubModel(id="m1", loaded=True),
                                          StubModel(id="m2", loaded=True)])).start()
    try:
        assert main(_argv(tmp_path, closed_port(), "--host", stub.url,
                          "--omlx-key", "k-180", "--no-check")) == 0
    finally:
        stub.stop()

    out = capsys.readouterr().out
    assert "2 model(s)" in out, out
    assert "model list not read" not in out, out
    # and the key question came before the node questions
    assert out.index("oMLX key: taken from --omlx-key") < out.index("name:"), out


def test_the_model_count_is_not_claimed_when_no_key_was_read_yet():
    """`0 model(s)` named a measurement nobody made: oMLX answers 401 without a
    key, and `init` asks for the key after the nodes (issue #168)."""
    from ripple.init import Node, _offer_line

    empty = Node(name="local", addresses=["http://192.0.2.5:8000"])
    line = _offer_line(empty, has_key=False)
    assert "0 model(s)" not in line
    assert "model list not read" in line and "the key comes later" in line
    assert _offer_line(empty, has_key=True) == "0 model(s) offered"
    full = Node(name="local", addresses=["http://192.0.2.5:8000"],
                models=["a", "b", "c"])
    assert _offer_line(full, has_key=False) == "3 model(s)"


def test_a_typed_address_that_answers_as_omlx_is_taken_without_a_question():
    """The probe is the one the automatic path uses (issue #167)."""
    from ripple.init import Options, _ask_checked_addresses

    stub = StubUpstream(StubState(models=[StubModel(id="m1", loaded=True)])).start()
    try:
        prompter = _ScriptedPrompter([stub.url], [])
        out = []
        nodes = _ask_checked_addresses(Options(), prompter, out.append)
    finally:
        stub.stop()
    assert [n.addresses for n in nodes] == [[stub.url]]
    # A name comes with it (issue #173): loopback is `local`, and an empty
    # answer to the name question then keeps it instead of writing `name: ''`.
    assert nodes[0].name == "local"
    # one question only: the address. Nothing to confirm.
    assert prompter.questions == ["oMLX address(es), comma separated"]
    assert any("oMLX answered" in line for line in out)


def test_a_typed_address_that_answers_nothing_is_asked_about_then_taken():
    """A node that is off today is legitimate — a typo is the common cause, so
    the answer is confirmed once instead of refused (issue #167)."""
    from ripple.init import Options, _ask_checked_addresses

    silent = f"http://127.0.0.1:{closed_port()}"
    prompter = _ScriptedPrompter([silent], [True])       # keep it anyway
    out = []
    nodes = _ask_checked_addresses(Options(), prompter, out.append)

    assert [n.addresses for n in nodes] == [[silent]]
    assert any("no answer (oMLX running?, port correct?)" in line for line in out)
    assert any("keep it anyway?" in q for q in prompter.questions)


def test_a_typed_address_that_answers_nothing_can_be_corrected():
    """Answering the question with "no" asks for the address again."""
    from ripple.init import Options, _ask_checked_addresses

    stub = StubUpstream(StubState(models=[StubModel(id="m1", loaded=True)])).start()
    try:
        silent = f"http://127.0.0.1:{closed_port()}"
        prompter = _ScriptedPrompter([silent, stub.url], [False])
        out = []
        nodes = _ask_checked_addresses(Options(), prompter, out.append)
    finally:
        stub.stop()

    assert [n.addresses for n in nodes] == [[stub.url]]
    assert prompter.questions.count("oMLX address(es), comma separated") == 2


def test_an_openai_compatible_stranger_is_asked_about_not_accepted():
    """A 401 on /v1/models is not proof of an oMLX (issue #167).

    Measured on this machine: the gateway on :8642 answers 401 on /v1/models and
    404 on the identity path, and discovery accepted it on the weaker evidence.
    `init` now asks, because oMLX answers /api/cluster/node_id without a key.
    """
    import http.server
    import threading

    from ripple.init import _classify_typed_addresses

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                # noqa: N802
            code = 401 if self.path.startswith("/v1/models") else 404
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "no key"}')

        def log_message(self, *args):                    # keep the test quiet
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        verdict = _classify_typed_addresses([url], "")[url][0]
    finally:
        server.shutdown()
        server.server_close()
    assert verdict == "unconfirmed"


def test_the_weight_question_says_what_the_number_does_and_insists():
    """`weight` is a word from the file, not from the operator's day (issue #169).

    Each wrong answer gets its own sentence, and a wrong answer asks again
    instead of being replaced by the default.
    """
    from ripple.init import _ask_weight, _weight_complaint

    # legal answers
    for good in ("1", "2", "20", " 7 "):
        assert _weight_complaint(good) is None
    # every illegal answer has its own sentence
    complaints = {bad: _weight_complaint(bad) for bad in
                  ("abc", "1.5", "0", "-3", "21")}
    assert "not a number" in complaints["abc"]
    assert "not a whole number" in complaints["1.5"]
    assert "out of the rotation" in complaints["0"]
    assert "out of the rotation" in complaints["-3"]
    assert "above 20" in complaints["21"]
    assert len({c for c in complaints.values() if c}) == 5

    prompter = _ScriptedPrompter(["abc", "0", "21", "1.5", "2"], [])
    assert _ask_weight(prompter, "node 1/1 weight", 1.0) == 2.0
    assert prompter.questions[0] == ("node 1/1 weight (relative: higher takes "
                                     "more new threads, 1-20)")
    assert len(prompter.questions) == 5


def test_the_weight_question_takes_enter_for_the_default():
    from ripple.init import _ask_weight

    prompter = _ScriptedPrompter([""], [])
    assert _ask_weight(prompter, "node 1/1 weight", 1.0) == 1.0


# ---------------------------------------------------------------------------
# The oMLX key is asked per node (#171)
# ---------------------------------------------------------------------------
def test_the_key_question_names_the_oMLX_panel_not_a_file_oMLX_writes(
        tmp_path, monkeypatch):
    """oMLX keeps the key in settings.json, not in `~/.omlx/api_key.txt` (#177).

    Verified on 23.11. against oMLX 0.8.3: the string appears in none of its
    binaries and the documentation names the admin panel. A stranger with two
    fresh Mac Studios was sent to a path that does not exist — twice.
    """
    from ripple.init import Options, _step_omlx_key

    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    prompter = _ScriptedPrompter([], [], secrets=["k1"])
    _step_omlx_key(Options(omlx_key=""), tmp_path, prompter, lambda _line: None)

    question = prompter.questions[-1]
    assert "admin panel" in question and "Settings" in question, question
    assert "api_key.txt" not in question, question


def test_a_key_file_that_exists_is_offered_as_a_copy(tmp_path, monkeypatch):
    """Our own machines have the file; it is offered, but named for what it is."""
    from ripple.init import Options, _step_omlx_key

    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    (tmp_path / ".omlx").mkdir()
    (tmp_path / ".omlx" / "api_key.txt").write_text("k-from-file\n", encoding="utf-8")

    prompter = _ScriptedPrompter([], [True])
    key = _step_omlx_key(Options(omlx_key=""), tmp_path, prompter, lambda _l: None)

    assert key == "k-from-file"
    offered = [q for q in prompter.questions if "api_key.txt" in q]
    assert offered, prompter.questions
    assert "copy" in offered[0], offered[0]


def test_each_node_can_have_its_own_key():
    """A fleet of machines with their own oMLX key (issue #171)."""
    from ripple.init import Node, Options, _step_node_keys

    # Closed ports: no node answers, so no key is refused and none is re-asked.
    nodes = [Node(name="A", addresses=["http://127.0.0.1:9"]),
             Node(name="B", addresses=["http://127.0.0.1:10"])]
    prompter = _ScriptedPrompter([], [False], secrets=["k1", "k2"])
    fleet = _step_node_keys(Options(omlx_key=""), Path("/tmp"), nodes, prompter,
                            lambda _line: None)

    assert fleet == "k1"
    assert [n.api_key for n in nodes] == ["k1", "k2"]
    assert any("same oMLX key as node 1?" in q for q in prompter.questions)


def test_the_first_key_is_the_default_for_the_rest():
    from ripple.init import Node, Options, _step_node_keys

    nodes = [Node(name="A", addresses=["http://127.0.0.1:9"]),
             Node(name="B", addresses=["http://127.0.0.1:10"])]
    prompter = _ScriptedPrompter([], [True], secrets=["k1"])
    fleet = _step_node_keys(Options(omlx_key=""), Path("/tmp"), nodes, prompter,
                            lambda _line: None)

    assert fleet == "k1"
    assert [n.api_key for n in nodes] == ["k1", "k1"]


def test_a_key_the_node_refuses_is_asked_for_again():
    """Measured on 23.11.: the second node answered 401 and stayed read-only."""
    import http.server
    import threading

    from ripple.init import Node, _retry_refused_key

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                # noqa: N802
            if self.headers.get("authorization") == "Bearer right":
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data": []}')
                return
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "API key required"}}')

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        node = Node(name="B", addresses=[url], api_key="wrong")
        prompter = _ScriptedPrompter([], [], secrets=["right"])
        out = []
        _retry_refused_key(node, prompter, out.append)
        assert node.api_key == "right"
        assert any("refused" in line for line in out)
        assert prompter.questions == ["node B oMLX API key (empty = leave it "
                                      "read-only)"]
    finally:
        server.shutdown()
        server.server_close()


def test_no_key_means_read_only_and_is_not_asked_about_again():
    """`empty = read-only` is an answer — a 401 is then expected (issue #171)."""
    from ripple.init import Node, _retry_refused_key

    node = Node(name="B", addresses=["http://127.0.0.1:9"], api_key="")
    prompter = _ScriptedPrompter([], [], secrets=["unused"])
    _retry_refused_key(node, prompter, lambda _line: None)
    assert prompter.questions == []
    assert node.api_key == ""


def test_a_node_that_differs_keeps_its_key_in_the_file(tmp_path):
    from ripple.init import Node, config_document

    nodes = [Node(name="A", addresses=["http://127.0.0.1:8000"], api_key="k1"),
             Node(name="B", addresses=["http://192.0.2.9:8000"], api_key="k2")]
    doc = config_document(port=8010, nodes=nodes, api_key="k1",
                          admin_key="a" * 32, home=tmp_path)
    hosts = {h["name"]: h for h in doc["hosts"]}
    assert doc["defaults"]["api_key_file"].endswith("#omlx.fleet")
    assert "api_key" not in hosts["A"]
    assert hosts["B"]["api_key_file"].endswith("#omlx.nodes.B")
    assert "api_key" not in hosts["B"], "the key stays in the secret file"

def test_the_config_lands_in_the_ripple_home_not_in_the_checkout(tmp_path, monkeypatch):
    """Issue #183: `./install.sh` runs inside the clone, so `Path.cwd()` put a
    stranger's config.yaml into the checkout — where `ripple` started anywhere
    else does not look, and where the next re-clone loses it."""
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("OMLX_API_KEY", raising=False)

    # no --config: the default is what this test is about
    argv = ["--yes", "--port", str(closed_port()), "--home-dir", str(home),
            "--no-check"]
    assert main(argv) == 0

    assert (home / ".ripple" / "config.yaml").is_file()
    assert not (checkout / "config.yaml").exists(), "the checkout is not a store"


def test_the_admin_key_line_names_the_secret_file(tmp_path, capsys):
    """#179 moved the key out of `config.yaml`; the line kept pointing at
    `auth.admin_key` for a release, which is a file that no longer has it."""
    home = tmp_path
    assert main(_argv(home, closed_port(), "--no-check")) == 0

    out = capsys.readouterr().out
    assert "secrets.yaml at admin" in out, out
    assert "in config.yaml at auth.admin_key" not in out, out

def test_a_network_sweep_asks_about_every_node_it_finds(tmp_path, monkeypatch):
    """Issue #187: entering `192.0.2.0/24` at the site question reads as
    "look there" — so the sweep is offered, and each node it finds is asked
    name, weight and models like any other."""
    import sys
    import types

    _fake_discovery(monkeypatch)
    stub = StubUpstream(StubState(models=[StubModel(id="m3-model", loaded=True)])).start()

    # The sweep: one address, the stub — a second machine with its own node_id.
    found = types.SimpleNamespace(address=stub.url, node_id="node-m3",
                                  models=["m3-model"], name="", key_ok=True,
                                  error=None)

    async def fake_scan(cidr, **kwargs):
        # `scan_network_detailed` is a coroutine: `init` runs it via asyncio.run
        return [types.SimpleNamespace(found=found, error=None)]

    # On the module `init` will import from — `_fake_discovery` put its own
    # stand-in into `sys.modules`, and that is the object to patch.
    monkeypatch.setattr(sys.modules["ripple.discover"], "scan_network_detailed",
                        fake_scan, raising=False)

    seen = {"name": 0}

    def answer(prompt: str) -> str:
        if "Scan " in prompt:
            return "y"
        if prompt.startswith("Site networks"):
            return "127.0.0.1/32"
        if prompt.startswith("node ") and " name" in prompt:
            seen["name"] += 1
            return "alpha" if seen["name"] == 1 else "beta"
        if prompt.startswith("node ") and " weight" in prompt:
            return "3" if seen["name"] == 1 else "2"
        return ""

    prompter = ConsolePrompter(input_fn=answer, secret_fn=lambda _p: "")
    home = tmp_path
    lines = []
    try:
        code = run(Options(port=_free_port_in_range(), home_dir=home,
                           config_path=home / "config.yaml", check=False),
                   prompter, out=lines.append)
    finally:
        stub.stop()
    assert code == 0

    doc = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    names = [h["name"] for h in doc["hosts"]]
    assert names == ["alpha", "beta"], (names, lines, prompter.questions)
    m3 = doc["hosts"][1]
    assert m3["weight"] == 2.0
    assert m3["addresses"] == [stub.url]
    assert m3["node_id"] == "node-m3"
    # the loopback node keeps its own address and weight
    assert doc["hosts"][0]["addresses"] == ["http://127.0.0.1:8000"]
    assert doc["hosts"][0]["weight"] == 3.0
    assert any("found " in line and stub.url in line for line in lines), lines


def test_a_swept_node_already_in_the_fleet_is_not_asked_twice(tmp_path, monkeypatch):
    """The same oMLX answers on every address it listens on — this machine
    included. `node_id` is what says "that one is already in the fleet"."""
    import sys
    import types

    _fake_discovery(monkeypatch)
    # A sweep that returns the loopback node under a LAN address, same id.
    same = types.SimpleNamespace(address="http://192.0.2.7:8000", node_id="node-id-1",
                                 models=["m1"], name="", key_ok=True, error=None)

    async def fake_scan(cidr, **kw):
        return [types.SimpleNamespace(found=same, error=None)]

    monkeypatch.setattr(sys.modules["ripple.discover"], "scan_network_detailed",
                        fake_scan, raising=False)

    asked = []

    def answer(prompt: str) -> str:
        asked.append(prompt)
        if "Scan " in prompt:
            return "y"
        if prompt.startswith("Site networks"):
            return "192.0.2.0/24"
        return ""

    prompter = ConsolePrompter(input_fn=answer, secret_fn=lambda _p: "")
    home = tmp_path
    assert run(Options(port=_free_port_in_range(), home_dir=home,
                       config_path=home / "config.yaml", check=False),
               prompter, out=lambda _line: None) == 0

    doc = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert len(doc["hosts"]) == 1, doc["hosts"]
    assert doc["hosts"][0]["addresses"] == ["http://127.0.0.1:8000"]
    assert not any("192.0.2.7" in q for q in asked), asked
