"""`ripple doctor` — one command that says what is missing (issue #63).

Why this module exists: `deploy/verify-ripple.command` answered "is this machine
healthy?" for the person who built it — a fixed port, a fixed key path, a shell
script that assumes the checkout it sits in and the operator's own habits.
Someone who installs RIPPLE from a bundle has none of that: they do not know
which file is missing, whether the port is taken by something else, or whether
the configured nodes answer at all.

Doctor walks the same ground inside the program: one line per check, `OK` /
`WARN` / `FAIL` (and `SKIP` where a check cannot run), a short reason an
outsider can act on, and a summary with an exit code a script can read.

What it is *not*: a second configuration parser. Validation stays in
`ripple/config.py`, reached through `load_config` — the same call `ripple
--check` (`__main__.run_check`) makes — and the node probes use the real
`OmlxBackend`, the same code the proxy uses. Doctor only asks the questions
`--check` does not: is the port free, does the key store have the right mode,
does a real completion come back.

Exit code 0 when nothing is FAIL. A WARN is a working installation with a
limitation: no admin rights on a node is the normal case for someone who does
not hold oMLX's own key, and it must not read as a broken install.

No secret ever reaches the output: a key is named by its path and its mode,
never by its value (FA-15).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from . import __version__
from .backends import Backend, Health, OmlxBackend, OpenAICompatibleBackend
from .config import Config, ConfigError, HostConfig, load_config
from .hosts import ordered_addresses
from .keys import KeyStore, KeyStoreError
from .sites import is_local_address, network_summary

# The floor measured on the cold-start machine (Python 3.9.6: every module
# imports). Deliberately a constant and not read from `pyproject.toml`: a bundle
# or a plain checkout may not carry the file, and a doctor that cannot answer
# its first question without a build file is not a doctor. #60 writes the same
# number into `requires-python`.
MIN_PYTHON = (3, 9)

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

_RULE = "─" * 72
# The longest check name is "completion" — keep the column readable for a
# terminal and stable for a diff.
_NAME_WIDTH = 10
_STATUS_WIDTH = 5


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
@dataclass
class Check:
    """One line of the report. `name` is stable, the reason is for a human."""

    name: str
    status: str
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"check": self.name, "status": self.status,
                               "reason": self.reason}
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass
class Report:
    config_path: str = ""
    url: str = ""
    checks: List[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def count(self, status: str) -> int:
        return sum(1 for c in self.checks if c.status == status)

    @property
    def failed(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": __version__,
            "config": self.config_path,
            "url": self.url,
            "checks": [c.as_dict() for c in self.checks],
            "summary": {
                "checks": len(self.checks),
                "ok": self.count(OK),
                "warn": self.count(WARN),
                "fail": self.count(FAIL),
                "skipped": self.count(SKIP),
            },
            "exit_code": self.exit_code,
        }


def render(report: Report) -> str:
    """The text report: header, one line per check, summary."""
    lines = [
        f"RIPPLE {__version__} — doctor",
        f"config: {report.config_path}   proxy: {report.url}",
        _RULE,
    ]
    for check in report.checks:
        lines.append(f"{check.status:<{_STATUS_WIDTH}}{check.name:<{_NAME_WIDTH}} "
                     f"{check.reason}")
    lines.append(_RULE)
    summary = (f"{len(report.checks)} checks: {report.count(OK)} OK, "
               f"{report.count(WARN)} WARN, {report.count(FAIL)} FAIL")
    if report.count(SKIP):
        summary += f", {report.count(SKIP)} skipped"
    lines.append(summary)
    if report.failed:
        lines.append(f"Result: FAILED — {len(report.failed)} check(s) need attention.")
    elif report.count(WARN):
        lines.append("Result: OK — warnings only, nothing is broken.")
    else:
        lines.append("Result: OK — nothing to fix.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _headers(api_key: str = "") -> Dict[str, str]:
    """Accept JSON, and the key only when there is one (never an empty bearer)."""
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def _ms(value: Optional[float]) -> str:
    return f"{value:.1f} ms" if value is not None else "? ms"


def _short(response: httpx.Response, limit: int = 120) -> str:
    """A response body short enough for one line — the reason, not a dump."""
    text = " ".join((response.text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _host_port(url: str) -> Tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port:
        return host, parsed.port
    return host, 443 if parsed.scheme == "https" else 80


def _is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")


def _listening(host: str, port: int, timeout: float) -> bool:
    """Is something accepting connections there?

    A connect attempt instead of `bind()`: it answers the question doctor
    actually has ("is a RIPPLE reachable here?") and needs no privileges, while
    `bind()` would have to guess whether `0.0.0.0` and `127.0.0.1` collide.
    """
    try:
        with socket.create_connection((host, port), timeout=min(timeout, 3.0)):
            return True
    except OSError:
        return False


def _port_owner(port: int) -> Optional[str]:
    """`name (pid N)` of the local process listening on `port`, or None.

    `lsof`/`ps` are the only dependency-free way to name a foreign process on
    macOS and Linux. When they are missing the caller says "a process this tool
    could not identify" — a FAIL without a PID is still a FAIL.
    """
    try:
        found = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                               capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [line.strip() for line in (found.stdout or "").splitlines()
            if line.strip().isdigit()]
    if not pids:
        return None
    pid = pids[0]
    name = ""
    try:
        ps = subprocess.run(["ps", "-p", pid, "-o", "comm="],
                            capture_output=True, text=True, timeout=5.0)
        name = (ps.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return f"{name or 'unknown process'} (pid {pid})"


def _backend_for(host: HostConfig, address: str, client: httpx.AsyncClient,
                 timeout: float) -> Backend:
    if host.backend == "openai_compatible":
        return OpenAICompatibleBackend(address, host.api_key, client=client,
                                       timeout=timeout)
    return OmlxBackend(address, host.api_key, client=client, timeout=timeout)


# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
def _check_python() -> Check:
    have = sys.version_info
    text = f"{have.major}.{have.minor}.{have.micro}"
    need = ".".join(str(n) for n in MIN_PYTHON)
    if (have.major, have.minor) >= MIN_PYTHON:
        return Check("python", OK, f"{text} (requires >= {need})",
                     {"version": text, "required": need})
    return Check("python", FAIL,
                 f"{text} is older than the required {need} — "
                 f"install a newer interpreter and recreate the venv",
                 {"version": text, "required": need})


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------
def _check_config(path: str) -> Tuple[Check, Optional[Config]]:
    """Validate through `load_config` — the same call `ripple --check` makes.

    A second parser here would drift from the one the proxy starts with, and
    then doctor would bless a file the proxy refuses. `run_check` itself is not
    invoked: it starts a Fleet and prints the routing table, which is the
    `--check` report, not a list of gaps. The validation both paths share is
    `load_config`, and `cfg.warnings` is what it collected.
    """
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        message = str(exc)
        if str(Path(path).expanduser()) not in message:
            message = f"{path}: {message}"
        return Check("config", FAIL,
                     f"{message} (the full report is `python -m ripple "
                     f"--config {path} --check`)", {"path": path}), None
    except OSError as exc:
        return Check("config", FAIL, f"{path}: {exc}", {"path": path}), None

    hosts = len(cfg.hosts)
    if cfg.warnings:
        more = (f" (+{len(cfg.warnings) - 1} more, see --json)"
                if len(cfg.warnings) > 1 else "")
        return Check("config", WARN,
                     f"{path} — {hosts} host(s), warning: {cfg.warnings[0]}{more}",
                     {"path": path, "hosts": hosts,
                      "warnings": list(cfg.warnings)}), cfg
    return Check("config", OK, f"{path} — {hosts} host(s), no warnings",
                 {"path": path, "hosts": hosts}), cfg


def _check_rebind(cfg: Config) -> Check:
    """Can `placement.rebind` price a move at all? (issue #81)

    A move costs one cold prefill on the destination, priced as
    `prompt_tokens / pp_tps` (`placement.plan_rebind`). A host without a rate —
    `hosts[].pp_tps` unset and `placement.rebind.default_pp_tps: 0`, which means
    *not measured*, not *infinitely fast* — is skipped by the loop, so rebinding
    is on and can never fire. Nothing raises, nothing logs: the feature is dead
    and the installation looks healthy.

    Not a `FAIL`: routing works, only the balancing between nodes does not
    happen. `SKIP` when rebinding is off (nothing is silently missing), `OK`
    when at least one enabled node carries a rate and can be a destination.
    """
    rebind = cfg.placement.rebind
    if not rebind.enabled:
        return Check("rebind", SKIP,
                     "placement.rebind.enabled is false — no move to price")
    priced, unpriced = cfg.rebind_prefill_rates()
    if priced:
        detail: Dict[str, Any] = {"priced": priced}
        if unpriced:
            detail["unpriced"] = unpriced
            reason = (f"{', '.join(priced)} can be priced; "
                      f"{', '.join(unpriced)} has no prefill rate and cannot be "
                      f"a destination")
        else:
            reason = (f"{', '.join(priced)} can be priced — every enabled node "
                      f"has a prefill rate")
        return Check("rebind", OK, reason, detail)
    finding = cfg.rebind_cannot_fire()
    assert finding is not None            # priced is empty, so this is the case
    return Check("rebind", WARN, finding,
                 {"field": "hosts[].pp_tps", "unpriced": unpriced,
                  "default_pp_tps": rebind.default_pp_tps})


# ---------------------------------------------------------------------------
# 3. Port
# ---------------------------------------------------------------------------
async def _check_port(client: httpx.AsyncClient, url: str,
                      timeout: float) -> Tuple[Check, bool]:
    """Free, serving RIPPLE, or held by someone else.

    Returns the check and whether a RIPPLE answers at `url` — the two later
    checks (dashboard, completion) need that answer to decide between a real
    check and a `SKIP`.
    """
    host, port = _host_port(url)
    base = url.rstrip("/")
    if not _listening(host, port, timeout):
        return Check("port", OK,
                     f"port {port} on {host} is free — RIPPLE can bind it",
                     {"port": port}), False

    payload: Optional[Dict[str, Any]] = None
    try:
        response = await client.get(base + "/healthz", timeout=timeout)
        if response.status_code == 200:
            data = response.json()
            if (isinstance(data, dict) and data.get("ok") is True
                    and "hosts" in data and "capabilities" in data):
                payload = data
    except (httpx.HTTPError, ValueError, OSError):
        payload = None

    if payload is not None:
        version = str(payload.get("version") or __version__)
        return Check("port", OK,
                     f"port {port} is already serving RIPPLE {version}",
                     {"port": port, "ripple": True}), True

    owner: Optional[str] = None
    if _is_local(host):
        owner = _port_owner(port)
        who = owner or "a process this tool could not identify"
    else:
        who = "another process on that host"
    return Check("port", FAIL,
                 f"port {port} is held by {who} — RIPPLE cannot bind it",
                 {"port": port, "owner": owner}), False


# ---------------------------------------------------------------------------
# 4-6. Nodes: reachable, identity, admin rights
# ---------------------------------------------------------------------------
@dataclass
class NodeProbe:
    """What one configured host answered. `health is None` means unreachable."""

    host: HostConfig
    address: Optional[str] = None
    backend: Optional[Backend] = None
    health: Optional[Health] = None
    error: Optional[str] = None
    node_id: Optional[str] = None
    admin_status: Optional[int] = None
    admin_error: Optional[str] = None

    @property
    def reached(self) -> bool:
        return self.health is not None


async def _probe_host(host: HostConfig, client: httpx.AsyncClient,
                timeout: float) -> NodeProbe:
    """Walk the address list in order; the first address that answers wins.

    The order is `hosts.ordered_addresses` — the same one the proxy uses, so the
    address this report calls "the one RIPPLE reaches the node on" is the address
    it really uses (issue #67: the site is derived from that address). The
    identity filter is left out on purpose: doctor wants to *report* a node_id
    mismatch, so an address that answers is the right address even when its
    identity is wrong.
    """
    probe = NodeProbe(host=host)
    if not host.enabled:
        return probe
    for address in ordered_addresses(host.addresses):
        backend = _backend_for(host, address, client, timeout)
        health = await backend.health()
        if health.error is None:
            probe.address, probe.backend, probe.health = address, backend, health
            break
        probe.error = f"{address}: {health.error}"
    if probe.backend is None or probe.address is None:
        return probe

    if isinstance(probe.backend, OmlxBackend):
        probe.node_id = await probe.backend.identity()
        await _probe_admin(probe, client, timeout)
    return probe


async def _probe_admin(probe: NodeProbe, client: httpx.AsyncClient,
                       timeout: float) -> None:
    """Ask one admin route. The status is the answer; the key is never echoed."""
    assert probe.address is not None
    try:
        response = await client.get(probe.address.rstrip("/") + "/admin/api/activity",
                                    headers=_headers(probe.host.api_key),
                                    timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        probe.admin_error = f"{type(exc).__name__}: {exc}"
        return
    probe.admin_status = response.status_code


def _node_checks(probes: List[NodeProbe]) -> List[Check]:
    """Three groups, in the order of the issue: reach, identity, admin."""
    reach: List[Check] = []
    identity: List[Check] = []
    admin: List[Check] = []

    for probe in probes:
        name = probe.host.name
        if not probe.host.enabled:
            for group, group_name in ((reach, "node"), (identity, "identity"),
                                      (admin, "admin")):
                group.append(Check(group_name, SKIP,
                                   f"{name}: disabled in the config"))
            continue

        # 4. reachable, with the measured latency
        if probe.health is None:
            reach.append(Check("node", FAIL,
                               f"{name}: unreachable — {probe.error or 'no address answered'}",
                               {"host": name, "address": probe.host.addresses[0]}))
        elif probe.health.healthy:
            reach.append(Check("node", OK,
                               f"{name}: {probe.address} answered in "
                               f"{_ms(probe.health.rtt_ms)}",
                               {"host": name, "address": probe.address,
                                "rtt_ms": probe.health.rtt_ms}))
        else:
            reach.append(Check("node", WARN,
                               f"{name}: {probe.address} answered in "
                               f"{_ms(probe.health.rtt_ms)} but reports not healthy",
                               {"host": name, "address": probe.address,
                                "rtt_ms": probe.health.rtt_ms}))

        # 5. the node_id the config believes in
        if probe.health is None:
            identity.append(Check("identity", SKIP,
                                  f"{name}: not checked, node unreachable"))
        elif not isinstance(probe.backend, OmlxBackend):
            identity.append(Check("identity", SKIP,
                                  f"{name}: backend {probe.host.backend} has no "
                                  f"node_id endpoint"))
        elif not probe.host.node_id:
            identity.append(Check("identity", WARN,
                                  f"{name}: no node_id in the config — the address "
                                  f"counts as identity (RB-9)"))
        elif probe.node_id is None:
            identity.append(Check("identity", WARN,
                                  f"{name}: node_id unreadable on {probe.address} — "
                                  f"cannot verify the config"))
        elif probe.node_id == probe.host.node_id:
            identity.append(Check("identity", OK,
                                  f"{name}: node_id {probe.node_id} matches the config",
                                  {"host": name, "node_id": probe.node_id}))
        else:
            identity.append(Check("identity", FAIL,
                                  f"{name}: node reports node_id {probe.node_id}, "
                                  f"the config says {probe.host.node_id}",
                                  {"host": name, "seen": probe.node_id,
                                   "configured": probe.host.node_id}))

        # 6. admin rights — 401/403 is the normal case, not a failure
        if probe.health is None:
            admin.append(Check("admin", SKIP,
                               f"{name}: not checked, node unreachable"))
        elif not isinstance(probe.backend, OmlxBackend):
            admin.append(Check("admin", SKIP,
                               f"{name}: backend {probe.host.backend} has no admin API"))
        elif probe.admin_status is None:
            admin.append(Check("admin", WARN,
                               f"{name}: admin endpoint did not answer "
                               f"({probe.admin_error or 'no response'})"))
        elif probe.admin_status == 200:
            admin.append(Check("admin", OK,
                               f"{name}: admin rights (HTTP 200 on /admin/api/activity)"))
        elif probe.admin_status in (401, 403):
            admin.append(Check("admin", WARN,
                               f"{name}: read-only, no admin rights "
                               f"(HTTP {probe.admin_status}) — set hosts[].api_key "
                               f"to oMLX's own key for admin features"))
        else:
            admin.append(Check("admin", WARN,
                               f"{name}: /admin/api/activity answered HTTP "
                               f"{probe.admin_status}"))
    return reach + identity + admin


# ---------------------------------------------------------------------------
# 7. The declared site against the address that answers (issue #67)
# ---------------------------------------------------------------------------
def _site_checks(cfg: Config, probes: List[NodeProbe]) -> List[Check]:
    """Is each node where the configuration says it is?

    The claim is `hosts[].site`; the fact is the address that actually answered
    (`probe.address`, the same address the proxy would use). Three answers, and
    they are different things:

    * the address is inside the declared site's networks — OK, and the line says
      which network confirmed it;
    * the address is inside *another* site's networks — one WARN naming both
      sites. This is the case of the M5 that declares `SA` and is reached over
      `198.51.100.49`, which belongs to the site-b site;
    * the address is in no network at all — one WARN naming the site and its
      networks. The claim is not contradicted, it is unconfirmed, and the
      operator usually forgot a network.

    And one case that is not a comparison at all (issue #76): a node reached over
    loopback is *this* machine. Loopback is in no site's networks — and putting
    it there would make every site contain every machine — so the line is `SKIP`
    and it says that, whether or not a `site:` was declared. Reporting `OK` here
    would claim a check that compared nothing; reporting `WARN` would blame the
    operator for the normal single-machine install that `ripple init` writes.

    Nothing here touches placement: the site is a location, not a routing rule
    (issue #67 point 5).
    """
    if not cfg.sites and not any(p.host.site for p in probes):
        return [Check("site", SKIP,
                      "no site declared — nothing to check (a site is a name plus "
                      "the networks it contains)")]

    checks: List[Check] = []
    for probe in probes:
        host = probe.host
        name = host.name
        declared = cfg.site_of(host)
        if host.site and declared is None:
            checks.append(Check("site", WARN,
                                f"{name}: site {host.site!r} is not defined under "
                                f"'sites' — the declaration cannot be checked",
                                {"host": name, "site": host.site}))
            continue
        if declared is None:
            if is_local_address(probe.address):
                checks.append(Check("site", SKIP,
                                    f"{name}: reached over loopback "
                                    f"({probe.address}) — this machine; a site "
                                    f"names networks and loopback is in none, so "
                                    f"nothing was compared",
                                    {"host": name, "address": probe.address,
                                     "local": True}))
            else:
                checks.append(Check("site", SKIP, f"{name}: no site declared"))
            continue
        if probe.address is None:
            checks.append(Check("site", SKIP,
                                f"{name}: not checked, node unreachable"))
            continue
        if is_local_address(probe.address):
            # Not contradicted, only unconfirmable — and the normal single-machine
            # install, so a WARN would be blaming the operator (issue #76).
            checks.append(Check("site", SKIP,
                                f"{name}: declares site {declared.label} "
                                f"({declared.id}), but is reached over loopback "
                                f"({probe.address}) — this machine; loopback is in "
                                f"no network, so the claim stays unconfirmed",
                                {"host": name, "address": probe.address,
                                 "declared": declared.id, "local": True}))
            continue
        derived = cfg.site_for_address(probe.address)
        if derived is None:
            checks.append(Check("site", WARN,
                                f"{name}: declares site {declared.label} "
                                f"({declared.id}), but {probe.address} is in none of "
                                f"its networks ({network_summary(declared)})",
                                {"host": name, "address": probe.address,
                                 "declared": declared.id, "derived": None}))
        elif derived.id == declared.id:
            checks.append(Check("site", OK,
                                f"{name}: {probe.address} is in {declared.label} "
                                f"({declared.id}), as declared",
                                {"host": name, "address": probe.address,
                                 "declared": declared.id, "derived": derived.id}))
        else:
            checks.append(Check("site", WARN,
                                f"{name}: declares site {declared.label} "
                                f"({declared.id}), but {probe.address} is reached in "
                                f"{derived.label} ({derived.id}) — the node is not "
                                f"where the configuration says",
                                {"host": name, "address": probe.address,
                                 "declared": declared.id, "derived": derived.id}))
    return checks


# ---------------------------------------------------------------------------
# 8. Key store
# ---------------------------------------------------------------------------
def _check_keys(cfg: Config) -> Tuple[Check, Optional[KeyStore]]:
    """Exists, mode 0600, parses through the real `KeyStore`, admin key set.

    Missing file: WARN when auth is on (every client is refused until a key
    exists), OK when auth is off (nothing needs a store). Corrupt file: FAIL —
    the proxy itself refuses to start on it. No admin key: WARN, the `/admin`
    routes are simply locked.
    """
    path = Path(cfg.auth.keys_file).expanduser()
    label = str(path)

    if path.exists() and not path.is_file():
        return Check("keys", FAIL, f"{label} — not a regular file",
                     {"path": label}), None
    try:
        store = KeyStore(path, cfg.auth.admin_key)
    except KeyStoreError as exc:
        return Check("keys", FAIL, str(exc), {"path": label}), None

    if not path.exists():
        admin = ("admin key set" if store.admin_key_set
                 else "no admin key either (/admin stays locked)")
        if cfg.auth.enabled:
            return Check("keys", WARN,
                         f"{label} — missing; every client key is refused until "
                         f"one is created, {admin}", {"path": label}), store
        return Check("keys", OK,
                     f"{label} — missing, but auth is off (no key store needed), "
                     f"{admin}", {"path": label}), store

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return Check("keys", WARN, f"{label} — mode unreadable ({exc})",
                     {"path": label}), store

    keys = len(store.keys)
    parts = [f"{label} — {keys} key(s)", f"mode {mode:04o}"]
    status = OK
    if store.admin_key_set:
        parts.append("admin key set")
    else:
        status = WARN
        parts.append("no admin key (/admin stays locked)")
    reason = ", ".join(parts)
    if mode != 0o600:
        status = WARN
        reason += f" — should be 0600: chmod 600 {label}"
    return Check("keys", status, reason,
                 {"path": label, "mode": f"{mode:04o}", "keys": keys,
                  "admin_key": store.admin_key_set}), store


# ---------------------------------------------------------------------------
# 9. Dashboard
# ---------------------------------------------------------------------------
async def _check_dashboard(client: httpx.AsyncClient, url: str, serving: bool,
                           timeout: float) -> Check:
    base = url.rstrip("/")
    if not serving:
        return Check("dashboard", SKIP, f"no RIPPLE on {base} — skipped")
    try:
        health = await client.get(base + "/healthz", timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        return Check("dashboard", FAIL,
                     f"{base}/healthz did not answer ({type(exc).__name__}: {exc})")
    if health.status_code != 200:
        return Check("dashboard", FAIL,
                     f"{base}/healthz answered HTTP {health.status_code}")
    try:
        payload = health.json()
    except ValueError:
        return Check("dashboard", FAIL,
                     f"{base}/healthz answered 200 but not JSON — this is not RIPPLE")
    if (not isinstance(payload, dict) or "hosts" not in payload
            or "capabilities" not in payload):
        return Check("dashboard", FAIL,
                     f"{base}/healthz is not RIPPLE's payload "
                     f"(no hosts/capabilities)")

    try:
        page = await client.get(base + "/", timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        return Check("dashboard", FAIL,
                     f"{base}/ did not answer ({type(exc).__name__}: {exc})")
    if page.status_code != 200:
        return Check("dashboard", FAIL,
                     f"{base}/ answered HTTP {page.status_code}")
    html = page.text or ""
    if "<html" not in html.lower() or "ripple" not in html.lower():
        return Check("dashboard", FAIL,
                     f"{base}/ answered 200 but not the dashboard page "
                     f"({len(html)} bytes)")
    hosts = [h for h in (payload.get("hosts") or []) if isinstance(h, dict)]
    healthy = sum(1 for h in hosts if h.get("status") == "healthy")
    return Check("dashboard", OK,
                 f"{base}: /healthz 200, dashboard page {len(html) / 1024:.1f} kB, "
                 f"{healthy}/{len(hosts)} node(s) healthy",
                 {"bytes": len(html), "hosts": len(hosts), "healthy": healthy})


# ---------------------------------------------------------------------------
# 10. A real completion
# ---------------------------------------------------------------------------
def _client_token(cfg: Optional[Config], store: Optional[KeyStore]) -> str:
    """The first enabled client key, so doctor can call an auth-protected proxy.

    Used, never printed: it travels in one request header and is not part of
    the report, the JSON or any log line (FA-15).
    """
    if cfg is None or store is None or not cfg.auth.enabled:
        return ""
    for key in store.keys:
        if key.enabled:
            return key.key
    return ""


# The completion probe: one word asked for, the word required back (issue #82).
#
# `max_tokens` is measured, not rounded. Against the live fleet's reasoning
# model (DeepSeek-V4.1-Flash) a five-token budget went entirely into the
# reasoning channel — `content: ""`, `finish_reason: "length"`, 26-43
# characters of reasoning — and the same held at 6, 8 and 10 tokens (0 of 3
# runs visible at each). At 12 one run of three produced the word; from 16 on
# every run did. 16 is where the text starts appearing; 64 is where it keeps
# appearing: 43 runs of 43 returned `ok`, using 9-26 completion tokens and
# stopping by itself. 32 was enough in 18 of 19 — that one empty answer is why
# the probe does not sit at "twice the worst case": a false finding at every
# start costs more than 48 tokens nobody spends, because a model that stops
# early never uses the budget (a non-reasoning model, Qwen3.5-9B, answers `ok`
# in one token at 5 or at 64).
_PROBE_PROMPT = "Reply with the single word ok."
_PROBE_WORD = "ok"
_PROBE_MAX_TOKENS = 64


async def _check_completion(client: httpx.AsyncClient, url: str,
                            cfg: Optional[Config], store: Optional[KeyStore],
                            serving: bool, timeout: float) -> Check:
    base = url.rstrip("/")
    if not serving:
        return Check("completion", SKIP, f"no RIPPLE on {base} — skipped")
    headers = _headers(_client_token(cfg, store))

    try:
        listing = await client.get(base + "/v1/models", headers=headers,
                                   timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        return Check("completion", FAIL,
                     f"/v1/models did not answer ({type(exc).__name__}: {exc})")
    if listing.status_code == 401:
        return Check("completion", FAIL,
                     "HTTP 401 on /v1/models — auth is on and no usable client "
                     "key was found")
    if listing.status_code != 200:
        return Check("completion", FAIL,
                     f"/v1/models answered HTTP {listing.status_code}")
    try:
        payload = listing.json()
    except ValueError:
        return Check("completion", FAIL, "/v1/models answered 200 but not JSON")
    models = [str(m["id"]) for m in (payload.get("data") or [])
              if isinstance(m, dict) and m.get("id")]
    if not models:
        return Check("completion", FAIL,
                     "no routable model — no configured node offers one")

    model = models[0]
    body = {"model": model,
            "messages": [{"role": "user", "content": _PROBE_PROMPT}],
            "max_tokens": _PROBE_MAX_TOKENS}
    started = time.perf_counter()
    try:
        response = await client.post(base + "/v1/chat/completions", json=body,
                                     headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return Check("completion", FAIL,
                     f"{model}: no answer within {timeout:g} s — the node may "
                     f"still be loading it")
    except (httpx.HTTPError, OSError) as exc:
        return Check("completion", FAIL,
                     f"{model}: request failed ({type(exc).__name__}: {exc})")
    elapsed = (time.perf_counter() - started) * 1000.0
    if response.status_code != 200:
        return Check("completion", FAIL,
                     f"{model}: HTTP {response.status_code} — {_short(response)}")
    try:
        completed = response.json()
    except ValueError:
        completed = None
    if not isinstance(completed, dict) or not completed.get("choices"):
        return Check("completion", FAIL,
                     f"{model}: HTTP 200 but no choices in the answer")
    host = response.headers.get("x-ripple-host") or ""
    # Read the answer, not just its envelope (issue #82): a node that returns
    # `choices` with an empty message is the failure the cold start hit, and it
    # looks exactly like a healthy node to a check that only counts choices.
    choice = completed["choices"][0]
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message") or {}
    raw = message.get("content")
    text = raw.strip() if isinstance(raw, str) else ""
    finish = str(choice.get("finish_reason") or "")
    detail = {"model": model, "ms": round(elapsed, 1), "host": host,
              "finish_reason": finish, "content_chars": len(text)}
    if not text:
        if finish == "length":
            return Check("completion", FAIL,
                         f"{model}: the node answers, but the answer is empty — "
                         f"a reasoning model spent the whole "
                         f"{_PROBE_MAX_TOKENS}-token budget on its reasoning "
                         f"channel; raise max_tokens or clients see empty "
                         f"replies", detail)
        return Check("completion", FAIL,
                     f"{model}: the node answers with an empty message "
                     f"(finish_reason {finish or 'missing'})", detail)
    reason = f"{model} answered in {elapsed:.0f} ms" + (f" via {host}" if host else "")
    if _PROBE_WORD not in text.lower():
        # Text came back, so the node serves — it just did not answer the probe.
        return Check("completion", WARN,
                     f"{reason} — but not with {_PROBE_WORD!r}: {text[:40]!r}",
                     detail)
    return Check("completion", OK, f"{reason} ({text[:20]!r})", detail)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def diagnose(config_path: str, url: str, timeout: float) -> Report:
    report = Report(config_path=config_path, url=url)
    report.add(_check_python())

    config_check, cfg = _check_config(config_path)
    report.add(config_check)
    if cfg is not None:
        # Configuration-derived, no probe needed (issue #81): does anything in
        # this fleet give a move a price at all?
        report.add(_check_rebind(cfg))

    async with httpx.AsyncClient(timeout=timeout) as client:
        port_check, serving = await _check_port(client, url, timeout)
        report.add(port_check)

        store: Optional[KeyStore] = None
        if cfg is not None:
            # Concurrently: one dead node must not add its timeout to the next.
            probes = await asyncio.gather(*[_probe_host(host, client, timeout)
                                            for host in cfg.hosts])
            for check in _node_checks(list(probes)):
                report.add(check)
            # The declared site against the address that actually answered
            # (issue #67) — after the probes, because the address is their fact.
            for check in _site_checks(cfg, list(probes)):
                report.add(check)
            keys_check, store = _check_keys(cfg)
            report.add(keys_check)
        else:
            report.add(Check("rebind", SKIP,
                             "no configuration — nothing to price"))
            report.add(Check("node", SKIP, "no configuration — nothing to probe"))
            report.add(Check("identity", SKIP,
                             "no configuration — nothing to verify"))
            report.add(Check("admin", SKIP, "no configuration — nothing to ask"))
            report.add(Check("site", SKIP,
                             "no configuration — no declared site to check"))
            report.add(Check("keys", SKIP, "no configuration — no key store path"))

        report.add(await _check_dashboard(client, url, serving, timeout))
        report.add(await _check_completion(client, url, cfg, store, serving, timeout))
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ripple doctor",
        description="Check an installation of RIPPLE: one line per check, exit 0 "
                    "when nothing is FAIL.")
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="path to the YAML configuration (default: config.yaml)")
    parser.add_argument("--url", default="http://127.0.0.1:8010",
                        help="base URL of the running proxy "
                             "(default: http://127.0.0.1:8010)")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON instead of lines")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="seconds per network check (default: 10)")
    args = parser.parse_args(argv)

    report = asyncio.run(diagnose(args.config, args.url, args.timeout))
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
