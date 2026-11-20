"""`ripple init` — first-run configuration for a machine that has nothing (issue #62).

A stranger clones the repository, installs it and runs `ripple init`; afterwards
there is a `config.yaml` and a key store that the proxy can actually start with.
Everything in this module serves that one sentence:

* **The `--yes` path is the same code as the conversation.** Both are a
  `Prompter` handed to `run()`; `--yes` only swaps in `AutoPrompter`, which
  answers every question with its default. A second implementation would mean
  the cold-start test (`tools/coldstart.sh`, `--yes`) tests something other than
  what a human gets — and the two would drift apart silently.
* **A busy port is not a dead end.** The port is probed, the holder is named
  (PID and process name from `lsof`), and the next free port is offered.
* **No oMLX key is a valid answer.** The node is then read-limited (its model
  list answers 401) and `ripple doctor` says so; `init` writes a configuration
  without an `api_key` and does not pretend otherwise.
* **Nothing is overwritten without `--force`**, and the refusal names the exact
  path. Both files are `0600` and their parents are created — one of them is a
  list of secrets.
* **The client key is shown exactly once.** After that only
  `ClientKey.masked_key()` is ever printed (FA-15). The generated admin key is
  never printed at all: it lives in `config.yaml` at `auth.admin_key`.

Discovery (issue #61) is imported lazily inside the functions that need it, so
this module imports and runs on a tree where `ripple/discover.py` is not merged
yet; `--host` then supplies the addresses directly.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from .sites import SiteConfig, ip_of, parse_networks

DEFAULT_PORT = 8010
DEFAULT_ADDRESS = "http://127.0.0.1:8000"
CLIENT_KEY_NAME = "first-client"
FILE_MODE = 0o600

_HEADER = """\
# RIPPLE — configuration written by `ripple init`.
#
# Layer A of the data model: hand-maintained, persistent, never written back by
# the proxy. Changing a value means editing this file and restarting (FA-3) —
# a weight that changed under a running thread would move a conversation away
# from its prefix cache.
#
# Start:     ripple --config {path}
# Validate:  ripple --config {path} --check
# Dashboard: http://127.0.0.1:{port}/
"""


class Cancelled(Exception):
    """The operator stopped answering (EOF or Ctrl-C). Nothing has been written."""


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
class Prompter:
    """The question/answer surface `run()` talks to.

    Deliberately tiny: three shapes of question. `--yes` is a `Prompter` too, so
    there is exactly one flow function and no second implementation to drift.
    """

    questions: list[str]

    def text(self, label: str, default: str = "") -> str:
        raise NotImplementedError

    def yes_no(self, label: str, default: bool = True) -> bool:
        raise NotImplementedError

    def secret(self, label: str, default: str = "") -> str:
        raise NotImplementedError


class AutoPrompter(Prompter):
    """Answers every question with its default — the `--yes` path.

    It prints what it decided, so the transcript of a `--yes` run says which
    values were chosen instead of silently producing a file. A secret is never
    echoed, not even here: `secret()` prints only whether one was supplied.
    """

    def __init__(self, out: Callable[[str], None] = print) -> None:
        self._out = out
        self.questions = []

    def text(self, label: str, default: str = "") -> str:
        self.questions.append(label)
        self._out(f"{label}: {default if default else '(empty)'}")
        return default

    def yes_no(self, label: str, default: bool = True) -> bool:
        self.questions.append(label)
        self._out(f"{label}: {'yes' if default else 'no'}")
        return default

    def secret(self, label: str, default: str = "") -> str:
        self.questions.append(label)
        self._out(f"{label}: {'(supplied)' if default else '(none)'}")
        return default


class ConsolePrompter(Prompter):
    """The conversation. `input_fn`/`secret_fn` are injectable so a test can
    drive this exact path without a terminal."""

    def __init__(self, input_fn: Callable[[str], str] = input,
                 secret_fn: Callable[[str], str] = getpass.getpass) -> None:
        self._input = input_fn
        self._secret_fn = secret_fn
        self.questions = []

    def _ask(self, prompt: str) -> str:
        try:
            return self._input(prompt)
        except EOFError:
            raise Cancelled() from None

    def text(self, label: str, default: str = "") -> str:
        self.questions.append(label)
        suffix = f" [{default}]" if default else ""
        raw = (self._ask(f"{label}{suffix}: ") or "").strip()
        return raw or default

    def yes_no(self, label: str, default: bool = True) -> bool:
        self.questions.append(label)
        hint = "Y/n" if default else "y/N"
        raw = (self._ask(f"{label} [{hint}]: ") or "").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes", "j", "ja")

    def secret(self, label: str, default: str = "") -> str:
        self.questions.append(label)
        # `getpass` so the key is not echoed onto a shared terminal.
        try:
            raw = self._secret_fn(f"{label}: ")
        except (EOFError, KeyboardInterrupt):
            raise Cancelled() from None
        return (raw or "").strip() or default


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PortState:
    port: int
    free: bool
    pid: int | None = None
    process: str = ""

    def holder(self) -> str:
        if self.pid is None:
            return "an unidentified process"
        if self.process:
            return f"PID {self.pid} ({self.process})"
        return f"PID {self.pid}"


def _lsof_holder(port: int) -> tuple[int | None, str]:
    """`(pid, command)` of the process listening on `port`, from `lsof`.

    `-Fpc` is lsof's machine-readable output (`p<pid>`, `c<command>` per line),
    which is why the parsing here is three lines instead of a table reader.
    """
    exe = shutil.which("lsof")
    if not exe:
        return None, ""
    try:
        proc = subprocess.run(
            [exe, "-nP", "-Fpc", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    pid: int | None = None
    name = ""
    for line in proc.stdout.splitlines():
        if line.startswith("p") and pid is None:
            try:
                pid = int(line[1:])
            except ValueError:
                continue
        elif line.startswith("c") and not name:
            name = line[1:].strip()
    return pid, name


def _can_bind(port: int) -> bool:
    """True when a listener could take `port` on all interfaces right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("", port))
        except OSError:
            return False
    return True


