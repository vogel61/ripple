"""The CLI surface: the first-run words, the service words (#121), the old flags.

`ripple init`, `ripple discover`, `ripple doctor` and the five service words
`start|status|shutdown|restart|update` are dispatched by hand in
`__main__.main()` so that `ripple --config x`, `ripple --check` and
`ripple --version` keep behaving exactly as they did. These tests pin both
halves of that: the dispatch, and the fact that a flag is never mistaken for a
subcommand.

The service tests never touch a real port or a real launchd job. Every command
takes its label from `RIPPLE_LABEL` and its port from `RIPPLE_PORT` — that is
the whole point of the contract — and the tests replace the four things that
would reach outside the process (`job_state`, `listener_pid`, `healthz`,
`_load`) with fakes. Nothing here can stop, start or signal the operator's
service.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ripple import __main__ as cli
from ripple import service
from ripple.config import Config


# --- dispatch ---------------------------------------------------------------

def test_the_first_run_words_are_wired():
    assert cli.FIRST_RUN_WORDS == ("init", "discover", "doctor")
    assert cli.SERVICE_WORDS == ("start", "status", "shutdown", "restart", "update")
    assert cli.SUBCOMMANDS == cli.FIRST_RUN_WORDS + cli.SERVICE_WORDS


@pytest.mark.parametrize("word", ["init", "discover", "doctor"])
def test_each_first_run_word_reaches_its_module_with_the_rest_of_argv(monkeypatch, word):
    module = __import__(f"ripple.{word}", fromlist=["main"])
    seen: dict = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(module, "main", fake_main)
    assert cli.main([word, "--flag", "value"]) == 7
    assert seen["argv"] == ["--flag", "value"]


@pytest.mark.parametrize("argv", [[], ["start"], ["status"], ["shutdown"],
                                  ["restart"], ["update"]])
def test_every_service_word_reaches_the_service_module(monkeypatch, argv):
    # `ripple` with no word at all is `start` — the same handler, an empty tail.
    seen: dict = {}

    def fake_main(rest=None):
        seen["argv"] = rest
        return 7

    monkeypatch.setattr(service, "main", fake_main)
    assert cli.main(argv) == 7
    assert seen["argv"] == argv


def test_a_flag_is_not_a_subcommand():
    # `--check` on a missing config must take the ordinary path: ConfigError →
    # exit 2. If a flag were ever routed to a subcommand this would not happen.
    assert cli.main(["--config", "/nonexistent/ripple.yaml", "--check"]) == 2


def test_the_module_entry_points_exist():
    for word in cli.FIRST_RUN_WORDS:
        module = __import__(f"ripple.{word}", fromlist=["main"])
        assert callable(module.main)
    for word in cli.SERVICE_WORDS:
        assert callable(service.HANDLERS[word])


def test_help_of_a_subcommand_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["discover", "--help"])
    assert exc.value.code == 0
    assert "--scan" in capsys.readouterr().out


def test_help_says_which_word_starts_and_which_reports(capsys):
    # #121: `ripple` (start) and `ripple status` must not be confusable.
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "ripple              start here, or report that it already runs" in out
    assert "ripple status       running/stopped, pid, port, version, uptime, log path" in out
    assert "RIPPLE_LABEL" in out and "RIPPLE_PORT" in out
    assert "RIPPLE_UPDATE_SUITE" in out


# --- the contract: label and port from the environment ----------------------

@pytest.fixture(autouse=True)
def clean_service_env(monkeypatch):
    for name in ("RIPPLE_LABEL", "RIPPLE_PORT", "RIPPLE_CONFIG", "RIPPLE_PLIST",
                 "RIPPLE_UPDATE_SUITE", "RIPPLE_WAIT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def job(loaded=False, pid=None, state=None, raw=""):
    return {"loaded": loaded, "pid": pid, "state": state, "raw": raw}


def completed(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=["git"], returncode=rc, stdout=out, stderr=err)


@pytest.fixture
def cfg(tmp_path):
    """A config that names a port and a log, without reading the operator's."""
    c = Config()
    c.listen_port = 8011
    c.log_file = str(tmp_path / "ripple.log")
    return c


