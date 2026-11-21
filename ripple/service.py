"""The service operations behind `ripple`: start, status, shutdown, restart, update (#121),
and the autostart toggle `ripple -perm` / `ripple shutdown -perm` (#122).

Before this module, restarting the proxy meant typing
`launchctl kickstart -k gui/$(id -u)/ai.ripple` — an annoyance for the operator
and an obstacle for anyone else who has to keep the service running. The words
here are the whole vocabulary:

    ripple            start (or report that it already runs)
    ripple status     running/stopped, pid, port, version, uptime, log path
    ripple shutdown   stop
    ripple restart    stop, start, wait until the dashboard answers, say how long
    ripple update     fetch, run the suite, restart, report the version before/after
    ripple -perm      install the launchd job from THIS checkout and start it
    ripple shutdown -perm
                      stop, then remove the job — no autostart any more

The two `-perm` words exist because `shutdown` alone does not switch autostart
off: it boots the job out of the running domain, and the plist stays in
`~/Library/LaunchAgents`, so launchd loads it again at the next login. That is
the difference the flag names — *permanently*, not "until the next login".

**The contract this module is built to (#121), because it is what makes the
commands testable without touching the production service:**

* The label comes from ``RIPPLE_LABEL`` (default ``ai.ripple``), the port from
  ``RIPPLE_PORT`` (default: ``listen_port`` from the config, else 8010), the
  plist from ``RIPPLE_PLIST`` (default ``~/Library/LaunchAgents/<label>.plist``).
  Neither is hardcoded anywhere else — a test drives a throwaway label on a
  throwaway port and production never sees it.
* Two modes, one port. Under launchd the label is loaded and launchd owns the
  process; a manual start is the plain foreground
  `python -m ripple --config …` that ``deploy/start-ripple.command`` runs. The
  commands never start a second instance: a busy port is named, with the PID
  that holds it, and the command fails instead of racing.
* Idempotent and honest. Starting a running service reports it and exits 0;
  stopping a stopped one says so and exits 0; every failure names the cause and
  the next step.

The port check is a *read* of the socket, never a signal to it: an instance on
another port is somebody else's, and this module only ever acts on the port it
resolved for itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .build import REPO
from .config import Config, ConfigError, load_config

#: Production defaults. Each is read exactly once, through the accessor below;
#: nothing else in the repository spells them out.
DEFAULT_LABEL = "ai.ripple"
DEFAULT_PORT = 8010

#: The default suite `ripple update` runs. ``{python}`` is the interpreter that
#: is running the CLI (the venv python), so the suite runs in the same
#: environment as the proxy. ``RIPPLE_UPDATE_SUITE`` overrides the whole line —
#: it exists so the tests do not have to wait the ~245 s the real suite takes,
#: and it is the only reason it exists.
SUITE_COMMAND = "{python} -m pytest -q"

#: How long a restart may take to answer GET / before it is called a failure.
#: The #117 lesson: the socket is open before the app is ready, so the port
#: binding is not the signal — the dashboard is.
DEFAULT_WAIT_SECONDS = 30.0

#: How long `ripple -perm` waits for /healthz after the job is bootstrapped.
#: The same #117 lesson, one step earlier: launchd reports the job as loaded
#: before uvicorn has bound the port, so "bootstrapped" is not "answering".
DEFAULT_INSTALL_WAIT_SECONDS = 30.0

#: The one flag that means autostart (#122). Alone it switches autostart on,
#: after `shutdown` it switches it off; with any other word it is a usage error.
PERM_FLAG = "-perm"

#: The two ProgramArguments the plist template carries for THIS checkout. They
#: are replaced by value, not by `__REPO__` substitution, because the interpreter
#: and the config file are not always `<repo>/.venv/bin/python` and
#: `<repo>/config.yaml` (a worktree has no venv; RIPPLE_CONFIG moves the config).
PYTHON_ARG_LINE = "<string>__REPO__/.venv/bin/python</string>"
CONFIG_ARG_LINE = "<string>__REPO__/config.yaml</string>"

#: launchd's own "no such service in this domain" (measured: rc 113).
NO_SUCH_SERVICE = 113
LAUNCHCTL_TIMEOUT_SECONDS = 10.0

# Exit codes. 0 is success, 2 is the config error `__main__` already uses; the
# rest name the stage that failed, so a script can tell them apart.
# `-perm` adds no code of its own (#122): 2 for a missing config or key (and for
# `-perm` on the wrong word), 3 when the port belongs to something that is not
# RIPPLE or launchd refuses the plist, 4 when a stop did not stop, 5 when the
# installed job never answered /healthz, 6 when the plist could not be written
# or does not lint.
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PORT_BUSY = 3
EXIT_NOT_STOPPED = 4
EXIT_NO_ANSWER = 5
EXIT_REFUSED = 6
EXIT_SUITE_RED = 7


class ServiceError(RuntimeError):
    """A failure that has a message for the operator and an exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --- environment -----------------------------------------------------------

def label() -> str:
    """The launchd job label: ``RIPPLE_LABEL``, else ``ai.ripple``."""
    return (os.environ.get("RIPPLE_LABEL") or "").strip() or DEFAULT_LABEL


def domain() -> str:
    """The launchd domain of this login session: ``gui/<uid>``."""
    return f"gui/{os.getuid()}"


def plist_path(lab: str | None = None) -> Path:
    """Where the job's plist lives: ``RIPPLE_PLIST``, else the standard place."""
    override = os.environ.get("RIPPLE_PLIST")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "LaunchAgents" / f"{lab or label()}.plist"


def config_path() -> Path:
    """The configuration the commands reason about: ``RIPPLE_CONFIG``, else config.yaml."""
    return Path(os.environ.get("RIPPLE_CONFIG") or "config.yaml").expanduser()


def job_config_path() -> Path:
    """The config path the launchd job gets: ``RIPPLE_CONFIG``, else ``<repo>/config.yaml``.

    A job has no shell and no working directory of ours, so a relative
    ``RIPPLE_CONFIG`` has to become absolute — resolved against the directory
    this command runs in, which is the one the operator meant. Without the
    override the path is the checkout's own ``config.yaml``: the plist names the
    checkout, not the shell's idea of "here".
    """
    raw = os.environ.get("RIPPLE_CONFIG")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(REPO) / "config.yaml"


