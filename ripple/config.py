"""Configuration: load YAML, expand variables, validate (FA-3, FA-20, FA-21).

The configuration is two-level 1:n — one proxy to n hosts, one host to n
addresses. Everything here is layer A of §5.1: hand-maintained and persistent.
Live state and measurements are never written back into it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .sites import (SiteConfig, overlapping_pairs, parse_networks, site_by_id,
                    site_for_address)

log = logging.getLogger("ripple.config")


class ConfigError(Exception):
    """The configuration cannot be used."""


#: Issue #81: the one sentence `ripple doctor` and the start-up log share.
#: `placement.plan_rebind` prices a move as `prompt_tokens / pp_tps` on the
#: destination, so a fleet in which no node carries a rate has rebinding
#: switched on and no candidate it can ever pick — a silent failure with no
#: line anywhere. Both places quote this constant instead of writing the
#: sentence twice and letting the two drift apart.
REBIND_NO_PREFILL_RATE = ("rebind is on, but no destination has a prefill rate — "
                          "it cannot fire")


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any, missing: list[str]) -> Any:
    """Expand ${VAR} recursively. Missing variables are collected, never
    silently swallowed — an empty API key would otherwise be a silent failure."""
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                missing.append(name)
                return ""
            return os.environ[name]

        return _ENV_RE.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v, missing) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, missing) for k, v in value.items()}
    return value


def _num(value: Any, name: str, *, minimum: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if minimum is not None and out < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {out}")
    return out


def _int(value: Any, name: str, *, minimum: int = 0) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if out < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {out}")
    return out


def _proxies(value: Any) -> list[str]:
    """`proxy.trusted_proxies`: IP addresses or CIDR networks, checked once here.

    A typo in this list would silently trust nobody (or, worse, be matched by
    hand somewhere else), and the failure mode of trusting the wrong peer is a
    forged origin in the log — so an entry that is not an address is an error,
    not a warning.
    """
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ConfigError("proxy.trusted_proxies must be a list of IP addresses or networks")
    out: list[str] = []
    for entry in value:
        text = str(entry).strip()
        if not text:
            continue
        try:
            ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ConfigError(
                f"proxy.trusted_proxies: {text!r} is neither an IP address nor a "
                f"network (e.g. 127.0.0.1 or 192.0.2.0/24)") from exc
        out.append(text)
    return out


def _model_root_warnings(roots: Any, *, where: str) -> list[str]:
    """Warnings for configured replication model roots that cannot be used (#20).

    A root is a path on *some* machine and the two sides are different machines,
    so the load cannot decide the side. What it can do is say out loud what will
    happen: `~` is expanded as the home of the machine running RIPPLE, which is
    right for a source on this machine and wrong for a target (whose home is not
    known here — the plan refuses such a root by name). A root that is still
    relative after expansion is refused on both sides. Silence here would be the
    silent skip the specification rules out.
    """
    out: list[str] = []
    for root in roots or ():
        text = str(root or "").strip()
        if not text:
            out.append(f"{where}: an empty model root is ignored — write the "
                       f"absolute directory")
            continue
        expanded = os.path.expanduser(text)
        if not os.path.isabs(expanded):
            out.append(f"{where}: model root {text!r} is relative — replication "
                       f"refuses it; write an absolute path")
        elif text.startswith("~"):
            out.append(
                f"{where}: model root {text!r} is expanded as {expanded} — the "
                f"home of the machine that runs RIPPLE. Correct for a source on "
                f"this machine; a target refuses it, so a target needs the "
                f"absolute path as it exists on that machine")
    return out


@dataclass
class HostConfig:
    """Layer A — static configuration of one host (§5.1)."""

    name: str
    addresses: list[str]
    node_id: str | None = None
    weight: float = 1.0
    enabled: bool = True
    role: str = "worker"          # entry | worker | fallback
    site: str = ""                # a site id from the top-level `sites:` block
                                  # (issue #67) — where the machine stands, never
                                  # how it is reached. Nothing in placement reads
                                  # it; `doctor` checks it against the address.
    backend: str = "omlx"         # omlx | openai_compatible
    api_key: str = ""
    max_inflight: int = 8         # hard upper bound (FA-5)
    max_threads: int = 20         # thread slots (FA-19)
    spill_threshold: int = 3      # considered full from here on (FA-5)
    hard_threshold: float = 0.95  # admission target for FA-18
    pp_tps: float | None = None   # measured cold prefill tok/s (rebind cost model)
    tg_tps: float | None = None   # measured idle decode tok/s — the capability, not
                                  # the moment: live rates follow the current load
    models: list[str] = field(default_factory=list)   # allow-list
    warmup: list[str] = field(default_factory=list)   # FA-16
    prefer_resident: bool = True  # FA-12
    # SSH destination and model roots for a replication job (issue #13).
    replication: "ReplicationHostConfig" = field(
        default_factory=lambda: ReplicationHostConfig())
    # Where this host's declaration comes from (issue #70): `config` for a host
    # written in `config.yaml`, `overlay` for one the operator added at the page
    # (`~/.ripple/runtime.yaml`). `/healthz` carries it, so the page can say which
    # of the machines are the operator's own file and which he added. Never read
    # from YAML — it is assigned by the merge, not declared.
    source: str = "config"

    def model_allowed(self, model: str) -> bool:
        """An empty allow-list means: everything the host offers."""
        return not self.models or model in self.models


@dataclass
class AffinityConfig:
    header: str = "X-Session-Id"
    ttl_seconds: int = 1800
    # A thread opened by a key with `kind: batch` keeps its slot for this long
    # instead. Batch traffic is throughput, not a person waiting: a job makes its
    # turns back to back and then nobody comes back to the prefix cache, so the
    # slot is better spent on the next conversation (FA-19). Not measured — 120 s
    # is two minutes, far beyond the gap between two turns of one job and far
    # below the 30 min a conversation may sit idle.
    batch_ttl_seconds: int = 120
    prefix_chars: int = 4096
    match_threshold: float = 0.5
    min_prompt_tokens: int = 8192
    ephemeral_window_seconds: int = 60


@dataclass
class RebindConfig:
    """Moving a bound thread to another host (FA-2, §6.1).

    The only price of a move is one full prefill on the destination — measured
    14.11.2026: M5 3.680 tok/s, M3 970 tok/s cold at 24.5k. So a move is only
    worth it when the decode gain it buys is larger than that prefill, and only
    at a moment where nothing is lost: the thread must be idle, no request of it
    in flight, and the destination must already hold the model (a cold load on
    top of a prefill would never pay).
    """

    enabled: bool = True
    min_idle_seconds: float = 0.0           # 0 = judge on every request, not only on wake-up
    per_thread_cooldown_seconds: float = 600.0
    max_per_hour: int = 4
    safety_factor: float = 1.5              # gain must beat cost by this much
    min_gain_seconds: float = 2.0           # below this a move is not worth the churn
    assumed_completion_tokens: int = 400    # used when the request has no max_tokens
    default_pp_tps: float = 0.0             # 0 = not measured (not infinite): only
                                            # hosts with a measured pp_tps count


@dataclass
class OptimizerConfig:
    """The periodic placement optimizer (issue #80).

    Rebind answers "may *this* thread move, now?" while a request runs; the
    optimizer asks the fleet-wide question on a timer: which host carries the
    highest relative load, and does moving one idle thread raise the **total**
    throughput. It is deliberately not part of `RebindConfig`: rebind keeps its
    per-request guards unchanged, and the two budgets are separate — the
    optimizer scans, the rebind guards every single move.

    `enabled` is on by default, but a host without a measured concurrency curve
    is skipped with `no_curve`, so a fresh install moves nothing until its nodes
    have been measured. `interval_seconds` is the scan period (a zero interval
    would be a busy loop, not a schedule) and `max_moves_per_hour` is the
    optimizer's own budget, on top of `rebind.max_per_hour`.
    """

    enabled: bool = True
    interval_seconds: float = 30.0
    max_moves_per_hour: int = 4


@dataclass
class MigrateConfig:
    """The arriving turn (issue #95, §6.4): may *this* turn move the thread?

    `placement.rebind` is the sweep — it prices an idle thread against the
    fleet, on a timer, where nobody waits. This block is the other half: a turn
    that is about to be prefilled in full is the one moment a move can still be
    paid for, so the same price is asked at arrival and amortised over the
    turns that will follow (`turns_left × gain_per_turn > cost × safety_factor +
    band`). Keeping it a separate block is deliberate — the two decisions share
    the arithmetic and none of their budgets, and switching one off must not
    switch the other off.

    The three hysteresis mechanisms each get their number here, except the one
    that must not be a number: the band is **measured** (`band_samples` scans of
    `Δtotal`), because no constant is right for every model pair. `return_factor`
    raises the bar for a move back to the host the thread just left, and
    `max_moves_per_thread` inside `window_seconds` pins a conversation that has
    spent its moves — the fleet-wide `rebind.max_per_hour` bounds the fleet, not
    one conversation.

    A config without this block keeps these defaults, so the shipped
    `config.yaml` needs no edit.
    """

    enabled: bool = True
    band_samples: int = 5              # Δtotal scans the observed band is measured over
    return_factor: float = 2.0         # a move *back* needs this much more
    max_moves_per_thread: int = 2      # moves inside the window before the pin
    window_seconds: float = 3600.0


@dataclass
class PrefillBiasConfig:
    """Host-relative prefill penalty in the deficit score (FA-2, §0.2).

    Decode is 1,3 : 1 between the two Macs and already in the 3 : 2 weights;
    cold prefill is 3,6–4,0 : 1 and is not. A long context placed on the weak
    node therefore costs minutes of prefill that the deficit never saw. The bias
    charges exactly that: `t = prompt / pp_tps` per host, and every host that is
    not the fastest loses `(t - t_fastest) / slot_seconds` slots — capped at
    `max_slots`, never a bonus, never below `min_prompt_tokens`.

    `slot_seconds` is the exchange rate: one minute of extra prefill is worth
    one conversation slot. Smaller = stricter.
    """

    enabled: bool = True
    slot_seconds: float = 60.0       # 1 slot == 1 minute of extra prefill
    max_slots: float = 3.0           # cap: a 1M-token prompt must not dominate
    min_prompt_tokens: int = 4096    # below this: zero bias, old behaviour


@dataclass
class LoadingConfig:
    """How RIPPLE treats a cold load it caused itself (loading state).

    While a host loads, its health probes are suspended and it keeps its last
    known health, so it stays in the candidate pool. The load rate is a property
    of the node's storage path, not of RIPPLE: an M5 loading from an external SSD
    is roughly twice as slow as one loading from its internal disk. That is why
    the estimate comes from `calibration_file` and not from a constant.
    """

    enabled: bool = True
    safety_factor: float = 1.3       # the estimate may be long, never short
    max_seconds: float = 900.0       # ceiling: a hung load must not suspend probes forever
    calibration_file: str = "~/.ripple/calibration.yaml"
    samples: int = 3                 # calibration sizes per node (2 or 3)


@dataclass
class ReplicationHostConfig:
    """Per-host facts a model copy needs (issue #13, issue #67).

    `address` is an **ordered** list of SSH destinations: the link the operator
    wants first (the Thunderbolt bridge) and the ones to fall back to (the LAN).
    It is the one thing the fleet's own address choice cannot supply — both of
    the M3's addresses are on-link from the M5 (`192.0.2.12` from
    `192.0.2.11`, `203.0.113.2` from `203.0.113.1`), so
    `ordered_addresses()` cannot prefer the cable and the routed LAN address
    wins by configuration order. Order decides, not a site: a site rule would
    point the copy at a bridge that has no address (issue #67 point 3).

    A single string still parses (`address: 10.0.0.9`) and becomes a list of
    one, so an existing configuration keeps loading unchanged.

    `model_roots` are the directories oMLX discovers on this machine
    (`model.model_dirs` in `~/.omlx/settings.json`). The relative subpath of a
    model is what gets recreated on the target; without a root there is nothing
    to be relative to, and the preflight refuses instead of guessing.
    """

    address: list[str] = field(default_factory=list)
    ssh_user: str = ""
    api_key: str = ""
    model_roots: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.address = _address_list(self.address)

    @property
    def ssh_target(self) -> str:
        """The first configured destination — the one the operator meant."""
        return self.address[0] if self.address else ""

    def is_configured(self) -> bool:
        return bool(self.address or self.ssh_user or self.api_key or self.model_roots)


def _address_list(raw: Any) -> list[str]:
    """`address` as an ordered, de-duplicated list of non-empty strings."""
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("hosts[].replication.address must be an address or a list "
                          "of addresses")
    out: list[str] = []
    for entry in raw:
        text = str(entry or "").strip()
        if text and text not in out:
            out.append(text)
    return out


@dataclass
class ReplicationConfig:
    """Replicating a model from one node to another (issue #13, FA-22 exception 3).

    Off by default and inert without configuration: `enabled: false` means the
    endpoints answer 409 and nothing else in RIPPLE changes. This is the only
    part of RIPPLE that writes to another machine's filesystem, which is why it
    is operator-initiated, one model at a time, and behind a confirmation dialog
    that names source, target, size and free space.
    """

    enabled: bool = False
    # The SSH command as a template with `{target}`. Not a hard-coded host: the
    # operator's user, key and options belong in their own configuration.
    ssh_command: str = "ssh -o BatchMode=yes -o ConnectTimeout=8 {target}"
    rsync_command: str = "rsync"
    ssh_user: str = ""
    # oMLX's own API key for `GET /admin/api/models`, `POST /admin/api/reload`
    # and `PUT /admin/api/models/{id}/settings`. Empty means the operator's
    # per-host `replication.api_key` is used; without either, the admin calls
    # answer 401 and the job says so.
    admin_key: str = ""
    # Fallback model roots for hosts that do not name their own.
    model_roots: list[str] = field(default_factory=list)
    # The free-space headroom of issue #13 point 2 (>= actual_size × 1.05).
    free_space_factor: float = 1.05
    probe_timeout_seconds: float = 15.0
    copy_timeout_seconds: float = 6 * 3600.0
    # Keys `GET /admin/api/models` reports but `PUT …/settings` rejects. Measured,
    # not guessed: see `ripple/replication.py` and `docs/replication.md` §1.
    settings_exclude: list[str] = field(
        default_factory=lambda: ["display_name", "description", "active_profile_name"])
    verify_settings: bool = True


@dataclass
class AuthConfig:
    """Client authentication (FA-23).

    Off by default so the test suite and a fresh checkout behave as before; the
    live config turns it on. `admin_key` unlocks the key page; client keys come
    from `keys_file` (YAML, mode 0600, never in the repo).
    """

    enabled: bool = False
    keys_file: str = "~/.ripple/keys.yaml"
    admin_key: str = ""
    # Requests from these addresses may pass without a key (loopback only by
    # default, so the operator's own tools keep working during the switch).
    allow_loopback: bool = False
    # A model outside a key's allow-list is answered with 404 model_not_found,
    # like OpenAI does, instead of leaking that it exists.
    hide_foreign_models: bool = True


@dataclass
class PlacementConfig:
    binding_strategy: str = "deficit"   # deficit | consistent_hash | least_threads
    balance_abs_threshold: float = 1.0
    balance_rel_threshold: float = 1.5
    rebind: "RebindConfig" = field(default_factory=lambda: RebindConfig())
    optimizer: "OptimizerConfig" = field(default_factory=lambda: OptimizerConfig())
    # The arriving turn (issue #95): the same price as `rebind`, asked at the
    # moment a prompt is about to be prefilled, where a move can still pay.
    migrate: "MigrateConfig" = field(default_factory=lambda: MigrateConfig())
    prefill_bias: "PrefillBiasConfig" = field(
        default_factory=lambda: PrefillBiasConfig())
    loading: "LoadingConfig" = field(default_factory=lambda: LoadingConfig())
    # A host becomes `down` only after this many consecutive *soft* probe
    # failures. 1 restores the behaviour before docs/loading-state.md, i.e. drop
    # on the first failure. A hard failure (connection refused, identity
    # mismatch) still drops the host at once — see HostRuntime.in_pool.
    flap_guard_failures: int = 3
    # A thread counts for the deficit with weight 1.0 while it works and fades
    # linearly to 0 over this many seconds of idleness. Without it a thread that
    # has been silent for 29 of its 30 TTL minutes still attracts new threads
    # away from the stronger host (FA-2, measured 14.11.2026).
    thread_decay_seconds: float = 300.0
    # Operator-declared maintenance windows (`ripple/maintenance.py`). Persisted,
    # so a node taken out for an update stays out across a RIPPLE restart.
    maintenance_file: str = "~/.ripple/maintenance.yaml"
    # Operator-declared model policy (`ripple/model_policy.py`): a (host, model)
    # pair that is `disabled` (no dispatch) or `hidden` (gone from /v1/models).
    # Persisted for the same reason.
    model_policy_file: str = "~/.ripple/models.yaml"


@dataclass
class DiscoveryConfig:
    interval_seconds: float = 10.0
    timeout_seconds: float = 5.0
    unhealthy_after: int = 3
    warmup_timeout_seconds: float = 300.0


@dataclass
class StatsConfig:
    """The per-host ledger (issue #33): where it lives and how often it is written.

    `file` is machine state like the maintenance windows and the model policy —
    written by RIPPLE, read at start-up, never in the repository. `persist: false`
    keeps the ledger in memory only (a test or a throwaway instance should not
    leave a file behind). `flush_seconds` is the write interval; a reset writes
    at once, whatever this says.
    """

    file: str = "~/.ripple/host_stats.json"
    persist: bool = True
    flush_seconds: float = 30.0


@dataclass
class FleetConfig:
    """The runtime overlay: hosts added or changed at the page (issue #70).

    `config.yaml` is the hand-written baseline and stays exactly that — nothing
    RIPPLE does at runtime writes back into it. What the operator adds or
    disables at the page lives in `file` and is merged **over** the config at
    start-up. The two sources have one rule, and it is not symmetric:

    * a node declared in `config.yaml` may be **disabled** by the overlay, never
      deleted — the file is the baseline, and a page click must not be able to
      make a hand-written machine disappear;
    * a node added in the overlay may be deleted again, because the overlay is
      the only place it ever existed.

    A node that exists in both keeps the config's identity (addresses, node_id,
    backend, key) and takes the overlay's `weight`, `site` and `enabled` —
    those are the operator's decisions, not the machine's description.

    `persist: false` keeps the overlay in memory only (a test or a throwaway
    instance must not leave a file behind, and two tests must not read each
    other's fleet through one path).

    Since issue #78 the file also carries `policy` — the routing policy
    (`affinity.*`, `placement.*`) the page may set without a restart. The key
    is still `fleet.file` and the shape of `hosts.overrides` is unchanged; only
    the file's name moved from `fleet.yaml` to `runtime.yaml`, because it is no
    longer only the fleet.
    """

    file: str = "~/.ripple/runtime.yaml"
    persist: bool = True
    # How long a removal or a disable waits for `inflight == 0` before it says
    # what is still running instead of dropping it. 120 s is longer than the
    # longest measured decode on a single request and short enough that an
    # operator does not think the call hung.
    drain_seconds: float = 120.0
    drain_poll_seconds: float = 0.25


@dataclass
class ProxyConfig:
    connect_timeout_seconds: float = 10.0
    retry_other_node: bool = True
    request_log: bool = True
    # Peers whose `X-Forwarded-For` is believed (IP or CIDR). Empty = never:
    # the header is written by whoever sends it, so an unlisted peer's claim
    # about its own origin is worthless and must not reach the log as one.
    trusted_proxies: list[str] = field(default_factory=list)


@dataclass
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8010
    log_level: str = "info"
    log_file: str | None = None
    hosts: list[HostConfig] = field(default_factory=list)
    # Named locations (issue #67). Empty is the old world: no host can claim a
    # site, and nothing is checked. A host names one by id in `hosts[].site`.
    sites: list[SiteConfig] = field(default_factory=list)
    affinity: AffinityConfig = field(default_factory=AffinityConfig)
    placement: PlacementConfig = field(default_factory=PlacementConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    replication: ReplicationConfig = field(default_factory=ReplicationConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    # The runtime overlay (issue #70): what the operator added or disabled at the
    # page, merged over `hosts` at start-up. See `FleetConfig`.
    fleet: FleetConfig = field(default_factory=FleetConfig)
    # What the overlay currently sets, as dotted paths (issue #78):
    # `{"affinity.ttl_seconds": 900, "hosts.alpha.weight": 2.0}`. Filled by
    # `apply_overlay`, so `/config` can say where a value in force comes from
    # without reading the overlay file again.
    overrides: dict[str, Any] = field(default_factory=dict)
    # The values `config.yaml` itself declares for the editable paths, taken
    # before any overlay is merged. A reset drops the override and puts these
    # back — the file is the baseline, so the baseline is what it says, not
    # whatever the value happened to be before the click.
    baseline: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source: Path | None = None

    # --- derived views -----------------------------------------------------
    @property
    def total_weight(self) -> float:
        return sum(h.weight for h in self.hosts if h.enabled) or 1.0

    def target_share(self, host: HostConfig) -> float:
        return host.weight / self.total_weight

    def host(self, name: str) -> HostConfig | None:
        for h in self.hosts:
            if h.name == name:
                return h
        return None

    def enabled_hosts(self) -> list[HostConfig]:
        return [h for h in self.hosts if h.enabled]

    # --- sites (issue #67) --------------------------------------------------
    def site(self, site_id: str) -> SiteConfig | None:
        """The site a host names, or None when the id is unknown/empty."""
        return site_by_id(self.sites, site_id)

    def site_of(self, host: HostConfig) -> SiteConfig | None:
        """The *declared* site of a host — what the configuration claims."""
        return self.site(host.site)

    def site_for_address(self, address: str | None) -> SiteConfig | None:
        """The site the given address is actually reachable in — the derivation.

        An address that no network contains returns None: the claim cannot be
        confirmed, which is different from a contradiction and is reported as
        such by `ripple doctor`.
        """
        return site_for_address(self.sites, address)

    # --- the rebind cost model (issue #81) ----------------------------------
    def rebind_prefill_rates(self) -> tuple[list[str], list[str]]:
        """Enabled hosts split by whether a move to them can be priced.

        `placement.plan_rebind` charges a move one cold prefill on the
        destination, `prompt_tokens / pp_tps`, and drops every host it cannot
        price. The rate is `hosts[].pp_tps`, else
        `placement.rebind.default_pp_tps` — and 0 there means *not measured*,
        not *infinitely fast*, so it prices nothing. Returns
        `(priced, unpriced)` host names, both in configuration order.
        """
        default = self.placement.rebind.default_pp_tps
        priced: list[str] = []
        unpriced: list[str] = []
        for host in self.enabled_hosts():
            (priced if (host.pp_tps or default) else unpriced).append(host.name)
        return priced, unpriced

    def rebind_cannot_fire(self) -> str | None:
        """The finding, or None when rebinding has at least one destination.

        Issue #81: rebinding is on, and no enabled node carries a prefill rate
        — so every candidate is skipped in the loop and the feature can never
        move a thread. Nothing raised, nothing logged: the kind of failure an
        operator only notices by wondering why the fleet never balances.

        The sentence is built here, once, because `ripple doctor` and the
        start-up log have to say the same thing (and name the same field).
        """
        if not self.placement.rebind.enabled:
            return None
        priced, unpriced = self.rebind_prefill_rates()
        if priced:
            return None
        names = ", ".join(unpriced) or "no enabled node"
        return (f"{REBIND_NO_PREFILL_RATE}: 'hosts[].pp_tps' is unset on {names} "
                f"and 'placement.rebind.default_pp_tps' is 0 — 0 means not "
                f"measured, not infinitely fast; set 'hosts[].pp_tps' on a node "
                f"to give a move a price")

    def summary(self) -> dict[str, Any]:
        """The effective configuration, ready to be shown and never to be edited.

        This backs `GET /config` and the dashboard's parameter view: the values
        that are actually in force, not the ones in the file — defaults, `${VAR}`
        expansion and validation are already applied. Secrets are reduced to
        "set"/"none", so a key can never leak through here (FA-15, AS-10).

        The dashboard is no longer only a window (issue #78): the routing policy
        — weights, spill thresholds, the affinity and rebind numbers — may be set
        at the page and takes effect without a restart, through the runtime
        overlay. Identity and transport still mean editing `config.yaml` and
        restarting (FA-3), and the `editable` list below is what tells the page
        which is which: a path on it may be PATCHed, everything else is shown
        with its reason instead of an input.
        """
        def host_view(h: HostConfig) -> dict[str, Any]:
            declared = self.site_of(h)
            return {
                "name": h.name,
                "role": h.role,
                # `config` or `overlay` (issue #70): which of the two sources
                # declared this machine.
                "source": h.source,
                "site": h.site,
                # The declared site resolved, and what it claims to contain
                # (issue #67). Empty strings when the id is unknown — the
                # warning list says so, this view stays printable.
                "site_name": declared.name if declared else "",
                "site_networks": list(declared.networks) if declared else [],
                "backend": h.backend,
                "enabled": h.enabled,
                "weight": h.weight,
                "share": round(self.target_share(h), 4),
                "addresses": list(h.addresses),
                "node_id": h.node_id or "not configured",
                "max_inflight": h.max_inflight,
                "max_threads": h.max_threads,
                "spill_threshold": h.spill_threshold,
                "hard_threshold": h.hard_threshold,
                "prefer_resident": h.prefer_resident,
                "pp_tps": h.pp_tps,
                "tg_tps": h.tg_tps,
                "models": list(h.models),
                "api_key": "set" if h.api_key else "none",
                "replication": {
                    "address": list(h.replication.address) or "not configured",
                    "ssh_user": h.replication.ssh_user or "not configured",
                    "model_roots": list(h.replication.model_roots),
                    "api_key": "set" if h.replication.api_key else "none",
                },
            }

        return {
            "source": str(self.source) if self.source else None,
            "listen": f"{self.listen_host}:{self.listen_port}",
            "log_level": self.log_level,
            "log_file": self.log_file,
            "sites": [s.as_dict() for s in self.sites],
            "hosts": [host_view(h) for h in self.hosts],
            "affinity": asdict(self.affinity),
            "placement": asdict(self.placement),
            "auth": {"enabled": self.auth.enabled,
                     "keys_file": self.auth.keys_file,
                     "admin_key": "set" if self.auth.admin_key else "none",
                     "allow_loopback": self.auth.allow_loopback},
            "discovery": asdict(self.discovery),
            "proxy": asdict(self.proxy),
            "stats": asdict(self.stats),
            # The runtime overlay's own settings (issue #70), never its content:
            # `/config` is the effective configuration, and the overlay's hosts
            # are already part of `hosts` above with their `source`.
            "fleet": asdict(self.fleet),
            # Where each value in force comes from (issue #78): only the paths
            # the overlay actually sets, as dotted paths. The page marks a row
            # as overridden by looking its path up here, and shows a reset
            # control for exactly those — nothing about the overlay is
            # hardcoded there.
            "overrides": dict(self.overrides),
            # The paths that may be set at the page at all. `*` stands for a
            # host name, and a path that is not on this list has no input.
            "editable": list(EDITABLE_PATHS),
            "replication": {
                "enabled": self.replication.enabled,
                "ssh_command": self.replication.ssh_command,
                "rsync_command": self.replication.rsync_command,
                "ssh_user": self.replication.ssh_user or "not configured",
                "admin_key": "set" if self.replication.admin_key else "none",
                "model_roots": list(self.replication.model_roots),
                "free_space_factor": self.replication.free_space_factor,
                "settings_exclude": list(self.replication.settings_exclude),
                "verify_settings": self.replication.verify_settings,
            },
            "warnings": list(self.warnings),
        }


def _parse_host(raw: dict[str, Any], defaults: dict[str, Any], idx: int) -> HostConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"hosts[{idx}] must be a mapping")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ConfigError(f"hosts[{idx}]: 'name' missing (stable identity, key for slots)")
    merged = {**defaults, **raw}

    addresses = merged.get("addresses") or []
    if isinstance(addresses, str):
        addresses = [addresses]
    if not addresses:
        raise ConfigError(f"host {name}: 'addresses' missing")
    norm: list[str] = []
    for a in addresses:
        a = str(a).rstrip("/")
        if not a.startswith(("http://", "https://")):
            raise ConfigError(f"host {name}: address {a!r} needs http:// or https://")
        norm.append(a)

    backend = str(merged.get("backend", "omlx")).lower()
    if backend not in ("omlx", "openai_compatible"):
        raise ConfigError(f"host {name}: unknown backend {backend!r} "
                          "(allowed: omlx, openai_compatible)")

    models = merged.get("models") or []
    if isinstance(models, str):
        models = [models]
    warmup = merged.get("warmup") or []
    if isinstance(warmup, str):
        warmup = [warmup]

    # Per-host replication facts (issue #13). Everything is optional; a host
    # without them simply cannot be a source or a target.
    rep_raw = merged.get("replication") or {}
    if not isinstance(rep_raw, dict):
        raise ConfigError(f"host {name}: 'replication' must be a mapping")
    rep_roots = rep_raw.get("model_roots") or []
    if isinstance(rep_roots, str):
        rep_roots = [rep_roots]
    host_replication = ReplicationHostConfig(
        address=_address_list(rep_raw.get("address")),
        ssh_user=str(rep_raw.get("ssh_user") or ""),
        api_key=str(rep_raw.get("api_key") or ""),
        model_roots=[str(r) for r in rep_roots],
    )

    host = HostConfig(
        name=name,
        addresses=norm,
        node_id=(str(merged["node_id"]) if merged.get("node_id") else None),
        weight=_num(merged.get("weight", 1), f"host {name}: weight", minimum=0.0),
        enabled=bool(merged.get("enabled", True)),
        role=str(merged.get("role", "worker")),
        site=str(merged.get("site", "")),
        backend=backend,
        api_key=str(merged.get("api_key") or ""),
        max_inflight=_int(merged.get("max_inflight", 8), f"host {name}: max_inflight", minimum=1),
        max_threads=_int(merged.get("max_threads", 20), f"host {name}: max_threads", minimum=1),
        spill_threshold=_int(merged.get("spill_threshold", 3), f"host {name}: spill_threshold"),
        hard_threshold=_num(merged.get("hard_threshold", 0.95),
                            f"host {name}: hard_threshold", minimum=0.0),
        models=[str(m) for m in models],
        warmup=[str(m) for m in warmup],
        prefer_resident=bool(merged.get("prefer_resident", True)),
        replication=host_replication,
        pp_tps=(_num(merged["pp_tps"], f"host {name}: pp_tps", minimum=1.0)
                if merged.get("pp_tps") else None),
        tg_tps=(_num(merged["tg_tps"], f"host {name}: tg_tps", minimum=1.0)
                if merged.get("tg_tps") else None),
    )
    if host.weight <= 0:
        host.weight = 0.0
    if host.spill_threshold > host.max_inflight:
        raise ConfigError(f"host {name}: spill_threshold ({host.spill_threshold}) "
                          f"must not exceed max_inflight ({host.max_inflight})")
    if host.hard_threshold > 1.0:
        raise ConfigError(f"host {name}: hard_threshold must be <= 1.0")
    return host


def _parse_sites(raw: Any) -> list[SiteConfig]:
    """The `sites:` block (issue #67) — named locations and their networks.

    A site is data, so the block is validated: an id is required (it is what a
    host names), a duplicate id is refused (two locations with one name is the
    silent ambiguity this issue is about), and every network must parse as CIDR.
    A site without networks is accepted and warned about by the caller — it is a
    name that can never be confirmed.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ConfigError("'sites' must be a list of {id, name, networks} blocks")
    out: list[SiteConfig] = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"sites[{idx}] must be a mapping with an 'id'")
        site_id = str(entry.get("id") or "").strip()
        if not site_id:
            raise ConfigError(f"sites[{idx}]: 'id' missing — hosts name a site by id")
        if site_id in seen:
            raise ConfigError(f"sites: duplicate site id {site_id!r}")
        seen.add(site_id)
        try:
            networks = parse_networks(entry.get("networks"), where=f"site {site_id}")
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        out.append(SiteConfig(id=site_id,
                              name=str(entry.get("name") or "").strip(),
                              networks=networks))
    return out


def host_from_block(block: dict[str, Any], defaults: dict[str, Any] | None = None,
                    idx: int = 0) -> HostConfig:
    """One host block as the overlay and `POST /admin/nodes` carry it.

    The same parser the file uses, so a host added at the page is validated
    exactly like a hand-written one — an address without a scheme, an unknown
    backend or a `spill_threshold` above `max_inflight` is refused here instead
    of failing on the first request. `ConfigError` is the caller's 400.
    """
    return _parse_host(block, defaults or {}, idx)


# -- the routing policy the page may set (issue #78) -----------------------
#
# One allow-list, one grammar, one validator.
#
# The grammar is a dotted path — `hosts.alpha.weight`,
# `affinity.ttl_seconds`, `placement.rebind.max_per_hour` — because the same
# string has to name a row on the page, a key in the overlay file and a segment
# of the endpoint's URL, and three spellings of one field would drift apart.
#
# The list is *policy* only. Identity and transport (an address, a key, a
# node_id, a limit, the listening socket, the log, auth) are what a restart is
# for: changing them under running requests means a different machine or a
# different process, not a different routing decision. The endpoint refuses
# those with a reason — the field exists, it is simply not settable.
POLICY_EDITABLE: tuple[tuple[str, str], ...] = (
    # per host: routing preference, never an address, a key or a limit
    ("hosts.*.weight", "float"),
    ("hosts.*.spill_threshold", "int"),
    ("hosts.*.prefer_resident", "bool"),
    # per thread: how a conversation is recognised and when its slot frees
    ("affinity.ttl_seconds", "int"),
    ("affinity.match_threshold", "float"),
    ("affinity.ephemeral_window_seconds", "int"),
    # rebind: bounds a behaviour that already exists
    ("placement.rebind.enabled", "bool"),
    ("placement.rebind.max_per_hour", "int"),
    ("placement.rebind.safety_factor", "float"),
    ("placement.rebind.per_thread_cooldown_seconds", "float"),
    # balance: hysteresis, visible in the ledger within minutes
    ("placement.balance_abs_threshold", "float"),
    ("placement.balance_rel_threshold", "float"),
)
EDITABLE_PATHS: tuple[str, ...] = tuple(path for path, _ in POLICY_EDITABLE)
_POLICY_KINDS: dict[str, str] = dict(POLICY_EDITABLE)

# What a host block may carry that the page may *not* set, and why. Identity
# first: these are the machine and the way to reach it, not a preference.
_HOST_STARTUP_FIELDS: dict[str, str] = {
    "addresses": "the address is how the machine is reached",
    "node_id": "node_id is the identity RIPPLE verifies on every probe",
    "api_key": "the api_key is a credential, not a routing decision",
    "backend": "the backend decides how the host is spoken to",
    "max_inflight": "max_inflight is the hard admission limit (FA-5)",
    "max_threads": "max_threads is the slot account of the machine",
    "models": "models is what the machine carries",
    "warmup": "warmup is a declaration about the machine",
    "replication": "replication is transport and credentials",
    "pp_tps": "pp_tps is a measurement of the machine, not a preference",
    "tg_tps": "tg_tps is a measurement of the machine, not a preference",
    "hard_threshold": "hard_threshold is part of the admission contract",
    "role": "role is a declaration about the machine",
    "name": "the name is the identity slots and affinity hang off",
}
_STARTUP_ONLY: dict[str, str] = {
    "listen_host": "the listening socket is opened once, at start-up",
    "listen_port": "the listening socket is opened once, at start-up",
    "log_level": "logging is configured before the first request",
    "log_file": "logging is configured before the first request",
    "affinity.header": "the header name is how every live thread is recognised — "
                       "changing it would orphan all of them",
}


def policy_target(path: str) -> tuple[str, str, str] | None:
    """`(section, subsection, field)` for an editable path, None otherwise.

    `hosts.alpha.weight` → `("hosts", "alpha", "weight")`;
    `affinity.ttl_seconds` → `("affinity", "", "ttl_seconds")`;
    `placement.rebind.max_per_hour` → `("placement", "rebind", "max_per_hour")`.

    The field is the last segment and, for a host, the name is everything
    between `hosts` and it — so a name that itself contains a dot still parses.
    """
    text = (path or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return None
    if parts[0] == "hosts" and len(parts) >= 3:
        name = ".".join(parts[1:-1])
        field = parts[-1]
        if name and f"hosts.*.{field}" in _POLICY_KINDS:
            return ("hosts", name, field)
        return None
    if parts[0] == "affinity" and len(parts) == 2 and text in _POLICY_KINDS:
        return ("affinity", "", parts[1])
    if parts[0] == "placement" and text in _POLICY_KINDS:
        if len(parts) == 3:
            return ("placement", parts[1], parts[2])
        if len(parts) == 2:
            return ("placement", "", parts[1])
    return None


def policy_refusal(path: str) -> str:
    """Why `path` has no input at the page — the sentence behind a 403.

    Never "not found": a field the operator can see in block 6.5 exists, and a
    404 would say RIPPLE does not know it. What he needs is the reason it is
    restart-bound.
    """
    text = (path or "").strip()
    parts = text.split(".")
    if parts[0] == "hosts" and len(parts) >= 3:
        field = parts[-1]
        if field in _HOST_STARTUP_FIELDS:
            return (f"{text}: hosts[].{field} — {_HOST_STARTUP_FIELDS[field]}. "
                    f"Edit config.yaml and restart")
        if field in ("enabled", "site"):
            return (f"{text}: a host's enabled state and site are set through "
                    f"POST /admin/nodes/{{name}}/disable, "
                    f"POST /admin/nodes/{{name}}/enable and POST /admin/nodes — "
                    f"not as a policy value")
        return f"{text}: no such configuration field"
    if text in _STARTUP_ONLY:
        return f"{text}: {_STARTUP_ONLY[text]} — edit config.yaml and restart"
    if parts[0] == "auth":
        return (f"{text}: auth decides who may call the API and is read at "
                f"start-up — edit config.yaml and restart")
    return f"{text}: not a routing-policy value — edit config.yaml and restart"


def policy_read(cfg: Config, path: str) -> Any:
    """The value in force for an editable path, read off the live config."""
    target = policy_target(path)
    if target is None:
        raise ConfigError(f"{path}: not an editable configuration path")
    section, sub, field = target
    if section == "hosts":
        host = cfg.host(sub)
        if host is None:
            raise ConfigError(f"host {sub!r} is not declared in config.yaml")
        return getattr(host, field)
    if section == "affinity":
        return getattr(cfg.affinity, field)
    if sub == "rebind":
        return getattr(cfg.placement.rebind, field)
    return getattr(cfg.placement, field)


def policy_value(cfg: Config, path: str, value: Any) -> Any:
    """`value` as the loader would read it, or `ConfigError` in the loader's words.

    This is the point of the issue, so it is worth being explicit about what it
    does *not* do: it does not re-implement a single range check. The one value
    is written into the block the loader reads and the loader's own parser
    (`_parse_host`, `_parse_affinity`, `_parse_placement`) is run over it — so a
    value refused at start-up is refused here with the same sentence, including
    the cross-field ones (`spill_threshold` above `max_inflight`, a
    `match_threshold` outside `(0, 1]`), and a value the loader would coerce is
    coerced the same way.

    The one addition is the boolean: the loader reads `bool(x)`, so the string
    `"false"` would mean true. A JSON body says `true`/`false` and nothing else,
    and accepting a string here would be a trap the page cannot show.
    """
    target = policy_target(path)
    if target is None:
        raise ConfigError(f"{path}: not an editable configuration path")
    section, sub, field = target
    kind = _POLICY_KINDS.get(f"hosts.*.{field}" if section == "hosts" else path)
    if kind == "bool" and not isinstance(value, bool):
        raise ConfigError(f"{path} must be true or false, got {value!r}")
    if section == "hosts":
        host = cfg.host(sub)
        if host is None:
            raise ConfigError(f"host {sub!r} is not declared in config.yaml")
        block = host_block(host)
        block[field] = value
        return getattr(_parse_host(block, {}, 0), field)
    if section == "affinity":
        raw = asdict(cfg.affinity)
        raw[field] = value
        return getattr(_parse_affinity(raw), field)
    raw = asdict(cfg.placement)
    if sub == "rebind":
        nested = raw.get("rebind")
        if not isinstance(nested, dict):
            nested = {}
            raw["rebind"] = nested
        nested[field] = value
        return getattr(_parse_placement(raw).rebind, field)
    raw[field] = value
    return getattr(_parse_placement(raw), field)


def policy_assign(cfg: Config, path: str, value: Any) -> Any:
    """Put a validated value into the live config — in place, never a new object.

    In place is not a style choice: the registry holds `cfg.affinity` and every
    `HostRuntime` holds its `HostConfig`, so replacing the object would leave the
    running service with the old numbers while `/config` reported the new ones.
    """
    target = policy_target(path)
    if target is None:
        raise ConfigError(f"{path}: not an editable configuration path")
    section, sub, field = target
    if section == "hosts":
        host = cfg.host(sub)
        if host is None:
            raise ConfigError(f"host {sub!r} is not declared in config.yaml")
        setattr(host, field, value)
    elif section == "affinity":
        setattr(cfg.affinity, field, value)
    elif sub == "rebind":
        setattr(cfg.placement.rebind, field, value)
    else:
        setattr(cfg.placement, field, value)
    return value


def policy_baseline(cfg: Config) -> dict[str, Any]:
    """Every editable path's value as `config.yaml` declares it.

    Taken before the overlay is merged, because a reset has to fall back to what
    the file says — not to whatever the last click left in the object.
    """
    out: dict[str, Any] = {}
    for host in cfg.hosts:
        for field in ("weight", "spill_threshold", "prefer_resident"):
            out[f"hosts.{host.name}.{field}"] = getattr(host, field)
    for path in EDITABLE_PATHS:
        if path.startswith("hosts."):
            continue
        try:
            out[path] = policy_read(cfg, path)
        except ConfigError:
            continue
    return out


def _plain(node: Any) -> Any:
    """A dict tree with plain values, so YAML never sees a dataclass or a tuple."""
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_plain(v) for v in node]
    return node


# -- the runtime overlay (issue #70, grown in #78) --------------------------
#
# Three maps, because the sources have three different rules:
#
#   hosts.nodes:      nodes added at the page. Full host blocks, deletable.
#   hosts.overrides:  nodes from config.yaml the operator changed at the page.
#                     Only `enabled`, `weight`, `site`, `spill_threshold` and
#                     `prefer_resident` — the config keeps the identity.
#   policy:           the routing policy below `affinity:` and `placement:`
#                     (issue #78). Mirrors the config tree.
#
# One file, written atomically, read at start-up, never merged back into
# `config.yaml`.
OVERLAY_VERSION = 1
# The per-host decisions an override may carry. `enabled` and `site` keep the
# meaning they had in #70; the other three are the per-host policy of #78 —
# never an address, a key, a node_id or a limit.
OVERLAY_KEYS = ("enabled", "weight", "site", "spill_threshold", "prefer_resident")
DEFAULT_FLEET_FILE = "~/.ripple/runtime.yaml"
# Before #78 the file was `fleet.yaml` and held only the fleet. It is read once
# under the old name so an upgrade does not lose a node the operator added; the
# first save writes `runtime.yaml`.
LEGACY_FLEET_FILE = "~/.ripple/fleet.yaml"


@dataclass
class FleetOverlay:
    """What the operator changed at the page, ready to be merged over the config.

    `error` is the reason the file could not be read, kept so the page can say
    it instead of showing a fleet that quietly lost a node.
    """

    path: Path | None = None
    persist: bool = True
    hosts: dict[str, dict[str, Any]] = field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The routing policy (issue #78), mirroring the config tree below
    # `affinity:` and `placement:` — `{"affinity": {"ttl_seconds": 900},
    # "placement": {"rebind": {"max_per_hour": 6}}}`.
    policy: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    # -- writes ------------------------------------------------------------
    def add(self, name: str, block: dict[str, Any]) -> None:
        """An overlay node, as `host_block()` shapes it."""
        self.hosts[name] = dict(block)
        self.overrides.pop(name, None)

    def drop(self, name: str) -> bool:
        """Forget an overlay node entirely. False when the name was not ours.

        An override for a config node is *not* dropped here: that name belongs
        to `config.yaml`, and its entry may only be disabled.
        """
        return self.hosts.pop(name, None) is not None

    def override(self, name: str, **values: Any) -> dict[str, Any]:
        """Set the operator decisions on a node from `config.yaml`.

        `enabled` and `site` (issue #70) plus `weight`, `spill_threshold` and
        `prefer_resident` (issue #78) — anything else is refused here, before it
        can reach the merge.
        """
        entry = self.overrides.setdefault(name, {})
        for key, value in values.items():
            if key not in OVERLAY_KEYS:
                raise ConfigError(
                    f"overlay: {key!r} cannot be changed for {name} — a host "
                    f"declared in config.yaml keeps its identity; only "
                    f"{', '.join(OVERLAY_KEYS)} may be overridden")
            entry[key] = value
        return entry

    def clear_override(self, name: str, *keys: str) -> bool:
        """Drop one or all overrides of a config node. True when something went."""
        entry = self.overrides.get(name)
        if not entry:
            return False
        if not keys:
            self.overrides.pop(name, None)
            return True
        before = dict(entry)
        for key in keys:
            entry.pop(key, None)
        if not entry:
            self.overrides.pop(name, None)
        return entry != before

    # -- dotted paths (issue #78) ------------------------------------------
    # One grammar for the file, the endpoint's URL and the page's rows:
    # `hosts.alpha.weight`, `affinity.ttl_seconds`,
    # `placement.rebind.max_per_hour`. The rules above are unchanged — a host
    # path lands in `overrides` (so it can only ever touch the five decisions),
    # an `affinity`/`placement` path lands in `policy`.
    def set_path(self, path: str, value: Any) -> None:
        """Write one editable dotted path into the overlay."""
        target = policy_target(path)
        if target is None:
            raise ConfigError(f"{path}: not an editable configuration path")
        section, sub, field = target
        if section == "hosts":
            self.overrides.setdefault(sub, {})[field] = value
            return
        node = self.policy
        for key in (section, sub):
            if not key:
                continue
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        node[field] = value

    def drop_path(self, path: str) -> bool:
        """Forget one dotted path. True when the overlay had it.

        Empty parents are pruned, so a file that had one policy value does not
        keep `affinity: {}` shells after the reset.
        """
        target = policy_target(path)
        if target is None:
            return False
        section, sub, field = target
        if section == "hosts":
            entry = self.overrides.get(sub)
            if not entry or field not in entry:
                return False
            entry.pop(field, None)
            if not entry:
                self.overrides.pop(sub, None)
            return True
        chain: list[tuple[dict[str, Any], str]] = []
        node: Any = self.policy
        for key in (section, sub):
            if not key:
                continue
            if not isinstance(node, dict) or not isinstance(node.get(key), dict):
                return False
            chain.append((node, key))
            node = node[key]
        if field not in node:
            return False
        node.pop(field, None)
        for parent, key in reversed(chain):
            if parent[key]:
                break
            parent.pop(key, None)
        return True

    def paths(self) -> dict[str, Any]:
        """Everything the overlay sets, as dotted paths (what `/config` shows).

        Includes `enabled` and `site` from #70 even though they are not on the
        policy allow-list: they *are* overridden, and a page that hid them would
        claim the file's value is in force when it is not.
        """
        out: dict[str, Any] = {}
        for name, entry in self.overrides.items():
            for key, value in entry.items():
                out[f"hosts.{name}.{key}"] = value

        def walk(node: dict[str, Any], prefix: str) -> None:
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(value, dict):
                    walk(value, path)
                else:
                    out[path] = value

        walk(self.policy, "")
        return out

    # -- view --------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "version": OVERLAY_VERSION,
            # A file the operator may edit by hand, so the rule is written in it.
            "comment": (
                "RIPPLE runtime overlay (issue #70, #78). Written by the admin "
                "API — config.yaml stays the hand-written baseline and is never "
                "rewritten. 'hosts.nodes' are machines added at the page and may "
                "be deleted again; 'hosts.overrides' may only change the routing "
                "decisions of a host from config.yaml (enabled, weight, site, "
                "spill_threshold, prefer_resident), never its identity; 'policy' "
                "holds affinity.* and placement.* values set at the page."),
            "hosts": {
                "overrides": {name: dict(entry) for name, entry in self.overrides.items()},
                "nodes": {name: dict(block) for name, block in self.hosts.items()},
            },
            "policy": _plain(self.policy),
        }


def host_block(host: HostConfig) -> dict[str, Any]:
    """A `HostConfig` in the shape `_parse_host` reads — the overlay's entry.

    Everything a restart needs to rebuild the same host: the address it answers
    on, the identity it must match, its key and its capacity. `source` is not
    written: it is the merge's verdict, not a declaration.
    """
    block: dict[str, Any] = {
        "name": host.name,
        "addresses": list(host.addresses),
        "enabled": bool(host.enabled),
        "role": host.role,
        "weight": host.weight,
        "site": host.site,
        "backend": host.backend,
        "max_inflight": host.max_inflight,
        "max_threads": host.max_threads,
        "spill_threshold": host.spill_threshold,
        "hard_threshold": host.hard_threshold,
        "prefer_resident": bool(host.prefer_resident),
    }
    if host.node_id:
        block["node_id"] = host.node_id
    if host.api_key:
        block["api_key"] = host.api_key
    if host.models:
        block["models"] = list(host.models)
    if host.pp_tps:
        block["pp_tps"] = host.pp_tps
    if host.tg_tps:
        block["tg_tps"] = host.tg_tps
    return block


def _legacy_overlay_path(p: Path) -> Path | None:
    """The pre-#78 name of the overlay, for a path that is `runtime.yaml`."""
    return p.with_name("fleet.yaml") if p.name == "runtime.yaml" else None


def _read_policy(raw: Any) -> dict[str, Any]:
    """The `policy:` block, keeping only what the page may set (issue #78).

    A key that is not on the allow-list is a warning and nothing else: the file
    is hand-editable, and a value that could not have been loaded from
    `config.yaml` must not be merged in just because it sits in the overlay.
    """
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        if raw not in (None, ""):
            log.warning("overlay_invalid section=policy — not a mapping")
        return out

    def walk(node: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                walk(value, path)
                continue
            if path in _POLICY_KINDS and not path.startswith("hosts."):
                section, sub, field = policy_target(path) or ("", "", "")
                cursor = out
                for part in (section, sub):
                    if not part:
                        continue
                    nxt = cursor.get(part)
                    if not isinstance(nxt, dict):
                        nxt = {}
                        cursor[part] = nxt
                    cursor = nxt
                cursor[field] = value
            else:
                log.warning("overlay_unknown_key path=%s — ignored (not a policy "
                            "value; hosts belong under hosts.overrides)", path)

    walk(raw, "")
    return out


def load_overlay(path: str | Path | None = None, *, persist: bool = True) -> FleetOverlay:
    """Read the runtime overlay. Missing or broken means "nothing added".

    A corrupt file must never keep RIPPLE from starting: the config's hosts are
    still the fleet, and the reason is kept in `error` so the page can show it
    instead of silently dropping the machines the operator added.

    Two shapes are read. The #78 one is `hosts.overrides` + `hosts.nodes` +
    `policy`; the #70 one had `hosts` (node blocks) and `overrides` at the top
    level, and is still understood so an upgrade does not silently lose a node.
    A `runtime.yaml` that does not exist yet but has a `fleet.yaml` beside it is
    read from that file, once, with one warning — the first save writes the new
    name.
    """
    p = Path(path).expanduser() if path else None
    store = FleetOverlay(path=p, persist=bool(persist and p is not None))
    if p is None or not store.persist:
        return store
    source = p
    if not p.is_file():
        legacy = _legacy_overlay_path(p)
        if legacy is None or not legacy.is_file():
            return store
        log.warning("overlay_legacy_file path=%s — rename to %s", legacy, p.name)
        source = legacy
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        store.error = f"{type(exc).__name__}: {exc}"
        log.warning("fleet_overlay_unreadable path=%s error=%s — nothing added", source, exc)
        return store
    if not isinstance(raw, dict):
        store.error = "root is not a mapping"
        log.warning("fleet_overlay_invalid path=%s — root is not a mapping", source)
        return store

    def read_nodes(nodes: Any) -> None:
        if not isinstance(nodes, dict):
            return
        for name, block in nodes.items():
            if isinstance(block, dict) and str(name).strip():
                store.hosts[str(name).strip()] = dict(block)

    def read_overrides(entries: Any) -> None:
        if not isinstance(entries, dict):
            return
        for name, entry in entries.items():
            if not isinstance(entry, dict) or not str(name).strip():
                continue
            # Only the operator decisions survive a read, so a hand-edited
            # `addresses:` under `overrides` cannot rewrite a config host.
            kept = {k: v for k, v in entry.items() if k in OVERLAY_KEYS}
            if kept:
                store.overrides[str(name).strip()] = kept

    hosts_raw = raw.get("hosts")
    if isinstance(hosts_raw, dict) and ("overrides" in hosts_raw or "nodes" in hosts_raw):
        read_overrides(hosts_raw.get("overrides"))
        read_nodes(hosts_raw.get("nodes"))
    else:
        # The #70 layout: `hosts:` was the map of node blocks.
        read_nodes(hosts_raw)
    read_overrides(raw.get("overrides"))
    store.policy = _read_policy(raw.get("policy"))
    return store


def save_overlay(overlay: FleetOverlay, path: str | Path | None = None) -> None:
    """Write the overlay atomically: `.tmp` + fsync + rename, mode 0600.

    Same rule as `host_stats.json` — a reader sees the old file or the new one,
    never half of one — plus the fsync, because this file is the only record of
    a machine the operator added and a crash right after the click must not lose
    it. 0600 because an overlay host may carry an oMLX key.
    """
    target = Path(path or overlay.path or DEFAULT_FLEET_FILE).expanduser()
    if not overlay.persist:
        overlay.path = target
        return
    overlay.path = target
    text = yaml.safe_dump(overlay.as_dict(), sort_keys=False, allow_unicode=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("fleet_overlay: cannot write %s (%s)", target, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def apply_policy(cfg: Config, overlay: FleetOverlay,
                 applied: dict[str, Any] | None = None) -> list[str]:
    """Merge the overlay's `policy:` block over `cfg.affinity` / `cfg.placement`.

    Same validator as the loader and the endpoint (`policy_value`), same rule as
    the host overrides: a value that would have been refused at start-up is a
    warning here and the file's value stays in force. The overlay never
    half-applies a block it could not have loaded — a TTL of `"soon"` in a
    hand-edited file must not become `1800` silently either.

    `applied`, when given, collects the paths that really landed, so `/config`
    never claims an override that was refused.
    """
    warnings: list[str] = []
    for path, value in overlay.paths().items():
        if path.startswith("hosts."):
            continue                      # applied with the host overrides
        try:
            effective = policy_value(cfg, path, value)
        except ConfigError as exc:
            warnings.append(f"policy overlay: {exc} — ignored")
            continue
        policy_assign(cfg, path, effective)
        if applied is not None:
            applied[path] = effective
        log.info("policy_overlay path=%s value=%r", path, effective)
    return warnings


def apply_overlay(cfg: Config, overlay: FleetOverlay) -> list[str]:
    """Merge the overlay over `cfg.hosts` and the policy — the one place the rule lives.

    * an override touches only the operator decisions of a host that
      `config.yaml` declares: `enabled`, `site` (as in #70) and the policy
      fields `weight`, `spill_threshold`, `prefer_resident` (#78), each
      validated by the loader's own host parser. A name the config does not
      know is a warning and nothing else: an override can never *create* a
      host, because then a typo in the overlay would silently add a machine;
    * an overlay host is appended whole and marked `source: "overlay"`. A name
      the config already uses is a warning and the config wins — two
      declarations of one machine would mean two slot accounts for it;
    * a node declared in `config.yaml` is never removed here. Deleting it is the
      API's refusal (issue #70), not a merge accident;
    * `policy:` is merged by `apply_policy`, with the same validator.

    Returns the warnings for `cfg.warnings`; the caller extends the list, so a
    test can read the merge's complaints without a log capture.
    """
    warnings: list[str] = []
    applied: dict[str, Any] = {}
    # The baseline first (issue #78): a reset falls back to what the file says,
    # so it is taken before anything is merged over it.
    cfg.baseline = policy_baseline(cfg)
    # The overlay's own nodes first, so an override that names one of them is
    # applied to it in the same pass — and survives the next restart, when the
    # node is re-read from `hosts.nodes` and the override from
    # `hosts.overrides`.
    for name, block in overlay.hosts.items():
        if cfg.host(name) is not None:
            warnings.append(
                f"fleet overlay: {name!r} is also declared in config.yaml — the "
                f"config keeps the identity and the overlay block is ignored")
            continue
        try:
            host = _parse_host({**block, "name": name}, {}, len(cfg.hosts))
        except ConfigError as exc:
            warnings.append(f"fleet overlay: host {name!r} is unusable ({exc}) — "
                            f"left out of the fleet")
            continue
        host.source = "overlay"
        cfg.hosts.append(host)
        # The block is this node's baseline: `hosts.nodes` is the file it lives
        # in, so a reset at the page returns to what that block says.
        for field in ("weight", "spill_threshold", "prefer_resident"):
            cfg.baseline.setdefault(f"hosts.{name}.{field}", getattr(host, field))
        log.info("fleet_overlay_host name=%s addresses=%s node_id=%s enabled=%s",
                 host.name, ",".join(host.addresses), host.node_id, host.enabled)
    for name, entry in overlay.overrides.items():
        host = cfg.host(name)
        if host is None:
            warnings.append(
                f"fleet overlay: override for {name!r}, which config.yaml does "
                f"not declare — ignored (an override cannot add a host)")
            continue
        for key, value in entry.items():
            if key == "enabled":
                host.enabled = bool(value)
                applied[f"hosts.{name}.enabled"] = host.enabled
                continue
            if key == "site":
                host.site = str(value or "")
                applied[f"hosts.{name}.site"] = host.site
                continue
            path = f"hosts.{name}.{key}"
            try:
                effective = policy_value(cfg, path, value)
            except ConfigError as exc:
                warnings.append(f"fleet overlay: {exc} — ignored")
                continue
            policy_assign(cfg, path, effective)
            applied[path] = effective
        log.info("fleet_overlay_override host=%s %s", name,
                 ", ".join(f"{k}={entry[k]!r}" for k in OVERLAY_KEYS if k in entry))
    warnings.extend(apply_policy(cfg, overlay, applied))
    # What the overlay actually set, for `/config` (issue #78): the page reads
    # its origin marks and its reset controls from here and hardcodes nothing.
    # Only what landed — a refused value must not be shown as an override.
    cfg.overrides = applied
    return warnings


def _parse_affinity(raw: dict[str, Any]) -> AffinityConfig:
    """The `affinity:` block — the loader's one reading of it.

    Factored out of `load_config` for issue #78: the admin endpoint validates a
    changed value by running *this* function over the block it would load, so a
    value refused at start-up is refused there with the same sentence. A second
    copy of these range checks would drift, and the drift would be a page that
    accepts what a restart rejects.
    """
    aff = AffinityConfig(
        header=str(raw.get("header", "X-Session-Id")),
        ttl_seconds=_int(raw.get("ttl_seconds", 1800), "affinity.ttl_seconds", minimum=1),
        batch_ttl_seconds=_int(raw.get("batch_ttl_seconds", 120),
                               "affinity.batch_ttl_seconds", minimum=1),
        prefix_chars=_int(raw.get("prefix_chars", 4096), "affinity.prefix_chars", minimum=16),
        match_threshold=_num(raw.get("match_threshold", 0.5), "affinity.match_threshold"),
        min_prompt_tokens=_int(raw.get("min_prompt_tokens", 8192),
                               "affinity.min_prompt_tokens", minimum=1),
        ephemeral_window_seconds=_int(raw.get("ephemeral_window_seconds", 60),
                                      "affinity.ephemeral_window_seconds", minimum=0),
    )
    if not 0.0 < aff.match_threshold <= 1.0:
        raise ConfigError("affinity.match_threshold must be within (0, 1]")
    return aff


def _parse_placement(raw: dict[str, Any]) -> PlacementConfig:
    """The `placement:` block, including `rebind`, `optimizer`, `prefill_bias`
    and `loading` — the loader's one reading of it (see `_parse_affinity`)."""
    plc = PlacementConfig(
        binding_strategy=str(raw.get("binding_strategy", "deficit")),
        balance_abs_threshold=_num(raw.get("balance_abs_threshold", 1),
                                   "placement.balance_abs_threshold"),
        balance_rel_threshold=_num(raw.get("balance_rel_threshold", 1.5),
                                   "placement.balance_rel_threshold", minimum=0.0),
        thread_decay_seconds=_num(raw.get("thread_decay_seconds", 300),
                                  "placement.thread_decay_seconds", minimum=0.0),
        # The two operator stores. Both have a working default, so an existing
        # config file keeps loading unchanged.
        maintenance_file=str(raw.get("maintenance_file")
                             or PlacementConfig().maintenance_file),
        model_policy_file=str(raw.get("model_policy_file")
                              or PlacementConfig().model_policy_file),
    )
    rb_raw = raw.get("rebind") or {}
    plc.rebind = RebindConfig(
        enabled=bool(rb_raw.get("enabled", True)),
        min_idle_seconds=_num(rb_raw.get("min_idle_seconds", 0),
                              "placement.rebind.min_idle_seconds", minimum=0.0),
        per_thread_cooldown_seconds=_num(rb_raw.get("per_thread_cooldown_seconds", 600),
                                         "placement.rebind.per_thread_cooldown_seconds",
                                         minimum=0.0),
        max_per_hour=_int(rb_raw.get("max_per_hour", 4), "placement.rebind.max_per_hour",
                          minimum=0),
        safety_factor=_num(rb_raw.get("safety_factor", 1.5), "placement.rebind.safety_factor",
                          minimum=0.0),
        min_gain_seconds=_num(rb_raw.get("min_gain_seconds", 2.0),
                              "placement.rebind.min_gain_seconds", minimum=0.0),
        assumed_completion_tokens=_int(rb_raw.get("assumed_completion_tokens", 400),
                                       "placement.rebind.assumed_completion_tokens", minimum=1),
        default_pp_tps=_num(rb_raw.get("default_pp_tps", 0),
                            "placement.rebind.default_pp_tps", minimum=0.0),
    )
    if plc.binding_strategy not in ("deficit", "consistent_hash", "least_threads"):
        raise ConfigError("placement.binding_strategy must be deficit, consistent_hash "
                          "or least_threads")

    # The periodic optimizer (issue #80). A missing block is not an error: the
    # defaults are the documented behaviour, so an existing config file keeps
    # loading unchanged and the optimizer starts on its default interval.
    opt_raw = raw.get("optimizer") or {}
    if not isinstance(opt_raw, dict):
        raise ConfigError("placement.optimizer must be a mapping")
    plc.optimizer = OptimizerConfig(
        enabled=bool(opt_raw.get("enabled", True)),
        interval_seconds=_num(opt_raw.get("interval_seconds", 30.0),
                              "placement.optimizer.interval_seconds", minimum=0.0),
        max_moves_per_hour=_int(opt_raw.get("max_moves_per_hour", 4),
                                "placement.optimizer.max_moves_per_hour", minimum=0),
    )
    if plc.optimizer.interval_seconds <= 0:
        # Zero would mean "scan continuously": a busy loop that prices every
        # thread on every pass and burns the fleet's budget on churn.
        raise ConfigError("placement.optimizer.interval_seconds must be > 0 "
                          f"(a scan interval of {plc.optimizer.interval_seconds:g} s "
                          "is a busy loop, not a schedule)")

    # The arriving turn (issue #95). `rebind` stays the sweep; this block prices
    # the same move at the moment a turn arrives. Every key has a working
    # default, so a config written before the block existed keeps loading.
    mg_raw = raw.get("migrate") or {}
    if not isinstance(mg_raw, dict):
        raise ConfigError("placement.migrate must be a mapping")
    plc.migrate = MigrateConfig(
        enabled=bool(mg_raw.get("enabled", True)),
        band_samples=_int(mg_raw.get("band_samples", 5),
                          "placement.migrate.band_samples", minimum=2),
        # Below 1.0 the "return factor" would *reward* a move back — the one
        # thing this mechanism exists to prevent — so it is refused at load.
        return_factor=_num(mg_raw.get("return_factor", 2.0),
                           "placement.migrate.return_factor", minimum=1.0),
        max_moves_per_thread=_int(mg_raw.get("max_moves_per_thread", 2),
                                  "placement.migrate.max_moves_per_thread", minimum=1),
        window_seconds=_num(mg_raw.get("window_seconds", 3600.0),
                            "placement.migrate.window_seconds", minimum=1.0),
    )

    pb_raw = raw.get("prefill_bias") or {}
    plc.prefill_bias = PrefillBiasConfig(
        enabled=bool(pb_raw.get("enabled", True)),
        slot_seconds=_num(pb_raw.get("slot_seconds", 60.0),
                          "placement.prefill_bias.slot_seconds", minimum=0.0),
        max_slots=_num(pb_raw.get("max_slots", 3.0),
                       "placement.prefill_bias.max_slots", minimum=0.0),
        min_prompt_tokens=_int(pb_raw.get("min_prompt_tokens", 4096),
                               "placement.prefill_bias.min_prompt_tokens", minimum=0),
    )
    if plc.prefill_bias.enabled and plc.prefill_bias.slot_seconds <= 0:
        # A zero exchange rate would mean "any extra second is worth infinite
        # slots" — the cap would silently decide every placement instead.
        raise ConfigError("placement.prefill_bias.slot_seconds must be > 0 "
                          "when the prefill bias is enabled")

    ld_raw = raw.get("loading") or {}
    if not isinstance(ld_raw, dict):
        raise ConfigError("placement.loading must be a mapping")
    plc.loading = LoadingConfig(
        enabled=bool(ld_raw.get("enabled", True)),
        safety_factor=_num(ld_raw.get("safety_factor", 1.3),
                           "placement.loading.safety_factor", minimum=1.0),
        max_seconds=_num(ld_raw.get("max_seconds", 900.0),
                         "placement.loading.max_seconds", minimum=1.0),
        calibration_file=str(ld_raw.get("calibration_file")
                             or "~/.ripple/calibration.yaml"),
        samples=_int(ld_raw.get("samples", 3), "placement.loading.samples", minimum=2),
    )
    if plc.loading.samples > 5:
        raise ConfigError("placement.loading.samples must be between 2 and 5")
    plc.flap_guard_failures = _int(raw.get("flap_guard_failures", 3),
                                   "placement.flap_guard_failures", minimum=1)
    return plc


def load_config(path: str | Path) -> Config:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"configuration file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML error in {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: root must be a mapping")

    missing_env: list[str] = []
    raw = _expand(raw, missing_env)

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("'defaults' must be a mapping")

    hosts_raw = raw.get("hosts") or []
    if not hosts_raw:
        raise ConfigError("'hosts' is empty — nothing to balance without a host")
    hosts = [_parse_host(h, defaults, i) for i, h in enumerate(hosts_raw)]
    sites = _parse_sites(raw.get("sites"))

    names = [h.name for h in hosts]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate host names: {', '.join(sorted(dupes))}")

    aff = _parse_affinity(raw.get("affinity") or {})
    plc = _parse_placement(raw.get("placement") or {})
    dis_raw = raw.get("discovery") or {}
    prx_raw = raw.get("proxy") or {}

    auth_raw = raw.get("auth") or {}
    auth = AuthConfig(
        enabled=bool(auth_raw.get("enabled", False)),
        keys_file=str(auth_raw.get("keys_file") or "~/.ripple/keys.yaml"),
        admin_key=str(auth_raw.get("admin_key") or ""),
        allow_loopback=bool(auth_raw.get("allow_loopback", False)),
        hide_foreign_models=bool(auth_raw.get("hide_foreign_models", True)),
    )

    dis = DiscoveryConfig(
        interval_seconds=_num(dis_raw.get("interval_seconds", 10), "discovery.interval_seconds",
                              minimum=0.5),
        timeout_seconds=_num(dis_raw.get("timeout_seconds", 5), "discovery.timeout_seconds",
                             minimum=0.1),
        unhealthy_after=_int(dis_raw.get("unhealthy_after", 3), "discovery.unhealthy_after",
                             minimum=1),
        warmup_timeout_seconds=_num(dis_raw.get("warmup_timeout_seconds", 300),
                                    "discovery.warmup_timeout_seconds", minimum=1),
    )

    prx = ProxyConfig(
        connect_timeout_seconds=_num(prx_raw.get("connect_timeout_seconds", 10),
                                     "proxy.connect_timeout_seconds", minimum=0.1),
        retry_other_node=bool(prx_raw.get("retry_other_node", True)),
        request_log=bool(prx_raw.get("request_log", True)),
        trusted_proxies=_proxies(prx_raw.get("trusted_proxies")),
    )

    # The per-host ledger (issue #33). Every default works, so an existing
    # configuration keeps loading unchanged.
    stats_raw = raw.get("stats") or {}
    if not isinstance(stats_raw, dict):
        raise ConfigError("'stats' must be a mapping")
    stats_cfg = StatsConfig(
        file=str(stats_raw.get("file") or StatsConfig().file),
        persist=bool(stats_raw.get("persist", True)),
        flush_seconds=_num(stats_raw.get("flush_seconds", 30),
                           "stats.flush_seconds", minimum=1.0),
    )

    # The runtime overlay (issue #70). Only its own settings are read here; the
    # file itself is merged in `Ripple.__init__` (`apply_overlay`), so
    # `load_config` stays a pure read of the hand-written file — a test that
    # calls it must not pick up the operator's fleet.
    fleet_raw = raw.get("fleet") or {}
    if not isinstance(fleet_raw, dict):
        raise ConfigError("'fleet' must be a mapping")
    fleet_cfg = FleetConfig(
        file=str(fleet_raw.get("file") or FleetConfig().file),
        persist=bool(fleet_raw.get("persist", True)),
        drain_seconds=_num(fleet_raw.get("drain_seconds", 120.0),
                           "fleet.drain_seconds", minimum=0.0),
        drain_poll_seconds=_num(fleet_raw.get("drain_poll_seconds", 0.25),
                                "fleet.drain_poll_seconds", minimum=0.01),
    )

    # Replication (issue #13). Every default is inert: with `enabled: false` and
    # no host-level facts, the endpoints refuse and nothing else changes.
    rep_section = raw.get("replication") or {}
    if not isinstance(rep_section, dict):
        raise ConfigError("'replication' must be a mapping")
    rep_roots = rep_section.get("model_roots") or []
    if isinstance(rep_roots, str):
        rep_roots = [rep_roots]
    rep_exclude = rep_section.get("settings_exclude")
    replication = ReplicationConfig(
        enabled=bool(rep_section.get("enabled", False)),
        ssh_command=str(rep_section.get("ssh_command")
                        or ReplicationConfig().ssh_command),
        rsync_command=str(rep_section.get("rsync_command") or "rsync"),
        ssh_user=str(rep_section.get("ssh_user") or ""),
        admin_key=str(rep_section.get("admin_key") or ""),
        model_roots=[str(r) for r in rep_roots],
        free_space_factor=_num(rep_section.get("free_space_factor", 1.05),
                               "replication.free_space_factor", minimum=1.0),
        probe_timeout_seconds=_num(rep_section.get("probe_timeout_seconds", 15.0),
                                   "replication.probe_timeout_seconds", minimum=1.0),
        copy_timeout_seconds=_num(rep_section.get("copy_timeout_seconds", 21600.0),
                                  "replication.copy_timeout_seconds", minimum=60.0),
        settings_exclude=([str(k) for k in rep_exclude]
                          if isinstance(rep_exclude, list)
                          else list(ReplicationConfig().settings_exclude)),
        verify_settings=bool(rep_section.get("verify_settings", True)),
    )
    if "{target}" not in replication.ssh_command:
        # A template without the placeholder would send every copy to whatever
        # host the operator happens to have configured — refuse at start.
        raise ConfigError("replication.ssh_command must contain {target}")

    cfg = Config(
        listen_host=str(raw.get("listen_host", "0.0.0.0")),
        listen_port=_int(raw.get("listen_port", 8010), "listen_port", minimum=1),
        log_level=str(raw.get("log_level", "info")),
        log_file=(str(raw["log_file"]) if raw.get("log_file") else None),
        hosts=hosts, sites=sites, affinity=aff, placement=plc, discovery=dis,
        proxy=prx, auth=auth, replication=replication, stats=stats_cfg,
        fleet=fleet_cfg, source=p,
    )
    if cfg.listen_port > 65535:
        raise ConfigError("listen_port must be <= 65535")
    if dis.unhealthy_after != 3:
        # The key is in the spec (§4) and documented in config.example.yaml, but
        # the pool decision is `placement.flap_guard_failures` — a node is dropped
        # after that many consecutive soft probe failures, not after this one.
        cfg.warnings.append(
            f"discovery.unhealthy_after={dis.unhealthy_after} has no effect — "
            f"placement.flap_guard_failures="
            f"{plc.flap_guard_failures} decides when a host leaves the pool")

    # Visible warnings instead of silent degradation (FA-23, RB-9)
    if missing_env:
        cfg.warnings.append(
            "unset environment variables: " + ", ".join(sorted(set(missing_env))))
    # Replication roots are paths on two different machines (issue #20): `~` is
    # expanded as *this* home, which is right for a source on this machine and
    # wrong for a target. Say what will happen at start, not at the first plan.
    for h in hosts:
        cfg.warnings.extend(_model_root_warnings(
            h.replication.model_roots,
            where=f"host {h.name}: hosts[].replication.model_roots"))
    cfg.warnings.extend(_model_root_warnings(replication.model_roots,
                                             where="replication.model_roots"))
    for h in hosts:
        if h.backend == "omlx" and not h.node_id:
            cfg.warnings.append(
                f"host {h.name}: no node_id configured — address counts as identity "
                "(RB-9 violated, FA-21 cannot verify)")
        if h.backend == "omlx" and not h.api_key:
            cfg.warnings.append(f"host {h.name}: no api_key — client key is passed through")
        if replication.enabled and h.replication.is_configured() \
                and not h.replication.address:
            # Without an address the copy falls back to the fleet's address order,
            # which cannot tell the Thunderbolt cable from the LAN.
            cfg.warnings.append(
                f"host {h.name}: replication has no address — set "
                f"hosts[].replication.address; the fleet's address order prefers "
                f"neither the Thunderbolt link nor the LAN (docs/replication.md §3)")
        if h.warmup:
            # FA-16 is optional and NZ-4 forbids RIPPLE to load models, so the
            # key is accepted but does nothing. Silence here would be exactly the
            # silent degradation the specification rules out.
            cfg.warnings.append(
                f"host {h.name}: warmup {h.warmup} is not implemented "
                "(FA-16 is optional; NZ-4 forbids RIPPLE loading models)")
    if replication.enabled and not replication.admin_key \
            and not any(h.replication.api_key for h in hosts):
        # The admin API needs oMLX's own key; the client key RIPPLE passes
        # through is a different one.
        cfg.warnings.append(
            "replication.enabled without an admin_key — the oMLX admin calls "
            "(models, reload, settings) will answer 401; set replication.admin_key "
            "or hosts[].replication.api_key")

    # Sites (issue #67). The declaration is data, so everything that cannot be
    # checked is said out loud — but an unknown id stays a *warning*: `site: SA`
    # as a plain string must keep parsing, and a host whose location nobody
    # defined is exactly the silent case this issue is about, not a start-up
    # failure.
    for site in sites:
        if not site.networks:
            cfg.warnings.append(
                f"site {site.id}: no networks — a host that names it cannot be "
                f"checked; add the CIDR networks this location contains")
    for left, right in overlapping_pairs(sites):
        cfg.warnings.append(
            f"sites {left} and {right} overlap — an address inside the overlap "
            f"counts as {left} (the first site in the file wins)")
    for h in hosts:
        if not h.site:
            continue
        declared = site_by_id(sites, h.site)
        if declared is None:
            cfg.warnings.append(
                f"host {h.name}: site {h.site!r} is not defined under 'sites' — "
                f"the declared location cannot be checked; define it or leave "
                f"'site' empty")
        elif not declared.networks:
            cfg.warnings.append(
                f"host {h.name}: site {h.site!r} has no networks — the declared "
                f"location cannot be checked against the address RIPPLE uses")

    entries = [h for h in hosts if h.role == "entry" and h.enabled]
    if len(entries) > 1:
        cfg.warnings.append("more than one host with role: entry — roles are documentation only")
    # The values this file declares for the policy paths (issue #78), so a reset
    # at the page falls back to the file and not to the last click.
    cfg.baseline = policy_baseline(cfg)
    return cfg
