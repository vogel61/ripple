"""Restart from the page: the endpoint, the supervisor guard, the drain gate (#22).

The mechanism is deliberately small, and every part of it is a decision that can
go wrong in a way nobody notices until the operator clicks:

- **The exit is the restart.** `launchctl` is never called. `deploy/ai.ripple.plist`
  has `KeepAlive → SuccessfulExit → false`, so launchd starts the job again when
  it ends *unsuccessfully* — which is why the exit code is not zero, and why a
  test pins the two together.
- **202 before the end.** An endpoint that kills its own process cannot answer
  afterwards, so the response leaves first and the exit waits a second.
- **Only under a supervisor.** Started from a Terminal there is no
  `XPC_SERVICE_NAME` in the environment and nobody would bring the process back;
  the endpoint refuses and the process keeps running.
- **A label is not a supervisor (#27).** A process started by *another* launchd
  job inherits that job's label — measured: the agent gateway starts RIPPLE with
  `XPC_SERVICE_NAME=ai.hermes.gateway`. So the label is only the first of three
  facts: launchd has to be the parent (`ppid == 1`) and `launchctl print` has to
  name this pid. Each failure has its own sentence, because the operator reads it
  in the dialog.
- **`force` is an acknowledgement, not a command.** Nothing is executed, so there
  is no string to interpolate into a shell — the tests below assert both halves:
  the code contains no shell call, and a body carrying one changes nothing. The
  one process the guard starts is the read-only `launchctl print` query, and it
  is pinned to be exactly that.

The exit itself is never executed here: `ripple.proxy._hard_exit` is replaced, so
a test observes *that* the process would have ended, with which code, and in
which order relative to the answer.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import plistlib
import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest

import ripple.proxy as proxy
from ripple.config import PlacementConfig
from ripple.proxy import (INHERITED_LABEL_REASON, RESTART_DELAY_SECONDS,
                          RESTART_EXIT_CODE, UNOWNED_LABEL_REASON,
                          UNSUPERVISED_REASON)

from tests.support import (StubModel, bearer, chat_body, host_config, keyed_config,
                           wait_until)

ADMIN = bearer("test-admin-key")
MODEL = "m1"
PLIST = Path(__file__).resolve().parents[1] / "deploy" / "ai.ripple.plist"


def _cfg(tmp_path, hosts):
    cfg = keyed_config(hosts, str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(maintenance_file=str(tmp_path / "maintenance.yaml"),
                                    model_policy_file=str(tmp_path / "models.yaml"))
    return cfg


def _models() -> list[StubModel]:
    return [StubModel(id=MODEL, estimated_size=100_000_000_000)]


@pytest.fixture(autouse=True)
def exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the exit instead of taking it — the test process must survive.

    Autouse on purpose: a test that schedules a restart and forgets to await it
    would otherwise call the real `os._exit` from a leftover task and take the
    whole pytest process down with exit code 75, which reads like a crashed suite
    rather than a broken test.
    """
    codes: list[int] = []
    monkeypatch.setattr(proxy, "_hard_exit", codes.append)
    return codes


@pytest.fixture(autouse=True)
def ownership_memo() -> Iterator[dict[tuple[int, str], bool]]:
    """A proven ownership is remembered for the life of the process (#27).

    Autouse so the memo cannot leak between tests: the pytest process keeps one
    pid, so a fake `launchctl` that proves ownership for `(pid, "ai.ripple")` in
    one test would otherwise answer for every later test with the same key.
    """
    proxy._owns_pid_memo.clear()
    yield proxy._owns_pid_memo
    proxy._owns_pid_memo.clear()


@pytest.fixture
def supervised(monkeypatch: pytest.MonkeyPatch) -> str:
    """The two OS facts a launchd job has: its label, launchd as parent, ownership.

    The seam is the *facts*, not the rule: the pytest process is never launchd's
    child and no launchd job owns it, so `_parent_pid` and `_launchd_owns_pid` —
    the two thin probes into the OS — are replaced. `supervision_state` still
    decides, and it reads no configuration, so there is no flag a production
    process could set to skip the check.
    """
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.ripple")
    monkeypatch.setattr(proxy, "_parent_pid", lambda: 1)
    monkeypatch.setattr(proxy, "_launchd_owns_pid", lambda label: True)
    return "ai.ripple"


