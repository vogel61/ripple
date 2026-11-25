"""The test fleet the manual's pictures show (issue #104).

`tools/mockup_snapshot.py` renders the dashboard; the dashboard is filled by
`/healthz`, `/stats` and `/config`, and its Models tab asks `/admin/models` one
extra question — whether a lock can be resident anywhere (issue #126). The real
payloads of a running installation are far too large to write by hand for every
picture — and they would put the operator's machine names, addresses and model
ids into a public manual. This module is the one place where the picture's data
is written down: a small, complete test fleet that answers those endpoints, plus
the admin calls the Models, Nodes and Operations tabs need for their pictures.

The fleet is deliberately fictional and the manual promises exactly that:

    the addresses are documentation addresses (192.0.2.0/24, 198.51.100.0/24),
    the node names are node-a, node-b and node-c, the node ids are invented,
    and the model names, sizes and rates are example values.

Two rules follow from that promise and are checked in `tests/test_mockup_crops.py`:

- **No production identifier.** No host name, no address, no model id and no
  rate is taken from a real installation; the addresses are RFC 5737
  documentation ranges and the names are the ones the manual uses.
- **The state carries the panel's story.** A picture that shows an empty
  History tab, a fleet without a filled tok/s series or a Nodes tab without a
  preview would be a picture of a dashboard nobody has ever run: the payloads
  below carry six conversations, a busy thread, a scan result, a drain
  confirmation and its timeout, a maintenance window, a filled series and a
  model list with not-loaded models behind the fold.

The payload shapes are the ones `ripple/proxy.py` serves (`/healthz`,
`/stats`, `/config`) and the ones `ripple/static/dashboard.js` reads. They were
captured from a running instance and reduced to the keys the page uses; a key
the page adds later is missing here, and that is the failure mode this module
has — which is why the manifest (`docs/images/MANIFEST.md`) names the commit.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from ripple import __version__

GB = 1_000_000_000

# ── the fleet's vocabulary ───────────────────────────────────────────────────
# Four model families. The sizes add up: a node's `used_bytes_measured` is the
# sum of its loaded models' `actual_size`, and `used_bytes` the sum of their
# estimates — a picture where the card says 41.9 GB and the table's rows add up
# to 400 GB is a picture that lies about the one number the block explains.
ATLAS32 = "atlas-32b-oQ4e-mtp"
ATLAS32_Q6 = "atlas-32b-oQ6e-mtp"
ATLAS14 = "atlas-14b-oQ4e"
ATLAS9 = "atlas-9b-oQ4e-mtp"
LUMEN = "lumen-32b-oQ4e"
TIDE = "tide-7b-oQ4e-mtp"
SCRIBE = "scribe-extract"
SCRIBE_V = "scribe-vision"
# The one model no node in this fleet can hold (issue #126): 108.4 GB against a
# 60.8 GB and a 91.2 GB budget, with `atlas-32b-oQ4e-mtp` locked on both nodes
# and therefore not evictable. It exists so the Models tab shows the third
# padlock state — locked, and resident nowhere.
ATLAS140 = "atlas-140b-oQ4e-mtp"

NODE_A = "node-a"
NODE_B = "node-b"
NODE_C = "node-c"

# Addresses from the documentation ranges (RFC 5737). The manual names them.
ADDR_A = "192.0.2.10:8000"
ADDR_B = "198.51.100.11:8000"
ADDR_C = "192.0.2.30:8000"

# Invented node ids: what oMLX reports about itself, and what makes a node a
# node rather than an address.
ID_A = "6f2c1d84-3b7a-4c11-9a20-1f7c0d5b8e01"
ID_B = "b1e04a67-8d92-4f3c-8c55-2a9d7e6f0b12"
ID_C = "3d5a9c02-6e41-4b8f-a7d3-9c1e4f2a6b03"

CEILING_A = 64 * GB
USED_A = int(46.8 * GB)          # oMLX's estimate — what the guard decides on
MEASURED_A = int(41.9 * GB)      # the sum of the models' own `actual_size`
CEILING_B = 96 * GB
USED_B = int(71.2 * GB)
MEASURED_B = int(68.4 * GB)

# `atlas-140b-oQ4e-mtp` — the model behind the third padlock state (#126). It is
# offered on both nodes and resident on neither, and its size is what makes the
# lock a promise the arithmetic cannot keep.
BIG_SIZE = int(108.4 * GB)
HARD_THRESHOLD = 0.95
TARGET_A = HARD_THRESHOLD * CEILING_A / GB       # 60.8 GB
TARGET_B = HARD_THRESHOLD * CEILING_B / GB       # 91.2 GB
# What stays resident because it is locked: `atlas-32b-oQ4e-mtp` on both nodes.
LOCKED_A = 24.6
LOCKED_B = 24.6

TG_A = 61.2      # tok/s, one stream, as the node reports it
TG_B = 44.8
PP_A = 3680.0    # measured cold prefill
PP_B = 970.0

SERIES_BUCKET_S = 5
SERIES_WINDOW_S = 300


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def _series(rate: float, *, points: int = 22, spread: float = 3.4) -> dict:
    """A tok/s series with real samples and the empty buckets between them.

    The node is sampled every 10 s into 5 s buckets, so between two samples
    there is always exactly one empty bucket — `None`, never a 0. The drawing
    code (`seriesPath`, issue #96) carries the pen across one gap and breaks on
    two, which is why the line has to be built from this rhythm and not from a
    dense list: a dense list would draw a line the node never measured.
    """
    total: list[float | None] = []
    per_thread: list[float | None] = []
    counts: list[int | None] = []
    for i in range(points):
        if i % 2:
            total.append(None)
            per_thread.append(None)
            counts.append(None)
            continue
        step = (i // 2) % 6
        # One stream at first, two in the middle, back to one: the total rises
        # while the per-thread rate falls — the shape #96 and #80 are about.
        streams = 1 if step < 2 else (2 if step < 5 else 1)
        if i >= points - 4:
            streams = 0
        if streams == 0:
            total.append(0.0)
            per_thread.append(None)
            counts.append(0)
            continue
        per = round(rate - spread * (streams - 1), 1)
        total.append(round(per * streams, 1))
        per_thread.append(per)
        counts.append(streams)
    return {"model": ATLAS32, "bucket_s": SERIES_BUCKET_S, "window_s": SERIES_WINDOW_S,
            "n": counts, "total": total, "per_thread": per_thread}


def _host(name: str, **row) -> dict:
    """One `/healthz → hosts[]` row, complete: the page reads every key."""
    base = {
        "name": name, "role": "worker", "site": "lab", "weight": 1.0,
        "target_share": 0.5, "enabled": True, "source": "config",
        "address": None, "addresses": [], "state": "healthy", "status": "healthy",
        "in_pool": True, "draining": False, "drain_started_at": None,
        "drain_deadline": None, "drain_remaining_seconds": None,
        "maintenance": None, "loading": None, "healthy": True,
        "node_id": None, "node_id_expected": None, "identity_ok": True,
        "version": "0.8.0", "guard_enabled": True, "hard_threshold": 0.95,
        "ceiling_bytes": None, "used_bytes": None, "used_bytes_measured": None,
        "used_ratio": None, "inflight": 0, "phase": None, "phase_switches": 0,
        "max_inflight": 6, "max_inflight_utility": 6, "inflight_utility": 0,
        "spill_threshold": 4, "thread_slots": 0,
        "max_threads": 20, "target_threads": 0.0, "rtt_ms": None,
        "errors_total": 0, "http_507_total": 0, "requests_total": 0,
        "last_error": None, "last_seen": None, "models_available": 0,
        "models_read_at": None, "models_error": None, "models_loaded": [],
        "capabilities": [], "capability_notes": [], "addr_switches": 0,
        "metrics": {}, "site_name": "", "site_networks": [],
        "site_derived": None, "site_derived_name": "", "site_mismatch": False,
        "site_unconfirmed": False, "requests_by_model": {}, "series": [],
        "capability_rate": None, "relative_load": None, "usage": None,
    }
    base.update(row)
    return base


def _model(mid: str, hosts: list[dict], *, lock: bool = False,
           lock_reason: str | None = None) -> dict:
    return {"id": mid, "lock": lock, "lock_reason": lock_reason, "hosts": hosts}


def _mh(host: str, **row) -> dict:
    """One `/healthz → models[].hosts[]` row."""
    base = {
        "host": host, "available": True, "loaded": False, "estimated_size": 0,
        "actual_size": None, "context_len": None, "vision": False, "threads": 0,
        "inflight": 0, "tg_tps": None, "allowed": True, "policy": None,
        "dispatch": True, "phase": None, "lock": False,
    }
    base.update(row)
    return base


# ── /healthz ────────────────────────────────────────────────────────────────

def _does_not_fit(need_gb: float, target_gb: float, locked_gb: float,
                  evicts: list[str], locked: list[str]) -> str:
    """The preload's refusal sentence, word for word as `preload_forecast` writes it.

    `ripple/hosts.py:preload_forecast` refuses with `does not fit: <n> GB needed,
    <m> GB free`, names what it would have evicted, and names the locked model
    that stopped it. Written from the same numbers the fleet above carries, so
    the tooltip cannot quote a budget the cards do not have.
    """
    free = max(0.0, target_gb - locked_gb)
    reason = f"does not fit: {need_gb:.1f} GB needed, {free:.1f} GB free"
    if evicts:
        reason += f" after evicting {', '.join(evicts)}"
    if locked:
        reason += f"; {', '.join(locked)} is locked and cannot be evicted"
    return reason


# The third padlock state (issue #126): the lock is stored, and no node that
# offers the model could hold it. Both nodes refuse it for the same reason —
# `atlas-32b-oQ4e-mtp` is locked there and may not be evicted to make room.
FIT_REFUSAL_A = _does_not_fit(BIG_SIZE / GB, TARGET_A, LOCKED_A,
                              [ATLAS9, ATLAS14], [ATLAS32])
FIT_REFUSAL_B = _does_not_fit(BIG_SIZE / GB, TARGET_B, LOCKED_B,
                              [ATLAS9, LUMEN], [ATLAS32])

# `feasible` per model, as `GET /admin/models` carries it: `False` only when at
# least one host offers the model and every one of them refuses it. Every other
# model in this fleet fits where it is offered.
FEASIBILITY: dict[str, tuple[bool | None, str | None, str | None]] = {
    ATLAS140: (False, FIT_REFUSAL_A, NODE_A),   # the first blocking host speaks
}

# `fits` per (model, node): `None` where the node does not offer the model.
FIT_REFUSALS: dict[tuple[str, str], str] = {
    (ATLAS140, NODE_A): FIT_REFUSAL_A,
    (ATLAS140, NODE_B): FIT_REFUSAL_B,
}

# The sentence `/healthz` carries for the lock that is in force: the same text
# `_lock_reason` in `ripple/proxy.py` builds for a locked, resident, routed model.
LOCK_NOTE_A32 = (f"{ATLAS32} is locked and resident on {NODE_A}, {NODE_B}: a request "
                 f"that would need its memory is refused with 503 model_locked. "
                 f"Unlock with DELETE /admin/models/{ATLAS32}/lock")

# When the operator set the two locks. Fixed, like every other stamp here: a
# picture must not move because it was taken on another day.
LOCK_SINCE = "2026-11-19T09:41:00"


def healthz(now: float | None = None, commit: str = "0000000",
            dirty: bool = False, date: str | None = None) -> dict:
    """The Fleet tab's whole world: hosts, models, threads, ledger, series.

    `commit` is the repository the pictures were taken from — the page prints it
    in the footer, and a picture that names the wrong commit is a picture of
    another build.
    """
    now = time.time() if now is None else now
    maint_until = _iso(now + 24 * 60)
    drain_started = now - 41

    # `version` kommt aus dem Paket, nicht aus dieser Datei: hier stand "0.1.0",
    # und der README-Hero-Shot trug die Nummer noch, als 0.2.0 längst
    # veröffentlicht war (Betreiberhinweis 21.11.2026). Ein festes Datum neben
    # einer beweglichen Version ist derselbe Fehler in klein — es kommt deshalb
    # aus dem Commit, aus dem die Bilder stammen.
    return {
        "ok": True,
        "version": __version__,
        "build": {"commit": commit,
                  "date": date or datetime.now(timezone.utc).date().isoformat(),
                  "dirty": dirty},
        "uptime_seconds": 10569.3,
        "server_time": round(now, 3),
        "restart": {"supervised": True, "supervisor": "ai.ripple", "reason": None,
                    "inflight": 0, "hosts": [], "pending": False,
                    "delay_seconds": 1.0},
        "load": {
            "started_at": now - 10569.3, "reset_at": None, "since": now - 10569.3,
            "window_seconds": 10569.3, "sampled_seconds": 10530.0,
            "hosts": [
                {"name": NODE_A, "requests": 861, "errors": 2,
                 "prompt_tokens": 2_184_310, "completion_tokens": 184_902,
                 "prefill_seconds": 2140.5, "generation_seconds": 6820.4,
                 "client_seconds": 9012.7, "requests_without_usage": 4,
                 "idle": 4210.0, "loading": 240.0, "prefill": 1180.0,
                 "decode": 4760.0, "mixed": 140.0, "sampled_seconds": 10530.0},
                {"name": NODE_B, "requests": 423, "errors": 1,
                 "prompt_tokens": 741_208, "completion_tokens": 96_431,
                 "prefill_seconds": 3120.8, "generation_seconds": 4180.2,
                 "client_seconds": 7411.9, "requests_without_usage": 1,
                 "idle": 6120.0, "loading": 0.0, "prefill": 900.0,
                 "decode": 3420.0, "mixed": 90.0, "sampled_seconds": 10530.0},
            ],
            "total": {"requests": 1284, "errors": 3, "prompt_tokens": 2_925_518,
                      "completion_tokens": 281_333, "prefill_seconds": 5261.3,
                      "generation_seconds": 11000.6, "client_seconds": 16424.6,
                      "requests_without_usage": 5, "idle": 10330.0,
                      "loading": 240.0, "prefill": 2080.0, "decode": 8180.0,
                      "mixed": 230.0, "sampled_seconds": 21060.0},
            "cross": {
                "range": "today",
                "hosts": [
                    {"name": NODE_A, "requests": 884, "prefill_seconds": 2210.0,
                     "generation_seconds": 6902.0},
                    {"name": NODE_B, "requests": 401, "prefill_seconds": 3098.0,
                     "generation_seconds": 4110.0},
                ],
                "total": {"requests": 1285, "prefill_seconds": 5308.0,
                          "generation_seconds": 11012.0},
                "at": now - 300, "error": None,
            },
        },
        "capabilities": {"availability": True, "residency": True, "capacity": True,
                         "lifecycle": True, "identity": True, "prefix_cache": True,
                         "eviction_guard": True},
        "warnings": [],
        # The models whose lock is stored (#21/#118): one that protects what is
        # resident, one whose promise no node can keep (#126).
        "locks": sorted([ATLAS32, ATLAS140]),
        "fleet": {"overlay_file": "~/.ripple/fleet.yaml", "overlay_persist": True,
                  "overlay_error": None, "overlay_hosts": [],
                  "overlay_overrides": {}, "drain_seconds": 120.0},
        "requests_window_seconds": 900.0,
        "threads": {
            "active": 6,
            "target": {NODE_A: 0.6, NODE_B: 0.4},
            "target_share": {NODE_A: 0.6, NODE_B: 0.4},
            "actual_share": {NODE_A: 0.67, NODE_B: 0.33},
            "registry": [
                # One line per conversation; the busy one is `active: 1` and is
                # the thread the Threads tab's second picture is about.
                {"key": "sid:9f2c41ab", "model": ATLAS32, "host": NODE_A,
                 "source": "header", "kind": "interactive", "key_name": "manual",
                 "turns": 12, "ttl_seconds": 1800, "idle_seconds": 96.0, "active": 0},
                {"key": "sid:5b0e1c4d#fp8", "model": ATLAS32, "host": NODE_A,
                 "source": "header", "kind": "interactive", "key_name": "manual",
                 "turns": 27, "ttl_seconds": 1800, "idle_seconds": 412.0, "active": 0},
                {"key": "sid:c41d7e02", "model": ATLAS9, "host": NODE_B,
                 "source": "header", "kind": "batch", "key_name": "ingest",
                 "turns": 34, "ttl_seconds": 120, "idle_seconds": 18.0, "active": 1},
                {"key": "fp:1a77b3e9", "model": ATLAS32, "host": NODE_B,
                 "source": "fingerprint", "kind": "interactive", "key_name": None,
                 "turns": 3, "ttl_seconds": 1800, "idle_seconds": 244.0, "active": 0},
                {"key": "sid:77aa0d16", "model": ATLAS9, "host": NODE_A,
                 "source": "header", "kind": "one-shot", "key_name": "manual",
                 "turns": 1, "ttl_seconds": 1800, "idle_seconds": 623.0, "active": 0},
                {"key": "fp:0d9e52c8", "model": ATLAS32, "host": NODE_A,
                 "source": "fingerprint", "kind": "interactive", "key_name": None,
                 "turns": 8, "ttl_seconds": 1800, "idle_seconds": 1301.0, "active": 0},
            ],
        },
        "placement": {"binding_strategy": "deficit", "balance_abs_threshold": 1,
                      "balance_rel_threshold": 1.5, "thread_decay_seconds": 300,
                      "rebind": {"enabled": True, "max_per_hour": 4,
                                 "safety_factor": 1.5,
                                 "per_thread_cooldown_seconds": 600,
                                 "rebinds_last_hour": 1}},
        "hosts": [
            _host(
                NODE_A, role="entry", site="hub", site_name="Hub",
                site_networks=["192.0.2.0/24"], weight=3.0, target_share=0.6,
                address=ADDR_A, addresses=["http://" + ADDR_A],
                # Draining: on its way out, two requests still running. That is
                # the state the manual's §5.1 is about, and the drain dialog
                # (§5.6.3) names the same two requests.
                state="draining", status="draining", in_pool=False, draining=True,
                drain_started_at=drain_started,
                drain_deadline=drain_started + 120,
                drain_remaining_seconds=round(drain_started + 120 - now, 1),
                node_id=ID_A, node_id_expected=ID_A,
                version="0.8.0", ceiling_bytes=CEILING_A, used_bytes=USED_A,
                used_bytes_measured=MEASURED_A,
                used_ratio=round(USED_A / CEILING_A, 4),
                inflight=2, phase="tg", phase_switches=7, rtt_ms=7.4,
                thread_slots=3, max_threads=20, target_threads=12.0,
                requests_total=861, errors_total=2, models_available=7,
                models_loaded=[ATLAS32, ATLAS9, ATLAS14, SCRIBE],
                requests_by_model={ATLAS32: 512, ATLAS9: 171, ATLAS14: 178},
                addr_switches=1, capability_rate=TG_A, relative_load=0.71,
                metrics={ATLAS32: {"model": ATLAS32, "tg_tps": TG_A, "tg_samples": 309,
                               "measured_at": round(now - 12, 1)}},
                usage={"samples": 40,
                       "last": {"cached_tokens": 6824, "prompt_tokens": 6940,
                                "prompt_tokens_per_second": 3120.5,
                                "generation_tokens_per_second": TG_A,
                                "time_to_first_token": 0.412}},
                series=[_series(TG_A)],
            ),
            _host(
                NODE_B, role="worker", site="lab", site_name="Lab",
                site_networks=["198.51.100.0/24"], weight=2.0, target_share=0.4,
                address=ADDR_B, addresses=["http://" + ADDR_B],
                # A maintenance window with 24 minutes left: the Ops tab's 6.1
                # sentence and the card's maintenance row are the same fact.
                state="maintenance", status="maintenance", in_pool=False,
                maintenance={"until": maint_until, "minutes_left": 24.0,
                             "by": "operator"},
                node_id=ID_B, node_id_expected=ID_B,
                version="0.8.0", ceiling_bytes=CEILING_B, used_bytes=USED_B,
                used_bytes_measured=MEASURED_B,
                used_ratio=round(USED_B / CEILING_B, 4),
                inflight=0, phase=None, rtt_ms=11.8, thread_slots=2,
                target_threads=8.0, requests_total=423, errors_total=1,
                models_available=5, models_loaded=[ATLAS32, ATLAS9, LUMEN],
                requests_by_model={ATLAS32: 300, LUMEN: 123}, capability_rate=TG_B,
                relative_load=0.28,
                metrics={ATLAS32: {"model": ATLAS32, "tg_tps": TG_B, "tg_samples": 128,
                               "measured_at": round(now - 40, 1)}},
                usage={"samples": 22,
                       "last": {"cached_tokens": 0, "prompt_tokens": 691,
                                "prompt_tokens_per_second": 890.4,
                                "generation_tokens_per_second": TG_B,
                                "time_to_first_token": 1.804}},
                series=[_series(TG_B, spread=2.1)],
            ),
        ],
        "models": [
            # node-a's loaded models sum to 41.9 GB measured / 46.8 GB estimated,
            # node-b's to 68.4 / 71.2 — the two numbers each card's memory bar
            # prints. Change a size here and the bar changes with it.
            # Locked on both nodes: the model the six conversations run on. Its
            # padlock is the ordinary closed one (red, in force) — and it is the
            # model that blocks the big one below, because a locked model is
            # never evicted to make room (#119).
            _model(ATLAS32, [
                _mh(NODE_A, loaded=True, estimated_size=24_600_000_000,
                    actual_size=23_400_000_000, context_len=131072, vision=True,
                    threads=3, inflight=2, tg_tps=TG_A, phase="tg"),
                _mh(NODE_B, loaded=True, estimated_size=24_600_000_000,
                    actual_size=23_400_000_000, context_len=131072, vision=True,
                    threads=2, inflight=0, tg_tps=TG_B),
            ], lock=True, lock_reason=LOCK_NOTE_A32),
            _model(ATLAS9, [
                _mh(NODE_A, loaded=True, estimated_size=7_100_000_000,
                    actual_size=6_500_000_000, context_len=262144, vision=True,
                    threads=1, tg_tps=None),
                _mh(NODE_B, loaded=True, estimated_size=7_100_000_000,
                    actual_size=6_500_000_000, context_len=262144, vision=True,
                    threads=1, tg_tps=None, policy="disabled"),
            ]),
            _model(ATLAS14, [
                _mh(NODE_A, loaded=True, estimated_size=15_100_000_000,
                    actual_size=12_000_000_000, context_len=65536, vision=False,
                    threads=0, tg_tps=None),
            ]),
            _model(LUMEN, [
                _mh(NODE_B, loaded=True, estimated_size=39_500_000_000,
                    actual_size=38_500_000_000, context_len=131072, vision=False,
                    threads=1, tg_tps=None),
                _mh(NODE_A, loaded=False, estimated_size=39_500_000_000,
                    context_len=131072),
            ]),
            _model(SCRIBE, [
                _mh(NODE_A, loaded=True, context_len=None),
            ]),
            # Not loaded anywhere: these are the rows behind the fold
            # (`available, not loaded · n models`), and the reason the Models
            # tab has a fold at all.
            _model(ATLAS32_Q6, [
                _mh(NODE_A, loaded=False, estimated_size=28_100_000_000,
                    context_len=131072, vision=True, dispatch=False, policy="hidden"),
                _mh(NODE_B, loaded=False, estimated_size=28_100_000_000,
                    context_len=131072, vision=True, dispatch=False, policy="hidden"),
            ]),
            # The third padlock state (#126): locked, cold, and too big for
            # either node. `feasible: false` comes from `GET /admin/models` —
            # `/healthz` cannot say it — and the row is where the page paints the
            # dashed padlock with the `!`. Cold on purpose: a lock on a model
            # that is already resident would be a lock that is being kept.
            _model(ATLAS140, [
                _mh(NODE_A, loaded=False, estimated_size=BIG_SIZE,
                    context_len=131072, vision=True),
                _mh(NODE_B, loaded=False, estimated_size=BIG_SIZE,
                    context_len=131072, vision=True),
            ], lock=True),
            _model(TIDE, [
                _mh(NODE_B, loaded=False, estimated_size=4_100_000_000,
                    context_len=131072),
            ]),
            _model(SCRIBE_V, [
                _mh(NODE_A, loaded=False, estimated_size=2_400_000_000,
                    context_len=32768, vision=True),
            ]),
        ],
    }


# ── /stats ──────────────────────────────────────────────────────────────────
def _decision(ts: float, thread: str, model: str, host: str, reason: str,
              prompt_chars: int, duration_ms: float, status: int = 200) -> dict:
    return {"ts": round(ts, 3), "time": _iso(ts)[11:19], "thread": thread,
            "model": model, "host": host, "reason": reason,
            "prompt_chars": prompt_chars, "duration_ms": round(duration_ms, 1),
            "status": status}


def stats(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    # Newest first — that is the order the History tab draws (issue #101).
    # The payload itself is oldest first and the page reverses a copy of it, so
    # the list below is written newest-first for reading and reversed on the way
    # out: `/stats → decisions[]` is a ring buffer in arrival order.
    decisions = [
        _decision(now - 18, "sid:9f2c41ab", ATLAS32, NODE_A, "thread", 1294, 2310.4),
        _decision(now - 44, "sid:c41d7e02", ATLAS9, NODE_B, "thread", 6812, 884.2),
        _decision(now - 71, "sid:5b0e1c4d#fp8", ATLAS32, NODE_A, "thread", 8421, 3120.8),
        _decision(now - 96, "fp:1a77b3e9", ATLAS32, NODE_B, "new_thread", 512, 198.4),
        _decision(now - 118, "sid:c41d7e02", ATLAS9, NODE_B, "thread", 6933, 902.1),
        _decision(now - 143, "sid:77aa0d16", ATLAS9, NODE_A, "one_shot", 87, 64.2),
        _decision(now - 167, "sid:9f2c41ab", ATLAS32, NODE_A, "thread", 1187, 2211.9),
        _decision(now - 191, "fp:0d9e52c8", ATLAS32, NODE_A, "new_thread", 2048, 640.5),
        _decision(now - 214, "sid:5b0e1c4d#fp8", ATLAS32, NODE_A, "rebind", 8204, 3260.7),
        _decision(now - 238, "fp:1a77b3e9", ATLAS32, NODE_B, "fallback", 498, 1204.3,
                  502),
        _decision(now - 262, "sid:c41d7e02", ATLAS9, NODE_B, "thread", 6720, 871.6),
        _decision(now - 287, "sid:77aa0d16", ATLAS9, NODE_A, "ephemeral", 92, 71.8),
        _decision(now - 311, "sid:9f2c41ab", ATLAS32, NODE_A, "slot_evict", 1104, 2188.4),
        _decision(now - 336, "fp:0d9e52c8", ATLAS32, NODE_A, "thread", 2033, 628.9),
        _decision(now - 361, "sid:c41d7e02", ATLAS9, NODE_B, "model_pinned", 7004, 940.2),
        _decision(now - 388, "sid:5b0e1c4d#fp8", ATLAS32, NODE_A, "thread", 8100, 3044.1),
        _decision(now - 412, "fp:1a77b3e9", ATLAS32, NODE_B, "new_thread", 507, 201.7),
        _decision(now - 437, "sid:9f2c41ab", ATLAS32, NODE_A, "thread", 1211, 2298.0),
    ]
    return {
        "uptime_seconds": 10569.3,
        "requests_total": 1284, "requests_ok": 1281, "errors_total": 3,
        "http_503_total": 1, "http_507_total": 0,
        "threads_new": 47, "thread_hits": 812, "threads_evicted": 3,
        "threads_evicted_batch": 0, "threads_promoted": 5,
        "threads_rebound": 9, "threads_deleted": 2, "requests_one_shot": 64,
        "auth_failures": 0, "admin_key_blocks": 0,
        "by_key": {"manual": 702, "ingest": 431},
        "prefix_matches": 611, "spillover": 18, "fallbacks": 1,
        "addr_switches": 1, "evict_idle": 3, "cold_load": 4, "prefill_bias": 12,
        "loads_supervised": 4, "probes_skipped": 0, "loads_failed": 0,
        "degraded_pool_decisions": 0, "calibrations_started": 1,
        "calibrations_ok": 1, "calibrations_forced": 0,
        "by_host": {NODE_A: {"requests": 861, "errors": 2},
                    NODE_B: {"requests": 423, "errors": 1}},
        "by_model": {ATLAS32: {"requests": 990, "errors": 2},
                     ATLAS14: {"requests": 178, "errors": 0},
                     LUMEN: {"requests": 123, "errors": 1}},
        "decisions": list(reversed(decisions)),
        "threads": [],
        "requests_model_locked": 0,
        "host_stats": {},
    }


# ── /config ─────────────────────────────────────────────────────────────────
def config() -> dict:
    view = {
        "source": "config.yaml",
        "listen": "127.0.0.1:8010",
        "log_level": "info",
        "log_file": "~/Library/Logs/ripple.log",
        "sites": [
            {"id": "hub", "name": "Hub", "networks": ["192.0.2.0/24"]},
            {"id": "lab", "name": "Lab", "networks": ["198.51.100.0/24"]},
        ],
        "hosts": [
            {"name": NODE_A, "role": "entry", "source": "config", "site": "hub",
             "site_name": "Hub", "site_networks": ["192.0.2.0/24"],
             "backend": "omlx", "enabled": True, "weight": 3.0, "share": 0.6,
             "addresses": ["http://" + ADDR_A], "node_id": ID_A,
             "max_inflight": 6, "max_inflight_utility": 6,
             "max_threads": 20, "spill_threshold": 4,
             "hard_threshold": 0.95, "prefer_resident": True, "pp_tps": PP_A,
             "tg_tps": TG_A, "models": [], "api_key": "set",
             "replication": {"address": "not configured", "ssh_user": "not configured",
                             "model_roots": [], "api_key": "none"}},
            {"name": NODE_B, "role": "worker", "source": "config", "site": "lab",
             "site_name": "Lab", "site_networks": ["198.51.100.0/24"],
             "backend": "omlx", "enabled": True, "weight": 2.0, "share": 0.4,
             "addresses": ["http://" + ADDR_B], "node_id": ID_B,
             "max_inflight": 6, "max_inflight_utility": 6,
             "max_threads": 20, "spill_threshold": 3,
             "hard_threshold": 0.95, "prefer_resident": True, "pp_tps": PP_B,
             "tg_tps": TG_B, "models": [], "api_key": "set",
             "replication": {"address": "not configured", "ssh_user": "not configured",
                             "model_roots": [], "api_key": "none"}},
        ],
        "affinity": {"header": "X-Session-Id", "ttl_seconds": 1800,
                     "batch_ttl_seconds": 120, "prefix_chars": 4096,
                     "match_threshold": 0.5, "min_prompt_tokens": 8192,
                     "ephemeral_window_seconds": 60},
        "auth": {"enabled": True, "keys_file": "~/.ripple/keys.yaml",
                 "admin_key": "set", "allow_loopback": False},
        "discovery": {"interval_seconds": 10.0, "timeout_seconds": 5.0,
                      "unhealthy_after": 3, "warmup_timeout_seconds": 300.0},
        "editable": ["hosts.*.weight", "hosts.*.spill_threshold",
                     "hosts.*.prefer_resident", "affinity.ttl_seconds",
                     "affinity.match_threshold", "affinity.ephemeral_window_seconds",
                     "placement.rebind.enabled", "placement.rebind.max_per_hour",
                     "placement.rebind.safety_factor",
                     "placement.rebind.per_thread_cooldown_seconds",
                     "placement.balance_abs_threshold",
                     "placement.balance_rel_threshold"],
        "fleet": {"file": "~/.ripple/fleet.yaml", "persist": True,
                  "drain_seconds": 120.0, "drain_poll_seconds": 0.25},
        "overrides": {},
        "placement": {"binding_strategy": "deficit", "balance_abs_threshold": 1.0,
                      "balance_rel_threshold": 1.5,
                      "rebind": {"enabled": True, "min_idle_seconds": 0.0,
                                 "per_thread_cooldown_seconds": 600.0,
                                 "max_per_hour": 4, "safety_factor": 1.5,
                                 "min_gain_seconds": 2.0,
                                 "assumed_completion_tokens": 400,
                                 "default_pp_tps": 0.0},
                      "optimizer": {"enabled": True, "interval_seconds": 30.0,
                                    "max_moves_per_hour": 4},
                      "migrate": {"enabled": True, "band_samples": 5,
                                  "return_factor": 2.0, "max_moves_per_thread": 2,
                                  "window_seconds": 3600.0},
                      "prefill_bias": {"enabled": True, "slot_seconds": 60.0,
                                       "max_slots": 3.0, "min_prompt_tokens": 4096},
                      "loading": {"enabled": True, "safety_factor": 1.3,
                                  "max_seconds": 900.0,
                                  "calibration_file": "~/.ripple/calibration.yaml",
                                  "samples": 3},
                      "flap_guard_failures": 3, "thread_decay_seconds": 300.0,
                      "maintenance_file": "~/.ripple/maintenance.yaml",
                      "model_policy_file": "~/.ripple/models.yaml"},
        "proxy": {"connect_timeout_seconds": 10.0, "retry_other_node": True,
                  "request_log": True, "trusted_proxies": []},
        "replication": {"enabled": False,
                        "ssh_command": "ssh -o BatchMode=yes -o ConnectTimeout=8 {target}",
                        "rsync_command": "rsync", "ssh_user": "not configured",
                        "admin_key": "none", "model_roots": [],
                        "free_space_factor": 1.05,
                        "settings_exclude": ["display_name", "description",
                                             "active_profile_name"],
                        "verify_settings": True},
        "stats": {"file": "~/.ripple/host_stats.json", "persist": True,
                  "flush_seconds": 30.0},
        "warnings": [],
    }
    # Die Anteile werden gerechnet, nicht getippt: `weight / sum(weight)` über
    # die Knoten der Flotte — genau `Config.target_share`. Eine Zahl, die von
    # Hand neben einem Gewicht steht, ist die Art Zeile, die still falsch wird;
    # im Handbuchbild stand `node-c · 1 → 0.0 %` (24.11.2026), und ein Knoten,
    # der nicht in der Flotte ist, gehört ohnehin nicht in diese Tabelle.
    enabled = [h for h in view["hosts"] if h["enabled"]]
    total = sum(h["weight"] for h in enabled) or 1.0
    for host in view["hosts"]:
        host["share"] = round(host["weight"] / total, 4) if host["enabled"] else 0.0
    return view


# ── GET /admin/models ───────────────────────────────────────────────────────
def models_view(now: float | None = None) -> dict:
    """`GET /admin/models`: the lock view, including whether a lock can be paid.

    This is the one payload `/healthz` does not carry, and the page reads exactly
    three fields out of it per row — `feasible`, `feasible_reason`,
    `feasible_host` (issue #126). They are the preload's own forecast
    (`ripple/proxy.py:_lock_feasibility`), which is why a model that no node can
    hold says so *before* the operator clicks, and why the Models tab paints the
    third padlock state — locked, dashed outline, `!` in the body.

    Built from `healthz()` so the two payloads cannot disagree about which model
    is locked or where it is loaded; only the forecast is written down here, in
    `FEASIBILITY` and `FIT_REFUSALS`.
    """
    health = healthz(now)
    rows = []
    for model in health["models"]:
        mid = model["id"]
        lock = bool(model.get("lock"))
        feasible, reason, host = FEASIBILITY.get(mid, (True, None, None))
        rows.append({
            "id": mid,
            "lock": lock,
            "builtin": False,
            "lockable": True,
            "since": LOCK_SINCE if lock else None,
            "by": "operator" if lock else None,
            "note": "",
            # Where the lock protects something right now.
            "loaded_on": sorted(x["host"] for x in model["hosts"] if x["loaded"]),
            "lock_reason": model.get("lock_reason"),
            # Whether the promise can be delivered at all (#126).
            "feasible": feasible,
            "feasible_reason": reason,
            "feasible_host": host,
            "listed": any(x.get("available") and x.get("dispatch", True)
                          for x in model["hosts"]),
            "hosts": [{
                "host": x["host"],
                "offered": True,
                "loaded": bool(x["loaded"]),
                # A padlock per node: the lock protects *this* node's copy.
                "lock": bool(lock and x["loaded"]),
                "policy": x.get("policy"),
                "dispatch": bool(x.get("dispatch", True)),
                "routable": bool(x["loaded"] and x.get("dispatch", True)),
                "fits": (mid, x["host"]) not in FIT_REFUSALS,
                "fit_reason": FIT_REFUSALS.get((mid, x["host"])),
            } for x in model["hosts"]],
        })
    return {"locks": sorted(r["id"] for r in rows if r["lock"]), "models": rows}


# ── the admin calls the Nodes and Operations tabs need ──────────────────────
def node_preview() -> dict:
    """`POST /admin/nodes?dry_run=true` for node-c — the machine that is only
    ever previewed (manual §5.6.1)."""
    return {
        "address": "http://" + ADDR_C, "name": NODE_C, "node_id": ID_C,
        "models": [ATLAS32, ATLAS9, ATLAS14, LUMEN, SCRIBE, TIDE, SCRIBE_V],
        "model_count": 7, "models_error": None,
        "used_bytes": int(38.6 * GB), "ceiling_bytes": CEILING_A,
        "rtt_ms": 3.2, "site": "hub", "site_name": "Hub",
        "site_derived": "hub", "site_derived_name": "Hub",
        "site_mismatch": False, "weight": 1, "role": "worker",
        "step": None, "dry_run": True,
    }


def discover() -> dict:
    """`POST /admin/discover` over a documentation range (manual §5.6.2)."""
    return {
        "cidr": "192.0.2.0/28", "port": 8000, "count": 2,
        "found": [
            {"address": "http://192.0.2.10:8000", "name": NODE_A, "node_id": ID_A,
             "model_count": 7, "latency_ms": 7.1, "site_derived": "hub",
             "known": True, "known_nodes": [NODE_A]},
            {"address": "http://192.0.2.30:8000", "name": NODE_C, "node_id": ID_C,
             "model_count": 7, "latency_ms": 3.2, "site_derived": "hub",
             "known": False, "known_nodes": []},
        ],
        "scanned": 14, "error": None,
    }


def maintenance_set(now: float | None = None) -> dict:
    """`POST /admin/nodes/node-b/maintenance?minutes=30` — the sentence the
    Operations tab prints (§6.1) names the window it just set."""
    now = time.time() if now is None else now
    return {"node": NODE_B, "in_pool": False, "inflight": 0,
            "maintenance": {"until": _iso(now + 24 * 60), "minutes_left": 24.0,
                            "by": "operator"}}


def drain_timeout() -> dict:
    """`DELETE /admin/nodes/node-a` answering `409 drain_timeout` (§5.6.3).

    Nothing is removed: the node stays in the fleet and is disabled, and the
    dialog keeps the fresh numbers. The message is the server's own sentence,
    which the page shows as text and as tooltip.
    """
    return {"error": {
        "code": "drain_timeout",
        "message": "drain timed out after 120 s with 2 request(s) still in "
                   "flight; node-a stays in the fleet, disabled",
        "inflight": 2, "deadline_seconds": 120,
        "models": [{"model": ATLAS32, "inflight": 2}],
        "node": NODE_A,
    }}


def keys() -> dict:
    """`GET /admin/keys` for the key page (manual §5.7). Masks only: a page that
    can show a stored secret is a page that can leak it."""
    return {"keys": [
        {"name": "manual", "key": "rip-4c1d…9f02", "kind": "interactive",
         "models": [], "created_at": "2026-11-02T09:14:00", "enabled": True},
        {"name": "ingest", "key": "rip-a7e3…11bc", "kind": "batch",
         "models": [ATLAS9], "created_at": "2026-11-05T17:42:00", "enabled": True},
        {"name": "notebook", "key": "rip-2f80…c4d1", "kind": "interactive",
         "models": [ATLAS32, ATLAS9], "created_at": "2026-11-11T08:03:00", "enabled": True},
        {"name": "old-laptop", "key": "rip-9b12…7e6a", "kind": "interactive",
         "models": [], "created_at": "2026-10-28T21:19:00", "enabled": False},
    ]}


# ── what the shim answers ───────────────────────────────────────────────────
# One entry per (method, path). `q` narrows on the query string; the Nodes tab
# calls the same path for a dry run and for the write, and the picture is the
# dry run.
def endpoints(now: float | None = None, commit: str = "0000000",
              dirty: bool = False, date: str | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return [
        {"m": "GET", "p": "/healthz", "s": 200,
         "b": healthz(now, commit=commit, dirty=dirty, date=date)},
        {"m": "GET", "p": "/stats", "s": 200, "b": stats(now)},
        {"m": "GET", "p": "/config", "s": 200, "b": config()},
        # The lock view: the third padlock state lives here, not in /healthz (#126).
        {"m": "GET", "p": "/admin/models", "s": 200, "b": models_view(now)},
        {"m": "GET", "p": "/admin/keys", "s": 200, "b": keys()},
        {"m": "GET", "p": "/admin/nodes/%s/models" % NODE_A, "s": 200,
         "b": {"node": NODE_A, "models": []}},
        {"m": "GET", "p": "/admin/nodes/%s/models" % NODE_B, "s": 200,
         "b": {"node": NODE_B, "models": []}},
        {"m": "POST", "p": "/admin/nodes", "q": "dry_run", "s": 200,
         "b": node_preview()},
        {"m": "POST", "p": "/admin/discover", "s": 200, "b": discover()},
        {"m": "POST", "p": "/admin/nodes/%s/maintenance" % NODE_B, "s": 200,
         "b": maintenance_set(now)},
        {"m": "DELETE", "p": "/admin/nodes/%s" % NODE_A, "s": 409,
         "b": drain_timeout()},
    ]


def shim_script(now: float | None = None, commit: str = "0000000",
                dirty: bool = False, date: str | None = None) -> str:
    """The JavaScript that answers the page's fetches from `endpoints()`.

    Installed with `Page.addScriptToEvaluateOnNewDocument`, so it runs before
    `dashboard.js` on every navigation: the page never sees the instance's own
    (empty) fleet. Everything that does not match falls through to the real
    `fetch`, which is how the style sheet, the catalogs and the fonts still
    come from the instance.

    `confirm()` answers yes: the pictures show the state after the operator
    said yes, and a headless browser would otherwise dismiss every dialog.
    """
    import json
    table = json.dumps(endpoints(now, commit, dirty, date), ensure_ascii=False)
    return """
(() => {
  const TABLE = %s;
  const real = window.fetch ? window.fetch.bind(window) : null;
  const find = (m, path, search) => {
    for (const e of TABLE) {
      if (e.m !== m || e.p !== path) continue;
      if (e.q && search.indexOf(e.q) === -1) continue;
      return e;
    }
    return null;
  };
  window.fetch = function (input, init) {
    let url, method;
    if (typeof input === 'string') { url = input; method = (init && init.method) || 'GET'; }
    else { url = (input && input.url) || ''; method = (init && init.method) ||
           (input && input.method) || 'GET'; }
    method = String(method).toUpperCase();
    let u = null;
    try { u = new URL(url, location.href); } catch (e) { u = null; }
    const hit = u ? find(method, u.pathname, u.search) : null;
    if (!hit) return real ? real(input, init) : Promise.reject(new Error('no fetch'));
    return Promise.resolve(new Response(JSON.stringify(hit.b), {
      status: hit.s, headers: {'Content-Type': 'application/json'}}));
  };
  window.confirm = () => true;
  window.alert = () => undefined;
})();
""" % table