@pytest.fixture
def wired(monkeypatch, cfg, tmp_path):
    """The service commands, wired to fakes: no launchd, no socket, no config."""
    state = {"job": job(), "listener": None, "health": None,
             "command": "python -m ripple --config x", "terminated": [],
             "booted": [], "spawned": [], "started_job": []}
    monkeypatch.setattr(service, "_load", lambda: cfg)
    # `status` and `shutdown` read the config leniently (a stopped service must
    # report without one), so that path needs the fake config too.
    monkeypatch.setattr(service, "_config_or_none", lambda: cfg)
    monkeypatch.setattr(service, "job_state", lambda lab=None: state["job"])
    monkeypatch.setattr(service, "listener_pid", lambda port: state["listener"])
    monkeypatch.setattr(service, "healthz", lambda port, timeout=2.0: state["health"])
    monkeypatch.setattr(service, "_command_of", lambda pid: state["command"])
    monkeypatch.setattr(service, "plist_path", lambda lab=None: tmp_path / "ai.ripple-test.plist")
    monkeypatch.setattr(service, "stop_job",
                        lambda lab=None: state["booted"].append(lab) or f"removed the launchd job {lab}")
    monkeypatch.setattr(service, "wait_port_free",
                        lambda port, seconds=15.0: state["listener"])
    monkeypatch.setattr(service, "wait_job_gone",
                        lambda lab=None, seconds=15.0: True)
    def fake_terminate(pid, port, seconds=15.0):
        # A real SIGTERM frees the port; the fake has to do the same, or the
        # post-stop verification would always see a holder.
        state["terminated"].append(pid)
        state["listener"] = None

    monkeypatch.setattr(service, "terminate", fake_terminate)
    monkeypatch.setattr(service, "spawn_background",
                        lambda path, log: state["spawned"].append(path) or 4242)
    monkeypatch.setattr(service, "_launchctl",
                        lambda *a: state["started_job"].append(a) or completed(0))
    return state


def test_the_production_defaults_are_ai_ripple_on_8010():
    assert service.label() == "ai.ripple"
    assert service.resolve_port(Config()) == 8010
    assert service.plist_path("ai.ripple").name == "ai.ripple.plist"


def test_the_environment_points_the_commands_somewhere_else(monkeypatch):
    monkeypatch.setenv("RIPPLE_LABEL", "ai.ripple-test")
    monkeypatch.setenv("RIPPLE_PORT", "8011")
    monkeypatch.setenv("RIPPLE_PLIST", "/tmp/ai.ripple-test.plist")
    assert service.label() == "ai.ripple-test"
    assert service.resolve_port(Config()) == 8011
    assert str(service.plist_path()) == "/tmp/ai.ripple-test.plist"


def test_the_port_comes_from_the_config_when_the_environment_is_silent():
    cfg = Config()
    cfg.listen_port = 8123
    assert service.resolve_port(cfg) == 8123


@pytest.mark.parametrize("value", ["not-a-port", "0", "70000"])
def test_a_bad_port_is_a_named_failure(monkeypatch, value):
    monkeypatch.setenv("RIPPLE_PORT", value)
    with pytest.raises(service.ServiceError) as exc:
        service.resolve_port(Config())
    assert exc.value.code == service.EXIT_CONFIG
    assert "RIPPLE_PORT" in exc.value.message


def test_the_suite_command_is_injectable(monkeypatch):
    assert service.suite_command()[:2] == [sys.executable, "-m"]
    monkeypatch.setenv("RIPPLE_UPDATE_SUITE", "true")
    assert service.suite_command() == ["true"]


# --- start ------------------------------------------------------------------

def test_start_reports_a_loaded_job_and_exits_zero(wired, monkeypatch, capsys):
    wired["job"] = job(loaded=True, pid=49841)
    monkeypatch.setattr(cli, "run_foreground",
                        lambda cfg: pytest.fail("must not start a second instance"))
    assert service.cmd_start([]) == 0
    out = capsys.readouterr().out
    assert "already running as the launchd job ai.ripple" in out
    assert "49841" in out and "8011" in out