@pytest.fixture
def inherited(monkeypatch: pytest.MonkeyPatch) -> str:
    """The measured gateway case: another job's label, that job's pid as parent."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway")
    return "ai.hermes.gateway"


@pytest.fixture
def unsupervised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)


async def _restart(harness, **kwargs):
    return await harness.client.post("/admin/restart", headers=ADMIN, **kwargs)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
async def test_the_endpoints_need_the_admin_key(stub_factory, ripple_factory,
                                                tmp_path, supervised, exits):
    """401, like the rest of /admin — the lock's 403 stays where it is."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    assert (await harness.client.post("/admin/restart")).status_code == 401
    assert (await harness.client.get("/admin/restart")).status_code == 401
    wrong = await harness.client.post("/admin/restart", headers=bearer("wrong"))
    assert wrong.status_code == 401
    assert wrong.json()["error"]["message"] == "admin key required"

    # A denied call schedules nothing: the process is still here, and it stays.
    await asyncio.sleep(0.05)
    assert exits == []
    assert harness.ripple._restart_task is None
    assert (await harness.healthz())["ok"] is True


async def test_the_restart_endpoint_is_the_only_place_with_a_403(ripple_factory,
                                                                 stub_factory, tmp_path):
    """The contract for the page: /admin/restart answers 401, not 403.

    The model lock uses 403 on purpose (the state is forbidden, not the
    credential missing). A dashboard that has to tell "unlock" from "refuse"
    depends on the two not being mixed.
    """
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    assert (await harness.client.post("/admin/restart")).status_code == 401
    assert (await harness.client.post("/admin/models/x/lock")).status_code == 403


# ---------------------------------------------------------------------------
# The supervised path
# ---------------------------------------------------------------------------
async def test_a_supervised_instance_answers_202_and_then_ends_the_process(
        stub_factory, ripple_factory, tmp_path, supervised, exits):
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.01

    resp = await _restart(harness)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["supervised"] is True
    assert body["supervisor"] == "ai.ripple"
    assert body["inflight"] == 0
    assert body["pending"] is True

    await wait_until(lambda: bool(exits), timeout=2.0)
    assert exits == [RESTART_EXIT_CODE]


async def test_the_202_leaves_before_the_process_ends(stub_factory, ripple_factory,
                                                      tmp_path, supervised, exits):
    """The whole reason for the delay: the answer exists before the process does not."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.2

    resp = await _restart(harness)
    assert resp.status_code == 202
    # Answered, and nothing has ended yet.
    await asyncio.sleep(0.05)
    assert exits == []
    assert harness.ripple.restart_state()["pending"] is True
    # Then it does.
    await wait_until(lambda: bool(exits), timeout=2.0)
    assert exits == [RESTART_EXIT_CODE]


async def test_a_second_click_does_not_schedule_a_second_exit(
        stub_factory, ripple_factory, tmp_path, supervised, exits, caplog):
    """The dialog may be clicked twice; the process must still end once."""
    caplog.set_level(logging.WARNING, logger="ripple")
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.05

    first = await _restart(harness)
    second = await _restart(harness)
    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["pending"] is True

    await wait_until(lambda: bool(exits), timeout=2.0)
    await asyncio.sleep(0.05)
    assert exits == [RESTART_EXIT_CODE]
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("restart_requested")]
    assert len(lines) == 1, lines


async def test_the_log_line_has_the_agreed_shape(stub_factory, ripple_factory,
                                                 tmp_path, supervised, exits, caplog):
    """`restart_requested by=dashboard client=… inflight=…` — one line, parseable."""
    caplog.set_level(logging.WARNING, logger="ripple")
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.01

    await _restart(harness)
    await wait_until(lambda: bool(exits), timeout=2.0)

    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("restart_requested")]
    assert lines == ["restart_requested by=dashboard client=127.0.0.1 inflight=0"], lines


# ---------------------------------------------------------------------------
# The unsupervised path
# ---------------------------------------------------------------------------
async def test_a_terminal_start_refuses_and_keeps_running(
        stub_factory, ripple_factory, tmp_path, unsupervised, exits, caplog):
    """No supervisor, no restart — and a reason instead of a dead process."""
    caplog.set_level(logging.WARNING, logger="ripple")
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.01

    resp = await _restart(harness)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"]["code"] == "restart_unsupervised"
    assert body["error"]["message"] == UNSUPERVISED_REASON
    assert "XPC_SERVICE_NAME" in body["error"]["message"]
    assert body["supervised"] is False

    # `force` is not a way around the guard: ending a process nobody restarts is
    # not a stronger confirmation, it is a mistake with extra steps.
    forced = await harness.client.post("/admin/restart?force=1", headers=ADMIN)
    assert forced.status_code == 503

    await asyncio.sleep(0.05)
    assert exits == []
    assert harness.ripple._restart_task is None
    assert (await harness.healthz())["ok"] is True
    assert any(r.getMessage().startswith("restart_refused reason=unsupervised")
               for r in caplog.records)


async def test_the_guard_state_is_readable_before_the_first_click(
        stub_factory, ripple_factory, tmp_path, supervised, exits):
    """The page disables the control with a reason, not with a guess."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    assert (await harness.client.get("/admin/restart", headers=ADMIN)).json() == {
        "supervised": True, "supervisor": "ai.ripple", "reason": None,
        "reason_code": None, "reason_params": None,
        "inflight": 0, "hosts": [], "pending": False,
        "delay_seconds": RESTART_DELAY_SECONDS,
    }
    assert exits == []