def wait_seconds() -> float:
    """How long `restart` waits for the dashboard: ``RIPPLE_WAIT_SECONDS``, else 30."""
    raw = os.environ.get("RIPPLE_WAIT_SECONDS")
    if not raw:
        return DEFAULT_WAIT_SECONDS
    try:
        return max(0.5, float(raw))
    except ValueError:
        raise ServiceError(f"RIPPLE_WAIT_SECONDS must be a number of seconds, not {raw!r}",
                           EXIT_CONFIG)


def _config_or_none() -> Config | None:
    """The config, or None when there is none — `status` must work without one."""
    try:
        return load_config(str(config_path()))
    except ConfigError:
        return None


def resolve_port(cfg: Config | None = None) -> int:
    """The port these commands act on.

    ``RIPPLE_PORT`` first (that is the convention ``deploy/install-launchd.command``
    already follows), then ``listen_port`` from the config, then 8010. Never a
    second source: a command and the service it talks to must agree on one number.
    """
    raw = (os.environ.get("RIPPLE_PORT") or "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            raise ServiceError(f"RIPPLE_PORT must be a port number, not {raw!r}", EXIT_CONFIG)
        if not 1 <= port <= 65535:
            raise ServiceError(f"RIPPLE_PORT must be between 1 and 65535, not {port}", EXIT_CONFIG)
        return port
    if cfg is None:
        cfg = _config_or_none()
    if cfg is not None:
        return int(cfg.listen_port)
    return DEFAULT_PORT


def log_file(cfg: Config | None = None) -> str:
    """The log path the service writes, or a sentence saying there is none."""
    if cfg is None:
        cfg = _config_or_none()
    if cfg is not None and cfg.log_file:
        return str(Path(cfg.log_file).expanduser())
    return "not configured (no log_file in the config)"


# --- launchd ---------------------------------------------------------------

def _launchctl(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True,
                              timeout=LAUNCHCTL_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def job_state(lab: str | None = None) -> dict[str, Any]:
    """What launchd says about the job: ``{loaded, pid, state, raw}``.

    ``loaded`` is False only when launchd *answered* that the label is not in the
    domain (rc 113). A missing ``launchctl`` or a timeout is not an answer, and
    is reported as ``loaded=None`` — the caller says "could not tell" instead of
    inventing "not loaded", which would start a second instance.
    """
    lab = lab or label()
    out = _launchctl("print", f"{domain()}/{lab}")
    if out is None:
        return {"loaded": None, "pid": None, "state": None, "raw": "launchctl not available"}
    if out.returncode != 0:
        loaded = False if out.returncode == NO_SUCH_SERVICE else None
        return {"loaded": loaded, "pid": None, "state": None,
                "raw": (out.stderr or out.stdout).strip()}
    pid = None
    m = re.search(r"^\s*pid = (\d+)\s*$", out.stdout, re.MULTILINE)
    if m:
        pid = int(m.group(1))
    m = re.search(r"^\s*state = (\S+)\s*$", out.stdout, re.MULTILINE)
    return {"loaded": True, "pid": pid, "state": m.group(1) if m else None, "raw": out.stdout}


def job_loaded(lab: str | None = None) -> bool:
    """True when launchd knows the job. An unknown answer counts as "not loaded"."""
    return job_state(lab)["loaded"] is True


def wait_job_gone(lab: str | None = None, seconds: float = 15.0) -> bool:
    """Wait until launchd no longer lists the job; True when it is gone.

    `bootout` returns before launchd has finished removing the job, and a
    `launchctl print` in that window still answers "loaded" with the dying pid.
    Measured on the first real restart: the follow-up `bootstrap` was skipped
    because the stale answer said "still loaded", and the service stayed down.
    The port being free is not the same fact as the job being gone.
    """
    lab = lab or label()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not job_state(lab)["loaded"]:
            return True
        time.sleep(0.2)
    return not job_state(lab)["loaded"]


def stop_job(lab: str | None = None) -> str:
    """Remove the job from launchd. Returns a sentence for the operator."""
    lab = lab or label()
    out = _launchctl("bootout", f"{domain()}/{lab}")
    if out is None:
        raise ServiceError("launchctl is not available — cannot stop the launchd job",
                           EXIT_NOT_STOPPED)
    if out.returncode != 0 and job_loaded(lab):
        raise ServiceError(
            f"launchctl bootout {domain()}/{lab} failed (rc {out.returncode}): "
            f"{(out.stderr or out.stdout).strip()}\n"
            f"  next: launchctl print {domain()}/{lab}",
            EXIT_NOT_STOPPED)
    return f"removed the launchd job {lab}"


def _plutil(*args: str) -> subprocess.CompletedProcess[str] | None:
    """`plutil` with a timeout; None when it is not there at all."""
    try:
        return subprocess.run(["plutil", *args], capture_output=True, text=True,
                              timeout=LAUNCHCTL_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def job_python(repo: Path | None = None) -> str:
    """The interpreter the job runs: ``<repo>/.venv/bin/python`` when it exists.

    In the documented install the venv sits inside the checkout, so this is
    literally `<repo>/.venv/bin/python` — the value the contract asks for. A
    checkout without one (a worktree, a test) would get a plist that cannot
    start at all, so the interpreter running this command is the fallback: it is
    the one that can import `ripple` from here, which is the property that
    matters.
    """
    candidate = Path(repo or REPO) / ".venv" / "bin" / "python"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return sys.executable


def render_plist(*, repo: Path | None = None, lab: str | None = None,
                 cfg_path: Path | None = None, python: str | None = None,
                 home: Path | None = None) -> str:
    """The plist for THIS checkout, rendered from ``deploy/ai.ripple.plist``.

    Pure: no file is written and no command is run, so a test can hold the
    result against the paths it expects. The template is the source — a rendered
    plist is never copied, because the one thing that must be current is the
    directory the command ran from. ProgramArguments and WorkingDirectory are
    filled from `repo`, `cfg_path` and `python` by value; a template that no
    longer carries those two lines is refused instead of rendered into a plist
    that starts the wrong thing.
    """
    repo = Path(repo or REPO)
    lab = lab or label()
    cfg_path = Path(cfg_path or job_config_path())
    template = repo / "deploy" / "ai.ripple.plist"
    try:
        text = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise ServiceError(
            f"cannot read the plist template {template}: {exc.strerror or exc}\n"
            f"  next: run this from a complete checkout — deploy/ai.ripple.plist "
            f"is part of it",
            EXIT_REFUSED)
    replacements = ((PYTHON_ARG_LINE, python or job_python(repo), "the interpreter"),
                    (CONFIG_ARG_LINE, str(cfg_path), "the config file"))
    for line, value, what in replacements:
        if line not in text:
            raise ServiceError(
                f"{template} has no {line!r} — it does not look like the template "
                f"this command knows, and a plist rendered from it would name {what} "
                f"wrongly.\n"
                f"  next: restore deploy/ai.ripple.plist from the repository",
                EXIT_REFUSED)
        text = text.replace(line, f"<string>{value}</string>")
    text = (text.replace("__REPO__", str(repo))
                .replace("__HOME__", str(home or Path.home()))
                .replace("__LABEL__", lab))
    leftover = [name for name in ("__REPO__", "__HOME__", "__LABEL__") if name in text]
    if leftover:
        raise ServiceError(
            f"the rendered plist still contains {', '.join(leftover)} — "
            f"refusing to install a half-filled job", EXIT_REFUSED)
    return text


def plist_points_at(path: Path, *, repo: Path, lab: str, cfg_path: Path) -> bool:
    """True when the installed plist already names this checkout, label and config.

    A string test, not a re-render: the installed file carries the oMLX key
    (written by plutil) where the render has the placeholder, so the two texts
    are never equal. What has to match is where the job points.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return all(f"<string>{value}</string>" in text
               for value in (lab, str(repo), str(cfg_path)))


def install_plist(*, lab: str, text: str, key: str) -> Path:
    """Write the plist (0600), put the oMLX key in it with plutil, read it back.

    The key is written after the file exists because that is what plutil edits,
    and it is read back because "plutil returned 0" is a claim, not a
    measurement. A plist that ends up without its key is removed again: a job
    that cannot start is worse than no job, because it looks installed.
    """
    path = plist_path(lab)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        raise ServiceError(
            f"could not write {path}: {exc.strerror or exc}\n"
            f"  next: check that {path.parent} exists and is writable",
            EXIT_REFUSED)
    out = _plutil("-replace", "EnvironmentVariables.OMLX_API_KEY", "-string", key, str(path))
    if out is None or out.returncode != 0:
        detail = (out.stderr or out.stdout).strip() if out is not None else "plutil is not available"
        path.unlink(missing_ok=True)
        raise ServiceError(
            f"plutil could not write the oMLX key into {path}: {detail}\n"
            f"  next: the job is NOT installed — check plutil, then run `ripple -perm` again",
            EXIT_REFUSED)
    os.chmod(path, 0o600)
    read_back = _plutil("-extract", "EnvironmentVariables.OMLX_API_KEY", "raw", str(path))
    if read_back is None or read_back.returncode != 0 or read_back.stdout.strip() != key:
        path.unlink(missing_ok=True)
        raise ServiceError(
            f"the oMLX key did not land in {path} — refusing to install a job that "
            f"cannot start.\n  next: check plutil and ~/.omlx/api_key.txt",
            EXIT_REFUSED)
    lint = _plutil("-lint", str(path))
    if lint is None or lint.returncode != 0:
        detail = (lint.stderr or lint.stdout).strip() if lint is not None else "plutil is not available"
        path.unlink(missing_ok=True)
        raise ServiceError(f"{path} does not lint ({detail}) — removed it again", EXIT_REFUSED)
    return path


def remove_plist(lab: str | None = None) -> Path:
    """Delete the job's plist; returns the path whether or not it existed."""
    path = plist_path(lab or label())
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ServiceError(
            f"could not remove {path}: {exc.strerror or exc}\n"
            f"  next: rm {path} — the job itself is already unloaded",
            EXIT_NOT_STOPPED)
    return path


def bootstrap_job(lab: str | None = None) -> None:
    """Load the installed plist into launchd, and refuse to pretend it worked."""
    lab = lab or label()
    path = plist_path(lab)
    out = _launchctl("bootstrap", domain(), str(path))
    if out is None:
        raise ServiceError("launchctl is not available — cannot install the job",
                           EXIT_PORT_BUSY)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip()
        raise ServiceError(
            f"launchctl bootstrap {domain()} {path} failed: {detail}\n"
            f"  next: launchctl print {domain()}/{lab} — the plist stays installed, "
            f"`ripple -perm` is safe to repeat",
            EXIT_PORT_BUSY)


# --- the socket ------------------------------------------------------------

def listener_pid(port: int) -> int | None:
    """The PID listening on ``port`` (IPv4 or IPv6), or None when it is free."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=10.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.split():
        if line.isdigit():
            return int(line)
    return None


#: A RIPPLE instance, as it appears in the process table: `python -m ripple
#: --config …` — launchd's ProgramArguments and `spawn_background` both use it,
#: so this is the one signature a shutdown may act on. A stranger on the port is
#: named and left alone: killing somebody else's process to free "our" port is
#: exactly the mistake this check exists to prevent (measured: a bare `restart`
#: once terminated the test socket holder it was supposed to refuse).
RIPPLE_CMDLINE = re.compile(r"-m\s+ripple\b")


def _command_of(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=10.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown command"
    # One line, short: ps prints an embedded newline in a `-c` script as \012,
    # and a 400-character line helps nobody read the message.
    text = " ".join(out.stdout.split())
    return (text[:200] + " …") if len(text) > 200 else (text or "unknown command")


def is_ripple_process(pid: int) -> bool:
    """True when the command line of `pid` is a RIPPLE instance."""
    return bool(RIPPLE_CMDLINE.search(_command_of(pid)))


def wait_port_free(port: int, seconds: float = 15.0) -> int | None:
    """Wait until nothing listens on ``port``; the last holder's PID, or None."""
    deadline = time.monotonic() + seconds
    holder = listener_pid(port)
    while holder is not None and time.monotonic() < deadline:
        time.sleep(0.25)
        holder = listener_pid(port)
    return holder


def terminate(pid: int, port: int, seconds: float = 15.0) -> None:
    """SIGTERM one process and wait for its port; SIGKILL if it will not go.

    The PID is the one that *holds the port* — never a pattern match over the
    process table, which is how the old stop script could have missed the
    process in this environment (see deploy/stop-ripple.command).
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        raise ServiceError(f"not allowed to signal PID {pid} — start the proxy as this user",
                           EXIT_NOT_STOPPED)
    if wait_port_free(port, seconds) is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if wait_port_free(port, 5.0) is not None:
        raise ServiceError(
            f"PID {pid} still holds port {port} after SIGKILL — "
            f"check: lsof -nP -iTCP:{port} -sTCP:LISTEN",
            EXIT_NOT_STOPPED)


# --- the service itself ----------------------------------------------------

def healthz(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """``/healthz`` as a dict, or None when nothing answers on the port."""
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def dashboard_answers(port: int, timeout: float = 2.0) -> bool:
    """True when GET / answers 200 — the app is ready, not just the socket."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_dashboard(port: int, seconds: float,
                       poll: Callable[[int], bool] = dashboard_answers) -> float | None:
    """Poll GET / until it answers; the seconds it took, or None on timeout."""
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        if poll(port):
            return time.monotonic() - started
        time.sleep(0.2)
    return None


def wait_for_healthz(port: int, seconds: float) -> float | None:
    """Poll GET /healthz until it answers; the seconds it took, or None.

    What `ripple -perm` waits for. `launchctl` says "loaded" as soon as the job
    is in the domain, and the socket opens a moment later; `/healthz` is the
    first thing that knows the proxy is actually there (#117).
    """
    return wait_for_dashboard(port, seconds, poll=lambda p: healthz(p) is not None)


def tail(path: str, lines: int = 15) -> str:
    """The last lines of the log, for a failure that needs evidence."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""


def human_uptime(seconds: float) -> str:
    """``4559.5`` → ``4559 s (1 h 15 min)`` — the unit belongs next to the number."""
    total = int(max(0.0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if not parts:
        parts.append(f"{secs} s")
    return f"{total} s ({' '.join(parts)})"


# --- starting --------------------------------------------------------------

def api_key_path() -> Path:
    """Where the oMLX key comes from: ``~/.omlx/api_key.txt``.

    One path, named in one place, because the plist and a spawned instance must
    read the same key — and because the error message has to name the file.
    """
    return Path.home() / ".omlx" / "api_key.txt"


def api_key_from_file() -> str | None:
    """``OMLX_API_KEY`` from ``~/.omlx/api_key.txt`` — launchd reads no files.

    Only used for a process this module spawns itself (a restart in manual
    mode); the launchd path gets the key from the plist. Loopback clients need
    no key, but the instance finds no models without it.
    """
    try:
        key = api_key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def api_key_or_error() -> str:
    """The oMLX key for the plist, or a ServiceError that names the file."""
    key = api_key_from_file()
    if key is None:
        path = api_key_path()
        raise ServiceError(
            f"no oMLX key at {path} — the launchd job cannot start without it "
            f"(launchd reads no files; the key has to be inside the plist).\n"
            f"  next: write the key there and chmod 600 {path}, then run "
            f"`ripple -perm` again",
            EXIT_CONFIG)
    return key


def spawn_background(cfg_path: Path, log_path: str | None) -> int:
    """Start the proxy detached from this terminal; the child's PID.

    Same arguments as the launchd job (`python -m ripple --config …`) and the
    same working directory, so a restarted instance behaves exactly like the one
    it replaced. The log file is handed to the child as stdout/stderr —
    `setup_logging` notices that stderr *is* the log file and keeps only the
    rotating handler, so the lines are not doubled.
    """
    env = dict(os.environ)
    if not env.get("OMLX_API_KEY"):
        key = api_key_from_file()
        if key:
            env["OMLX_API_KEY"] = key
    out: Any = subprocess.DEVNULL
    handle = None
    if log_path and not log_path.startswith("not configured"):
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            handle = open(log_path, "a", encoding="utf-8")
            out = handle
        except OSError:
            out = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ripple", "--config", str(cfg_path)],
            cwd=str(REPO), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True)
    finally:
        if handle is not None:
            handle.close()          # the child holds its own copy of the fd
    return proc.pid


def start_loaded_or_spawn(*, cfg: Config, port: int, background: bool) -> tuple[str, int]:
    """The one place that decides how an instance comes up.

    Returns ``(mode, pid)`` where mode is ``"job"`` (launchd started it) or
    ``"spawned"`` (detached from this terminal). Raises ServiceError when the
    port is taken by somebody else or launchd refuses the plist.
    """
    lab = label()
    state = job_state(lab)
    if state["loaded"]:
        return "job", state["pid"] or 0
    if state["loaded"] is None:
        raise ServiceError(
            f"launchctl could not tell whether {lab} is loaded ({state['raw']}).\n"
            f"  next: launchctl print {domain()}/{lab}",
            EXIT_PORT_BUSY)
    holder = listener_pid(port)
    if holder is not None:
        raise ServiceError(
            f"port {port} is already in use by PID {holder}: {_command_of(holder)}\n"
            f"  next: ripple status   (or stop it: ripple shutdown)",
            EXIT_PORT_BUSY)
    plist = plist_path(lab)
    if background and plist.is_file():
        out = _launchctl("bootstrap", domain(), str(plist))
        if out is None or out.returncode != 0:
            detail = (out.stderr or out.stdout).strip() if out is not None else "no launchctl"
            raise ServiceError(
                f"launchctl bootstrap {domain()} {plist} failed: {detail}\n"
                f"  next: launchctl print {domain()}/{lab}",
                EXIT_PORT_BUSY)
        return "job", (job_state(lab)["pid"] or 0)
    return "spawned", spawn_background(config_path(), log_file(cfg))


# --- the commands ----------------------------------------------------------

def _parser(word: str, summary: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f"ripple {word}".strip(), description=summary)


def cmd_start(argv: list[str]) -> int:
    """`ripple` / `ripple start` — start, or report that it already runs.

    Three honest cases (contract 3):

    a. the job is loaded → report label, pid, port; exit 0. Starting what runs
       is not an error, and it must not start a second instance.
    b. no job, port free → run in the foreground, exactly the code path of
       `python -m ripple --config …` (blocking; Ctrl-C stops it).
    c. no job, port busy → name the holder and the next step; exit 3.
    """
    p = _parser("start", "Start the proxy in this terminal, or report that it already runs.")
    p.add_argument("--background", action="store_true",
                   help="detach instead of blocking (used by `ripple restart`)")
    args = p.parse_args(argv)

    cfg = _load()
    port = resolve_port(cfg)
    lab = label()
    state = job_state(lab)
    if state["loaded"]:
        print(f"RIPPLE is already running as the launchd job {lab}.")
        print(f"  pid:  {state['pid'] or 'unknown'}")
        print(f"  port: {port}")
        print("  restart it with: ripple restart   —   stop it with: ripple shutdown")
        return EXIT_OK
    holder = listener_pid(port)
    if holder is not None:
        # A RIPPLE instance that launchd does not own is still a running
        # service: report it and exit 0, like case (a). Only a *stranger* on the
        # port is a conflict (case c) — that one is named and left alone.
        if is_ripple_process(holder):
            print(f"RIPPLE is already running on port {port} (PID {holder}, "
                  f"not under launchd).")
            print("  restart it with: ripple restart   —   stop it with: ripple shutdown")
            return EXIT_OK
        raise ServiceError(
            f"port {port} is already in use by PID {holder}: {_command_of(holder)}\n"
            f"  next: ripple status — or stop it first: ripple shutdown",
            EXIT_PORT_BUSY)
    if args.background:
        mode, pid = start_loaded_or_spawn(cfg=cfg, port=port, background=True)
        print(f"RIPPLE started in the background (pid {pid}, port {port}, mode {mode}).")
        return EXIT_OK

    print(f"RIPPLE is not running as the launchd job {lab} — starting in this terminal.")
    print(f"  port: {port}   log: {log_file(cfg)}")
    print("  stop: Ctrl-C here, or `ripple shutdown` in another terminal")
    from .__main__ import run_foreground      # the same code path, by design
    return run_foreground(cfg)


def cmd_status(argv: list[str]) -> int:
    """`ripple status` — one line per fact, from the service first.

    ``/healthz`` is the first source, because it is the only one that knows the
    version, the uptime and the fleet; launchd only knows whether a job exists
    and which pid it owns. The output says which source answered, so a stopped
    service and an unresponsive one are not the same sentence.

    Exit 0 when the service runs *or* is cleanly stopped; 1 when launchd has a
    job that does not answer — that is the state worth a script's attention.
    """
    _parser("status", "Report whether the proxy runs: pid, port, version, uptime, log.").parse_args(argv)
    lab = label()
    cfg = _config_or_none()
    port = resolve_port(cfg)
    state = job_state(lab)
    health = healthz(port)

    if health is not None:
        pid = listener_pid(port) or state["pid"]
        print("state: running")
        print(f"source: /healthz on port {port}")
        print(f"label: {lab} ({'loaded in launchd' if state['loaded'] else 'not a launchd job'})")
        print(f"pid: {pid or 'unknown'}")
        print(f"port: {port}")
        print(f"version: {health.get('version') or 'unknown'}")
        build = health.get("build") or {}
        if build.get("commit"):
            dirty = " (dirty tree)" if build.get("dirty") else ""
            print(f"commit: {build['commit']}{dirty}")
        if health.get("uptime_seconds") is not None:
            print(f"uptime: {human_uptime(float(health['uptime_seconds']))}")
        print(f"log: {log_file(cfg)}")
        print(f"dashboard: http://127.0.0.1:{port}/")
        return EXIT_OK

    if state["loaded"]:
        print("state: not answering")
        print(f"source: launchctl (no answer on http://127.0.0.1:{port}/healthz)")
        print(f"label: {lab} (loaded in launchd)")
        print(f"pid: {state['pid'] or 'unknown'}")
        print(f"port: {port}")
        print(f"log: {log_file(cfg)}")
        print("next: ripple restart — or read the log tail above the last start")
        return 1

    print("state: stopped")
    print(f"source: launchctl (job {lab} is not loaded) and port {port} is free")
    print(f"label: {lab} (not loaded)")
    print(f"port: {port}")
    print(f"log: {log_file(cfg)}")
    print("next: ripple   (start in this terminal)   —   ripple restart   (detached)")
    return EXIT_OK


def _stop_service(lab: str, port: int, state: dict[str, Any]) -> str:
    """Stop whatever is running; returns what it stopped, in words.

    The one stop path: the job if launchd owns one, else the listener on the
    port. A listener that is not a RIPPLE instance is named and left alone
    (exit 4) — freeing "our" port by killing a stranger is the mistake this
    module exists to prevent. Every claim is verified before it is printed:
    the job must be gone *and* the port free.
    """
    if state["loaded"]:
        sentence = stop_job(lab)
        print(f"RIPPLE stopped: {sentence}.")
        if not wait_job_gone(lab, 15.0):
            raise ServiceError(
                f"launchd still lists the job {lab} after bootout.\n"
                f"  next: launchctl print {domain()}/{lab}",
                EXIT_NOT_STOPPED)
        holder = wait_port_free(port, 15.0)
        if holder is not None:
            raise ServiceError(
                f"the job is gone but port {port} is still held by PID {holder}: "
                f"{_command_of(holder)}\n"
                f"  next: that is not RIPPLE — stop it yourself, or run `ripple status`",
                EXIT_NOT_STOPPED)
        print(f"  port {port} is free.")
        return f"the launchd job {lab}"

    holder = listener_pid(port)
    if holder is None:
        print(f"RIPPLE is not running — job {lab} is not loaded and port {port} is free.")
        return "nothing"
    if not is_ripple_process(holder):
        raise ServiceError(
            f"port {port} is held by PID {holder}, which is not a RIPPLE instance:\n"
            f"  {_command_of(holder)}\n"
            f"  next: not touching it — stop that process yourself, or set RIPPLE_PORT "
            f"to the port RIPPLE listens on",
            EXIT_NOT_STOPPED)
    print(f"RIPPLE is not a launchd job — stopping PID {holder} on port {port}:")
    print(f"  {_command_of(holder)}")
    terminate(holder, port)
    if listener_pid(port) is not None:
        raise ServiceError(f"port {port} is still held after stopping PID {holder}",
                           EXIT_NOT_STOPPED)
    print(f"  stopped (SIGTERM), port {port} is free.")
    return f"PID {holder} (not under launchd)"


def cmd_shutdown(argv: list[str]) -> int:
    """`ripple shutdown` / `ripple shutdown -perm` — stop, and verify.

    Without the flag: stop only. If the job is loaded it is booted out of
    launchd (otherwise launchd would start it again immediately); otherwise the
    PID holding the port gets SIGTERM, then SIGKILL after 15 s. The plist stays,
    so the service comes back at the next login — that is `ripple shutdown`,
    not autostart off.

    With `-perm`: stop first, *then* remove the job, in that order and never the
    other way round (a job removed while its process runs leaves an orphan on
    the port that nothing knows how to stop). Afterwards both facts are read
    back — `launchctl print` fails and the port has no listener — and the
    command prints `autostart: off` plus which of the two it stopped.
    """
    p = _parser("shutdown", "Stop the proxy: bootout the launchd job, or signal the listener.")
    p.add_argument(PERM_FLAG, dest="perm", action="store_true",
                   help="also remove the launchd job: no autostart any more "
                        "(stop first, then remove, then verify)")
    args = p.parse_args(argv)

    lab = label()
    cfg = _config_or_none()
    port = resolve_port(cfg)
    state = job_state(lab)

    if not args.perm:
        _stop_service(lab, port, state)
        return EXIT_OK

    plist = plist_path(lab)
    installed = plist.is_file()
    stopped = _stop_service(lab, port, state)
    if installed:
        print(f"removed {remove_plist(lab)}")

    # Read the state back: "bootout returned 0" is not "the job is gone", and
    # "SIGTERM sent" is not "the port is free" (#121, measured twice).
    gone = wait_job_gone(lab, 5.0)
    holder = wait_port_free(port, 5.0)
    if not gone:
        raise ServiceError(
            f"launchd still lists the job {lab} after bootout — autostart is NOT off.\n"
            f"  next: launchctl print {domain()}/{lab}",
            EXIT_NOT_STOPPED)
    if holder is not None:
        raise ServiceError(
            f"the job is gone but port {port} is still held by PID {holder}: "
            f"{_command_of(holder)}\n"
            f"  next: that is not RIPPLE — stop it yourself",
            EXIT_NOT_STOPPED)

    print(f"autostart: off ({lab}) — stopped {stopped}"
          f"{'' if installed else '; no plist was installed'}")
    # The last line of launchctl's answer, not all of it: `launchctl print` on a
    # missing service prints "Bad request." first and the sentence second.
    answer = [line.strip() for line in (job_state(lab)["raw"] or "").splitlines() if line.strip()]
    print(f"  verified: launchctl print {domain()}/{lab} → "
          f"{answer[-1] if answer else 'no answer'}; port {port} has no listener")
    return EXIT_OK


def cmd_autostart(argv: list[str]) -> int:
    """`ripple -perm` — turn autostart on: install the job from THIS checkout.

    The order is the whole design, because each step can fail and a
    half-installed job looks installed:

    1. The inputs are checked before anything is written: the config must load,
       the oMLX key must exist, and the port must not belong to a stranger.
       `config.yaml` and the key are named with the next step if they are
       missing; a stranger on the port is named and left alone (exit 3).
    2. The plist is rendered from the template for *this* checkout — the
       interpreter, the config file and the working directory of the code that
       is running, never a copy of an old file.
    3. It is bootstrapped, and /healthz has to answer before the command says
       "on". "Bootstrapped" is launchd's claim; the answer is the measurement.

    A manual instance holding the port is the one case that is not an error: it
    keeps running and the job starts at the next login. Bootstrap it now and
    launchd's `KeepAlive` would restart a job that cannot bind, every ten
    seconds, until the login — so the plist is installed and the command says
    exactly that instead of pretending to have taken over.
    """
    p = argparse.ArgumentParser(
        prog="ripple -perm",
        description="Turn autostart on: install the launchd job from this checkout "
                    "and start it. It starts at login and is kept running. "
                    "`ripple shutdown -perm` turns it off again.")
    p.parse_args(argv)

    lab = label()
    repo = Path(REPO)
    cfg_path = job_config_path()
    cfg = _load(cfg_path)
    port = resolve_port(cfg)
    key = api_key_or_error()

    state = job_state(lab)
    if state["loaded"] is None:
        raise ServiceError(
            f"launchctl could not tell whether {lab} is loaded ({state['raw']}) — "
            f"not installing a second copy.\n"
            f"  next: launchctl print {domain()}/{lab}",
            EXIT_PORT_BUSY)
    holder = listener_pid(port)
    if holder is not None and not is_ripple_process(holder):
        raise ServiceError(
            f"port {port} is held by PID {holder}, which is not a RIPPLE instance:\n"
            f"  {_command_of(holder)}\n"
            f"  next: nothing was installed — stop it, or set RIPPLE_PORT to the "
            f"port this checkout should serve",
            EXIT_PORT_BUSY)

    path = plist_path(lab)
    healthy = healthz(port) is not None
    if state["loaded"] and plist_points_at(path, repo=repo, lab=lab, cfg_path=cfg_path) and healthy:
        print(f"autostart: on ({lab}, {repo})")
        print(f"  already installed and running (pid {state['pid'] or 'unknown'}); "
              f"nothing to change")
        return EXIT_OK

    text = render_plist(repo=repo, lab=lab, cfg_path=cfg_path)
    if state["loaded"]:
        # Loaded, but not what this checkout would install: either it does not
        # answer, or it points somewhere else. Say which before touching it.
        why = "does not answer" if not healthy else "points at another checkout"
        print(f"the job {lab} is loaded but {why} — reinstalling it from {repo}")
        stop_job(lab)
        if not wait_job_gone(lab, 15.0):
            raise ServiceError(
                f"launchd still lists the job {lab} after bootout — not installing "
                f"a second copy.\n  next: launchctl print {domain()}/{lab}",
                EXIT_NOT_STOPPED)
        holder = wait_port_free(port, 15.0)
        if holder is not None:
            raise ServiceError(
                f"the job {lab} was removed but PID {holder} still holds port {port}: "
                f"{_command_of(holder)}\n  next: ripple shutdown, then try again",
                EXIT_NOT_STOPPED)

    installed = install_plist(lab=lab, text=text, key=key)

    if holder is not None:
        print(f"autostart: on ({lab}, {repo})")
        print(f"  installed {installed}, but the job is NOT loaded yet: the manual "
              f"instance on port {port} (PID {holder}) keeps running.")
        print("  launchd starts the job at the next login — or now: ripple restart "
              "(it stops the manual instance and bootstraps the job)")
        return EXIT_OK

    bootstrap_job(lab)
    took = wait_for_healthz(port, DEFAULT_INSTALL_WAIT_SECONDS)
    if took is None:
        detail = tail(log_file(cfg)) if cfg.log_file else ""
        raise ServiceError(
            f"the job {lab} is installed and loaded, but /healthz did not answer on "
            f"http://127.0.0.1:{port}/ within {DEFAULT_INSTALL_WAIT_SECONDS:.0f} s.\n"
            + (f"  last lines of {log_file(cfg)}:\n{detail}" if detail
               else f"  log: {log_file(cfg)}")
            + f"\n  next: ripple status — or launchctl print {domain()}/{lab}",
            EXIT_NO_ANSWER)

    now = job_state(lab)
    print(f"autostart: on ({lab}, {repo})")
    print(f"  verified: launchctl print {domain()}/{lab} → "
          f"{'loaded' if now['loaded'] else 'NOT loaded'}, "
          f"pid {now['pid'] or 'unknown'}; /healthz answered after {took:.1f} s")
    return EXIT_OK


def cmd_restart(argv: list[str]) -> int:
    """`ripple restart` — stop, start, and return only once GET / answers.

    Returns when the *dashboard* answers, not when the socket is bound: the port
    is open before the app is ready (#117), so a restart that returned on the
    bind would hand the operator a proxy that 502s for a second. The measured
    seconds are printed; a timeout is a failure with the log tail, because a
    silent restart that did not come back is the one thing this command must
    never report as success.
    """
    _parser("restart", "Stop and start the proxy, then wait until the dashboard answers.").parse_args(argv)
    lab = label()
    cfg = _load()
    port = resolve_port(cfg)
    seconds = wait_seconds()
    state = job_state(lab)
    was_loaded = state["loaded"] is True

    started = time.monotonic()
    if was_loaded:
        print(f"stopped: {stop_job(lab)}")
        # launchd ends the job asynchronously: the socket outlives the bootout
        # by a moment, and the job entry outlives the socket. Waiting for both
        # facts is the difference between a restart and a half-restart —
        # measured on the first real run, where the port was free but launchd
        # still listed the job, so the bootstrap was skipped and the service
        # stayed down.
        gone = wait_job_gone(lab, 15.0)
        holder = wait_port_free(port, 15.0)
        if not gone:
            raise ServiceError(
                f"launchd still lists the job {lab} after bootout — not starting a "
                f"second copy.\n"
                f"  next: launchctl print {domain()}/{lab}",
                EXIT_NOT_STOPPED)
        if holder is not None:
            raise ServiceError(
                f"the job {lab} was removed but PID {holder} still holds port {port}:\n"
                f"  {_command_of(holder)}\n"
                f"  next: ripple shutdown (it signals that PID), then try again",
                EXIT_NOT_STOPPED)
    else:
        holder = listener_pid(port)
        if holder is None:
            print(f"nothing was running (job {lab} is not loaded, port {port} is free)")
        elif not is_ripple_process(holder):
            raise ServiceError(
                f"port {port} is held by PID {holder}, which is not a RIPPLE instance:\n"
                f"  {_command_of(holder)}\n"
                f"  next: not touching it — stop that process yourself, or set "
                f"RIPPLE_PORT to the port RIPPLE listens on",
                EXIT_NOT_STOPPED)
        else:
            print(f"stopped: PID {holder} on port {port} ({_command_of(holder)})")
            terminate(holder, port)

    if listener_pid(port) is not None:
        raise ServiceError(f"port {port} is still held — refusing to start a second instance",
                           EXIT_NOT_STOPPED)

    mode, pid = start_loaded_or_spawn(cfg=cfg, port=port, background=True)
    print(f"started: {'launchd job ' + lab if mode == 'job' else 'detached process'} "
          f"(pid {pid or 'unknown'}, port {port})")
    took = wait_for_dashboard(port, seconds)
    if took is None:
        detail = tail(log_file(cfg)) if cfg.log_file else ""
        raise ServiceError(
            f"the dashboard did not answer on http://127.0.0.1:{port}/ within "
            f"{seconds:.0f} s.\n"
            + (f"  last lines of {log_file(cfg)}:\n{detail}" if detail
               else f"  log: {log_file(cfg)}")
            + "\n  next: ripple status — or read the log",
            EXIT_NO_ANSWER)
    print(f"ready: the dashboard answered after {took:.1f} s "
          f"(total {time.monotonic() - started:.1f} s)")
    return EXIT_OK


# --- update ----------------------------------------------------------------

def suite_command() -> list[str]:
    """The suite `ripple update` runs, as an argv list.

    ``RIPPLE_UPDATE_SUITE`` replaces the whole line (split like a shell would
    split it) so the tests do not have to spend ~245 s per case; ``{python}`` is
    substituted with the interpreter running this CLI.
    """
    raw = os.environ.get("RIPPLE_UPDATE_SUITE") or SUITE_COMMAND
    return [part.replace("{python}", sys.executable) for part in shlex.split(raw)]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, check=False)


#: pytest's last line: "2 failed, 961 passed in 245.10s". The number that
#: matters to the operator is the failing count, and it is in that line.
def _suite_summary(output: str) -> str | None:
    """The suite's own summary line, or None when it printed none."""
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    for line in reversed(lines[-5:]):
        if re.search(r"\b\d+ (?:failed|passed|error)", line):
            return line.strip("= ").strip()
    return None


def _version_now(port: int, fallback: str) -> str:
    health = healthz(port)
    if health and health.get("version"):
        build = health.get("build") or {}
        commit = f" ({build['commit']})" if build.get("commit") else ""
        return f"{health['version']}{commit}"
    return f"{fallback} (not running)"


def cmd_update(argv: list[str]) -> int:
    """`ripple update` — fetch, run the suite, restart, report the versions.

    Order matters more than anything else here: **the running build keeps
    running** until the suite is green. A red suite prints the failing count and
    stops — no restart, no half-updated service. A dirty tree is refused before
    the fetch, because pulling into local edits is how a "green" build turns out
    to be neither the commit nor the edit.

    When the fetch brings no new commit there is nothing to verify or restart;
    the command says so and exits 0 rather than bouncing a healthy service for
    the pleasure of it. `RIPPLE_UPDATE_SUITE` overrides the suite (tests).
    """
    _parser("update", "Fetch, run the suite, restart — only if the suite is green.").parse_args(argv)
    cfg = _load()
    port = resolve_port(cfg)
    lab = label()

    if not (REPO / ".git").exists():
        raise ServiceError(f"{REPO} is not a git checkout — `ripple update` has nothing to pull",
                           EXIT_REFUSED)
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0:
        raise ServiceError(f"git status failed in {REPO}: {dirty.stderr.strip()}", EXIT_REFUSED)
    if dirty.stdout.strip():
        files = "\n".join(f"  {line}" for line in dirty.stdout.strip().splitlines()[:10])
        raise ServiceError(
            "the tree has uncommitted changes — refusing to update.\n"
            f"{files}\n"
            "  next: commit or stash them, then run `ripple update` again.\n"
            "  (untracked files such as config.yaml do not count.)",
            EXIT_REFUSED)

    before = _version_now(port, __version__)
    print(f"version before: {before}")

    # A local branch with no upstream has nothing to pull. Saying that is
    # clearer than git's "There is no tracking information for the current
    # branch", and it keeps the command useful in a checkout that is not
    # published anywhere.
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
        print(f"branch: {branch} has no upstream — nothing to fetch or pull")
        head_before = head_after = _git("rev-parse", "HEAD").stdout.strip()
    else:
        fetch = _git("fetch", "--quiet")
        if fetch.returncode != 0:
            raise ServiceError(
                f"git fetch failed: {fetch.stderr.strip() or 'no output'}\n"
                "  next: check the remote (`git -C %s remote -v`) and the network; "
                "the running build is untouched" % REPO,
                EXIT_REFUSED)
        head_before = _git("rev-parse", "HEAD").stdout.strip()
        pull = _git("pull", "--ff-only", "--quiet")
        if pull.returncode != 0:
            raise ServiceError(
                f"git pull --ff-only failed: {pull.stderr.strip() or pull.stdout.strip()}\n"
                "  next: the branch has diverged — resolve it by hand; "
                "the running build is untouched",
                EXIT_REFUSED)
        head_after = _git("rev-parse", "HEAD").stdout.strip()

    if head_before == head_after:
        print(f"commit: {head_after[:7]} — already the newest commit, nothing to restart")
        print(f"version after:  {_version_now(port, __version__)}")
        return EXIT_OK
    print(f"commit: {head_before[:7]} → {head_after[:7]}")

    cmd = suite_command()
    print(f"suite: {' '.join(cmd)}")
    try:
        suite = subprocess.run(cmd, cwd=str(REPO), check=False,
                               capture_output=True, text=True)
    except OSError as exc:
        raise ServiceError(f"could not run the suite ({cmd[0]}): {exc}", EXIT_SUITE_RED)
    output = (suite.stdout or "") + (suite.stderr or "")
    if output.strip():
        print(output.rstrip())
    if suite.returncode != 0:
        summary = _suite_summary(output) or f"exit {suite.returncode}, no summary line"
        raise ServiceError(
            f"the suite is red: {summary} — not restarting.\n"
            f"  the running build ({before}) keeps running; the new code is in the "
            f"tree but not live.\n"
            f"  next: fix the failures, or go back with `git -C {REPO} reset --hard "
            f"{head_before[:7]}`",
            EXIT_SUITE_RED)
    print(f"suite: green ({_suite_summary(output) or 'no summary line'})")

    rc = cmd_restart([])
    if rc != EXIT_OK:
        return rc
    print(f"version after:  {_version_now(port, __version__)}")
    return EXIT_OK


# --- dispatch --------------------------------------------------------------

def _load(path: Path | None = None) -> Config:
    """The configuration, or a ServiceError that names the file and the next step."""
    path = path or config_path()
    try:
        return load_config(str(path))
    except ConfigError as exc:
        raise ServiceError(
            f"configuration error in {path}: {exc}\n"
            f"  next: copy config.example.yaml to config.yaml and adapt it, "
            f"or point RIPPLE_CONFIG at the right file",
            EXIT_CONFIG)


HANDLERS: dict[str, Callable[[list[str]], int]] = {
    "start": cmd_start,
    "status": cmd_status,
    "shutdown": cmd_shutdown,
    "restart": cmd_restart,
    "update": cmd_update,
}

#: Words that mean "start", so `ripple` alone is not a special case in dispatch.
START_WORDS = ("", "start")

#: Words that cannot honour `-perm`. A flag that would silently do nothing is
#: worse than a flag that is refused: `ripple restart -perm` reads like "restart
#: it and keep it running", and it would do neither.
PERM_REFUSED = {
    "start": "`ripple` starts the proxy in this terminal; autostart is `ripple -perm`",
    "status": "`ripple status` only reports; autostart is `ripple -perm`",
    "restart": "`ripple restart` restarts the running proxy; it does not change "
               "autostart — install the job with `ripple -perm`",
    "update": "`ripple update` fetches, tests and restarts; it does not change "
              "autostart — install the job with `ripple -perm`",
}


def _perm_error(word: str) -> int:
    print(f"ripple: {PERM_FLAG} cannot be combined with `{word or 'ripple'}`: "
          f"{PERM_REFUSED.get(word or 'start')}\n"
          f"  {PERM_FLAG} alone switches autostart on, `shutdown {PERM_FLAG}` "
          f"switches it off", file=sys.stderr)
    return EXIT_CONFIG


def main(argv: list[str] | None = None) -> int:
    """`ripple [-perm | start|status|shutdown|restart|update]` — the service commands."""
    raw = list(sys.argv[1:] if argv is None else argv)
    # No word at all is `start`: `ripple` alone must keep meaning "run it here",
    # which is what it meant before this module existed.
    word = raw[0] if raw and not raw[0].startswith("-") else ""
    perm = PERM_FLAG in (raw[1:] if word else raw)
    if word and word not in HANDLERS:
        print(f"ripple: unknown service command {word!r}\n"
              f"  known: start, status, shutdown, restart, update", file=sys.stderr)
        return EXIT_CONFIG
    if perm and word not in ("", "shutdown"):
        return _perm_error(word)
    if perm and word == "":
        # `ripple -perm` — autostart on. The flag is the whole command, so it is
        # removed before argparse sees it; anything else on the line is refused
        # there, by name.
        handler, tail = cmd_autostart, [arg for arg in raw if arg != PERM_FLAG]
    else:
        handler = HANDLERS[word or "start"]
        tail = raw[1:] if word else raw
    try:
        return handler(tail)
    except ServiceError as exc:
        print(f"ripple: {exc.message}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("\nripple: interrupted", file=sys.stderr)
        return 130