def test_start_runs_in_the_foreground_when_the_port_is_free(wired, monkeypatch, capsys):
    seen: dict = {}

    def fake_foreground(cfg):
        seen["cfg"] = cfg
        return 0

    monkeypatch.setattr(cli, "run_foreground", fake_foreground)
    assert service.cmd_start([]) == 0
    assert seen["cfg"].listen_port == 8011
    assert "starting in this terminal" in capsys.readouterr().out


def test_start_names_the_holder_of_a_busy_port_and_fails(wired):
    wired["listener"] = 4242
    wired["command"] = "nginx: master process"
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_start([])
    assert exc.value.code == service.EXIT_PORT_BUSY
    assert "4242" in exc.value.message and "nginx" in exc.value.message
    assert "next:" in exc.value.message


def test_start_reports_a_manual_instance_instead_of_colliding(wired, capsys):
    # A RIPPLE instance that launchd does not own is still a running service:
    # case (a), exit 0 — not case (c), which is for a stranger on the port.
    wired["listener"] = 5150
    wired["command"] = "python -m ripple --config config.yaml"
    assert service.cmd_start([]) == 0
    assert "already running on port 8011 (PID 5150" in capsys.readouterr().out


# --- status -----------------------------------------------------------------

def test_status_reads_the_service_first(wired, capsys):
    wired["job"] = job(loaded=True, pid=49841)
    wired["listener"] = 49841
    wired["health"] = {"version": "9.9.9", "uptime_seconds": 4559.5,
                       "build": {"commit": "abc1234", "dirty": False}}
    assert service.cmd_status([]) == 0
    out = capsys.readouterr().out
    assert "state: running" in out
    assert "source: /healthz on port 8011" in out
    assert "version: 9.9.9" in out
    assert "commit: abc1234" in out
    assert "uptime: 4559 s (1 h 15 min)" in out
    assert "log: " in out and "ripple.log" in out
    assert "pid: 49841" in out


def test_status_of_a_stopped_service_exits_zero(wired, capsys):
    assert service.cmd_status([]) == 0
    out = capsys.readouterr().out
    assert "state: stopped" in out
    assert "is not loaded" in out


def test_status_of_a_loaded_job_that_does_not_answer_exits_nonzero(wired, capsys):
    wired["job"] = job(loaded=True, pid=49841)
    assert service.cmd_status([]) == 1
    out = capsys.readouterr().out
    assert "state: not answering" in out
    assert "next: ripple restart" in out


# --- shutdown ---------------------------------------------------------------

def test_shutdown_boots_out_the_job_and_says_so(wired, capsys):
    wired["job"] = job(loaded=True, pid=49841)
    assert service.cmd_shutdown([]) == 0
    assert wired["booted"] == ["ai.ripple"]
    out = capsys.readouterr().out
    assert "removed the launchd job ai.ripple" in out
    assert "port 8011 is free" in out


def test_shutdown_of_a_stopped_service_exits_zero(wired, capsys):
    assert service.cmd_shutdown([]) == 0
    assert "not running" in capsys.readouterr().out


def test_shutdown_signals_the_listener_when_there_is_no_job(wired, capsys):
    wired["listener"] = 5150
    wired["command"] = "python -m ripple --config config.yaml"
    assert service.cmd_shutdown([]) == 0
    assert wired["terminated"] == [5150]
    assert "not a launchd job" in capsys.readouterr().out


def test_shutdown_leaves_a_stranger_on_the_port_alone(wired):
    wired["listener"] = 4242
    wired["command"] = "nginx: master process"
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_shutdown([])
    assert exc.value.code == service.EXIT_NOT_STOPPED
    assert wired["terminated"] == []
    assert "not a RIPPLE instance" in exc.value.message


# --- restart ----------------------------------------------------------------