async def test_the_guard_state_names_the_missing_supervisor(
        stub_factory, ripple_factory, tmp_path, unsupervised, exits):
    """The same GET from a Terminal start: `supervised: false` and the reason."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    state = (await harness.client.get("/admin/restart", headers=ADMIN)).json()
    assert state["supervised"] is False
    assert state["supervisor"] is None
    assert state["reason"] == UNSUPERVISED_REASON
    assert state["pending"] is False
    assert exits == []


async def test_healthz_carries_the_state_without_a_key(
        stub_factory, ripple_factory, tmp_path, unsupervised, exits):
    """The control is disabled *before* an unlock — so /healthz has to say it."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    state = (await harness.healthz())["restart"]
    assert state["supervised"] is False
    assert state["supervisor"] is None
    assert state["reason"] == UNSUPERVISED_REASON
    assert state["inflight"] == 0
    assert state["pending"] is False
    assert exits == []


# ---------------------------------------------------------------------------
# The drain gate
# ---------------------------------------------------------------------------
async def test_requests_in_flight_need_the_second_confirmation(
        stub_factory, ripple_factory, tmp_path, supervised, exits, caplog):
    """The first dialog warns; the restart needs the "trotzdem" acknowledgement."""
    caplog.set_level(logging.WARNING, logger="ripple")
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.01
    harness.host("A").inflight = 1              # a stream is generating right now

    first = await _restart(harness)
    assert first.status_code == 409, first.text
    body = first.json()
    assert body["error"]["code"] == "restart_inflight"
    assert body["inflight"] == 1 and body["hosts"] == ["A"]
    message = body["error"]["message"]
    # The text names the streaming request — the operator's own session is one.
    assert "in flight" in message and "A" in message and "force=1" in message
    assert "streaming" in message

    await asyncio.sleep(0.05)
    assert exits == []                          # refused: nothing was scheduled
    assert harness.ripple._restart_task is None
    assert any(r.getMessage().startswith("restart_refused reason=inflight")
               for r in caplog.records)

    second = await harness.client.post("/admin/restart?force=1", headers=ADMIN)
    assert second.status_code == 202, second.text
    assert second.json()["inflight"] == 1
    await wait_until(lambda: bool(exits), timeout=2.0)
    assert exits == [RESTART_EXIT_CODE]
    assert [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("restart_requested")] == \
        ["restart_requested by=dashboard client=127.0.0.1 inflight=1"]


async def test_the_acknowledgement_is_read_from_the_body_too(
        stub_factory, ripple_factory, tmp_path, supervised, exits):
    """The dashboard posts JSON; `force: true` there means the same as `?force=1`."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.01
    harness.host("A").inflight = 1

    resp = await harness.client.post("/admin/restart", headers=ADMIN,
                                     json={"force": True})
    assert resp.status_code == 202, resp.text
    await wait_until(lambda: bool(exits), timeout=2.0)
    assert exits == [RESTART_EXIT_CODE]


async def test_the_drain_state_counts_the_whole_fleet(stub_factory, ripple_factory,
                                                      tmp_path, supervised, exits):
    """`inflight` is the fleet sum, and `hosts` names where the work is."""
    a = stub_factory(models=_models())
    b = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url),
                                                   host_config("B", b.url)]))
    harness.host("A").inflight = 1
    harness.host("B").inflight = 2

    state = harness.ripple.restart_state()
    assert state["inflight"] == 3
    assert state["hosts"] == ["A", "B"]
    assert (await harness.healthz())["restart"]["inflight"] == 3

    # The same number the page already has from `hosts[].inflight` — one source.
    health = await harness.healthz()
    assert sum(h["inflight"] for h in health["hosts"]) == state["inflight"]

    assert (await _restart(harness)).status_code == 409
    assert exits == []


async def test_a_drained_fleet_needs_no_acknowledgement(
        stub_factory, ripple_factory, tmp_path, supervised, exits):
    """After the stream ends the ordinary click is enough again."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.01
    harness.host("A").inflight = 1
    assert (await _restart(harness)).status_code == 409

    harness.host("A").inflight = 0
    resp = await _restart(harness)
    assert resp.status_code == 202, resp.text
    assert resp.json()["inflight"] == 0
    # Let the scheduled exit run while the recorder is still in place.
    await wait_until(lambda: bool(exits), timeout=2.0)