def probe_port(port: int) -> PortState:
    """Whether `port` is free, and who holds it when it is not.

    Two sources on purpose: `lsof` can *name* the holder, the bind is the
    verdict — lsof sees only what this user is allowed to see, and a bind on
    `0.0.0.0` conflicts with any specific-address listener on the same port.
    """
    pid, name = _lsof_holder(port)
    bindable = _can_bind(port)
    return PortState(port=port, free=bindable and pid is None, pid=pid, process=name)


def next_free_port(start: int, *, limit: int = 64) -> int | None:
    """The first free port above `start`, or `None` when the range is exhausted."""
    for port in range(start + 1, min(start + 1 + limit, 65536)):
        if probe_port(port).free:
            return port
    return None


def _parse_port(raw: str) -> int | None:
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
@dataclass
class Node:
    """One node as `init` is about to write it."""

    name: str
    addresses: list[str]
    node_id: str | None = None
    models: list[str] = field(default_factory=list)
    weight: float = 1.0


def _discover(addresses: Sequence[str], api_key: str) -> list[Any] | None:
    """Ask `ripple.discover` (issue #61) about `addresses`.

    Returns `None` when the module is not there — that is a different answer
    from `[]` ("nothing answered"), and the flow says which one it got. A
    discovery that raises is reported and read as "nothing answered": a broken
    probe must not make the whole cold start impossible.
    """
    try:
        from ripple.discover import discover        # noqa: WPS433 — lazy on purpose
    except ImportError:
        return None
    try:
        return list(asyncio.run(discover(list(addresses), api_key=api_key or None)))
    except Exception as exc:                        # noqa: BLE001 — see docstring
        print(f"discovery failed: {exc}", file=sys.stderr)
        return []


def _default_name(index: int) -> str:
    return f"node-{index}"


def _nodes_from_found(found: Sequence[Any], addresses: Sequence[str]) -> list[Node]:
    """Group discovery results into nodes.

    One node may answer on several addresses (the M5 has three); `discover`
    reports one `Found` per address, and `node_id` is the identity that says
    which of them belong together. An address that answered nothing still
    becomes a node: a node that is down right now is exactly the one an
    operator configures today so it is picked up when it comes back.
    """
    nodes: list[Node] = []
    by_identity: dict[str, Node] = {}
    for entry in found:
        identity = str(getattr(entry, "node_id", None) or getattr(entry, "address", ""))
        existing = by_identity.get(identity)
        models = [str(m) for m in (getattr(entry, "models", ()) or ())]
        if existing is not None:
            if entry.address not in existing.addresses:
                existing.addresses.append(entry.address)
            for model in models:
                if model not in existing.models:
                    existing.models.append(model)
            continue
        node = Node(name=str(getattr(entry, "name", "") or ""),
                    addresses=[str(entry.address)],
                    node_id=getattr(entry, "node_id", None),
                    models=models)
        by_identity[identity] = node
        nodes.append(node)

    known = {a for node in nodes for a in node.addresses}
    for address in addresses:
        if address not in known:
            nodes.append(Node(name="", addresses=[address]))
            known.add(address)

    seen: set[str] = set()
    for index, node in enumerate(nodes, 1):
        if not node.name:
            node.name = _default_name(index)
        base, suffix = node.name, 2
        while node.name in seen:
            node.name = f"{base}-{suffix}"
            suffix += 1
        seen.add(node.name)
    return nodes