def test_restart_waits_for_the_dashboard_and_prints_the_seconds(wired, monkeypatch, capsys):
    wired["job"] = job(loaded=True, pid=49841)
    monkeypatch.setattr(service, "wait_for_dashboard", lambda port, seconds: 1.25)
    assert service.cmd_restart([]) == 0
    out = capsys.readouterr().out
    assert wired["booted"] == ["ai.ripple"]
    assert "ready: the dashboard answered after 1.2 s" in out


def test_restart_fails_with_the_log_tail_when_nothing_answers(wired, monkeypatch):
    monkeypatch.setattr(service, "wait_for_dashboard", lambda port, seconds: None)
    monkeypatch.setattr(service, "tail", lambda path, lines=15: "line A\nline B")
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_restart([])
    assert exc.value.code == service.EXIT_NO_ANSWER
    assert "line A" in exc.value.message


def test_restart_refuses_a_stranger_on_the_port(wired):
    wired["listener"] = 4242
    wired["command"] = "nginx: master process"
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_restart([])
    assert exc.value.code == service.EXIT_NOT_STOPPED
    assert wired["terminated"] == []


# --- update -----------------------------------------------------------------

@pytest.fixture
def repo(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(service, "REPO", root)
    return root


def test_update_refuses_a_dirty_tree(repo, wired, monkeypatch):
    def fake_git(*args):
        if args[:2] == ("status", "--porcelain"):
            return completed(0, " M ripple/proxy.py\n")
        return completed(0)

    monkeypatch.setattr(service, "_git", fake_git)
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_update([])
    assert exc.value.code == service.EXIT_REFUSED
    assert "uncommitted changes" in exc.value.message
    assert "ripple/proxy.py" in exc.value.message


def test_update_does_not_restart_on_a_red_suite(repo, wired, monkeypatch):
    calls = {"restart": 0}

    def fake_git(*args):
        if args[:2] == ("status", "--porcelain"):
            return completed(0, "")
        if args[:3] == ("rev-parse", "--abbrev-ref", "--symbolic-full-name"):
            return completed(0, "origin/main\n")
        if args[:2] == ("rev-parse", "HEAD"):
            return completed(0, "bbbb2222\n" if calls.get("pulled") else "aaaa1111\n")
        if args[0] == "pull":
            calls["pulled"] = True
            return completed(0)
        return completed(0)

    monkeypatch.setattr(service, "_git", fake_git)
    monkeypatch.setattr(service, "cmd_restart",
                        lambda argv: calls.__setitem__("restart", calls["restart"] + 1) or 0)
    monkeypatch.setattr(service, "suite_command", lambda: ["python", "-m", "pytest", "-q"])
    monkeypatch.setattr(service.subprocess, "run",
                        lambda *a, **kw: completed(1, "2 failed, 961 passed in 245.1s\n"))
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_update([])
    assert exc.value.code == service.EXIT_SUITE_RED
    assert "2 failed, 961 passed" in exc.value.message
    assert "not restarting" in exc.value.message
    assert calls["restart"] == 0


def test_update_restarts_only_after_a_green_suite(repo, wired, monkeypatch, capsys):
    calls = {"restart": 0, "versions": iter(["0.1.1 (aaaa111)", "0.1.2 (bbbb222)"])}

    def fake_git(*args):
        if args[:2] == ("status", "--porcelain"):
            return completed(0, "")
        if args[:3] == ("rev-parse", "--abbrev-ref", "--symbolic-full-name"):
            return completed(0, "origin/main\n")
        if args[:2] == ("rev-parse", "HEAD"):
            return completed(0, "bbbb2222\n" if calls.get("pulled") else "aaaa1111\n")
        if args[0] == "pull":
            calls["pulled"] = True
            return completed(0)
        return completed(0)

    monkeypatch.setattr(service, "_git", fake_git)
    monkeypatch.setattr(service, "suite_command", lambda: ["python", "-m", "pytest", "-q"])
    monkeypatch.setattr(service.subprocess, "run",
                        lambda *a, **kw: completed(0, "961 passed in 245.1s\n"))
    monkeypatch.setattr(service, "_version_now", lambda port, fallback: next(calls["versions"]))

    def fake_restart(argv):
        calls["restart"] += 1
        return 0

    monkeypatch.setattr(service, "cmd_restart", fake_restart)
    assert service.cmd_update([]) == 0
    assert calls["restart"] == 1
    out = capsys.readouterr().out
    assert "version before: 0.1.1 (aaaa111)" in out
    assert "version after:  0.1.2 (bbbb222)" in out
    assert "suite: green" in out


def test_update_says_so_when_there_is_nothing_to_pull(repo, wired, monkeypatch, capsys):
    def fake_git(*args):
        if args[:2] == ("status", "--porcelain"):
            return completed(0, "")
        if args[:3] == ("rev-parse", "--abbrev-ref", "--symbolic-full-name"):
            return completed(128, "", "fatal: no upstream configured\n")
        return completed(0, "aaaa1111\n")

    monkeypatch.setattr(service, "_git", fake_git)
    monkeypatch.setattr(service, "cmd_restart",
                        lambda argv: pytest.fail("nothing changed: must not restart"))
    assert service.cmd_update([]) == 0
    out = capsys.readouterr().out
    assert "has no upstream" in out
    assert "already the newest commit" in out


# --- autostart: ripple -perm / ripple shutdown -perm (#122) -----------------

@pytest.fixture
def checkout(tmp_path):
    """A checkout with the real plist template in it, and no .venv."""
    src = Path(__file__).resolve().parent.parent / "deploy" / "ai.ripple.plist"
    repo = tmp_path / "checkout"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "ai.ripple.plist").write_text(src.read_text(encoding="utf-8"),
                                                     encoding="utf-8")
    return repo


