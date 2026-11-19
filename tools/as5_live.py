#!/usr/bin/env python3
"""AS-5b and AS-5c against the live fleet — unattended, verified, self-healing.

The two scenarios prove the eviction guard on real 453 GB models:

    AS-5b   DeepSeek on the M5, the M3 free of it. A request for Qwen3.8 must go
            10/10 to the M3 (first reason `cold_load`) and must not touch the
            M5 — the M5 is the entry node and runs the operator's own session.

    AS-5c   Both nodes hold DeepSeek and both have a live thread on it. A request
            for Qwen3.8 must be refused with 503 `conflict`, with no upstream
            call at all: nothing loaded, nothing evicted.

It runs at 01:00 while nobody is watching, so every rule here is about being
diagnosable and harmless in the morning:

  * `--check` and `--dry-run` are strictly read-only (no load, no unload, no
    chat request). Only `--run` changes anything.
  * Every state change is verified by polling the host, never assumed.
  * A `finally` block always puts the fleet back: Qwen3.8 unloaded everywhere,
    DeepSeek loaded on both nodes. Three attempts, growing waits; if that fails
    the run exits 2 and prints the exact commands a human would type.
  * A global deadline (default 45 min) stops new phases and jumps to the restore.
  * A transient 503 `no_healthy_host` / `no_candidate` — RIPPLE's pool dropping
    the host because one health probe timed out, which happens in the seconds
    after a 111 GB cold load — is waited out (`/healthz` polled every 2 s, up to
    `--health-wait-seconds`, default 120 s) and retried at most three times. The
    deliberate 503 `conflict` of AS-5c is never retried: it is the measurement.
  * `--verify-only` is the watchdog: check the final state, repair it, one line
    of output. Safe at any time, no scenario.

Usage:
    ./.venv/bin/python tools/as5_live.py --check
    ./.venv/bin/python tools/as5_live.py --dry-run
    ./.venv/bin/python tools/as5_live.py --run
    ./.venv/bin/python tools/as5_live.py --run --health-wait-seconds 180
    ./.venv/bin/python tools/as5_live.py --verify-only

Exit codes: 0 all good · 2 restore failed · 3 preflight refused · 4 the M5 was
touched · 5 AS-5c served instead of refused · 1 any other scenario failure.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ripple.config import Config, ConfigError, HostConfig, load_config  # noqa: E402

HEAVY_DEFAULT = "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"
PROBE_DEFAULT = "Qwen3.8-Flash-Next-oQ4e-mtp"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_RESTORE_FAILED = 2
EXIT_PREFLIGHT = 3
EXIT_M5_TOUCHED = 4
EXIT_SERVED = 5

# A 503 carrying one of these reasons is not an answer about the model: it means
# RIPPLE's candidate pool no longer holds a host for it. `ripple/hosts.py` sets
# `healthy` from *every* probe result, so one health probe timing out during a
# 111 GB cold load removes the host — and the entry host is not on the probe's
# allow-list, so placement has nowhere else to go and answers 503. It is a
# transient condition, seconds long, and worth waiting out.
# A 503 `conflict` is the opposite: it is AS-5c's measurement and must never be
# retried. Status code alone cannot tell them apart, the reason field can.
TRANSIENT_503_REASONS = frozenset({"no_healthy_host", "no_candidate"})
MAX_HEALTH_RETRIES = 3      # retries after the first attempt, so 4 sends at most
HEALTH_POLL_S = 2.0         # /healthz poll interval while waiting for the host

# The filler for the long prompts of the AS-5c setup. A thread only takes a slot
# above `affinity.min_prompt_tokens` (FA-7), and only a slot-holding thread makes
# the eviction forecast report CONFLICT — that is the whole mechanism AS-5c
# exercises. Deliberately bland, so the model answers it and the prefix is cheap.
FILLER = (
    "Ripple routes a conversation to one host and keeps it there; a conversation "
    "that moves pays its whole prefill again. "
)


# ---------------------------------------------------------------------------
# HTTP with honest retries
# ---------------------------------------------------------------------------
@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    text: str
    latency_s: float

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


class TransportError(RuntimeError):
    """No HTTP answer at all (connection refused, reset, protocol error)."""


class Http:
    """A thin httpx wrapper that retries what is safe to retry.

    Connection errors and 5xx are retried three times with backoff; 4xx never —
    a 404 or a 401 is an answer, not a hiccup. A *read* timeout is not retried:
    the first AS-5b request waits for a 111 GB cold load, and repeating that on
    a timeout would double the wait instead of diagnosing it.

    Chat requests pass `retry_5xx=False`: a 503 from RIPPLE is the measurement
    (AS-5c expects exactly that), not a hiccup, and retrying it would report
    three refusals as one. The one 503 that *is* transient — `no_healthy_host`
    after a cold load — is handled one level up in `Runner.chat`, which waits
    for the host to be healthy again before retrying: a fixed backoff here would
    either be too short or blind.
    """

    RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                 httpx.WriteError, httpx.ReadError, httpx.RemoteProtocolError,
                 httpx.NetworkError, OSError)

    def __init__(self, *, retries: int = 3, backoff: float = 1.5,
                 timeout: float = 30.0, log: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.log = log or (lambda _msg: None)
        self.sleep = sleep
        self._client = httpx.Client(follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, url: str, *, json_body: Any = None,
                headers: dict[str, str] | None = None,
                timeout: float | None = None, retry_5xx: bool = True) -> Resp:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            t0 = time.perf_counter()
            try:
                r = self._client.request(method, url, json=json_body,
                                         headers=headers,
                                         timeout=timeout or self.timeout)
            except self.RETRYABLE as exc:            # connection-level only
                last = TransportError(f"{type(exc).__name__}: {exc}")
                self.log(f"  ! {method} {url} attempt {attempt}/{self.retries} "
                         f"failed: {last}")
                if attempt < self.retries:
                    self.sleep(self.backoff ** attempt)
                    continue
                raise last from exc
            resp = Resp(status=r.status_code,
                        headers={k.lower(): v for k, v in r.headers.items()},
                        text=r.text, latency_s=round(time.perf_counter() - t0, 3))
            self.log(f"  http {method} {url} -> {resp.status} "
                     f"{resp.latency_s:.3f}s")
            if retry_5xx and resp.status >= 500 and attempt < self.retries:
                self.log(f"  ! {resp.status} on attempt {attempt}/{self.retries}, "
                         f"retrying in {self.backoff ** attempt:.1f}s")
                self.sleep(self.backoff ** attempt)
                continue
            return resp
        raise last or TransportError(f"{method} {url} failed")

# ---------------------------------------------------------------------------
# Run options and small carriers
# ---------------------------------------------------------------------------
@dataclass
class Options:
    ripple: str = "http://127.0.0.1:8010"
    heavy: str = HEAVY_DEFAULT
    probe: str = PROBE_DEFAULT
    log_dir: Path = field(default_factory=lambda: REPO / "tools" / "as5")
    report_dir: Path = field(default_factory=lambda: REPO / "docs")
    deadline_s: float = 45 * 60
    health_wait_s: float = 120.0
    mail: bool = True
    requests: int = 10
    verbose: bool = False
    api_key: str = ""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True      # an optional check is reported, never fatal


class ScenarioAbort(RuntimeError):
    """A phase refused to continue. `code` is the exit code it wants."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeadlineReached(ScenarioAbort):
    def __init__(self, message: str = "global deadline reached") -> None:
        super().__init__(EXIT_FAILED, message)


class M5Touched(ScenarioAbort):
    def __init__(self, message: str) -> None:
        super().__init__(EXIT_M5_TOUCHED, message)