def _split_list(raw: str) -> list[str]:
    """Comma- or whitespace-separated entries, de-duplicated, order kept."""
    out: list[str] = []
    for part in str(raw or "").replace(",", " ").split():
        text = part.strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalise_address(raw: str) -> str:
    """`host:port` → `http://host:port`. Config validation refuses the short form."""
    text = str(raw or "").strip().rstrip("/")
    if text and not text.startswith(("http://", "https://")):
        text = f"http://{text}"
    return text


# ---------------------------------------------------------------------------
# The location of this machine (issue #67)
# ---------------------------------------------------------------------------
def _run(argv: Sequence[str]) -> str:
    """stdout of a short system command, `""` when it is missing or fails."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True,
                              timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _prefix_from_mask(mask: str) -> int | None:
    """`0xffffff00` (macOS) or `255.255.255.0` → 24. `None` when not a mask."""
    text = str(mask or "").strip()
    try:
        value = int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        try:
            value = int(ipaddress.IPv4Address(text))
        except ValueError:
            return None
    if value < 0 or value > 0xFFFFFFFF:
        return None
    prefix = 0
    while prefix < 32 and value & (0x80000000 >> prefix):
        prefix += 1
    return prefix


def _default_route_interface() -> str:
    """The interface that carries the default route — the one the fleet is on."""
    if sys.platform == "darwin":
        out = _run(["route", "-n", "get", "default"])
        match = re.search(r"^\s*interface:\s*(\S+)", out, re.M)
        return match.group(1) if match else ""
    out = _run(["ip", "route", "show", "default"])
    match = re.search(r"\bdev\s+(\S+)", out)
    return match.group(1) if match else ""


def _interface_networks() -> list[str]:
    """This machine's non-loopback IPv4 networks as CIDR, routed one first.

    `ripple init` has to offer a network for the location it is about to write,
    and the only one it can honestly derive is the one its own interface carries.
    Read from `ifconfig` (macOS) or `ip -o -4 addr` (Linux) — no dependency, and
    a machine where neither works yields an empty list: `init` then asks instead
    of inventing `192.168.0.0/24`.

    Loopback (`127.0.0.0/8`) and link-local (`169.254.0.0/16`) are dropped: a
    location that only contains a self-assigned address is not a location, it is
    the absence of one (that is what the Thunderbolt bridge looked like on
    17.11.2026). Loopback stays out for a second reason (issue #76): every
    machine's loopback is `127.0.0.1`, so a site that contained it would contain
    every machine — an `OK` in `ripple doctor` that compared nothing. A node
    reached over loopback therefore gets no `site:` (`_step_site` says so in one
    line) and `doctor` reports `SKIP` with the loopback as the reason.
    """
    found: list[tuple[str, str]] = []
    if sys.platform == "darwin":
        iface = ""
        for line in _run(["ifconfig"]).splitlines():
            head = re.match(r"^([A-Za-z0-9]+):\s", line)
            if head:
                iface = head.group(1)
                continue
            addr = re.match(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(\S+)",
                            line)
            if addr and iface:
                prefix = _prefix_from_mask(addr.group(2))
                if prefix is not None:
                    found.append((iface, f"{addr.group(1)}/{prefix}"))
    else:
        for line in _run(["ip", "-o", "-4", "addr", "show"]).splitlines():
            match = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)",
                             line)
            if match:
                found.append((match.group(1), f"{match.group(2)}/{match.group(3)}"))

    preferred = _default_route_interface()
    found.sort(key=lambda pair: 0 if pair[0] == preferred else 1)

    out: list[str] = []
    for _iface, cidr in found:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if net.is_loopback or net.is_link_local or net.is_unspecified:
            continue
        text = str(net)
        if text not in out:
            out.append(text)
    return out


def _site_id(name: str) -> str:
    """`Site A West` → `site-a-west`: an id a host can name, stable and ASCII."""
    text = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return text or "local"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
@dataclass
class Options:
    """Everything the flags carry. The flow reads nothing else from the CLI."""

    yes: bool = False
    force: bool = False
    port: int | None = None
    hosts: list[str] = field(default_factory=list)
    omlx_key: str = ""
    home_dir: Path | None = None
    config_path: Path | None = None
    name: str = ""
    check: bool = True
    # The location `init` writes (issue #67). Empty means: ask, with the network
    # derived from this machine's interface as the default; a value that strips
    # to nothing is the deliberate "no site" (see `_step_site`).
    site_name: str = ""
    site_networks: str = ""


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def _step_port(options: Options, prompter: Prompter,
               out: Callable[[str], None]) -> int | None:
    """Step 1 — the listen port. A busy port is named and moved past, not fatal."""
    if options.port is not None:
        port = options.port
    else:
        port = _parse_port(prompter.text("Port", str(DEFAULT_PORT)))
        if port is None:
            print("that is not a port number (1-65535)", file=sys.stderr)
            return None

    while True:
        state = probe_port(port)
        if state.free:
            return port
        out(f"port {port} is already in use by {state.holder()}")
        candidate = next_free_port(port)
        if candidate is None:
            print(f"no free port in the 64 above {port} — pick another one",
                  file=sys.stderr)
            return None
        if prompter.yes_no(f"Use port {candidate} instead?", True):
            port = candidate
            continue
        port = _parse_port(prompter.text("Port", str(candidate)))
        if port is None:
            print("that is not a port number (1-65535)", file=sys.stderr)
            return None


def _ask_addresses(prompter: Prompter) -> list[Node]:
    raw = prompter.text("oMLX address(es), comma separated", DEFAULT_ADDRESS)
    addresses: list[str] = []
    for entry in _split_list(raw):
        address = _normalise_address(entry)
        if address and address not in addresses:
            addresses.append(address)
    if not addresses:
        addresses = [DEFAULT_ADDRESS]
    return [Node(name="", addresses=[a]) for a in addresses]


def _step_nodes(options: Options, prompter: Prompter,
                out: Callable[[str], None]) -> list[Node] | None:
    """Step 2 — where the nodes are, then name, weight and allow-list per node."""
    requested = [_normalise_address(h) for h in options.hosts if str(h).strip()]
    probe = requested or [DEFAULT_ADDRESS]
    found = _discover(probe, options.omlx_key)
    nodes = _nodes_from_found(found or [], probe)

    if requested:
        if found:
            out(f"discovered {len(nodes)} node(s) on {', '.join(requested)}")
        elif found is None:
            out("no node discovery available (ripple/discover.py) — using the "
                "addresses as given")
        else:
            out(f"no oMLX answered on {', '.join(requested)} — writing the "
                f"addresses as given")
    elif found:
        out(f"discovered {len(nodes)} node(s):")
        for node in nodes:
            out(f"  {node.name}  {', '.join(node.addresses)}  "
                f"{len(node.models)} model(s)")
        if not prompter.yes_no("Use these nodes?", True):
            nodes = _ask_addresses(prompter)
    else:
        if found is None:
            out("no node discovery available (ripple/discover.py) — enter the "
                "address(es) by hand")
        else:
            out(f"no oMLX answered on {DEFAULT_ADDRESS}")
        nodes = _ask_addresses(prompter)

    if not nodes:
        print("no node configured — nothing to balance without a host",
              file=sys.stderr)
        return None

    if options.name:
        if len(nodes) != 1:
            print(f"--name needs exactly one node, {len(nodes)} were found — "
                  f"edit config.yaml instead", file=sys.stderr)
            return None
        nodes[0].name = options.name

    for index, node in enumerate(nodes, 1):
        label = f"node {index}/{len(nodes)} ({', '.join(node.addresses)})"
        node.name = prompter.text(f"{label} name", node.name) or node.name
        node.weight = _ask_weight(prompter, f"{label} weight", node.weight)
        # The default is the empty answer, not the offered list: an empty
        # allow-list means "everything this node offers" and stays correct when
        # the operator adds a model later. Writing tonight's list into the file
        # would hide every model added afterwards — silently, which is the one
        # failure mode this whole issue is about.
        if node.models:
            out(f"{label}: {len(node.models)} model(s) offered")
        raw = prompter.text(f"{label} models (comma separated, empty = all "
                            f"offered)", "")
        node.models = _split_list(raw)
    return nodes


def _ask_weight(prompter: Prompter, label: str, default: float) -> float:
    raw = prompter.text(label, f"{default:g}")
    try:
        weight = float(raw)
    except (TypeError, ValueError):
        return default
    return weight if weight >= 0 else default


def _step_site(options: Options, nodes: list[Node], prompter: Prompter,
               out: Callable[[str], None]) -> SiteConfig | None:
    """Step 3 — the location this machine stands in (issue #67).

    The network is derived from the interface that carries the default route and
    offered as the default; the name is asked for because only the operator knows
    what the room or the city is called. Enter takes the offered default
    `local`, and the prompt says so — an install that is not asked twice writes
    `site: local` with the derived network, which is a location, not a missing
    one.

    "No site" is therefore a deliberate answer and not the empty line: it is
    `--site-name` with a value that strips to nothing, or a hand-written
    configuration. A location nobody named is not data, and in that case the
    configuration stays as it was before this issue.

    A host gets `site:` only when one of its addresses is inside the networks —
    a node reached over a different network is in a different location, and
    writing this id on it would be the very claim this issue exists to stop
    making silently.

    A node reached over loopback is the exception the operator has to be told
    about (issue #76): it is *this* machine, loopback is in no site's networks,
    and attaching the id would turn `doctor`'s `SKIP` into a `WARN` about a
    claim nothing measured. So it gets no `site:` and the step says so in one
    line, naming `doctor` — the silent `site=-` in the routing table below is
    what the issue was filed about.
    """
    derived = _interface_networks()
    # `--site-name` first, and stripped: a value that is only whitespace is the
    # documented way to decline a location (see the docstring), so it must not
    # reach `if not name` as a truthy " ". The question's default is `local`,
    # which makes Enter a location — the prompt text says so.
    if options.site_name:
        name = options.site_name.strip()
    else:
        name = prompter.text(
            "Site name (room, building or city; empty = local)",
            "local").strip()
    if not name:
        out("site: none — the configuration carries no location "
            "(a host may still name one by id)")
        return None

    default_networks = ", ".join(derived)
    raw = options.site_networks or prompter.text(
        "Site networks (CIDR, comma separated)", default_networks)
    try:
        networks = parse_networks(raw, where=f"site {name}")
    except ValueError as exc:
        print(f"{exc} — the site is written without networks, so it cannot be "
              f"checked", file=sys.stderr)
        networks = []
    if not networks and not derived:
        out(f"site {name}: no network could be derived from this machine — add "
            f"the networks later, otherwise `ripple doctor` cannot check the "
            f"location")

    site = SiteConfig(id=_site_id(name), name=name, networks=networks)
    out(f"site {site.id}: {site.name} — "
        f"{', '.join(site.networks) if site.networks else 'no networks'}")
    inside: list[str] = []
    local: list[str] = []
    for node in nodes:
        address = ip_of(node.addresses[0]) if node.addresses else None
        if site.contains(address):
            inside.append(node.name)
        elif address is not None and address.is_loopback:
            local.append(node.name)
    if inside:
        out(f"  nodes inside it: {', '.join(inside)}")
    if local:
        # One line instead of a silent `site=-` (issue #76). The node is this
        # machine and a site is a network claim, so there is nothing to attach
        # and nothing for `doctor` to compare — saying that is the point.
        out(f"  loopback: {', '.join(local)} — this machine, no site: written "
            f"(loopback is in no site's networks; `ripple doctor` reports SKIP)")
    return site


def _step_omlx_key(options: Options, home: Path, prompter: Prompter,
                   out: Callable[[str], None]) -> str:
    """Step 4 — the oMLX API key. An empty answer is a valid, reported answer."""
    if options.omlx_key:
        out("oMLX key: taken from --omlx-key")
        return options.omlx_key.strip()

    # The environment before the file: it is how the operator already exports the
    # key (`export OMLX_API_KEY=$(cat ~/.omlx/api_key.txt)`) and how a scripted
    # cold start hands it over without writing it into the transcript.
    from_env = (os.environ.get("OMLX_API_KEY") or "").strip()
    if from_env and prompter.yes_no("Use the oMLX key from $OMLX_API_KEY?", True):
        out("oMLX key: read from $OMLX_API_KEY")
        return from_env

    # The key file belongs to the *effective* home: with `--home-dir /tmp/x` the
    # caller said "pretend this directory is my home", and a scripted cold start
    # passes the key through `$OMLX_API_KEY` instead. Reading the real
    # `~/.omlx/api_key.txt` behind a `--home-dir` would also let a test pick up
    # the operator's live key — the leak this repository has been bitten by twice.
    hint = home / ".omlx" / "api_key.txt"
    if hint.is_file():
        if prompter.yes_no(f"Use the oMLX key from {hint}?", True):
            try:
                key = hint.read_text(encoding="utf-8").strip()
            except OSError as exc:
                print(f"cannot read {hint}: {exc}", file=sys.stderr)
                key = ""
            if key:
                out(f"oMLX key: read from {hint}")
                return key

    key = prompter.secret("oMLX API key (usually in ~/.omlx/api_key.txt; "
                          "empty = read-only)")
    if key:
        out("oMLX key: entered")
    else:
        out("oMLX key: none — the node answers its model list with 401, so "
            "RIPPLE runs read-limited; `ripple doctor` reports it")
    return key


def _fill_models(nodes: list[Node], api_key: str,
                 out: Callable[[str], None]) -> None:
    """Read the model list now that a key is known — and leave the allow-list empty.

    The conversation asks for the key after the nodes (the issue's order), so the
    first discovery may have seen a 401. What this does is report what the node
    offers and fill in a `node_id` that discovery could not read without a key.

    It deliberately does **not** write the offered models into `models:`. An empty
    allow-list means "everything this node offers" (`config.example.yaml`), and a
    concrete list would freeze tonight's fleet into the file: the operator adds a
    model to oMLX, RIPPLE keeps hiding it, and nothing says why. Measured on the
    reference machine — 23 models were offered and all 23 would have been written.
    """
    wanted = [node for node in nodes if not node.models]
    if not wanted:
        return
    addresses = [a for node in wanted for a in node.addresses]
    found = _discover(addresses, api_key)
    if not found:
        return
    by_address = {str(getattr(entry, "address", "")): entry for entry in found}
    for node in wanted:
        for address in node.addresses:
            entry = by_address.get(address)
            if entry is None:
                continue
            models = [str(m) for m in (getattr(entry, "models", ()) or ())]
            if not node.node_id:
                node.node_id = getattr(entry, "node_id", None)
            if models:
                out(f"node {node.name}: {len(models)} model(s) offered — "
                    f"allow-list stays empty, which means all of them "
                    f"(restrict with `models:` in the config)")
            break


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------
def _state_path(home: Path, name: str) -> str:
    """A state file as it should appear in the configuration.

    Under the real home it stays `~/.ripple/<name>` — portable and exactly what
    the example configuration shows. Under a `--home-dir` it is absolute,
    because `~` would point back at the real home and the whole point of the
    flag is that nothing lands there.
    """
    if home == Path.home():
        return f"~/.ripple/{name}"
    return str(home / ".ripple" / name)


def _log_path(home: Path) -> str:
    if home == Path.home():
        return "~/Library/Logs/ripple.log"
    return str(home / "Library" / "Logs" / "ripple.log")


def config_document(*, port: int, nodes: Sequence[Node], api_key: str,
                    admin_key: str, home: Path,
                    site: SiteConfig | None = None) -> dict[str, Any]:
    """The `config.yaml` body. Every value is either measured, given or a default
    that `ripple/config.py` documents — no invented throughput numbers."""
    defaults: dict[str, Any] = {
        "backend": "omlx",
        "max_inflight": 8,
        "max_threads": 20,
        "spill_threshold": 3,
        "hard_threshold": 0.95,
        "prefer_resident": True,
    }
    if api_key:
        # One oMLX key for the whole fleet is the normal case (the nodes are
        # clones); a host that differs overrides `api_key` in its own block.
        defaults["api_key"] = api_key

    hosts: list[dict[str, Any]] = []
    for node in nodes:
        entry: dict[str, Any] = {
            "name": node.name,
            "addresses": list(node.addresses),
            "weight": node.weight,
        }
        # The declared location (issue #67) — only where an address really is
        # inside it. A node on another network stays without a site instead of
        # inheriting a claim that `ripple doctor` would then have to contradict.
        # Loopback is the case that matters here (issue #76): `127.0.0.1` is in
        # no site's networks, so a local node keeps `site=-` and `doctor` says
        # why it cannot check one — see `_step_site`.
        if site is not None and any(site.contains(ip_of(a)) for a in node.addresses):
            entry["site"] = site.id
        if node.node_id:
            entry["node_id"] = node.node_id
        if node.models:
            entry["models"] = list(node.models)
        hosts.append(entry)

    document: dict[str, Any] = {
        "listen_host": "0.0.0.0",
        "listen_port": port,
        "log_level": "info",
        "log_file": _log_path(home),
        "defaults": defaults,
    }
    if site is not None:
        document["sites"] = [site.as_dict()]
    document["hosts"] = hosts
    document.update({
        "auth": {
            "enabled": True,
            "keys_file": _state_path(home, "keys.yaml"),
            "admin_key": admin_key,
            "allow_loopback": False,
            "hide_foreign_models": True,
        },
        "placement": {
            "maintenance_file": _state_path(home, "maintenance.yaml"),
            "model_policy_file": _state_path(home, "models.yaml"),
            "loading": {"calibration_file": _state_path(home, "calibration.yaml")},
        },
        "stats": {
            "file": _state_path(home, "host_stats.json"),
            "persist": True,
            "flush_seconds": 30,
        },
        # The runtime overlay (issue #70, renamed in #78): what the admin API
        # writes — nodes added at the page and the routing policy set there.
        # Named here for the same reason as the other state files — a
        # `--home-dir` install must not touch the real home, and the file sits
        # next to the ledger it belongs with.
        "fleet": {
            "file": _state_path(home, "runtime.yaml"),
        },
    })
    return document


def render_config(document: dict[str, Any], *, port: int, path: str) -> str:
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
    return _HEADER.format(path=path, port=port) + body


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` atomically with mode `0600`.

    Same shape as `KeyStore.save`: temp file in the same directory, fsync,
    chmod, `os.replace`. A half-written `config.yaml` would read as a broken
    installation, and the key store is a list of secrets.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(FILE_MODE)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    path.chmod(FILE_MODE)


# ---------------------------------------------------------------------------
# The final self-test
# ---------------------------------------------------------------------------
def _pick_model(ripple: Any) -> str:
    """A model to send the test request to — a resident one first.

    A resident model answers immediately; a non-resident one would make `init`
    trigger a cold load of up to several hundred gigabytes (NZ-4 forbids that
    as a side effect of a configuration step).
    """
    loaded: list[str] = []
    available: list[str] = []
    for host in ripple.fleet.hosts:
        for model_id, info in host.models.items():
            if not host.cfg.model_allowed(model_id):
                continue
            (loaded if info.loaded else available).append(model_id)
    for pool in (loaded, available):
        if pool:
            return sorted(pool)[0]
    return ""


async def one_request(cfg: Any, client_key: str) -> tuple[int, str]:
    """One real completion through the freshly written configuration.

    Returns `(status, detail)`; status 0 means the request was skipped because
    no node offers a model, which is not a failure of the configuration.
    """
    import httpx

    from .proxy import Ripple

    ripple = Ripple(cfg)
    try:
        await ripple.fleet.start(warmup=False)
    except Exception as exc:                        # noqa: BLE001 — reported
        await ripple.proxy_client.aclose()
        return 0, f"the fleet did not start: {exc}"
    try:
        model = _pick_model(ripple)
        if not model:
            return 0, "no node offers a model yet — request skipped"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        }
        transport = httpx.ASGITransport(app=ripple.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ripple",
                                     timeout=120.0) as client:
            response = await client.post(
                "/v1/chat/completions", json=body,
                headers={"Authorization": f"Bearer {client_key}"})
        detail = " ".join(response.text.split())[:200]
        return response.status_code, f"model {model} — {detail}"
    finally:
        await ripple.fleet.stop()
        await ripple.proxy_client.aclose()


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------
@dataclass
class Answers:
    """What the conversation produced — everything the files are built from."""

    port: int
    nodes: list[Node]
    api_key: str
    site: SiteConfig | None = None


def _ask_all(options: Options, home: Path, prompter: Prompter,
             out: Callable[[str], None]) -> Answers | None:
    """Steps 1-4: port, nodes, location, oMLX key. `None` = unusable answer."""
    port = _step_port(options, prompter, out)
    if port is None:
        return None
    out(f"port: {port}")

    nodes = _step_nodes(options, prompter, out)
    if nodes is None:
        return None

    # The location comes after the nodes: whether a node is inside it is the
    # question this whole block is for (issue #67).
    site = _step_site(options, nodes, prompter, out)

    api_key = _step_omlx_key(options, home, prompter, out)
    if api_key:
        _fill_models(nodes, api_key, out)
    return Answers(port=port, nodes=nodes, api_key=api_key, site=site)


def run(options: Options, prompter: Prompter,
        out: Callable[[str], None] = print) -> int:
    """The whole cold start, from the port question to the test request.

    One function for both paths: `prompter` decides whether a human is asked or
    a default is taken. Returns the process exit code (0 ok, 1 refused/aborted,
    2 written but the check failed).
    """
    from .config import ConfigError, load_config

    home = (options.home_dir or Path.home()).expanduser().resolve()
    config_path = (options.config_path or (Path.cwd() / "config.yaml"))
    config_path = Path(config_path).expanduser().resolve()
    keys_path = home / ".ripple" / "keys.yaml"

    # -- 0. never overwrite in silence --------------------------------------
    for path, label in ((config_path, "config"), (keys_path, "key store")):
        if path.exists() and not options.force:
            print(f"{label} already exists: {path} — refusing to overwrite it "
                  f"(use --force to replace it)", file=sys.stderr)
            return 1

    out("RIPPLE init — first-run configuration")
    out(f"config:   {config_path}")
    out(f"key store: {keys_path}")
    out("")

    # -- 1.-3. port, nodes, oMLX key ----------------------------------------
    # Everything is asked before anything is written, so an abort here leaves
    # the machine exactly as it was.
    try:
        answers = _ask_all(options, home, prompter, out)
    except (Cancelled, KeyboardInterrupt):
        print("\ncancelled — nothing was written", file=sys.stderr)
        return 1
    if answers is None:
        return 1
    port, nodes, api_key = answers.port, answers.nodes, answers.api_key

    # -- 4. RIPPLE admin key (generated, never asked for, never printed) ----
    admin_key = secrets.token_hex(16)          # 32 hex characters
    out("RIPPLE admin key: generated (32 hex) — it is in config.yaml at "
        "auth.admin_key and is not shown here")

    # -- 5./6. write, then show the client key exactly once -----------------
    document = config_document(port=port, nodes=nodes, api_key=api_key,
                               admin_key=admin_key, home=home, site=answers.site)
    try:
        write_private(config_path,
                      render_config(document, port=port, path=str(config_path)))
    except OSError as exc:
        print(f"cannot write {config_path}: {exc}", file=sys.stderr)
        return 1
    out("")
    out(f"wrote {config_path} (mode 0600)")

    from .keys import KeyStore, KeyStoreError

    # A fresh store, staged beside the real one and renamed into place: with
    # `--force` the old key store is *replaced*, never read and appended to —
    # otherwise its `first-client` row would collide with the new one, and a
    # half-written store would lock every client out at once.
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    staged = keys_path.with_name(f".{keys_path.name}.init")
    staged.unlink(missing_ok=True)
    try:
        store = KeyStore(staged)
        client = store.create(CLIENT_KEY_NAME, kind="interactive",
                              created_by="ripple init",
                              note="created by ripple init")
        staged.chmod(FILE_MODE)
        staged.replace(keys_path)
        keys_path.chmod(FILE_MODE)
    except KeyStoreError as exc:
        print(f"cannot write the key store {keys_path}: {exc}", file=sys.stderr)
        print(f"{config_path} was written; the key store was not",
              file=sys.stderr)
        return 1
    finally:
        staged.unlink(missing_ok=True)
    out(f"wrote {keys_path} (mode 0600)")

    out("")
    out(f"client key \"{client.name}\" — shown once, store it now:")
    out(f"  {client.key}")
    out("")
    out("  use it as:  Authorization: Bearer <key>")
    out("  later only the masked form is shown; rotate it on the /keys page "
        "with the admin key.")

    # -- 7. the existing --check path, then one real request ----------------
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"the written configuration does not load: {exc}", file=sys.stderr)
        return 2

    if not options.check:
        out("")
        out("--no-check: configuration check and test request skipped")
        return 0

    from .__main__ import run_check

    out("")
    out("— configuration check —")
    check_code = asyncio.run(run_check(cfg))

    out("")
    out("— one real request through the new configuration —")
    status, detail = asyncio.run(one_request(cfg, client.key))
    if status == 200:
        out(f"test request: 200 — {detail}")
    elif status == 0:
        out(f"test request: skipped — {detail}")
    else:
        out(f"test request: {status} — {detail}")

    ok = check_code == 0 and status in (0, 200)
    out("")
    if ok:
        out("RIPPLE is configured. Start it with:  ripple --config "
            f"{config_path}")
        return 0
    out("RIPPLE is configured, but the check did not pass — see above. "
        "Nothing is wrong with the files; the nodes did not answer as expected.")
    return 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ripple init",
        description="First-run configuration: write config.yaml and a key store "
                    "for a machine that has none yet.")
    parser.add_argument("--yes", action="store_true",
                        help="take every default instead of asking")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing config.yaml / key store")
    parser.add_argument("--port", type=int, default=None,
                        help=f"listen port (default {DEFAULT_PORT}; a busy port "
                             f"moves to the next free one)")
    parser.add_argument("--host", action="append", default=[], metavar="URL",
                        help="oMLX address, repeatable (e.g. http://10.0.0.5:8000)")
    parser.add_argument("--omlx-key", default="",
                        help="oMLX API key; without one the node is read-limited")
    parser.add_argument("--home-dir", default="",
                        help="home directory to use instead of $HOME — the key "
                             "store goes to <home>/.ripple/keys.yaml")
    parser.add_argument("--config", default="",
                        help="where to write config.yaml (default ./config.yaml)")
    parser.add_argument("--name", default="",
                        help="node name, only with exactly one node")
    parser.add_argument("--site-name", default="",
                        help="name of the location this machine stands in "
                             "(default: asked, 'local'); a blank value = no site")
    parser.add_argument("--site-networks", default="",
                        help="CIDR networks of that location, comma separated "
                             "(default: derived from this machine's interface)")
    parser.add_argument("--no-check", action="store_true",
                        help="skip the final configuration check and test request")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    options = Options(
        yes=args.yes,
        force=args.force,
        port=args.port,
        hosts=list(args.host),
        omlx_key=args.omlx_key,
        home_dir=Path(args.home_dir) if args.home_dir else None,
        config_path=Path(args.config) if args.config else None,
        name=args.name,
        check=not args.no_check,
        site_name=args.site_name,
        site_networks=args.site_networks,
    )
    prompter: Prompter = AutoPrompter() if options.yes else ConsolePrompter()
    try:
        return run(options, prompter)
    except Cancelled:
        print("\ncancelled — nothing was written", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Only reachable outside the question phase (that one is handled in
        # `run`), i.e. during the check — by then the files exist.
        print("\ninterrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