def test_the_plist_names_this_checkout_and_this_config(checkout):
    # #122: rendered, never copied — the paths are the ones the command runs from.
    python = str(checkout / ".venv" / "bin" / "python")
    text = service.render_plist(repo=checkout, lab="ai.ripple-test",
                               cfg_path=checkout / "config.yaml", python=python,
                               home=Path("/Users/someone"))
    assert f"<string>{python}</string>" in text
    assert f"<string>{checkout}/config.yaml</string>" in text
    assert f"<string>{checkout}</string>" in text            # WorkingDirectory
    assert "<string>ai.ripple-test</string>" in text
    assert "__REPO__" not in text and "__HOME__" not in text and "__LABEL__" not in text
    assert text.index("<key>ProgramArguments</key>") < text.index("<key>WorkingDirectory</key>")


def test_a_config_outside_the_checkout_is_the_one_the_job_gets(checkout):
    text = service.render_plist(repo=checkout, lab="ai.ripple-test",
                               cfg_path=Path("/tmp/ripple-8011.yaml"),
                               python="/usr/bin/python3", home=Path("/tmp"))
    assert "<string>/tmp/ripple-8011.yaml</string>" in text
    assert f"<string>{checkout}/config.yaml</string>" not in text


def test_a_template_without_the_argument_lines_is_refused(checkout):
    template = checkout / "deploy" / "ai.ripple.plist"
    template.write_text("<plist><dict><key>Label</key><string>__LABEL__</string>"
                        "</dict></plist>", encoding="utf-8")
    with pytest.raises(service.ServiceError) as exc:
        service.render_plist(repo=checkout, lab="ai.ripple-test",
                             cfg_path=checkout / "config.yaml", python="/usr/bin/python3")
    assert exc.value.code == service.EXIT_REFUSED
    assert "deploy/ai.ripple.plist" in exc.value.message


