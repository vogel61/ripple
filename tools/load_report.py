#!/usr/bin/env python3
"""How evenly are the two Macs loaded, and where did the time go?

Read-only. It reads three logs — RIPPLE's own request log and the oMLX
`server.log` on each host (the M3 over SSH) — and answers three questions:

1. **Per host**: how many requests, how many seconds of prefill (TTFT), how
   many seconds of decode, how much of the window the host was busy at all, and
   how much it was idle.
2. **Over the day**: busy seconds per hour and per host, side by side, so
   "evenly loaded" is something one can see instead of infer.
3. **Movements**: rebind events with from/to/gain/cost, per direction, plus the
   promotions from batch to interactive.

The oMLX line it parses (one per completed request, INFO):

    2026-11-14 19:49:17,313 - omlx.server - INFO - [-] - Chat completion:
    model=DeepSeek-V4.1-Flash-1017-oQ4e-mtp, 15722 tokens in 102.81s
    (158.1 tok/s), prompt: 42644, finish_reason=stop, max_tokens=102400,
    request_max_tokens=None, stream_model_ttft=3.35s

Why the numbers are what they are:

* The timestamp is the **end** of the request, and the line carries no request
  id. The interval is therefore `[end - total, end]`, and two requests that
  overlap must be counted once — busy time is the measure of the **union** of
  those intervals, never the sum.
* `stream_model_ttft` is wall time: the prompt is read and the first token
  arrives after it. So prefill = sum of TTFT, decode = sum of
  (total - TTFT). oMLX's own tok/s in the parentheses is computed exactly that
  way (15722 / (102.81 - 3.35) = 158.1).
* The `prompt:` number is **not** a prefill rate. The prefix cache makes it
  meaningless as a denominator: 98.8 M prompt tokens for 797 requests means
  most of them were cache hits. The column is labelled nominal.

Idle is the complement: `window - busy`. Not `window - (PP + TG)`, because
concurrent requests overlap and PP + TG can exceed the window.

Usage:

    tools/load_report.py                          # last 24 h, both hosts
    tools/load_report.py --since 7d --json
    tools/load_report.py --since 2026-11-14T13:00 --hosts M5-alpha
    tools/load_report.py --no-m3                  # offline, local host only

Exit codes: 0 = something was parsed, 3 = nothing could be parsed, 2 = the
configuration could not be read. Nothing is written anywhere but stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ripple.config import ConfigError, load_config  # noqa: E402

OMLX_LOG_DIR = "~/.omlx/logs"
OMLX_LOG_NAME = "server.log"
RIPPLE_LOG_DEFAULT = "~/Library/Logs/ripple.log"
SSH_TIMEOUT_SECONDS = 120


# --------------------------------------------------------------------------- #
# oMLX server log
# --------------------------------------------------------------------------- #

# Only the INFO completion line — the DEBUG "Chat completion request received:"
# line sits next to it and must not match. The diffusion models put extra
# numbers inside the tok/s parentheses, so that part is skipped, not parsed.
# `stream_model_ttft` is missing for a non-streamed request; that variant is
# matched by a second pattern and counted with TTFT 0 (there is no prefill
# measurement then, and the line says so by omitting it).
_OMLX_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - omlx\.server - INFO - "
    r"\[-\] - Chat completion: model=(?P<model>[^,]+), "
    r"(?P<tokens>\d+) tokens in (?P<total>\d+(?:\.\d+)?)s \([^)]*\), "
    r"prompt: (?P<prompt>\d+),.*?stream_model_ttft=(?P<ttft>\d+(?:\.\d+)?)s"
)
_OMLX_NO_TTFT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - omlx\.server - INFO - "
    r"\[-\] - Chat completion: model=(?P<model>[^,]+), "
    r"(?P<tokens>\d+) tokens in (?P<total>\d+(?:\.\d+)?)s \([^)]*\), "
    r"prompt: (?P<prompt>\d+),"
)

# What the remote grep looks for. POSIX ERE, not Python: named groups are not
# available there, and the Python pattern above is only used locally.
OMLX_GREP = "Chat completion: model="

_TS_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class OmlxRecord:
    """One completed request as oMLX reported it. `end` is the log timestamp."""

    end: datetime
    model: str
    completion_tokens: int
    prompt_tokens: int
    total_s: float
    ttft_s: float

    @property
    def start(self) -> datetime:
        return self.end - timedelta(seconds=self.total_s)

    @property
    def decode_s(self) -> float:
        return max(0.0, self.total_s - self.ttft_s)


def parse_omlx(text: str, since: datetime | None = None,
               until: datetime | None = None) -> list[OmlxRecord]:
    """Parse oMLX `Chat completion:` lines, filtered to [since, until]."""
    out: list[OmlxRecord] = []
    for line in text.splitlines():
        m = _OMLX_RE.match(line) or _OMLX_NO_TTFT_RE.match(line)
        if not m:
            continue
        try:
            end = datetime.strptime(m.group("ts"), _TS_FMT)
        except ValueError:
            continue
        if since is not None and end < since:
            continue
        if until is not None and end > until:
            continue
        out.append(OmlxRecord(
            end=end,
            model=m.group("model").strip(),
            completion_tokens=int(m.group("tokens")),
            prompt_tokens=int(m.group("prompt")),
            total_s=float(m.group("total")),
            ttft_s=float(m.groupdict().get("ttft") or 0.0),
        ))
    return out


# --------------------------------------------------------------------------- #
# RIPPLE log
# --------------------------------------------------------------------------- #

_TS = r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
_REQ_RE = re.compile(_TS + r"\s+\w+\s+ripple\.request:\s+(?P<rest>.*)$")
_REBIND_RE = re.compile(
    _TS + r"\s+\w+\s+ripple: rebind thread=(?P<thread>\S+) from=(?P<src>\S+) "
    r"to=(?P<dst>\S+) gain=(?P<gain>[\d.]+)s cost=(?P<cost>[\d.]+)s")
_PROMOTE_RE = re.compile(
    _TS + r"\s+\w+\s+ripple: promote thread=(?P<thread>\S+) batch->interactive")
_KV_RE = re.compile(r"(\w+)=(\S+)")


@dataclass(frozen=True)
class RippleRequest:
    ts: datetime
    host: str | None
    reason: str
    thread: str | None
    duration_ms: float
    status: int


@dataclass(frozen=True)
class Movement:
    ts: datetime
    kind: str                 # "rebind" | "promote"
    thread: str
    src: str | None = None
    dst: str | None = None
    gain_s: float = 0.0
    cost_s: float = 0.0


@dataclass
class RippleLog:
    requests: list[RippleRequest] = field(default_factory=list)
    movements: list[Movement] = field(default_factory=list)
    lines: int = 0
    duplicates: int = 0


def dedupe_lines(text: str) -> tuple[list[str], int]:
    """Drop byte-identical lines, keeping the first — and count them.

    Until the logging fix, two handlers wrote every record into the same file,
    so an old or rotated log holds each line twice. Full-line comparison is safe
    because the line carries a second-resolution timestamp: two identical lines
    are one record seen twice, not two requests.
    """
    seen: set[str] = set()
    kept: list[str] = []
    dropped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if line in seen:
            dropped += 1
            continue
        seen.add(line)
        kept.append(line)
    return kept, dropped


def parse_ripple(text: str, since: datetime | None = None,
                 until: datetime | None = None) -> RippleLog:
    """Parse RIPPLE's request, rebind and promote lines (deduplicated)."""
    kept, dropped = dedupe_lines(text)
    log = RippleLog(lines=len(kept), duplicates=dropped)
    for line in kept:
        m = _REBIND_RE.match(line)
        if m:
            ts = datetime.strptime(m.group("ts"), _TS_FMT)
            if _in_window(ts, since, until):
                log.movements.append(Movement(
                    ts=ts, kind="rebind", thread=m.group("thread"),
                    src=m.group("src"), dst=m.group("dst"),
                    gain_s=float(m.group("gain")), cost_s=float(m.group("cost"))))
            continue
        m = _PROMOTE_RE.match(line)
        if m:
            ts = datetime.strptime(m.group("ts"), _TS_FMT)
            if _in_window(ts, since, until):
                log.movements.append(Movement(ts=ts, kind="promote",
                                              thread=m.group("thread")))
            continue
        m = _REQ_RE.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group("ts"), _TS_FMT)
        if not _in_window(ts, since, until):
            continue
        kv = dict(_KV_RE.findall(m.group("rest")))
        host = kv.get("host")
        if host in (None, "-"):
            host = None
        try:
            duration = float(kv.get("duration_ms", "0"))
            status = int(kv.get("status", "0"))
        except ValueError:
            continue
        log.requests.append(RippleRequest(
            ts=ts, host=host, reason=kv.get("reason", "-"),
            thread=None if kv.get("thread") in (None, "-") else kv.get("thread"),
            duration_ms=duration, status=status))
    return log