# ---------------------------------------------------------------------------
# No shell, no command
# ---------------------------------------------------------------------------
async def test_nothing_the_operator_sends_is_executed(stub_factory, ripple_factory,
                                                      tmp_path, supervised, exits):
    """A body full of commands is ignored: the endpoint reads `force` and nothing else."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.ripple.restart_delay = 0.01

    resp = await harness.client.post(
        "/admin/restart", headers=ADMIN,
        json={"force": True, "command": "touch /tmp/ripple-pwned",
              "cmd": "rm -rf /", "shell": "bash -c id", "args": ["-k"]})
    assert resp.status_code == 202, resp.text
    await wait_until(lambda: bool(exits), timeout=2.0)
    # The only effect is the exit itself.
    assert exits == [RESTART_EXIT_CODE]
    assert not Path("/tmp/ripple-pwned").exists()


def test_the_module_never_shells_out():
    """A static guard: nothing here builds a command line out of a string.

    `restart` is an exit status, not a command line. Since #27 the guard does
    start exactly one process — the read-only `launchctl print` query — so the
    rule is now precise instead of absolute: no shell, and `subprocess` only in
    that one function. A `shell=True` or an `os.system` would turn the operator's
    body (or an environment label) into code, which is the thing this pins.
    """
    source = Path(proxy.__file__).read_text(encoding="utf-8")
    for forbidden in ("shell=True", "os.system", "os.popen", "os.exec", "os.spawn"):
        assert forbidden not in source, f"{forbidden} in ripple/proxy.py"
    uses = [line.strip() for line in source.splitlines()
            if "subprocess" in line and line.strip() != "import subprocess"
            and not line.strip().startswith("#")]
    allowed = [line.strip()
               for line in inspect.getsource(proxy._query_launchd_owns_pid).splitlines()
               if "subprocess" in line]
    assert uses == allowed, uses


def _code_of(function) -> str:
    """The source of a function without its docstring — prose is not a command."""
    return inspect.getsource(function).split('"""')[-1]


def test_the_only_command_the_guard_runs_is_a_read_only_query():
    """`launchctl print` — the query verb, an argv list, a parsed answer.

    Measured 15.11.2026: `launchctl print gui/501/ai.ripple` answers with the job
    dictionary, and a label nobody owns answers rc 113 (`Could not find service
    …`). Both are reads. Nothing in this function may load, unload, kickstart or
    stop a job: the restart is the exit, and the query is what proves that the
    exit will be noticed.
    """
    source = _code_of(proxy._query_launchd_owns_pid)
    assert "subprocess.run(" in source
    assert "shell" not in source
    assert '"print"' in source
    for verb in ("kickstart", "bootout", "bootstrap", "unload", "load", "stop",
                 "kill", "submit", "enable", "disable", "remove"):
        assert verb not in source, verb


# ---------------------------------------------------------------------------
# The contract with launchd
# ---------------------------------------------------------------------------
def test_the_exit_code_is_one_that_launchd_restarts():
    """`KeepAlive → SuccessfulExit → false` restarts on a *failure* only.

    A zero exit would be read as "finished on purpose" and the service would stay
    down after the click — the one failure mode the operator would blame on the
    button. Both halves are pinned here because they only work together.
    """
    with PLIST.open("rb") as fh:
        job = plistlib.load(fh)
    assert job["Label"] == "ai.ripple"
    assert job["KeepAlive"] == {"SuccessfulExit": False}
    assert RESTART_EXIT_CODE != 0
    assert RESTART_DELAY_SECONDS > 0


