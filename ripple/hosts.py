"""Host resolution and live state (FA-21, layer B of §5.1).

A host is only considered resolved once its **identity** is confirmed: the
`node_id` from `GET /api/cluster/node_id` must match the configuration.
Reachability alone is not enough — addresses are not an identity marker (RB-9).
Slot counting and affinity hang off the `name`, never off the address.

Two things decide whether a host may carry traffic, and they are not the same
question:

* `in_pool` — the one placement predicate. Only `down` leaves the pool.
* `healthy` — the verdict of the *last* probe, kept because the operator asks for
  it, not because placement should act on a single sample.

A probe failure is therefore not automatically a verdict. A failure that proves
the node is gone (a refused connect, an unusable protocol, an identity mismatch)
drops the host at once; an ambiguous one (a read timeout, an answered but
unhealthy body) only counts toward `placement.flap_guard_failures`. The incident
of 14.11.2026 was a *read timeout* inside a 111 GB cold load: one such probe used
to make the host unusable for the request that came right after the load.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx

from .backends import (Backend, Capacity, Health, ModelInfo, OmlxBackend,
                       OpenAICompatibleBackend, probe_capabilities)
from .calibration import Calibration, estimate, load as load_calibration
from .config import Config, HostConfig, PlacementConfig
from .model_policy import ModelPolicy
from .stats import (BUCKET_SECONDS, SERIES_BUCKETS, SERIES_WINDOW_SECONDS, Bucket,
                    Stats, bucket_index, series_entry, series_points)

log = logging.getLogger("ripple.hosts")

# The two phases a request is in (issue #18): between dispatch and the first
# token it is prefilling (`pp`), from the first token on it is generating
# (`tg`). `None` means nothing runs. The names are the ones `/healthz` carries
# and the ones the dashboard's CSS hangs its two animations on.
PHASE_PP = "pp"
PHASE_TG = "tg"

# Placeholder load rate: the worst slope measured on the AS-5 night run,
# 0.311 s per GB (docs/loading-state.md §3). It is the same for every host,
# which is wrong for a node whose storage path differs — the calibration engine
# (wave W3) replaces it with a per-node curve.
DEFAULT_LOAD_SECONDS_PER_GB = 0.311
# Too long is harmless (probes stay off a little longer), too short is not: the
# floor keeps a tiny model from getting an ETA that a real load always beats.
MIN_LOAD_ESTIMATE_SECONDS = 10.0

# How many clean buckets a concurrency level needs before it is a point of
# `rate_h(n)` (issue #80). Two buckets are a coincidence, one is an anecdote;
# the optimizer may only divide by a number that was measured more than once.
CURVE_MIN_BUCKETS = 3

# Exception names that prove the node is gone. `Backend.health()` answers an
# exception as `Health(healthy=False, error="<ExceptionName>: …")`, so the class
# name in that string is the only trace of what happened — backends.py belongs to
# another wave. Everything not listed here is soft by default: a failure we do not
# recognise is not evidence that the machine is gone.
_HARD_PROBE_FAILURES = frozenset({
    "ConnectError",          # connection refused / unreachable
    "ConnectTimeout",        # TCP handshake never completed
    "UnsupportedProtocol",   # the address is not an HTTP endpoint at all
    "OSError",               # socket-level failure below httpx
})


def _hard_failure(health: Health) -> bool:
    """Does this failed probe prove the node is gone (drop it now)?"""
    name = (health.error or "").split(":", 1)[0].strip()
    return name in _HARD_PROBE_FAILURES


def _model_list_error(exc: Exception) -> str:
    """Why a model list could not be read, short enough for a 503 message.

    `str(HTTPStatusError)` carries the whole request URL; the status and its
    reason phrase are the part an operator can act on — "401 Unauthorized" says
    the key is wrong, the URL only says which node.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        phrase = exc.response.reason_phrase or ""
        return f"{exc.response.status_code} {phrase}".strip()
    return f"{type(exc).__name__}: {exc}"


def epoch_seconds(value: object, now: float, horizon: float) -> float | None:
    """oMLX's `loading_started_at` as a usable epoch, or None.

    Measured live on 15.11.2026 on beta: oMLX reports a **monotonic clock
    value** there (158021.3 s after boot), not a Unix timestamp, and the value
    does not change while a load runs. Read as epoch it is ~57 years in the past,
    which clamps every ETA to the floor and would make a calibration store a
    sample of 1.8 billion seconds.

    So the field is used only when it looks like an epoch — within `horizon`
    seconds of now. Otherwise the caller falls back to its own clock, which is
    the honest thing: our observation is at most one probe interval late, and the
    ceiling covers the rest.
    """
    try:
        candidate = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    age = now - candidate
    if age < -60.0 or age > max(60.0, float(horizon)):
        return None
    return candidate