@pytest.fixture
def perm(monkeypatch, cfg, tmp_path):
    """The autostart commands, wired to fakes: no launchd, no socket, no plist."""
    state = {"job": job(), "listener": None, "health": None,
             "command": "python -m ripple --config config.yaml",
             "launchctl": [], "bootstrapped": 0, "terminated": [], "plutil": []}
    monkeypatch.setattr(service, "_load", lambda path=None: cfg)
    monkeypatch.setattr(service, "_config_or_none", lambda: cfg)
    monkeypatch.setattr(service, "job_state", lambda lab=None: state["job"])
    monkeypatch.setattr(service, "listener_pid", lambda port: state["listener"])
    monkeypatch.setattr(service, "healthz", lambda port, timeout=2.0: state["health"])
    monkeypatch.setattr(service, "_command_of", lambda pid: state["command"])
    monkeypatch.setattr(service, "plist_path", lambda lab=None: tmp_path / "ai.ripple-test.plist")
    monkeypatch.setattr(service, "api_key_from_file", lambda: "test-key-123")
    monkeypatch.setattr(service, "wait_job_gone", lambda lab=None, seconds=15.0: True)
    monkeypatch.setattr(service, "wait_port_free", lambda port, seconds=15.0: state["listener"])
    monkeypatch.setattr(service, "wait_for_healthz", lambda port, seconds: 1.5)

    def fake_launchctl(*args):
        state["launchctl"].append(args)
        if args[0] == "bootstrap":
            # launchd reports the job as loaded; the pid comes with it.
            state["bootstrapped"] += 1
            state["job"] = job(loaded=True, pid=49841)
        if args[0] == "bootout":
            state["job"] = job()
        return completed(0)

    def fake_plutil(*args):
        state["plutil"].append(args)
        if args[0] == "-extract":
            return completed(0, "test-key-123\n")
        return completed(0)

    def fake_terminate(pid, port, seconds=15.0):
        state["terminated"].append(pid)
        state["listener"] = None

    monkeypatch.setattr(service, "_launchctl", fake_launchctl)
    monkeypatch.setattr(service, "_plutil", fake_plutil)
    monkeypatch.setattr(service, "terminate", fake_terminate)
    return state


def test_perm_installs_the_job_and_waits_until_healthz_answers(perm, capsys):
    assert service.cmd_autostart([]) == 0
    plist = service.plist_path()
    assert plist.is_file()
    assert oct(plist.stat().st_mode & 0o777) == "0o600"
    text = plist.read_text(encoding="utf-8")
    assert "<string>ai.ripple</string>" in text               # the label
    # Issue #185: the configuration names its key itself (api_key_file), so the
    # plist carries none — and plutil is never asked to write one.
    assert "OMLX_API_KEY" not in text
    assert "<key>PATH</key>" in text                          # the env block survives
    assert not [call for call in perm["plutil"] if call[0] == "-replace"]
    assert perm["bootstrapped"] == 1
    assert ("bootstrap", service.domain(), str(plist)) in perm["launchctl"]
    out = capsys.readouterr().out
    assert f"autostart: on ({service.label()}, {service.REPO})" in out
    assert "verified: launchctl print" in out and "49841" in out
    assert "answered after 1.5 s" in out
    assert "the job carries no oMLX key" in out


def test_perm_twice_is_idempotent(perm, capsys):
    assert service.cmd_autostart([]) == 0
    perm["health"] = {"version": "0.1.1"}                    # the job answers now
    capsys.readouterr()
    assert service.cmd_autostart([]) == 0
    assert perm["bootstrapped"] == 1                          # not bootstrapped again
    out = capsys.readouterr().out
    assert f"autostart: on ({service.label()}, {service.REPO})" in out
    assert "already installed and running" in out


def test_perm_does_not_kill_a_manual_instance(perm, capsys):
    # The constraint, not an accident: the manual process keeps the port, the
    # plist is installed, launchd takes over at the next login.
    perm["listener"] = 5150
    perm["health"] = {"version": "0.1.1"}
    assert service.cmd_autostart([]) == 0
    assert perm["terminated"] == []
    assert perm["bootstrapped"] == 0                          # nothing to race onto 8011
    assert service.plist_path().is_file()
    out = capsys.readouterr().out
    assert "autostart: on" in out
    assert "manual instance on port 8011 (PID 5150) keeps running" in out
    assert "next login" in out


def test_perm_refuses_a_stranger_on_the_port_and_installs_nothing(perm):
    perm["listener"] = 4242
    perm["command"] = "nginx: master process"
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_autostart([])
    assert exc.value.code == service.EXIT_PORT_BUSY
    assert "nginx" in exc.value.message and "nothing was installed" in exc.value.message
    assert not service.plist_path().exists()