def _in_window(ts: datetime, since: datetime | None, until: datetime | None) -> bool:
    return (since is None or ts >= since) and (until is None or ts <= until)


# --------------------------------------------------------------------------- #
# Interval arithmetic
# --------------------------------------------------------------------------- #

Interval = tuple[datetime, datetime]


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Union of intervals as a sorted, disjoint list. Touching intervals merge."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def union_seconds(intervals: list[Interval]) -> float:
    return sum((end - start).total_seconds() for start, end in merge_intervals(intervals))


def overlap_seconds(intervals: list[Interval], start: datetime, end: datetime) -> float:
    """Union length of `intervals` clipped to [start, end]."""
    total = 0.0
    for a, b in intervals:
        lo = max(a, start)
        hi = min(b, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


# --------------------------------------------------------------------------- #
# Per-host aggregation
# --------------------------------------------------------------------------- #

@dataclass
class HostLoad:
    name: str
    records: list[OmlxRecord] = field(default_factory=list)
    source: str = ""
    error: str | None = None       # set when the host could not be read
    skipped: str | None = None     # set when the host was left out on purpose


def summarise(records: list[OmlxRecord], window_start: datetime,
              window_end: datetime) -> dict:
    """The per-host numbers.

    PP and TG are sums over the requests that *ended* inside the window; busy is
    the union of their intervals clipped to the window, so a request that began
    before the window still counts its whole prefill/decode but only its share
    of busy time. Idle is the complement of busy.
    """
    intervals = merge_intervals([(r.start, r.end) for r in records
                                 if r.end > window_start and r.start < window_end])
    span = (window_end - window_start).total_seconds()
    busy = overlap_seconds(intervals, window_start, window_end)
    pp = sum(r.ttft_s for r in records)
    tg = sum(r.decode_s for r in records)
    completion = sum(r.completion_tokens for r in records)
    prompt = sum(r.prompt_tokens for r in records)
    return {
        "requests": len(records),
        "pp_seconds": round(pp, 1),
        "tg_seconds": round(tg, 1),
        "busy_seconds": round(busy, 1),
        "busy_pct": round(100.0 * busy / span, 1) if span > 0 else 0.0,
        "idle_pct": round(100.0 * (1 - busy / span), 1) if span > 0 else 0.0,
        "mean_ttft_s": round(pp / len(records), 2) if records else 0.0,
        "tg_tps": round(completion / tg, 1) if tg > 0 else 0.0,
        "nominal_prompt_tokens": prompt,
        "completion_tokens": completion,
    }


def hourly_profile(records: list[OmlxRecord], window_start: datetime,
                   window_end: datetime) -> list[tuple[datetime, float]]:
    """Busy seconds per clock hour, clipped to the window (union, not sum)."""
    intervals = merge_intervals([(r.start, r.end) for r in records
                                 if r.end > window_start and r.start < window_end])
    buckets: list[tuple[datetime, float]] = []
    hour = window_start.replace(minute=0, second=0, microsecond=0)
    if hour < window_start:
        hour += timedelta(hours=1)
    hour -= timedelta(hours=1)          # first partial hour
    while hour < window_end:
        lo = max(hour, window_start)
        hi = min(hour + timedelta(hours=1), window_end)
        buckets.append((hour, round(overlap_seconds(intervals, lo, hi), 1)))
        hour += timedelta(hours=1)
    return buckets


def movements_summary(movements: list[Movement]) -> dict:
    rebinds = [m for m in movements if m.kind == "rebind"]
    directions: dict[str, int] = {}
    for m in rebinds:
        key = f"{m.src} -> {m.dst}"
        directions[key] = directions.get(key, 0) + 1
    return {
        "rebinds": [
            {"ts": m.ts.strftime(_TS_FMT), "thread": m.thread, "from": m.src,
             "to": m.dst, "gain_s": m.gain_s, "cost_s": m.cost_s}
            for m in rebinds
        ],
        "directions": dict(sorted(directions.items())),
        "rebind_count": len(rebinds),
        "promotions": sum(1 for m in movements if m.kind == "promote"),
        "promotion_threads": [m.thread for m in movements if m.kind == "promote"],
    }


def ripple_by_host(requests: list[RippleRequest]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in requests:
        name = r.host or "unrouted"
        h = out.setdefault(name, {"requests": 0, "duration_ms": 0.0})
        h["requests"] += 1
        h["duration_ms"] += r.duration_ms
    for h in out.values():
        h["mean_duration_ms"] = round(h["duration_ms"] / h["requests"], 1)
    return out


# --------------------------------------------------------------------------- #
# Sources: local file or the other Mac over SSH
# --------------------------------------------------------------------------- #

@dataclass
class Source:
    host: str
    kind: str            # "local" | "ssh" | "skip"
    target: str          # ssh destination, or "-" for local
    paths: list[str]     # local absolute paths, or $HOME-based remote paths
    display: str


@dataclass
class Fetch:
    text: str = ""
    files: list[str] = field(default_factory=list)
    error: str | None = None


def local_addresses() -> set[str]:
    """Addresses that mean 'this machine'. Loopback plus every interface IP."""
    out = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            out.add(str(info[4][0]))
    except OSError:
        pass
    try:
        out.add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    return out


def _address_host(address: str) -> str:
    return urlsplit(address).hostname or address


def log_paths(directory: str, since: datetime, until: datetime,
              *, remote: bool) -> list[str]:
    """`server.log` plus the daily rotations that can hold [since, until]."""
    base = directory.rstrip("/")
    prefix = base.replace("~", "$HOME", 1) if remote else str(Path(base).expanduser())
    paths = [f"{prefix}/{OMLX_LOG_NAME}"]
    day = since.date()
    while day <= until.date():
        if day != until.date():
            paths.append(f"{prefix}/{OMLX_LOG_NAME}.{day.isoformat()}")
        day += timedelta(days=1)
    return paths


def build_source(host_cfg, since: datetime, until: datetime,
                 *, no_remote: bool) -> Source:
    """Decide local vs SSH from the host's own addresses (loopback = this Mac)."""
    local = local_addresses()
    addresses = list(getattr(host_cfg, "addresses", []) or [])
    local_addr = next((a for a in addresses if _address_host(a) in local), None)
    if local_addr is not None:
        paths = log_paths(OMLX_LOG_DIR, since, until, remote=False)
        return Source(host=host_cfg.name, kind="local", target="-", paths=paths,
                      display=f"local {paths[0]}")
    remote_addr = _address_host(addresses[0]) if addresses else "-"
    paths = log_paths(OMLX_LOG_DIR, since, until, remote=True)
    if no_remote:
        return Source(host=host_cfg.name, kind="skip", target=remote_addr,
                      paths=paths,
                      display=f"{remote_addr}:{OMLX_LOG_DIR}/{OMLX_LOG_NAME} (--no-m3)")
    return Source(host=host_cfg.name, kind="ssh", target=remote_addr, paths=paths,
                  display=f"ssh {remote_addr}:{OMLX_LOG_DIR}/{OMLX_LOG_NAME}")


def _remote_command(paths: list[str]) -> str:
    quoted = " ".join(f'"{p}"' for p in paths)          # paths are built here, not user input
    pattern = shlex.quote(OMLX_GREP)
    # A remote grep, not a copy of the file. The READ lines on stderr make the
    # provenance checkable — the report prints exactly which files it saw. The
    # trailing `exit 0` keeps a missing rotation from looking like a broken host.
    return (f'for f in {quoted}; do if [ -f "$f" ]; then echo "READ $f" >&2; '
            f'grep -hF {pattern} "$f"; fi; done; exit 0')


def run_ssh(target: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, command],
        capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS)


def fetch_source(source: Source, since: datetime) -> Fetch:
    """Read one host's oMLX log — a remote grep, never a copy of the file."""
    if source.kind == "skip":
        return Fetch(error="skipped")
    if source.kind == "local":
        chunks: list[str] = []
        used: list[str] = []
        for path in source.paths:
            p = Path(path)
            if not p.is_file():
                continue
            try:
                chunks.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                return Fetch(files=used, error=f"read failed: {exc}")
            used.append(str(p))
        if not used:
            return Fetch(error=f"no log file under {Path(source.paths[0]).parent}")
        return Fetch(text="\n".join(chunks), files=used)
    try:
        proc = run_ssh(source.target, _remote_command(source.paths))
    except subprocess.TimeoutExpired:
        return Fetch(error=f"ssh timeout after {SSH_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return Fetch(error=f"ssh failed: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return Fetch(error=f"ssh exit {proc.returncode}: {detail[-1] if detail else 'no output'}")
    files = [line[5:].strip() for line in (proc.stderr or "").splitlines()
             if line.startswith("READ ")]
    if not files:
        return Fetch(error=f"no log file on {source.target} under {OMLX_LOG_DIR}")
    return Fetch(text=proc.stdout, files=[f"{source.target}:{f}" for f in files])


def load_hosts(sources: list[Source], since: datetime, until: datetime,
               *, fetcher=fetch_source) -> list[HostLoad]:
    """One HostLoad per source; a failing host is marked, never fatal."""
    out: list[HostLoad] = []
    for src in sources:
        if src.kind == "skip":
            out.append(HostLoad(name=src.host, source=src.display, skipped="--no-m3"))
            continue
        fetched = fetcher(src, since)
        if fetched.error:
            out.append(HostLoad(name=src.host, source=src.display, error=fetched.error))
            continue
        records = parse_omlx(fetched.text, since, until)
        files = ", ".join(fetched.files) if fetched.files else src.display
        out.append(HostLoad(name=src.host, records=records, source=files))
    return out


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #

_REL_RE = re.compile(r"^(\d+)\s*([hdwm])$")
_UNIT_HOURS = {"h": 1, "d": 24, "w": 24 * 7, "m": 24 * 30}


def parse_since(spec: str, now: datetime) -> datetime:
    """`24h`, `7d`, `48h`, `2w` or an ISO timestamp."""
    m = _REL_RE.match(spec.strip())
    if m:
        return now - timedelta(hours=int(m.group(1)) * _UNIT_HOURS[m.group(2)])
    try:
        ts = datetime.fromisoformat(spec.strip())
    except ValueError as exc:
        raise ValueError(f"--since: expected 24h, 7d, 48h or an ISO timestamp, "
                         f"got {spec!r}") from exc
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    return ts


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

BAR_WIDTH = 30


def render(window_start: datetime, window_end: datetime, loads: list[HostLoad],
           ripple: RippleLog, ripple_files: list[str], m3_note: str | None = None) -> str:
    span = (window_end - window_start).total_seconds()
    lines: list[str] = []
    add = lines.append

    add("RIPPLE load report")
    add(f"window   {window_start:%Y-%m-%d %H:%M} .. {window_end:%Y-%m-%d %H:%M}"
        f"  ({span / 3600:.1f} h, local time)")
    add("sources")
    for load in loads:
        state = ""
        if load.error:
            state = f"  [unreachable: {load.error}]"
        elif load.skipped:
            state = f"  [skipped: {load.skipped}]"
        add(f"  {load.name:<12} {load.source}{state}")
    add(f"  {'RIPPLE log':<12} {', '.join(ripple_files) or '-'}"
        f"  ({ripple.lines} lines, {ripple.duplicates} duplicates dropped)")
    if m3_note is None:
        missing = [load.name for load in loads if load.error]
        if missing:
            m3_note = (f"{', '.join(missing)} unreachable — only the hosts read "
                       f"above are in the numbers; the report is not a fleet total")
    if m3_note:
        add(f"  note         {m3_note}")

    # --- per host ---------------------------------------------------------
    add("")
    add("per host (oMLX log)")
    header = (f"  {'host':<12}{'req':>6}{'PP s':>9}{'TG s':>9}{'busy s':>9}"
              f"{'busy%':>7}{'idle%':>7}{'mean TTFT':>11}{'TG tok/s':>10}"
              f"{'prompt tok':>14}{'compl tok':>11}")
    add(header)
    add("  " + "-" * (len(header) - 2))
    measured: list[tuple[HostLoad, dict]] = []
    for load in loads:
        if load.error or load.skipped:
            continue
        s = summarise(load.records, window_start, window_end)
        measured.append((load, s))
        add(f"  {load.name:<12}{s['requests']:>6}{s['pp_seconds']:>9,.0f}"
            f"{s['tg_seconds']:>9,.0f}{s['busy_seconds']:>9,.0f}"
            f"{s['busy_pct']:>6.1f}%{s['idle_pct']:>6.1f}%"
            f"{s['mean_ttft_s']:>10.2f}s{s['tg_tps']:>10.1f}"
            f"{s['nominal_prompt_tokens']:>14,}{s['completion_tokens']:>11,}")
    if not measured:
        add("  (no host could be read)")
    add("  prompt tok is nominal (prefix-cache inflated) — not a prefill rate.")
    add("  PP and TG are per-request sums; concurrent requests overlap, so they")
    add("  can add up to more than busy. busy is the union of [end-total, end].")
    for load, s in measured:
        pp = 100.0 * s["pp_seconds"] / span if span else 0
        tg = 100.0 * s["tg_seconds"] / span if span else 0
        add(f"  {load.name}: prefill {s['pp_seconds']:,.0f}s ({pp:.1f}% of window), "
            f"decode {s['tg_seconds']:,.0f}s ({tg:.1f}%), idle {s['idle_pct']:.1f}%")

    # --- hourly profile ---------------------------------------------------
    add("")
    add("hourly busy seconds (union per host, one row per clock hour)")
    profiles = {load.name: hourly_profile(load.records, window_start, window_end)
                for load, _ in measured}
    if profiles:
        top = max((v for prof in profiles.values() for _, v in prof), default=0.0)
        scale = max(top, 60.0)
        names = list(profiles)
        add("  " + f"{'hour':<6}" + "".join(f"{n:<{BAR_WIDTH + 9}}" for n in names))
        hours = sorted({h for prof in profiles.values() for h, _ in prof})
        per_host = {n: dict(p) for n, p in profiles.items()}
        day = None
        day_totals: dict[str, float] = {n: 0.0 for n in names}
        for hour in hours:
            if day is not None and hour.date() != day:
                add(f"  {day.isoformat()} total  " + "".join(
                    f"{day_totals[n]:>{BAR_WIDTH + 8},.0f}s " for n in names))
                day_totals = {n: 0.0 for n in names}
            day = hour.date()
            row = f"  {hour:%H:%M}  "
            for n in names:
                v = per_host[n].get(hour, 0.0)
                day_totals[n] += v
                fill = int(round(BAR_WIDTH * v / scale))
                row += "#" * fill + " " * (BAR_WIDTH - fill) + f"{v:>7,.0f}s "
            add(row.rstrip())
        if day is not None:
            add(f"  {day.isoformat()} total  " + "".join(
                f"{day_totals[n]:>{BAR_WIDTH + 8},.0f}s " for n in names))
        add(f"  bar: {BAR_WIDTH} chars = {scale:,.0f}s")

    # --- movements --------------------------------------------------------
    add("")
    add("thread movements (RIPPLE log)")
    mv = movements_summary(ripple.movements)
    if mv["rebinds"]:
        for r in mv["rebinds"]:
            add(f"  {r['ts'][11:]}  {r['thread']:<18} {r['from']} -> {r['to']}"
                f"  gain {r['gain_s']:.1f}s  cost {r['cost_s']:.1f}s")
    else:
        add("  no rebind events")
    for direction, count in mv["directions"].items():
        add(f"  direction {direction}: {count}")
    add(f"  rebinds {mv['rebind_count']}, promotions (batch->interactive) "
        f"{mv['promotions']}")
    if mv["promotion_threads"]:
        add("  promoted: " + ", ".join(mv["promotion_threads"]))
    by_host = ripple_by_host(ripple.requests)
    if by_host:
        add("  RIPPLE requests per host (deduplicated):")
        for name, h in sorted(by_host.items()):
            add(f"    {name:<12}{h['requests']:>6} requests"
                f"{h['mean_duration_ms']:>10,.1f} ms mean")
    else:
        add("  no ripple.request: lines in the window")

    add("")
    add("idle = window - busy. TTFT is wall time (prefill); decode = total - TTFT.")
    return "\n".join(lines)


def as_json(window_start: datetime, window_end: datetime, loads: list[HostLoad],
            ripple: RippleLog, ripple_files: list[str]) -> dict:
    return {
        "window": {"start": window_start.strftime(_TS_FMT),
                   "end": window_end.strftime(_TS_FMT),
                   "seconds": round((window_end - window_start).total_seconds(), 1)},
        "sources": [
            {"host": load.name, "source": load.source,
             "error": load.error, "skipped": load.skipped}
            for load in loads
        ] + [{"host": "RIPPLE", "source": ", ".join(ripple_files),
              "error": None, "skipped": None,
              "lines": ripple.lines, "duplicates": ripple.duplicates}],
        "hosts": {
            load.name: {
                **summarise(load.records, window_start, window_end),
                "hourly": [{"hour": h.strftime(_TS_FMT), "busy_seconds": v}
                           for h, v in hourly_profile(load.records, window_start, window_end)],
                "error": load.error,
                "skipped": load.skipped,
            }
            for load in loads
        },
        "movements": movements_summary(ripple.movements),
        "ripple_requests": ripple_by_host(ripple.requests),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def read_ripple_log(path: str) -> tuple[str, list[str]]:
    """The live log plus its rotations; the window filter happens in parsing."""
    base = Path(path).expanduser()
    candidates = [base] + [base.with_name(f"{base.name}.{i}") for i in range(1, 6)]
    chunks: list[str] = []
    used: list[str] = []
    for p in candidates:
        if not p.is_file():
            continue
        try:
            chunks.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        used.append(str(p))
    return "\n".join(chunks), used


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="load_report.py",
        description="Read-only: how evenly are the hosts loaded, and where did "
                    "the time go (prefill / decode / idle)?")
    p.add_argument("--since", default="24h",
                   help="24h, 7d, 48h or an ISO timestamp (default: 24h)")
    p.add_argument("--hosts", action="append", default=None,
                   help="subset of host names, comma separated (default: all)")
    p.add_argument("--config", default="config.yaml",
                   help="configuration for host names, addresses and log paths")
    p.add_argument("--ripple-log", default=None,
                   help=f"RIPPLE log (default: log_file from the config, else "
                        f"{RIPPLE_LOG_DEFAULT})")
    p.add_argument("--no-m3", action="store_true",
                   help="do not touch remote hosts (offline)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    now = datetime.now().replace(microsecond=0)
    try:
        since = parse_since(args.since, now)
    except ValueError as exc:
        print(f"load_report: {exc}", file=sys.stderr)
        return 2
    if since >= now:
        print("load_report: --since lies in the future", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"load_report: configuration not usable ({exc})", file=sys.stderr)
        return 2

    hosts = [h for h in cfg.hosts if h.enabled]
    wanted = None
    if args.hosts:
        wanted = {n.strip() for item in args.hosts for n in item.split(",") if n.strip()}
        unknown = wanted - {h.name for h in hosts}
        if unknown:
            print(f"load_report: unknown host(s): {', '.join(sorted(unknown))}",
                  file=sys.stderr)
            return 2
        hosts = [h for h in hosts if h.name in wanted]
    if not hosts:
        print("load_report: no hosts selected", file=sys.stderr)
        return 2

    sources = [build_source(h, since, now, no_remote=args.no_m3) for h in hosts]
    loads = load_hosts(sources, since, now)

    ripple_path = args.ripple_log or cfg.log_file or RIPPLE_LOG_DEFAULT
    ripple_text, ripple_files = read_ripple_log(ripple_path)
    ripple = parse_ripple(ripple_text, since, now)

    if args.json:
        print(json.dumps(as_json(since, now, loads, ripple, ripple_files), indent=2))
    else:
        print(render(since, now, loads, ripple, ripple_files))

    parsed = any(load.records for load in loads) or bool(ripple.requests or ripple.movements)
    return 0 if parsed else 3


if __name__ == "__main__":
    sys.exit(main())