def _local_source_ip(host: str, port: int) -> str | None:
    """Which local address is used to reach `host`? Plain UDP connect, no
    packet is sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect((host, port))
            return s.getsockname()[0]
    except OSError:
        return None


def _same_24(a: str, b: str) -> bool:
    return a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


def _directly_connected(address: str) -> bool:
    """Is the target address on one of our own interfaces? Heuristic via the
    source address: same /24 as the address we use to reach it."""
    parsed = urlparse(address)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        socket.inet_aton(host)
    except OSError:
        try:
            host = socket.gethostbyname(host)
        except OSError:
            return False
    src = _local_source_ip(host, port)
    if not src:
        return False
    return _same_24(src, host)


def ordered_addresses(addresses: Sequence[str]) -> list[str]:
    """The probing order (FA-21 rule 1): on-link addresses first, config order kept.

    One definition, because two orders would mean two answers: the proxy resolves
    the address it will use through this, and `ripple doctor` reports the site of
    that same address (issue #67).
    """
    direct = [a for a in addresses if _directly_connected(a)]
    rest = [a for a in addresses if a not in direct]
    return direct + rest


@dataclass
class SeriesRing:
    """The tachograph's memory: 5 s buckets per (host, model), 720 of them.

    In memory only — a restart clears it, and that is the contract, not an
    oversight: the chart is a view of the recent past, the ledger keeps what
    matters for billing (issue #83). Oldest falls out; nothing grows.

    `Bucket` (in `stats.py`) does the averaging inside one bucket, so two
    samples 2 s apart are a mean and not "whatever came last". A bucket that
    never got a sample is simply absent here and becomes `null` in the digest —
    the gap the renderer breaks the line at.
    """

    bucket_s: int = BUCKET_SECONDS
    buckets: int = SERIES_BUCKETS
    _by_model: dict[str, deque[Bucket]] = field(default_factory=dict, repr=False)

    def add(self, model: str, n: float, rate: float | None, *,
            now: float | None = None) -> None:
        """Fold one sample into the bucket `now` falls in.

        `rate=None` is a sample that measured no rate (nothing ran); `Bucket.add`
        keeps it out of the rate mean instead of reading it as 0 tok/s.
        """
        moment = time.time() if now is None else float(now)
        index = bucket_index(moment, self.bucket_s)
        ring = self._by_model.get(model)
        if ring is None:
            ring = deque(maxlen=self.buckets)
            self._by_model[model] = ring
        last = ring[-1] if ring else None
        if last is not None and last.index == index:
            last.add(n, rate)
            return
        if last is not None and index < last.index:
            # The wall clock stepped back (an NTP correction). Folding the sample
            # into a bucket that is already closed would put a later measurement
            # before an earlier one; the sample is dropped and said out loud once.
            log.debug("series_clock_back host_model=%s index=%d last=%d",
                      model, index, last.index)
            return
        ring.append(Bucket(index=index).add(n, rate))

    def digest(self, window_s: float = SERIES_WINDOW_SECONDS, *,
               now: float | None = None) -> list[dict]:
        """The served window, oldest first — one entry per model with samples.

        Only the window, never the ring: 180 points by default against 720 held,
        because `/healthz` is polled every 2 s and the payload has to stay small.
        A model with no bucket inside the window gets **no entry at all** — an
        all-`null` line is noise, not information. Buckets older than the ring
        are pruned here as well, so the dict cannot keep a model that stopped
        running for ever.
        """
        moment = time.time() if now is None else float(now)
        points = min(series_points(window_s, self.bucket_s), self.buckets)
        end = bucket_index(moment, self.bucket_s)
        start = end - points + 1
        ring_start = end - self.buckets + 1
        entries: list[dict] = []
        for model in sorted(self._by_model):
            ring = self._by_model[model]
            while ring and ring[0].index < ring_start:
                ring.popleft()
            if not ring:
                del self._by_model[model]
                continue
            if ring[-1].index < start:
                continue
            filled = {bucket.index: bucket for bucket in ring if bucket.index >= start}
            counts: list = []
            rates: list = []
            for index in range(start, end + 1):
                bucket = filled.get(index)
                counts.append(None if bucket is None else bucket.n)
                rates.append(None if bucket is None else bucket.rate)
            entries.append(series_entry(model, counts, rates, bucket_s=self.bucket_s,
                                        window_s=points * self.bucket_s))
        return entries

    def models(self) -> list[str]:
        """The models the ring currently holds a bucket for (oldest name first)."""
        return sorted(self._by_model)

    def points(self, window_s: float = SERIES_WINDOW_SECONDS, *,
               now: float | None = None) -> list[tuple[str, float, float]]:
        """Every bucket inside the window that measured a rate: `(model, n, rate)`.

        The read side of the ring for everything that is **not** the chart: the
        concurrency curve (issue #80) and the relative load are computed from
        these, so neither has to know how the ring is stored. A bucket without a
        rate is not in here — `n` alone says nothing about `rate_h(n)`, and a
        made-up zero would be a point in its own right.

        Read-only on purpose: the pruning that `digest()` does belongs to the
        payload path, and a measurement query must not shrink the ring.
        """
        moment = time.time() if now is None else float(now)
        count = min(series_points(window_s, self.bucket_s), self.buckets)
        end = bucket_index(moment, self.bucket_s)
        start = end - count + 1
        rows: list[tuple[str, float, float]] = []
        for model in sorted(self._by_model):
            for bucket in self._by_model[model]:
                if bucket.index < start or bucket.index > end:
                    continue
                rate = bucket.rate
                if rate is None:
                    continue
                rows.append((model, bucket.n, rate))
        return rows


@dataclass
class HostRuntime:
    cfg: HostConfig
    client: httpx.AsyncClient
    backend: Backend | None = None
    resolved_address: str | None = None
    resolved_at: float | None = None
    healthy: bool = False
    status: str = "unknown"          # unknown | healthy | loading | degraded | down
    last_seen: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    node_id_seen: str | None = None
    version: str | None = None
    ceiling_bytes: int | None = None
    used_bytes: int | None = None
    # Consecutive answers that said 0 bytes while `used_bytes` holds a non-zero
    # value. See `_apply_used_bytes()`.
    used_bytes_zero_ticks: int = 0
    guard_enabled: bool = False
    models: dict[str, ModelInfo] = field(default_factory=dict)
    # When `models` was last read from the node, and why the last attempt failed
    # (issue #53). An empty `models` alone cannot tell "the node does not carry
    # the model" from "the node never told us": the first is a permanent 404 for
    # the client, the second an operator problem that has to read as a
    # retryable 503. See `model_list_unreadable`.
    models_read_at: float | None = None
    models_error: str | None = None
    # The next healthy verdict owes a re-read of everything the node says —
    # version, model list, capabilities, memory (issue #86). Set on the
    # transition, cleared by `_relearn()`; never set by a steady state, so a node
    # that stays healthy is not re-read on every poll.
    relearn_due: bool = True
    rtt_ms: float | None = None
    inflight: int = 0
    requests_total: int = 0
    errors_total: int = 0
    http_507_total: int = 0
    capability_notes: list[str] = field(default_factory=list)
    # The capability set this node reported the last time it was asked, or None
    # while it has never answered. The comparison for a change runs against this
    # and not against `self.capabilities`: `resolve()` swaps the backend object,
    # so the set the old one held is gone by the time the new one answered
    # (issue #88).
    capabilities_seen: frozenset[str] | None = None
    # The fleet this node is a member of, if any. `Fleet.capabilities` is the
    # intersection over all nodes, and a node that re-reads its own set is the
    # only one that knows it changed — so it tells the fleet instead of the
    # fleet asking every node on every poll (issue #88). None keeps a bare host
    # constructible in a test and outside a fleet.
    fleet: "Fleet | None" = None
    metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    addr_switches: int = 0
    identity_ok: bool = True
    # The two knobs the state machine needs from the config (ceiling, flap
    # guard). A default instance keeps a host constructible in a test without a
    # Fleet.
    placement: PlacementConfig = field(default_factory=PlacementConfig)
    # The fleet's counters (`probes_skipped`). Optional for the same reason.
    stats: Stats | None = None
    # This node's measured load curve (docs/loading-state.md §6.5). None keeps the
    # placeholder slope, which is what a bare host in a test wants.
    calibration: Calibration | None = None
    # The load this host is running, if RIPPLE caused it:
    # {"model", "size_gb", "started_at", "eta_seconds", "source"}.
    loading: dict | None = None
    # Operator-declared maintenance window (`ripple/maintenance.py`), or None.
    # Not a health verdict: `status` keeps saying what the probes saw, and only
    # `in_pool` and the reporting `state` change.
    maintenance: dict | None = None
    # The fleet's model policy (`ripple/model_policy.py`). The same store object
    # is shared by every host, so a change through the admin endpoints is in
    # force on the next request without an apply step. None keeps a bare host in
    # a test constructible — and then nothing is switched off.
    policy: ModelPolicy | None = None
    # Requests in flight per model, split into the phase they are in (issue #18).
    # `{"pp": n, "tg": n}` — counts, not flags: two conversations can prefill the
    # same model at once, and the phase is over only when the last one left it.
    # An entry disappears when both counts are 0, so this never grows with every
    # model the node has ever served.
    phases: dict[str, dict[str, int]] = field(default_factory=dict)
    # How often a model's phase changed on this node (idle→pp, pp→tg, tg→idle).
    # Cheap (one comparison per transition) and the only way to see from outside
    # whether the dots are actually moving or the state got stuck.
    phase_switches: int = 0
    # A removal (or a disable) that stopped new traffic and is waiting for
    # `inflight` to reach 0 (issue #70). Not a health state and not a maintenance
    # window: the node is leaving, and the countdown is the operator's to watch.
    # While it is set the host is out of the pool, so placement already sends
    # elsewhere — that is what makes the wait finite.
    draining: bool = False
    drain_started_at: float | None = None
    drain_deadline: float | None = None
    # The tachograph (issue #83): 5 s buckets of `(n, rate)` per model, one hour
    # deep, in memory only. Filled by `_sample_series()` on every refresh tick —
    # no second loop and no extra request to the node, because both numbers are
    # already in RIPPLE's own bookkeeping.
    series_ring: SeriesRing = field(default_factory=SeriesRing)

    # -- placement predicate ------------------------------------------------
    @property
    def in_pool(self) -> bool:
        """May placement hand this host a request? (contract §3.2)

        `down` is the only *health* state that says no. `degraded` means "the last
        probe failed, but not often enough to give up on the host", and `loading`
        is not a health verdict at all — a loading host keeps its last known health
        and stays in the pool by design. `unknown` is a host that was never
        resolved and has no backend to talk to.

        A maintenance window says no for a different reason: the operator took the
        node out (an update, a reboot). Running requests are unaffected; a bound
        thread falls back to the other host through the FA-11 path, because a host
        outside the pool is exactly what that path is for.

        A draining host (issue #70) says no for the same reason and with the same
        consequence — that is the whole mechanism of a drain: new work goes
        elsewhere while the running requests finish, so `inflight` can reach 0.
        A disabled host says no because the operator turned it off, whether or
        not it is still in the fleet list.
        """
        return (self.maintenance is None and not self.draining and self.cfg.enabled
                and self.status in ("healthy", "loading", "degraded"))

    # -- capabilities ------------------------------------------------------
    @property
    def capabilities(self) -> frozenset[str]:
        return self.backend.capabilities if self.backend else frozenset()

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    # -- address order (FA-21 rule 1) --------------------------------------
    def ordered_addresses(self) -> list[str]:
        return ordered_addresses(self.cfg.addresses)

    def _make_backend(self, address: str) -> Backend:
        if self.cfg.backend == "openai_compatible":
            return OpenAICompatibleBackend(address, self.cfg.api_key, client=self.client)
        return OmlxBackend(address, self.cfg.api_key, client=self.client)

    # -- resolution --------------------------------------------------------
    async def resolve(self) -> bool:
        """Walk the address list in order. The first address with a confirmed
        identity wins."""
        # A host that had to be re-resolved is not loading: whatever was running
        # on the old address is not running on the new one (contract §4). Logged
        # like any other end, because a vanished state in the log is a mystery.
        self.end_loading("resolved")
        old = self.resolved_address
        for idx, address in enumerate(self.ordered_addresses()):
            backend = self._make_backend(address)
            try:
                if self.cfg.backend == "omlx":
                    nid = await backend.identity()
                    if self.cfg.node_id and nid and nid != self.cfg.node_id:
                        self.identity_ok = False
                        log.error("identity_mismatch host=%s addr=%s expected=%s seen=%s",
                                  self.cfg.name, address, self.cfg.node_id, nid)
                        self.last_error = (f"identity_mismatch: {address} reports {nid}, "
                                           f"expected {self.cfg.node_id}")
                        continue
                    if self.cfg.node_id and nid is None:
                        self.last_error = f"{address}: node_id unreadable"
                        continue
                    if nid:
                        self.node_id_seen = nid
                        self.identity_ok = True
                health = await backend.health()
                if not health.healthy:
                    self.last_error = f"{address}: {health.error or 'not healthy'}"
                    continue
            except (httpx.HTTPError, ValueError, OSError) as exc:
                self.last_error = f"{address}: {type(exc).__name__}: {exc}"
                continue

            self.backend = backend
            self.resolved_address = address
            self.resolved_at = time.time()
            self._apply_health(health)
            # The single reader for the version, the model list, the capabilities
            # and the memory (issue #86). `resolve()` is the rare path; the other
            # one is the transition into a healthy verdict (`_apply_health`).
            await self._relearn(health)
            if old and old != address:
                self.addr_switches += 1
                log.warning("addr_switch host=%s old=%s new=%s", self.cfg.name, old, address)
            else:
                log.info("resolved host=%s addr=%s node_id=%s index=%d",
                         self.cfg.name, address, self.node_id_seen, idx)
            return True

        log.error("unresolved host=%s last_error=%s", self.cfg.name, self.last_error)
        self.status = "down"
        self.healthy = False
        return False

    async def reinit(self) -> bool:
        """Deliberate switch: drop the resolution and probe again (FA-21 rule 4).
        Slot counting and bindings hang off the name and survive."""
        self.resolved_address = None
        self.backend = None
        return await self.resolve()

    # -- health / discovery ------------------------------------------------
    def _apply_used_bytes(self, used: int | None) -> None:
        """Fold a memory answer into `used_bytes` without letting one 0 erase it.

        oMLX answers `current_model_memory: 0` for a tick right after a load
        finishes — measured on the second cold-load test (15.11.2026): `/healthz`
        said `used=0.0 GB` for the M3 right after a 453,6 GB load, and the correct
        111,6 GB a minute later. That 0 means "no answer yet", not "the node is
        empty", and writing it empties the dashboard's memory bar for a tick.

        So one 0 against a known non-zero value is *held*; the second one in a
        row is taken as the truth, because a node that keeps answering 0 really
        is empty (a model unloaded in the oMLX UI). A node that never reported a
        non-zero value has nothing to hold — its honest 0/None stays.

        `end_loading()` restarts the count: the answers before and after a load
        are separated by a gap (the probes are suspended), so they must not add
        up to "the node insists on 0". That gap is exactly where the incident
        of the second cold-load test sat.

        `None` is a failed probe, not an answer: it stays unknown and `refresh()`
        re-asks the capacity endpoint for it.
        """
        if used is None:
            self.used_bytes = None
            self.used_bytes_zero_ticks = 0
            return
        if used == 0 and self.used_bytes:
            self.used_bytes_zero_ticks += 1
            if self.used_bytes_zero_ticks < 2:
                log.debug("memory_zero host=%s held=%d tick=%d", self.cfg.name,
                          self.used_bytes, self.used_bytes_zero_ticks)
                return
        self.used_bytes = used
        self.used_bytes_zero_ticks = 0

    def _apply_health(self, health: Health) -> None:
        """Fold one probe result into the host state.

        A failure is not one thing. A *hard* one (refused connect, unusable
        protocol, socket failure) proves the node is gone and drops it at once —
        AS-5 depends on that: a stopped node must lose its traffic on the first
        probe. Everything else is ambiguous — a read timeout, a read error, an
        answered but unhealthy body — and only counts toward
        `placement.flap_guard_failures`. Until that count is reached the host
        reports `degraded` and keeps serving.

        The *transition* into a healthy verdict marks `relearn_due`: a node that
        was updated and restarted is back within seconds, too short for the flap
        guard to ever call it `down`, so this is the only moment its version,
        model list, capabilities and memory are read again (issue #86).
        """
        was_healthy = self.healthy
        self.healthy = health.healthy
        self.rtt_ms = health.rtt_ms
        self.ceiling_bytes = health.ceiling_bytes
        # One 0 after a load is "no answer yet", not "the node is empty"
        # (`_apply_used_bytes`).
        self._apply_used_bytes(health.used_bytes)
        # guard_enabled: if the memory guard is off, the eviction forecast is
        # meaningless — the real risk is then a jetsam kill.
        self.guard_enabled = bool(health.ceiling_bytes and health.ceiling_bytes > 0)
        if health.healthy:
            self.last_seen = time.time()
            self.last_error = None
            self.consecutive_failures = 0
            if not was_healthy:
                # `down`/`loading`/`degraded` → healthy: everything the node can
                # tell us is re-read, once (`_relearn`).
                self.relearn_due = True
            # `healthy` is the verdict of the last probe; `loading` is reported on
            # top of it for the operator and never replaces the verdict.
            self.status = "loading" if self.loading is not None else "healthy"
            return
        self.consecutive_failures += 1
        if health.error:
            self.last_error = health.error
        limit = max(1, self.placement.flap_guard_failures)
        if _hard_failure(health) or self.consecutive_failures >= limit:
            self.status = "down"
        else:
            self.status = "degraded"

    async def refresh(self) -> None:
        """One probe tick — and one tachograph sample taken from its numbers.

        The sample rides on this tick on purpose (issue #83): `n` and `rate` are
        RIPPLE's own counters (`phases`, `metrics`), already in memory, so the
        series costs the node no request and needs no sampling loop of its own.
        It is taken in `finally` so that a tick that ends early — a host in a
        cold load, an unreachable one — still contributes what it knows.
        """
        try:
            await self._refresh_once()
        finally:
            self._sample_series()

    async def _refresh_once(self) -> None:
        if self.backend is None:
            if not await self.resolve():
                return
        assert self.backend is not None
        if self.loading is not None:
            # Probe suppression (contract §4). `/health` is exactly the endpoint a
            # loading engine answers late, so probing it here would turn the load
            # into the health verdict — the defect this state exists to prevent.
            # The counter keeps a suspended host from hiding behind silence.
            # `ceiling_bytes`/`used_bytes` keep their last known value: a
            # suspended probe is not an answer, and a 0 written here would say
            # "empty node" about a node that is loading 453,6 GB.
            if self.stats is not None:
                self.stats.probes_skipped += 1
            await self._poll_loading()
            return
        try:
            health = await self.backend.health()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            health = Health(healthy=False, error=f"{type(exc).__name__}: {exc}")
        self._apply_health(health)

        if not health.healthy:
            if self.status == "down":
                # FA-21 rule 3: re-resolve only when needed — on a hard failure at
                # once, on a soft one only after the flap guard said `down`.
                log.warning("unhealthy host=%s failures=%d → re-probing address list",
                            self.cfg.name, self.consecutive_failures)
                await self.resolve()
            return

        if self.relearn_due:
            # The node answered healthy again after being `down`, `degraded` or
            # `loading`: read everything it can tell us once — the version it
            # runs, the model list with `loaded` and size, the capabilities, the
            # memory (issue #86). Not on every poll: these change rarely.
            residency = await self._relearn(health)
        else:
            residency = await self._read_models()
        if residency is not None:
            # §5 signal 2: a load that oMLX started without us (the UI, a client
            # talking to :8000 directly). Without it the state would only ever
            # cover loads RIPPLE caused itself.
            self._note_external_load(residency)
        if self.ceiling_bytes is None or self.used_bytes is None:
            try:
                cap: Capacity | None = await self.backend.capacity()
            except (httpx.HTTPError, ValueError, OSError):
                cap = None
            if cap:
                self.ceiling_bytes = cap.ceiling_bytes
                self._apply_used_bytes(cap.used_bytes)
        if not self.guard_enabled and self.loading is None:
            # Reported, not fatal: without a ceiling the eviction forecast is off,
            # which is a warning for the operator and not a pool decision. The
            # `loading is None` guard matters: an external load discovered in this
            # same tick has already set `status = "loading"`, and overwriting it
            # here would hide the state the dashboard is meant to show.
            self.status = "degraded"

    def _note_model_list(self, error: str | None) -> None:
        """Record the outcome of the last model-list attempt (issue #53).

        Logged on the transition only: a node that lost its admin key would
        otherwise repeat the same warning on every probe interval, and the
        moment it starts or stops working is the part the operator needs.
        """
        if error is None:
            if self.models_error is not None:
                log.info("models_read host=%s models=%d after=%s",
                         self.cfg.name, len(self.models), self.models_error)
            self.models_error = None
            self.models_read_at = time.time()
            return
        if self.models_error != error:
            log.warning("models_unreadable host=%s cause=%s", self.cfg.name, error)
        self.models_error = error

    async def _read_models(self) -> dict[str, ModelInfo] | None:
        """Read the model list — with `loaded` and the size of each model — and
        fold it in. Returns the residency answer when the node gave one.

        Two sources, because they say different things: `/v1/models/status` is the
        only one carrying `loaded`, `actual_size` and `is_loading` (what placement
        and the eviction guard need), `/v1/models` the fallback for a node that
        does not serve it. The return value is the residency dict, not `models`:
        `refresh()` uses it for the external-load signal (§5 signal 2) and must not
        read the list a second time.
        """
        assert self.backend is not None
        try:
            residency = await self.backend.residency()
        except (httpx.HTTPError, ValueError, OSError):
            residency = None
        if residency is not None:
            self.models = residency
            self._note_model_list(None)
            return residency
        try:
            listed = await self.backend.models()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            # The node did not say what it carries — a 401 from a missing admin
            # key, a timeout, a broken body. `models` keeps whatever it had, so
            # its content is not evidence of absence: recorded here so placement
            # can answer 503 instead of 404 (issue #53).
            self._note_model_list(_model_list_error(exc))
            return None
        if listed:
            self.models = {m.id: m for m in listed}
        self._note_model_list(None)
        return None

    async def _read_capabilities(self, health: Health | None = None) -> None:
        """Ask the node what it can do and keep the notes (FA-23).

        `health` is the probe answer the caller already holds — see
        `probe_capabilities()`.

        A set that differs from the last one this node reported is news, and it
        is the node that has to say so: `Fleet.capabilities` is an intersection
        over all nodes, and nothing in the fleet knows about the change until
        the node that re-read its own set speaks up (`_note_capabilities`).
        """
        if self.backend is None:
            return
        caps, notes, _models = await probe_capabilities(self.backend, health=health)
        self.backend.capabilities = caps  # type: ignore[misc]
        self.capability_notes = notes
        for note in notes:
            log.warning("capability host=%s %s", self.cfg.name, note)
        self._note_capabilities(caps)

    def _note_capabilities(self, caps: frozenset[str]) -> None:
        """One line per change, and the fleet recomputes its intersection (#88).

        `Fleet.capabilities` is the intersection every routing decision is
        checked against, and it used to be computed in `start()` and `add()` only.
        A node that lost or gained a capability on an update — the case that
        matters is `residency`, because #80's eviction builds on it — therefore
        left the fleet on the old set until RIPPLE restarted.

        The fleet is told from here, on the change and not on the poll: the node
        has just answered, so this costs no request and no extra tick, and a node
        that stays as it is says nothing. The first read is not a change — there
        is nothing to compare it against — and a re-resolve that finds the same
        set stays quiet for the same reason.
        """
        before = self.capabilities_seen
        after = frozenset(caps)
        self.capabilities_seen = after
        if before is None or before == after:
            return
        changed = " ".join(f"{c}={'on' if c in after else 'off'}"
                           for c in sorted(before ^ after))
        log.warning("capabilities changed host=%s %s", self.cfg.name, changed)
        if self.fleet is not None:
            self.fleet._recompute_capabilities()

    def _note_version(self, version: str | None) -> None:
        """Keep the version the node reports, and say so when it changes (#86).

        The version is the one field that says which engine a measurement was
        taken against (`docs/measurements.md`), so a change is news: one line,
        both values, once. A node that stays on the new version must not repeat
        it on every interval, and an unreadable version is not a change — it
        keeps the last known value.
        """
        if not version or version == self.version:
            return
        old = self.version
        self.version = version
        if old is None:
            log.info("version host=%s old=- new=%s", self.cfg.name, version)
        else:
            log.warning("version host=%s old=%s new=%s", self.cfg.name, old, version)

    async def _relearn(self, health: Health | None = None) -> dict[str, ModelInfo] | None:
        """Re-read everything the node can tell us — on the transition, not the poll.

        A node that was updated and restarted is `healthy` again within seconds:
        too short for the flap guard to ever declare it `down`, so `resolve()` —
        the only place that used to read the version — never ran again, and
        `/healthz` and the fleet card kept reporting the old engine (issue #86).
        The same shape of staleness applied to the capability set, and one step
        softer to the model list.

        So the *transition* into a healthy verdict re-reads all of it, and so does
        `resolve()`, so the two paths agree. Not on every poll: that would be a
        handful of requests per node per interval for data that changes when an
        operator updates a machine, which is what `relearn_due` exists to avoid.

        A failing read here is not a verdict on the node — the probe just said it
        is healthy. The old value stays, the reason is logged, and the next
        transition tries again.

        `health` is this tick's probe answer. The ceiling and the memory in use
        come from it (`_apply_health` has just folded them in), so this costs no
        second `/health` request.
        """
        self.relearn_due = False
        backend = self.backend
        if backend is None:
            return None
        if isinstance(backend, OmlxBackend):
            try:
                version = await backend.version()
            except (httpx.HTTPError, ValueError, OSError) as exc:
                log.warning("version_unreadable host=%s cause=%s: %s",
                            self.cfg.name, type(exc).__name__, exc)
                version = None
            self._note_version(version)
        residency = await self._read_models()
        await self._read_capabilities(health)
        return residency

    # -- loading state (docs/loading-state.md §4) --------------------------
    def begin_loading(self, model: str, *, size_gb: float | None,
                      eta_seconds: float, source: str,
                      started_at: float | None = None,
                      seen_loading: bool = False) -> None:
        """Record that this host is loading `model`.

        The newer load is the truth, so an existing entry is overwritten. The
        state changes neither `healthy` nor the pool membership: setting
        `healthy = False` here would reproduce the original defect with a
        different label.

        `started_at` is oMLX's own timestamp when the load was not started by
        RIPPLE (signal 2 of §5); the ceiling then counts from the real start
        instead of from our first observation of it.
        """
        if not self.placement.loading.enabled:
            return
        self.loading = {"model": model, "size_gb": size_gb,
                        "started_at": float(started_at) if started_at is not None
                        else time.time(),
                        "eta_seconds": float(eta_seconds), "source": source,
                        # True only when the node itself reported this load as
                        # running (signal 2). A load we dispatched is unobserved
                        # until oMLX lists it, and `is_loading: false` before that
                        # means "not started", not "finished".
                        "seen_loading": bool(seen_loading),
                        # One tick of "not loading, not loaded" is not a verdict
                        # yet (`_poll_loading`).
                        "idle_unloaded": False}
        if self.status != "down":
            self.status = "loading"
        log.info("loading_start host=%s model=%s size_gb=%s eta=%.0fs source=%s",
                 self.cfg.name, model, size_gb, eta_seconds, source)

    def end_loading(self, reason: str) -> None:
        """Leave the loading state; idempotent.

        Three signals end a load and the first one wins — oMLX says it is done,
        the request that caused it returned, or the ceiling expired. The others
        are silent no-ops, which is why this must not log or count twice.
        """
        entry = self.loading
        if entry is None:
            return
        self.loading = None
        # A load is a gap in the memory answers: whatever the node said about
        # `used_bytes` before it is not evidence about the node after it, so the
        # tolerance of `_apply_used_bytes()` starts again with this load's end.
        self.used_bytes_zero_ticks = 0
        limit = max(1, self.placement.flap_guard_failures)
        # A host that was `down` when the load began stays `down`: the load was
        # never a health verdict, and ending it is not evidence either.
        if self.status != "down":
            if self.healthy:
                self.status = "healthy"
                # `loading` → `healthy` is a transition too, and the load changed
                # the model list: the next probe re-reads what the node says
                # (issue #86), instead of trusting the list from before the load.
                self.relearn_due = True
            elif self.consecutive_failures >= limit:
                self.status = "down"
            else:
                self.status = "degraded"
        started = float(entry.get("started_at") or time.time())
        log.info("loading_end host=%s model=%s reason=%s seconds=%.1f",
                 self.cfg.name, entry.get("model"), reason,
                 max(0.0, time.time() - started))

    def loading_expired(self, now: float | None = None) -> bool:
        """Has the load outlived `placement.loading.max_seconds`?

        The ceiling is the only protection against a hung load suspending that
        host's probes for ever.
        """
        entry = self.loading
        if entry is None:
            return False
        now = time.time() if now is None else now
        started = float(entry.get("started_at") or now)
        return now - started >= self.placement.loading.max_seconds

    def _note_load_failed(self, entry: dict, reason: str) -> None:
        """A supervised load ended without the model being resident (§12.1).

        `end_loading()` cannot tell a load that produced a model from one that
        did not — both clear the state, and `/stats` used to report every one of
        them as successful. Counted here, once per load: the caller ends the state
        right after, so no second poll can see the entry.
        """
        if self.stats is not None:
            self.stats.loads_failed += 1
        started = float(entry.get("started_at") or time.time())
        log.warning("loading_failed host=%s model=%s reason=%s source=%s seconds=%.1f",
                    self.cfg.name, entry.get("model"), reason, entry.get("source"),
                    max(0.0, time.time() - started))

    async def _poll_loading(self) -> None:
        """Ask `/v1/models/status` — and only that — whether the load is over.

        `is_loading is None` means the node does not report the field, which is
        *unknown*, not "done": the state then ends on the request returning or on
        the ceiling. A model that vanished from the node cannot be loading, so
        that ends the state too.

        Two of these ends are not successes. oMLX can stop reporting the load and
        leave the model at `loaded: false`, or drop the model from the list
        altogether — a load that returned without a resident model. Both are
        counted (`stats.loads_failed`) and logged once, because "the load is
        over" and "the model is there" are different statements.
        """
        entry = self.loading
        if entry is None or self.backend is None:
            return
        try:
            residency = await self.backend.residency()
        except (httpx.HTTPError, ValueError, OSError):
            residency = None
        if residency is not None:
            self.models = residency
            info = residency.get(entry["model"])
            if info is None:
                # Not listed at all — so certainly not resident.
                self._note_load_failed(entry, "model_gone")
                self.end_loading("model_gone")
                return
            if info.is_loading is True:
                # The node confirms the load. Only from here on does `false`
                # mean "finished" — before the first `true` it means "oMLX has
                # not started it yet", and ending the state on that would put a
                # probe back inside the load this state exists to protect.
                entry["seen_loading"] = True
                entry["idle_unloaded"] = False
            elif info.is_loading is False and entry.get("seen_loading"):
                if info.loaded:
                    self.end_loading("omlx_idle")
                    return
                if not entry.get("idle_unloaded"):
                    # One tick of "not loading and not loaded" is not a verdict
                    # yet — the same rule `_apply_used_bytes()` applies to a
                    # single 0. The next poll decides, and staying in the state
                    # one tick longer only keeps a probe out of a load.
                    entry["idle_unloaded"] = True
                    log.debug("loading_unconfirmed host=%s model=%s",
                              self.cfg.name, entry.get("model"))
                else:
                    self._note_load_failed(entry, "omlx_idle")
                    self.end_loading("omlx_idle")
                    return
        if self.loading_expired():
            # The ceiling is our own verdict, not the node's: the model may well
            # still be loading, so this ends the state without counting a failure.
            self.end_loading("ceiling")

    def _note_external_load(self, residency: dict[str, ModelInfo]) -> None:
        """Signal 2 of §5: a load RIPPLE did not cause (oMLX UI, a client on :8000).

        Without this the state would only ever cover loads RIPPLE started itself,
        and a load started in the oMLX UI would hit exactly the defect of
        14.11.2026: a probe landing inside it. The ETA is what is left of the
        estimate, counted from oMLX's own start timestamp.
        """
        if not self.placement.loading.enabled:
            return
        running = [(mid, info) for mid, info in residency.items()
                   if info.is_loading is True]
        if not running:
            return
        model, info = max(running, key=lambda item: item[1].estimated_size or 0)
        if self.loading is not None and self.loading.get("model") == model:
            return
        size_gb = round((info.estimated_size or 0) / 1e9, 2) or None
        started = epoch_seconds(info.loading_started_at, time.time(),
                                self.placement.loading.max_seconds)
        elapsed = max(0.0, time.time() - started) if started is not None else 0.0
        budget = self.estimate_load_seconds(size_gb)
        remaining = budget - elapsed
        if remaining < MIN_LOAD_ESTIMATE_SECONDS:
            # The estimate is already exceeded (a queued load, or a node whose
            # storage is slower than the curve says). The only honest upper bound
            # left is the ceiling — reporting the floor would look like "almost
            # done" for minutes.
            remaining = max(MIN_LOAD_ESTIMATE_SECONDS,
                            self.placement.loading.max_seconds - elapsed)
        log.info("loading_external host=%s model=%s size_gb=%s started=%s", self.cfg.name,
                 model, size_gb, "omlx" if started is not None else "our clock")
        self.begin_loading(model, size_gb=size_gb, eta_seconds=remaining,
                           source="omlx", started_at=started,
                           # We are looking at the node while it reports the
                           # load, so `false` from here on means "finished".
                           seen_loading=True)

    def estimate_load_seconds(self, size_gb: float | None) -> float:
        """How long a load of `size_gb` may take on this host, in seconds.

        With a calibration attached this is the node's own measured curve
        (`ripple/calibration.py`), keyed by `node_id` so a renamed host keeps it;
        the safety factor and the clamp come from `placement.loading` and are the
        same either way. Without one — a bare host in a test, or a fleet whose
        store could not be read — the worst slope measured on the AS-5 night run
        (0.311 s/GB, docs/loading-state.md §3) is applied to every host, which is
        wrong for a node whose storage path differs. Either way the estimate may
        be too long (probes stay off a little longer), never too short.
        """
        ld = self.placement.loading
        if self.calibration is not None:
            return estimate(self.calibration, self.cfg.node_id or self.node_id_seen,
                            size_gb, safety_factor=ld.safety_factor,
                            max_seconds=ld.max_seconds)
        seconds = max(0.0, float(size_gb or 0.0)) * DEFAULT_LOAD_SECONDS_PER_GB
        seconds *= ld.safety_factor
        return float(min(max(seconds, MIN_LOAD_ESTIMATE_SECONDS), ld.max_seconds))

    # -- live counters -----------------------------------------------------
    def acquire(self) -> bool:
        if self.inflight >= self.cfg.max_inflight:
            return False
        self.inflight += 1
        return True

    def release(self) -> None:
        self.inflight = max(0, self.inflight - 1)

    # -- drain (issue #70) --------------------------------------------------
    def begin_drain(self, seconds: float) -> None:
        """Leave the pool and start the countdown for the removal.

        `in_pool` turns False here, which is the *only* thing that makes a drain
        terminate: placement stops handing the node work, so `inflight` can only
        fall. `drain_deadline` is wall clock on purpose — `/healthz` shows the
        remaining seconds against `server_time`, so the page can count down
        without asking again.
        """
        now = time.time()
        self.draining = True
        self.drain_started_at = now
        self.drain_deadline = now + max(0.0, float(seconds))
        log.warning("drain_begin host=%s inflight=%d deadline_in=%.0fs",
                    self.cfg.name, self.inflight, max(0.0, float(seconds)))

    def end_drain(self) -> None:
        """The drain is over (the node left, or the operator put it back)."""
        if not self.draining:
            return
        self.draining = False
        self.drain_started_at = None
        self.drain_deadline = None

    @property
    def drain_remaining_seconds(self) -> float | None:
        if self.drain_deadline is None:
            return None
        return round(max(0.0, self.drain_deadline - time.time()), 2)

    def running_models(self) -> list[dict[str, Any]]:
        """What is still in flight, per model — the answer a timed-out drain gives.

        Read from the phase bookkeeping (issue #18), which is per (host, model)
        and updated on the request path: `pp` is a request between dispatch and
        its first token, `tg` one that is streaming. `inflight` alone would only
        say how many; a removal that has to give up must say *what* it is
        waiting for, or the operator cannot tell a stuck request from a busy one.
        """
        rows: list[dict[str, Any]] = []
        for model in sorted(self.phases):
            slot = self.phases[model] or {}
            prefill = int(slot.get(PHASE_PP, 0))
            decode = int(slot.get(PHASE_TG, 0))
            if prefill + decode <= 0:
                continue
            rows.append({"model": model, "inflight": prefill + decode,
                         "prefill": prefill, "decode": decode})
        return rows

    # -- request phase (issue #18) -----------------------------------------
    def model_phase(self, model: str) -> str | None:
        """What this node is doing with `model` right now: `pp`, `tg` or nothing.

        Read from RIPPLE's own bookkeeping of the requests it is running — the
        proxy marks the dispatch, the first token and the end. No probe, no
        extra call to oMLX: a node in a 26 s prefill looks exactly like an idle
        one over the network, which is the whole reason this state exists.
        """
        slot = self.phases.get(model)
        if not slot:
            return None
        if slot.get("pp"):
            return PHASE_PP
        if slot.get("tg"):
            return PHASE_TG
        return None

    @property
    def phase(self) -> str | None:
        """The node's aggregate: prefill if any model prefills, else decode.

        Prefill wins because it is the phase an operator waits on and the one
        that produces no tokens at all — a node that is prefilling one model and
        decoding another is, from the outside, a node that is not answering.
        """
        decode = False
        for model in self.phases:
            phase = self.model_phase(model)
            if phase == PHASE_PP:
                return PHASE_PP
            if phase == PHASE_TG:
                decode = True
        return PHASE_TG if decode else None

    def begin_phase(self, model: str) -> str | None:
        """A request was dispatched: this model is prefilling from now on."""
        before = self.model_phase(model)
        slot = self.phases.setdefault(model, {"pp": 0, "tg": 0})
        slot["pp"] += 1
        self._note_phase(model, before)
        return before

    def note_first_token(self, model: str) -> str | None:
        """The first token went out: this request is generating, not prefilling."""
        before = self.model_phase(model)
        slot = self.phases.setdefault(model, {"pp": 0, "tg": 0})
        if slot["pp"]:
            slot["pp"] -= 1
        slot["tg"] += 1
        self._note_phase(model, before)
        return before

    def end_phase(self, model: str, phase: str) -> str | None:
        """A request that was in `phase` is over (or went away).

        Idempotent in the sense that matters: a counter never goes below 0, and
        an unknown model is a no-op. The entry is dropped when the last request
        leaves, so `phases` holds only what is running.
        """
        before = self.model_phase(model)
        slot = self.phases.get(model)
        if slot is not None:
            key = PHASE_TG if phase == PHASE_TG else PHASE_PP
            if slot[key]:
                slot[key] -= 1
            if not slot["pp"] and not slot["tg"]:
                self.phases.pop(model, None)
        self._note_phase(model, before)
        return before

    def _note_phase(self, model: str, before: str | None) -> None:
        after = self.model_phase(model)
        if after == before:
            return
        self.phase_switches += 1
        log.debug("phase host=%s model=%s %s->%s", self.cfg.name, model,
                  before or "idle", after or "idle")

    def record_tps(self, model: str, completion_tokens: int, seconds: float) -> None:
        """Decode rate, exponentially smoothed.

        One request is a noisy sample — a single long prefill or a burst of
        concurrent streams swings it by a factor of four (measured: the M3
        reported 3.9 tok/s while idle at 43.5). The rebind cost model decides
        with this number, so it must not jump on one request.
        """
        if completion_tokens <= 0 or seconds <= 0:
            return
        self.record_rate(model, completion_tokens / seconds)

    def record_rate(self, model: str, rate: float) -> None:
        """Record a decode rate that was measured elsewhere (oMLX reports one)."""
        if rate <= 0:
            return
        m = self.metrics.setdefault(model, {})
        prev = m.get("tg_tps")
        m["tg_tps"] = round(rate, 1) if prev is None else round(prev + (rate - prev) * 0.3, 1)
        m["tg_samples"] = m.get("tg_samples", 0) + 1
        m["measured_at"] = round(time.time(), 1)

    # -- tachograph (issue #83) ---------------------------------------------
    def _model_streams(self, model: str) -> int:
        """How many of RIPPLE's streams this model is running on this node now.

        Read from the phase bookkeeping (issue #18), the same source the
        dashboard's dots use: `pp` counts requests between dispatch and their
        first token, `tg` those that are streaming. A request is a stream in
        both phases — it is the client waiting either way — so the sum is what
        the tachograph's `n` means.
        """
        slot = self.phases.get(model) or {}
        return int(slot.get(PHASE_PP, 0)) + int(slot.get(PHASE_TG, 0))

    def _sample_series(self, now: float | None = None) -> None:
        """Take one sample per model with a measured rate (issue #83).

        `n` is the stream count above, `rate` the decode rate the model last
        measured (`tg_tps`, already smoothed by `record_rate`). Both come from
        this process, so a sample is free.

        Two rules, and both are about not drawing what we do not know:

        * A model without a measured rate has **no point at all** — there is no
          rate to report, and a made-up one would be worse than a gap.
        * A host whose last probe said `down` samples nothing: the node is gone,
          and a 0 would draw it as idle instead of as absent. The line ends
          there, which is what the `null` is for.

        `n = 0` *is* sampled: a tick with nothing running is a real measurement
        (`total = 0` by the formula), and skipping it would leave the chart
        empty for sporadic traffic, where most buckets hold no stream. But it is
        sampled with `n` alone: `tg_tps` is an EMA that outlives the request that
        fed it, so passing it along at `n = 0` would file the rate of a *finished*
        stream into a bucket in which nothing ran (issue #83: `n=0` beside
        `per_thread=55.5`). The rate goes in only when a stream was running —
        `series_entry` enforces the same rule at the payload, this is where the
        lie would otherwise enter the mean.

        `now` is a test seam; the default is the wall clock.
        """
        if self.status == "down":
            return
        moment = time.time() if now is None else float(now)
        for model in list(self.metrics):
            rate = (self.metrics.get(model) or {}).get("tg_tps")
            if not rate or rate <= 0:
                continue
            streams = self._model_streams(model)
            self.series_ring.add(model, streams, rate if streams > 0 else None,
                                 now=moment)

    def series(self, window_s: float | None = None) -> list[dict]:
        """The tachograph digest for `/healthz` — oldest first, per model.

        The derivation (`total = n · per_thread`) lives in `stats.series_entry`,
        not here: this method only asks the ring for the window.
        """
        return self.series_ring.digest(
            SERIES_WINDOW_SECONDS if window_s is None else window_s)

    # -- concurrency curve and relative load (issue #80) ---------------------
    def curve(self, window_s: float | None = None,
              model: str | None = None) -> dict[float, float]:
        """`rate_h(n)`: the measured per-stream rate at concurrency `n`.

        Grouped from the ring by `n`, `rate` is the mean of the per-thread rates
        of the buckets at that `n`. Only **clean** buckets are measurement
        points:

        * `n >= 1` — a bucket in which nothing ran has no per-stream rate; its
          `n = 0` is a statement about the host being idle, not about `rate_h`.
        * `n == round(n)` — a bucket whose 5 s window held one idle and one busy
          tick has a mean count of, say, 1.5. That is an artefact of the bucket
          width, not a concurrency level the node ever ran at, so it is not a
          point either.

        And a point exists only once at least `CURVE_MIN_BUCKETS` clean buckets
        agree on that `n`: one bucket is an anecdote, and the optimizer must not
        divide by an anecdote. Fewer than that and the level is simply absent
        from the dict — the honest answer is "not measured yet".

        The default window is the one `/healthz` serves (`SERIES_WINDOW_SECONDS`);
        the ring holds an hour, so a caller may ask for more.

        `model=None` is the node's own answer, across every model it ran: one
        curve per host, which is what `/healthz` reports. A node serves models of
        very different speeds (a 400 GB DeepSeek and a 9B Qwen on the same box),
        and their curves are not comparable — a mean over both would describe
        neither. So a caller that decides about *one* model passes it, and gets
        that model's curve alone (issue #80); the level is then simply absent
        until that model has been measured.
        """
        window = SERIES_WINDOW_SECONDS if window_s is None else window_s
        grouped: dict[float, list[float]] = {}
        for row_model, n, rate in self.series_ring.points(window):
            if model is not None and row_model != model:
                continue
            if n < 1 or n != round(n):
                continue
            grouped.setdefault(float(round(n)), []).append(rate)
        return {n: round(sum(rates) / len(rates), 1)
                for n, rates in sorted(grouped.items())
                if len(rates) >= CURVE_MIN_BUCKETS}

    def capability_rate(self, model: str | None = None) -> float | None:
        """The single-stream rate `rate_h(1)` — the denominator of the load.

        `None` when the curve has no measured `n = 1` point yet. A fresh process
        (or a node that has not decoded anything since the restart) answers
        `None` here, and every caller must treat that as "not measurable" rather
        than as zero: there is nothing to divide by. `model` is passed through to
        `curve()`; without it the answer is the node's, across all models.
        """
        return self.curve(model=model).get(1.0)

    def relative_load(self, model: str | None = None) -> float | None:
        """How loaded this host is, as `1 − per_request_rate / capability_rate`.

        `per_request_rate` is the current EMA (`metrics[model]["tg_tps"]`) of the
        model this host **serves** — the one with the most rate-carrying buckets
        in the window (ties broken by name, so the answer is stable). The
        capability rate is the host's own single-stream rate from `curve()[1]`.

        The ratio is `0` at no load and approaches `1` as the stream slows toward
        the rate a single stream gets: 77.4 tok/s alone against 64.7 tok/s now is
        `1 − 64.7 / 77.4 = 0.164`, i.e. about 16 % of the headroom is gone.

        `None` whenever either number is missing — no curve, no measured rate on
        the served model — because there is nothing honest to report then, and
        certainly nothing to divide by.

        With `model`, the ratio is that model's alone: its curve is the
        denominator and its own EMA the numerator, no "served model" guess. The
        optimizer asks this way, because a thread carries a model and a move is
        decided on the curve of *that* model (issue #80).

        The result is **clamped to 0..1**: an EMA can sit briefly above the
        single-stream rate (one fast request, or a bucket that averaged a lull
        into the capability point), and a negative "load" would read as spare
        capacity that does not exist. Over 1 is equally meaningless — the
        formula's ceiling is "no faster than one stream".
        """
        curve = self.curve(model=model)
        capability = curve.get(1.0)
        if not capability or capability <= 0:
            return None
        if model is not None:
            rate = float((self.metrics.get(model) or {}).get("tg_tps") or 0.0)
            if rate <= 0:
                return None
            return round(min(1.0, max(0.0, 1.0 - rate / float(capability))), 4)
        buckets: dict[str, int] = {}
        for row_model, _n, _rate in self.series_ring.points():
            buckets[row_model] = buckets.get(row_model, 0) + 1
        served = sorted(((count, model) for model, count in buckets.items()
                         if (self.metrics.get(model) or {}).get("tg_tps")),
                        key=lambda item: (-item[0], item[1]))
        if not served:
            return None
        rate = float((self.metrics.get(served[0][1]) or {}).get("tg_tps") or 0.0)
        if rate <= 0:
            return None
        return round(min(1.0, max(0.0, 1.0 - rate / float(capability))), 4)

    def model_permitted(self, model: str) -> bool:
        """May this host carry `model` at all, if the node has it?

        The allow-list in the config (a host is only meant to carry what it is
        configured for) and the operator's model policy
        (`ripple/model_policy.py`: `disabled` and `hidden` both say no). Not the
        node's own list — that is `model_available()`'s third gate, and the two
        are asked separately where the difference decides the answer (issue
        #53: an unreadable list is a 503, a model the node does not carry a
        404).
        """
        if not self.cfg.model_allowed(model):
            return False
        if self.policy is not None and self.policy.blocks(self.cfg.name, model):
            return False
        return True

    def model_available(self, model: str) -> bool:
        """May RIPPLE dispatch `model` to this host?

        Three gates, in this order: the allow-list in the config (a host is only
        meant to carry what it is configured for), the operator's model policy
        (`ripple/model_policy.py`: `disabled` and `hidden` both say no), and
        whether oMLX actually lists the model.
        """
        return self.model_permitted(model) and model in self.models

    @property
    def model_list_unreadable(self) -> bool:
        """Is the node's model list unusable as evidence of what it carries?

        True when it was never read or when the last attempt failed (issue #53):
        `models` is then empty or stale, and "not in it" says nothing about the
        node. `model_available()` still answers False for such a host — dispatch
        must not start on a guess — but placement reports a 503 with the cause
        instead of the 404 that means "this model does not exist".
        """
        return self.models_read_at is None or self.models_error is not None

    def used_bytes_measured(self) -> int | None:
        """Sum of `actual_size` over the resident models — the measured base.

        The second base next to `used_bytes`, never mixed with it
        (`docs/dashboard-design.md` §5): `used_bytes` is oMLX's own
        `engine_pool.current_model_memory` from `/health`, i.e. the sum of the
        *estimated* sizes, and it is what the guard, placement and eviction
        decide on. This one is the sum of what `/v1/models/status` reports as
        `actual_size` after the load — the number the card shows, because it is
        the only one that was measured rather than configured.

        A model without an `actual_size` contributes nothing (it is skipped, not
        counted as zero), and a reported zero *is* a contribution: a resident
        model that occupies nothing (a tokenizer-only entry) must not make the
        sum look unknown. `None` when no resident model reports a size at all —
        that is "not measured", not "empty", and the caller falls back to
        `used_bytes`.
        """
        sizes = [m.actual_size for m in self.models.values()
                 if m.loaded and m.actual_size is not None]
        if not sizes:
            return None
        return sum(sizes)

    def locked_loaded(self) -> list[str]:
        """Locked models this node has resident right now (issue #21).

        The one predicate the three unload paths share: a dispatch that would
        evict one of these, a calibration run on this node and a replication to
        it all take memory away from it — so all three refuse while the list is
        not empty. Empty also when no policy is attached (a bare host in a
        test), which is the same as "nothing is locked".
        """
        if self.policy is None:
            return []
        return sorted(m.id for m in self.models.values()
                      if m.loaded and self.policy.locked(m.id))

    def reporting_state(self) -> str:
        """The one word the page shows: what the operator did, else the probe.

        Order matters. A drain is a removal in progress and a disable is a
        decision that outlives the process, so both outrank a maintenance window,
        which in turn outranks the probe verdict (`status`). The probe verdict is
        never overwritten — `status` keeps saying what the last health check saw.

        The single implementation of that rule: `/healthz` reads it through
        `to_dict()` and the admin views read it directly. Two answers about one
        node in one moment must not be able to disagree (issue #90), so nobody
        re-derives the order anywhere else.
        """
        if self.draining:
            return "draining"
        if not self.cfg.enabled:
            return "disabled"
        if self.maintenance:
            return "maintenance"
        return self.status

    def to_dict(self, *, target_threads: float = 0.0, slots: int = 0,
                target_share: float = 0.0) -> dict:
        loaded = sorted(m.id for m in self.models.values() if m.loaded)
        used_ratio = None
        if self.ceiling_bytes and self.used_bytes is not None:
            used_ratio = round(self.used_bytes / self.ceiling_bytes, 4)
        return {
            "name": self.cfg.name,
            "role": self.cfg.role,
            "site": self.cfg.site,
            "weight": self.cfg.weight,
            "target_share": round(target_share, 4),
            "enabled": self.cfg.enabled,
            # Which of the two sources declared this machine (issue #70): the
            # operator's own `config.yaml` or the overlay he added it to. The
            # page shows it, so "why is this node not in my file?" has an answer.
            "source": self.cfg.source,
            "address": self.resolved_address,
            "addresses": self.cfg.addresses,
            # `state` is `status` under its reporting name (contract §8);
            # `status` stays for the clients that already read it. A maintenance
            # window is the one place where they differ: `status` keeps the probe
            # verdict, `state` says what the operator did. A drain and a disable
            # are operator states too, and they win over the probe verdict for
            # the same reason — the page must not show a healthy node that is
            # on its way out.
            "state": self.reporting_state(),
            "status": self.status,
            # Whether placement may hand this host work right now — the same
            # predicate, reported. `state` says *why* it may not; this says
            # whether, so a page does not have to re-derive the rule.
            "in_pool": self.in_pool,
            # The drain's countdown (issue #70), for the page to render against
            # `server_time`: None when the node is not leaving.
            "draining": self.draining,
            "drain_started_at": self.drain_started_at,
            "drain_deadline": self.drain_deadline,
            "drain_remaining_seconds": self.drain_remaining_seconds,
            "maintenance": dict(self.maintenance) if self.maintenance is not None else None,
            "loading": dict(self.loading) if self.loading is not None else None,
            "healthy": self.healthy,
            "node_id": self.node_id_seen,
            "node_id_expected": self.cfg.node_id,
            "identity_ok": self.identity_ok,
            "version": self.version,
            "guard_enabled": self.guard_enabled,
            "hard_threshold": self.cfg.hard_threshold,
            "ceiling_bytes": self.ceiling_bytes,
            "used_bytes": self.used_bytes,
            # The measured base beside the estimated one (§5): the card shows
            # this, the guard decides on `used_bytes`. None = not measured.
            "used_bytes_measured": self.used_bytes_measured(),
            "used_ratio": used_ratio,
            "inflight": self.inflight,
            # What the node is doing right now (issue #18): `pp` between dispatch
            # and the first token, `tg` while tokens stream, None when idle. The
            # aggregate over this node's models — prefill wins over decode.
            "phase": self.phase,
            "phase_switches": self.phase_switches,
            "max_inflight": self.cfg.max_inflight,
            "spill_threshold": self.cfg.spill_threshold,
            "thread_slots": slots,
            "max_threads": self.cfg.max_threads,
            "target_threads": round(target_threads, 2),
            "rtt_ms": self.rtt_ms,
            "errors_total": self.errors_total,
            "http_507_total": self.http_507_total,
            "requests_total": self.requests_total,
            "last_error": self.last_error,
            "last_seen": self.last_seen,
            "models_available": len(self.models),
            # Whether the count above is evidence (issue #53): `models_read_at`
            # None or `models_error` set means the node never told us what it
            # carries, so a 0 here is "not known", not "carries nothing".
            "models_read_at": self.models_read_at,
            "models_error": self.models_error,
            "models_loaded": loaded,
            "capabilities": sorted(self.capabilities),
            "capability_notes": self.capability_notes,
            "addr_switches": self.addr_switches,
            "metrics": self.metrics,
        }


class HostUnresolved(Exception):
    """A host could not be inserted into the fleet (issue #70).

    Carries the `HostRuntime` that failed, because the answer an operator needs
    is the one the resolution already wrote down (`last_error`: a refused
    connect, an identity mismatch, an unreadable `node_id`) — not a generic
    "could not add host".
    """

    def __init__(self, host: "HostRuntime") -> None:
        super().__init__(host.last_error or "unresolved")
        self.host = host


class Fleet:
    """All hosts, their capabilities and the background refresh."""

    def __init__(self, cfg: Config, stats: Stats,
                 client: httpx.AsyncClient | None = None,
                 policy: ModelPolicy | None = None) -> None:
        self.cfg = cfg
        self.stats = stats
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=cfg.discovery.timeout_seconds,
                                  read=cfg.discovery.timeout_seconds,
                                  write=cfg.discovery.timeout_seconds,
                                  pool=cfg.discovery.timeout_seconds),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        # One store for the whole fleet, read once at start-up: the curve is
        # written by the calibration job (wave W3) and read on every estimate.
        self.calibration = load_calibration(cfg.placement.loading.calibration_file)
        # The operator's model policy, shared with every host so that
        # `model_available()` — the one predicate placement, admission and rebind
        # all ask — sees a change without an apply step.
        self.policy = policy if policy is not None else ModelPolicy()
        self.hosts: list[HostRuntime] = [
            HostRuntime(cfg=h, client=self.client,
                        placement=cfg.placement, stats=stats,
                        calibration=self.calibration, policy=self.policy,
                        # The back-reference the intersection needs (issue #88):
                        # a node that re-reads its capabilities tells the fleet.
                        fleet=self)
            for h in cfg.enabled_hosts()]
        self.capabilities: frozenset[str] = frozenset()
        self.capability_notes: list[str] = []
        self._task: asyncio.Task | None = None
        self._warmup_task: asyncio.Task | None = None

    # -- views -------------------------------------------------------------
    def get(self, name: str) -> HostRuntime | None:
        for h in self.hosts:
            if h.cfg.name == name:
                return h
        return None

    def healthy_hosts(self) -> list[HostRuntime]:
        """The hosts in the pool. The name is older than `in_pool`: a degraded or
        loading host is in it, only a `down` one is not."""
        return [h for h in self.hosts if h.in_pool]

    def entry_host(self) -> HostRuntime | None:
        for h in self.hosts:
            if h.cfg.role == "entry" and h.in_pool:
                return h
        return self.healthy_hosts()[0] if self.healthy_hosts() else None

    # -- lifecycle ---------------------------------------------------------
    async def start(self, *, warmup: bool = True) -> None:
        await asyncio.gather(*(h.resolve() for h in self.hosts))
        await asyncio.gather(*(h.refresh() for h in self.hosts))

        # The intersection over the nodes that have a voice — the same
        # computation `add()`, `remove()` and a node's own capability change run
        # (issue #88), so there is one rule and not three.
        for h in self.hosts:
            if h.backend is not None:
                await h._read_capabilities()
        self._recompute_capabilities()
        if "residency" not in self.capabilities:
            log.warning("eviction_guard: false — FA-18 is switched off because at least "
                        "one host delivers no residency signals")
        if "identity" not in self.capabilities:
            log.warning("identity: false — address counts as identity (RB-9 violated)")

        self._task = asyncio.create_task(self._refresh_loop(), name="ripple-refresh")
        if warmup:
            self._warmup_task = asyncio.create_task(self._warmup(), name="ripple-warmup")

    async def stop(self) -> None:
        for t in (self._task, self._warmup_task):
            if t:
                t.cancel()
        await self.client.aclose()

    async def _refresh_loop(self) -> None:
        interval = self.cfg.discovery.interval_seconds
        while True:
            try:
                await asyncio.sleep(interval)
                await asyncio.gather(*(h.refresh() for h in self.hosts),
                                     return_exceptions=True)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — the loop must never die
                log.exception("refresh_loop error")

    async def _warmup(self) -> None:
        """FA-16 is optional and **not implemented**: NZ-4 forbids RIPPLE to load
        models, and the only exception is an operator-initiated calibration
        (`ripple/calibration_api.py`). The `warmup:` key is accepted and warned
        about in `config.py`; nothing may act on it here."""
        return

    async def reinit(self) -> dict[str, str | None]:
        old = {h.cfg.name: h.resolved_address for h in self.hosts}
        await asyncio.gather(*(h.reinit() for h in self.hosts), return_exceptions=True)
        changed = {h.cfg.name: h.resolved_address for h in self.hosts
                   if h.resolved_address != old.get(h.cfg.name)}
        if changed:
            self.stats.addr_switches += len(changed)
        return changed

    # -- runtime fleet (issue #70) ------------------------------------------
    async def add(self, host_cfg: HostConfig, *, refresh: bool = True) -> HostRuntime:
        """Insert a host into the running fleet — the one write path.

        The node is *resolved before it is inserted*: a host that cannot be
        reached or whose `node_id` does not match is not a fleet member, it is an
        error for the caller to report. So the fleet never holds a host that was
        never seen, and `POST /admin/nodes` cannot half-add one.

        `resolve()` reads `/api/cluster/node_id`, which is the identity slots and
        affinity hang off; `refresh()` then reads the model list and the memory
        ceiling, so placement can use the node on the very next tick and
        `/healthz` shows its models without waiting for the probe loop.
        """
        host = HostRuntime(cfg=host_cfg, client=self.client, placement=self.cfg.placement,
                           stats=self.stats, calibration=self.calibration,
                           policy=self.policy, fleet=self)
        if not await host.resolve():
            # Not a fleet member: the fleet never holds a host that was never
            # seen, so `POST /admin/nodes` cannot half-add one.
            raise HostUnresolved(host)
        if not host_cfg.node_id and host.node_id_seen:
            # The identity is what slots, affinity and the calibration curve hang
            # off. Read it once here and keep it, so every later resolve verifies
            # the same machine instead of trusting the address (RB-9).
            host_cfg.node_id = host.node_id_seen
        if refresh:
            await host.refresh()
        await self._probe_capabilities(host)
        self.hosts.append(host)
        # `Config.hosts` is what `target_share` and `/healthz → threads.target`
        # divide by, so the two lists must not drift apart. Same object, not a
        # copy: the caller's `HostConfig` is the fleet's.
        if not any(h is host_cfg for h in self.cfg.hosts):
            self.cfg.hosts.append(host_cfg)
        self._recompute_capabilities()
        log.warning("fleet_add host=%s source=%s address=%s node_id=%s models=%d",
                    host.cfg.name, host.cfg.source, host.resolved_address,
                    host.node_id_seen, len(host.models))
        return host

    def remove(self, name: str) -> HostRuntime | None:
        """Take a host out of the fleet and out of the config's host list.

        No drain here on purpose: this is the mechanical half, and it must not
        be reachable without the wait. The API drains first and calls this only
        when `inflight == 0`; a caller that skips the drain is a caller that
        drops requests, and that decision belongs in one place (`proxy.py`).
        """
        host = self.get(name)
        if host is None:
            return None
        self.hosts = [h for h in self.hosts if h.cfg.name != name]
        self.cfg.hosts = [h for h in self.cfg.hosts if h.name != name]
        self._recompute_capabilities()
        log.warning("fleet_remove host=%s source=%s inflight=%d",
                    name, host.cfg.source, host.inflight)
        return host

    async def drain(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Stop routing to a host and wait until nothing runs on it.

        The wait is the whole point: a removal that pulls the `HostRuntime` out
        from under a streaming request kills that request, and the client sees a
        truncated answer instead of an error. So the host leaves the *pool* at
        once (`begin_drain` → `in_pool` False → placement sends elsewhere) and
        leaves the *fleet* only when `inflight` reaches 0.

        `timeout` is the deadline; the default is `fleet.drain_seconds`. On
        expiry nothing is removed: the answer carries what is still running, and
        the caller decides (disable the node and keep the entry, or try again).
        """
        host = self.get(name)
        if host is None:
            raise KeyError(name)
        limit = self.cfg.fleet.drain_seconds if timeout is None else max(0.0, float(timeout))
        poll = max(0.01, float(self.cfg.fleet.drain_poll_seconds))
        host.begin_drain(limit)
        started = time.monotonic()
        while host.inflight > 0 and (time.monotonic() - started) < limit:
            await asyncio.sleep(min(poll, max(0.0, limit - (time.monotonic() - started))))
        waited = time.monotonic() - started
        drained = host.inflight <= 0
        if drained:
            log.warning("drain_done host=%s waited=%.2fs", name, waited)
        else:
            log.warning("drain_timeout host=%s inflight=%d waited=%.2fs models=%s",
                        name, host.inflight, waited,
                        ",".join(r["model"] for r in host.running_models()) or "-")
        return {"drained": drained, "waited_seconds": round(waited, 3),
                "inflight": host.inflight, "models": host.running_models(),
                "deadline_seconds": round(limit, 3)}

    # -- capabilities -------------------------------------------------------
    async def _probe_capabilities(self, host: HostRuntime) -> None:
        """Ask one host what it can do and keep the notes (as `start()` does)."""
        await host._read_capabilities()

    def _recompute_capabilities(self) -> None:
        """The fleet's capabilities are the *intersection* — recompute them.

        A new node that cannot report residency switches FA-18 off fleet-wide,
        and a removed node that was the one holding a capability back gives it
        back. Either way the number the dashboard shows must follow the fleet,
        not the fleet that existed at start-up.

        Called from `start()`, `add()`, `remove()` — and from a node that has
        just re-read its own set (`HostRuntime._note_capabilities`, issue #88).
        That last caller is what makes a capability gained or lost on an update
        show up in `/healthz` within one interval: the intersection is recomputed
        when a node says its set changed, never on the poll.
        """
        caps: set[str] | None = None
        for h in self.hosts:
            if h.backend is None:
                continue
            caps = set(h.capabilities) if caps is None else caps & set(h.capabilities)
        before = self.capabilities
        self.capabilities = frozenset(caps or ())
        self.capability_notes = [n for h in self.hosts for n in h.capability_notes]
        lost = before - self.capabilities
        if lost:
            log.warning("fleet_capabilities lost=%s — the fleet no longer agrees on them",
                        ",".join(sorted(lost)))