def test_perm_without_a_config_installs_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("RIPPLE_CONFIG", str(tmp_path / "nope.yaml"))
    monkeypatch.setattr(service, "plist_path", lambda lab=None: tmp_path / "ai.ripple-test.plist")
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_autostart([])
    assert exc.value.code == service.EXIT_CONFIG
    assert "configuration file not found" in exc.value.message
    assert "next:" in exc.value.message
    assert not (tmp_path / "ai.ripple-test.plist").exists()


def test_perm_without_the_key_file_installs_nothing_when_the_config_asks_for_it(
        perm, monkeypatch, tmp_path):
    """A configuration that says `${OMLX_API_KEY}` needs the key in the plist,
    and launchd cannot read files — so a missing file is a refusal (issue #153)."""
    asking = tmp_path / "asking.yaml"
    asking.write_text("defaults:\n  api_key: ${OMLX_API_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(service, "job_config_path", lambda: asking)
    monkeypatch.setattr(service, "api_key_from_file", lambda: None)
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_autostart([])
    assert exc.value.code == service.EXIT_CONFIG
    assert str(Path.home() / ".omlx" / "api_key.txt") in exc.value.message
    assert "next:" in exc.value.message
    assert not service.plist_path().exists()


def test_perm_without_a_key_file_is_fine_when_the_config_names_its_own(
        perm, monkeypatch, tmp_path, capsys):
    """The normal case since #179: `api_key_file` in the configuration, no key
    in the plist, no `~/.omlx/api_key.txt` needed (issue #185)."""
    naming = tmp_path / "naming.yaml"
    naming.write_text("defaults:\n  api_key_file: ~/.ripple/secrets.yaml#omlx.fleet\n",
                      encoding="utf-8")
    monkeypatch.setattr(service, "job_config_path", lambda: naming)
    monkeypatch.setattr(service, "api_key_from_file", lambda: None)
    assert service.cmd_autostart([]) == 0
    text = service.plist_path().read_text(encoding="utf-8")
    assert "OMLX_API_KEY" not in text
    assert "the job carries no oMLX key" in capsys.readouterr().out


def test_perm_writes_the_key_when_the_config_asks_for_the_variable(
        perm, monkeypatch, tmp_path):
    asking = tmp_path / "asking.yaml"
    asking.write_text("defaults:\n  api_key: ${OMLX_API_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(service, "job_config_path", lambda: asking)
    assert service.cmd_autostart([]) == 0
    replace = [call for call in perm["plutil"] if call[0] == "-replace"]
    assert replace and "test-key-123" in replace[0]
    assert any(call[0] == "-extract" for call in perm["plutil"])


def test_shutdown_perm_stops_the_job_and_removes_it(perm, capsys):
    perm["job"] = job(loaded=True, pid=49841)
    service.plist_path().write_text("<plist/>", encoding="utf-8")
    assert service.cmd_shutdown(["-perm"]) == 0
    assert ("bootout", f"{service.domain()}/ai.ripple") in perm["launchctl"]
    assert not service.plist_path().exists()
    out = capsys.readouterr().out
    assert f"autostart: off ({service.label()})" in out
    assert f"stopped the launchd job {service.label()}" in out
    assert "Could not find service" in out or "verified: launchctl print" in out


def test_shutdown_perm_stops_the_listener_when_there_is_no_job(perm, capsys):
    perm["listener"] = 5150
    service.plist_path().write_text("<plist/>", encoding="utf-8")
    assert service.cmd_shutdown(["-perm"]) == 0
    assert perm["terminated"] == [5150]
    assert not service.plist_path().exists()
    out = capsys.readouterr().out
    assert "autostart: off" in out
    assert "stopped PID 5150 (not under launchd)" in out


def test_shutdown_perm_when_nothing_is_installed_says_so(perm, capsys):
    assert service.cmd_shutdown(["-perm"]) == 0
    out = capsys.readouterr().out
    assert "autostart: off" in out
    assert "stopped nothing" in out
    assert "no plist was installed" in out


def test_shutdown_perm_removes_nothing_when_the_stop_fails(perm, monkeypatch):
    # Stop first, remove second — in that order, or a job is gone while its
    # process still holds the port and nothing knows how to stop it.
    perm["listener"] = 4242
    perm["command"] = "nginx: master process"
    service.plist_path().write_text("<plist/>", encoding="utf-8")
    with pytest.raises(service.ServiceError) as exc:
        service.cmd_shutdown(["-perm"])
    assert exc.value.code == service.EXIT_NOT_STOPPED
    assert service.plist_path().is_file()
    assert perm["terminated"] == []


@pytest.mark.parametrize("word", ["start", "status", "restart", "update"])
def test_perm_with_another_word_is_a_usage_error(word, capsys):
    assert service.main([word, "-perm"]) == service.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "-perm" in err and "autostart" in err
    assert "shutdown -perm" in err


def test_perm_alone_is_routed_to_the_autostart_command(monkeypatch):
    seen: dict = {}

    def fake_main(rest=None):
        seen["argv"] = rest
        return 7

    monkeypatch.setattr(service, "main", fake_main)
    assert cli.main(["-perm"]) == 7
    assert seen["argv"] == ["-perm"]
    assert cli.main(["shutdown", "-perm"]) == 7
    assert seen["argv"] == ["shutdown", "-perm"]


def test_help_makes_the_four_forms_unmistakable(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "ripple -perm        autostart ON" in out
    assert "ripple shutdown -perm" in out
    assert "-perm alone is ON; -perm after `shutdown` is OFF" in out


# --- the installer ----------------------------------------------------------

def test_the_installer_writes_a_working_wrapper(tmp_path):
    # Real subprocess, real wrapper, real `--version` — but into a throwaway
    # directory, so nothing on the operator's PATH changes.
    repo = Path(__file__).resolve().parent.parent
    installer = repo / "deploy" / "install-cli.command"
    assert installer.is_file() and os.access(installer, os.X_OK)
    env = dict(os.environ, RIPPLE_CLI_DIR=str(tmp_path / "bin"),
               RIPPLE_PYTHON=sys.executable, HOME=str(tmp_path))
    first = subprocess.run([str(installer), "--quiet"], env=env,
                           capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stdout + first.stderr
    wrapper = tmp_path / "bin" / "ripple"
    assert wrapper.is_file() and os.access(wrapper, os.X_OK)
    text = wrapper.read_text(encoding="utf-8")
    assert str(repo) in text and sys.executable in text

    second = subprocess.run([str(installer), "--quiet"], env=env,
                            capture_output=True, text=True, timeout=120)
    assert second.returncode == 0
    assert "already installed and up to date" in second.stdout

    version = subprocess.run([str(wrapper), "--version"], env=env,
                             capture_output=True, text=True, timeout=120)
    assert version.returncode == 0, version.stdout + version.stderr
    assert version.stdout.startswith("RIPPLE ")

def test_the_service_commands_read_what_init_wrote(tmp_path, monkeypatch):
    """Issue #186: after #183 the config lives in `~/.ripple`, but the service
    commands still resolved `<repo>/config.yaml` — `ripple -perm` from the home
    directory died on a file the install never wrote."""
    from ripple import service

    monkeypatch.delenv("RIPPLE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ripple").mkdir()
    written = tmp_path / ".ripple" / "config.yaml"
    written.write_text("listen_port: 8010\n", encoding="utf-8")

    assert service.config_path() == written
    # the plist needs an absolute path: a job has no working directory
    assert service.job_config_path() == written
    assert service.job_config_path().is_absolute()


def test_ripple_config_still_wins(tmp_path, monkeypatch):
    """The override is older than the default and keeps its meaning."""
    from ripple import service

    monkeypatch.setenv("RIPPLE_CONFIG", str(tmp_path / "somewhere.yaml"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert service.config_path() == tmp_path / "somewhere.yaml"
    assert service.job_config_path() == tmp_path / "somewhere.yaml"
