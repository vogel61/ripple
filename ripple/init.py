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
  (PID and process name from `lsof`), and the next free port is offered. A port
  that a closed listener left in `TIME_WAIT` is free, not "held by an
  unidentified process" (issue #151), and a port that does move is said again at
  the end of the run.
* **No oMLX key is a valid answer.** The node is then read-limited (its model
  list answers 401) and `ripple doctor` says so; `init` writes a configuration
  without an `api_key` and does not pretend otherwise.
* **Nothing is overwritten without `--force`**, and the refusal names the exact
  path. With `--force` the files that are about to be replaced are copied aside
  first — `<config>.bak-<YYYYmmdd-HHMMSS>`, `<keys>.bak-<YYYYmmdd-HHMMSS>`, mode
  `0600`, byte for byte — and both paths are printed (issue #154): `--force` is
  the flag an operator reaches for exactly when the machine is already in
  trouble, so the destructive path runs at the worst possible moment. Both files
  are `0600` and their parents are created — one of them is a list of secrets.
* **The client key is shown exactly once.** After that only
  `ClientKey.masked_key()` is ever printed (FA-15).
* **The admin key is asked for, and printed exactly once when generated**
  (issue #158). A fresh install used to end with the operator holding no admin
  key: `init` generated one, wrote it to `config.yaml` and said "not shown
  here", so the first thing a new operator did was open a mode-0600 file to copy
  a secret out of it. The question takes a typed key (used as given, never
  echoed, only its length reported — the running installation uses a 4-digit key
  on purpose) or an empty answer, which generates 32 hex characters and prints
  them once, in the same shape as the client key. `--yes` generates too, and
  prints it: a scripted run must not end without one either. No key material
  reaches the log, the `--json` output or the routing table.

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
from datetime import datetime
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
        """Who holds the port, as far as this user is allowed to see (issue #151).

        `lsof` names only the sockets this user may look at, so "no pid" is not
        "no process": it is a listener this user cannot name, or a socket that
        is still closing. Saying "an unidentified process" turned that into a
        claim about a port that was in fact free.
        """
        if self.pid is None:
            return ("no process this user can see — another user's listener, "
                    "or a socket that is still closing")
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


def _bind_ok(address: str, port: int) -> bool:
    """Can a listener bind `address:port` right now, `SO_REUSEADDR` set?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((address, port))
        except OSError:
            return False
    return True


def _can_bind(port: int) -> bool:
    """True when a listener could take `port` now — a socket in `TIME_WAIT` is
    not a holder (issue #151).

    `SO_REUSEADDR` is what makes the verdict match what uvicorn does when RIPPLE
    starts: without it, a listener that accepted a connection leaves the port
    unbindable for the `TIME_WAIT` window (30-45 s measured here) while `lsof`
    names nobody — which is how `init` came to report "an unidentified process"
    about a port that was already free.

    Two probes, because `SO_REUSEADDR` on macOS lets a wildcard bind succeed
    beside a listener that sits on one specific address, and the other way
    round. A port is free only when both the wildcard and the loopback bind are.
    """
    return _bind_ok("", port) and _bind_ok("127.0.0.1", port)


def probe_port(port: int) -> PortState:
    """Whether `port` is free, and who holds it when it is not.

    Two sources on purpose: `lsof` can *name* the holder, the bind is the
    verdict — lsof sees only what this user is allowed to see, and a bind on
    `0.0.0.0` conflicts with any specific-address listener on the same port.
    `_can_bind` therefore probes both the wildcard and the loopback: with
    `SO_REUSEADDR` set, either one can pass beside a listener on the other
    (issue #151).
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
    return port if PORT_MIN <= port <= PORT_MAX else None


# The port this proxy can actually use (issue #166). Below 1024 needs root, and
# RIPPLE runs as a normal user — accepting `80` here only moves the failure to
# start-up, minutes after the question. The documented range is 8010-8099; a
# port outside it stays possible (8080 and 9000 are legitimate on a machine that
# already runs something) and is said once instead of being refused.
PORT_MIN = 1024
PORT_MAX = 65535
PORT_RANGE = (8010, 8099)


def _port_complaint(raw: str) -> str:
    """Why an answer is unusable — four causes, four sentences, one range."""
    text = str(raw).strip()
    try:
        port = int(text)
    except (TypeError, ValueError):
        return (f"'{text}' is not a number — give a port between {PORT_MIN} and "
                f"{PORT_MAX}; this project uses {PORT_RANGE[0]}-{PORT_RANGE[1]}")
    if port == 0:
        # Not a privileged port: `0` asks the kernel for any free port, and this
        # port has to be written down and reachable by clients.
        return ("0 means 'any free port' to the kernel, and the port has to be "
                f"known before it is written — use {PORT_RANGE[0]}-{PORT_RANGE[1]}")
    if port < PORT_MIN:
        return (f"port {port} needs root, and RIPPLE does not run as root — "
                f"use {PORT_RANGE[0]}-{PORT_RANGE[1]}")
    return (f"{PORT_MAX} is the highest port — give a number between {PORT_MIN} "
            f"and {PORT_MAX}; this project uses {PORT_RANGE[0]}-{PORT_RANGE[1]}")


def _outside_documented_range(port: int) -> str | None:
    """One neutral line for a port that works but is not the documented range.

    The range is documented, not enforced — but the line says what the deviation
    means instead of shrugging (issue #172): the dashboard, `ripple doctor` and
    `ripple start` all follow this number, so a typo here is not cosmetic.
    """
    if PORT_RANGE[0] <= port <= PORT_RANGE[1]:
        return None
    return (f"port {port} is outside {PORT_RANGE[0]}-{PORT_RANGE[1]}, the range "
            f"this project documents — the dashboard, `ripple doctor` and "
            f"`ripple start` follow this number")


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
    #: This node's own oMLX key (issue #171). Empty means "the fleet default",
    #: which is `Answers.api_key` — the first node's answer.
    api_key: str = ""
    #: `(id, model_type)` from the node's own `/v1/models`, when it could be
    #: read. `models:` is decided from this, never from the ids alone: oMLX
    #: serves utility engines beside the chat models and only the type says
    #: which is which (issue #157).
    offered: list[tuple[str, str]] = field(default_factory=list)
    #: The operator answered the models question with the offered default (an
    #: empty line). The allow-list is then every offered model, utility engines
    #: included — decided as soon as the list is readable, which needs the oMLX
    #: key, and said out loud when it is decided (`_resolve_models`).
    models_by_default: bool = False


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


# ---------------------------------------------------------------------------
# Which of a node's models can hold a thread (issue #157)
# ---------------------------------------------------------------------------
#: `model_type` values oMLX reports for something a chat thread can be sent to.
#: Anything else it *names* (measured: `embedding`, `markitdown`) is a utility
#: engine: legitimate — MarkItDown converts documents — and routed like any
#: other model (issue #193); the type is what `init` reads to say which of the
#: offered models it included and why.
CHAT_MODEL_TYPES = frozenset({"llm", "chat", "vlm", "vision", "text", "completion"})

#: The one answer that keeps an allow-list empty on purpose: "really all of
#: them". Without it an operator who wants a new model routed without editing
#: `config.yaml` would have no way to say so.
ALL_MODELS = "all"


def _is_chat_model(model_type: str) -> bool:
    """Is this `model_type` a chat type? An absent type is not evidence against.

    oMLX reports `model_type` for what it knows; a node that omits the field is
    not saying "utility engine", and reading the absence as one would hide a
    chat model. Only a *named* type outside `CHAT_MODEL_TYPES` is a utility.
    """
    kind = str(model_type or "").strip().lower()
    return not kind or kind in CHAT_MODEL_TYPES


def _offered_models(address: str, api_key: str,
                    timeout: float = 3.0) -> list[tuple[str, str]] | None:
    """`(id, model_type)` from the node's own `/v1/models`, or `None`.

    `ripple.discover` answers ids only, and the id does not say what the engine
    is — `MarkItDown` looks like a chat model right up to the first request. The
    type is on this endpoint, which needs the oMLX key; `None` is "not read",
    never "nothing offered".
    """
    try:
        import httpx                                    # noqa: WPS433 — lazy
    except ImportError:                                 # pragma: no cover
        return None
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(address.rstrip("/") + "/v1/models",
                                  headers=headers)
        if response.status_code != 200:
            return None
        body = response.json()
    except Exception:                                   # noqa: BLE001 — best effort
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None
    out: list[tuple[str, str]] = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("id"):
            out.append((str(entry["id"]), str(entry.get("model_type") or "")))
    return out


def _read_offered(node: Node, api_key: str) -> None:
    """Fill `node.offered` from the first of its addresses that answers.

    Attempted even without a key: a node that does not protect its model list
    answers anyway, and one that does answers 401 — which is "not read", the
    same as before.
    """
    if node.offered:
        return
    for address in node.addresses:
        offered = _offered_models(address, api_key)
        if offered is not None:
            node.offered = offered
            return


def _named_utility(offered: Sequence[tuple[str, str]]) -> str:
    """`MarkItDown (markitdown), Qwen3-Embedding-4B (embedding)` — id and type."""
    return ", ".join(f"{mid} ({kind})" for mid, kind in offered)


def _resolve_models(node: Node, out: Callable[[str], None]) -> bool:
    """Decide `node.models` from the typed list and say what it became.

    One line per node, and only where the answer was the default or "all": a
    list the operator typed is written exactly as typed and needs no comment.
    The default is every offered model, utility engines included — an
    allow-list that left them out would refuse a call the node answers.
    Returns True when this call decided the allow-list.
    """
    if not node.offered:
        return False
    utility = [(mid, kind) for mid, kind in node.offered if not _is_chat_model(kind)]

    if node.models_by_default:
        node.models_by_default = False
        node.models = [mid for mid, _kind in node.offered]
        if utility:
            out(f"node {node.name}: allow-list = all {len(node.offered)} "
                f"offered model(s), including {len(utility)} that are not "
                f"chat models: {_named_utility(utility)} — included because "
                f"an embedding or conversion call must reach the model the "
                f"node lists")
        else:
            out(f"node {node.name}: allow-list = all {len(node.offered)} "
                f"offered model(s), every one of them chat-capable")
        return True
    if not node.models:
        detail = (f", including {len(utility)} that are not chat models: "
                  f"{_named_utility(utility)}" if utility else "")
        out(f"node {node.name}: allow-list empty — all {len(node.offered)} "
            f"offered model(s) are routable{detail}")
        return True
    return False


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
@dataclass(frozen=True)
class PortChoice:
    """The port `init` is about to write, and the one that was asked for.

    `requested` is kept so the end of the run can say that the port moved
    (issue #151): under `--yes` the question that moves it is answered with its
    default, so the move has to be said again outside the question.
    """

    port: int
    requested: int


def _step_port(options: Options, prompter: Prompter,
               out: Callable[[str], None]) -> PortChoice | None:
    """Step 1 — the listen port. A busy port is named and moved past, not fatal."""
    if options.port is not None:
        requested = port = options.port
        note = _outside_documented_range(port)
        if note:
            out(note)
    else:
        port = None
        while port is None:
            # A typo must not end the install: ask again, with the reason the
            # last answer was unusable (#166). EOF/Ctrl-C still cancels.
            raw = prompter.text("Port", str(DEFAULT_PORT))
            port = _parse_port(raw)
            if port is None:
                print(_port_complaint(raw), file=sys.stderr)
                continue
            # A port outside the documented range is a decision, not a shrug
            # (issue #172): one question, default No, and the consequence named.
            note = _outside_documented_range(port)
            if note:
                out(note)
                if not isinstance(prompter, AutoPrompter) and \
                        not prompter.yes_no(f"Use port {port} anyway?", False):
                    port = None
        requested = port

    while True:
        state = probe_port(port)
        if state.free:
            return PortChoice(port=port, requested=requested)
        if state.pid is not None:
            out(f"port {port} is already in use by {state.holder()}")
        else:
            # A failed bind with nobody to name is not a claim that a process
            # holds the port (issue #151): say what was measured, not more.
            out(f"port {port} cannot be bound and {state.holder()}")
        candidate = next_free_port(port)
        if candidate is None:
            print(f"no free port in the 64 above {port} — pick another one",
                  file=sys.stderr)
            return None
        if prompter.yes_no(f"Use port {candidate} instead?", True):
            port = candidate
            continue
        port = None
        while port is None:
            raw = prompter.text("Port", str(candidate))
            port = _parse_port(raw)
            if port is None:
                print(_port_complaint(raw), file=sys.stderr)


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


def _http_answers(address: str, timeout: float = 2.0) -> bool:
    """Is *anything* listening at `address`? Any HTTP status counts (issue #167)."""
    try:
        import httpx

        httpx.get(address.rstrip("/") + "/", timeout=timeout, follow_redirects=False)
        return True
    except Exception:                              # noqa: BLE001 — the answer is no
        return False


def _classify_typed_addresses(addresses: Sequence[str],
                              api_key: str) -> dict[str, tuple[str, Any]]:
    """Probe what the operator typed, with the probe the automatic path uses.

    Four outcomes, because they mean four different things (issue #167):

    * `omlx` — the node answered `/api/cluster/node_id` with a `node_id`. That
      endpoint needs no key, so an oMLX is recognised without one.
    * `unconfirmed` — discovery accepted the address on the weaker evidence of a
      key-protected `/v1/models` (401), which every OpenAI-compatible server with
      auth also produces. Measured on this machine: the gateway on :8642 answers
      `401` on `/v1/models` and `404` on the identity path, and was taken for an
      oMLX. It is a question, not a verdict.
    * `not-omlx` — something answered, but neither signal is there.
    * `silent` — nothing answered at all.
    """
    verdicts: dict[str, tuple[str, Any]] = {}
    for address in addresses:
        found = _discover([address], api_key)
        if found and found[0].node_id:
            verdicts[address] = ("omlx", found[0])
        elif found:
            verdicts[address] = ("unconfirmed", found[0])
        elif _http_answers(address):
            verdicts[address] = ("not-omlx", None)
        else:
            verdicts[address] = ("silent", None)
    return verdicts


def _offer_line(node: Node, has_key: bool) -> str:
    """What the node offers, or the honest statement that it was not read (#168).

    oMLX answers 401 on `/v1/models` without a key, and `init` asks for the key
    *after* the nodes — so "0 model(s)" here names a fact nobody measured. At
    this point the only measurement is that the node answered at all, and that is
    what the line says.
    """
    if node.models:
        return f"{len(node.models)} model(s)"
    if has_key:
        return "0 model(s) offered"
    return "oMLX answered — model list not read (the key comes later)"


def _ask_checked_addresses(options: Options, prompter: Prompter,
                           out: Callable[[str], None]) -> list[Node]:
    """Ask for addresses, probe them, and say what was found (issue #167).

    An address that answers nothing is still a legitimate answer — a node that
    is switched off today is the one you configure today — so it is asked about
    once instead of being refused. Under `--yes` there is nobody to ask, and the
    addresses are taken with a note.
    """
    while True:
        nodes = _ask_addresses(prompter)
        addresses = [a for node in nodes for a in node.addresses]
        verdicts = _classify_typed_addresses(addresses, options.omlx_key)
        for address in addresses:
            kind, found = verdicts[address]
            if kind == "omlx":
                identity = (f"node_id {found.node_id}" if found.node_id
                            else "no node_id")
                # Not `_offer_line`: that one starts with "oMLX answered", which
                # this line has already said.
                if found.models:
                    offer = f"{len(found.models)} model(s)"
                elif options.omlx_key:
                    offer = "0 model(s) offered"
                else:
                    offer = "model list not read (the key comes later)"
                out(f"{address}: oMLX answered — {identity}, {offer}")
            elif kind == "unconfirmed":
                out(f"{address}: answered, but it reports no "
                    f"/api/cluster/node_id — oMLX answers that without a key, so "
                    f"this may be another OpenAI-compatible server")
            elif kind == "not-omlx":
                out(f"{address}: something answered, but not as oMLX — no "
                    f"/api/cluster/node_id and no model list")
            else:
                out(f"{address}: no answer (oMLX running?, port correct?)")
        # A name the operator can keep (issue #173). A typed address had none, so
        # an empty answer to the name question wrote `name: ''` — a configuration
        # that does not load. `discover.suggest_name` is the suggestion the
        # automatic path already gets (`local`, a DNS name, a `node-…` stub).
        try:
            from ripple.discover import suggest_name
        except ImportError:                          # pragma: no cover — #61 absent
            suggest_name = None                      # type: ignore[assignment]
        for position, node in enumerate(nodes, 1):
            if node.name or not node.addresses:
                continue
            found = verdicts.get(node.addresses[0], ("silent", None))[1]
            if suggest_name is not None:
                node.name = suggest_name(node.addresses[0],
                                         getattr(found, "node_id", None))
            else:
                node.name = _default_name(position)

        broken = [a for a in addresses if verdicts[a][0] != "omlx"]
        if not broken:
            return nodes
        question = (f"keep {len(broken)} address(es) that did not answer as oMLX?"
                    if len(broken) > 1 else
                    "keep it anyway?")
        if isinstance(prompter, AutoPrompter):
            out("keeping the addresses as given (--yes)")
            return nodes
        if prompter.yes_no(question, False):
            return nodes
        # Asked again: the usual cause is a typo, not a machine that is off.


def _step_nodes(options: Options, prompter: Prompter,
                out: Callable[[str], None],
                api_key: str = "") -> list[Node] | None:
    """Step 2 — where the nodes are, then name, weight and allow-list per node.

    `api_key` is the key already answered in step 1 (issue #180): with it the
    discovery line carries the real model count, instead of saying that the list
    will be read later — and then reading it a step later without a word.
    """
    requested = [_normalise_address(h) for h in options.hosts if str(h).strip()]
    probe = requested or [DEFAULT_ADDRESS]
    key = api_key or options.omlx_key
    found = _discover(probe, key)
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
                f"{_offer_line(node, bool(key))}")
        if not prompter.yes_no("Use these nodes?", True):
            nodes = _ask_checked_addresses(options, prompter, out)
    else:
        if found is None:
            out("no node discovery available (ripple/discover.py) — enter the "
                "address(es) by hand")
        else:
            out(f"no oMLX answered on {DEFAULT_ADDRESS}")
        nodes = _ask_checked_addresses(options, prompter, out)

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
        _ask_node_detail(index, len(nodes), node, options, prompter, out)
    return nodes


def _ask_node_detail(index: int, total: int, node: Node, options: Options,
                     prompter: Prompter, out: Callable[[str], None]) -> None:
    """One node, one round of questions: name, weight, allow-list.

    Extracted so that a node found by the network sweep (#187) is asked exactly
    the same three questions as one that answered on loopback — two prompts for
    the same decision would drift apart.
    """
    label = f"node {index}/{total} ({', '.join(node.addresses)})"
    node.name = prompter.text(f"{label} name", node.name) or node.name
    node.weight = _ask_weight(prompter, f"{label} weight", node.weight)
    # The default is every offered model, not the empty list (issue #157,
    # and #193 for the utility engines): an empty allow-list is the widest
    # possible statement — every model the node offers *and every model added
    # later* becomes routable — so it is no longer what an empty answer means.
    # The list itself needs the oMLX key to be read, so the answer is settled
    # in `_fill_models` once the key is known; `models_by_default` carries
    # "the operator took the default" that far.
    # `all` is the documented way to keep the list empty on purpose.
    if node.models:
        out(f"{label}: {len(node.models)} model(s) offered")
    # A key already in hand (`--omlx-key`) lets the question show the real
    # list as its default; without one the node answers 401 here and the
    # default is settled later, which is the same decision, said later.
    if options.omlx_key:
        _read_offered(node, options.omlx_key)
    default_models = ", ".join(mid for mid, _kind in node.offered)
    raw = prompter.text(f"{label} models (comma separated; empty = every "
                        f"model the node offers, `{ALL_MODELS}` = leave the "
                        f"list open for models added later)", default_models)
    typed = _split_list(raw)
    if len(typed) == 1 and typed[0].lower() == ALL_MODELS:
        node.models, node.models_by_default = [], False
    elif not typed or typed == _split_list(default_models):
        node.models, node.models_by_default = [], True
    else:
        node.models, node.models_by_default = typed, False


#: The weight range the question offers. The configuration accepts any positive
#: float, so a hand-edited `1.5` stays valid — the range is what the question
#: asks for, not what the file allows (issue #169).
WEIGHT_MIN = 1
WEIGHT_MAX = 20


def _weight_complaint(raw: str) -> Optional[str]:
    """Why *raw* is not a weight, in one sentence — None when it is one.

    The weight is relative: it says how much of the new traffic this node gets
    compared with the others, so only the ratio means anything. A number that is
    silently replaced by the default teaches the operator nothing (issue #169).
    """
    text = (raw or "").strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        return (f"'{text}' is not a number — give a whole number between "
                f"{WEIGHT_MIN} and {WEIGHT_MAX}")
    if value != int(value):
        return (f"{text} is not a whole number — give {WEIGHT_MIN} to "
                f"{WEIGHT_MAX}; the weight is a share, so 1.5 and 2 would ask "
                f"for nearly the same thing")
    if value < WEIGHT_MIN:
        return (f"a weight of {int(value)} would take the node out of the "
                f"rotation — `enabled: false` is how a node is switched off; "
                f"give {WEIGHT_MIN} to {WEIGHT_MAX}")
    if value > WEIGHT_MAX:
        return (f"{int(value)} is above {WEIGHT_MAX} — the weight is relative, "
                f"so {WEIGHT_MAX} already means {WEIGHT_MAX} times the traffic "
                f"of a node at 1")
    return None


def _ask_weight(prompter: Prompter, label: str, default: float) -> float:
    """The node's share of new threads — asked until the answer is a weight.

    The prompt says what the number does, because "weight" alone is a word from
    the configuration file, not from the operator's day (issue #169).
    """
    question = (f"{label} (relative: higher takes more new threads, "
                f"{WEIGHT_MIN}-{WEIGHT_MAX})")
    while True:
        raw = prompter.text(question, f"{default:g}")
        if not (raw or "").strip():
            return default
        complaint = _weight_complaint(raw)
        if complaint is None:
            return float(raw)
        print(complaint, file=sys.stderr)


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
        if prompter.yes_no(f"Use the oMLX key in {hint} (a copy, not oMLX's "
                           f"own store)?", True):
            try:
                key = hint.read_text(encoding="utf-8").strip()
            except OSError as exc:
                print(f"cannot read {hint}: {exc}", file=sys.stderr)
                key = ""
            if key:
                out(f"oMLX key: read from {hint}")
                return key

    key = prompter.secret("oMLX API key (in oMLX: admin panel → Settings → API "
                          "key; empty = read-only)")
    if key:
        out("oMLX key: entered")
    else:
        out("oMLX key: none — the node answers its model list with 401, so "
            "RIPPLE runs read-limited; `ripple doctor` reports it")
    return key


def _model_list_status(address: str, api_key: str) -> Optional[int]:
    """What `/v1/models` answers for this key — None when nothing answered."""
    try:
        import httpx

        headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        response = httpx.get(address.rstrip("/") + "/v1/models",
                             headers=headers, timeout=4.0)
        return response.status_code
    except Exception:                              # noqa: BLE001 — no answer
        return None


def _retry_refused_key(node: Node, prompter: Prompter,
                       out: Callable[[str], None]) -> None:
    """Ask again for a node whose key it refuses (issue #171).

    A fleet of machines each with its own oMLX key is as normal as a fleet of
    clones. With one key for all of them the second node answered 401
    and was written read-only — no model list, no residency signal, and FA-18 off
    for the whole fleet. Measured on a two-node fleet on 23.11.
    """
    if not node.addresses or not node.api_key:
        # No key was given on purpose: "empty = read-only" is an answer, and a
        # 401 is then the expected reply, not a refusal to ask about again.
        return
    for _attempt in (1, 2):
        if _model_list_status(node.addresses[0], node.api_key) != 401:
            return
        out(f"node {node.name}: the oMLX key was refused — 401 on /v1/models")
        if isinstance(prompter, AutoPrompter):
            out(f"node {node.name}: read-only, no model list")
            return
        key = prompter.secret(f"node {node.name} oMLX API key (empty = leave it "
                              f"read-only)").strip()
        if not key:
            out(f"node {node.name}: read-only, no model list")
            return
        node.api_key = key
        out(f"node {node.name}: oMLX key: entered")


def _step_scan(options: Options, nodes: list[Node], site: Any, api_key: str,
               prompter: Prompter, out: Callable[[str], None]) -> None:
    """Step 3b — sweep the site's networks for nodes, then ask about each (#187).

    `init` probes the addresses it was given and nothing else; the site's
    networks are a *label* for where this machine stands, not a search area. An
    operator who types `192.0.2.0/24` reads that as "look there" — so the sweep
    is offered, and only runs when it is answered yes: a sweep is traffic on
    someone else's network, which is why `ripple discover` keeps it behind an
    explicit `--scan` too.

    A node the sweep finds is asked the same three questions as one that
    answered on loopback (`_ask_node_detail`), and it is skipped when the fleet
    already has it — matched by `node_id` first, because the same oMLX answers
    on every address it listens on, including this machine's LAN address.
    """
    networks = [str(n) for n in (getattr(site, "networks", ()) or ()) if str(n)]
    if not networks:
        return
    # One question for both front ends (the `--yes` run answers its default,
    # `False`): a sweep nobody confirmed is traffic on someone else's network.
    if not prompter.yes_no(
            f"Scan {', '.join(networks)} for further oMLX nodes? "
            f"(a sweep: one request per address, port 8000)", False):
        out("not swept — `ripple discover --scan <CIDR>` looks for more")
        return
    try:
        from ripple.discover import scan_network_detailed   # noqa: WPS433
    except ImportError:
        out("no network scan available (ripple/discover.py) — skipped")
        return

    known_ids = {node.node_id for node in nodes if node.node_id}
    known_addresses = {a for node in nodes for a in node.addresses}
    added = 0
    for cidr in networks:
        out(f"scanning {cidr} …")
        try:
            key = api_key or None
            probes = asyncio.run(scan_network_detailed(cidr, api_key=key))
        except Exception as exc:                        # noqa: BLE001
            out(f"  {cidr}: scan failed ({exc}) — skipped")
            continue
        found = [p.found for p in probes if getattr(p, "found", None)]
        fresh = []
        for entry in found:
            if entry.node_id and entry.node_id in known_ids:
                continue
            if entry.address in known_addresses:
                continue
            fresh.append(entry)
        if not fresh:
            out(f"  {cidr}: {len(found)} answered, none new")
            continue
        for node in _nodes_from_found(fresh, [entry.address for entry in fresh]):
            out(f"  found {', '.join(node.addresses)}  "
                f"{_offer_line(node, bool(api_key))}")
            _ask_node_detail(len(nodes) + added + 1, len(nodes) + len(fresh),
                             node, options, prompter, out)
            nodes.append(node)
            added += 1
            if node.node_id:
                known_ids.add(node.node_id)
            known_addresses.update(node.addresses)
    if added:
        out(f"added {added} node(s) from the network sweep")


def _step_node_keys(options: Options, home: Path, nodes: list[Node],
                    prompter: Prompter, out: Callable[[str], None],
                    fleet_key: str = "") -> str:
    """Step 4 — the oMLX key, asked per node (issue #171).

    The first node's key is the fleet default, because the nodes are usually
    clones and one answer is then enough. Every further node is asked whether it
    takes the same key, and a node that refuses it is asked once more with its
    own — `config_document` writes `hosts.<name>.api_key` for the ones that
    differ, so the difference survives into the file.
    """
    fleet_key = ""
    for index, node in enumerate(nodes, 1):
        label = f"node {index}/{len(nodes)} ({', '.join(node.addresses)})"
        if index == 1:
            # Asked before the nodes since #180 — this is only the fallback for
            # a caller that has no answer yet.
            node.api_key = fleet_key or _step_omlx_key(options, home, prompter,
                                                       out)
        elif fleet_key and prompter.yes_no(
                f"{label}: same oMLX key as node 1?", True):
            node.api_key = fleet_key
            out(f"{label}: oMLX key: the first node's")
        else:
            node.api_key = prompter.secret(
                f"{label} oMLX API key (that node's admin panel → Settings → "
                f"API key; empty = read-only)").strip()
            out(f"{label}: oMLX key: "
                f"{'entered' if node.api_key else 'none — read-only'}")
        _retry_refused_key(node, prompter, out)
        if index == 1:
            # After the retry, so a key that had to be corrected is the one the
            # other nodes are offered as "the same".
            fleet_key = node.api_key
    return fleet_key


def _step_admin_key(prompter: Prompter,
                    out: Callable[[str], None]) -> tuple[str, bool]:
    """Step 5 — the RIPPLE admin key (issue #158). Returns `(key, generated)`.

    Empty means "generate a 32-hex one"; anything typed is used **as given** and
    never echoed — the operator already knows it. Its length is reported and
    nothing is judged: the running installation uses a 4-digit admin key on
    purpose, so a short key gets one neutral note, never a refusal.

    The generated key is not printed here: `run()` prints it once the
    configuration it belongs to is actually on disk, so a key is never handed
    out for a file that failed to be written.
    """
    typed = prompter.secret("RIPPLE admin key (empty = generate a 32-hex one)")
    if typed:
        out(f"RIPPLE admin key: {len(typed)} character(s), used as given — "
            f"not shown here")
        if len(typed) < 16:
            out("  note: shorter than the 32 hex `init` would generate; it is "
                "used as given, not replaced")
        return typed, False
    return secrets.token_hex(16), True          # 32 hex characters


def _fill_models(nodes: list[Node], api_key: str,
                 out: Callable[[str], None]) -> None:
    """Settle `models:` now that a key is known — and say what was settled.

    The conversation asks for the key after the nodes (the issue's order), so
    the first discovery may have seen a 401: what this does is fill in a
    `node_id` discovery could not read without a key, read each node's typed
    model list, and decide the allow-list where the operator took the default
    (issue #157).

    The default is every offered model, utility engines included: a node that
    lists an embedding model answers `/v1/embeddings` for it, and an allow-list
    that left it out would refuse a call the node serves. The empty allow-list
    is still the widest statement — it also covers models added later — so it
    stays what `all` means, not what an empty answer means. A list the operator
    typed is left exactly as typed.
    """
    for node in nodes:
        key = node.api_key or api_key
        if not node.node_id and key:
            found = _discover(node.addresses, key)
            for entry in found or []:
                if getattr(entry, "node_id", None):
                    node.node_id = str(entry.node_id)
                    break

    for node in nodes:
        if not (node.models_by_default or not node.models):
            continue                      # a typed list: nothing to decide
        key = node.api_key or api_key
        _read_offered(node, key)
        if _resolve_models(node, out):
            continue
        # Still empty and undecidable — the empty answer is stated, not assumed.
        reason = "no oMLX key" if not key else "the node did not answer"
        out(f"node {node.name}: allow-list empty — every model the node "
            f"offers is routable, and its model list could not be read "
            f"({reason}); name the models in `config.yaml` to restrict it")


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
    secrets_ref = _state_path(home, "secrets.yaml")
    if api_key:
        # One oMLX key for the whole fleet is the normal case (the nodes are
        # clones); a host that differs names its own entry. The key itself is
        # *not* written here (issue #179): this file is the one an operator
        # pastes into a forum, so it carries a reference into the 0600 secret
        # file that `init` writes next to the key store.
        defaults["api_key_file"] = f"{secrets_ref}#omlx.fleet"

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
        if node.api_key and node.api_key != api_key:
            # This node's key differs from the fleet default (issue #171) — and
            # it lives in the secret file under the node's own name (#179).
            entry["api_key_file"] = f"{secrets_ref}#omlx.nodes.{node.name}"
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
    if api_key:
        # The same oMLX key again, under the second name the runtime reads it
        # (issue #159). `replication.admin_key` is what the per-host state
        # sampling (`GET /admin/api/activity`) and the replication admin calls
        # use; `defaults.api_key` is only the client key RIPPLE passes through.
        # Writing the key alone must not switch replication on, so `enabled`
        # keeps its default (`false`) and the section carries nothing else.
        document["replication"] = {"admin_key_file": f"{secrets_ref}#omlx.fleet"}
    document["hosts"] = hosts
    document.update({
        "auth": {
            "enabled": True,
            "keys_file": _state_path(home, "keys.yaml"),
            "admin_key_file": f"{secrets_ref}#admin",
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
# Keeping what is about to be replaced (issue #154)
# ---------------------------------------------------------------------------
BACKUP_SUFFIX = ".bak-"
BACKUP_STAMP = "%Y%m%d-%H%M%S"


def backup_path(path: Path, *, now: datetime | None = None) -> Path:
    """`config.yaml` → `config.yaml.bak-20261123-141500`, never an existing file.

    The stamp is the moment of the replacement, and the name matches the
    `config.yaml.bak*` pattern `.gitignore` already carries. A second `--force`
    inside the same second would otherwise copy onto the first backup and leave
    one copy for two overwrites, so a name that is already taken gets a `-2`,
    `-3`, … — which still matches that pattern.
    """
    stamp = (now or datetime.now()).strftime(BACKUP_STAMP)
    candidate = path.with_name(f"{path.name}{BACKUP_SUFFIX}{stamp}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}{BACKUP_SUFFIX}{stamp}-{counter}")
        counter += 1
    return candidate


def copy_private(src: Path, dest: Path) -> None:
    """Copy `src` to `dest` byte for byte, mode `0600`, atomically.

    `copyfile`, not a re-render: the backup is evidence of what was on disk, so
    it must not pass through YAML — a comment, a key order or an anchor would
    not survive that, and a 9 KB configuration is not reproducible from memory.
    The temp-file dance is the one `write_private` uses: a half-written backup
    is worse than none, because it looks like a backup.
    """
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{dest.name}.",
                                    suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        tmp.chmod(FILE_MODE)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def secrets_document(api_key: str, nodes: list[Node], admin_key: str) -> str:
    """The 0600 secret file the configuration points into (issue #179).

    One place for the credentials, so `config.yaml` can be pasted anywhere
    without a key coming along. The names are the ones `config_document`
    references: `omlx.fleet`, `omlx.nodes.<name>`, `admin`.
    """
    body: dict[str, Any] = {}
    omlx: dict[str, Any] = {}
    if api_key:
        omlx["fleet"] = api_key
    differing = {n.name: n.api_key for n in nodes
                 if n.api_key and n.api_key != api_key}
    if differing:
        omlx["nodes"] = differing
    if omlx:
        body["omlx"] = omlx
    if admin_key:
        body["admin"] = admin_key
    header = (
        "# RIPPLE — credentials, written by `ripple init` (mode 0600).\n"
        "#\n"
        "# The configuration does not contain these values; it names them:\n"
        "#   defaults.api_key_file: ~/.ripple/secrets.yaml#omlx.fleet\n"
        "#   hosts[].api_key_file:  ~/.ripple/secrets.yaml#omlx.nodes.<name>\n"
        "#   auth.admin_key_file:   ~/.ripple/secrets.yaml#admin\n"
        "#\n"
        "# Keep this file out of every backup you share, and out of git.\n")
    return header + yaml.safe_dump(body, sort_keys=True, allow_unicode=True)


def backup_existing(path: Path, label: str,
                    out: Callable[[str], None]) -> Path | None:
    """Copy `path` aside before it is replaced; `None` when there was nothing there.

    A first run on a fresh machine must not leave a `.bak` of a file that never
    existed: that is litter an operator would later read as "there was a
    configuration here". The path is printed because a backup nobody can find is
    not a backup — the whole point is what the operator does next.
    """
    if not path.exists():
        return None
    dest = backup_path(path)
    copy_private(path, dest)
    out(f"backup: {dest}  (the previous {label}, copied byte for byte)")
    return dest


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
    #: The port that was asked for, when `init` had to move to another one.
    moved_from: int | None = None


def _ask_all(options: Options, home: Path, prompter: Prompter,
             out: Callable[[str], None]) -> Answers | None:
    """Steps 1-4: port, oMLX key, nodes, location. `None` = unusable answer.

    The key comes before the discovery since #180: with it the node list carries
    the model count it actually read, and a key that does not fit is visible in
    that same line instead of one step later.
    """
    choice = _step_port(options, prompter, out)
    if choice is None:
        return None
    port = choice.port
    out(f"port: {port}")

    api_key = _step_omlx_key(options, home, prompter, out)

    nodes = _step_nodes(options, prompter, out, api_key=api_key)
    if nodes is None:
        return None

    # The location comes after the nodes: whether a node is inside it is the
    # question this whole block is for (issue #67).
    site = _step_site(options, nodes, prompter, out)

    # The networks are named now, so the sweep can be offered with them (#187).
    _step_scan(options, nodes, site, api_key, prompter, out)

    api_key = _step_node_keys(options, home, nodes, prompter, out,
                              fleet_key=api_key)
    _fill_models(nodes, api_key, out)
    return Answers(port=port, nodes=nodes, api_key=api_key, site=site,
                   moved_from=choice.requested if choice.requested != port else None)


def _say_port_move(answers: Answers, out: Callable[[str], None]) -> None:
    """One line at the end when the port moved (issue #151).

    The question that moves it is answered with its default under `--yes`, so a
    scripted run would otherwise read as "configured on 8010" while the file,
    launchd and every client still point at the other number.
    """
    if answers.moved_from is not None:
        out(f"listening on {answers.port}, not {answers.moved_from} — "
            f"{answers.moved_from} was not free")


def run(options: Options, prompter: Prompter,
        out: Callable[[str], None] = print) -> int:
    """The whole cold start, from the port question to the test request.

    One function for both paths: `prompter` decides whether a human is asked or
    a default is taken. Returns the process exit code (0 ok, 1 refused/aborted,
    2 written but the check failed).
    """
    from .config import ConfigError, load_config

    home = (options.home_dir or Path.home()).expanduser().resolve()
    # The configuration belongs to the machine, not to the directory the install
    # happened to run in (issue #183): `./install.sh` runs inside the checkout, so
    # `Path.cwd()` put a stranger's config.yaml into the clone — invisible to a
    # `ripple` started anywhere else, and gone after the next re-clone.
    config_path = (options.config_path or (home / ".ripple" / "config.yaml"))
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
    out(f"secrets:   {home / '.ripple' / 'secrets.yaml'}")
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

    # -- 4. RIPPLE admin key (asked; empty = generated, then printed once) ---
    try:
        admin_key, admin_generated = _step_admin_key(prompter, out)
    except (Cancelled, KeyboardInterrupt):
        print("\ncancelled — nothing was written", file=sys.stderr)
        return 1

    # -- 5. keep what is about to be replaced (issue #154) ------------------
    # `--force` is the flag an operator reaches for exactly when the machine is
    # already in trouble, so the destructive path runs at the worst moment: a
    # 9 KB configuration with two hosts, sites and placement tuning was replaced
    # with the 1.3 KB first-run document and nothing was left to go back to.
    # Both files are copied before either is written, and a failed copy aborts
    # the run with both still in place — overwriting without a backup is the one
    # thing this step exists to prevent.
    if options.force:
        try:
            backup_existing(config_path, "config", out)
            backup_existing(keys_path, "key store", out)
            backup_existing(home / ".ripple" / "secrets.yaml", "secrets", out)
        except OSError as exc:
            print(f"cannot back up before overwriting: {exc} — nothing was "
                  f"written", file=sys.stderr)
            return 1
        out("")

    # -- 6./7. write, then show the client key exactly once -----------------
    document = config_document(port=port, nodes=nodes, api_key=api_key,
                               admin_key=admin_key, home=home, site=answers.site)
    secrets_path = home / ".ripple" / "secrets.yaml"
    if api_key or admin_key:
        try:
            write_private(secrets_path, secrets_document(api_key, nodes,
                                                         admin_key))
        except OSError as exc:
            print(f"cannot write the secret file {secrets_path}: {exc} — "
                  f"nothing was written", file=sys.stderr)
            return 1
        out(f"wrote {secrets_path} (mode 0600)")
    try:
        write_private(config_path,
                      render_config(document, port=port, path=str(config_path)))
    except OSError as exc:
        print(f"cannot write {config_path}: {exc}", file=sys.stderr)
        return 1
    out("")
    out(f"wrote {config_path} (mode 0600)")
    if admin_generated:
        # Printed here, not when it was generated: a key for a file that failed
        # to be written is a key that does not open anything. Same shape as the
        # client key below, so a fresh install ends with both secrets in hand
        # and nobody has to open the 0600 file to copy one out of it (issue #158).
        out("RIPPLE admin key — shown once, store it now:")
        out(f"  {admin_key}")
        # Since #179 the key is not in config.yaml — the file names it. The line
        # said "also in config.yaml at auth.admin_key" for one release, which
        # sent the operator to a file that no longer carries it.
        out(f"  (also in {home / '.ripple' / 'secrets.yaml'} at admin; it opens "
            "/keys and /admin/*)")
        out("")

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

    # -- 8. the existing --check path, then one real request ----------------
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"the written configuration does not load: {exc}", file=sys.stderr)
        return 2

    if not options.check:
        out("")
        out("--no-check: configuration check and test request skipped")
        _say_port_move(answers, out)
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
    _say_port_move(answers, out)
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
                   help="replace an existing config.yaml / key store; both are "
                        "copied aside first as <file>.bak-<YYYYmmdd-HHMMSS>")
    parser.add_argument("--port", type=int, default=None,
                        help=f"listen port (default {DEFAULT_PORT}; "
                             f"{PORT_MIN}-{PORT_MAX}, this project uses "
                             f"{PORT_RANGE[0]}-{PORT_RANGE[1]}; a busy port "
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
    if args.port is not None and _parse_port(str(args.port)) is None:
        # The flag gets the same three sentences as the question (#166) — a
        # configuration that cannot bind is worse than a refused flag.
        print(_port_complaint(str(args.port)), file=sys.stderr)
        return 2
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
