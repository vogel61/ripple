"""Replicating a model from one node to another (issue #13).

A model that exists on one node is not routable on the other until its files are
there and oMLX has re-read its model directories. This module does that in one
operator action: plan, preflight, copy, register, copy the parameters back, and a
report of what could not be replicated.

Three shapes decided by measurement, not by guessing (see `docs/replication.md`
for the calls behind each one):

* **The settings payload is the `settings` object of `GET /admin/api/models`.**
  The matching `PUT /admin/api/models/{id}/settings` takes a *flat* body
  (`ModelSettingsRequest`, `extra="forbid"`). The GET returns 86 keys, the PUT
  accepts 83 of them; `display_name`, `description` and `active_profile_name`
  come back on the read and are rejected on the write (422). Those three are
  dropped (`settings_exclude`) and the rest is written as it was read. `null`
  means "the default" in oMLX, so null values are not sent — a fresh model
  already has them.
* **`POST /admin/api/reload` does not require an empty node; it empties it.**
  It unloads every loaded model, clears the pool, re-discovers the directories
  and then preloads the *pinned* models. The maintenance window is therefore
  mandatory, not polite: the reload takes the target's residency away by itself.
* **The copy is staged in a dot-directory.** oMLX's discovery skips entries whose
  name starts with `.` (`model_discovery.py`), and it only rescans on an explicit
  reload (or after a finished download) — so a half-copied directory is never
  registered, and an interrupted copy resumes from the staging directory.
* **`rsync` is openrsync on macOS, GNU rsync on Linux, and the two take different
  progress flags.** openrsync has no `--info=progress2`: it prints its usage and
  exits 1, which is what made the first real copy (issue #13, 15.11.2026) die
  after 0,15 s with a message that looked like a truncated error. The flag is
  therefore *measured* on the transport that will run the copy
  (`SshTransport.rsync_progress_flag`) and the output of both formats is read by
  `ProgressReader`; a rejected flag falls back to `--progress` and then to no
  progress at all instead of failing the job (issue #26).
* **The side of the copy that is this machine never goes over SSH.** The local
  host is the one whose `hosts[].addresses` contains a loopback entry; its
  `replication.address` is ignored, because rsync copies local→remote or
  remote→local and never remote→remote — with an address configured for both
  nodes the first real copy died on exactly that (issue #28). No local host
  identifiable means no plan: it is refused with the fix in the message.

Pure logic and the job state machine; the network lives behind `Transport`, which
tests replace with a stub. No FastAPI import here: the admin endpoints are in
`proxy.py`, in the shape of `/admin/nodes/{n}/calibrate`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol, Sequence
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("ripple.replication")

# The staging directory under the target's model root. A leading dot is what
# makes it invisible to oMLX's discovery, which skips `.`-entries — a partial
# copy must never be registered as a model.
STAGING_DIR = ".replication-incoming"

# Keys `GET /admin/api/models` reports but `PUT /admin/api/models/{id}/settings`
# rejects (`ModelSettingsRequest` has `extra="forbid"`). Measured 15.11.2026
# against both hosts: 86 keys read, 83 accepted, these three are the difference.
DEFAULT_SETTINGS_EXCLUDE: tuple[str, ...] = (
    "display_name", "description", "active_profile_name")

# `rsync --info=progress2` writes one line per update, separated by carriage
# returns: "     1,234,567  45%  123.45MB/s    0:01:23". openrsync's `--progress`
# writes the same shape per *file*, and appends its counter to the line that
# ends the file: "(xfer#1, to-check=1/3)".
_PROGRESS_RE = re.compile(
    r"(?P<bytes>[\d,]+)\s+(?P<percent>\d{1,3})%\s+"
    r"(?P<rate>[\d.,]+[kKMGTP]?B/s)\s+(?P<eta>\d+:\d{2}(?::\d{2})?)")
# The two spellings are two different counters, not two names for one:
# openrsync's `to-check=k/m` is the number of file-list entries *checked so far*
# out of m (measured: 2 files → "1/3", then "2/3", the copy's own root directory
# being the entry that is never reported), GNU's `to-chk=k/m` is the number of
# entries *still to check*. Both are used below, each the way its rsync means it.
_COUNTER_RE = re.compile(
    r"\(xfer#(?P<xfer>\d+),\s*(?P<label>to-check|to-chk)=(?P<a>\d+)/(?P<b>\d+)\)")
# The closing summary both implementations print for `--stats`.
_STATS_SENT_RE = re.compile(
    r"sent\s+(?P<sent>[\d,]+)\s+bytes\s+received\s+(?P<received>[\d,]+)\s+bytes"
    r"(?:\s+(?P<rate>[\d.,]+)\s+bytes/sec)?")
_STATS_TOTAL_RE = re.compile(r"total size is\s+(?P<total>[\d,]+)")
# What a rsync prints when it does not know an option. openrsync:
# "rsync: unrecognized option `--info=progress2'" + its usage; GNU:
# "rsync: --info=progress2: unknown option" + "syntax or usage error".
_REJECTED_MARKERS = ("unrecognized option", "unknown option", "invalid option",
                     "illegal option", "syntax or usage error", "usage error")


class ReplicationError(Exception):
    """A job that cannot finish for a reason the operator can read."""


# ---------------------------------------------------------------------------
# What a node reports about one model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RemoteModel:
    """One entry of `GET /admin/api/models`, reduced to what a copy needs."""

    id: str
    model_path: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    pinned: bool = False
    is_favorite: bool = False
    is_hidden: bool = False
    estimated_size: int = 0
    actual_size: int = 0
    loaded: bool = False

    @classmethod
    def from_api(cls, raw: Any) -> "RemoteModel | None":
        """Tolerant: an entry without an id is not a model, everything else may
        be missing — a field that is not there is not an error, it is absent."""
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        settings = raw.get("settings")
        return cls(
            id=str(raw["id"]),
            model_path=str(raw.get("model_path") or ""),
            settings=dict(settings) if isinstance(settings, dict) else {},
            pinned=bool(raw.get("pinned")),
            is_favorite=bool(raw.get("is_favorite")),
            is_hidden=bool(raw.get("is_hidden")),
            estimated_size=_int(raw.get("estimated_size")) or 0,
            actual_size=_int(raw.get("actual_size")) or 0,
            loaded=bool(raw.get("loaded")),
        )

    @property
    def size_bytes(self) -> int:
        """The bytes to move: the measured size when oMLX has one, else the
        estimate. `actual_size` is 0 for a model that was never loaded on this
        node — which is exactly the case for a model that is about to be copied."""
        return self.actual_size if self.actual_size > 0 else self.estimated_size


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def models_from_api(payload: Any) -> list[RemoteModel]:
    """The `{"models": [...]}` answer of `/admin/api/models`, tolerantly."""
    raw = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    return [m for m in (RemoteModel.from_api(item) for item in raw) if m is not None]


# ---------------------------------------------------------------------------
# Plan: what a POST would do
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    """The preflight verdict, in the shape the confirmation dialog needs.

    Every refusal is collected, not just the first: an operator who has to fix
    "SSH unreachable" and "not enough space" should see both before starting
    over. `as_dict()` is the wire shape (GET plan and POST answer alike).
    """

    source: str
    target: str
    model: str
    source_path: str = ""
    relative: str = ""
    target_root: str = ""
    target_path: str = ""
    staging_path: str = ""
    size_bytes: int = 0
    required_bytes: int = 0
    free_bytes: int | None = None
    overwrite: bool = False
    already_there: bool = False
    settings_keys: int = 0
    ssh_target: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def refuse(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "model": self.model,
            "source_path": self.source_path,
            "relative": self.relative,
            "target_root": self.target_root,
            "target_path": self.target_path,
            "staging_path": self.staging_path,
            "ssh_target": self.ssh_target,
            "size_bytes": self.size_bytes,
            "size_gb": round(self.size_bytes / 1e9, 2),
            "required_bytes": self.required_bytes,
            "free_bytes": self.free_bytes,
            "free_gb": (round(self.free_bytes / 1e9, 2)
                        if self.free_bytes is not None else None),
            "overwrite": self.overwrite,
            "already_there": self.already_there,
            "settings_keys": self.settings_keys,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def local_root(root: str) -> str:
    """A source-side model root, with `~` expanded.

    The source of a copy is the machine RIPPLE runs on, so its home is the one
    `~` has to mean here (issue #20). A *target* root must never go through this:
    that home belongs to the other machine and is not known locally.
    """
    return os.path.expanduser(str(root or "").strip())


# ---------------------------------------------------------------------------
# Which configured host is this machine (issue #28)
# ---------------------------------------------------------------------------
# `hosts[].addresses` entries that cannot leave the machine. They are the
# operator's own statement that this node is the one RIPPLE runs on — read from
# the configuration, never guessed from `role` or the node_id, and never probed:
# a probe would answer "which node is up", not "which node am I".
LOOPBACK_NAMES: tuple[str, ...] = ("localhost", "::1")
LOOPBACK_PREFIXES: tuple[str, ...] = ("127.",)


def _address_host(address: Any) -> str:
    """The host part of one `hosts[].addresses` entry, lowercased.

    The entries are URLs (`http://localhost:8000`) but a bare `localhost:8000`
    must read the same way, and `http://[::1]:8000` has to come out as `::1` for
    `is_loopback_address` to recognise it.
    """
    text = str(address or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"//{text}"          # a bare host:port needs a netloc for urlsplit
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:              # a malformed entry is not a loopback one
        return ""


def is_loopback_address(address: Any) -> bool:
    """Is this `hosts[].addresses` entry this machine? `localhost`, `127/8`, `::1`."""
    host = _address_host(address)
    return host in LOOPBACK_NAMES or host.startswith(LOOPBACK_PREFIXES)


def local_host_name(cfg: Any) -> str:
    """The configured host that is this machine, or "" when none says so.

    The marker is a loopback entry in `hosts[].addresses`: the M5 lists
    `http://localhost:8000`, the M3 lists nothing loopback (measured against the
    live `config.yaml`, 16.11.2026). When several hosts carry one — a test fleet
    on loopback ports, or two nodes on one machine — `role: entry` wins, because
    RIPPLE is the entry's machine; otherwise configuration order decides.
    """
    hosts = [h for h in (getattr(cfg, "hosts", None) or [])
             if any(is_loopback_address(a)
                    for a in (getattr(h, "addresses", None) or []))]
    if not hosts:
        return ""
    if len(hosts) > 1:
        entries = [h for h in hosts if str(getattr(h, "role", "") or "") == "entry"]
        if len(entries) == 1:
            return str(entries[0].name)
    return str(hosts[0].name)


def host_is_local(host_cfg: Any, cfg: Any) -> bool:
    """Is this host configuration the machine RIPPLE runs on?

    `hosts[].name` is the stable identity of a node in this configuration (it is
    the key for slots and affinity), so the name is what is compared.
    """
    name = str(getattr(host_cfg, "name", "") or "")
    return bool(name) and name == local_host_name(cfg)


@dataclass(frozen=True)
class LocalSide:
    """The local host of a copy, and the address its configuration wanted.

    `name` empty means no host could be identified as this machine; the preflight
    refuses then, because the two sides of the copy are indistinguishable and
    rsync cannot copy from one remote host to another. `ignored_address` is the
    `replication.address` configured for that host and *not* used — the plan warns
    about it, because a configured value that is dropped in silence is what costs
    an evening (issue #28).
    """

    name: str = ""
    ignored_address: str = ""


def configured_ssh_targets(host_cfg: Any, cfg: Any) -> list[str]:
    """Every SSH destination `hosts[].replication.address` names, in order.

    The list is ordered on purpose (issue #67 point 3): the bridge first, the LAN
    as the fallback. Order is the whole rule — a site-based rule would point the
    copy at a bridge that currently has no address, which is exactly what the
    Thunderbolt link did on 17.11.2026 (a self-assigned 169.254 address).
    """
    rep = getattr(host_cfg, "replication", None)
    raw = getattr(rep, "address", None) or ()
    if isinstance(raw, str):
        raw = [raw]
    user = str(getattr(rep, "ssh_user", "")
               or getattr(getattr(cfg, "replication", None), "ssh_user", "")
               or "").strip()
    out: list[str] = []
    for entry in raw:
        text = str(entry or "").strip()
        if not text:
            continue
        target = f"{user}@{text}" if user and "@" not in text else text
        if target not in out:
            out.append(target)
    return out


def configured_ssh_target(host_cfg: Any, cfg: Any) -> str:
    """The first configured SSH destination — `""` when none is configured."""
    targets = configured_ssh_targets(host_cfg, cfg)
    return targets[0] if targets else ""


def local_side(cfg: Any) -> LocalSide:
    """Which host is this machine, and which of its addresses is therefore ignored."""
    name = local_host_name(cfg)
    if not name:
        return LocalSide()
    host_cfg = next((h for h in (getattr(cfg, "hosts", None) or [])
                     if str(getattr(h, "name", "") or "") == name), None)
    return LocalSide(name=name,
                     ignored_address=", ".join(configured_ssh_targets(host_cfg, cfg)))


def root_refusal(root: str, *, host: str, side: str) -> str | None:
    """Why `root` cannot be a `side` ("source"/"target") root of `host`.

    `None` means the root is usable. A root that is still relative after
    expanding `~` is refused rather than resolved against RIPPLE's working
    directory, and a target root is not expanded at all: `shlex.quote` would
    carry a literal `~` to the target's shell, which then creates a directory
    named `~` there — the worst outcome, because the plan would look fine.
    """
    text = str(root or "").strip()
    if not text:
        return (f"an empty model root is configured for {host} — write the "
                f"absolute directory")
    if side == "target":
        if text.startswith("~"):
            return (f"target model root {text!r} of {host} starts with `~` — the "
                    f"home of {host} is not known on this machine, so it is not "
                    f"expanded; write the absolute path as it exists on {host}")
        if not os.path.isabs(text):
            return (f"target model root {text!r} of {host} is relative — write "
                    f"the absolute path as it exists on {host}")
        return None
    if not os.path.isabs(local_root(text)):
        return (f"model root {text!r} of {host} is still relative after "
                f"expanding `~` — write an absolute path")
    return None


def lock_refusal(target: str, locked: Sequence[str]) -> str:
    """Why a copy to `target` cannot start while `locked` is resident there.

    The job's last step is `POST /admin/api/reload`, which unloads every loaded
    model on the node before it re-discovers the directories — so a locked model
    would be gone, and the model it was holding memory for is exactly what the
    operator wanted to keep. The refusal is worded like the calibration one, so
    the operator reads the same sentence in both places.
    """
    names = ", ".join(str(m) for m in locked)
    plural = "models" if len(locked) > 1 else "model"
    undo = ", ".join(f"DELETE /admin/models/{m}/lock" for m in locked)
    return (f"{target} has the locked {plural} {names} loaded; the reload at the end "
            f"of a replication unloads every model on {target}, which would take "
            f"{'them' if len(locked) > 1 else 'it'} away. Unlock with {undo}, or "
            f"replicate to another node")


def relative_under(model_path: str, roots: Sequence[str]) -> tuple[str, str] | None:
    """`(root, relative)` for the configured root that contains `model_path`.

    Roots are read as source-side roots, so `~` is expanded (issue #20) — the
    source is this machine. A root that stays relative cannot match an absolute
    `model_path` and is skipped here; `root_refusal` names it in the preflight.

    Longest prefix wins, so a root that is itself inside another root does not
    shadow the more specific one. The relative part is what is recreated on the
    target — the copy keeps the layout (`Jundot/Qwen3.8-…`), it does not flatten
    it, because that is how oMLX names the model afterwards.
    """
    path = os.path.normpath(str(model_path or ""))
    if not path:
        return None
    best: tuple[str, str] | None = None
    for root in roots or ():
        expanded = local_root(root)
        if not os.path.isabs(expanded):
            continue
        norm = os.path.normpath(expanded.rstrip("/") or "/")
        if not norm:
            continue
        if path == norm:
            continue
        prefix = norm if norm.endswith("/") else norm + "/"
        if path.startswith(prefix):
            rel = path[len(prefix):].strip("/")
            if not rel:
                continue
            if best is None or len(norm) > len(best[0]):
                best = (norm, rel)
    return best


def staging_for(target_root: str, relative: str) -> str:
    """Where the copy lands before it is moved into place (dot-directory)."""
    return os.path.join(target_root.rstrip("/"), STAGING_DIR, relative.strip("/"))


def settings_payload(settings: dict[str, Any],
                     exclude: Sequence[str] = DEFAULT_SETTINGS_EXCLUDE) -> dict[str, Any]:
    """The flat body for `PUT /admin/api/models/{id}/settings`.

    Drops the three keys oMLX reports but does not accept (`extra="forbid"` would
    answer 422 for the whole body) and drops `null` values: in oMLX `null` *is*
    the default, and a freshly registered model already has it. Sending nulls
    would only add keys to the `sent` set, which is what makes oMLX auto-unload a
    loaded model for a change that is not one.
    """
    skip = set(exclude or ())
    return {str(k): v for k, v in (settings or {}).items()
            if k not in skip and v is not None}


def _same(a: Any, b: Any, tolerance: float = 1e-9) -> bool:
    """Equality with a float tolerance — 0.95 must equal 0.9500000000000001."""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) is bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tolerance * max(1.0, abs(float(a)))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k], tolerance) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y, tolerance) for x, y in zip(a, b))
    return a == b


def compare_settings(sent: dict[str, Any], readback: dict[str, Any],
                     tolerance: float = 1e-9) -> list[dict[str, Any]]:
    """Which of the sent keys did not arrive. Empty list = the round trip held.

    Only the keys that were sent are compared: the read-back carries every
    default oMLX has, and a default that differs from what we sent is the
    finding, while a default we never touched is not.
    """
    out: list[dict[str, Any]] = []
    for key, want in (sent or {}).items():
        if key not in (readback or {}):
            out.append({"key": key, "source": want, "target": None,
                        "reason": "not reported by the target"})
            continue
        got = readback[key]
        if not _same(want, got, tolerance):
            out.append({"key": key, "source": want, "target": got,
                        "reason": "different value"})
    return out


@dataclass
class Progress:
    """One update of a running copy.

    `bytes_done` is the copy's bytes, not the current file's, and `percent` is
    the copy's percentage — both formats are reduced to that, because a caller
    that has to know which rsync is running would get it wrong.
    """

    bytes_done: int = 0
    percent: float = 0.0
    rate: str = ""
    eta: str = ""
    files_done: int = 0
    files_total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"bytes_done": self.bytes_done, "percent": self.percent,
                "rate": self.rate, "eta": self.eta}


@dataclass(frozen=True)
class Stats:
    """The `--stats` summary: the numbers that survive a missing progress flag."""

    bytes_total: int = 0
    sent: int = 0
    received: int = 0
    bytes_per_second: float = 0.0

    @property
    def rate(self) -> str:
        return human_rate(self.bytes_per_second)


def human_rate(bytes_per_second: float) -> str:
    """`369425869` → `"369.43MB/s"`, the unit rsync itself prints (10^6)."""
    for unit, factor in (("GB/s", 1e9), ("MB/s", 1e6), ("kB/s", 1e3)):
        if bytes_per_second >= factor:
            return f"{bytes_per_second / factor:.2f}{unit}"
    return f"{bytes_per_second:.0f}B/s"


def _int_or_zero(text: Any) -> int:
    try:
        return int(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def parse_stats(text: str) -> Stats | None:
    """The `--stats` summary of a finished copy, or None if it is not there.

    `total size is N` is the model's size (what the report wants); `sent X bytes
    … Z bytes/sec` is the protocol traffic, which is a little more than the
    files because it carries the file list. Both rsyncs print the same block.
    """
    if not text:
        return None
    sent = _STATS_SENT_RE.search(text)
    total = _STATS_TOTAL_RE.search(text)
    if sent is None and total is None:
        return None
    rate_text = sent.group("rate") if sent is not None else None
    try:
        rate = float(str(rate_text).replace(",", "")) if rate_text else 0.0
    except ValueError:
        rate = 0.0
    return Stats(
        bytes_total=_int_or_zero(total.group("total") if total is not None else 0),
        sent=_int_or_zero(sent.group("sent") if sent is not None else 0),
        received=_int_or_zero(sent.group("received") if sent is not None else 0),
        bytes_per_second=rate)


def _fmt_hms(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    return f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"


class ProgressReader:
    """rsync output → `Progress`, for both progress formats (issue #26).

    `flavour` is which rsync wrote the stream, and it cannot be guessed line by
    line: GNU's cumulative `--info=progress2` line and openrsync's per-file
    `--progress` line have the same shape, and only the flag that was passed
    tells them apart. `"gnu"` takes the numbers as they are; `"openrsync"`
    turns the file counter into the copy's percentage; `"auto"` reads like GNU
    until it sees a counter, which is what a caller that does not know gets.

    A block read can end in the middle of a number, so only whole `\\r`/`\\n`
    segments are parsed and the tail is kept until the next `feed()`.
    """

    def __init__(self, *, flavour: str = "auto", total_bytes: int = 0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.flavour = flavour
        self.total_bytes = int(total_bytes or 0)
        self.clock = clock
        self._buffer = ""
        self._started: float | None = None
        self._done_bytes = 0          # bytes of the files that are finished
        self._checked = 0             # file-list entries checked (openrsync)
        self._entries = 0             # file-list entries the copy will check
        self._files_done = 0

    # -- feeding -----------------------------------------------------------
    def feed(self, text: str) -> Progress | None:
        """Parse the whole segments in `text`; the last update wins."""
        if not text:
            return None
        if self._started is None:
            self._started = self.clock()
        self._buffer += text
        segments = re.split(r"[\r\n]", self._buffer)
        self._buffer = segments.pop()
        update: Progress | None = None
        for segment in segments:
            found = self._segment(segment)
            if found is not None:
                update = found
        return update

    def flush(self) -> Progress | None:
        """Parse what is left in the buffer — a stream that ends without a newline."""
        rest, self._buffer = self._buffer, ""
        return self._segment(rest) if rest else None

    # -- one line ----------------------------------------------------------
    def _segment(self, segment: str) -> Progress | None:
        match = _PROGRESS_RE.search(segment or "")
        if not match:
            return None
        value = _int_or_zero(match.group("bytes"))
        if not value:
            return None
        share = min(1.0, float(match.group("percent")) / 100.0)
        counter = _COUNTER_RE.search(segment)
        if counter is not None:
            self.flavour = "openrsync"
            self._done_bytes += value
            self._files_done += 1
            if counter.group("label") == "to-check":
                # openrsync: k entries checked so far out of m, and the last
                # file's k is m-1 — the copy's root directory is the entry it
                # counts but never prints.
                self._checked = int(counter.group("a"))
                self._entries = max(1, int(counter.group("b")) - 1)
            else:
                # GNU `to-chk`: k entries are still to check out of m.
                remaining, total = int(counter.group("a")), int(counter.group("b"))
                self._checked, self._entries = max(0, total - remaining), max(1, total)
            percent = 100.0 * min(1.0, self._checked / self._entries)
            done = self._done_bytes
        elif self.flavour == "openrsync":
            done = self._done_bytes + value
            if self._entries:
                percent = 100.0 * min(1.0, (self._checked + share) / self._entries)
            elif self.total_bytes > 0:
                percent = 100.0 * min(1.0, done / self.total_bytes)
            else:
                # Nothing has finished yet, so there is no total to divide by:
                # the file's own share is the only honest number there is.
                percent = 100.0 * share
        else:
            # GNU progress2 is cumulative and already overall.
            done = value
            self._done_bytes = value
            percent = 100.0 * share
        return Progress(
            bytes_done=int(done), percent=round(percent, 1),
            rate=match.group("rate"),
            eta=self._eta(percent, match.group("eta")),
            files_done=self._files_done, files_total=self._entries)

    def _eta(self, percent: float, printed: str) -> str:
        """openrsync's eta is the current *file's*, so it is recomputed.

        From the copy's own percentage and the time it has taken so far — the
        one estimate that is about the model and not about the file in flight.
        GNU's `--info=progress2` already prints the copy's eta; it is kept.
        """
        if self.flavour != "openrsync":
            return printed
        if percent <= 0.0 or percent >= 100.0 or self._started is None:
            return printed if percent < 100.0 else "00:00:00"
        elapsed = max(0.0, self.clock() - self._started)
        return _fmt_hms(elapsed * (100.0 - percent) / percent)


def parse_progress(line: str, *, flavour: str = "auto", total_bytes: int = 0,
                   clock: Callable[[], float] = time.monotonic) -> Progress | None:
    """The last progress update of one line or chunk, or None for anything else.

    A convenience wrapper around `ProgressReader` for a single blob; the copy
    itself uses one reader across all its reads, because a block can end in the
    middle of a line.
    """
    if not line:
        return None
    reader = ProgressReader(flavour=flavour, total_bytes=total_bytes, clock=clock)
    return reader.feed(line) or reader.flush()


def option_rejected(output: str) -> bool:
    """Did this rsync refuse an option? Both implementations say so in words."""
    text = (output or "").lower()
    return any(marker in text for marker in _REJECTED_MARKERS)


def preflight(*, source: str, target: str, model: str, entry: RemoteModel | None,
              source_roots: Sequence[str], target_roots: Sequence[str],
              overwrite: bool, free_bytes: int | None, target_has_model: bool | None,
              ssh_ok: bool, ssh_error: str | None, ssh_target: str,
              local: LocalSide | None = None,
              factor: float = 1.05) -> Plan:
    """The verdict, from facts a caller has already gathered. No I/O here.

    Refuses rather than half-copies (issue #13, point 2): every check that can be
    made before the first byte is written is made before the first byte is
    written, and each failure is named.

    `local` is which host is this machine (`local_side(cfg)`) and `ssh_target` is
    the *target's* SSH destination, empty when the target is that host. Both are
    facts, not derived here: a preflight that cannot say which side is local
    refuses, because rsync would then be asked for a remote→remote copy, which it
    cannot do (issue #28).
    """
    plan = Plan(source=str(source), target=str(target), model=str(model),
                overwrite=bool(overwrite), free_bytes=free_bytes,
                ssh_target=ssh_target)

    local = local or LocalSide()
    local_is_source = bool(local.name) and local.name == str(source)
    local_is_target = bool(local.name) and local.name == str(target)

    if not str(model or "").strip():
        plan.refuse("no model was named")
    if source == target:
        plan.refuse(f"source and target are the same node ({source})")
    if not local.name:
        # rsync copies local→remote or remote→local, never remote→remote. Without
        # an identified local host the two sides cannot be told apart, so no argv
        # may be built at all — and the operator gets the fix, not rsync's
        # "both source and destination cannot be remote files" (issue #28).
        plan.refuse(
            f"no host is identifiable as this machine: no hosts[].addresses entry "
            f"of {source} or {target} is a loopback address, so the local side of "
            f"the copy cannot be told from the remote one and rsync cannot copy "
            f"from one remote host to another. Give the host that runs RIPPLE a "
            f"loopback address in hosts[].addresses (localhost, 127.0.0.1 or "
            f"[::1], e.g. http://localhost:8000).")
    elif not (local_is_source or local_is_target):
        plan.refuse(
            f"neither {source} nor {target} is this machine ({local.name}), so both "
            f"sides of the copy would be remote — rsync cannot do that. Check "
            f"which hosts[].addresses carries a loopback entry.")
    elif local.ignored_address:
        side = ("source" if local_is_source else
                "target" if local_is_target else "local side")
        plan.warn(
            f"{local.name} is this machine (loopback in hosts[].addresses), so its "
            f"hosts[].replication.address {local.ignored_address} is ignored: the "
            f"{side} of the copy is a plain path, not an SSH destination.")

    if entry is None:
        plan.refuse(f"{source} does not report a model {model!r} — "
                    f"nothing to copy")
        return plan

    plan.source_path = entry.model_path
    plan.settings_keys = len(settings_payload(entry.settings))
    plan.size_bytes = entry.size_bytes
    plan.required_bytes = int(round(plan.size_bytes * max(1.0, float(factor))))

    # Roots are configuration and are checked before anything is derived from
    # them (issue #20). On the source side `~` is expanded — this is the machine
    # that runs RIPPLE, so its home is the right one. On the target side it is
    # not: an unexpanded `~` would be quoted into the remote shell and created
    # there as a literal directory, so such a root is refused by name.
    configured_source_roots = list(source_roots or ())
    for root in configured_source_roots:
        reason = root_refusal(root, host=str(source), side="source")
        if reason:
            plan.refuse(reason)
    expanded_source_roots = [local_root(r) for r in configured_source_roots]

    usable_target_roots: list[str] = []
    for root in target_roots or ():
        reason = root_refusal(root, host=str(target), side="target")
        if reason:
            plan.refuse(reason)
        else:
            usable_target_roots.append(str(root).strip())

    if not entry.model_path:
        plan.refuse(f"{source} reports no model_path for {model} — there is no "
                    f"directory to copy")
    else:
        found = relative_under(entry.model_path, expanded_source_roots)
        if found is None:
            plan.refuse(
                f"{entry.model_path} is under none of the configured model roots "
                f"of {source} ({', '.join(expanded_source_roots) or 'none'}) — the "
                f"relative subpath on the target cannot be derived. Set "
                f"hosts[].replication.model_roots for {source}.")
        else:
            plan.relative = found[1]

    if not target_roots:
        plan.refuse(f"no model root is configured for {target} "
                    f"(hosts[].replication.model_roots or replication.model_roots)")
    elif plan.relative and usable_target_roots:
        # The first configured root is the destination. Deliberately not "the
        # root that already holds it": that would need the very probe the answer
        # decides, and a silent second choice is worse than one documented rule.
        plan.target_root = usable_target_roots[0].rstrip("/")
        plan.target_path = os.path.join(plan.target_root, plan.relative)
        plan.staging_path = staging_for(plan.target_root, plan.relative)

    if plan.size_bytes <= 0:
        plan.refuse(f"{source} reports no size for {model} — a copy without a "
                    f"size cannot be checked against the free space")

    if not ssh_ok:
        plan.refuse(f"{target} is not reachable over SSH"
                    + (f": {ssh_error}" if ssh_error else ""))
    elif free_bytes is None:
        plan.refuse(f"the free space on {target} could not be read")
    elif plan.required_bytes > free_bytes:
        plan.refuse(
            f"not enough space on {target}: {plan.size_bytes / 1e9:.1f} GB × "
            f"{max(1.0, float(factor)):.2f} = {plan.required_bytes / 1e9:.1f} GB "
            f"needed, {free_bytes / 1e9:.1f} GB free")

    plan.already_there = bool(target_has_model)
    if plan.already_there and not overwrite:
        plan.refuse(f"{model} is already on {target} ({plan.target_path}); "
                    f"pass overwrite=1 to replace it")
    elif plan.already_there:
        plan.warn(f"{model} is already on {target} and will be replaced")

    if not ssh_target and not local_is_target:
        plan.refuse(f"no SSH target is configured for {target}")
    return plan


# ---------------------------------------------------------------------------
# The transport: everything that talks to a machine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Endpoint:
    """One machine, as seen from RIPPLE.

    `ssh_target` empty means "this machine" — the source of a copy may well be
    the node RIPPLE runs on, and then no SSH is involved at all.
    """

    name: str
    ssh_target: str = ""
    api_base: str = ""
    api_key: str = ""

    @property
    def local(self) -> bool:
        return not self.ssh_target


@dataclass
class CopyResult:
    """What the rsync run did: exit code, bytes, seconds and why it stopped.

    `progress_flag` is the flag the run that produced this result actually used
    ("" when it ran without one) and `fallback_note` says so in words when a
    flag had to be given up — the job reports both (issue #26).
    """

    ok: bool = False
    bytes_done: int = 0
    seconds: float = 0.0
    cancelled: bool = False
    error: str | None = None
    output: str = ""
    progress_flag: str = ""
    fallback_note: str = ""
    rate: str = ""


class Transport(Protocol):
    """The seam tests replace. Every method is a round trip to a machine."""

    async def probe(self, ep: Endpoint) -> tuple[bool, str | None]: ...
    async def free_bytes(self, ep: Endpoint, path: str) -> int | None: ...
    async def exists(self, ep: Endpoint, path: str) -> bool | None: ...
    async def mkdir(self, ep: Endpoint, path: str) -> tuple[bool, str | None]: ...
    async def move(self, ep: Endpoint, src: str, dst: str) -> tuple[bool, str | None]: ...
    async def copy(self, src: Endpoint, dst: Endpoint, src_path: str, dst_path: str,
                   *, on_progress: Callable[[Progress], None] | None = None,
                   cancel: asyncio.Event | None = None,
                   total_bytes: int = 0) -> CopyResult: ...
    async def models(self, ep: Endpoint) -> list[RemoteModel] | None: ...
    async def reload(self, ep: Endpoint) -> tuple[bool, str]: ...
    async def put_settings(self, ep: Endpoint, model_id: str,
                           payload: dict[str, Any]) -> tuple[int, Any, str | None]: ...


def _ssh_argv(template: str, target: str, command: str) -> list[str]:
    """`ssh_command` is a template with `{target}`; never a hard-coded host."""
    return shlex.split(template.format(target=target)) + [command]


class SshTransport:
    """SSH for the filesystem, HTTP for oMLX's admin API.

    The SSH command is configuration (`replication.ssh_command`), not a constant:
    the operator's key, user and options belong in their own config, and this
    class must not know them. `BatchMode=yes` keeps a missing key from turning
    into an interactive prompt in a background job.
    """

    def __init__(self, *, ssh_command: str = "ssh -o BatchMode=yes -o ConnectTimeout=8",
                 rsync_command: str = "rsync",
                 client: httpx.AsyncClient | None = None,
                 probe_timeout: float = 15.0,
                 copy_timeout: float = 6 * 3600.0,
                 http_timeout: float = 60.0) -> None:
        self.ssh_command = ssh_command
        self.rsync_command = rsync_command
        self.client = client
        self.probe_timeout = probe_timeout
        self.copy_timeout = copy_timeout
        self.http_timeout = http_timeout
        # The answer to "which progress flag does this rsync take", measured
        # once (issue #26). None = not asked yet.
        self._progress_flag: str | None = None

    # -- shell -------------------------------------------------------------
    async def run(self, argv: Sequence[str], *, timeout: float) -> tuple[int, str, str]:
        """Run a command without a shell; returns (rc, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"timeout after {timeout:.0f}s"
        return (proc.returncode or 0,
                out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

    async def shell(self, ep: Endpoint, command: str, *, timeout: float) -> tuple[int, str, str]:
        if ep.local:
            return await self.run(["/bin/sh", "-c", command], timeout=timeout)
        return await self.run(_ssh_argv(self.ssh_command, ep.ssh_target, command),
                              timeout=timeout)

    # -- filesystem --------------------------------------------------------
    async def probe(self, ep: Endpoint) -> tuple[bool, str | None]:
        if ep.local:
            return True, None
        rc, out, err = await self.shell(ep, "echo ripple-ok", timeout=self.probe_timeout)
        if rc == 0 and "ripple-ok" in out:
            return True, None
        detail = (err or out).strip().splitlines()
        return False, (detail[-1] if detail else f"ssh exit {rc}")

    async def free_bytes(self, ep: Endpoint, path: str) -> int | None:
        """Free bytes on the filesystem holding `path` (`df -k`, 1024-blocks)."""
        # An empty path means "wherever the copy would land if no root is
        # configured": the target's home. `$HOME` must stay unquoted to expand.
        quoted = path if str(path).startswith("$") else (shlex.quote(path) if path
                                                         else "$HOME")
        # The directory may not exist yet; df on the nearest existing parent is
        # what the copy will actually write into.
        command = (f"p={quoted}; while [ ! -d \"$p\" ] && [ \"$p\" != / ]; do "
                   f"p=$(dirname \"$p\"); done; df -k \"$p\" | tail -1 | awk '{{print $4}}'")
        rc, out, _ = await self.shell(ep, command, timeout=self.probe_timeout)
        if rc != 0:
            return None
        value = _int(str(out).strip().splitlines()[0]) if str(out).strip() else None
        return value * 1024 if value is not None else None

    async def exists(self, ep: Endpoint, path: str) -> bool | None:
        rc, out, _ = await self.shell(
            ep, f"test -e {shlex.quote(path)} && echo yes || echo no",
            timeout=self.probe_timeout)
        if rc != 0:
            return None
        return str(out).strip() == "yes"

    async def mkdir(self, ep: Endpoint, path: str) -> tuple[bool, str | None]:
        rc, _, err = await self.shell(ep, f"mkdir -p {shlex.quote(path)}",
                                      timeout=self.probe_timeout)
        if rc == 0:
            return True, None
        detail = err.strip().splitlines()
        return False, (detail[-1] if detail else f"mkdir exit {rc}")

    async def move(self, ep: Endpoint, src: str, dst: str) -> tuple[bool, str | None]:
        """`mv`, with the destination's parent created first.

        Same filesystem on both sides (the staging directory lives under the
        model root), so this is a rename: the model appears complete or not at
        all.
        """
        parent = os.path.dirname(dst.rstrip("/")) or "/"
        command = (f"mkdir -p {shlex.quote(parent)} && "
                   f"rm -rf {shlex.quote(dst)} && "
                   f"mv {shlex.quote(src)} {shlex.quote(dst)}")
        rc, _, err = await self.shell(ep, command, timeout=max(self.probe_timeout, 60.0))
        if rc == 0:
            return True, None
        detail = err.strip().splitlines()
        return False, (detail[-1] if detail else f"mv exit {rc}")

    # -- copy --------------------------------------------------------------
    async def rsync_progress_flag(self, *, refresh: bool = False) -> str:
        """The progress flag this rsync accepts: measured once, then cached.

        `rsync --info=progress2 --version` is the probe, and it is a question
        only the running binary can answer: GNU rsync parses the option and
        prints its version (exit 0), openrsync stops at
        "unrecognized option `--info=progress2'" and exits 1 before `--version`
        is looked at (measured on the M5, issue #26). `--help` is the second
        opinion, so a build that fails the first form but documents the flag is
        still believed; a probe that cannot run at all (no rsync, a timeout)
        falls through to `--progress`, which both implementations take.
        """
        if self._progress_flag is not None and not refresh:
            return self._progress_flag
        base = shlex.split(self.rsync_command)
        try:
            rc, out, err = await self.run(base + ["--info=progress2", "--version"],
                                          timeout=self.probe_timeout)
        except OSError as exc:
            log.warning("replication_rsync_probe_failed command=%s error=%s",
                        self.rsync_command, exc)
            self._progress_flag = "--progress"
            return self._progress_flag
        if rc == 0:
            self._progress_flag = "--info=progress2"
        else:
            try:
                hrc, hout, herr = await self.run(base + ["--help"],
                                                 timeout=self.probe_timeout)
            except OSError:
                hrc, hout, herr = 1, "", ""
            # Second opinion, for a wrapper that fails the version form but
            # documents the flag: GNU spells it `--info=FLAGS` in its help,
            # openrsync has no `--info` at all. A wrong yes here costs one
            # rejected attempt, which `copy()` falls back from.
            help_text = hout + herr
            documented = hrc == 0 and ("--info=FLAGS" in help_text
                                       or "info=progress2" in help_text)
            self._progress_flag = "--info=progress2" if documented else "--progress"
        log.info("replication_rsync_progress command=%s flag=%s",
                 self.rsync_command, self._progress_flag)
        return self._progress_flag

    def _rsync_argv(self, src: Endpoint, dst: Endpoint, src_path: str,
                    dst_path: str, *, progress_flag: str = "") -> list[str]:
        """`-a --partial --stats`, plus the progress flag this rsync takes.

        `--stats` is in every attempt: it is what still gives bytes and rate
        when no progress flag survived, and both implementations print it.
        """
        def spec(ep: Endpoint, path: str) -> str:
            path = path.rstrip("/") + "/"
            return f"{ep.ssh_target}:{path}" if not ep.local else path

        argv = shlex.split(self.rsync_command) + ["-a"]
        if progress_flag:
            argv.append(progress_flag)
        return argv + ["--partial", "--stats", spec(src, src_path), spec(dst, dst_path)]

    async def copy(self, src: Endpoint, dst: Endpoint, src_path: str, dst_path: str,
                   *, on_progress: Callable[[Progress], None] | None = None,
                   cancel: asyncio.Event | None = None,
                   total_bytes: int = 0) -> CopyResult:
        """`rsync -a <progress flag> --partial --stats`, cancellable, resumable.

        The flag is the one the probe chose, and a copy does not fail on an
        option again: if the chosen flag is rejected anyway, the next attempt
        drops to `--progress` and then to no progress at all — an option
        rejection costs milliseconds (openrsync prints its usage and exits 1),
        while a job that dies on one costs the whole copy. The result says
        which flag ran, so the report can tell the operator that progress was
        given up rather than silently showing 0%.

        The child is killed on cancellation, not the coroutine: `--partial` keeps
        what is there, so the next run continues instead of starting over.
        """
        flag = await self.rsync_progress_flag()
        attempts: list[str] = [flag] if flag else []
        if flag != "--progress":
            attempts.append("--progress")
        attempts.append("")
        dropped: list[str] = []
        attempt = flag
        result = CopyResult(progress_flag=flag)
        for index, attempt in enumerate(attempts):
            argv = self._rsync_argv(src, dst, src_path, dst_path,
                                    progress_flag=attempt)
            result = await self._run_copy(
                argv, progress_flag=attempt, result=CopyResult(progress_flag=attempt),
                total_bytes=total_bytes, on_progress=on_progress, cancel=cancel)
            if result.ok or result.cancelled or index == len(attempts) - 1:
                break
            if not option_rejected(result.output):
                break
            dropped.append(attempt or "(no progress flag)")
            log.warning("replication_rsync_option_rejected flag=%s dropped=%s",
                        attempt or "-", ",".join(dropped))
        result.progress_flag = attempt
        if dropped:
            used = result.progress_flag or "no progress flag"
            # Only a copy that ended without a flag has no percentage at all;
            # one that fell back to `--progress` still has real numbers.
            extra = ("" if result.progress_flag else
                     " — bytes and rate come from --stats, no percentage is shown")
            result.fallback_note = (f"rsync rejected {' and '.join(dropped)}; "
                                    f"copied with {used}{extra}")
        return result

    async def _run_copy(self, argv: Sequence[str], *, progress_flag: str,
                        result: CopyResult, total_bytes: int,
                        on_progress: Callable[[Progress], None] | None,
                        cancel: asyncio.Event | None) -> CopyResult:
        """One rsync attempt: stream its output through a `ProgressReader`."""
        # The two formats are not distinguishable line by line, so the flag we
        # passed decides how the numbers are read.
        flavour = ("gnu" if progress_flag == "--info=progress2"
                   else "openrsync" if progress_flag else "gnu")
        reader = ProgressReader(flavour=flavour, total_bytes=total_bytes)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        chunks: list[str] = []

        async def pump() -> None:
            assert proc.stdout is not None
            while True:
                block = await proc.stdout.read(4096)
                if not block:
                    return
                text = block.decode("utf-8", "replace")
                chunks.append(text)
                update = reader.feed(text)
                if update is not None:
                    result.bytes_done = update.bytes_done
                    if on_progress is not None:
                        on_progress(update)

        async def watch() -> None:
            """Cancel support: the event ends the child, not the job."""
            while proc.returncode is None:
                if cancel is not None and cancel.is_set():
                    result.cancelled = True
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                    return
                await asyncio.sleep(0.2)

        pump_task = asyncio.create_task(pump())
        watch_task = asyncio.create_task(watch())
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.copy_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            result.error = f"rsync did not finish within {self.copy_timeout:.0f}s"
        finally:
            watch_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
        result.seconds = round(time.monotonic() - started, 2)
        err = b""
        if proc.stderr is not None:
            try:
                err = await proc.stderr.read()
            except (ValueError, OSError):
                err = b""
        result.output = "".join(chunks) + err.decode("utf-8", "replace")
        if result.cancelled:
            result.error = "cancelled"
            return result
        if result.error is None and (proc.returncode or 0) != 0:
            tail = [line for line in result.output.splitlines() if line.strip()]
            result.error = (f"rsync exit {proc.returncode}: "
                            f"{tail[-1][:400] if tail else 'no output'}")
        result.ok = result.error is None
        stats = parse_stats(result.output)
        if stats is not None:
            result.rate = stats.rate
            if result.ok and stats.bytes_total:
                # The model's size, not the protocol traffic: `sent` carries the
                # file list on top of the files. Also the number a copy that ran
                # without a progress flag has no other way of learning.
                result.bytes_done = stats.bytes_total
        return result

    # -- oMLX admin API ----------------------------------------------------
    async def _http(self, ep: Endpoint, method: str, path: str,
                    *, json_body: Any = None, timeout: float | None = None
                    ) -> tuple[int, Any, str | None]:
        if not ep.api_base:
            return 0, None, f"no admin address is known for {ep.name}"
        headers = {"accept": "application/json"}
        if ep.api_key:
            headers["authorization"] = f"Bearer {ep.api_key}"
        client = self.client or httpx.AsyncClient()
        owned = self.client is None
        try:
            resp = await client.request(method, f"{ep.api_base.rstrip('/')}{path}",
                                        headers=headers, json=json_body,
                                        timeout=timeout or self.http_timeout)
        except (httpx.HTTPError, OSError) as exc:
            return 0, None, f"{type(exc).__name__}: {exc}"
        finally:
            if owned:
                await client.aclose()
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body, None

    async def models(self, ep: Endpoint) -> list[RemoteModel] | None:
        status, body, error = await self._http(ep, "GET", "/admin/api/models")
        if error is not None or status >= 400:
            log.warning("replication_models_failed node=%s status=%s error=%s",
                        ep.name, status, error)
            return None
        return models_from_api(body)

    async def reload(self, ep: Endpoint) -> tuple[bool, str]:
        """`POST /admin/api/reload` — unloads everything, re-discovers, preloads pinned."""
        status, body, error = await self._http(ep, "POST", "/admin/api/reload",
                                               json_body=None, timeout=600.0)
        if error is not None:
            return False, error
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("status") or "")
        if status >= 400:
            detail = body.get("detail") if isinstance(body, dict) else None
            return False, f"reload answered {status}: {detail or message or 'no detail'}"
        return True, message or "reload ok"

    async def put_settings(self, ep: Endpoint, model_id: str,
                           payload: dict[str, Any]) -> tuple[int, Any, str | None]:
        path = f"/admin/api/models/{model_id}/settings"
        return await self._http(ep, "PUT", path, json_body=payload, timeout=300.0)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_job(plan: Plan) -> dict[str, Any]:
    """The job state the GET reports and the dashboard polls."""
    return {
        "id": "",
        "state": "running",
        "step": "starting",
        "started_at": _now_iso(),
        "finished_at": None,
        "source": plan.source,
        "target": plan.target,
        "model": plan.model,
        "size_bytes": plan.size_bytes,
        "required_bytes": plan.required_bytes,
        "free_bytes": plan.free_bytes,
        "target_path": plan.target_path,
        "staging_path": plan.staging_path,
        "overwrite": plan.overwrite,
        "bytes_done": 0,
        "percent": 0.0,
        "rate": "",
        "eta": "",
        "reload": None,
        "registered": False,
        "settings": {"sent": 0, "compared": 0, "mismatches": [], "error": None},
        "report": {"bytes": 0, "seconds": 0.0, "mb_per_s": 0.0,
                   "progress": "", "progress_note": "",
                   "not_replicated": []},
        "error": None,
    }


async def run_job(*, job: dict[str, Any], plan: Plan, transport: Transport,
                  source: Endpoint, target: Endpoint, factor: float = 1.05,
                  exclude: Sequence[str] = DEFAULT_SETTINGS_EXCLUDE,
                  verify_settings: bool = True,
                  cancel: asyncio.Event | None = None,
                  open_window: Callable[[], Any] | None = None,
                  close_window: Callable[[], Any] | None = None,
                  on_start: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Copy, register, compare — and always leave the target back in the pool.

    The order is the issue's: target into maintenance, copy into the staging
    directory, move it into place, reload, verify, settings round trip, report.
    A cancellation ends the job after the copy without a reload: nothing is
    registered, and the staging directory stays for the next attempt.

    `open_window`/`close_window` are callbacks instead of a `Ripple` instance so
    this function is testable on its own; `proxy.py` wires them to
    `ripple.maintenance` (`by="replication"`).
    """
    started = time.monotonic()
    not_replicated: list[str] = job["report"]["not_replicated"]
    opened = False
    try:
        if open_window is not None:
            open_window()
            opened = True
        if on_start is not None:
            on_start()
        if plan.required_bytes and plan.free_bytes is not None \
                and plan.required_bytes > plan.free_bytes:
            raise ReplicationError("free space shrank below the plan's requirement")
        # The move step runs `rm -rf` on the destination. A destination that is
        # the root itself (or empty) would delete every model on the machine, so
        # it is refused here as well as in the preflight — belt and braces on the
        # one operation that cannot be undone.
        if not plan.relative or not plan.target_path \
                or os.path.normpath(plan.target_path) == os.path.normpath(
                    plan.target_root or ""):
            raise ReplicationError(
                f"refusing to move into {plan.target_path!r}: the destination must "
                f"be a subpath of the model root, not the root itself")

        # 1. the destination directory of the *staging* copy
        job["step"] = "prepare"
        ok, error = await transport.mkdir(target, plan.staging_path)
        if not ok:
            raise ReplicationError(f"could not create {plan.staging_path}: {error}")

        # 2. copy — resumable, cancellable
        job["step"] = "copy"

        def on_progress(update: Progress) -> None:
            job["bytes_done"] = update.bytes_done
            job["percent"] = update.percent
            job["rate"] = update.rate
            job["eta"] = update.eta

        copied = await transport.copy(source, target, plan.source_path,
                                      plan.staging_path, on_progress=on_progress,
                                      cancel=cancel, total_bytes=plan.size_bytes)
        job["bytes_done"] = copied.bytes_done or job["bytes_done"]
        # Which progress flag the copy actually ran with, and whether one had to
        # be given up. A fallback is not a failure — the copy finished — but the
        # operator must be able to see why the percentage stopped moving.
        job["report"]["progress"] = copied.progress_flag or "none"
        job["report"]["progress_note"] = copied.fallback_note
        if copied.fallback_note:
            log.warning("replication_progress_fallback target=%s model=%s note=%s",
                        plan.target, plan.model, copied.fallback_note)
        if copied.cancelled:
            job["state"] = "cancelled"
            job["step"] = "cancelled"
            job["error"] = ("cancelled during the copy — nothing was registered; "
                            "the staging directory is kept and the next run resumes")
            log.warning("replication_cancelled target=%s model=%s bytes=%d",
                        plan.target, plan.model, job["bytes_done"])
            return job
        if not copied.ok:
            raise ReplicationError(f"copy failed: {copied.error}")
        # The copy is complete, so the model is there in full — whatever the last
        # progress line said. A copy that ran without a progress flag has no
        # percentage at all, and 0 % on a finished job would read as a lie.
        job["percent"] = 100.0
        job["eta"] = "00:00:00"
        if copied.rate and not job["rate"]:
            job["rate"] = copied.rate

        # 3. into place: a rename inside the model root, so oMLX sees a complete
        #    directory or none at all.
        job["step"] = "move"
        ok, error = await transport.move(target, plan.staging_path, plan.target_path)
        if not ok:
            raise ReplicationError(f"could not move the copy into place: {error}")

        # 4. register
        job["step"] = "reload"
        ok, message = await transport.reload(target)
        job["reload"] = {"ok": ok, "message": message}
        if not ok:
            raise ReplicationError(f"reload on {plan.target} failed: {message}")

        job["step"] = "verify"
        models = await transport.models(target)
        if models is None:
            raise ReplicationError(f"{plan.target} did not report its models after "
                                   f"the reload")
        found = next((m for m in models if m.id == plan.model), None)
        job["registered"] = found is not None
        if found is None:
            raise ReplicationError(f"{plan.model} is not listed on {plan.target} "
                                   f"after the reload — the copy is there, the model "
                                   f"is not")
        if plan.size_bytes and found.estimated_size != plan.size_bytes:
            not_replicated.append(
                f"estimated_size differs: source {plan.size_bytes} B, target "
                f"{found.estimated_size} B")

        # 5. the parameters, read fresh from the source: the operator may have
        #    changed them between the plan and now.
        job["step"] = "settings"
        if verify_settings:
            fresh = await transport.models(source)
            origin = next((m for m in (fresh or []) if m.id == plan.model), None)
            payload = settings_payload((origin or RemoteModel(id=plan.model)).settings,
                                       exclude)
            job["settings"]["sent"] = len(payload)
            if payload:
                status, body, error = await transport.put_settings(target, plan.model,
                                                                   payload)
                if error is not None:
                    job["settings"]["error"] = error
                    not_replicated.append(f"settings could not be written: {error}")
                elif status >= 400:
                    detail = ""
                    if isinstance(body, dict):
                        detail = str(body.get("detail") or body.get("error") or "")
                    job["settings"]["error"] = f"PUT answered {status}: {detail}"[:400]
                    not_replicated.append(
                        f"settings rejected by {plan.target}: "
                        f"{job['settings']['error']}")
                else:
                    # 6. read back and compare — a mismatch is reported, never
                    #    smoothed over into a success.
                    after = await transport.models(target)
                    readback = next((m for m in (after or []) if m.id == plan.model),
                                    None)
                    job["settings"]["compared"] = len(payload)
                    mismatches = compare_settings(
                        payload, (readback.settings if readback else {}) or {})
                    job["settings"]["mismatches"] = mismatches
                    if mismatches:
                        keys = ", ".join(m["key"] for m in mismatches[:8])
                        more = "" if len(mismatches) <= 8 else f" (+{len(mismatches) - 8})"
                        not_replicated.append(
                            f"settings differ after the round trip: {keys}{more}")
            else:
                job["settings"]["error"] = None
                log.info("replication_settings_empty target=%s model=%s",
                         plan.target, plan.model)

        # 7. the report
        elapsed = round(time.monotonic() - started, 2)
        moved = job["bytes_done"] or plan.size_bytes
        job["report"].update({
            "bytes": int(moved),
            "seconds": elapsed,
            "mb_per_s": round((moved / 1e6) / elapsed, 1) if elapsed > 0 else 0.0,
            "not_replicated": not_replicated,
        })
        if not_replicated:
            job["state"] = "failed"
            job["step"] = "failed"
            job["error"] = not_replicated[0]
        else:
            job["state"] = "done"
            job["step"] = "done"
        log.info("replication_done source=%s target=%s model=%s bytes=%d seconds=%.1f "
                 "mbps=%.1f state=%s not_replicated=%s",
                 plan.source, plan.target, plan.model, job["report"]["bytes"],
                 elapsed, job["report"]["mb_per_s"], job["state"],
                 ",".join(not_replicated) or "-")
        return job
    except ReplicationError as exc:
        job["state"] = "failed"
        job["step"] = "failed"
        job["error"] = str(exc)
        not_replicated.append(str(exc))
        log.warning("replication_failed target=%s model=%s step=%s error=%s",
                    plan.target, plan.model, job.get("step"), exc)
        return job
    except asyncio.CancelledError:
        job["state"] = "cancelled"
        job["step"] = "cancelled"
        job["error"] = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — a job must never take the loop down
        job["state"] = "failed"
        job["step"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
        not_replicated.append(job["error"])
        log.exception("replication_crashed target=%s model=%s", plan.target, plan.model)
        return job
    finally:
        job["finished_at"] = _now_iso()
        if job["state"] == "running":          # a path that returned early
            job["state"] = "failed"
            job["step"] = "failed"
            job["error"] = job["error"] or "job ended without a verdict"
        # The report is filled here as well, so a cancelled or failed job says
        # how long it ran and how far it got — a bare `state: failed` is not a
        # report.
        elapsed = round(time.monotonic() - started, 2)
        moved = job["bytes_done"] or 0
        job["report"]["seconds"] = elapsed
        job["report"]["bytes"] = int(moved) or job["report"].get("bytes", 0)
        job["report"]["mb_per_s"] = (round((moved / 1e6) / elapsed, 1)
                                     if elapsed > 0 and moved else 0.0)
        # Back into service — unless the operator had already taken the target
        # out for a reason of their own; that window is theirs.
        if opened and close_window is not None:
            try:
                close_window()
            except Exception:  # noqa: BLE001 — cleanup must not mask the reason
                log.warning("replication_window_clear_failed target=%s", plan.target)


# ---------------------------------------------------------------------------
# Endpoints: what a host configuration says about replication
# ---------------------------------------------------------------------------
def ssh_targets_for(host_cfg: Any, cfg: Any) -> list[str]:
    """The ordered SSH destinations for a host: configured, never invented.

    Empty means "this machine". The host whose `hosts[].addresses` holds a
    loopback entry is the one RIPPLE runs on, so it gets no SSH destination at
    all — whatever `replication.address` says (issue #28).
    """
    if host_is_local(host_cfg, cfg):
        return []
    return configured_ssh_targets(host_cfg, cfg)


def ssh_target_for(host_cfg: Any, cfg: Any) -> str:
    """The first SSH destination for a host: configured first, never invented.

    Empty means "this machine". The host whose `hosts[].addresses` holds a
    loopback entry is the one RIPPLE runs on, so it gets no SSH destination at
    all — whatever `replication.address` says (issue #28). The copy on that side
    is a plain path: rsync copies local→remote or remote→local, and with an
    address configured for both nodes the first real copy died on exactly
    "both source and destination cannot be remote files".

    `hosts[].replication.address` is an ordered list (issue #67 point 3): the
    first entry is the intended link (the Thunderbolt bridge), the rest are the
    fallbacks (the LAN). `reachable_endpoint` walks that order and uses the first
    one that answers; this function only names the first. See
    `docs/replication.md` §3 and §3a.
    """
    targets = ssh_targets_for(host_cfg, cfg)
    return targets[0] if targets else ""


def model_roots_for(host_cfg: Any, cfg: Any) -> list[str]:
    rep = getattr(host_cfg, "replication", None)
    roots = list(getattr(rep, "model_roots", None) or [])
    return roots or list(cfg.replication.model_roots or [])


def endpoint_for(host: Any, cfg: Any) -> Endpoint:
    """The SSH side and the HTTP side of one host, in one object."""
    host_cfg = getattr(host, "cfg", host)
    api_base = ""
    resolved = getattr(host, "resolved_address", None)
    if resolved:
        api_base = str(resolved).rstrip("/")
    elif getattr(host_cfg, "addresses", None):
        api_base = str(host_cfg.addresses[0]).rstrip("/")
    # The admin API takes oMLX's own API key, not the client key RIPPLE passes
    # through: `require_admin` accepts the main key as a Bearer token.
    rep = getattr(host_cfg, "replication", None)
    api_key = str(getattr(rep, "api_key", "") or cfg.replication.admin_key or "")
    return Endpoint(name=str(getattr(host_cfg, "name", "")),
                    ssh_target=ssh_target_for(host_cfg, cfg),
                    api_base=api_base, api_key=api_key)


async def reachable_endpoint(transport: Transport, host: Any, cfg: Any
                             ) -> tuple[Endpoint, bool, str | None, list[str]]:
    """The endpoint to use for a host: the first SSH destination that answers.

    `hosts[].replication.address` is ordered (issue #67 point 3) — bridge first,
    LAN as the fallback — and this walks that order. Returns the endpoint, the
    probe verdict, the error and the destinations that were tried and refused, so
    the plan can say which one it fell back to instead of dropping the fact.

    A host with no configured destination (this machine, or none at all) is
    probed once with an empty target: "no SSH involved" is a fact, not a failure.
    """
    base = endpoint_for(host, cfg)
    host_cfg = getattr(host, "cfg", host)
    targets = ssh_targets_for(host_cfg, cfg)
    if not targets:
        ok, error = await transport.probe(base)
        return base, ok, error, []

    tried: list[str] = []
    for target in targets:
        candidate = replace(base, ssh_target=target)
        ok, error = await transport.probe(candidate)
        if ok:
            return candidate, True, None, tried
        tried.append(f"{target} ({error or 'no answer'})")
    return replace(base, ssh_target=targets[0]), False, "; ".join(tried), tried


def build_transport(cfg: Any, *, client: httpx.AsyncClient | None = None) -> SshTransport:
    """The real transport, from `replication:` in the configuration."""
    rep = cfg.replication
    return SshTransport(ssh_command=rep.ssh_command, rsync_command=rep.rsync_command,
                        client=client, probe_timeout=rep.probe_timeout_seconds,
                        copy_timeout=rep.copy_timeout_seconds)