def test_the_supervisor_marker_is_read_from_the_environment(
        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert proxy._supervisor_marker() is None
    monkeypatch.setenv("XPC_SERVICE_NAME", "  ")
    assert proxy._supervisor_marker() is None      # blank is not a supervisor
    # What macOS's libxpc writes into every process that is *not* a launchd job.
    # Measured on this machine: a Terminal-started python sees `0`, the running
    # service sees `ai.ripple`. Treating `0` as a label would have made the
    # guard useless in exactly the case it exists for.
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    assert proxy._supervisor_marker() is None
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.ripple")
    assert proxy._supervisor_marker() == "ai.ripple"


async def test_the_unsupervised_refusal_survives_the_value_macos_writes(
        stub_factory, ripple_factory, tmp_path, monkeypatch, exits):
    """`XPC_SERVICE_NAME=0` is a Terminal start, so it must still be refused."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    assert (await harness.healthz())["restart"]["supervised"] is False
    assert (await _restart(harness)).status_code == 503
    assert exits == []


# ---------------------------------------------------------------------------
# The restart does not disturb anything else
# ---------------------------------------------------------------------------
async def test_a_refused_restart_leaves_the_fleet_serving(stub_factory, ripple_factory,
                                                          tmp_path, unsupervised, exits):
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    client = bearer(harness.ripple.keys.create("client", "interactive").key)

    assert (await _restart(harness)).status_code == 503
    resp = await harness.ok(chat_body(MODEL, "still there?", turns=2), headers=client)
    assert resp.headers["x-ripple-host"] == "A"
    assert (await harness.stats())["requests_total"] >= 1


# ---------------------------------------------------------------------------
# The supervisor is proven, not inherited (#27)
#
# The label in the environment is a *claim*. It becomes a fact only when launchd
# is the parent and the job of that name owns this pid. The tests below produce
# the three failures separately, because the operator gets a different sentence
# for each one and the whole point of the guard is that it never says "yes" on
# evidence it does not have.
# ---------------------------------------------------------------------------
def _job_dict(label: str, pid: int) -> str:
    """`launchctl print` output, abridged from what was measured on 15.11.2026.

    The real answer for `gui/501/ai.ripple` is 1840 bytes; the shape that matters
    here is the single-tab indentation of the job's own fields and the `pid = …`
    line among them.
    """
    return (
        f"gui/{os.getuid()}/{label} = {{\n"
        "\tactive count = 1\n"
        f"\tpath = /Users/vogel/Library/LaunchAgents/{label}.plist\n"
        "\ttype = LaunchAgent\n"
        "\tstate = running\n"
        "\n"
        "\tprogram = /Users/vogel/Projects/ripple/.venv/bin/python\n"
        "\targuments = {\n"
        "\t\t/Users/vogel/Projects/ripple/.venv/bin/python\n"
        "\t\t-m\n"
        "\t\tripple\n"
        "\t}\n"
        "\n"
        "\tendpoints = {\n"
        "\t\t\"gui/501\" = {\n"
        "\t\t\thandler = 0x1\n"
        "\t\t\tstate = active\n"
        "\t\t}\n"
        "\t}\n"
        "\n"
        f"\tpid = {pid}\n"
        "\tstate = running\n"
        "}\n"
    )


def _fake_launchctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                    stdout: str = "", code: int = 0) -> Path:
    """A `launchctl` on PATH that answers from a file and logs every call.

    The production job on :8010 is never touched by any of this: the guard is
    exercised against a shell script in a temp directory, which is also how the
    failure answers (`rc 113`, "Could not find service …") are produced without
    depending on which jobs happen to exist on this machine.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    out = tmp_path / "print.out"
    out.write_text(stdout, encoding="utf-8")
    calls = tmp_path / "calls.log"
    tool = bin_dir / "launchctl"
    tool.write_text(
        "#!/bin/sh\n"
        f"printf 'x' >> {shlex.quote(str(calls))}\n"
        f"cat {shlex.quote(str(out))}\n"
        f"exit {code}\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return calls


async def test_a_label_inherited_from_another_job_is_refused(
        stub_factory, ripple_factory, tmp_path, inherited, exits, caplog):
    """The measured gateway case: `ai.hermes.gateway` and a ppid that is not 1.

    Nothing is patched here. The label is in the environment and the parent is
    whatever runs pytest — never launchd — which is exactly the shape of the
    process the agent gateway starts. Before #27 that process answered 202 and
    ended itself with nothing left to start it again.
    """
    caplog.set_level(logging.WARNING, logger="ripple")
    assert os.getppid() != proxy.LAUNCHD_PID, "pytest runs under launchd?"
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    resp = await _restart(harness)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"]["code"] == "restart_unsupervised"
    message = body["error"]["message"]
    assert message == INHERITED_LABEL_REASON.format(label="ai.hermes.gateway",
                                                   ppid=os.getppid())
    assert "ai.hermes.gateway" in message
    assert "inherited that label" in message
    assert "does not own" not in message          # that is the *other* refusal
    assert body["supervised"] is False
    assert body["supervisor"] is None

    # The page reads the same sentence from /healthz, before anyone unlocks.
    assert (await harness.healthz())["restart"]["reason"] == message

    forced = await harness.client.post("/admin/restart?force=1", headers=ADMIN)
    assert forced.status_code == 503
    await asyncio.sleep(0.05)
    assert exits == []
    assert harness.ripple._restart_task is None
    assert (await harness.healthz())["ok"] is True
    assert any(r.getMessage().startswith("restart_refused reason=unsupervised "
                                        "cause=inherited") for r in caplog.records)


def test_an_inherited_label_is_settled_before_launchctl_is_asked(inherited):
    """The parent pid decides it: the label is not even looked up."""
    asked: list[str] = []

    def owns(label: str) -> bool:
        asked.append(label)
        return True

    state = proxy.supervision_state(ppid=4242, owns=owns)
    assert asked == []
    assert state == {"supervised": False, "supervisor": None, "cause": "inherited",
                     "reason": INHERITED_LABEL_REASON.format(
                         label="ai.hermes.gateway", ppid=4242),
                     "reason_code": "inherited_label",
                     "reason_params": {"label": "ai.hermes.gateway", "ppid": 4242}}


def test_a_label_that_does_not_own_this_pid_is_refused(
        monkeypatch: pytest.MonkeyPatch):
    """Right label, launchd as parent, but the job names another pid."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.ripple")
    state = proxy.supervision_state(ppid=1, owns=lambda label: False)

    assert state["supervised"] is False
    assert state["supervisor"] is None
    assert state["cause"] == "unowned"
    assert state["reason"] == UNOWNED_LABEL_REASON.format(
        label="ai.ripple", pid=os.getpid(), uid=os.getuid())
    assert "does not own this process" in state["reason"]
    assert f"pid {os.getpid()}" in state["reason"]


async def test_the_ownership_refusal_reaches_the_dialog(
        monkeypatch: pytest.MonkeyPatch, stub_factory, ripple_factory, tmp_path, exits):
    """A label that launchd does not confirm must not look like a supervisor."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.ripple")
    monkeypatch.setattr(proxy, "_parent_pid", lambda: 1)
    monkeypatch.setattr(proxy, "_launchd_owns_pid", lambda label: False)
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    resp = await _restart(harness)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["message"].startswith(
        "RIPPLE carries the launchd label `ai.ripple`, but that job does not own")
    assert resp.json()["supervised"] is False
    assert exits == []


def test_the_three_refusals_are_distinguishable():
    """Each sentence names its own missing fact, and all three name the way out."""
    no_label = UNSUPERVISED_REASON
    inherited = INHERITED_LABEL_REASON.format(label="ai.hermes.gateway", ppid=99)
    unowned = UNOWNED_LABEL_REASON.format(label="ai.ripple", pid=7, uid=501)

    assert len({no_label, inherited, unowned}) == 3
    assert "no launchd label" in no_label
    assert "inherited that label" in inherited
    assert "ai.hermes.gateway" in inherited and "ppid 99" in inherited
    assert "does not own this process" in unowned and "pid 7" in unowned
    for reason in (no_label, inherited, unowned):
        assert "refuses to end itself" in reason
        assert reason.endswith("to use this control.")


def test_a_launchd_owned_label_is_the_only_supervised_state(
        monkeypatch: pytest.MonkeyPatch):
    """All three facts present: the label is reported as the supervisor."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.ripple")
    asked: list[str] = []

    def owns(label: str) -> bool:
        asked.append(label)
        return True

    state = proxy.supervision_state(ppid=proxy.LAUNCHD_PID, owns=owns)
    assert state == {"supervised": True, "supervisor": "ai.ripple",
                     "cause": None, "reason": None,
                     "reason_code": None, "reason_params": None}
    assert asked == ["ai.ripple"]


async def test_no_environment_variable_turns_the_guard_off(
        monkeypatch: pytest.MonkeyPatch, stub_factory, ripple_factory, tmp_path,
        inherited, exits):
    """There is no switch. A production process cannot flag its way past this.

    The seam in the tests replaces the two OS probes; nothing in the decision
    reads the environment except the label itself, so no `RIPPLE_*` name can make
    an inherited label count.
    """
    for name in ("RIPPLE_SUPERVISED", "RIPPLE_RESTART_ALLOW_UNSUPERVISED",
                 "RIPPLE_SKIP_SUPERVISION", "RIPPLE_FORCE_RESTART",
                 "RIPPLE_RESTART_ANYWAY", "RIPPLE_ASSUME_LAUNCHD"):
        monkeypatch.setenv(name, "1")
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    assert harness.ripple.restart_state()["supervised"] is False
    assert (await _restart(harness)).status_code == 503
    assert exits == []


def test_the_decision_reads_no_configuration():
    """Static: the rule itself has no `os.environ`/config read to switch it off."""
    source = _code_of(proxy.supervision_state)
    assert "os.environ" not in source
    assert "config" not in source
    assert "_supervisor_marker()" in source


def test_the_job_pid_is_read_from_the_top_level_of_the_job_block():
    """Only the job's own `pid` counts — nested dictionaries are indented deeper."""
    assert proxy._job_pid(_job_dict("ai.ripple", 11129)) == 11129
    # A job that is not running has no pid line at all.
    assert proxy._job_pid(f"gui/{os.getuid()}/ai.ripple = {{\n\tstate = waiting\n}}\n") is None
    # Synthetic: a pid inside a nested block is not the job's pid.
    nested = ("gui/501/ai.ripple = {\n\tstate = running\n\tendpoints = {\n"
              "\t\t\"gui/501\" = {\n\t\t\tpid = 7\n\t\t}\n\t}\n}\n")
    assert proxy._job_pid(nested) is None
    assert proxy._job_pid("") is None


def test_launchd_output_naming_another_pid_is_not_ownership(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ownership_memo):
    _fake_launchctl(tmp_path, monkeypatch,
                    stdout=_job_dict("ai.ripple", os.getpid() + 1))
    assert proxy._launchd_owns_pid("ai.ripple") is False
    assert ownership_memo == {}                 # a "no" is never remembered


def test_launchd_output_naming_this_pid_is_ownership(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fake_launchctl(tmp_path, monkeypatch, stdout=_job_dict("ai.ripple", os.getpid()))
    assert proxy._launchd_owns_pid("ai.ripple") is True


def test_a_label_nobody_owns_is_not_ownership(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The measured failure: rc 113, `Could not find service … in domain`."""
    _fake_launchctl(tmp_path, monkeypatch,
                    stdout=f'Could not find service "ai.ripple" in domain for '
                           f'user gui: {os.getuid()}\n', code=113)
    assert proxy._launchd_owns_pid("ai.ripple") is False


def test_a_machine_without_launchctl_is_not_supervised(
        monkeypatch: pytest.MonkeyPatch):
    """No tool, no proof — the conservative answer, and no exception.

    Since #29 the answer is `None`, not `False`: nobody could be asked, which is
    a different fact from "the job names another pid".
    """
    monkeypatch.setattr(proxy.shutil, "which", lambda name: None)
    assert proxy._query_launchd_owns_pid("ai.ripple") is None


def test_a_failing_query_is_not_supervised(monkeypatch: pytest.MonkeyPatch):
    """A timeout is not a supervisor either."""
    def boom(*args, **kwargs):
        raise proxy.subprocess.TimeoutExpired(cmd="launchctl", timeout=5.0)

    monkeypatch.setattr(proxy.shutil, "which", lambda name: "/bin/launchctl")
    monkeypatch.setattr(proxy.subprocess, "run", boom)
    assert proxy._query_launchd_owns_pid("ai.ripple") is None


def test_a_label_with_a_path_is_never_queried(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A label is a name, not an address: `gui/501/x` is refused unread."""
    calls = _fake_launchctl(tmp_path, monkeypatch,
                            stdout=_job_dict("ai.ripple", os.getpid()))
    assert proxy._query_launchd_owns_pid(f"gui/{os.getuid()}/ai.ripple") is None
    assert not calls.exists()


def test_the_ownership_query_is_asked_once_per_process(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`/healthz` carries this state and the page polls it every 2 s."""
    calls = _fake_launchctl(tmp_path, monkeypatch,
                            stdout=_job_dict("ai.ripple", os.getpid()))
    assert proxy._launchd_owns_pid("ai.ripple") is True
    assert proxy._launchd_owns_pid("ai.ripple") is True
    assert calls.read_text(encoding="utf-8") == "x"


def test_a_refused_ownership_is_asked_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A transient failure must not leave the control disabled for good."""
    calls = _fake_launchctl(tmp_path, monkeypatch,
                            stdout=_job_dict("ai.ripple", os.getpid() + 1))
    assert proxy._launchd_owns_pid("ai.ripple") is False
    assert proxy._launchd_owns_pid("ai.ripple") is False
    assert calls.read_text(encoding="utf-8") == "xx"



# ---------------------------------------------------------------------------
# #29: the cause is a code, not a sentence
# ---------------------------------------------------------------------------
# The page is bilingual and the prose above is English, so the refusal it shows
# has to be a token the catalog can translate. These tests pin the contract from
# the service's side: every cause has a code from the closed set, the code
# carries the values the sentence interpolates, and it survives both the
# `/healthz` projection and the two refusal bodies.
def test_every_cause_carries_a_code_from_the_closed_set(
        monkeypatch: pytest.MonkeyPatch):
    """The five causes, each with its own token — and no token outside the set."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.ripple")
    cases = {
        # (env label, ppid, ownership answer) → code
        ("ai.ripple", 1, True): None,                  # supervised: no refusal
        ("ai.ripple", 1, False): "label_not_owner",
        ("ai.ripple", 1, None): "query_failed",
        ("ai.ripple", 4242, True): "inherited_label",  # the parent decides
    }
    seen = []
    for (label, ppid, owns), code in cases.items():
        monkeypatch.setenv("XPC_SERVICE_NAME", label)
        state = proxy.supervision_state(ppid=ppid, owns=lambda _l, a=owns: a)
        assert state["reason_code"] == code, (label, ppid, owns)
        if code is not None:
            seen.append(state["reason_code"])
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert proxy.supervision_state()["reason_code"] == "no_label"
    seen.append("no_label")
    # `inflight` is the fifth code: the drain gate, decided in `restart_state`.
    assert set(seen) | {"inflight"} == set(proxy.REASON_CODES)


def test_each_code_carries_the_values_its_sentence_interpolates(
        monkeypatch: pytest.MonkeyPatch):
    """The page interpolates parameters, it never parses numbers out of prose."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway")
    assert proxy.supervision_state(ppid=99)["reason_params"] == {
        "label": "ai.hermes.gateway", "ppid": 99}
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.ripple")
    assert proxy.supervision_state(ppid=1, owns=lambda _l: False)["reason_params"] == {
        "label": "ai.ripple", "pid": os.getpid()}
    assert proxy.supervision_state(ppid=1, owns=lambda _l: None)["reason_params"] == {
        "label": "ai.ripple"}
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert proxy.supervision_state()["reason_params"] is None


async def test_the_code_survives_the_healthz_projection(
        stub_factory, ripple_factory, tmp_path, unsupervised, exits):
    """`/healthz → restart` is where the page reads it before an unlock."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    state = (await harness.healthz())["restart"]
    assert state["reason_code"] == "no_label"
    assert state["reason"] == UNSUPERVISED_REASON      # the prose is unchanged
    assert state["reason_params"] is None


async def test_the_503_body_carries_the_code(
        stub_factory, ripple_factory, tmp_path, inherited, exits):
    """The refusal body names the cause beside the sentence the page quotes."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))

    body = (await _restart(harness)).json()
    assert body["reason_code"] == "inherited_label"
    assert body["reason_params"] == {"label": "ai.hermes.gateway",
                                     "ppid": os.getppid()}
    assert body["error"]["code"] == "restart_unsupervised"


async def test_the_409_body_carries_the_code(
        stub_factory, ripple_factory, tmp_path, supervised, exits):
    """`inflight` is a code of its own: the drain gate, not the guard."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.host("A").inflight = 2

    body = (await _restart(harness)).json()
    assert body["reason_code"] == "inflight"
    assert body["reason_params"] == {"n": 2, "hosts": "A"}
    assert body["error"]["code"] == "restart_inflight"
    # /healthz says the same thing before the click
    assert (await harness.healthz())["restart"]["reason_code"] == "inflight"


async def test_the_guard_answers_before_the_drain_gate(
        stub_factory, ripple_factory, tmp_path, unsupervised, exits):
    """Both refusals can hold at once; the 503 is the one that fires, and the
    code says so — the page must not offer a `force` that cannot help."""
    a = stub_factory(models=_models())
    harness = await ripple_factory(_cfg(tmp_path, [host_config("A", a.url)]))
    harness.host("A").inflight = 1

    body = (await _restart(harness)).json()
    assert body["reason_code"] == "no_label"
    assert body["inflight"] == 1
    assert (await harness.healthz())["restart"]["reason_code"] == "no_label"
    assert exits == []


def test_the_query_failure_has_its_own_sentence(
        monkeypatch: pytest.MonkeyPatch):
    """ "The job does not own you" and "nobody could tell" are different facts."""
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.ripple")
    monkeypatch.setattr(proxy.shutil, "which", lambda name: None)
    state = proxy.supervision_state(ppid=1)
    assert state["reason_code"] == "query_failed"
    assert state["reason"] == proxy.QUERY_FAILED_REASON.format(label="ai.ripple")
    assert state["reason"] != UNOWNED_LABEL_REASON.format(
        label="ai.ripple", pid=os.getpid(), uid=os.getuid())
    assert state["supervised"] is False