class Logger:
    """Plain-text log, one line per event; the morning's only witness."""

    def __init__(self, path: Path | None, echo: bool = False) -> None:
        self.path = path
        self.echo = echo
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def __call__(self, message: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {message}"
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.echo:
            print(line, flush=True)


def gb(value: float | int | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1e9:.1f} GB"


def minutes(seconds: float) -> str:
    """Deadlines read badly as '0 min' when they are 15 s."""
    if seconds >= 120:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, cfg: Config, opts: Options, *, http: Any | None = None,
                 log: Logger | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.opts = opts
        self.log = log or Logger(None, echo=opts.verbose)
        self.http = http or Http(log=self.log, sleep=sleep)
        self.clock = clock
        self.sleep = sleep
        self.run_id = uuid.uuid4().hex[:8]
        self.phases: list[dict[str, Any]] = []
        self.checks: list[Check] = []
        self.notes: list[str] = []
        self.mutated = False
        self.restore_ok: bool | None = None
        self.abort_message = ""
        self.mail_note = ""
        self.deadline = math.inf
        self.addr: dict[str, str] = {}
        self.session_header = cfg.affinity.header
        self.keep: HostConfig
        self.work: HostConfig
        self._sizes: dict[str, dict[str, float]] = {}

    # -- configuration ------------------------------------------------------
    def pick_hosts(self) -> None:
        """The M5 is the entry node and must not be touched; the other one is
        the work host. Refuse anything that is not exactly two nodes — the
        scenarios are written for one pair."""
        enabled = self.cfg.enabled_hosts()
        if len(enabled) != 2:
            raise ScenarioAbort(
                EXIT_PREFLIGHT,
                f"expected exactly two enabled hosts, config has {len(enabled)}: "
                + ", ".join(h.name for h in enabled))
        entry = [h for h in enabled if h.role == "entry"]
        self.keep = entry[0] if entry else enabled[0]
        self.work = next(h for h in enabled if h.name != self.keep.name)

    def url(self, host: str, path: str) -> str:
        base = self.addr.get(host) or ""
        if not base:
            cfg = self.cfg.host(host)
            base = cfg.addresses[0] if cfg and cfg.addresses else ""
        return base.rstrip("/") + path

    def host_key(self, name: str) -> str:
        cfg = self.cfg.host(name)
        return (cfg.api_key if cfg and cfg.api_key else self.opts.api_key) or ""

    def host_headers(self, name: str) -> dict[str, str]:
        key = self.host_key(name)
        return {"authorization": f"Bearer {key}"} if key else {}

    # -- deadline -----------------------------------------------------------
    def check_deadline(self) -> None:
        if self.clock() >= self.deadline:
            raise DeadlineReached(
                f"global deadline of {minutes(self.opts.deadline_s)} reached "
                f"after phases: {', '.join(p['id'] for p in self.phases) or 'none'}")

    # -- RIPPLE views -------------------------------------------------------
    def healthz(self) -> dict:
        resp = self.http.request("GET", f"{self.opts.ripple}/healthz", timeout=30.0)
        if resp.status != 200:
            raise TransportError(f"/healthz -> HTTP {resp.status}")
        data = resp.json()
        if not isinstance(data, dict):
            raise TransportError("/healthz returned no JSON object")
        return data

    def stats(self) -> dict:
        resp = self.http.request("GET", f"{self.opts.ripple}/stats", timeout=30.0)
        if resp.status != 200:
            raise TransportError(f"/stats -> HTTP {resp.status}")
        return resp.json() or {}

    @staticmethod
    def hz_host(hz: dict, name: str) -> dict:
        for h in hz.get("hosts") or []:
            if h.get("name") == name:
                return h
        return {}

    @staticmethod
    def hz_model(hz: dict, model: str) -> dict:
        for m in hz.get("models") or []:
            if m.get("id") == model:
                return m
        return {}

    @staticmethod
    def bound_threads(hz: dict, model: str) -> list[dict]:
        reg = (hz.get("threads") or {}).get("registry") or []
        return [t for t in reg if t.get("model") == model]

    # -- oMLX state ---------------------------------------------------------
    def models(self, host: str) -> dict[str, dict]:
        resp = self.http.request("GET", self.url(host, "/v1/models/status"),
                                 headers=self.host_headers(host), timeout=30.0)
        if resp.status != 200:
            raise TransportError(f"{host} /v1/models/status -> HTTP {resp.status}")
        data = resp.json() or {}
        return {m["id"]: m for m in data.get("models") or [] if "id" in m}

    def loaded(self, host: str, model: str) -> bool:
        info = self.models(host).get(model)
        return bool(info and info.get("loaded"))

    def ripple_model_loaded(self, host: str, model: str) -> bool:
        for m in self.hz_model(self.healthz(), model).get("hosts") or []:
            if m.get("host") == host:
                return bool(m.get("loaded"))
        return False

    def wait_ripple_loaded(self, host: str, model: str, want: bool,
                           timeout: float = 120.0, interval: float = 5.0) -> bool:
        """Wait until *RIPPLE* believes the host holds the model.

        The proxy decides placement on its own view, refreshed every
        `discovery.interval_seconds` (10 s here). Sending the probe request
        before the view caught up would measure the old state — a `cold_load`
        would come back as `evict_idle`, and a conflict as a served request.
        """
        t0 = self.clock()
        while True:
            if self.ripple_model_loaded(host, model) is want:
                self.log(f"  ripple view: {host} {model} loaded={want}")
                return True
            if self.clock() - t0 >= timeout:
                self.log(f"  timeout after {timeout:.0f}s waiting for RIPPLE to "
                         f"see {host} {model} loaded={want}")
                return False
            self.check_deadline()
            self.sleep(interval)

    def wait_loaded(self, host: str, model: str, want: bool, timeout: float,
                    interval: float = 3.0) -> bool:
        t0 = self.clock()
        while True:
            if self.loaded(host, model) is want:
                return True
            if self.clock() - t0 >= timeout:
                self.log(f"  timeout after {timeout:.0f}s waiting for {host} "
                         f"{model} loaded={want}")
                return False
            self.check_deadline()
            self.sleep(interval)

    def unload(self, host: str, model: str, *, timeout: float = 180.0) -> None:
        if not self.loaded(host, model):
            self.log(f"  {host}: {model} already unloaded")
            return
        self.log(f"  {host}: unloading {model}")
        resp = self.http.request(
            "POST", self.url(host, f"/v1/models/{model}/unload"),
            headers=self.host_headers(host), timeout=120.0)
        if resp.status >= 400:
            raise ScenarioAbort(EXIT_FAILED,
                                f"unload {model} on {host} -> HTTP {resp.status}: "
                                f"{resp.text[:200]}")
        if not self.wait_loaded(host, model, False, timeout):
            raise ScenarioAbort(EXIT_FAILED,
                                f"{host} still reports {model} loaded after "
                                f"{timeout:.0f}s")

    def load(self, host: str, model: str, *, timeout: float = 480.0) -> None:
        if self.loaded(host, model):
            self.log(f"  {host}: {model} already loaded")
            return
        self.log(f"  {host}: loading {model} (this takes a minute or two)")
        resp = self.http.request(
            "POST", self.url(host, f"/v1/models/{model}/load"),
            headers=self.host_headers(host), timeout=timeout)
        if resp.status >= 400:
            raise ScenarioAbort(EXIT_FAILED,
                                f"load {model} on {host} -> HTTP {resp.status}: "
                                f"{resp.text[:200]}")
        if not self.wait_loaded(host, model, True, timeout):
            raise ScenarioAbort(EXIT_FAILED,
                                f"{host} does not report {model} loaded within "
                                f"{timeout:.0f}s")

    # -- chat through RIPPLE ------------------------------------------------
    def serve_host(self) -> str:
        """The host that has to serve a probe request: the work host.

        The entry host is not on the probe's allow-list, which is exactly why a
        transient removal from the pool turns into a 503 instead of a failover —
        there is no second candidate to fall back to.
        """
        work = getattr(self, "work", None)
        return work.name if work is not None else ""

    @staticmethod
    def reason_of(resp: Resp) -> str:
        """`error.reason` of a RIPPLE error body, or '' for anything else."""
        body = resp.json()
        if not isinstance(body, dict):
            return ""
        err = body.get("error")
        if not isinstance(err, dict):
            return ""
        return str(err.get("reason") or "")

    @staticmethod
    def describe_host(status: dict) -> str:
        """One line about the last /healthz view of a host, for a failure mail."""
        if not status:
            return "no /healthz answer"
        if status.get("error"):
            return str(status["error"])
        return (f"healthy={status.get('healthy')} "
                f"identity_ok={status.get('identity_ok')} "
                f"status={status.get('status')}")

    def wait_host_healthy(self, host: str, *, timeout: float | None = None,
                          interval: float = HEALTH_POLL_S) -> tuple[bool, dict]:
        """Poll /healthz until *host* is healthy and identity-checked again.

        Returns (ok, last view of the host). Every poll is logged, so the morning
        can see whether the pool came back in two seconds or in ninety. Bounded
        twice: by `health_wait_s` and by what is left of the global deadline.
        """
        timeout = self.opts.health_wait_s if timeout is None else timeout
        if self.deadline != math.inf:
            timeout = max(0.0, min(timeout, self.deadline - self.clock()))
        attempts_max = max(1, int(math.ceil(timeout / interval)))
        t0 = self.clock()
        last: dict[str, Any] = {}
        for attempt in range(1, attempts_max + 1):
            try:
                hz = self.healthz()
            except (TransportError, OSError) as exc:
                last = {"error": f"/healthz unreachable: {exc}"}
                self.log(f"  wait: {host} status=unknown attempt "
                         f"{attempt}/{attempts_max} ({last['error']})")
            else:
                last = self.hz_host(hz, host)
                healthy = last.get("healthy") is True
                ident = last.get("identity_ok") is True
                self.log(f"  wait: {host} healthy={str(healthy).lower()} "
                         f"identity_ok={str(ident).lower()} "
                         f"attempt {attempt}/{attempts_max}")
                if healthy and ident:
                    self.log(f"  wait: {host} back in the pool after "
                             f"{self.clock() - t0:.1f}s "
                             f"({attempt} poll(s), {self.describe_host(last)})")
                    return True, last
            if self.clock() - t0 >= timeout:
                break
            self.sleep(interval)
        return False, last

    def chat(self, model: str, *, max_tokens: int, prompt: str, timeout: float,
             purpose: str, session_id: str | None = None) -> Resp:
        """One chat request through RIPPLE, with the transient-503 wait.

        A 503 whose `error.reason` is `no_healthy_host` (or `no_candidate`) is
        not an answer about the model — it is RIPPLE's pool having dropped the
        host because a single health probe timed out, which is what happens in
        the seconds after a 111 GB cold load. Those are waited out (poll
        /healthz until the host is healthy again) and retried, at most
        `MAX_HEALTH_RETRIES` times. A 503 `conflict` is returned untouched: it
        is AS-5c's measurement, and retrying it would report three refusals as
        one.
        """
        headers = {"content-type": "application/json"}
        if session_id:
            headers[self.session_header] = session_id
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "temperature": 0, "stream": False}
        host = self.serve_host()
        attempts = MAX_HEALTH_RETRIES + 1
        for attempt in range(1, attempts + 1):
            t0 = self.clock()
            resp = self.http.request(
                "POST", f"{self.opts.ripple}/v1/chat/completions",
                json_body=body, headers=headers, timeout=timeout,
                retry_5xx=False)
            wall = self.clock() - t0
            reason = self.reason_of(resp)
            self.log(f"  chat[{purpose}] model={model} status={resp.status} "
                     f"host={resp.header('x-ripple-host') or '-'} "
                     f"reason={resp.header('x-ripple-reason') or '-'} "
                     f"wall={wall:.1f}s (http {resp.latency_s:.1f}s)"
                     + (f" attempt={attempt}/{attempts}" if attempt > 1 else "")
                     + (f" session={session_id}" if session_id else ""))
            if not (resp.status == 503 and reason in TRANSIENT_503_REASONS):
                return resp

            if attempt >= attempts:
                raise ScenarioAbort(
                    EXIT_FAILED,
                    f"{purpose}: {attempt} attempts for {model} all answered "
                    f"HTTP 503 {reason} even though {host or 'the host'} "
                    f"reported healthy — giving up after {MAX_HEALTH_RETRIES} "
                    f"retries: {resp.text[:200]}")

            self.log(f"  chat[{purpose}]: HTTP 503 {reason} — RIPPLE dropped "
                     f"{host} from the pool on a failed health probe; waiting "
                     f"for it to come back (retry {attempt}/{MAX_HEALTH_RETRIES})")
            ok, last = self.wait_host_healthy(host)
            if not ok:
                raise ScenarioAbort(
                    EXIT_FAILED,
                    f"{purpose}: {host} never became healthy again within "
                    f"{self.opts.health_wait_s:.0f}s after HTTP 503 {reason}; "
                    f"last /healthz for {host}: {self.describe_host(last)}. "
                    f"Nothing else offers {model}, so the request was not "
                    f"served and the run stops instead of measuring a "
                    f"half-restored fleet.")
        raise ScenarioAbort(EXIT_FAILED, f"{purpose}: no attempt was made")

    # -- preflight ----------------------------------------------------------
    def preflight(self) -> list[Check]:
        """Read-only. Nothing here may load, unload or chat."""
        self.checks = []
        self.notes = []
        keep, work = self.keep.name, self.work.name

        def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
            self.checks.append(Check(name, ok, detail, required))
            tag = "PASS" if ok else ("FAIL" if required else "info")
            self.log(f"preflight {tag}  {name}: {detail}")

        add("config: exactly two enabled hosts",
            len(self.cfg.enabled_hosts()) == 2,
            f"{keep} (entry, weight {self.keep.weight:g}) and {work} "
            f"(weight {self.work.weight:g})")
        auth = self.cfg.auth
        add("config: RIPPLE accepts loopback without a key",
            (not auth.enabled) or auth.allow_loopback,
            f"auth.enabled={auth.enabled} allow_loopback={auth.allow_loopback}")

        try:
            hz = self.healthz()
        except (TransportError, OSError) as exc:
            add("ripple /healthz reachable", False, str(exc))
            return self.checks
        add("ripple /healthz reachable", hz.get("ok") is True,
            f"ok={hz.get('ok')} version={hz.get('version')} "
            f"uptime={hz.get('uptime_seconds')}s")
        add("ripple: eviction guard enabled",
            bool((hz.get("capabilities") or {}).get("eviction_guard")),
            f"eviction_guard={(hz.get('capabilities') or {}).get('eviction_guard')}")

        for name in (keep, work):
            h = self.hz_host(hz, name)
            add(f"ripple: host {name} healthy",
                bool(h) and h.get("healthy") is True and h.get("status") == "healthy",
                f"status={h.get('status')} healthy={h.get('healthy')} "
                f"address={h.get('address')}")
            add(f"ripple: host {name} identity_ok", h.get("identity_ok") is True,
                f"identity_ok={h.get('identity_ok')} "
                f"node_id={str(h.get('node_id'))[:8]}")
            self.addr[name] = h.get("address") or self.cfg.host(name).addresses[0]

        status: dict[str, dict[str, dict]] = {}
        for name in (keep, work):
            try:
                status[name] = self.models(name)
                self._sizes[name] = {mid: float(info.get("estimated_size") or 0)
                                     for mid, info in status[name].items()}
                add(f"{name}: model status readable", True,
                    f"{len(status[name])} models, "
                    f"{sum(1 for m in status[name].values() if m.get('loaded'))} loaded")
            except (TransportError, OSError) as exc:
                add(f"{name}: model status readable", False, str(exc))
                return self.checks

        for name in (keep, work):
            info = status[name].get(self.opts.heavy) or {}
            add(f"{name}: {self.opts.heavy} loaded", bool(info.get("loaded")),
                f"loaded={info.get('loaded')} pinned={info.get('pinned')} "
                f"size={gb(info.get('estimated_size'))}")

        pinned = [n for n in (keep, work)
                  if (status[n].get(self.opts.heavy) or {}).get("pinned")]
        add(f"{work}: {self.opts.heavy} is not pinned (it must be unloadable)",
            work not in pinned, f"pinned_on={pinned or 'none'}")

        offered = []
        for name in (keep, work):
            entry = None
            for m in self.hz_model(hz, self.opts.probe).get("hosts") or []:
                if m.get("host") == name:
                    entry = m
            allowed = bool(entry and entry.get("allowed") and entry.get("available"))
            if allowed:
                offered.append(name)
            # Only the work host is required to offer it. Whether the keep host
            # does is a property of the allow-list, not of the run: if it does
            # not, the M5 can never be a candidate and the conflict message can
            # only name the work host. Reported, never fatal.
            add(f"ripple: {name} offers {self.opts.probe}", allowed,
                f"allowed={entry.get('allowed') if entry else None} "
                f"available={entry.get('available') if entry else None}",
                required=(name == work))
        if work not in offered:
            add(f"ripple: no host offers {self.opts.probe}", False,
                f"offered_by={offered}")
        if keep not in offered:
            self.notes.append(
                f"{keep} does not allow {self.opts.probe} (allow-list), so the "
                "routing decision in AS-5b is constrained by the allow-list as "
                "well as by the eviction forecast, and the AS-5c conflict "
                f"message can only name {work}.")

        for name in (keep, work):
            info = status[name].get(self.opts.probe) or {}
            add(f"{name}: {self.opts.probe} not loaded", not info.get("loaded"),
                f"loaded={info.get('loaded')} size={gb(info.get('estimated_size'))}")

        for name in (keep, work):
            h = self.hz_host(hz, name)
            add(f"ripple: no request in flight on {name}", h.get("inflight") == 0,
                f"inflight={h.get('inflight')}/{h.get('max_inflight')}")

        probe_threads = self.bound_threads(hz, self.opts.probe)
        add(f"ripple: no bound thread on {self.opts.probe}", not probe_threads,
            f"threads={[(t.get('key'), t.get('host')) for t in probe_threads]}")

        # The arithmetic the scenario rests on, using oMLX's own estimated_size.
        wh = self.hz_host(hz, work)
        ceiling = float(wh.get("ceiling_bytes") or 0)
        used = float(wh.get("used_bytes") or 0)
        target = self.work.hard_threshold * ceiling
        heavy_size = self._sizes[work].get(self.opts.heavy, 0.0)
        probe_size = self._sizes[work].get(self.opts.probe, 0.0)
        after = used - heavy_size + probe_size
        add(f"{work}: {self.opts.probe} fits after unloading {self.opts.heavy}",
            bool(ceiling) and after <= target,
            f"forecast {gb(after)} <= target {gb(target)} "
            f"({self.work.hard_threshold:g} x {gb(ceiling)})")
        add(f"{work}: {self.opts.probe} does not fit next to {self.opts.heavy}",
            bool(ceiling) and used + probe_size > target,
            f"now {gb(used + probe_size)} > target {gb(target)}")
        return self.checks

    @property
    def preflight_ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks if c.required)

    def print_checks(self) -> None:
        print(f"preflight against {self.opts.ripple} — read-only, nothing is changed")
        for c in self.checks:
            tag = "ok" if c.ok else ("info" if not c.required else "FAIL")
            print(f"  [{tag}] {c.name}: {c.detail}")
        passed = sum(1 for c in self.checks if c.ok)
        print(f"preflight: {passed}/{len(self.checks)} checks ok"
              + ("" if self.preflight_ok else " — REFUSED, nothing changed"))

    # -- the plan (--dry-run) ----------------------------------------------
    def print_plan(self) -> None:
        keep, work = self.keep.name, self.work.name
        probe, heavy = self.opts.probe, self.opts.heavy
        n = self.opts.requests
        print("")
        print("PLAN — the following would run; nothing is executed by --dry-run")
        print(f"  1. POST {self.url(work, f'/v1/models/{heavy}/unload')}"
              f"  → poll loaded=false, timeout 120 s")
        print(f"  2. {n} x POST {self.opts.ripple}/v1/chat/completions"
              f"  model={probe} max_tokens=8 timeout=300 s each")
        print(f"     after every request: GET {self.url(keep, '/v1/models/status')}"
              f" and assert {heavy} still loaded")
        print(f"     expected: {n}/{n} x-ripple-host={work}, first "
              f"x-ripple-reason=cold_load, no 503")
        print(f"  3. POST {self.url(work, f'/v1/models/{probe}/unload')}"
              f"  → then POST .../{heavy}/load  → poll loaded=true, timeout 480 s")
        print(f"  4. open a {heavy} thread on each host (2+ requests, distinct "
              f"{self.session_header}, prompts above "
              f"{self.cfg.affinity.min_prompt_tokens} est. tokens so they hold "
              f"slots), verify via /healthz thread registry")
        print(f"  5. 1 x POST {self.opts.ripple}/v1/chat/completions"
              f"  model={probe} max_tokens=8 timeout=120 s")
        print("     expected: HTTP 503, error.reason=conflict, no upstream call, "
              "nothing loaded, nothing evicted")
        print(f"  6. restore: {probe} unloaded everywhere, {heavy} loaded on "
              f"{keep} and {work}; write "
              f"{self.opts.report_dir}/as5-live-{datetime.now():%Y-%m-%d}.md")
        print(f"  deadline {minutes(self.opts.deadline_s)}; exit codes 0/2/3/4/5")

    # -- phase 1: AS-5b -----------------------------------------------------
    def phase_as5b(self) -> None:
        keep, work = self.keep.name, self.work.name
        probe, heavy = self.opts.probe, self.opts.heavy
        self.check_deadline()
        phase: dict[str, Any] = {"id": "AS-5b", "status": "running",
                                 "started": iso_now(), "rows": []}
        self.phases.append(phase)
        self.log(f"phase AS-5b: unload {heavy} on {work}, then "
                 f"{self.opts.requests} requests for {probe}")

        t0 = self.clock()
        self.unload(work, heavy, timeout=120.0)
        phase["unload_s"] = round(self.clock() - t0, 1)
        if not self.wait_ripple_loaded(work, heavy, False, timeout=120.0):
            phase["status"] = "failed"
            raise ScenarioAbort(
                EXIT_FAILED,
                f"RIPPLE still believes {heavy} is loaded on {work}; a cold "
                f"load cannot be observed and AS-5b would measure the wrong "
                f"reason")

        for i in range(1, self.opts.requests + 1):
            self.check_deadline()
            prompt = (f"AS-5b probe {i} of {self.opts.requests}, run {self.run_id}. "
                      "Answer with the single word: ok.")
            resp = self.chat(probe, max_tokens=8, prompt=prompt, timeout=300.0,
                             purpose=f"as5b #{i}")
            row = {"n": i, "status": resp.status,
                   "host": resp.header("x-ripple-host"),
                   "reason": resp.header("x-ripple-reason"),
                   "latency_s": round(resp.latency_s, 3)}
            phase["rows"].append(row)
            if i == 1:
                phase["first_s"] = round(resp.latency_s, 1)

            if resp.status != 200:
                phase["status"] = "failed"
                raise ScenarioAbort(
                    EXIT_FAILED,
                    f"AS-5b request {i} answered HTTP {resp.status}: "
                    f"{resp.text[:300]}")

            host = row["host"]
            if host == keep:
                phase["status"] = "failed"
                self.log(f"! AS-5b request {i} landed on {keep} — that must not "
                         f"happen; restoring")
                self.restore_now()
                raise M5Touched(
                    f"AS-5b request {i} was served by {keep}, not {work}")

            # The M5 is the operator's machine: after *every* request its
            # DeepSeek must still be loaded. If it is not, restore at once.
            if not self.loaded(keep, heavy):
                phase["status"] = "failed"
                self.log(f"! {heavy} is gone from {keep} after request {i}; "
                         f"restoring immediately")
                self.restore_now()
                raise M5Touched(
                    f"{heavy} was evicted from {keep} during AS-5b (request {i})")

        hosts = {r["host"] for r in phase["rows"]}
        reasons = [r["reason"] for r in phase["rows"]]
        if hosts != {work}:
            phase["status"] = "failed"
            raise ScenarioAbort(EXIT_FAILED,
                                f"AS-5b: responses landed on {sorted(hosts)}, "
                                f"expected only {work}")
        if reasons[0] != "cold_load":
            phase["status"] = "failed"
            raise ScenarioAbort(EXIT_FAILED,
                                f"AS-5b: first reason was {reasons[0]!r}, "
                                f"expected 'cold_load'")
        phase["status"] = "pass"
        phase["reasons"] = reasons
        self.log(f"phase AS-5b: PASS {len(phase['rows'])}/{len(phase['rows'])} on "
                 f"{work}, first reason {reasons[0]}, "
                 f"{phase.get('first_s')}s cold load")

    # -- phase 2: restore the fleet before AS-5c ---------------------------
    def phase_between(self) -> None:
        keep, work = self.keep.name, self.work.name
        probe, heavy = self.opts.probe, self.opts.heavy
        self.check_deadline()
        phase: dict[str, Any] = {"id": "restore", "status": "running",
                                 "started": iso_now()}
        self.phases.append(phase)
        self.log(f"phase restore: {probe} off {work}, {heavy} back on {work}")
        t0 = self.clock()
        self.unload(work, probe, timeout=180.0)
        phase["unload_s"] = round(self.clock() - t0, 1)
        t1 = self.clock()
        self.load(work, heavy, timeout=480.0)
        phase["load_s"] = round(self.clock() - t1, 1)
        if not self.loaded(work, heavy):
            phase["status"] = "failed"
            raise ScenarioAbort(EXIT_FAILED, f"{heavy} not loaded on {work} "
                                             f"after restore")
        for model, want in ((probe, False), (heavy, True)):
            if not self.wait_ripple_loaded(work, model, want, timeout=120.0):
                phase["status"] = "failed"
                raise ScenarioAbort(
                    EXIT_FAILED,
                    f"RIPPLE's view of {work} did not settle: {model} "
                    f"loaded={want} not observed; AS-5c would measure the "
                    f"wrong state")
        phase["status"] = "pass"
        self.log(f"phase restore: PASS ({heavy} loaded on {work} and {keep})")

    # -- phase 3: AS-5c -----------------------------------------------------
    def _long_prompt(self, marker: str) -> str:
        """A prompt above `min_prompt_tokens` so the thread holds a slot.

        Without a slot the eviction forecast stays on the EVICT_IDLE tier and
        the host would happily drop an idle DeepSeek — AS-5c would then measure
        nothing. estimate_tokens is len(text)//4 (ripple/registry.py), so the
        margin is deliberate.
        """
        needed = int(self.cfg.affinity.min_prompt_tokens * 4 * 1.2) + 600
        body = (FILLER * (needed // len(FILLER) + 1))[:needed]
        return (f"Session {marker} of run {self.run_id}. Read the reference block "
                f"and then answer with one word.\n\n{body}\n\nAnswer with the "
                f"single word: ok.")

    def phase_as5c(self) -> None:
        keep, work = self.keep.name, self.work.name
        probe, heavy = self.opts.probe, self.opts.heavy
        self.check_deadline()
        phase: dict[str, Any] = {"id": "AS-5c", "status": "running",
                                 "started": iso_now(), "threads": []}
        self.phases.append(phase)
        self.log(f"phase AS-5c: bind a {heavy} thread on each host, then ask for "
                 f"{probe}")

        # 1. a live thread on DeepSeek per host — a bound thread is what makes
        #    the forecast say CONFLICT instead of EVICT_IDLE.
        for attempt in range(1, 7):
            self.check_deadline()
            hz = self.healthz()
            have = {t.get("host") for t in self.bound_threads(hz, heavy)}
            if {keep, work} <= have:
                break
            sid = f"as5c-{self.run_id}-{attempt}"
            resp = self.chat(heavy, max_tokens=4,
                             prompt=self._long_prompt(f"{sid}-a"),
                             session_id=sid, timeout=300.0,
                             purpose=f"as5c thread {attempt}")
            if resp.status != 200:
                phase["status"] = "failed"
                raise ScenarioAbort(EXIT_FAILED,
                                    f"AS-5c thread setup answered HTTP "
                                    f"{resp.status}: {resp.text[:300]}")
            phase["threads"].append({"session": sid,
                                     "host": resp.header("x-ripple-host"),
                                     "latency_s": round(resp.latency_s, 1)})

        hz = self.healthz()
        bound = {t.get("host") for t in self.bound_threads(hz, heavy)}
        slots = {n: self.hz_host(hz, n).get("thread_slots") for n in (keep, work)}
        phase["bound_hosts"] = sorted(bound)
        phase["slots"] = slots
        if not {keep, work} <= bound:
            phase["status"] = "failed"
            raise ScenarioAbort(
                EXIT_FAILED,
                f"AS-5c: no {heavy} thread bound on {sorted({keep, work} - bound)} "
                f"after {len(phase['threads'])} requests (bound: {sorted(bound)})")
        self.log(f"phase AS-5c: threads bound on {sorted(bound)}, slots {slots}")

        # 2. the request that must be refused, with no upstream call at all.
        before = (self.stats().get("by_model") or {}).get(probe, {}).get("requests", 0)
        resp = self.chat(probe, max_tokens=8,
                         prompt=f"AS-5c probe, run {self.run_id}. Answer with the "
                                f"single word: ok.",
                         timeout=120.0, purpose="as5c probe")
        after = (self.stats().get("by_model") or {}).get(probe, {}).get("requests", 0)
        phase["status_code"] = resp.status
        phase["latency_s"] = round(resp.latency_s, 3)
        phase["upstream_requests_before"] = before
        phase["upstream_requests_after"] = after
        body = resp.json() or {}
        err = body.get("error") or {}
        phase["reason"] = err.get("reason")
        phase["message"] = err.get("message")

        if resp.status == 200:
            phase["status"] = "failed"
            phase["served_by"] = resp.header("x-ripple-host")
            self.log(f"! AS-5c was served by {phase['served_by']} instead of "
                     f"refused; restoring")
            self.restore_now()
            raise ScenarioAbort(EXIT_SERVED,
                                f"AS-5c was served (HTTP 200 by "
                                f"{phase['served_by']}) instead of refused")

        if resp.status != 503:
            phase["status"] = "failed"
            raise ScenarioAbort(EXIT_FAILED,
                                f"AS-5c answered HTTP {resp.status} "
                                f"({err.get('reason')}): {resp.text[:300]}")

        message = phase["message"] or ""
        offered = [n for n in (keep, work) if self._offers(hz, n, probe)]
        missing = [n for n in offered if n not in message]
        phase["named_hosts"] = [n for n in offered if n in message]
        if phase["reason"] != "conflict" or missing:
            phase["status"] = "failed"
            raise ScenarioAbort(
                EXIT_FAILED,
                f"AS-5c 503 was not a conflict naming every offered host "
                f"(reason={phase['reason']!r}, missing={missing}): {message[:300]}")

        # 3. nothing was loaded and nothing was evicted.
        problems = []
        for name in (keep, work):
            if self.loaded(name, probe):
                problems.append(f"{probe} is loaded on {name}")
            if not self.loaded(name, heavy):
                problems.append(f"{heavy} is gone from {name}")
        if after != before:
            problems.append(f"{probe} reached an upstream ({before} -> {after} "
                            f"requests)")
        if problems:
            phase["status"] = "failed"
            raise ScenarioAbort(EXIT_FAILED,
                                "AS-5c refused but the fleet changed: "
                                + "; ".join(problems))
        phase["status"] = "pass"
        self.log(f"phase AS-5c: PASS HTTP 503 conflict in "
                 f"{phase['latency_s']}s, no upstream call, nothing loaded")

    def _offers(self, hz: dict, name: str, model: str) -> bool:
        for m in self.hz_model(hz, model).get("hosts") or []:
            if m.get("host") == name:
                return bool(m.get("allowed") and m.get("available"))
        return False

    # -- restore ------------------------------------------------------------
    def restore_now(self) -> bool:
        """Immediate restore, used inside a failure path. The `finally` in
        `execute` restores again afterwards; every step here is idempotent."""
        return self.restore_final_state()

    def restore_final_state(self, attempts: int = 3) -> bool:
        keep, work = self.keep.name, self.work.name
        probe, heavy = self.opts.probe, self.opts.heavy
        self.deadline = math.inf          # the restore is never cut short
        waits = (0.0, 15.0, 60.0)
        for attempt in range(1, attempts + 1):
            wait = waits[min(attempt - 1, len(waits) - 1)]
            if wait:
                self.log(f"restore: waiting {wait:.0f}s before attempt {attempt}")
                self.sleep(wait)
            try:
                self.log(f"restore: attempt {attempt}/{attempts}")
                for name in (keep, work):
                    if self.loaded(name, probe):
                        self.unload(name, probe, timeout=180.0)
                for name in (keep, work):
                    if not self.loaded(name, heavy):
                        self.load(name, heavy, timeout=480.0)
                state = self.final_state()
                if not state["ok"]:
                    raise ScenarioAbort(EXIT_FAILED,
                                        "final state still wrong: "
                                        + "; ".join(state["problems"]))
                self.log("restore: ok — " + state["summary"])
                return True
            except Exception as exc:                       # noqa: BLE001
                self.log(f"restore: attempt {attempt} failed: "
                         f"{type(exc).__name__}: {exc}")
        self.print_restore_failed()
        return False

    def print_restore_failed(self) -> None:
        keep, work = self.keep.name, self.work.name
        print("RESTORE FAILED — the fleet is not in its documented state. "
              "Run these by hand:")
        print("  KEY=$(cat ~/.omlx/api_key.txt)")
        for name in (keep, work):
            for verb in ("unload", "load"):
                model = self.opts.probe if verb == "unload" else self.opts.heavy
                print(f"  curl -s -X POST -H \"Authorization: Bearer $KEY\" "
                      f"{self.url(name, f'/v1/models/{model}/{verb}')}")
            print(f"  curl -s -H \"Authorization: Bearer $KEY\" "
                  f"{self.url(name, '/v1/models/status')}")
        print("  ./.venv/bin/python tools/as5_live.py --verify-only")

    # -- final state --------------------------------------------------------
    def final_state(self) -> dict[str, Any]:
        problems: list[str] = []
        info: dict[str, Any] = {"hosts": {}, "problems": problems}
        try:
            hz = self.healthz()
        except (TransportError, OSError) as exc:
            problems.append(f"RIPPLE unreachable: {exc}")
            info["ok"] = False
            info["summary"] = "RIPPLE unreachable"
            return info
        if not hz.get("ok"):
            problems.append("RIPPLE /healthz reports not ok")
        for name in (self.keep.name, self.work.name):
            h = self.hz_host(hz, name)
            row = {"healthy": h.get("healthy"), "status": h.get("status"),
                   "used_bytes": h.get("used_bytes"),
                   "ceiling_bytes": h.get("ceiling_bytes"),
                   "thread_slots": h.get("thread_slots")}
            try:
                models = self.models(name)
                row["loaded"] = sorted(mid for mid, m in models.items()
                                       if m.get("loaded"))
            except (TransportError, OSError) as exc:
                problems.append(f"{name} model status unreachable: {exc}")
                row["loaded"] = []
            info["hosts"][name] = row
            if not h.get("healthy"):
                problems.append(f"{name} is not healthy ({h.get('status')})")
            if self.opts.probe in row["loaded"]:
                problems.append(f"{self.opts.probe} is still loaded on {name}")
            if self.opts.heavy not in row["loaded"]:
                problems.append(f"{self.opts.heavy} is not loaded on {name}")
        leftover = self.bound_threads(hz, self.opts.probe)
        if leftover:
            problems.append(f"{len(leftover)} bound thread(s) on "
                            f"{self.opts.probe}")
        info["ok"] = not problems
        info["summary"] = "; ".join(problems) if problems else (
            f"{self.opts.heavy} on both hosts, {self.opts.probe} on neither, "
            f"RIPPLE healthy")
        return info

    # -- orchestration ------------------------------------------------------
    def execute(self) -> int:
        self.deadline = self.clock() + self.opts.deadline_s
        self.pick_hosts()
        self.preflight()
        self.print_checks()
        if not self.preflight_ok:
            self.log("preflight refused — nothing was changed")
            return EXIT_PREFLIGHT

        self.mutated = True
        code = EXIT_FAILED
        try:
            code = self.run_scenarios()
        except ScenarioAbort as exc:
            self.log(f"abort: {exc}")
            code = exc.code
        except Exception:                                   # noqa: BLE001
            self.log("unexpected error:\n" + traceback.format_exc())
            code = EXIT_FAILED
        finally:
            self.restore_ok = self.restore_final_state()
            if not self.restore_ok:
                code = EXIT_RESTORE_FAILED
            self.finish(code)
        return code

    def run_scenarios(self) -> int:
        try:
            self.phase_as5b()
            self.phase_between()
            self.phase_as5c()
            return EXIT_OK
        except ScenarioAbort as exc:
            self.abort_message = str(exc)
            self.log(f"scenario aborted (exit {exc.code}): {exc}")
            self.close_running_phases(str(exc))
            return exc.code
        except Exception as exc:                            # noqa: BLE001
            self.abort_message = f"{type(exc).__name__}: {exc}"
            self.log("unexpected error in a phase:\n" + traceback.format_exc())
            self.close_running_phases(self.abort_message)
            return EXIT_FAILED

    def close_running_phases(self, message: str) -> None:
        """A phase that was cut off must not be reported as 'running'."""
        for phase in self.phases:
            if phase.get("status") == "running":
                phase["status"] = "failed"
                phase["abort"] = message

    def watchdog(self) -> int:
        """--verify-only: is the final state intact? Repair it if not.

        One line of output either way — this is what a cron job mails at 03:00.
        """
        self.pick_hosts()
        state = self.final_state()
        if state["ok"]:
            self.log("watchdog: intact — " + state["summary"])
            print(f"AS-5 final state intact — {state['summary']}")
            return EXIT_OK
        broken = "; ".join(state["problems"])
        self.log("watchdog: not intact — " + broken)
        ok = self.restore_final_state()
        state = self.final_state()
        if ok and state["ok"]:
            print(f"AS-5 final state repaired — was: {broken}; now: "
                  f"{state['summary']}")
            return EXIT_OK
        print(f"RESTORE FAILED — still: {'; '.join(state['problems'])}")
        return EXIT_RESTORE_FAILED

    # -- report and summary -------------------------------------------------
    def finish(self, code: int) -> None:
        state = self.final_state()
        path: Path | None = None
        try:
            path = self.write_report(code, state)
        except OSError as exc:
            self.log(f"report not written: {exc}")
        self.mail_note = self.mail_status(path)
        self.log(self.mail_note)
        for line in self.summary_lines(code, state, path):
            print(line)

    def mail_status(self, report: Path | None) -> str:
        if not self.opts.mail:
            return "Mail: disabled (--no-mail)"
        if report is None:
            return "Mail: skipped — no report file to send"
        if not mail_available():
            return ("Mail: skipped — tools/send_report_mail.py has no --report "
                    "argument (it sends a fixed Olla text)")
        return f"Mail: {send_mail(report)}"

    def phase_table(self) -> list[str]:
        rows = ["| Phase | Result | Measured |", "|---|---|---|"]
        for p in self.phases:
            measured = []
            for key in ("unload_s", "load_s", "first_s", "latency_s"):
                if key in p:
                    measured.append(f"{key}={p[key]}")
            if p.get("rows"):
                measured.append(f"{len(p['rows'])} requests")
            if p.get("slots"):
                measured.append(f"slots={p['slots']}")
            if p.get("status_code"):
                measured.append(f"http={p['status_code']}")
            rows.append(f"| {p['id']} | {p.get('status', '?')} | "
                        f"{', '.join(measured) or '—'} |")
        return rows

    def write_report(self, code: int, state: dict[str, Any]) -> Path:
        keep, work = self.keep.name, self.work.name
        probe, heavy = self.opts.probe, self.opts.heavy
        now = datetime.now()
        path = Path(self.opts.report_dir) / f"as5-live-{now:%Y-%m-%d}.md"
        lines: list[str] = []
        add = lines.append
        add(f"# AS-5b / AS-5c — live run, {now:%d.%m.%Y %H:%M}")
        add("")
        add(f"**Exit code:** {code} · **Run id:** `{self.run_id}` · "
            f"**Log:** `{self.log.path}`")
        add("")
        verdict = {EXIT_OK: "all scenarios passed, fleet restored",
                   EXIT_PREFLIGHT: "preflight refused — nothing was changed",
                   EXIT_M5_TOUCHED: "the M5 was touched — run aborted",
                   EXIT_SERVED: "AS-5c was served instead of refused",
                   EXIT_RESTORE_FAILED: "restore failed — fleet NOT in its "
                                        "documented state",
                   EXIT_FAILED: "a scenario failed"}.get(code, "unknown")
        add(f"**Result:** {verdict}")
        if self.abort_message:
            add(f"**Stopped because:** {self.abort_message}")
        add("")
        add("## Verdict per scenario")
        add("")
        add("| Scenario | Expectation | Verdict |")
        add("|---|---|---|")
        for p in self.phases:
            if p["id"] == "AS-5b":
                done = len(p.get("rows") or [])
                add(f"| AS-5b | {done}/{self.opts.requests} to {work}, first "
                    f"reason `cold_load`, {keep} untouched | "
                    f"**{p.get('status', '?')}** |")
            elif p["id"] == "AS-5c":
                add(f"| AS-5c | HTTP 503 `conflict`, no upstream call, nothing "
                    f"loaded, nothing evicted | **{p.get('status', '?')}** |")
        if not any(p["id"] == "AS-5b" for p in self.phases):
            add("| AS-5b | not run | — |")
        if not any(p["id"] == "AS-5c" for p in self.phases):
            add("| AS-5c | not run | — |")
        add("")
        add("## Method")
        add("")
        add(f"`tools/as5_live.py --run` on {now:%d.%m.%Y}, RIPPLE at "
            f"`{self.opts.ripple}`, hosts `{keep}` (entry, weight "
            f"{self.keep.weight:g}, must not be touched) and `{work}` (weight "
            f"{self.work.weight:g}). Heavy model `{heavy}`, probe model "
            f"`{probe}`. Every state change is verified by polling the host's "
            f"`/v1/models/status`, never assumed; the M5's `{heavy}` is re-read "
            f"after every single request. Timeouts: unload 120 s, load 480 s, "
            f"cold request 300 s. Deadline {minutes(self.opts.deadline_s)}.")
        add("")
        add(f"A chat request answered with HTTP 503 `no_healthy_host` (or "
            f"`no_candidate`) is waited out instead of failed: RIPPLE sets a "
            f"host's `healthy` flag from every single health probe, so one probe "
            f"timing out during a "
            f"{gb(self._sizes.get(work, {}).get(probe))} cold load drops `{work}` "
            f"from the candidate pool, and `{keep}` is not on the probe's "
            f"allow-list — the 503 is transient, not a dead host. The tool polls "
            f"`/healthz` every 2 s until `{work}` reports `healthy: true` and "
            f"`identity_ok: true` (up to {self.opts.health_wait_s:.0f} s) and then "
            f"retries, at most {MAX_HEALTH_RETRIES} times; the deliberate AS-5c "
            f"503 `conflict` is never retried — that one is the measurement.")
        add("")
        add("## Phase 0 — preflight (read-only)")
        add("")
        add("| Check | Value | Result |")
        add("|---|---|---|")
        for c in self.checks:
            mark = "ok" if c.ok else ("info" if not c.required else "**FAIL**")
            add(f"| {c.name} | {c.detail} | {mark} |")
        add("")
        add("## Phases")
        add("")
        lines.extend(self.phase_table())
        add("")

        for p in self.phases:
            if p["id"] == "AS-5b" and p.get("rows"):
                add("### AS-5b — one request per row")
                add("")
                add("| # | HTTP | `x-ripple-host` | `x-ripple-reason` | Latency |")
                add("|---|---|---|---|---|")
                for r in p["rows"]:
                    add(f"| {r['n']} | {r['status']} | {r['host'] or '—'} | "
                        f"`{r['reason'] or '—'}` | {r['latency_s']} s |")
                add("")
                add(f"Cold load on the first request: **{p.get('first_s')} s** "
                    f"(unload took {p.get('unload_s')} s).")
                add("")
            if p["id"] == "AS-5c":
                add("### AS-5c — the refusal")
                add("")
                opened = ", ".join(f"{t['session']}→{t['host']}"
                                   for t in p.get("threads") or []) or "—"
                add(f"- Threads opened for `{heavy}`: {opened}")
                add(f"- Bound hosts: {p.get('bound_hosts')}, slots: "
                    f"{p.get('slots')}")
                add(f"- Response: HTTP {p.get('status_code')} in "
                    f"{p.get('latency_s')} s, reason "
                    f"`{p.get('reason')}`")
                add(f"- Message: {p.get('message')}")
                add(f"- Upstream `{probe}` requests: "
                    f"{p.get('upstream_requests_before')} before, "
                    f"{p.get('upstream_requests_after')} after")
                add(f"- Hosts named in the conflict: {p.get('named_hosts')}")
                add(f"- The {len(p.get('threads') or [])} setup thread(s) stay "
                    f"bound to `{heavy}` for the thread TTL "
                    f"({self.cfg.affinity.ttl_seconds} s) and expire on their own.")
                add("")

        add("## Final state")
        add("")
        add("| Host | Status | Loaded | Used / Ceiling | Slots |")
        add("|---|---|---|---|---|")
        for name, row in state["hosts"].items():
            add(f"| {name} | {row.get('status')} | "
                f"{', '.join(row.get('loaded') or []) or '—'} | "
                f"{gb(row.get('used_bytes'))} / {gb(row.get('ceiling_bytes'))} | "
                f"{row.get('thread_slots')} |")
        add("")
        add(f"Verified: **{'intact' if state['ok'] else 'BROKEN'}** — "
            f"{state['summary']}.")
        if self.restore_ok is not None:
            add(f"Restore attempts: {'succeeded' if self.restore_ok else 'FAILED'}.")
        add("")
        if self.notes:
            add("## Notes on what this run can and cannot show")
            add("")
            for n in self.notes:
                add(f"- {n}")
            add("")
        add("## What a failure would have left behind")
        add("")
        add(f"- If phase 1 had landed on `{keep}`, oMLX would have evicted "
            f"`{heavy}` there — the operator's own session would have paid a "
            f"cold reload of {gb(self._sizes.get(keep, {}).get(heavy))}.")
        add(f"- If the restore had failed, `{work}` would sit without `{heavy}` "
            f"and possibly with `{probe}` loaded "
            f"({gb(self._sizes.get(work, {}).get(probe))}), i.e. the fleet would "
            f"be one model short of its documented state.")
        add(f"- The exact commands are printed by the script on a failed restore "
            f"and are repeated in `{self.log.path}`.")
        add("")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.log(f"report written: {path}")
        return path

    def summary_lines(self, code: int, state: dict[str, Any],
                      path: Path | None) -> list[str]:
        lines = [f"AS-5b/AS-5c live run {datetime.now():%Y-%m-%d %H:%M} "
                 f"(exit {code})"]
        for p in self.phases:
            if p["id"] == "AS-5b":
                rows = p.get("rows") or []
                if rows:
                    lines.append(
                        f"AS-5b: {p.get('status', '?').upper()} — "
                        f"{len(rows)}/{self.opts.requests} on {rows[0]['host']}, "
                        f"first reason {rows[0]['reason']}, cold load "
                        f"{p.get('first_s')}s, M5 {self.opts.heavy} intact after "
                        f"every request")
                else:
                    lines.append(f"AS-5b: {p.get('status', '?').upper()} — no "
                                 f"request was made")
            elif p["id"] == "AS-5c":
                lines.append(
                    f"AS-5c: {p.get('status', '?').upper()} — HTTP "
                    f"{p.get('status_code')} {p.get('reason')} in "
                    f"{p.get('latency_s')}s, upstream {self.opts.probe} requests "
                    f"{p.get('upstream_requests_before')}→"
                    f"{p.get('upstream_requests_after')}")
        if not self.phases:
            lines.append("no phase ran (preflight refused)")
        if self.abort_message:
            lines.append(f"Stopped: {self.abort_message[:160]}")
        lines.append(f"Final state: {'intact' if state['ok'] else 'BROKEN'} — "
                     f"{state['summary']}")
        if self.restore_ok is False:
            lines.append("RESTORE FAILED — see the log, commands printed above")
        lines.append(f"Report: {path if path else 'not written'}")
        lines.append(f"Log: {self.log.path}")
        if self.mail_note:
            lines.append(self.mail_note)
        return lines[:15]


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------
def mail_available() -> bool:
    """`tools/send_report_mail.py` is a one-off with a hardcoded body and no CLI,
    so mailing our report through it would send the wrong text. Detect that and
    skip, instead of pretending a mail went out."""
    path = REPO / "tools" / "send_report_mail.py"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return "--report" in text and "--subject" in text


def send_mail(report: Path) -> str:
    """Send the report if the mail helper has an interface for it. Returns one
    line for the summary; never raises."""
    cmd = [sys.executable, str(REPO / "tools" / "send_report_mail.py"),
           "--report", str(report),
           "--subject", f"AS-5b/AS-5c live run {datetime.now():%Y-%m-%d}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed — {type(exc).__name__}: {exc}"
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip().splitlines()[-1:]
        return f"failed — exit {out.returncode}: {' '.join(tail)[:200]}"
    return "sent"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def api_key_from_file(path: str | Path) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AS-5b/AS-5c live acceptance run: preflight, scenarios, "
                    "always-restore, watchdog.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="preflight only, strictly read-only")
    mode.add_argument("--dry-run", action="store_true",
                      help="preflight plus the exact plan, no writes")
    mode.add_argument("--run", action="store_true",
                      help="run both scenarios (default)")
    mode.add_argument("--verify-only", action="store_true",
                      help="watchdog: check the final state, repair it, no scenario")
    p.add_argument("--config", default="config.yaml", help="RIPPLE config (default config.yaml)")
    p.add_argument("--ripple", default=None,
                   help="RIPPLE base URL (default http://127.0.0.1:<listen_port>)")
    p.add_argument("--log-dir", default=str(REPO / "tools" / "as5"))
    p.add_argument("--report-dir", default=str(REPO / "docs"))
    p.add_argument("--heavy-model", default=HEAVY_DEFAULT)
    p.add_argument("--probe-model", default=PROBE_DEFAULT)
    p.add_argument("--requests", type=int, default=10,
                   help="requests for AS-5b (default 10)")
    p.add_argument("--deadline-minutes", type=float, default=45.0)
    p.add_argument("--health-wait-seconds", type=float, default=120.0,
                   help="how long to wait for RIPPLE to see a host healthy "
                        "again after a transient 503 no_healthy_host "
                        "(default 120)")
    p.add_argument("--api-key-file", default=str(Path.home() / ".omlx" / "api_key.txt"))
    p.add_argument("--no-mail", action="store_true")
    p.add_argument("--verbose", action="store_true",
                   help="echo the log to stdout (interactive use)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"configuration unusable: {exc}")
        return EXIT_PREFLIGHT

    log_dir = Path(args.log_dir)
    log = Logger(log_dir / f"live-{datetime.now():%Y-%m-%dT%H-%M-%S}.log",
                 echo=args.verbose)
    log(f"as5_live.py {' '.join(argv if argv is not None else sys.argv[1:])}")

    key = api_key_from_file(args.api_key_file)

    opts = Options(
        ripple=args.ripple or f"http://127.0.0.1:{cfg.listen_port}",
        heavy=args.heavy_model,
        probe=args.probe_model,
        log_dir=log_dir,
        report_dir=Path(args.report_dir),
        deadline_s=max(0.0, args.deadline_minutes * 60.0),
        health_wait_s=max(0.0, args.health_wait_seconds),
        mail=not args.no_mail,
        requests=max(1, args.requests),
        verbose=args.verbose,
        api_key=key,
    )
    runner = Runner(cfg, opts, log=log)
    try:
        if args.verify_only:
            return runner.watchdog()
        if args.check or args.dry_run:
            runner.pick_hosts()
            runner.preflight()
            runner.print_checks()
            if not runner.preflight_ok:
                return EXIT_PREFLIGHT
            if args.dry_run:
                runner.print_plan()
            return EXIT_OK
        return runner.execute()
    except ScenarioAbort as exc:
        log(f"aborted: {exc}")
        print(f"aborted (exit {exc.code}): {exc}")
        return exc.code
    except Exception:                                       # noqa: BLE001
        log("fatal:\n" + traceback.format_exc())
        print("fatal error — see the log: " + str(log.path))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
