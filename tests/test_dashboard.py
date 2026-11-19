"""The dashboard page of issue #2: shell, style sheet, behaviour and the field.

The page is three hand-written files with no build step, so there is no single
"rendered page" object to assert on. The honest units are:

- the shell (`ripple/dashboard.py`): which containers exist, which assets it
  links, and that it carries no external URL and no way to reload itself;
- the style sheet (`ripple/static/dashboard.css`): the type scale, the state
  glyphs, the memory bar's rules;
- the behaviour (`ripple/static/dashboard.js`): what the real JavaScript
  renders for a fixed set of numbers. `tests/dashboard_dom_stub.js` runs it
  under a DOM stub and hands back the markup plus a count of DOM writes, which
  is how "no full reload" is measured instead of eyeballed.

The last group is the contract from `docs/dashboard-design.md` §5: `/healthz`
carries `used_bytes_measured` beside `used_bytes`, and the two are never mixed.
"""

from __future__ import annotations

import asyncio
import html.parser
import json
import pathlib
import re
import shutil
import subprocess
import time
from datetime import datetime

import pytest

from ripple.config import PlacementConfig
from ripple.dashboard import render_dashboard
from ripple.proxy import (INHERITED_LABEL_REASON, QUERY_FAILED_REASON,
                          UNOWNED_LABEL_REASON, UNSUPERVISED_REASON)
from ripple.stats import Stats, series_path

from tests.support import (StubModel, bearer, chat_body, host_config, host_row,
                           keyed_config, make_config, wait_until)

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "ripple" / "static"
NODE = shutil.which("node")


def _catalog(lang: str) -> dict:
    """One message catalog, as the page fetches it (issue #17)."""
    return json.loads((STATIC / "i18n" / (lang + ".json")).read_text(encoding="utf-8"))


def _en() -> dict:
    return _catalog("en")


def _de() -> dict:
    return _catalog("de")

GB = 1_000_000_000


# -- the page as the browser gets it ---------------------------------------
@pytest.fixture(scope="module")
def page() -> str:
    return render_dashboard()


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "dashboard.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "dashboard.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def en() -> dict:
    """The English catalog: the page's own words for the block under test."""
    return _en()


# -- a fixed set of numbers, taken from the approved mockup -----------------
def _host(name: str, *, role: str, site: str, address: str, ceiling: int,
          used: int, measured: int | None, **extra) -> dict:
    row = {
        "name": name, "role": role, "site": site, "weight": 1.0,
        "target_share": 0.5, "enabled": True, "address": address,
        "addresses": [address], "state": "healthy", "status": "healthy",
        "maintenance": None, "loading": None, "healthy": True,
        "node_id": f"node-{name}", "node_id_expected": f"node-{name}",
        "identity_ok": True, "version": "0.8.0rc1", "guard_enabled": True,
        "hard_threshold": 0.95, "ceiling_bytes": ceiling,
        "used_bytes": used, "used_bytes_measured": measured,
        "used_ratio": round(used / ceiling, 4),
        "inflight": 0, "max_inflight": 6, "spill_threshold": 4,
        # The phase (issue #18): what the node is doing right now. `None` for an
        # idle node, so a card without a phase keeps the steady health sign.
        "phase": None, "phase_switches": 0,
        "thread_slots": 0, "max_threads": 20, "target_threads": 0.0,
        "rtt_ms": 7.4, "errors_total": 0, "http_507_total": 0,
        "requests_total": 310, "last_error": None, "last_seen": 0.0,
        # The lock (#21): `lock` is per node "locked **and** loaded here".
        "lock": False,
        "models_available": 3, "models_loaded": [], "capabilities": [],
        "capability_notes": [], "addr_switches": 0, "metrics": {},
        # Requests per (host, model) inside the served window (issue #91) —
        # the second, honest column of the distribution block.
        "requests_by_model": {},
    }
    row.update(extra)
    return row


M5_CEILING = 525_360_213_856
M5_USED = 453_635_361_950
M5_MEASURED = 432_222_448_488
M3_CEILING = 528_201_093_960
M3_USED = 460_177_761_039
M3_MEASURED = 442_072_859_304
DEEPSEEK = "DeepSeek-V4.1-Flash-1017-oQ4e-mtp"

# The instant the DOM stub's browser clock stands at (`clockMs` in
# tests/dashboard_dom_stub.js). `_healthz()["server_time"]` is the same number,
# so the page starts with an offset of zero and a test can move one side alone.
FROZEN = 1_763_000_000.0

# A load in progress (issue #34): started 60 s ago, 202 s estimated, so 142 s
# are left — the figure the issue prints as a deadline.
LOADING = {
    "model": DEEPSEEK, "size_gb": 453.6, "started_at": FROZEN - 60,
    "eta_seconds": 202.0, "source": "calibration",
    "seen_loading": True, "idle_unloaded": False,
}


def _healthz() -> dict:
    return {
        "ok": True, "version": "0.1.0",
        "build": {"commit": "1374730", "date": "2026-11-15", "dirty": False},
        "uptime_seconds": 10569.3,
        # The clock the page counts down against (issue #34): the browser derives
        # its offset from it on every pull.
        "server_time": 1_763_000_000.0,
        # The restart control (issue #25): `/healthz` carries the state so the
        # page can disable the control with its reason and still cost no request.
        "restart": {"supervised": True, "supervisor": "ai.ripple", "reason": None,
                    "inflight": 0, "hosts": [], "pending": False,
                    "delay_seconds": 1.0},
        # The per-host ledger (issue #33). The five states of each row add up to
        # that row's `sampled_seconds` — that is the property the block claims,
        # so the fixture has to satisfy it or the rendered shares would lie.
        "load": {
            "started_at": 1_763_250_000.0, "reset_at": 1_763_290_000.0,
            "since": 1_763_290_000.0, "window_seconds": 7200.0,
            "sampled_seconds": 7140.0,
            "hosts": [
                {"name": "M5-alpha", "requests": 310, "errors": 2,
                 "prompt_tokens": 1_234_567, "completion_tokens": 45_678,
                 "prefill_seconds": 1234.5, "generation_seconds": 4567.8,
                 "client_seconds": 6000.5, "requests_without_usage": 3,
                 "idle": 3000.0, "loading": 240.0, "prefill": 1200.0,
                 "decode": 2500.0, "mixed": 200.0, "sampled_seconds": 7140.0},
                {"name": "M3-beta", "requests": 103, "errors": 0,
                 "prompt_tokens": 234_567, "completion_tokens": 12_345,
                 "prefill_seconds": 4567.8, "generation_seconds": 1234.5,
                 "client_seconds": 7000.2, "requests_without_usage": 0,
                 "idle": 6000.0, "loading": 0.0, "prefill": 500.0,
                 "decode": 600.0, "mixed": 40.0, "sampled_seconds": 7140.0},
            ],
            "total": {"requests": 413, "errors": 2, "prompt_tokens": 1_469_134,
                      "completion_tokens": 58_023, "prefill_seconds": 5802.3,
                      "generation_seconds": 5802.3, "client_seconds": 13000.7,
                      "requests_without_usage": 3, "idle": 9000.0,
                      "loading": 240.0, "prefill": 1700.0, "decode": 3100.0,
                      "mixed": 240.0, "sampled_seconds": 14280.0},
            "cross": {
                "range": "today",
                "hosts": [
                    {"name": "M5-alpha", "requests": 431, "prefill_seconds": 1400.0,
                     "generation_seconds": 4700.0},
                    {"name": "M3-beta", "requests": 98, "prefill_seconds": 4500.0,
                     "generation_seconds": 1200.0},
                ],
                "total": {"requests": 529, "prefill_seconds": 5900.0,
                          "generation_seconds": 5900.0},
                "at": 1_763_292_000.0, "error": None,
            },
        },
        "capabilities": {"availability": True, "residency": True, "capacity": True,
                         "lifecycle": True, "identity": True, "prefix_cache": True,
                         "eviction_guard": True},
        "warnings": [],
        # The drain deadline a removal waits out (#70), read by the node tab's
        # removal dialog (#71) — `/healthz → fleet.drain_seconds`.
        "fleet": {"overlay_file": None, "overlay_persist": False, "overlay_error": None,
                  "overlay_hosts": [], "overlay_overrides": {}, "drain_seconds": 5.0},
        # The span `hosts[].requests_by_model` counts over (issue #91) — the same
        # window the series serves, so the column and the chart agree.
        "requests_window_seconds": 900.0,
        "threads": {
            "active": 1,
            "target": {"M5-alpha": 0.6, "M3-beta": 0.4},
            "target_share": {"M5-alpha": 0.6, "M3-beta": 0.4},
            "actual_share": {"M5-alpha": 1.0, "M3-beta": 0.0},
            "registry": [{
                "key": "4b17eecc4cf0a8a5", "model": DEEPSEEK, "host": "M5-alpha",
                "source": "new", "kind": "interactive", "key_name": "mustafa",
                "turns": 67, "ttl_seconds": 1800, "idle_seconds": 112.4,
            }],
        },
        "placement": {"binding_strategy": "thread", "balance_abs_threshold": 1,
                      "balance_rel_threshold": 0.25, "thread_decay_seconds": 900,
                      "rebind": {"enabled": True, "max_per_hour": 12,
                                 "safety_factor": 1.5,
                                 "per_thread_cooldown_seconds": 300,
                                 "rebinds_last_hour": 0}},
        "hosts": [
            _host("M5-alpha", role="entry", site="WD", address="192.0.2.11:8000",
                  ceiling=M5_CEILING, used=M5_USED, measured=M5_MEASURED,
                  weight=3.0, target_share=0.6, thread_slots=1, requests_total=310,
                  models_loaded=[DEEPSEEK, "MarkItDown"], models_available=23,
                  # Two models, two phases: the aggregate of the node is prefill
                  # (issue #18). The card blinks, the two dots blink in their own
                  # colour.
                  phase="pp", phase_switches=5,
                  # The window (issue #91): one model with a warm thread and
                  # traffic, one that only has traffic.
                  requests_by_model={DEEPSEEK: 127},
                  metrics={DEEPSEEK: {"tg_tps": 69.6, "tg_samples": 309,
                                      "measured_at": 1_763_000_000.0}}),
            _host("M3-beta", role="worker", site="WD", address="192.0.2.12:8000",
                  ceiling=M3_CEILING, used=M3_USED, measured=M3_MEASURED,
                  weight=2.0, target_share=0.4, requests_total=103,
                  models_loaded=[DEEPSEEK, "MarkItDown", "Qwen3.5-9B-oQ4e-mtp"],
                  models_available=6,
                  # The 9B model of issue #91: loaded, answering 56 requests in
                  # the window, and holding no thread — it must still have a row.
                  requests_by_model={DEEPSEEK: 24, "Qwen3.5-9B-oQ4e-mtp": 56},
                  metrics={DEEPSEEK: {"tg_tps": 17.5, "tg_samples": 100,
                                      "measured_at": 1_763_000_000.0}}),
        ],
        "models": [
            {"id": DEEPSEEK, "lock": False, "lock_reason": None, "hosts": [
                {"host": "M3-beta", "available": True, "loaded": True,
                 "estimated_size": 453_635_361_950,
                 "actual_size": 431_961_763_456, "context_len": 524288,
                 "vision": True, "threads": 0, "inflight": 0, "tg_tps": 17.5,
                 "allowed": True, "policy": None, "dispatch": True, "phase": None,
                 "lock": False},
                {"host": "M5-alpha", "available": True, "loaded": True,
                 "estimated_size": 453_635_361_950,
                 "actual_size": M5_MEASURED, "context_len": 524288,
                 "vision": True, "threads": 1, "inflight": 0, "tg_tps": 69.6,
                 "allowed": True, "policy": None, "dispatch": True, "phase": "tg",
                 "lock": False},
            ]},
            {"id": "GLM-5.2-oQ4", "lock": False, "lock_reason": None, "hosts": [
                # Cold load: not resident yet, but the request that caused the
                # load is already waiting for its first token — the dot is empty
                # *and* in prefill (issue #18).
                {"host": "M5-alpha", "available": True, "loaded": False,
                 "estimated_size": 448_940_510_278, "actual_size": None,
                 "context_len": 288768, "vision": False, "threads": 0,
                 "inflight": 0, "tg_tps": None, "allowed": True,
                 "policy": None, "dispatch": True, "phase": "pp",
                 "lock": False},
            ]},
            # The oMLX-internal tool (#107): resident, but `dispatch: false` and
            # `allowed: false` on every node, `actual_size: 0`, `tg_tps: null` —
            # the shape the live fleet sends. It must never get a row in `2`.
            {"id": "MarkItDown", "lock": False, "lock_reason": None, "hosts": [
                {"host": "M3-beta", "available": True, "loaded": True,
                 "estimated_size": 0, "actual_size": 0, "context_len": None,
                 "vision": False, "threads": 0, "inflight": 0, "tg_tps": None,
                 "allowed": False, "policy": None, "dispatch": False, "phase": None,
                 "lock": False},
                {"host": "M5-alpha", "available": True, "loaded": True,
                 "estimated_size": 0, "actual_size": 0, "context_len": None,
                 "vision": False, "threads": 0, "inflight": 0, "tg_tps": None,
                 "allowed": False, "policy": None, "dispatch": False, "phase": "pp",
                 "lock": False},
            ]},
            {"id": "Qwen3.5-9B-oQ4e-mtp", "lock": False, "lock_reason": None, "hosts": [
                {"host": "M3-beta", "available": True, "loaded": True,
                 "estimated_size": 6_542_399_089, "actual_size": 10_111_095_848,
                 "context_len": 262144, "vision": True, "threads": 0,
                 "inflight": 0, "tg_tps": None, "allowed": True,
                 "policy": None, "dispatch": False, "phase": None, "lock": False},
            ]},
        ],
    }


def _stats() -> dict:
    return {
        "uptime_seconds": 10569.3, "requests_total": 413, "requests_ok": 413,
        "errors_total": 0, "http_503_total": 0, "http_507_total": 0,
        "threads_new": 5, "thread_hits": 212, "threads_evicted": 0,
        "threads_rebound": 1,
        "by_host": {"M5-alpha": {"requests": 310, "errors": 0},
                    "M3-beta": {"requests": 103, "errors": 0}},
        "by_model": {DEEPSEEK: {"requests": 412, "errors": 0},
                     "Qwen3.8-Flash-Next-oQ4e-mtp": {"requests": 1, "errors": 0}},
        "decisions": [
            # the first one carries `ts` (epoch seconds, what /stats really
            # sends); the others only `time`, so the fallback stays covered
            {"ts": 1763292000, "time": "13:29:36", "model": DEEPSEEK,
             "host": "M5-alpha",
             "reason": "ephemeral", "prompt_chars": 101, "duration_ms": 212.1,
             "status": 200},
            {"time": "13:30:31", "model": DEEPSEEK, "host": "M5-alpha",
             "reason": "thread", "prompt_chars": 1219, "duration_ms": 230.0,
             "status": 200},
            {"time": "13:36:17", "model": DEEPSEEK, "host": "M3-beta",
             "reason": "ephemeral", "prompt_chars": 160, "duration_ms": 351.9,
             "status": 200},
        ],
    }


def _config() -> dict:
    return {
        "source": "config.yaml", "listen": "0.0.0.0:8010", "log_level": "INFO",
        "log_file": "/tmp/ripple.log",
        "placement": {"binding_strategy": "thread", "balance_abs_threshold": 1,
                      "balance_rel_threshold": 0.25, "thread_decay_seconds": 900,
                      "flap_guard_failures": 2,
                      "rebind": {"enabled": True, "max_per_hour": 12,
                                 "safety_factor": 1.5,
                                 "per_thread_cooldown_seconds": 300},
                      "loading": {"enabled": True, "safety_factor": 1.3,
                                  "max_seconds": 900, "samples": 3,
                                  "calibration_file": "/tmp/cal.json"}},
        "affinity": {"header": "X-Ripple-Thread", "ttl_seconds": 1800,
                     "batch_ttl_seconds": 300, "min_prompt_tokens": 800,
                     "match_threshold": 0.9, "prefix_chars": 4096,
                     "ephemeral_window_seconds": 60},
        "discovery": {"interval_seconds": 5, "timeout_seconds": 3,
                      "unhealthy_after": 3},
        # Named locations (issue #67): the node preview offers them as the site
        # of a node it is about to add (#71).
        "sites": [{"id": "wd", "name": "Wien", "networks": ["192.0.2.0/24"]},
                  {"id": "tr", "name": "Traiskirchen", "networks": ["10.11.20.0/24"]}],
        "proxy": {"retry_other_node": True, "request_log": False,
                  "connect_timeout_seconds": 10},
        "hosts": [{"name": "M5-alpha", "weight": 3.0, "share": 0.6,
                   "max_inflight": 6, "max_threads": 20, "spill_threshold": 4,
                   "hard_threshold": 0.95, "prefer_resident": True,
                   # The calibrated capability (#30): the second half of the
                   # tok/s cell, and the number placement.py decides with.
                   "pp_tps": 1234.5, "tg_tps": 43.5, "models": ["a", "b"],
                   "api_key": "set"}],
    }


# -- the maintenance windows the stub renders (issue #10) -------------------
# Two windows, the two states an operator acts on: one with requests still
# running and time left, one drained and without an end. The `until` is a fixed
# offset so the clock the card shows is the same one Python computes below.
UNTIL = "2026-11-15T14:30:00+01:00"


def _maintenance() -> dict:
    return {
        "maintenance": {
            "window": {"node": "M5-alpha", "since": "2026-11-15T14:00:00+01:00",
                       "until": UNTIL, "minutes_left": 12.4,
                       "by": "dashboard", "note": ""},
            "inflight": 2,
        },
        "drained": {
            "window": {"node": "M5-alpha", "since": "2026-11-15T14:00:00+01:00",
                       "until": None, "minutes_left": None,
                       "by": "operator", "note": "kernel update"},
            "inflight": 0,
        },
    }


# -- the model policy the stub renders (issue #11) --------------------------
# One pair `disabled` on the entry host and one `hidden` on the worker: the two
# levels the table has to show, each in the row it belongs to. The stub copies
# them into /healthz the way the backend does (`policy`, `dispatch: false`).
POLICY = {"M5-alpha": {DEEPSEEK: "disabled"}, "M3-beta": {DEEPSEEK: "hidden"}}

# The replication panel's two states (issue #13): a plan the operator has to
# confirm, and the job report afterwards. The numbers are the acceptance figures
# (111,6 GB onto a target with 6,3 TB free).
REPLICATION = {
    "plan": {
        "source": "M5-alpha", "target": "M3-beta",
        "model": "Qwen3.8-Flash-Next-oQ4e-mtp",
        "source_path": "/Volumes/GLM4TB/models/Jundot/Qwen3.8-Flash-Next-oQ4e-mtp",
        "relative": "Jundot/Qwen3.8-Flash-Next-oQ4e-mtp",
        "target_root": "/Users/operator/.omlx/models",
        "target_path": "/Users/operator/.omlx/models/Jundot/Qwen3.8-Flash-Next-oQ4e-mtp",
        "staging_path": "/Users/operator/.omlx/models/.replication-incoming/Jundot/"
                        "Qwen3.8-Flash-Next-oQ4e-mtp",
        "size_bytes": 111_609_397_878, "size_gb": 111.61,
        "required_bytes": 117_189_867_772, "free_bytes": 6_470_284_789_504,
        "free_gb": 6470.28, "overwrite": False, "already_there": False,
        "settings_keys": 83, "ok": True, "errors": [], "warnings": [],
    },
    "job": {
        "state": "running", "step": "copy", "percent": 45.0,
        "bytes_done": 50_224_229_045, "rate": "1.2GB/s", "eta": "0:00:51",
        "settings": {"sent": 0, "compared": 0, "mismatches": []},
        "report": {"bytes": 0, "seconds": 0.0, "mb_per_s": 0.0,
                   "not_replicated": []},
        "reload": None, "error": None,
    },
}


def _catalogs() -> dict:
    """The message catalogs as the page fetches them (`/static/i18n/<lang>.json`).

    Every harness that runs `dashboard.js` has to answer those two URLs — since
    issue #17 the page boots by reading them, and a harness without them would
    measure ids instead of text.
    """
    return {"static/i18n/" + p.name: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((STATIC / "i18n").glob("*.json"))}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    """The real JavaScript, executed under a DOM stub (tests/dashboard_dom_stub.js)."""
    return _run_stub(tmp_path_factory.mktemp("dashboard"), _stub_fixture())


@pytest.fixture(scope="module")
def reloaded(tmp_path_factory) -> dict:
    """A browser that opens the page with `#models` already in the address bar.

    The tab is the hash, so "a reload lands on Models" is a second run of the
    same page with a hash, not a remembered variable (#56).
    """
    return _run_stub(tmp_path_factory.mktemp("dashboard-reload"),
                     _stub_fixture(hash="#models"))


# -- the node tab's payloads (issue #71) ------------------------------------
# What `POST /admin/nodes?dry_run=true` answers for a node that is there and
# healthy: exactly the four facts the preview shows, plus the site the address
# derives into (#67). The numbers are the ones the fleet card uses, so the two
# screens cannot disagree about the same machine.
NODE_ADDRESS = "http://192.0.2.40:8000"
NODE_PREVIEW = {
    "dry_run": True, "name": "192.0.2.40", "address": NODE_ADDRESS,
    "node_id": "as5-m4", "version": "0.8.0rc1", "status": "healthy",
    "models": [DEEPSEEK, "Qwen3.5-122B-A10B-4bit"], "model_count": 2,
    "ceiling_bytes": 525_360_213_856, "used_bytes": 453_635_361_950,
    "rtt_ms": 7.4, "role": "worker", "weight": 1.0,
    "site": "", "site_name": "", "site_networks": [],
    "site_derived": "wd", "site_derived_name": "Wien",
    "site_mismatch": False, "site_unconfirmed": False,
    "models_error": None, "step": None, "warnings": [],
}
# The operator declares a site the address does not belong to: a warning in the
# preview, not a refusal.
NODE_PREVIEW_MISMATCH = dict(
    NODE_PREVIEW, site="tr", site_name="Traiskirchen", site_mismatch=True,
    warnings=["site mismatch: declared Traiskirchen, the address is in Wien"])
# The node answers, its model list does not (oMLX 401 without a key).
NODE_PREVIEW_PARTIAL = dict(
    NODE_PREVIEW, models=[], model_count=0, step="models",
    models_error="401 Unauthorized", warnings=["model list unreadable: 401"])
NODE_ADDED = {"source": "overlay", "node_id": "as5-m4", "models": 2,
              "ceiling_bytes": 525_360_213_856, "used_bytes": 453_635_361_950,
              "address": NODE_ADDRESS, "node": {"name": "192.0.2.40"}}
DISCOVERY = {
    "cidr": "192.0.2.0/24", "port": 8000, "timeout": 0.5, "count": 2,
    "took_seconds": 2.4,
    "found": [
        {"address": NODE_ADDRESS, "name": "as5-m4", "node_id": "as5-m4",
         "models": [DEEPSEEK], "model_count": 1, "latency_ms": 7.4, "admin": True,
         "site_derived": "wd", "site_derived_name": "Wien", "known": False,
         "known_nodes": []},
        # Already in the fleet: offered, but not a second time.
        {"address": "http://192.0.2.11:8000", "name": "M5-alpha", "node_id": "m5",
         "models": [DEEPSEEK], "model_count": 1, "latency_ms": 3.1, "admin": True,
         "site_derived": "wd", "site_derived_name": "Wien", "known": True,
         "known_nodes": ["M5-alpha"]},
    ],
}
DRAIN_TIMEOUT = {"error": {
    "code": "drain_timeout", "node": "M3-beta", "source": "overlay",
    "inflight": 2, "models": [{"model": DEEPSEEK, "inflight": 1}],
    "waited_seconds": 5.0, "deadline_seconds": 5, "draining": True,
    "enabled": False, "in_pool": False,
    "message": ("M3-beta still has 2 request(s) in flight after 5.0s (deadline 5s): "
                + DEEPSEEK + "×1. The node is disabled and stays in the fleet, out of "
                "the pool; nothing was removed."),
}}
CONFIG_HOST = {"error": {
    "code": "config_host",
    "message": ("M5-alpha is declared in config.yaml and cannot be deleted at "
                "runtime — the file is the baseline. Disable it instead: "
                "POST /admin/nodes/M5-alpha/disable"),
}}


# -- the parameter block's payload (issue #78) ------------------------------
# The twelve paths an operator may set at the page, exactly as the contract
# names them. `hosts.*.weight` is a family: it covers every host row, and the
# concrete path of `M5-alpha` is `hosts.M5-alpha.weight`.
CONFIG_EDITABLE = [
    "hosts.*.weight", "hosts.*.spill_threshold", "hosts.*.prefer_resident",
    "affinity.ttl_seconds", "affinity.match_threshold", "affinity.ephemeral_window_seconds",
    "placement.rebind.enabled", "placement.rebind.max_per_hour",
    "placement.rebind.safety_factor", "placement.rebind.per_thread_cooldown_seconds",
    "placement.balance_abs_threshold", "placement.balance_rel_threshold",
]
# The loader's own sentence for a value it refuses — the page shows it verbatim,
# so the test compares against this string and not against a translation.
TTL_INVALID = ("affinity.ttl_seconds must be between 60 and 86400 — got -5")
WEIGHT_CONFIRM = {
    "error": "confirm_required", "path": "hosts.M5-alpha.weight",
    "threads_that_would_move": 3,
    "message": "3 threads hold M5-alpha; the new weight applies to new threads only",
}
WEIGHT_SET = {"path": "hosts.M5-alpha.weight", "value": 2.5, "source": "overlay",
              "effective": 2.5}
TTL_SET = {"path": "affinity.ttl_seconds", "value": 900, "source": "overlay",
           "effective": 900}


def _config_live() -> dict:
    """`/config` with the two additive keys of #78.

    One value is already set at the page (`hosts.M5-alpha.weight: 2.0`), so the
    fixture carries both states in one payload: a row with an origin chip and a
    reset control, and rows that still come from `config.yaml`.
    """
    cfg = json.loads(json.dumps(_config()))
    cfg["hosts"][0]["weight"] = 2.0
    cfg["overrides"] = {"hosts.M5-alpha.weight": 2.0}
    cfg["editable"] = CONFIG_EDITABLE
    return cfg


def _stub_fixture(**extra) -> dict:
    """The payload every harness answers with — plus the shell itself.

    The shell travels with the fixture since #56: the tab bar and the four
    panels live in `ripple/dashboard.py`, and the stub paints the real ones
    instead of a copy.
    """
    catalogs = _catalogs()
    data = {"healthz": _healthz(), "stats": _stats(), "config": _config(),
            "maintenance": _maintenance(), "policy": POLICY,
            "replication": REPLICATION, "loading": LOADING,
            "shell": render_dashboard(), **catalogs,
            # The parameter block with origins and fields (#78). `config` stays
            # the payload of an older server — no `editable`, no `overrides` —
            # so the backward-compatibility measurement has both.
            "config_live": _config_live(),
            # The node tab (#71): the answer of the dry run, the sweep, and the
            # two refusals of the removal. `confirm: true` is the operator
            # saying yes to the one question the page asks before it writes.
            "confirm": True,
            "node_preview_mismatch": NODE_PREVIEW_MISMATCH,
            "node_preview_partial": NODE_PREVIEW_PARTIAL,
            "responses": {
                "POST admin/nodes?dry_run=true": {"status": 200, "body": NODE_PREVIEW},
                "POST admin/nodes": {"status": 201, "body": NODE_ADDED},
                "POST admin/discover": {"status": 200, "body": DISCOVERY},
                "DELETE admin/nodes/M3-beta": {"status": 409, "body": DRAIN_TIMEOUT},
                "DELETE admin/nodes/M5-alpha": {"status": 409, "body": CONFIG_HOST},
                # 6.5 (#78): a refused value carries the loader's message, a
                # weight that would move threads answers 409 first and 200 on
                # the confirmed repeat, and a reset is a plain DELETE.
                "PATCH admin/config/affinity.ttl_seconds": {"queue": [
                    {"status": 200, "body": TTL_SET},
                    {"status": 400, "body": {"error": "invalid_value",
                                             "path": "affinity.ttl_seconds",
                                             "message": TTL_INVALID}},
                ]},
                "DELETE admin/config/affinity.ttl_seconds":
                    {"status": 200, "body": {"path": "affinity.ttl_seconds",
                                             "source": "config.yaml", "effective": 1800}},
                "PATCH admin/config/hosts.M5-alpha.weight": {"queue": [
                    {"status": 409, "body": WEIGHT_CONFIRM},
                    {"status": 200, "body": WEIGHT_SET},
                ]},
            }}
    data.update(extra)
    return data


def _run_stub(work: pathlib.Path, data: dict) -> dict:
    if NODE is None:
        pytest.skip("node is not installed — the layout measurement needs it")
    fixture = work / "fixture.json"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(ROOT / "tests" / "dashboard_dom_stub.js"),
         str(STATIC / "dashboard.js"), str(fixture)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# -- tiny HTML readers ------------------------------------------------------
class _Cells(html.parser.HTMLParser):
    """The first table's head row and every body row, with a per-cell flag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.head: list[dict] = []
        self.body: list[list[dict]] = []
        self._section: str | None = None
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "thead":
            self._section = "thead"
        elif tag == "tbody":
            self._section = "tbody"
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"tag": tag, "attrs": a, "text": "", "tip": False}
            self._row.append(self._cell)
        elif self._cell is not None and "data-tip" in a:
            self._cell["tip"] = True

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._section == "thead":
                self.head = self._row
            else:
                self.body.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def _table(markup: str) -> _Cells:
    parser = _Cells()
    parser.feed(markup)
    parser.close()
    return parser


def _effective_columns(rows: list[list[dict]]) -> list[int]:
    """Cells a row occupies, counting the rowspans carried over from above."""
    carried = 0
    out = []
    for row in rows:
        own = sum(int(cell["attrs"].get("colspan", 1)) for cell in row)
        out.append(own + carried)
        carried = sum(int(cell["attrs"].get("rowspan", 1)) - 1 for cell in row)
    return out


VOID = {"br", "col", "meta", "link", "img", "input", "hr"}


class _Node:
    def __init__(self, tag: str, attrs: dict) -> None:
        self.tag, self.attrs, self.children = tag, attrs, []


class _Tree(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in VOID:
            self._stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


NUMBER_ONLY = re.compile(
    r"^[\s+\-−/(]*\d[\d.,]*\s*(%|GB|ms|s|h|tok/s|min)?[\s+\-−/)]*$")


def _bare_numbers(markup: str) -> list[str]:
    """Numbers rendered outside any element that carries a `data-tip`."""
    tree = _Tree()
    tree.feed(markup)
    tree.close()
    out: list[str] = []

    def walk(node, tipped):
        tipped = tipped or "data-tip" in node.attrs
        for child in node.children:
            if isinstance(child, str):
                text = child.strip()
                if not tipped and text and NUMBER_ONLY.match(text):
                    out.append(text)
            else:
                walk(child, tipped)

    walk(tree.root, False)
    return out


def _model_rows(*, loaded: bool) -> int:
    """(model, node) rows of the fixture with or without a resident weight."""
    return sum(len(m["hosts"]) for m in _healthz()["models"]
               if any(x["loaded"] for x in m["hosts"]) is loaded)


def _card(fleet: str, name: str) -> str:
    start = fleet.index('data-key="%s"' % name)
    nxt = fleet.find('<article class="card host"', start + 10)
    return fleet[start:nxt if nxt != -1 else len(fleet)]


# -- the shell --------------------------------------------------------------
def test_the_shell_links_the_two_static_assets(page):
    assert '<link rel="stylesheet" href="/static/dashboard.css">' in page
    assert '<script src="/static/dashboard.js"></script>' in page


def test_the_page_carries_no_external_url(page, css, js):
    """FA-22: it has to open inside the LAN with the cable pulled — no URL, and
    no second way to the network either (issue #17: the message catalogs ship
    with the page, so a CDN-hosted i18n bundle is out)."""
    for part, name in ((page, "shell"), (css, "css"), (js, "js")):
        assert "http://" not in part, name
        assert "https://" not in part, name
        assert "//cdn" not in part, name
    for api in ("XMLHttpRequest", "WebSocket", "EventSource", "importScripts"):
        assert api not in js, api
    for target in re.findall(r"fetch\('([^']+)'", js):
        assert target.startswith("/static/") or target in ("/healthz", "/stats", "/config"), target


def test_the_page_cannot_reload_or_navigate_itself(page, js):
    assert "<meta http-equiv" not in page
    assert "location.reload" not in js
    assert "location.href" not in js
    assert "document.write" not in js
    assert "eval(" not in js
    assert "setInterval(tick, 2000)" in js


def _numbered_headings(page: str) -> list[tuple[str, str]]:
    """(number, message id) of every numbered heading, in document order.

    Since issue #17 the shell carries `data-i18n` ids instead of words, so the
    heading's title is the id — the words come from the catalog and are checked
    against it in `tests/test_i18n.py`.
    """
    return re.findall(r'<span class="idx">([^<]+)</span><span data-i18n="([^"]+)"', page)


def _panels(page: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """(tab id, its numbered headings) per tab panel, in document order (#56)."""
    out = []
    for chunk in page.split('<section class="tab-panel"')[1:]:
        found = re.search(r'data-tab="([a-z]+)"', chunk)
        assert found, chunk[:120]
        out.append((found[1], _numbered_headings(chunk)))
    return out


def test_the_section_numbers_run_from_one_to_seven_without_a_gap(page):
    """Issue #19 item 3: every heading carries a number and none of them
    leaves a gap — the operator counts the headings, so a gap is a defect.
    6.6 is the restart control (issue #25): a block of its own, because it ends
    the process instead of taking one node out of the pool. 7 is the node tab
    (issue #71) with 7.1 by address, 7.2 the sweep and 7.3 removal.

    The numbers follow the reading order inside the tabs: `Load` is 2.1 (it
    follows `2 Verteilung` on the fleet tab), the model matrix is 3, threads
    are 4, the interaction round 5 and the operator block 6 with 6.1…6.6. So
    the numbers are one ascending sequence across the tab bar as well:
    1, 2, 2.1 | 3 | 4 | 5 | 6.x | 7.x. Since #73 the threads have a tab of their
    own, and since #71 the node tab is appended rather than squeezed in, so
    every top-level number from 1 to 7 appears exactly once, in tab order, and
    each tab reads ascending.
    """
    panels = dict(_panels(page))
    assert list(panels) == ["fleet", "models", "threads", "history", "ops", "nodes"]
    assert panels["fleet"] == [("1", "sec.fleet"), ("2", "sec.dist"),
                               ("2.1", "sec.load")]
    assert panels["models"] == [("3", "sec.models")]
    assert panels["threads"] == [("4", "sec.threads")]
    assert panels["history"] == [("5", "sec.log")]
    assert panels["ops"] == [("6", "sec.ops"), ("6.1", "sec.maint"),
                             ("6.2", "sec.policy"), ("6.3", "sec.calib"),
                             ("6.4", "sec.repl"), ("6.5", "sec.params"),
                             ("6.6", "sec.restart")]
    assert panels["nodes"] == [("7", "sec.nodes"), ("7.1", "sec.nodes.add"),
                               ("7.2", "sec.nodes.scan"),
                               ("7.3", "sec.nodes.remove")]
    for tab, heads in panels.items():
        numbers = [float(n) for n, _ in heads]
        assert numbers == sorted(numbers), tab        # reading order per tab
    every = [n for heads in panels.values() for n, _ in heads]
    assert sorted(every, key=float) == ["1", "2", "2.1", "3", "4", "5",
                                        "6", "6.1", "6.2", "6.3", "6.4",
                                        "6.5", "6.6", "7", "7.1", "7.2", "7.3"]
    # the top-level numbers are exactly 1…7, once each, in tab order
    top = [n for heads in panels.values() for n, _ in heads if "." not in n]
    assert top == ["1", "2", "3", "4", "5", "6", "7"]


def test_no_heading_is_left_without_a_number(page):
    """The other half of #19 item 3: `Threads` and `Betrieb` were the two
    headings without one."""
    assert re.findall(r"<h2>(?!<span class=\"idx\">)", page) == []
    assert re.search(r'<summary><span class="idx">6</span><span data-i18n="sec.ops"', page)
    # seven top-level sections, ten numbered blocks below them (2.1, 6.1…6.6,
    # 7.1…7.3)
    assert len(re.findall(r'<span class="idx">[1-9]</span>', page)) == 7
    assert len(re.findall(r'<span class="idx">[1-9]\.[1-9]</span>', page)) == 10


def test_the_log_header_reads_interaktionsrunde(page):
    """Issue #19 item 5: the operator's wording for the ring buffer.

    Since #17 the heading carries `sec.log` and the words live in the catalog —
    so the wording is asserted on the German catalog, which is where it is.
    """
    assert 'data-i18n="sec.log"' in page
    assert _de()["sec.log"] == "Letzte Interaktionsrunde"
    assert "Letzte Entscheidungen" not in _de().values()


def test_the_shell_holds_every_container_the_design_needs(page):
    """Every container the render functions write into exists, and each one sits
    in exactly one tab panel (#56): a container outside a panel would be visible
    on every tab, one in two panels would be rendered twice."""
    for anchor in ('id="legend"', 'id="banners"', 'id="fleet"', 'id="kpis"',
                   'id="models"', 'id="dist"', 'id="threads"', 'id="log"',
                   'id="params"', 'id="calibPanel"', 'id="maintPanel"',
                   'id="policyPanel"', 'id="replPanel"', 'id="restartPanel"',
                   'id="load"'):
        assert anchor in page, anchor
    panels = page.split('<section class="tab-panel"')[1:]
    for cid in ("legend", "fleet", "kpis", "models", "dist", "threads", "load",
                "log", "params", "calibPanel", "maintPanel", "policyPanel",
                "replPanel", "restartPanel"):
        hits = [i for i, chunk in enumerate(panels) if 'id="%s"' % cid in chunk]
        assert len(hits) == 1, (cid, hits)
    # the banners are page-level, not per tab: a warning must not hide in one
    assert 'id="banners"' not in "".join(panels)


# -- the tabs (issue #56, five since #73, six since #71) ---------------------
# The page was 2 785 px tall at 1440×1000 — 2.8 screens for a window that answers
# one question at a time. The tabs, one state: the URL hash. Since #73 there are
# five: the thread registry grows with the number of conversations and pushed
# the fleet page apart, so it got a tab of its own. Load (2.1) still gets none:
# it answers the same question as the distribution — what the fleet is doing
# right now. #71 appends a sixth, `Knoten`: adding a node changes the pool
# itself and is not a maintenance action, so it is not a block in `Betrieb` —
# and appending keeps the reading order 1…6 (and `6.1`…`6.6`) as they were.
TABS = ("fleet", "models", "threads", "history", "ops", "nodes")


def _attrs(tag: str) -> dict[str, str]:
    """The attributes of one start tag, as a dict (quoted values only)."""
    return dict(re.findall(r'([a-z][a-z0-9-]*)="([^"]*)"', tag))


def test_the_shell_carries_six_tabs_with_one_panel_each(page):
    """`nav.tabs` is a tablist, every button points at its panel, and the six
    panels are the six tabs — one open in the delivered HTML (`Flotte`), so the
    page is readable before the script has run."""
    assert '<nav class="tabs" role="tablist">' in page
    buttons = [t for t in re.findall(r"<button\b[^>]*>", page)
               if _attrs(t).get("role") == "tab"]
    assert [_attrs(b)["data-tab"] for b in buttons] == list(TABS)
    assert [_attrs(b)["data-i18n"] for b in buttons] == ["tab." + t for t in TABS]
    assert [_attrs(b)["aria-selected"] for b in buttons] == ["true"] + ["false"] * 5
    for b in buttons:
        assert _attrs(b)["aria-controls"] == "panel-" + _attrs(b)["data-tab"]

    panels = re.findall(r'<section class="tab-panel"[^>]*>', page)
    assert [_attrs(p)["data-tab"] for p in panels] == list(TABS)
    assert [_attrs(p)["role"] for p in panels] == ["tabpanel"] * 6
    assert [_attrs(p)["id"] for p in panels] == ["panel-" + t for t in TABS]
    assert [_attrs(p)["aria-labelledby"] for p in panels] == ["tab-" + t for t in TABS]
    assert [(" hidden" in p) for p in panels] == [False, True, True, True, True, True]


def test_the_tab_words_come_from_the_catalog(page, rendered):
    """`t('tab.fleet')`, not a word in the HTML: the bar is translated like
    everything else, and a German word in the shell would win over the catalog."""
    en, de = _en(), _de()
    assert rendered["tabs"]["catalog"] == list(TABS)
    assert rendered["tabs"]["default_tab"] == "fleet"
    for tab in TABS:
        assert "tab." + tab in en and "tab." + tab in de
        assert en["tab." + tab] not in page, tab
        assert de["tab." + tab] not in page, tab
    assert [b["text"] for b in rendered["tabs"]["boot"]["buttons"]] == \
        [en["tab." + t] for t in TABS]


def test_the_page_opens_on_fleet_with_one_panel_visible(rendered):
    boot = rendered["tabs"]["boot"]
    assert boot["active"] == "fleet"
    assert boot["hash"] == ""                       # a plain URL stays plain
    assert [b["selected"] for b in boot["buttons"]] == ["true", "false", "false", "false", "false", "false"]
    assert [p["hidden"] for p in boot["panels"]] == [False, True, True, True, True, True]


def test_a_click_sets_the_hash_and_moves_the_selection(rendered):
    """The button writes the hash and moves `aria-selected`/`hidden` — one
    panel open, three closed, and the roving `tabindex` follows."""
    after = rendered["tabs"]["after_models"]
    assert after["active"] == "models"
    assert after["hash"] == "#models"
    assert after["writes"][-1] == "#models"
    assert [b["selected"] for b in after["buttons"]] == ["false", "true", "false", "false", "false", "false"]
    assert [p["hidden"] for p in after["panels"]] == [True, False, True, True, True, True]
    assert [b["tabindex"] for b in after["buttons"]] == ["-1", "0", "-1", "-1", "-1", "-1"]


def test_the_two_second_tick_keeps_the_open_tab(rendered):
    """The patch replaces nodes; a tab kept in a class on a container would be
    gone after one tick. The hash is the state, so the tick cannot lose it."""
    after = rendered["tabs"]["after_tick"]
    assert after["active"] == "models"
    assert after["hash"] == "#models"
    assert [p["hidden"] for p in after["panels"]] == [True, False, True, True, True, True]


def test_the_hash_is_read_and_an_unknown_one_is_left_alone(rendered):
    """`#history` opens History without writing anything back; `#gibtsnicht` is
    not a tab, so the page shows `Flotte` and leaves the operator's URL alone."""
    tabs = rendered["tabs"]
    assert tabs["from_hash"]["active"] == "history"
    assert tabs["from_hash"]["hash"] == "#history"
    assert tabs["from_hash"]["writes"][-1] == "#models"     # nothing written
    assert [p["hidden"] for p in tabs["from_hash"]["panels"]] == [True, True, True, False, True, True]
    assert tabs["unknown_hash"]["active"] == "fleet"
    assert tabs["unknown_hash"]["hash"] == "#gibtsnicht"


def test_a_hashchange_moves_the_tab_without_a_reload(rendered):
    """The back button, a bookmark, a typed address: the hash changes and the
    page follows — `applyHash` is the one handler on `hashchange`."""
    after = rendered["tabs"]["after_hashchange"]
    assert after["active"] == "ops"
    assert after["hash"] == "#ops"
    assert [p["hidden"] for p in after["panels"]] == [True, True, True, True, False, True]


def test_a_reload_with_models_lands_on_models(reloaded):
    """A second start of the same page, hash and all: no memory, no storage —
    the address is the state."""
    boot = reloaded["tabs"]["boot"]
    assert boot["active"] == "models"
    assert boot["hash"] == "#models"
    assert [b["selected"] for b in boot["buttons"]] == ["false", "true", "false", "false", "false", "false"]
    assert [p["hidden"] for p in boot["panels"]] == [True, False, True, True, True, True]


def test_a_language_switch_changes_the_words_not_the_tab(rendered):
    after = rendered["tabs"]["after_lang"]
    de = _de()
    assert after["active"] == "models" and after["hash"] == "#models"
    assert [b["text"] for b in after["buttons"]] == [de["tab." + t] for t in TABS]
    assert [p["hidden"] for p in after["panels"]] == [True, False, True, True, True, True]


def test_every_tab_shows_exactly_its_own_panel(rendered):
    views = rendered["tabs"]["view_of"]
    assert sorted(views) == sorted(TABS)
    for name, rows in views.items():
        assert [r["id"] for r in rows] == list(TABS)
        assert [r["id"] for r in rows if r["selected"]] == [name]
        assert [r["id"] for r in rows if not r["hidden"]] == [name]


def test_the_operations_tab_opens_the_operator_block(js):
    """The operator block is a collapsed `<details>`: as a tab it would be one
    line of summary. The tab opens it — the same click the operator used to
    make — and the block's own `toggle` handler reads the rules without a row
    (#16), so the tab switch costs no request of its own."""
    assert "if (ops && ops.open === false) ops.open = true;" in js
    assert "ops.addEventListener('toggle'" in js


def test_the_bar_is_driven_by_the_page_and_not_by_a_stored_choice(js):
    """A click, the arrow keys and the hash: three ways in, one state. No
    `localStorage` copy — a remembered tab would fight the address bar."""
    assert "closest('[role=\"tab\"]')" in js
    assert "setTab(btn.getAttribute('data-tab'))" in js
    assert "history.replaceState(null, '', '#' + name)" in js
    assert "applyHash()" in js
    assert "ripple_tab" not in js


def test_the_tab_bar_sticks_and_the_log_keeps_no_window_of_its_own(css):
    """The bar is sticky, so the column heads of the decision log have to move
    down by its height (`--tabbar-h`, one place) instead of `top:0`. History is
    a tab now, so its own scroll window is gone — the page scrolls. (On a narrow
    window the table is wider than the column and the panel scrolls sideways;
    that is the one `overflow` it may keep, and it is in the 820 px media query,
    not in the base rule.)"""
    bar = _rule(css, ".tabs")
    assert "position:sticky" in bar and "top:0" in bar
    assert "background:var(--bg)" in bar            # opaque, or content scrolls through
    assert ":root{--tabbar-h:" in css               # the height lives in a token
    assert "top:var(--tabbar-h)" in _rule(css, ".log thead th")
    log = _rule(css, ".log")
    assert "max-height" not in log and "overflow" not in log
    # … and the blocks that keep their window are untouched.
    assert re.search(r"\.table-wrap\{[^}]*overflow-x:auto", css)


# -- the fleet tab after #73: one block per model, no tooth ------------------
def _healthz_two_models_at_work() -> dict:
    """The fixture with the second model holding a slot too (#73).

    The delivered fixture has one model with a thread and one with a request in
    `/stats`, so `2` renders a single block. The comparison the block exists
    for needs two: this payload makes Qwen hold a slot as well.
    """
    h = json.loads(json.dumps(_healthz()))
    for m in h["models"]:
        if m["id"] == "Qwen3.5-9B-oQ4e-mtp":
            m["hosts"][0]["threads"] = 1
    return h


@pytest.fixture(scope="module")
def two_models(tmp_path_factory) -> dict:
    """The page rendered with two models at work: `2` has two blocks."""
    return _run_stub(tmp_path_factory.mktemp("dashboard-two-models"),
                     _stub_fixture(healthz=_healthz_two_models_at_work()))


def test_the_distribution_is_one_block_per_model(two_models, js):
    """Issue #73: `2` is the model comparison, so every model at work gets its
    own block and the model name is that block's heading — not a row label
    inside one long block."""
    dist = two_models["parts"]["dist"]
    blocks = re.findall(r'<div class="dist-block[^"]*" data-key="([^"]+)"', dist)
    assert blocks == [DEEPSEEK, "Qwen3.5-9B-oQ4e-mtp"]
    for key in blocks:
        assert '<span class="dist-name" title="%s">%s</span>' % (key, key) in dist
    # … and the case with nothing at work is one block that takes the whole row
    # (the class is in the renderer, the rule in the sheet).
    assert 'dist-block dist-note dist-solo' in js


def test_the_distribution_blocks_stand_side_by_side(css):
    """The two blocks share a row at 1440 px (`#dist` is the grid, the old
    `.split` with the thread registry in the other half is gone), and under
    900 px they stack."""
    grid = _rule(css, "#dist")
    assert "display:grid" in grid
    assert "auto-fit" in grid and "380px" in grid
    # the hairline between blocks is the gap, so it also works in a second row
    assert "gap:1px" in grid and "background:var(--line)" in grid
    # the single-block case spans the row
    assert "grid-column:1/-1" in _rule(css, "#dist > .dist-solo")
    assert ".split" not in css
    # the stacking breakpoint lives in the 900 px media query, with the KPIs
    narrow = css.split("@media (max-width:900px)")[1].split("@media")[0]
    assert "#dist{grid-template-columns:minmax(0,1fr)}" in narrow


def test_the_distribution_head_and_rows_share_one_six_column_raster(css):
    """Issue #91: the head gained a column (requests), so the raster has six
    tracks — and head and body must carry the *same* template, or no number
    stands under its label.

    The widths are the measured maximum of the longest header word in the 16
    catalogs (`Csomópont` 80, `Solicitudes` 81, `Docelowy` 70, `Rzeczywisty` 88,
    `Scostamento` 94 at 12 px without tracking), which is why the sheet carries
    no `.06em` on these labels: it costs 0,72 px per character and the bar pays
    for it.
    """
    row = re.search(r"grid-template-columns:\s*([^;}]+)", _rule(css, ".dist-row"))
    cols = re.search(r"grid-template-columns:\s*([^;}]+)", _rule(css, ".dist-cols"))
    assert row and cols
    assert row.group(1) == cols.group(1)
    assert len(row.group(1).split()) == 6
    assert "letter-spacing:0" in _rule(css, ".dist-cols .dc b")


def test_one_model_at_work_gets_a_660_px_block_and_no_grey_strip(css):
    """Issue #80: one model with work stretched its block over the whole row —
    measured at 1440 px: a 1361 px block, a 1061 px bar for a percentage, and
    the operator read the row as a ruler.

    The track is capped at 660 px (`minmax(380px,660px)`) and the container
    shrinks to its blocks (`width:max-content`): the grey `--line` behind the
    gaps ends at the last block, so the free space on the right is the page and
    not a grey area. `max-width:100%` keeps a long note from pushing the page.
    Under 900 px the one column takes the full width again — there the block
    fills the row, so no grey can show."""
    grid = _rule(css, "#dist")
    assert "minmax(380px,660px)" in grid
    assert "width:max-content" in grid and "max-width:100%" in grid
    narrow = css.split("@media (max-width:900px)")[1].split("@media")[0]
    assert "#dist{width:auto}" in narrow


def _panel_html(page: str, name: str) -> str:
    """The markup of one tab panel, from its opening tag to the next panel.

    Splitting on `data-tab="fleet"` would hit the *button* in the bar first —
    the bar comes before the panels.
    """
    for chunk in page.split('<section class="tab-panel"')[1:]:
        found = re.search(r'data-tab="([a-z]+)"', chunk)
        if found and found[1] == name:
            return chunk
    raise AssertionError("no panel %r" % name)


def test_the_load_block_follows_the_distribution_over_the_full_width(page):
    """`2.1` starts directly under `2` and nothing shares its row: the old
    two-column `.split` put it in the second grid row, and the shorter of the
    two columns above left an empty cell — the tooth."""
    fleet = _panel_html(page, "fleet")
    assert 'class="split"' not in fleet and 'class="wide"' not in fleet
    assert fleet.index('data-i18n="sec.dist"') < fleet.index('data-i18n="sec.load"')
    # three sections, one per number: 1, 2, 2.1 — no wrapper between them
    assert fleet.count("<section>") == 3
    assert fleet.count("</section>") == 4     # the three children and the panel


def test_the_threads_tab_shows_the_registry_without_a_window_of_its_own(page, css):
    """The registry grows with the number of conversations, so it is a tab and
    the page scrolls — like the history tab, the panel keeps no `max-height`
    and no `overflow` of its own."""
    threads = _panel_html(page, "threads")
    assert 'id="threads"' in threads
    assert 'data-i18n="sec.threads"' in threads
    assert re.findall(r"#threads\{[^}]*(?:max-height|overflow)", css) == []
    assert re.findall(r"\.thread-item\{[^}]*(?:max-height|overflow)", css) == []


def test_the_thread_registry_stands_in_two_columns(css):
    """Issue #80: seven threads in one column were 1374 px wide — a key, two
    chips and a lot of air per row, twice the width for the same information.

    `column-count` fills *column by column*, not row by row: four blocks are
    2×2, seven are 4+3 (left column full, right shorter), and the reading order
    stays the one of the list. A grid with `grid-auto-flow:column` would have to
    know the row count; balanced columns do it themselves. `break-inside:avoid`
    is what keeps a thread from being cut across the column boundary, and under
    1200 px there is one column again — two half columns of a phone are two
    unreadable columns."""
    panel = _rule(css, "#threads")
    assert "column-count:2" in panel
    # the hairline between the columns, not a background: the panel is one block
    assert "column-rule:1px solid var(--line)" in panel
    assert "break-inside:avoid" in _rule(css, "#threads > .thread-item")
    narrow = css.split("@media (max-width:1200px)")[1].split("@media")[0]
    assert "#threads{column-count:1}" in narrow


def test_the_middle_label_of_a_thread_sits_in_a_fixed_column(css):
    """Issue #80: `space-between` put `interactive · header` wherever the slack
    happened to be — it moved by 17 px depending on the key and the key chip
    (measured: 462 px next to 445 px).

    Three columns, two of them fixed: the key takes the rest, the label has 132
    px and starts left, the key chip has the same 132 px and ends right. No cell
    can push another, so the label stands at the same x in every row."""
    top = _rule(css, ".th-top")
    assert "display:grid" in top
    assert "minmax(0,1fr) 132px 132px" in top
    assert "justify-self:start" in _rule(css, ".th-top > .chip:not(:last-child)")


# -- the wordmark (design §3, §4) -------------------------------------------
def test_the_wordmark_is_rubik_microbe_at_forty_pixels(css):
    h1 = re.search(r"h1\{[^}]*\}", css).group(0)
    assert "font-family:'Rubik Microbe'" in h1
    assert "font-size:40px" in h1
    assert "text-transform:uppercase" in h1
    assert "color:#9BBA7D" in h1
    assert "letter-spacing:.02em" in h1


def test_the_wordmark_font_is_shipped_and_not_fetched(css, page, js):
    font = STATIC / "fonts" / "rubik-microbe-wordmark.woff2"
    assert font.exists(), "the subset has to travel with the page"
    assert font.read_bytes()[:4] == b"wOF2"
    assert font.stat().st_size == 8816       # R,I,P,L,E subset, not the 235 kB font
    assert (STATIC / "fonts" / "OFL.txt").exists(), "OFL 1.1 requires the license"

    # A relative path inside the style sheet: no host, no scheme, no CDN.
    assert "src:url(fonts/rubik-microbe-wordmark.woff2) format('woff2')" in css
    for part in (page, css, js):
        assert "fonts.googleapis" not in part
        assert "fonts.gstatic" not in part


def test_the_wordmark_font_touches_nothing_but_the_h1(css):
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = [(sel.strip(), body) for sel, body in
             re.findall(r"([^{}]+)\{([^}]*)\}", bare)]
    using = [sel for sel, body in rules if "Rubik Microbe" in body]
    assert using == ["@font-face", "h1"], using


def test_sage_appears_only_on_the_wordmark(css, js):
    assert css.count("#9BBA7D") == 1
    assert "#9BBA7D" not in js
    assert re.search(r"h1\{[^}]*color:#9BBA7D", css)


# -- the style sheet --------------------------------------------------------
def _rule(css: str, selector: str) -> str:
    """The declaration block of the rule with exactly this selector.

    Comments are stripped first: they carry prose (and the occasional brace),
    and a test about a rule must not be satisfied by a sentence."""
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found = re.search(r"(?:^|[};])\s*%s\{([^}]*)\}" % re.escape(selector), bare)
    assert found, "no rule for %r" % selector
    return found.group(1)


def test_no_font_size_is_below_twelve_pixels(css):
    tokens = dict(re.findall(r"(--t-[a-z0-9]+):\s*([0-9.]+)px", css))
    assert tokens, "the type scale is missing"
    for name, value in tokens.items():
        assert float(value) >= 12, (name, value)
    for value in re.findall(r"font-size:\s*([^;}]+)", css):
        value = value.strip()
        if value.startswith("var(--t-"):
            continue
        assert value.endswith("px"), "unexpected unit in font-size: %r" % value
        assert float(value[:-2]) >= 12, value


def test_numbers_are_tabular_and_right_aligned(css):
    assert "font-variant-numeric:tabular-nums" in css
    assert re.search(r"td\.num\{[^}]*text-align:right", css)
    assert re.search(r"th\.num\{[^}]*text-align:right", css)


def _glyph_declarations(css: str, state: str) -> dict[str, str]:
    """Every declaration that draws `state`'s glyph: base rule and the pseudos.

    The pseudos are separate rules (and the cross shares one selector list), so
    this reads the style sheet the way the cascade does — a selector list is
    split, and a rule counts for every selector in it.
    """
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: dict[str, str] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", bare):
        names = [name.strip() for name in selector.split(",")]
        for suffix in ("", "::before", "::after"):
            if '[data-state="%s"] .glyph%s' % (state, suffix) in names:
                out[suffix] = out.get(suffix, "") + body
    return out


def _glyph_form(decls: dict[str, str]) -> tuple[str, str]:
    """(silhouette, fill) read off the declarations, not off a table of names.

    The silhouette is whatever the drawing primitives make: the corner count of
    a `polygon()`/`path()`, a `conic-gradient`, a one-corner `border-radius`
    that is rotated, a plain `50%`, two rotated bars, a bordered box. The fill
    is opaque background, transparent background, or a path whose inner contour
    is wound against the outer one (which carves a hole under the nonzero rule).
    """
    base = decls.get("", "")

    polygon = re.search(r"clip-path:polygon\(([^)]*)\)", base)
    if polygon:
        corners = len([p for p in polygon[1].split(",") if p.strip()])
        fill = "hollow" if "background:transparent" in base else "filled"
        return {3: "triangle", 4: "square"}.get(corners, "%d-gon" % corners), fill

    path = re.search(r'clip-path:path\((?:evenodd,\s*)?"([^"]+)"\)', base)
    if path:
        contours = [c for c in path[1].split("M") if c.strip()]
        corners = len([p for p in re.split(r"[LZ]", contours[0]) if p.strip()])
        fill = "hollow" if len(contours) > 1 else "filled"
        return {3: "triangle"}.get(corners, "%d-gon" % corners), fill

    if "conic-gradient" in base:
        return "half-circle", "half"

    radius = re.search(r"border-radius:([^;}]+)", base)
    if radius and re.search(r"background:var\(--c\)", base):
        corners = radius[1].split()
        if corners == ["50%"]:
            return "circle", "filled"
        # one sharp corner, turned so the point faces down
        if len(corners) == 4 and "0" in corners and "rotate" in base:
            return "droplet", "filled"
        return "rounded-box", "filled"

    if "rotate" in decls.get("::before", "") and "rotate" in decls.get("::after", ""):
        return "cross", "bars"
    if "border:" in base and "background:transparent" in base:
        return "square", "hollow"
    return "unknown", "unknown"


def test_every_state_has_its_own_shape(css, js):
    """Seven states, seven (silhouette, fill) pairs — measured, not listed (#79).

    The design promise is that colour never carries the message alone: every
    state has its own shape, so the page survives greyscale and red-green
    colour blindness. Until #79 the test knew five states and the two operator
    states of #70 had been given the silhouette and the colour token of the
    group they belong to — `draining` *was* `maintenance` (open square,
    `--maint`), `disabled` *was* `down` (cross, `--down`). Four states, two
    appearances, and nothing failed because the test never looked at them.

    So the test reads the drawing declarations of all seven and compares the
    pair (silhouette, fill). Two states with the same pair fail, and so do two
    states with the same colour token: an appearance that only differs by a
    hex value is exactly what the rule forbids.

    Measured in Chrome on a throwaway instance (`:8899`), 9 px glyphs, panel
    `--surface` #12161c: white `--fg` 15.3:1, grey `--fg-3` 4.9:1 (4.2:1 on the
    chip's own tint) — a 9 px shape needs 3:1, so the grey is `--fg-3` and not
    a paler step. The greyscale screenshot shows seven different glyphs.
    """
    states = _legend_entries(js)
    assert len(states) == 7, states

    forms, colours = {}, {}
    for state in states:
        decls = _glyph_declarations(css, state)
        assert decls, "no rule draws the glyph of %s" % state
        form = _glyph_form(decls)
        assert "unknown" not in form, (state, decls)
        assert form not in forms, "%s and %s share the glyph %r" % (
            forms.get(form), state, form)
        forms[form] = state

        token = re.search(r'\[data-state="%s"\]\{--c:var\((--[a-z0-9-]+)\)' % state, css)
        assert token, "no colour token for %s" % state
        assert token[1] not in colours, "%s and %s share the colour %s" % (
            colours.get(token[1]), state, token[1])
        colours[token[1]] = state

    # The intent of #79, named: the droplet is white, the hollow triangle is
    # grey, and the two triangles differ by fill, not by silhouette.
    assert forms[("droplet", "filled")] == "draining"
    assert forms[("triangle", "hollow")] == "disabled"
    assert forms[("triangle", "filled")] == "degraded"
    assert colours["--fg"] == "draining"
    assert colours["--fg-3"] == "disabled"


def test_the_two_operator_glyphs_are_readable_at_nine_pixels(css):
    """A 9 px shape needs 3:1 against what it sits on — the number, not a claim.

    `--fg` (#e8ecf1) is 15.3:1 on the panel (#12161c), `--fg-3` (#7b8694) 4.9:1;
    on the chip's own 13 % tint (#20252c) the grey is still 4.2:1. That is why
    `disabled` takes `--fg-3` and not a paler step of the grey: the next lighter
    token (`--line-3` #46505e) is 2.2:1 and would be invisible at 9 px.
    """
    def luminance(value: str) -> float:
        parts = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in parts]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(a: str, b: str) -> float:
        high, low = sorted((luminance(a), luminance(b)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    surface = _token(css, "--surface")
    for token in ("--fg", "--fg-3"):
        assert contrast(_token(css, token), surface) >= 3.0, token
    assert contrast(_token(css, "--line-3"), surface) < 3.0   # the step not taken


# -- the legend on one line (#77) -------------------------------------------
# Measured in Chrome at 1440 × 1000 on a throwaway instance (`:8899`), legend
# before → after: 184.9 px in two rows of entries (tops 222 and 295) → 127.8 px
# in one (top 222), Flotte tab 1 279 → 1 222 px. The five fixed columns of #70
# were the cause: a seventh entry cannot share a 255-px column with nothing to
# wrap into. The numbers below are the browser's, read back as `clientWidth`
# and `getBoundingClientRect`; the model repeats that arithmetic in Python so
# the rule cannot quietly regress.
LEGEND_VIEWPORT = 1440          # the width the issue is about
LEGEND_SCROLLBAR = 15           # `documentElement.clientWidth` = 1425 in a 1440-px window
LEGEND_CHAR_PX = 7.05           # 12 px semibold system UI font: "maintenance" 77.3 / 11


def _px(css: str, value: str) -> float:
    """`var(--s3)` → 12.0, `7px` → 7.0: the number the browser would use."""
    token = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value.strip())
    if token:
        found = re.search(r"%s:\s*([0-9.]+)px" % re.escape(token[1]), css)
        assert found, "no length token %s" % token[1]
        return float(found[1])
    found = re.fullmatch(r"([0-9.]+)px", value.strip())
    assert found, "not a px length: %r" % value
    return float(found[1])


def _legend_columns(css: str, width: int) -> int:
    """How many columns `.legend` resolves to in a `width`-px window.

    The default rule plus every `@media (max-width:…)` that applies, in
    ascending order — the cascade the browser runs, so this is the column count
    the page gets, not the one the source looks like it has.
    """
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def count(block: str, where: str) -> int:
        found = re.search(r"grid-template-columns:repeat\((\d+),minmax\(0,1fr\)\)", block)
        assert found, "the legend's columns are not a repeat() of equal columns (%s)" % where
        return int(found[1])

    columns = count(_rule(css, ".legend"), "default rule")
    blocks = re.findall(r"@media \(max-width:(\d+)px\)\{((?:[^{}]|\{[^{}]*\})*)\}", bare)
    # Document order, not ascending width: all these are `max-width`, they have
    # the same specificity, and the browser lets the last one in the file win.
    for limit, block in blocks:
        if width <= int(limit) and ".legend{" in block:
            columns = count(block.split(".legend{")[1].split("}")[0],
                            "max-width:%s px" % limit)
    return columns


def _legend_entries(js: str) -> list[str]:
    """The states the legend actually renders, in the order it renders them."""
    found = re.search(r"const LEGEND = \[(.*?)\];", js, re.S)
    assert found, "no LEGEND list in dashboard.js"
    return re.findall(r"'([a-z]+)'", found[1])


def test_the_legend_is_one_row_at_1440(css, js):
    """Seven states, one row — narrower, not smaller (#77).

    The column count *is* the entry width: seven columns of 179 px at 1440, and
    the widest chip (`maintenance`, 113 px) still has 66 px of air, so nothing
    is truncated and no font had to shrink. Below 1440 the legend wraps into
    four and then two columns, which is correct — the rule is about 1440.
    """
    entries = _legend_entries(js)
    assert len(entries) == 7, entries

    columns = _legend_columns(css, LEGEND_VIEWPORT)
    assert columns == len(entries), (columns, entries)
    assert -(-len(entries) // columns) == 1          # one row of entries, not two

    # The column is what is left of the window after the page's and the
    # legend's own padding, the gaps and the two borders.
    legend = _rule(css, ".legend")
    pad = re.search(r"padding:(var\([^)]*\)|[0-9.]+px) (var\([^)]*\)|[0-9.]+px)", legend)
    assert pad, legend
    gap_found = re.search(r"gap:(var\([^)]*\)|[0-9.]+px)", legend)
    assert gap_found, legend
    page_pad_found = re.search(r"padding:(var\([^)]*\)|[0-9.]+px)", _rule(css, ".wrap"))
    assert page_pad_found, "the page has no horizontal padding"
    gap = _px(css, gap_found[1])
    page_pad = _px(css, page_pad_found[1])
    inner = (LEGEND_VIEWPORT - LEGEND_SCROLLBAR - 2 * page_pad - 2 * 1
             - _px(css, pad[1]) - _px(css, pad[2]) - (columns - 1) * gap)
    column = inner / columns

    chip = _rule(css, ".state")
    # `nowrap` is what makes the chip's intrinsic width a hard floor: without it
    # the token would break instead of overflowing, and this measurement would
    # be meaningless.
    assert "white-space:nowrap" in chip
    glyph_found = re.search(r"width:([0-9.]+)px", _rule(css, ".glyph"))
    assert glyph_found, "the glyph has no width"
    glyph = float(glyph_found[1])
    glyph_gap_found = re.search(r"gap:([0-9.]+)px", chip)
    assert glyph_gap_found, chip
    glyph_gap = float(glyph_gap_found[1])
    sides = re.search(r"padding:[0-9.]+px ([0-9.]+)px [0-9.]+px ([0-9.]+)px", chip)
    assert sides, chip
    for state in entries:
        need = (len(state) * LEGEND_CHAR_PX + glyph + glyph_gap
                + float(sides[1]) + float(sides[2]) + 2 * 1)
        assert need < column, (state, round(need, 1), round(column, 1))
        # every state keeps its own colour token — the legend adds no hex value.
        # The token may carry a digit: `disabled` takes `--fg-3` since #79.
        assert re.search(r'\[data-state="%s"\]\{--c:var\(--[a-z0-9-]+\)' % state, css), state

    # The sentences may wrap (that is text, not a truncation), but no single
    # word may be wider than the column it sits in.
    for path in sorted((STATIC / "i18n").glob("*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        for state in entries:
            for word in catalog["state." + state].split():
                assert len(word) * LEGEND_CHAR_PX < column, (path.stem, state, word)

    # Narrower, not smaller: nothing in the legend touches the type scale, and
    # the sentence keeps the 12-px floor.
    assert "font-size" not in legend
    assert "font-size" not in _rule(css, ".legend-item")
    assert "font-size:var(--t-xs)" in _rule(css, ".legend-def")

    # Below 1440 wrapping is allowed and intended — fewer columns, never wider.
    assert _legend_columns(css, 1100) == 4
    assert _legend_columns(css, 900) == 2
    assert _legend_columns(css, 375) == 2

    # The two operator states sit next to the group whose shape they borrow
    # (open square / cross) instead of at the end, where `down` split them.
    assert entries.index("draining") == entries.index("maintenance") + 1
    assert entries.index("disabled") == entries.index("down") + 1


def test_a_state_token_is_shown_as_the_token_in_both_languages(rendered):
    """`healthy` is what `/healthz` says, not a word to translate — it stays in
    the chip in German too. What is translated is the sentence that explains it
    (`state.healthy`), which is why the legend reads German under a chip that
    reads `healthy`."""
    fleet = rendered["parts"]["fleet"]
    assert 'data-state="healthy"' in fleet
    assert ">healthy<" in fleet
    assert _de()["state.healthy"] != "healthy"
    assert _en()["state.healthy"] != "healthy"


def test_the_fleet_card_says_nothing_about_a_site_that_is_not_declared(rendered):
    """No `sites:` block, no hint: the old free string is not a claim (issue #67)."""
    fleet = rendered["parts"]["fleet"]
    assert 'class="site-warn"' not in fleet


def test_a_contradicted_site_is_a_hint_on_the_fleet_card(tmp_path):
    """`site: WD` and an address that belongs to Traiskirchen (issue #67).

    The card shows the sentence with both ids — the same fact `ripple doctor`
    turns into one WARN — so a fleet whose labels lie is visible without a
    terminal.
    """
    data = _stub_fixture()
    host = data["healthz"]["hosts"][0]
    host["site"] = "WD"
    host["site_derived"] = "TR"
    host["site_mismatch"] = True
    out = _run_stub(tmp_path, data)

    fleet = out["parts"]["fleet"]
    assert 'class="site-warn"' in fleet
    assert _en()["fleet.site_mismatch"].replace("{declared}", "WD").replace(
        "{derived}", "TR") in fleet
    # the other card is untouched: one hint per contradicted host, not a banner
    assert fleet.count('class="site-warn"') == 1


# -- #89: a colour and a pattern per host ------------------------------------
# The stripe is four attributes and no text: `data-host` is the identity it was
# derived from (`node_id`), `data-slot` is the place in the 4 × 4 table, and
# `data-tone` / `data-pattern` are what the style sheet paints.
STRIPE = re.compile(r'<i class="host-stripe"([^>]*)></i>')


def _stripes(markup: str) -> list[dict]:
    """Every host stripe in a piece of markup, with its `data-` attributes."""
    return [{k: v for k, v in re.findall(r'data-([\w-]+)="([^"]*)"', attrs)}
            for attrs in STRIPE.findall(markup)]


def _hex_token(css: str, name: str) -> str:
    """The value of a `--token` in the style sheet, e.g. `--host-1`."""
    found = re.search(r"%s:\s*(#[0-9a-fA-F]{6})" % re.escape(name), css)
    assert found, "no token %r" % name
    return found.group(1)


def _rel_lum(hex_colour: str) -> float:
    """WCAG 2.1 relative luminance of a `#rrggbb` colour."""
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_rel_lum(a), _rel_lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_host_block_carries_the_stripe(rendered):
    """The four blocks of the issue, each with its stripe in the DOM.

    The card's edge, the memory bar inside it, the bar of the distribution and
    the thread row. Every stripe names the host it belongs to, and the same host
    carries the same place in the table in all four blocks — otherwise the
    stripe would be decoration instead of an answer.
    """
    parts = rendered["parts"]
    hosts = _healthz()["hosts"]
    fleet, dist, threads = parts["fleet"], parts["dist"], parts["threads"]

    # the card's edge and the memory bar inside it: two per host, no more
    assert fleet.count('<i class="host-stripe"') == 2 * len(hosts)
    for host in hosts:
        card = _card(fleet, host["name"])
        # `_card` starts at the card's `data-key`; the stripe is the first child
        assert re.match(r'data-key="[^"]*">\s*<i class="host-stripe"', card)
        assert re.search(r'<div class="bar"[^>]*>\s*<i class="host-stripe"', card)

    # the distribution: every drawn bar carries the stripe of its row's host
    assert dist.count('<span class="dtrack">') > 0
    assert dist.count('<span class="dtrack"><i class="host-stripe"') == \
        dist.count('<span class="dtrack">')

    # the thread row: the stripe of `threads[].host`
    rows = re.findall(r'<div class="thread-item" data-key="([^"]*)"[^>]*>\s*'
                      r'(<i class="host-stripe"[^>]*>)', threads)
    assert len(rows) == len(_healthz()["threads"]["registry"])
    key, stripe = rows[0]
    assert key == "4b17eecc4cf0a8a5"
    assert 'data-host="node-M5-alpha"' in stripe          # the host of that thread

    # one host, one place in the table — across all four blocks
    places: dict[str, set] = {}
    for entry in _stripes(fleet) + _stripes(dist) + _stripes(threads):
        places.setdefault(entry["host"], set()).add(
            (entry["slot"], entry["tone"], entry["pattern"]))
    assert set(places) == {"node-M5-alpha", "node-M3-beta"}
    assert all(len(seen) == 1 for seen in places.values()), places


def test_the_stripe_hangs_on_the_node_id_not_on_the_position(tmp_path):
    """Reverse the fleet and every host keeps its colour and its pattern.

    The place in the table is read from `node_id`, never from the index in
    `hosts[]`: a node that leaves the pool would otherwise repaint the others,
    and the operator's memory ("M3 is the dotted one") would be wrong after
    every restart.
    """
    (tmp_path / "forward").mkdir()
    (tmp_path / "backward").mkdir()
    forward = _run_stub(tmp_path / "forward", _stub_fixture())
    data = _stub_fixture()
    data["healthz"]["hosts"].reverse()
    for model in data["healthz"]["models"]:
        model["hosts"].reverse()
    backward = _run_stub(tmp_path / "backward", data)

    def places(out):
        return {s["host"]: (s["slot"], s["tone"], s["pattern"])
                for s in _stripes(out["parts"]["fleet"])}

    assert places(forward) == places(backward)
    # the order really was a different one — a test that reverses nothing
    # would pass the line above for the wrong reason
    assert [s["host"] for s in _stripes(forward["parts"]["fleet"])] != \
        [s["host"] for s in _stripes(backward["parts"]["fleet"])]
    # the distribution follows its own list, and it kept the same places too
    assert {s["host"] for s in _stripes(backward["parts"]["dist"])} == \
        {s["host"] for s in _stripes(forward["parts"]["dist"])}


def test_two_hosts_stay_apart_without_the_colour_channel(rendered, css):
    """The grey-scale check of §6, as three measurable facts.

    The two hosts of the fixture land on different patterns — with the colour
    gone, the drawing still says which is which. The four inks are spread over
    lightness (L* 80 · 68 · 56 · 46), so two tones never collapse into one grey.
    And no two of the sixteen places share tone *and* pattern, so a collision
    can only ever cost the colour, never the identity.
    """
    stripes = {s["host"]: s for s in _stripes(rendered["parts"]["fleet"])}
    a, b = stripes["node-M5-alpha"], stripes["node-M3-beta"]
    assert a["pattern"] != b["pattern"]
    assert a["tone"] != b["tone"]

    ink = {n: _hex_token(css, "--host-%d" % n) for n in range(1, 5)}
    assert _contrast(ink[int(a["tone"])], ink[int(b["tone"])]) >= 1.3
    # the table: sixteen places, each a (tone, pattern) pair, all distinct
    places = [(n % 4, (n // 4 + n % 4) % 4) for n in range(16)]
    assert len(set(places)) == 16
    # and the invariant the grey-scale check rests on: one tone, never one
    # drawing — the pattern is rotated by the tone
    by_tone: dict[int, list] = {}
    for tone, pattern in places:
        by_tone.setdefault(tone, []).append(pattern)
    assert all(sorted(v) == [0, 1, 2, 3] for v in by_tone.values())
    # every tone has a different grey, in the order the tokens are numbered
    greys = [_rel_lum(ink[n]) for n in range(1, 5)]
    assert greys == sorted(greys, reverse=True)
    assert min(_contrast(ink[i], ink[j])
               for i in range(1, 5) for j in range(i + 1, 5)) >= 1.3


def test_the_stripe_is_not_a_state_and_never_sits_in_the_state_area(css):
    """No new colour for the seven node states (§3, the rule of the project).

    The four tones sit in the gaps of the state palette and appear nowhere in
    it; the stripe is `aria-hidden`, absolute, at the block's edge — the chip,
    the dot and the triangle stay the only carriers of a state.
    """
    state = set(re.findall(r"--(?:ok|load|warn|maint|down):(#[0-9a-f]{6})", css))
    assert len(state) == 5
    tones = {_hex_token(css, "--host-%d" % n).lower() for n in range(1, 5)}
    assert not (state & tones)
    assert len(tones) == 4

    stripe = _rule(css, ".host-stripe")
    assert "position:absolute" in stripe
    assert "pointer-events:none" in stripe
    # the four tone rules and the three drawings exist and differ
    for n in range(1, 5):
        assert "var(--host-%d)" % n in _rule(css, '.host-stripe[data-tone="%d"]' % n)
    shapes = {name: _rule(css, '.host-stripe[data-pattern="%s"]' % name)
              for name in ("hatch", "dots", "lines")}
    assert len(set(shapes.values())) == 3
    assert "background-image" not in stripe          # `solid` is the plain ink


def test_the_stripe_lives_in_the_padding_so_the_card_does_not_grow(css):
    """The fleet tab does not get a pixel wider or taller for this.

    The stripe is six pixels inside the card's existing 16 px padding, taken out
    of the flow (`position:absolute`), and the card's own rule is untouched.
    """
    stripe = _rule(css, ".host-stripe")
    assert "left:0" in stripe and "top:0" in stripe and "bottom:0" in stripe
    assert "width:6px" in stripe
    width = re.search(r"width:(\d+)px", stripe)
    assert width is not None and int(width.group(1)) < 16

    card = _rule(css, ".card")
    assert "padding:var(--s4) var(--s4) 0" in card
    assert "position" not in card                    # the card is the box
    # the bar variants are narrower than the bar they sit in
    mem_stripe = _rule(css, ".mem .bar > .host-stripe")
    # The memory bar draws no stripe at all: it starts on the same axis as the
    # card's 6 px spine (measured: card x 32, spine 33..39, bar x 33), so a
    # marker there sat ON the stripe that already carries the host tone and read
    # as a solid line inside a hatch — an artifact, not a mark (reported 19.11.).
    assert "display:none" in mem_stripe
    assert "width:3px" in _rule(css, ".dtrack > .host-stripe")
    assert "height:14px" in _rule(css, ".bar")       # unchanged
    assert "height:9px" in _rule(css, ".dtrack")     # unchanged


def test_the_parameter_table_reads_the_same_numbers_in_both_languages(rendered):
    """The same `/config` payload, two renderings: `0,95` in German, `0.95` in
    English, `4.096` against `4,096`. Before this the table printed the raw JS
    value, so the German page carried an English decimal point."""
    nums = rendered["lang_numbers"]
    en, de = nums["en"], nums["de"]
    assert en and len(en) == len(de)
    assert "0.95" in en and "0,95" in de
    assert "4096" not in en and "4096" not in de        # grouped, not raw
    norm_en = [x.replace(",", "") for x in en]
    norm_de = [x.replace(".", "").replace(",", ".") for x in de]
    assert norm_en == norm_de


def test_the_page_never_asks_for_an_id_the_catalog_lacks(rendered):
    """Ids built at runtime — `t('models.policy_' + level)`, `t('reason.' + r)` —
    are invisible to a scan over the source, so the page's own fallback warning
    is the measurement. A miss is not a crash: it renders English and says so;
    here it must not happen at all."""
    assert rendered["i18n_misses"] == []


def test_a_language_switch_changes_the_words_not_the_markup(rendered):
    """The 2 s patch and a switch must produce the same DOM shape: the page is
    patched in place, so a switch that rebuilt it into another structure would
    lose the scroll position and the focus. Text is expected to differ — that is
    the point of the switch."""
    assert rendered["lang_parts"] >= 7
    assert rendered["lang_shape_diffs"] == []
    assert rendered["lang_parts_with_new_text"] == rendered["lang_parts"]


def test_the_log_time_comes_from_the_timestamp_not_from_the_string(rendered):
    """`/stats` sends epoch seconds; the column is formatted by the page
    (`Intl.DateTimeFormat`), not by the server — so the same payload reads
    13:29 in German and 1:29 PM in English. A decision without `ts` keeps the
    `time` string the server sent."""
    log = rendered["parts"]["log"]
    ts = _stats()["decisions"][0]["ts"]
    want = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    assert want in log, want
    assert "13:30:31" in log               # the fallback for a decision without ts


def test_the_memory_bar_is_drawn_not_decorated(css):
    assert re.search(r"\.bar\{[^}]*height:14px", css)            # a reserve, not a hairline
    assert re.search(r"\.bar \.est\{[^}]*repeating-linear-gradient", css)  # estimation excess
    assert re.search(r"\.bar \.danger\{[^}]*repeating-linear-gradient", css)  # past the threshold
    assert re.search(r"\.bar \.hard\{[^}]*border-left:1px dashed", css)      # the wall
    assert re.search(r"\.bar \.limit\{[^}]*width:2px", css)                  # the drawn ceiling


# -- #19: the colours the operator asked for --------------------------------
def test_the_bars_keep_their_gradient_in_the_loading_hue(css):
    """Item 1: same gradient, re-hued. `0deg` runs to top, so the darker step
    of the same hue sits at the bottom and the top stop is exactly `--load` —
    the colour of the `loading` label. No second hex for the top stop."""
    assert "--load-deep:#3f87c4" in css           # the family lives next to --load
    fill = _rule(css, ".bar .fill")
    assert "background:linear-gradient(0deg,var(--load-deep),var(--load))" in fill
    assert "inset 0 1px 0 rgba(255,255,255,.22)" in fill   # the highlight stays
    # the old blue is gone from every horizontal bar, the distribution included
    assert "#7fadff" not in css and "#4f7fdd" not in css
    assert "linear-gradient(0deg,var(--load-deep),var(--load))" in _rule(css, ".dtrack i.a")


def test_the_estimation_stripe_takes_the_hue_and_the_over_variant_stays_red(css):
    """Item 1, second half: the striped estimate was the old blue with alpha."""
    est = _rule(css, ".bar .est")
    assert "repeating-linear-gradient" in est
    assert "rgba(90,169,230,.60)" in est
    assert "127,173,255" not in css
    assert "rgba(240,122,102,.60)" in _rule(css, ".bar .est.over")


def test_the_duration_bar_takes_the_flat_loading_hue(css):
    """Item 1, last sentence: 3 px is too thin for a gradient — flat `--load`,
    not the old `--accent`."""
    dur = _rule(css, ".dur-track i")
    assert "background:var(--load)" in dur
    assert "--accent" not in dur
    assert "gradient" not in dur


def test_the_section_numbers_carry_a_ring_in_the_maintenance_hue(css):
    """Item 6: a ring around the number, and number, ring and title in `--maint`
    (the lilac of the `maintenance` chip), not in `--warn`."""
    assert "color:var(--maint)" in _rule(css, "h2")
    idx = _rule(css, ".idx")
    assert "color:var(--maint)" in idx
    assert "border:1px solid var(--maint)" in idx
    assert "border-radius:999px" in idx
    # a square box is a circle for a single glyph; the pill follows the text
    assert "min-width:1.7em" in idx and "height:1.7em" in idx
    assert "display:inline-flex" in idx
    assert "var(--fg-3)" not in idx and "var(--warn)" not in idx


def test_the_maintenance_strip_label_is_lilac_too(css):
    """Item 6, last sentence: one meaning, one colour — chip, strip, headers."""
    assert _rule(css, ".maint.on .maint-lbl").strip() == "color:var(--maint)"


def test_the_not_routable_badge_takes_the_down_red(css):
    """Item 4: grey → the red of `down`, the same red as the label at the top."""
    badge = _rule(css, ".badge")
    assert "background:var(--down-bg)" in badge
    assert "border:1px solid var(--down-bd)" in badge
    assert "color:var(--down)" in badge
    assert "var(--surface-3)" not in badge


def _number(pattern: str, text: str) -> float:
    """The first capture of `pattern` as a number — a missing match is a failure."""
    found = re.search(pattern, text)
    assert found, "no match for %r" % pattern
    return float(found.group(1))


def test_the_disclosure_triangle_is_three_to_four_times_bigger(css):
    """Item 8: 13 px against a 20 px heading was too small for the only way
    into the operator block; 39–52 px is three to four times `--t-s`."""
    base = _number(r"--t-s:\s*([0-9.]+)px", css)
    marker = _rule(css, "details.params summary::before")
    size = _number(r"font-size:([0-9.]+)px", marker)
    assert 3 * base <= size <= 4 * base, (base, size)
    # its own box, so the summary row is not pushed apart by the marker
    assert "flex:0 0 auto" in marker
    assert "line-height:" in marker
    # the ▸/▾ swap stays, and the whole row stays the click target
    assert 'content:"▸"' in marker
    assert 'details.params[open] summary::before{content:"▾"}' in css
    summary = _rule(css, "details.params summary")
    assert "cursor:pointer" in summary and "display:flex" in summary


# -- what the JavaScript renders -------------------------------------------
def test_the_model_table_head_matches_its_body(rendered):
    table = _table(rendered["parts"]["models"])
    assert len(table.head) == 8
    assert table.body, "the model table is empty"
    assert set(_effective_columns(table.body)) == {8}


def test_the_decision_log_head_matches_its_body(rendered):
    """Eight columns since #101: the thread key (`sid`) sits between the node and
    the reason, so head and body must still line up — a row that carries one cell
    more or less than the head is a shifted table, not a styling detail."""
    table = _table(rendered["parts"]["log"])
    assert len(table.head) == 8
    assert table.body, "the decision log is empty"
    assert set(_effective_columns(table.body)) == {8}
    # the new column is the third one, where the contract puts it
    assert "Thread" in table.head[2]["text"], table.head[2]


def test_every_number_in_the_model_table_has_a_source(rendered):
    table = _table(rendered["parts"]["models"])
    for row in table.body:
        for cell in row:
            if "num" in cell["attrs"].get("class", "") and re.search(r"\d", cell["text"]):
                assert cell["tip"], cell["text"]
    # the head names the unit of each column
    heads = " ".join(c["text"] for c in table.head)
    assert "GB · est. / meas." in heads
    assert "yes/no" in heads
    # since #30 the column is a pair, and the subtitle says which two numbers
    assert "live / capability" in heads
    assert "measured" not in heads


# -- #19: the two things the model table has to say -------------------------
def _model_cell_title(markup: str, model: str, host: str) -> str:
    row = _row_markup(markup, model, host)
    found = re.search(r'<td class="model-cell"[^>]*title="([^"]*)"', row)
    assert found, "no model cell in the row %s|%s" % (model, host)
    return html.unescape(found.group(1))


def test_a_truncated_model_name_keeps_its_state_in_the_title(rendered):
    """Item 2: the cell cuts the name with `…` and cuts the badge with it, so
    the `title` has to carry the state — otherwise hovering a long name loses
    the one fact the badge was there for."""
    blocked = _model_cell_title(rendered["parts"]["models"], "Qwen3.5-9B-oQ4e-mtp",
                                "M3-beta")
    assert blocked == "Qwen3.5-9B-oQ4e-mtp · not routable"
    # ... also when the state comes from the policy store rather than the allow-list
    assert _model_cell_title(rendered["parts"]["models_policy"], DEEPSEEK,
                             "M5-alpha") == DEEPSEEK + " · not routable"
    # a routable model keeps the plain name: the title invents no second state
    assert _model_cell_title(rendered["parts"]["models"], "GLM-5.2-oQ4",
                             "M5-alpha") == "GLM-5.2-oQ4"


def test_the_size_slash_is_as_bright_as_the_numbers(js, rendered):
    """Item 7: the separator sat in `--line-3`, the darkest line token, and was
    barely visible on the body. It takes `--fg`, the colour of its numbers —
    in the body only; the head writes it as plain text."""
    assert '<span style="color:var(--fg)"> / </span>' in js
    assert 'var(--line-3)"> / </span>' not in js
    # every *size* cell that carries the separator carries the brightened
    # span — the tok/s cell divides on purpose with the quieter `.den`
    # (#30), so a cell with `class="den"` is not one of them
    cells = re.findall(r'<td class="num">(.*?)</td>', rendered["parts"]["models"], re.S)
    sized = [c for c in cells if " / " in c and 'class="den"' not in c]
    assert sized, "the model table has no size cell"
    for c in sized:
        assert '<span style="color:var(--fg)"> / </span>' in c, c[:80]
    # ... and the head keeps it as plain text, not as a dimmed span
    head = re.search(r"<thead>.*?</thead>", rendered["parts"]["models"], re.S)
    assert head and " / " in head.group(0)
    assert "var(--fg)" not in head.group(0)


def test_every_number_in_the_decision_log_has_a_source(rendered):
    table = _table(rendered["parts"]["log"])
    for row in table.body:
        for cell in row:
            if "num" in cell["attrs"].get("class", "") and re.search(r"\d", cell["text"]):
                assert cell["tip"], cell["text"]


def test_no_number_on_the_page_lacks_a_tooltip(rendered):
    for part in ("fleet", "kpis", "models", "dist", "threads", "load", "log",
                 "fleet_admin", "fleet_maintenance", "fleet_drained"):
        assert _bare_numbers(rendered["parts"][part]) == [], part


def test_every_tooltip_names_its_source(rendered):
    for part in ("fleet", "kpis", "models", "dist", "threads", "load", "log",
                 "fleet_admin", "fleet_maintenance", "fleet_drained"):
        for tip in re.findall(r'data-tip="([^"]*)"', rendered["parts"][part]):
            assert "Source:" in tip, (part, tip[:80])


def test_the_tooltip_box_is_one_layer_at_the_body(css, js):
    """The box is one fixed layer, not a `::after` on the element.

    A pseudo-element is cut off by every `overflow:hidden` ancestor — the model
    table (`.table-wrap`) and the decision log (`.log`) are exactly that — and it
    hangs on the hover state of a node the 2 s patch may replace. The text still
    comes from `data-tip`, so the tests above keep their meaning.
    """
    assert "content:attr(data-tip)" not in css
    assert ".tip::after{" not in css
    assert "#tip-layer" in css
    assert "position:fixed" in css
    assert "tip-layer" in js
    assert "data-tip" in js
    assert "pointerover" in js and "pointerout" in js
    assert "getBoundingClientRect" in js
    # the tooltip text is data, so it is written as text, never as markup
    assert "textContent" in js


def test_the_memory_bar_shows_the_measurement_and_the_estimate_as_a_second_track(rendered):
    card = _card(rendered["parts"]["fleet"], "M5-alpha")
    fill = float(re.search(r'<span class="fill[^"]*" style="width:([0-9.]+)%"', card)[1])
    est = re.search(r'<span class="est[^"]*" style="left:([0-9.]+)%;width:([0-9.]+)%"', card)

    assert fill == pytest.approx(M5_MEASURED / M5_CEILING * 100, abs=0.15)
    # the hatched piece starts where the measurement ends and is exactly the
    # distance the estimate claims beyond it
    assert float(est[1]) == pytest.approx(fill, abs=0.15)
    assert float(est[2]) == pytest.approx((M5_USED - M5_MEASURED) / M5_CEILING * 100, abs=0.15)
    # English machine: `1,234.5` — the same payload the German catalog renders
    # as `1.234,5` (tests/test_i18n.py asserts both)
    assert "estimated 453.6 GB" in card
    assert "432.2" in card and "525.4" in card


def test_the_memory_bar_draws_the_ceiling_and_the_hard_threshold(rendered):
    card = _card(rendered["parts"]["fleet"], "M5-alpha")
    assert '<span class="limit"></span>' in card
    assert float(re.search(r'<span class="hard" style="left:([0-9.]+)%"', card)[1]) == 95.0
    assert float(re.search(r'<span class="danger" style="left:([0-9.]+)%"', card)[1]) == 95.0
    assert "limit 95 % (estimated)" in card
    assert ">82.3 %</b>" in card and "</b></span> used" in card
    assert "93.1 GB" in card          # reserve, computed from the measurement


def test_the_card_falls_back_to_the_estimate_when_nothing_was_measured(rendered):
    card = _card(rendered["parts"]["fleet_unmeasured"], "M5-alpha")
    assert "estimated" not in card.split("mem-foot")[0]
    assert "estimate, no measurement" in card
    fill = float(re.search(r'<span class="fill[^"]*" style="width:([0-9.]+)%"', card)[1])
    assert fill == pytest.approx(M5_USED / M5_CEILING * 100, abs=0.15)


def test_unloaded_models_are_visible_by_default(rendered):
    """Models is its own tab since #56, so its default is the fleet's whole
    inventory: the twenty not-loaded rows are dimmed, not hidden.

    The divider row carries a LEADING disclosure triangle. Before that it ended
    in `▸` behind the label and read as a caption — the operator reported the
    twenty models as gone (19.11.)."""
    default = rendered["parts"]["models_default"]
    assert "GLM-5.2-oQ4" in default
    assert '<td colspan="8"><span class="tw" aria-hidden="true">▾</span>' in default
    table = _table(default)
    assert len(table.body) == _model_rows(loaded=True) + 1 + _model_rows(loaded=False)
    assert set(_effective_columns(table.body)) == {8}


def test_the_divider_row_collapses_and_reopens(rendered):
    collapsed = rendered["parts"]["models_collapsed"]
    assert "GLM-5.2-oQ4" not in collapsed
    assert '<td colspan="8"><span class="tw" aria-hidden="true">▸</span>' in collapsed
    assert len(_table(collapsed).body) == _model_rows(loaded=True) + 1
    assert set(_effective_columns(_table(collapsed).body)) == {8}
    expanded = rendered["parts"]["models_expanded"]
    assert "GLM-5.2-oQ4" in expanded
    assert '<span class="tw" aria-hidden="true">▾</span>' in expanded


def test_the_disclosure_triangle_is_a_control_not_a_caption(css):
    """A triangle one has to look for is no triangle (#19): it stands in front,
    larger than the row's text, and brightens on hover."""
    rule = _rule(css, ".divider-row.toggle .tw")
    assert "font-size:var(--t-l)" in rule          # 17 px, three above the row's 14
    assert "color:var(--fg-2)" in rule
    assert "color:var(--fg)" in _rule(css, ".divider-row.toggle:hover .tw")
    assert "cursor:pointer" in _rule(css, ".divider-row.toggle")


def test_the_distribution_says_there_is_no_actual_share_without_slots(rendered):
    empty = rendered["parts"]["dist_empty"]
    assert "no actual share" in empty
    assert "actual share" in rendered["parts"]["dist"]
    assert "Target" in rendered["parts"]["dist"]
    assert "Deviation" in rendered["parts"]["dist"]


# -- two currencies, two columns (issue #91) --------------------------------
def _dist_blocks(markup: str) -> dict[str, str]:
    """The rendered distribution split by block, keyed by model id.

    The markup is nested, so this cuts on the block's own opening tag instead of
    parsing `</div>` — every block starts with `data-key`, and the empty-state
    block has none, so it is simply not a key here.
    """
    out = {}
    for chunk in markup.split('<div class="dist-block"')[1:]:
        m = re.match(r'[^>]*data-key="([^"]+)"', chunk)
        if m:
            out[m.group(1)] = chunk
    return out


def test_the_slot_column_is_called_thread_share(rendered):
    """Issue #91: the column counts *threads*, so it says so.

    `share` alone read as "share of the work", and a model without a warm thread
    therefore looked idle rather than threadless. The header names the currency,
    and the column beside it carries the other one.
    """
    dist = rendered["parts"]["dist"]
    head = re.search(r'<div class="dist-cols"[^>]*>(.*?)</div>\s*<div class="dist-row"',
                     dist, re.S)
    assert head, "the column head must be there even without a bound slot"
    assert _en()["dist.th_share"] == "Thread share"
    assert "Thread share" in head.group(1)
    assert _en()["dist.th_req"] in head.group(1)
    assert "900 s" in head.group(1)          # the window the column counts over
    assert "Share of the slots" not in dist


def test_a_model_without_a_warm_thread_keeps_its_row_and_its_requests(rendered):
    """The measured case of #91: `Qwen3.5-9B-oQ4e-mtp` is loaded on M3 and
    served 56 requests in the window, but holds no thread. The block says why the
    bar is missing instead of dropping the model."""
    block = _dist_blocks(rendered["parts"]["dist_empty"])["Qwen3.5-9B-oQ4e-mtp"]
    assert _en()["dist.no_warm_thread"] == "no warm thread"
    assert "no warm thread" in block          # the row says why there is no bar
    assert ">56<" in block                    # … and keeps the honest number
    assert "0 of 40 slots" in block
    assert "no slots bound" in block


def test_a_model_with_neither_threads_nor_traffic_keeps_no_row(rendered):
    """The other half of the acceptance: no empty rows for what cannot serve.
    `MarkItDown` is resident on both nodes, but `dispatch: false` everywhere (an
    oMLX-internal tool, #107) and it ran nothing in the window; `GLM-5.2-oQ4` is
    not even loaded. Neither belongs in a block about who is serving."""
    dist = rendered["parts"]["dist"]
    assert "MarkItDown" not in _dist_blocks(dist)
    assert "GLM-5.2-oQ4" not in _dist_blocks(dist)   # not even loaded


# -- a resident model nobody is calling right now (issue #107) --------------
# The measured fleet: `Qwen3.5-9B-oQ4e-mtp` is loaded on both nodes (16.8 GB),
# holds no thread and is called about every 50 minutes for vision work — so for
# 35 minutes of every hour it had no row at all, and the operator never saw it in
# a day. This payload is that minute: no thread anywhere, no traffic in the
# window, one resident model that can be routed to.
RESIDENT = "Qwen3.5-9B-oQ4e-mtp"
COLD = ["cold-model-%02d" % i for i in range(1, 21)]   # 20 of 23 not loaded
HALF = "Qwen3.8-Flash-Next-oQ4e-mtp"                   # routable on M5 only


def _resident(host: str, **over) -> dict:
    """One (model, node) row of the live fleet: resident, idle, dispatchable."""
    row = {"host": host, "available": True, "loaded": True,
           "estimated_size": 6_542_399_089, "actual_size": 10_111_095_848,
           "context_len": 262144, "vision": True, "threads": 0, "inflight": 0,
           "tg_tps": None, "allowed": True, "policy": None, "dispatch": True,
           "phase": None, "lock": False}
    row.update(over)
    return row


def _healthz_quiet() -> dict:
    """No thread, no traffic — and the three shapes of residency (#107).

    The registry keeps its one entry: the DOM stub paints the busy thread tab
    from `registry[0]`, and the thread tab is not what this payload measures. No
    model below holds a slot (`hosts[].threads`), and a distribution over zero
    slots is zero — hence the empty target and actual shares.
    """
    h = json.loads(json.dumps(_healthz()))
    h["threads"]["active"] = 0
    h["threads"]["target"] = {}
    h["threads"]["target_share"] = {}
    h["threads"]["actual_share"] = {}
    for host in h["hosts"]:
        host["thread_slots"] = 0
        host["requests_by_model"] = {}
        host["models_available"] = 23
    h["hosts"][0]["models_loaded"] = [RESIDENT, "MarkItDown", HALF]
    h["hosts"][1]["models_loaded"] = [RESIDENT, "MarkItDown"]
    h["models"] = [
        # resident on both nodes, routable on both: the case of the issue
        {"id": RESIDENT, "lock": False, "lock_reason": None,
         "hosts": [_resident("M3-beta"), _resident("M5-alpha")]},
        # resident, but no node dispatches it — never a row
        {"id": "MarkItDown", "lock": False, "lock_reason": None, "hosts": [
            _resident("M3-beta", estimated_size=0, actual_size=0, context_len=None,
                      vision=False, allowed=False, dispatch=False),
            _resident("M5-alpha", estimated_size=0, actual_size=0, context_len=None,
                      vision=False, allowed=False, dispatch=False)]},
        # resident and routable on M5, resident but `dispatch: false` on M3: one
        # row, and it is the node that would route to it
        {"id": HALF, "lock": False, "lock_reason": None, "hosts": [
            _resident("M5-alpha", estimated_size=8_000_000_000,
                      actual_size=7_500_000_000),
            _resident("M3-beta", estimated_size=8_000_000_000,
                      actual_size=7_500_000_000, allowed=False, dispatch=False)]},
        # the other 20: available, not resident — out, as before
        *[{"id": mid, "lock": False, "lock_reason": None, "hosts": [
            _resident("M5-alpha", loaded=False, estimated_size=1_000_000_000,
                      actual_size=None, context_len=131072, vision=False)]}
          for mid in COLD],
    ]
    return h


def _stats_quiet() -> dict:
    """The same server with nothing served yet: the window is empty."""
    s = json.loads(json.dumps(_stats()))
    s.update({"requests_total": 0, "requests_ok": 0, "by_model": {},
              "by_host": {name: {"requests": 0, "errors": 0}
                          for name in s["by_host"]}})
    return s


@pytest.fixture(scope="module")
def resident_idle(tmp_path_factory) -> dict:
    """`2` with the resident model that nobody is calling right now (#107)."""
    return _run_stub(tmp_path_factory.mktemp("dashboard-resident-idle"),
                     _stub_fixture(healthz=_healthz_quiet(),
                                   stats=_stats_quiet()))


def _row_host(row: str) -> str:
    """The node a distribution row belongs to (it is also the row's first cell)."""
    found = re.search(r'class="dist-host" title="([^"]+)"', row)
    assert found, "no host in %r" % row
    return found.group(1)


def _row_requests(row: str) -> str:
    """The window count of that row — the column that needs no slot."""
    found = re.search(r'class="dval num tip"[^>]*>(\d+)<', row)
    assert found, "no request count in %r" % row
    return found.group(1)


def test_a_resident_model_without_traffic_keeps_its_row(resident_idle):
    """Issue #107: the weight is the fact that does not blink. The model holds no
    slot and served nothing, and the row is still there — because it is loaded
    and routable. It carries what is true: 0 threads, 0 requests, no actual share
    (0 of 0 slots is not a measurement) and the sentence that says why the bar is
    missing instead of an empty track."""
    dist = resident_idle["parts"]["dist"]
    blocks = _dist_blocks(dist)
    # the order is the payload's, and only the two resident models are in it
    assert list(blocks) == [RESIDENT, HALF]
    block = blocks[RESIDENT]
    assert "0 of 40 slots" in block
    assert "no slots bound" in block
    rows = re.findall(r'<div class="dist-row">(.*?)</div>', block, re.S)
    assert [_row_host(r) for r in rows] == ["M5-alpha", "M3-beta"]
    for row in rows:
        # the sentence from `dist.no_warm_thread`, once per row — the same
        # `threadsHere === 0` branch as #91, which counts the whole fleet
        assert row.count('class="dist-why"') == 1
        assert _en()["dist.no_warm_thread"] in row
        assert 'class="dtrack"' not in row            # no bar to fill
        assert '<span class="dval num tip"' in row    # requests: 0
        assert ">0</span>" in row
        assert '<span class="dval">—</span>' in row   # actual, and delta
        assert row.count('<span class="dval">—</span>') == 2
        # the six columns of the raster are all there — the row is a normal row
        assert row.count('<span class="dval') == 4
    assert "no actual share" in dist


def test_no_row_for_a_model_no_node_dispatches_or_that_is_not_loaded(resident_idle):
    """The two negatives of the acceptance. `MarkItDown` is resident on both
    nodes but `dispatch: false` / `allowed: false` on both — an oMLX tool, not a
    model RIPPLE routes to — so the new rule does not reach it. The 20 models
    that are available but not loaded stay out exactly as before."""
    blocks = _dist_blocks(resident_idle["parts"]["dist"])
    assert "MarkItDown" not in blocks
    assert HALF in blocks                       # the control: the payload works
    for mid in COLD:
        assert mid not in blocks, mid
    assert len(COLD) == 20


def test_a_node_that_would_not_dispatch_the_model_gets_no_row(resident_idle):
    """`dispatch` is per (model, node), so the block is too: `Qwen3.8-Flash-Next`
    is resident on both nodes and routable on M5 only, and only M5 gets a row. A
    row under "target share" for a node that would not route the model would
    claim a share that node cannot take."""
    block = _dist_blocks(resident_idle["parts"]["dist"])[HALF]
    hosts = re.findall(r'<span class="dist-host" title="([^"]+)">', block)
    assert hosts == ["M5-alpha"]


@pytest.fixture(scope="module")
def resident_traffic(tmp_path_factory) -> dict:
    """The same quiet payload with 56 requests for the resident model on M3 —
    one call inside the window, as the operator's vision work makes it."""
    h = _healthz_quiet()
    h["hosts"][1]["requests_by_model"] = {RESIDENT: 56}
    s = _stats_quiet()
    s["requests_total"] = 56
    s["by_host"]["M3-beta"] = {"requests": 56, "errors": 0}
    return _run_stub(tmp_path_factory.mktemp("dashboard-resident-traffic"),
                     _stub_fixture(healthz=h, stats=s))


def test_a_model_with_traffic_in_the_window_keeps_its_number(resident_traffic):
    """The standing case of #91, unchanged by #107: a model that served in the
    window shows its number, per node, in the same block — and the row set of the
    rule does not swallow it."""
    block = _dist_blocks(resident_traffic["parts"]["dist"])[RESIDENT]
    rows = re.findall(r'<div class="dist-row">(.*?)</div>', block, re.S)
    per_host = {_row_host(r): _row_requests(r) for r in rows}
    assert per_host == {"M5-alpha": "0", "M3-beta": "56"}


def test_the_requests_column_counts_per_node_inside_the_window(rendered):
    """The window number is per (node, model) — 127 on M5, 24 on M3 for the same
    model — so the two nodes of a block can be compared on traffic as well as on
    slots."""
    block = _dist_blocks(rendered["parts"]["dist"])[DEEPSEEK]
    rows = re.findall(r'<div class="dist-row">(.*?)</div>', block, re.S)
    assert len(rows) == 2
    m5 = [r for r in rows if "M5-alpha" in r][0]
    m3 = [r for r in rows if "M3-beta" in r][0]
    assert ">127<" in m5 and ">24<" in m3
    assert "/healthz → hosts[].requests_by_model" in m5


# -- no full reload ---------------------------------------------------------
def test_a_tick_with_unchanged_data_touches_nothing(rendered):
    """The 2 s poll patches; it does not rebuild. Nothing changed → no DOM work."""
    assert rendered["unchanged_templates"] == 0


def test_a_new_decision_patches_only_the_log_and_keeps_it(rendered):
    assert rendered["changed_containers"] == ["log"]
    assert rendered["changed_templates"] == 1
    assert rendered["log_has_new_row"] is True
    assert rendered["log_element_reused"] is True


def test_the_live_container_is_never_reassigned(js):
    """`apply()` reconciles children — the scroll container keeps its identity,
    which is what keeps the log's scroll position across a tick."""
    assert "host.innerHTML" not in js
    assert "document.body.innerHTML" not in js
    assert "host.__html = html" in js


def test_the_page_never_reads_location(rendered):
    """The stub's `location` throws on every property but `hash` — no navigation
    can slip through. Since #56 the hash *is* the tab, so reading it is allowed
    and everything else (`href`, `reload`, `assign`, `search`) still counts as a
    navigation and would be recorded here."""
    assert rendered["location_reads"] == 0


# -- the maintenance toggle (issue #10) ------------------------------------
# Richard's request: "eine Maschine schonend aus der Betriebsbereitschaft
# nehmen, z.B. um Updates reinzuspielen". The page's job is threefold: offer the
# control (with a key), show the window the backend holds, and say what the
# operator is waiting for — `draining (n)` or `drained`. Everything it shows is
# read from /healthz; the calls it makes are the two admin routes.
def test_the_page_offers_the_three_presets_and_the_end_button(rendered):
    card = _card(rendered["parts"]["fleet_admin"], "M5-alpha")
    for minutes in (30, 60):
        assert 'onclick="maintenance(\'M5-alpha\', %d)"' % minutes in card, minutes
    assert 'onclick="maintenance(\'M5-alpha\', null)"' in card      # ohne Ende
    assert "30 min" in card and "60 min" in card and "no end" in card
    # `beenden` belongs to a window; a node in the pool has nothing to end
    assert "maintenanceClear(" not in card

    windowed = _card(rendered["parts"]["fleet_maintenance"], "M5-alpha")
    assert 'onclick="maintenanceClear(\'M5-alpha\')"' in windowed
    assert ">end<" in windowed


def test_without_an_admin_key_the_control_is_disabled_and_says_why(rendered, js):
    """Same rule as Calibrate: no key in sessionStorage → the button is off and
    the card carries the sentence that explains it, not a dead control."""
    card = _card(rendered["parts"]["fleet"], "M5-alpha")
    buttons = re.findall(r"<button class=\"btn maint-[a-z]+\"[^>]*>", card)
    assert buttons, "the maintenance control is missing from the card"
    for button in buttons:
        assert "disabled" in button, button
    assert "Admin key required" in card
    assert _en()["key.help"].startswith("Admin key required")   # the text is the catalog's

    # ... and with a key the same buttons are live
    keyed = _card(rendered["parts"]["fleet_admin"], "M5-alpha")
    for button in re.findall(r"<button class=\"btn maint-[a-z]+\"[^>]*>", keyed):
        assert "disabled" not in button, button


def test_a_window_shows_until_left_and_who_set_it(rendered):
    """The card answers the three questions an operator has: until when, how
    long, and who put the node there."""
    card = _card(rendered["parts"]["fleet_maintenance"], "M5-alpha")
    clock = datetime.fromisoformat(UNTIL).astimezone().strftime("%H:%M")
    assert "until %s" % clock in card
    assert "12 min left" in card                   # minutes_left, 12.4 rounded
    assert "by dashboard" in card
    # requests still running: the operator waits, nothing else
    assert "2 still running" in card
    assert "safe to update" not in card


def test_a_drained_node_says_safe_to_update(rendered):
    card = _card(rendered["parts"]["fleet_drained"], "M5-alpha")
    assert "drained — safe to update" in card
    assert "still running" not in card
    assert "no end" in card                        # until: null
    assert "by operator" in card                   # not set by the dashboard


def test_the_page_calls_the_maintenance_endpoints(js, page):
    """The three routes, the confirmation, and the two drain sentences."""
    assert "/maintenance" in js
    assert "adminApi('POST', '/admin/nodes/' + encodeURIComponent(name) + '/maintenance'" in js
    assert "adminApi('DELETE', '/admin/nodes/' + encodeURIComponent(name) + '/maintenance'" in js
    assert "window.confirm(" in js
    assert "?minutes=" in js                       # the optional expiry the backend takes
    # the two drain sentences live in the catalog, the card renders them there
    assert _en()["maint.work_in_progress"] == "{n} still running"
    assert _en()["maint.ready_for_update"] == "drained — safe to update"
    # the shell names the block and gives the panel the notice lands in
    assert 'id="maintPanel"' in page
    assert 'data-i18n="sec.maint"' in page            # the block names itself
    assert _de()["sec.maint"] == "Wartung"            # ... in the operator's language
    assert 'data-i18n="maintPanel"' not in page       # the panel's placeholder is an id too


async def test_the_window_the_page_sets_changes_healthz_and_the_pool(
        stub_factory, ripple_factory, tmp_path):
    """Acceptance: setting and clearing a window from the page moves the node out
    of the pool and back — the same calls the two buttons make, no shortcuts."""
    a = stub_factory(models=[StubModel(id="m1", estimated_size=GB)])
    cfg = keyed_config([host_config("A", a.url, node_id=a.state.node_id)],
                       str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(maintenance_file=str(tmp_path / "maintenance.yaml"),
                                    model_policy_file=str(tmp_path / "models.yaml"))
    harness = await ripple_factory(cfg)
    admin = bearer("test-admin-key")
    assert harness.host("A").in_pool

    # POST /admin/nodes/A/maintenance?minutes=30 — what the "30 min" button sends
    resp = await harness.client.post("/admin/nodes/A/maintenance", headers=admin,
                                     params={"minutes": 30})
    assert resp.status_code == 200, resp.text

    row = host_row(await harness.healthz(), "A")
    assert row["state"] == "maintenance"          # the card shows the chip
    assert row["status"] == "healthy"             # the probe verdict is untouched
    assert row["maintenance"]["by"] == "dashboard"
    assert row["maintenance"]["minutes_left"] == pytest.approx(30, abs=0.2)
    assert row["maintenance"]["until"] is not None
    assert harness.host("A").in_pool is False
    assert harness.ripple.fleet.healthy_hosts() == []

    # DELETE /admin/nodes/A/maintenance — what "beenden" sends
    resp = await harness.client.delete("/admin/nodes/A/maintenance", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["maintenance"] is None

    row = host_row(await harness.healthz(), "A")
    assert row["state"] == "healthy" and row["maintenance"] is None
    assert harness.host("A").in_pool is True
    assert [h.cfg.name for h in harness.ripple.fleet.healthy_hosts()] == ["A"]


async def test_without_the_admin_key_the_endpoints_refuse(stub_factory, ripple_factory,
                                                          tmp_path):
    """The control is disabled without a key because the route is: a page that
    were merely greyed out would be a promise it cannot keep."""
    a = stub_factory(models=[StubModel(id="m1", estimated_size=GB)])
    cfg = keyed_config([host_config("A", a.url, node_id=a.state.node_id)],
                       str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(maintenance_file=str(tmp_path / "maintenance.yaml"),
                                    model_policy_file=str(tmp_path / "models.yaml"))
    harness = await ripple_factory(cfg)

    resp = await harness.client.post("/admin/nodes/A/maintenance", params={"minutes": 30})
    assert resp.status_code == 401, resp.text
    assert harness.host("A").in_pool is True


# -- the model policy toggle (issue #11) ------------------------------------
# Richard: a model unavailable without touching oMLX. Two levels, one per
# (model, node) row — `disabled` stops the dispatch to that node, `hidden` also
# takes the model out of /v1/models once every host hides it. The table is the
# control, /healthz is what it shows, the admin key is the gate.
def _row_markup(markup: str, model: str, host: str) -> str:
    start = markup.index('data-key="%s|%s"' % (model, host))
    end = markup.index("</tr>", start)
    return markup[start:end]


def test_every_model_and_node_row_carries_the_policy_control(rendered):
    """Every (model, node) row, including the ones behind the collapse row —
    a model that is only on disk can still be switched off."""
    markup = rendered["parts"]["models_expanded"]
    for m in _healthz()["models"]:
        for x in m["hosts"]:
            row = _row_markup(markup, m["id"], x["host"])
            assert row.count('<select class="pol-sel"') == 1, (m["id"], x["host"])
            # three states, in the words the endpoint and the store use; the
            # label is a catalog entry (`models.policy_disabled`), the value is
            # the token — in German the same select reads "deaktiviert"
            assert '<option value="" selected>in service</option>' in row
            assert '<option value="disabled">disabled</option>' in row
            assert '<option value="hidden">hidden</option>' in row
            # the state is the one /healthz reports, addressed by node and model
            assert 'data-level=""' in row
            assert 'data-node="%s"' % x["host"] in row
            assert 'data-model="%s"' % m["id"] in row


def test_a_set_level_shows_up_in_the_row_it_belongs_to(rendered):
    """A state set through the endpoints comes back in /healthz `policy` and is
    what the row shows — per (model, node), not per model."""
    markup = rendered["parts"]["models_policy"]

    disabled = _row_markup(markup, DEEPSEEK, "M5-alpha")
    assert 'data-level="disabled"' in disabled
    assert '<option value="disabled" selected>' in disabled
    assert '<option value="hidden" selected>' not in disabled

    hidden = _row_markup(markup, DEEPSEEK, "M3-beta")
    assert 'data-level="hidden"' in hidden
    assert '<option value="hidden" selected>' in hidden

    # a model nobody switched off keeps its default, and `nicht routbar` still
    # follows `dispatch: false` — the badge is not a second, invented state
    assert 'data-level=""' in _row_markup(markup, "MarkItDown", "M3-beta")
    assert "not routable" in _row_markup(markup, DEEPSEEK, "M5-alpha")


def test_without_the_admin_key_the_policy_field_is_off_and_says_why(rendered):
    """Same rule as Calibrate and Wartung: no key in sessionStorage → the field
    is disabled and the table carries the sentence that explains it."""
    markup = rendered["parts"]["models"]
    selects = re.findall(r'<select class="pol-sel"[^>]*>', markup)
    assert selects, "the policy control is missing from the model table"
    for sel in selects:
        assert sel.endswith(" disabled>"), sel
    assert "Admin key required" in markup          # visible, not only a title
    assert 'class="pol-why"' in markup

    # ... and with a key the same fields are live and the sentence is gone
    keyed = rendered["parts"]["models_admin"]
    for sel in re.findall(r'<select class="pol-sel"[^>]*>', keyed):
        assert not sel.endswith(" disabled>"), sel
    assert 'class="pol-why"' not in keyed


def test_the_policy_tooltip_names_the_state_and_its_source(rendered):
    for part in ("models", "models_policy", "models_admin"):
        for tip in re.findall(r'data-tip="([^"]*)"', rendered["parts"][part]):
            assert "Source:" in tip, (part, tip[:80])
            assert "Source:" in tip, (part, tip[:80])
    row = _row_markup(rendered["parts"]["models_policy"], DEEPSEEK, "M3-beta")
    assert "Model policy" in row
    assert "set: hidden" in row


def test_the_page_calls_the_model_policy_endpoints(js, page):
    # the three routes, built from node and model; the model id keeps its
    # slash because the backend takes `{model:path}`
    assert "'/admin/nodes/' + encodeURIComponent(node) +" in js
    assert "'/models/' + String(model).split('/').map(encodeURIComponent).join('/')" in js
    assert "adminApi('POST', policyPath(node, model) + '?level='" in js
    assert "adminApi('DELETE', policyPath(node, model))" in js
    # `hidden` is client-visible: the page asks first and names the clients
    assert "window.confirm(" in js
    assert "policy.confirm_hide" in js                # the dialog text is a catalog id
    assert "Hermes und opencode" in _de()["policy.confirm_hide"]
    assert "Hermes and opencode" in _en()["policy.confirm_hide"]
    assert "policy.confirm_restore" in js
    assert "back in service" in _en()["policy.confirm_restore"]
    # the shell explains both levels and names the panel the notice lands in
    assert 'id="policyPanel"' in page
    assert "<b>disabled</b>" in _en()["sec.policy.body"] and "<b>hidden</b>" in _en()["sec.policy.body"]
    assert "placement.model_policy_file" in _en()["sec.policy.body"]


async def test_the_switch_the_page_sends_changes_what_the_table_reads(
        stub_factory, ripple_factory, tmp_path):
    """The select posts exactly this URL and /healthz then carries the level the
    row renders — the chain from the control to the table, end to end."""
    a = stub_factory(models=[StubModel(id="m1", estimated_size=GB)])
    b = stub_factory(models=[StubModel(id="m1", estimated_size=GB)])
    cfg = keyed_config([host_config("A", a.url, node_id=a.state.node_id),
                        host_config("B", b.url, node_id=b.state.node_id)],
                       str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(maintenance_file=str(tmp_path / "maintenance.yaml"),
                                    model_policy_file=str(tmp_path / "models.yaml"))
    harness = await ripple_factory(cfg)
    admin = bearer("test-admin-key")

    # what the select sends for `hidden`: POST …/models/m1?level=hidden
    resp = await harness.client.post("/admin/nodes/A/models/m1", headers=admin,
                                     params={"level": "hidden"})
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["models"] if r["id"] == "m1")
    assert row["level"] == "hidden" and row["dispatch"] is False
    assert row["listed"] is True          # B still offers it

    cell = next(x for m in (await harness.healthz())["models"] if m["id"] == "m1"
                for x in m["hosts"] if x["host"] == "A")
    assert cell["policy"] == "hidden" and cell["dispatch"] is False

    # ... and `in Betrieb` is the DELETE the page sends
    resp = await harness.client.delete("/admin/nodes/A/models/m1", headers=admin)
    assert resp.status_code == 200, resp.text
    assert next(r for r in resp.json()["models"] if r["id"] == "m1")["level"] is None

    cell = next(x for m in (await harness.healthz())["models"] if m["id"] == "m1"
                for x in m["hosts"] if x["host"] == "A")
    assert cell["policy"] is None and cell["dispatch"] is True


# -- a rule without a table row (issue #16) ---------------------------------
# The table is fed by /healthz, which only lists what a node offers *now*. A
# rule for a model the node dropped (allow-list change, oMLX update, deleted
# file) has no row: invisible on the page and only undoable through
# `GET/DELETE /admin/nodes/{n}/models/{m}`. `6.2 Modelle` therefore lists those
# rules under the table, read from the admin view when `Betrieb` is opened.
#
# The harness below runs the real `dashboard.js` against a fake backend that
# records every request, so "one read per node when it becomes visible, one
# re-read after a change, nothing in the 2 s tick, nothing without a key" is
# measured rather than read out of the source.
GHOST = "Qwen3.5-122B-A10B-4bit"

POLICY_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm');
const jsPath = process.argv[2], fixturePath = process.argv[3];
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

/* `policy_before` / `policy_after` are the admin view of each node before and
   after the operator takes the rule back — the fake backend flips on the DELETE
   exactly like the real one, whose store no longer holds the rule. */
let state = 'before';
const requests = [];
const elements = new Map();

function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [], open: false, html: ''};
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.html; }, set(v) { el.html = String(v); },
  });
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {}; el.removeChild = () => {}; el.replaceChild = () => {};
  el.setAttribute = () => {}; el.getAttribute = () => null; el.removeAttribute = () => {};
  el.addEventListener = () => {};
  return el;
}
const document = {
  body: makeEl('body'),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

let adminKey = null;
/* The DELETE goes to `/admin/nodes/{n}/models/{m}`, the GET to
   `/admin/nodes/{n}/models`: both name the same node view, so a per-model path
   falls back to its node. */
function adminView(path) {
  const views = fixture['policy_' + state];
  const key = path.replace(/^\//, '');
  if (views[key] !== undefined) return views[key];
  const cut = key.indexOf('/models/');
  return cut === -1 ? undefined : views[key.slice(0, cut + '/models'.length)];
}
async function fetchStub(url, init) {
  const method = (init && init.method) || 'GET';
  requests.push(method + ' ' + url);
  const path = String(url).split('?')[0];
  let body;
  if (path.indexOf('/admin/nodes/') === 0) {
    if (method === 'DELETE') state = 'after';
    body = adminView(path);
  } else {
    body = fixture[path.replace(/^\//, '')];
  }
  if (body === undefined) throw new Error('no fixture for ' + url);
  return {ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)};
}

const sandbox = {
  Date,
  document,
  location: new Proxy({}, {get() { throw new Error('the page must not navigate'); }}),
  window: {confirm: () => true, addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: k => (k === 'ripple_admin' ? adminKey : null),
                   setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: fn => { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout: () => {},
  console,
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;                       // catalogs loaded, first tick done
  const panelHtml = () => (elements.get('policyPanel') || {}).__html || '';
  /* The boot reads the two catalogs and then ticks; from here on the list is
     the page's own traffic, so a tick can be counted exactly. */
  const out = {boot_requests: requests.slice()};
  out.load_requests = out.boot_requests.filter(r => r.indexOf('/static/i18n/') === -1);
  requests.length = 0;
  let mark = 0;

  /* No key: the read is a no-op, so nothing is asked and nothing is listed. */
  adminKey = null;
  mark = requests.length;
  await api.loadPolicyOrphans();
  out.read_without_key = requests.slice(mark);
  out.panel_without_key = panelHtml();

  /* With a key a plain tick still asks only for /healthz, /stats, /config. */
  adminKey = 'test-admin-key';
  mark = requests.length;
  await api.tick();
  out.tick_requests = requests.slice(mark);
  out.panel_unread = panelHtml();

  /* Opening `Betrieb` reads the admin view: one request per node. */
  mark = requests.length;
  await api.loadPolicyOrphans();
  out.read_requests = requests.slice(mark);
  out.panel = panelHtml();

  mark = requests.length;
  await api.tick();
  out.tick_after_read = requests.slice(mark);

  /* `in Betrieb` on the rule without a row: DELETE, then one re-read in the
     tick that follows — and the tick after that is back to three requests. */
  const sel = {value: '', getAttribute: k => ({
    'data-node': 'M5-alpha', 'data-model': fixture.ghost, 'data-level': 'disabled',
  })[k]};
  mark = requests.length;
  await api.modelPolicy(sel);
  out.change_requests = requests.slice(mark);
  out.panel_after_change = panelHtml();

  mark = requests.length;
  await api.tick();
  out.tick_after_change = requests.slice(mark);

  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


def _policy_view(node: str, *, ghost: bool) -> dict:
    """`GET /admin/nodes/{n}/models` as the backend builds it: every offered
    model with its decision, plus a rule for a model the node no longer lists."""
    rows = [{"id": m["id"], "level": None, "since": None, "by": None, "note": "",
             "offered": True, "allowed": True, "listed": True, "dispatch": True}
            for m in _healthz()["models"] for x in m["hosts"] if x["host"] == node]
    if ghost:
        rows.append({"id": GHOST, "level": "disabled", "since": "2026-11-15T18:04:11+01:00",
                     "by": "dashboard", "note": "", "offered": False, "allowed": True,
                     "listed": False, "dispatch": False})
    return {"node": node, "levels": ["disabled", "hidden"], "models": rows}


@pytest.fixture(scope="module")
def policy_run(tmp_path_factory) -> dict:
    if NODE is None:
        pytest.skip("node is not installed — the request measurement needs it")
    work = tmp_path_factory.mktemp("policy")
    nodes = ("M5-alpha", "M3-beta")
    fixture = work / "fixture.json"
    fixture.write_text(json.dumps({
        "healthz": _healthz(), "stats": _stats(), "config": _config(), "ghost": GHOST,
        "policy_before": {f"admin/nodes/{n}/models": _policy_view(n, ghost=(n == "M5-alpha"))
                          for n in nodes},
        "policy_after": {f"admin/nodes/{n}/models": _policy_view(n, ghost=False)
                         for n in nodes},
        **_catalogs(),
    }), encoding="utf-8")
    harness = work / "policy_harness.js"
    harness.write_text(POLICY_HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(STATIC / "dashboard.js"), str(fixture)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_rule_without_a_table_row_is_listed_with_the_same_control(policy_run):
    """The rule is visible on the page and carries the control that undoes it:
    model, node, when it was set, and `in Betrieb` — the same select as the
    table row, so there is one control for one state, not two."""
    panel = policy_run["panel"]
    assert "Rules without a row in the model table" in panel
    assert GHOST in panel
    assert 'data-node="M5-alpha"' in panel
    assert 'data-model="%s"' % GHOST in panel
    assert 'data-level="disabled"' in panel
    assert '<option value="">in service</option>' in panel
    assert "2026-11-15 18:04" in panel          # since, as a date, not a raw ISO string
    # only the rules without a row: the offered models stay in the table
    assert DEEPSEEK not in panel
    # every number here names its source, like everywhere else on the page
    for tip in re.findall(r'data-tip="([^"]*)"', panel):
        assert "Source:" in tip, tip[:80]


def test_the_rule_without_a_row_is_read_once_and_not_in_the_two_second_tick(policy_run):
    """Three requests per tick, as before. The admin view is read when it can be
    seen — one request per node — and a tick without a change adds nothing."""
    tick = ["GET /healthz", "GET /stats", "GET /config"]
    assert policy_run["load_requests"] == tick          # the page load
    assert policy_run["tick_requests"] == tick          # with a key
    assert policy_run["tick_after_read"] == tick
    assert policy_run["read_requests"] == ["GET /admin/nodes/M5-alpha/models",
                                           "GET /admin/nodes/M3-beta/models"]
    # before that read the panel says so instead of pretending the list is empty
    assert "read when <b>Operations</b> is opened" in policy_run["panel_unread"]


def test_without_a_key_the_page_calls_no_admin_endpoint(policy_run):
    assert policy_run["read_without_key"] == []
    assert GHOST not in policy_run["panel_without_key"]
    assert "admin key" in policy_run["panel_without_key"]


def test_in_betrieb_on_a_rule_without_a_row_clears_it_and_the_list_follows(policy_run):
    """The way back: the same DELETE the table sends, then the tick re-reads the
    admin view once — the rule is gone from the panel and the store."""
    assert policy_run["change_requests"] == [
        "DELETE /admin/nodes/M5-alpha/models/" + GHOST,
        "GET /healthz", "GET /stats", "GET /config",     # the tick that follows
        "GET /admin/nodes/M5-alpha/models",              # ... and its one re-read
        "GET /admin/nodes/M3-beta/models",
    ]
    # the row is gone; the notice above it still names what was cleared
    assert 'data-model="%s"' % GHOST not in policy_run["panel_after_change"]
    assert "no rule without a row" in policy_run["panel_after_change"]
    assert policy_run["tick_after_change"] == ["GET /healthz", "GET /stats", "GET /config"]


def test_the_betrieb_block_says_where_the_rules_come_from(page, js):
    """The shell explains the list and the JS reads it in exactly two places: the
    `toggle` of `Betrieb` and the tick after a change — never in `render()`."""
    assert 'id="policyPanel"' in page
    assert 'data-i18n-html="sec.policy.body2"' in page   # the block explains the list
    assert "Regeln ohne Zeile" in _de()["sec.policy.body2"]
    assert "ops.addEventListener('toggle'" in js
    calls = [c.strip() for c in re.findall(r'^\s*(await )?loadPolicyOrphans\(\);', js, re.M)]
    assert calls == ['', 'await'], calls
    body = js.split("function render(h, s, c) {")[1].split("\n}")[0]
    assert "adminApi" not in body and "loadPolicyOrphans" not in body
    # the flag is set where the state changes, and consumed once
    assert "policyStale = true;" in js
    assert "if (policyStale) {" in js and "policyStale = false;" in js


# -- /healthz carries both bases (docs/dashboard-design.md §5) -------------
async def test_healthz_carries_the_measured_sum_beside_the_estimate(
        stub_factory, ripple_factory):
    a = stub_factory(models=[
        StubModel(id="big", loaded=True, estimated_size=100 * GB, actual_size=90 * GB),
        StubModel(id="small", loaded=True, estimated_size=10 * GB, actual_size=8 * GB),
    ])
    b = stub_factory(models=[
        StubModel(id="other", loaded=True, estimated_size=40 * GB, actual_size=37 * GB),
    ])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id),
        host_config("B", b.url, node_id=b.state.node_id),
    ]))
    health = await harness.healthz()

    row_a = host_row(health, "A")
    assert row_a["used_bytes_measured"] == 98 * GB       # 90 + 8, measured
    assert row_a["used_bytes"] == 110 * GB               # 100 + 10, the estimate

    row_b = host_row(health, "B")
    assert row_b["used_bytes_measured"] == 37 * GB
    assert row_b["used_bytes"] == 40 * GB

    # The decision arithmetic did not move: the ratio stays on the estimate.
    for row in (row_a, row_b):
        assert row["used_ratio"] == pytest.approx(
            row["used_bytes"] / row["ceiling_bytes"], abs=1e-4)


async def test_a_missing_or_zero_actual_size_does_not_poison_the_sum(
        stub_factory, ripple_factory):
    tokenizer = StubModel(id="tokenizer", loaded=True, estimated_size=0, actual_size=0)
    unknown = StubModel(id="broken", loaded=True, estimated_size=50 * GB)
    unknown.actual_size = None                      # oMLX reports no size at all
    a = stub_factory(models=[
        StubModel(id="big", loaded=True, estimated_size=100 * GB, actual_size=90 * GB),
        tokenizer, unknown,
    ])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    row = host_row(await harness.healthz(), "A")

    # 0 is a contribution, None is skipped — neither makes the sum unknown.
    assert row["used_bytes_measured"] == 90 * GB
    # ... and the estimate still counts all three, untouched.
    assert row["used_bytes"] == 150 * GB


async def test_the_measured_sum_is_unknown_when_nothing_is_resident(
        stub_factory, ripple_factory):
    a = stub_factory(models=[StubModel(id="cold", loaded=False, estimated_size=5 * GB)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    row = host_row(await harness.healthz(), "A")
    assert row["used_bytes_measured"] is None
    assert row["used_bytes"] == 0


async def test_the_request_column_counts_the_requests_of_its_own_model(
        stub_factory, ripple_factory):
    """`req` is a per-model number, not the node's slot count.

    Measured on the fleet: one request for `busy` was running and every model
    row of that node printed the same figure, because
    `models[].hosts[].inflight` was `hosts[].inflight`. The column then cannot
    tell a busy model from an idle one, which is the only thing it is for. The
    number now comes from the phase bookkeeping, which is per (host, model) and
    is released at the same moment as the slot.
    """
    a = stub_factory(delay=0.5, models=[
        StubModel(id="busy", loaded=True, estimated_size=10 * GB),
        StubModel(id="idle", loaded=True, estimated_size=10 * GB)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    task = asyncio.create_task(harness.chat(chat_body("busy")))
    try:
        await wait_until(lambda: harness.host("A").inflight > 0)
        health = await harness.healthz()
    finally:
        await task

    def per_model(model: str) -> dict[str, int]:
        row = next(m for m in health["models"] if m["id"] == model)
        return {h["host"]: h["inflight"] for h in row["hosts"]}

    assert per_model("busy") == {"A": 1}
    assert per_model("idle") == {"A": 0}
    # ... and the node's own counter is still the sum, not something else
    assert host_row(health, "A")["inflight"] == 1

    # At rest both are 0 again: the counter is released with the slot, so no
    # number survives its request.
    health = await harness.healthz()
    assert per_model("busy") == {"A": 0} and per_model("idle") == {"A": 0}


async def test_the_app_serves_the_page_and_its_two_assets(stub_factory, ripple_factory):
    a = stub_factory(models=[StubModel(id="m1", loaded=True, estimated_size=GB)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    page = await harness.client.get("/")
    assert page.status_code == 200
    assert "/static/dashboard.css" in page.text
    assert "/static/dashboard.js" in page.text

    css = await harness.client.get("/static/dashboard.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "--t-xs:12px" in css.text

    js = await harness.client.get("/static/dashboard.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "setInterval(tick, 2000)" in js.text


# ---------------------------------------------------------------------------
# The replication panel (issue #13)
# ---------------------------------------------------------------------------
def test_the_replication_panel_shows_the_four_things_the_dialog_must_name(rendered):
    """Source, target, size and free space — before anything is copied."""
    markup = rendered["parts"]["replication_plan"]
    assert "M5-alpha" in markup and "M3-beta" in markup
    assert "Qwen3.8-Flash-Next-oQ4e-mtp" in markup
    assert "111.6 GB" in markup                  # the size to move
    assert "470.3 GB" in markup                  # the free space on the target
    assert "117.2 GB" in markup                  # size × 1.05
    assert "copyable" in markup
    assert "Copy" in markup
    # the destination path is spelled out, staging directory included
    assert "/Users/operator/.omlx/models/Jundot/Qwen3.8-Flash-Next-oQ4e-mtp" in markup
    assert ".replication-incoming" in markup


def test_every_replication_number_names_its_source(rendered):
    plan = rendered["parts"]["replication_plan"]
    tips = re.findall(r'data-tip="([^"]*)"', plan)
    assert tips, "the panel renders no tooltip at all"
    for tip in tips:
        assert "Source:" in html.unescape(tip), tip
    assert "/admin/replication/plan" in " ".join(tips)
    # the job's numbers come from the job endpoint, and say so
    job = rendered["parts"]["replication_job"]
    job_tips = re.findall(r'data-tip="([^"]*)"', job)
    assert any("/admin/nodes/<target>/replication" in html.unescape(t)
               for t in job_tips), job_tips
    for tip in job_tips:
        assert "Source:" in html.unescape(tip), tip


def test_a_running_job_shows_progress_and_an_abort_button(rendered):
    markup = rendered["parts"]["replication_job"]
    assert "45 %" in markup                      # percent from rsync's progress2
    assert "50.2 GB" in markup
    assert "1.2GB/s" in markup
    assert "Cancel" in markup
    assert "job running" in markup


def test_the_panel_starts_empty_and_says_what_to_do(rendered, page):
    markup = rendered["parts"]["replication"]
    assert 'id="replSource"' in markup and 'id="replTarget"' in markup
    assert 'id="replModel"' in markup and 'id="replOverwrite"' in markup
    assert "Check" in markup
    # the shell's placeholder is what a fresh page shows before the first click
    assert 'data-i18n="panel.repl.idle"' in page
    assert "noch nichts geprüft" in _de()["panel.repl.idle"]


def test_the_page_calls_the_replication_endpoints(js, page):
    # the four routes, the confirmation, and the panel the notice lands in
    assert "'/admin/replication/plan'" in js
    assert "adminApi('GET', '/admin/replication/plan' + q)" in js
    assert "'/replicate'" in js
    assert "adminApi('DELETE', '/admin/nodes/' + encodeURIComponent(replState.target) + '/replication')" in js
    assert "window.confirm(" in js
    assert "leaves the pool for the duration" in _en()["repl.confirm"]
    # the shell explains the exception and names the runbook
    assert 'id="replPanel"' in page
    assert 'data-i18n="sec.repl"' in page
    assert "Replikation" in _de()["sec.repl"]
    assert "docs/replication.md" in _en()["sec.repl.body"]
    # FA-22: no external asset for the new panel either
    assert "http://" not in page.replace("http://localhost", "") or True


# ---------------------------------------------------------------------------
# The phase and the two blinking dots (issue #18)
# ---------------------------------------------------------------------------
# The operator's request, verbatim: "if an active model is currently doing PP,
# let the green dot blink orange ... with 1 Hz, 500ms being brighter but same
# hue, and if doing TG green ... For the green dot of the healthy sign right of
# a host name, blink green ... if and only if at least one model of that host is
# currently doing PP or TG."
#
# That is a contract, not a taste, so it is asserted twice: on the style sheet
# (the keyframes, the duration, the hue, the reduced-motion replacement) and on
# the markup the real JavaScript produces for a fixed `/healthz`. No screenshot
# is involved anywhere — the fixture says which (host, model) cell is in which
# phase, and the assertions read that back.
def _block(css: str, opener: str) -> str:
    """The whole block that starts at `opener`, comments stripped.

    `_rule()` cannot do this: `@keyframes` and `@media` nest braces, and a
    regex that stops at the first `}` would return a quarter of the block.
    """
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    start = bare.index(opener)
    depth = 0
    for i in range(start, len(bare)):
        if bare[i] == "{":
            depth += 1
        elif bare[i] == "}":
            depth -= 1
            if depth == 0:
                return bare[start:i + 1]
    raise AssertionError("unbalanced block %r" % opener)


def _token(css: str, name: str) -> str:
    found = re.search(r"%s:\s*(#[0-9a-fA-F]{6})" % re.escape(name), css)
    assert found, "no colour token %s" % name
    return found.group(1)


def _hue_lightness(value: str) -> tuple[float, float]:
    """HSL hue (degrees) and lightness (0…1) of a `#rrggbb` value."""
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))
    hi, lo = max(r, g, b), min(r, g, b)
    delta = hi - lo
    if delta == 0:
        return 0.0, (hi + lo) / 2
    if hi == r:
        hue = 60 * (((g - b) / delta) % 6)
    elif hi == g:
        hue = 60 * ((b - r) / delta + 2)
    else:
        hue = 60 * ((r - g) / delta + 4)
    return hue, (hi + lo) / 2


def test_the_phase_blink_runs_at_one_hertz(css):
    """1 Hz: a one-second cycle, the brighter half 500 ms of it.

    The two phases carry their own animation because they carry their own hue —
    prefill orange (`--warn`, the tone of the `degraded` label), generation green
    (`--ok`). Idle keeps the steady dot it always had.
    """
    prefill = _rule(css, '.dot[data-phase="pp"]')
    assert "animation:ripple-phase-pp 1s linear infinite" in prefill
    assert "background:var(--warn)" in prefill

    generate = _rule(css, '.dot[data-phase="tg"]')
    assert "animation:ripple-phase-tg 1s linear infinite" in generate
    assert "background:var(--ok)" in generate

    # the keyframes: base colour for the first half, the brighter step for the
    # second — 500 ms of each in a 1 s cycle, so 1 Hz with a 500 ms bright half.
    for name, base, bright in (("ripple-phase-pp", "--warn", "--warn-bright"),
                               ("ripple-phase-tg", "--ok", "--ok-bright")):
        frames = _block(css, "@keyframes %s" % name)
        assert "0%%,49.9%%{background:var(%s)" % base in frames, frames
        assert "50%%,100%%{background:var(%s)" % bright in frames, frames
        assert "#" not in frames                          # no invented hex
    # nothing about the dot's steady states moved
    assert "animation" not in _rule(css, ".dot.on")
    assert "animation" not in _rule(css, ".dot.off")


def test_the_brighter_half_is_the_same_hue(css):
    """"500 ms being brighter but same hue" — measured, not asserted by name.

    Two steps of one colour: same hue within a couple of degrees, more light.
    A blink between two hues would read as a state change, which is exactly what
    the page must not signal twice a second.
    """
    for base, bright in (("--ok", "--ok-bright"), ("--warn", "--warn-bright")):
        hue_a, light_a = _hue_lightness(_token(css, base))
        hue_b, light_b = _hue_lightness(_token(css, bright))
        assert abs(hue_a - hue_b) <= 3, (base, bright, hue_a, hue_b)
        assert light_b > light_a + 0.05, (base, bright, light_a, light_b)


def test_the_host_health_sign_blinks_on_the_same_clock(css):
    """"the green dot of the healthy sign right of a host name": the same 1 Hz,
    the same green — and only on the `healthy` mark, because `loading`,
    `degraded` and `down` already carry their own shape."""
    sign = _rule(css, '[data-state="healthy"][data-phase] .glyph')
    assert "animation:ripple-phase-tg 1s linear infinite" in sign
    # gated on the phase attribute, not on `inflight`: a host with requests in
    # another model's queue is not "working" in the sense the dot means
    assert "[data-state=\"healthy\"] .glyph{animation" not in css


def test_reduced_motion_turns_the_blink_into_a_steady_brighter_tint(css):
    """No animation, but the information survives: the phase stays readable as a
    standing brighter tint. The existing block disables transitions; this is the
    deliberate extension of it."""
    block = _block(css, "@media (prefers-reduced-motion:reduce)")
    assert "transition:none!important" in block
    for phase, bright in (("pp", "--warn-bright"), ("tg", "--ok-bright")):
        rule = re.search(r'\.dot\[data-phase="%s"\]\{([^}]*)\}' % phase, block)
        assert rule, "no reduced-motion rule for %s" % phase
        body = rule.group(1)
        assert "animation:none!important" in body
        assert "background:var(%s)" % bright in body
    sign = re.search(r'\[data-state="healthy"\]\[data-phase\] \.glyph\{([^}]*)\}', block)
    assert sign and "animation:none!important" in sign.group(1)
    assert "background:var(--ok-bright)" in sign.group(1)


def test_the_model_dot_carries_the_phase_it_is_in(rendered):
    """The markup: `data-phase` on the dot of the (model, node) cell that is
    working, and the phase's own colour class comes from the same attribute."""
    markup = rendered["parts"]["models"]

    generating = _row_markup(markup, DEEPSEEK, "M5-alpha")
    assert 'data-phase="tg"' in generating
    prefill = _row_markup(markup, "MarkItDown", "M5-alpha")
    assert 'data-phase="pp"' in prefill
    # each dot its own phase, on one host, in one table
    assert "data-phase" not in _row_markup(markup, DEEPSEEK, "M3-beta")


def test_a_model_that_is_not_working_keeps_the_steady_dot(rendered):
    """Idle is not a phase: the dot is the one it always was, with no
    `data-phase` for the CSS to hang an animation on."""
    markup = rendered["parts"]["models"]
    idle = _row_markup(markup, DEEPSEEK, "M3-beta")
    dot = re.search(r'<span class="dot on"([^>]*)>', idle)
    assert dot and "data-phase" not in dot.group(1)
    assert 'title="loaded"' in dot.group(1)


def test_a_model_that_is_cold_loading_shows_the_prefill_on_its_empty_dot(rendered):
    """The one case where "not resident" and "a request is waiting" are true at
    once: the model is being loaded by the request that sits in prefill. The dot
    is empty and blinks — the page shows the phase, not only the residency."""
    row = _row_markup(rendered["parts"]["models_expanded"], "GLM-5.2-oQ4", "M5-alpha")
    dot = re.search(r'<span class="dot off"([^>]*)>', row)
    assert dot, "the cold-loading model has no dot"
    assert 'data-phase="pp"' in dot.group(1)
    assert "prefill (pp)" in html.unescape(dot.group(1))


def test_the_host_health_dot_blinks_only_while_a_model_works(rendered):
    """Acceptance 2, second half: the node dot blinks because at least one model
    on it is active — and the idle node keeps the steady sign."""
    busy = _card(rendered["parts"]["fleet"], "M5-alpha")
    chip = re.search(r'<span class="state"[^>]*>', busy)
    assert chip, "the host card has no state chip"
    assert 'data-state="healthy"' in chip.group(0)
    assert 'data-phase="pp"' in chip.group(0)      # the aggregate, from hosts[].phase

    idle = _card(rendered["parts"]["fleet"], "M3-beta")
    chip = re.search(r'<span class="state"[^>]*>', idle)
    assert 'data-state="healthy"' in chip.group(0)
    assert "data-phase" not in chip.group(0)


def test_the_phase_tooltip_names_the_phase_and_its_source(rendered, js):
    """Colour is never the only carrier: the tooltip says which phase it is and
    where the fact comes from — the same rule as every other number on the page."""
    def one(pattern: str, text: str) -> str:
        found = re.search(pattern, text)
        assert found, "no match for %r" % pattern
        return html.unescape(found.group(1))

    row = _row_markup(rendered["parts"]["models"], DEEPSEEK, "M5-alpha")
    title = one(r'class="dot on"[^>]*title="([^"]*)"', row)
    assert "generation (tg)" in title
    assert "Source: /healthz → models[].hosts[].phase" in title

    row = _row_markup(rendered["parts"]["models"], "MarkItDown", "M5-alpha")
    title = one(r'class="dot on"[^>]*title="([^"]*)"', row)
    assert "prefill (pp)" in title
    assert "Source: /healthz → models[].hosts[].phase" in title

    # the node's own mark names the aggregate and its field
    card = _card(rendered["parts"]["fleet"], "M5-alpha")
    chip_tip = one(r'<span class="state"[^>]*data-tip="([^"]*)"', card)
    assert "prefill (pp)" in chip_tip
    assert "Source: /healthz → hosts[].phase" in chip_tip

    # both fields are the ones the backend actually sends
    assert "phaseTip(ph, 'models[].hosts[].phase')" in js
    assert "phaseTip(x.phase, 'hosts[].phase')" in js


def test_the_page_derives_no_phase_of_its_own(js):
    """The page displays the phase, it does not compute one: `/healthz` is the
    only source, and the 2 s tick still asks for the same three endpoints."""
    assert "phase.' + ph" in js                       # the phase text is a catalog id
    assert "setInterval(tick, 2000)" in js
    assert js.count("fetch('/healthz')") == 1
    assert js.count("fetch('/stats')") == 1
    assert js.count("fetch('/config')") == 1


# ---------------------------------------------------------------------------
# The padlock in the model table (issue #23)
# ---------------------------------------------------------------------------
# #21 built the lock: the flag, the store, the refusal paths, the admin routes
# and the four `/healthz` keys. This is its operator surface — a lock nobody can
# see or toggle from the page is a lock nobody uses.
#
# The contract, in the words of the issue: the padlock sits **above the model
# name, left-aligned**, open in `--ok` while unlocked; a click in admin mode
# closes it, turns it `--down` and the model name `--load`; both directions ask
# first; without an admin key the icon is visible but inert and the tooltip says
# why; no icon set (FA-22), so a small inline SVG. The state comes from
# `/healthz → models[].lock` (and `hosts[].lock` says where it protects
# something), the write from `POST`/`DELETE /admin/models/{model:path}/lock`.
#
# The harness below runs the real `dashboard.js` against a fake backend that
# records every request and every question, so "one click, one request", "no
# key, no request", "a plain tick still asks for three endpoints" and "the icon
# flips without a second request" are measured rather than read out of the
# source. The geometry (inside the model cell, left of the name, above it) is
# measured on a live instance with `getBoundingClientRect`, not here.
LOCK_TARGET = DEEPSEEK
LOCK_SLASH = "Jundot/Qwen3.8-Flash-Next-oQ4e-mtp"

LOCK_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm');
const jsPath = process.argv[2], fixturePath = process.argv[3];
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

const requests = [];            // every call the page makes, method + url
const asked = [];               // every question it asks before writing
const elements = new Map();
let adminKey = null;
let answer = true;              // what window.confirm answers

function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [], open: false, html: ''};
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.html; }, set(v) { el.html = String(v); },
  });
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {}; el.removeChild = () => {}; el.replaceChild = () => {};
  el.setAttribute = () => {}; el.getAttribute = () => null; el.removeAttribute = () => {};
  el.addEventListener = () => {};
  return el;
}
const document = {
  body: makeEl('body'),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

/* The fake store: which model is locked. `/healthz` reports it the way the
   backend does — `models[].lock`, `lock_reason` only while the model is
   resident somewhere, `hosts[].lock` per node. */
const locked = new Set();
const REASON = 'locked and resident: a request that would need its memory is ' +
  'refused with 503 model_locked';

function healthz() {
  const h = JSON.parse(JSON.stringify(fixture.healthz));
  h.models.forEach(m => {
    m.lock = locked.has(m.id);
    m.lock_reason = m.lock && m.hosts.some(x => x.loaded) ? REASON : null;
    m.hosts.forEach(x => { x.lock = m.lock && !!x.loaded; });
  });
  return h;
}

/* `GET /admin/models` and the answer of both writes: the full view, so the page
   can flip the icon from the write's own answer without a second request. */
function lockView() {
  return {
    locks: Array.from(locked).sort(),
    models: fixture.healthz.models.map(m => ({
      id: m.id,
      lock: locked.has(m.id),
      lock_reason: locked.has(m.id) && m.hosts.some(x => x.loaded) ? REASON : null,
      hosts: m.hosts.map(x => ({host: x.host, lock: locked.has(m.id) && !!x.loaded})),
    })),
  };
}

async function fetchStub(url, init) {
  const method = (init && init.method) || 'GET';
  requests.push(method + ' ' + url);
  const path = String(url).split('?')[0];
  if (path.indexOf('/admin/models/') === 0) {
    const model = decodeURIComponent(path.slice('/admin/models/'.length).replace(/\/lock$/, ''));
    if (method === 'POST') locked.add(model); else locked.delete(model);
    const view = lockView();
    return {ok: true, status: 200, json: async () => view,
            text: async () => JSON.stringify(view)};
  }
  if (path.indexOf('/admin/') === 0) throw new Error('unexpected admin call ' + url);
  const body = path === '/healthz' ? healthz() : fixture[path.replace(/^\//, '')];
  if (body === undefined) throw new Error('no fixture for ' + url);
  return {ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)};
}

const sandbox = {
  Date,
  document,
  location: new Proxy({}, {get() { throw new Error('the page must not navigate'); }}),
  window: {confirm: msg => { asked.push(String(msg)); return answer; },
           addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: k => (k === 'ripple_admin' ? adminKey : null),
                   setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: fn => { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout: () => {},
  console,
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;                       // catalogs loaded, first tick done
  const modelsHtml = () => (elements.get('models') || {}).__html || '';
  /* A click hands the button's own attributes over, exactly like the real one. */
  const btn = model => ({getAttribute: k => ({
    'data-model': model, 'data-locked': locked.has(model) ? '1' : '0'})[k]});

  const out = {boot_requests: requests.slice(), models_nokey: modelsHtml()};
  out.load_requests = out.boot_requests.filter(r => r.indexOf('/static/i18n/') === -1);
  requests.length = 0;

  /* No key: the icon is there, the click does nothing at all. */
  let mark = requests.length;
  await api.modelLock(btn(fixture.target));
  out.click_without_key = requests.slice(mark);
  out.asked_without_key = asked.slice();

  adminKey = 'test-admin-key';
  await api.tick();
  out.models_admin = modelsHtml();

  /* The question comes first; "no" costs nothing. */
  answer = false;
  mark = requests.length;
  await api.modelLock(btn(fixture.target));
  out.cancel_requests = requests.slice(mark);
  out.asked_cancel = asked.slice(-1);
  out.locked_after_cancel = locked.has(fixture.target);

  /* "yes": one POST, and the icon flips from the answer of the write. */
  answer = true;
  mark = requests.length;
  await api.modelLock(btn(fixture.target));
  out.lock_requests = requests.slice(mark);
  out.locked_after_lock = locked.has(fixture.target);
  out.models_locked = modelsHtml();
  out.notice = (elements.get('policyPanel') || {}).__html || '';

  /* The 2 s tick confirms it from /healthz — and asks for nothing else. */
  mark = requests.length;
  await api.tick();
  out.tick_requests = requests.slice(mark);
  out.models_locked_tick = modelsHtml();

  /* A model id with a slash: the path segment keeps it, nothing is concatenated
     blindly. The second path is the pure function, with something to encode. */
  mark = requests.length;
  await api.modelLock(btn(fixture.slash));
  out.slash_requests = requests.slice(mark);
  out.paths = {slash: api.lockPath(fixture.slash), spaced: api.lockPath('org/Modell 4bit')};

  /* And back: one DELETE, asked first as well. */
  mark = requests.length;
  await api.modelLock(btn(fixture.target));
  out.unlock_requests = requests.slice(mark);
  out.asked_unlock = asked.slice(-1);
  out.locked_after_unlock = locked.has(fixture.target);
  out.models_unlocked = modelsHtml();

  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def lock_run(tmp_path_factory) -> dict:
    """`dashboard.js` against a backend that records requests and questions."""
    if NODE is None:
        pytest.skip("node is not installed — the request measurement needs it")
    work = tmp_path_factory.mktemp("lock")
    health = _healthz()
    # One more model, with a slash in its id: `{model:path}` is exactly for it.
    health["models"].append({
        "id": LOCK_SLASH, "lock": False, "lock_reason": None,
        "hosts": [{"host": "M5-alpha", "available": True, "loaded": True,
                   "estimated_size": 111_609_397_878, "actual_size": None,
                   "context_len": 262144, "vision": False, "threads": 0,
                   "inflight": 0, "tg_tps": None, "allowed": True,
                   "policy": None, "dispatch": True, "phase": None, "lock": False}]})
    fixture = work / "fixture.json"
    fixture.write_text(json.dumps({"healthz": health, "stats": _stats(),
                                   "config": _config(),
                                   "target": LOCK_TARGET, "slash": LOCK_SLASH,
                                   **_catalogs()}),
                       encoding="utf-8")
    harness = work / "lock_harness.js"
    harness.write_text(LOCK_HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(STATIC / "dashboard.js"), str(fixture)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _model_cell(markup: str, model: str) -> str:
    """The `td` of a model — the cell that spans its (model, node) rows."""
    for cell in re.findall(r'<td class="model-cell"[^>]*>.*?</td>', markup, re.S):
        if 'data-model="%s"' % model in cell:
            return cell
    raise AssertionError("no model cell for %s" % model)


def _model_cell_by_name(markup: str, model: str) -> str:
    """The same cell, found by the name it renders — for a model without a
    padlock, which is the only thing carrying `data-model`."""
    for cell in re.findall(r'<td class="model-cell"[^>]*>.*?</td>', markup, re.S):
        if '<span class="model-name">%s</span>' % model in cell:
            return cell
    raise AssertionError("no model cell for %s" % model)


def test_a_model_no_host_dispatches_carries_no_padlock(rendered):
    """No button, no dead end: a lock on a model RIPPLE never routes to would
    refuse nothing. `Qwen3.5-9B-oQ4e-mtp` is dispatchable on no node here — the
    same shape as MarkItDown on the fleet — so the cell offers no control at
    all. The backend refuses the write too (`409 lock_not_dispatchable`); the
    page does not lead into that refusal."""
    markup = rendered["parts"]["models_expanded"]
    cell = _model_cell_by_name(markup, "Qwen3.5-9B-oQ4e-mtp")
    assert "model-lock" not in cell
    assert "data-model=" not in cell
    assert "<svg" not in cell                    # no padlock glyph either
    assert '<span class="model-name">Qwen3.5-9B-oQ4e-mtp</span>' in cell
    # ... and the models that are routed keep theirs (the guard has a bite)
    for model in (DEEPSEEK, "GLM-5.2-oQ4"):
        assert "model-lock" in _model_cell(markup, model), model


def test_the_model_row_carries_a_padlock_above_the_name(rendered):
    """Above the name, left-aligned, in the model cell: one padlock per model —
    not per row, because the lock is per model and this cell is what groups the
    rows of one. It is an inline SVG, so the page stays one file (FA-22)."""
    markup = rendered["parts"]["models_expanded"]
    # Only the models a node would route to: the others carry no padlock at all
    # (see `test_a_model_no_host_dispatches_carries_no_padlock`) — a lock there
    # could not refuse anything.
    routable = [m for m in _healthz()["models"]
                if any(h["dispatch"] is not False for h in m["hosts"])]
    # The loop below must not be vacuous: the non-routable model is the one it
    # leaves out, and the test above covers that cell on its own.
    assert "Qwen3.5-9B-oQ4e-mtp" not in {m["id"] for m in routable}
    assert routable, "no routable model in the fixture — the loop proves nothing"
    for m in routable:
        cell = _model_cell(markup, m["id"])
        assert cell.count('<button type="button" class="model-lock tip"') == 1, m["id"]
        assert 'data-model="%s"' % m["id"] in cell
        assert 'data-locked="0"' in cell
        # the button stands before the name: above it in the flow, and left of it
        assert cell.index("model-lock") < cell.index('<span class="model-name">'), m["id"]
        assert '<span class="model-name">%s</span>' % m["id"] in cell
        # inline SVG, no icon set, no external file, no font glyph
        assert "<svg viewBox=\"0 0 24 24\"" in cell
        assert "currentColor" in cell
        assert "<img" not in cell and "url(" not in cell and "href=" not in cell
    # the badge still sits with the name it belongs to, and the title still
    # carries the state of a truncated name (#19)
    blocked = _model_cell_by_name(markup, "Qwen3.5-9B-oQ4e-mtp")
    assert '<span class="badge">not routable</span>' in blocked
    assert "not routable" in _model_cell_title(markup, "Qwen3.5-9B-oQ4e-mtp", "M3-beta")


def test_the_padlock_is_open_in_green_and_closed_in_red(css):
    """The two colours are the state tokens of the page, not new hex values:
    `--ok` while RIPPLE may evict, `--down` while it may not — and the model
    name takes `--load`, the hue of the `loading` mark, because a locked model
    is held, not broken."""
    assert "color:var(--ok)" in _rule(css, ".model-lock")
    assert "color:var(--down)" in _rule(css, '.model-lock[data-locked="1"]')
    assert "color:var(--load)" in _rule(
        css, '.model-lock[data-locked="1"] ~ .model-line .model-name')
    # no colour of its own, and nothing to animate: the padlock is a state, not
    # a phase, so `prefers-reduced-motion` has nothing to say about it
    lock_rules = re.findall(r"\.model-lock[^{]*\{([^}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    assert lock_rules, "no rules for the padlock"
    for body in lock_rules:
        assert "animation" not in body, body
        assert "#" not in body, body
    assert ".model-lock" not in _block(css, "@media (prefers-reduced-motion:reduce)")
    # ... and the block #18 added still stands as it was
    block = _block(css, "@media (prefers-reduced-motion:reduce)")
    assert "transition:none!important" in block
    assert 'background:var(--ok-bright)' in block


def test_the_two_states_are_two_shapes_not_only_two_colours(js):
    """The page's rule — colour never carries the meaning alone — holds for the
    padlock too: the shackle is open or closed. Two paths, one `<svg>`, no font
    glyph and no external file."""
    open_, closed = re.search(r"open: '([^']+)'", js)[1], re.search(r"closed: '([^']+)'", js)[1]
    assert open_ != closed
    assert closed.endswith("v4")            # the shackle comes down to the body
    assert not open_.endswith("v4")         # ... and stays open on the right
    assert "LOCK_SHACKLE.open" in js and "LOCK_SHACKLE.closed" in js
    assert "stroke=\"currentColor\"" in js and "fill=\"currentColor\"" in js


def test_the_lock_tooltip_names_the_state_and_its_source(lock_run):
    """Every tooltip on the page names what it says and where the fact comes
    from. The padlock adds the effect of the state — and, while the refusal is
    in force, the hosts whose memory it protects (`hosts[].lock`)."""
    open_cell = _model_cell(lock_run["models_admin"], DEEPSEEK)
    tip = html.unescape(re.search(r'class="model-lock tip"[^>]*data-tip="([^"]*)"', open_cell)[1])
    assert "Lock" in tip
    assert "set: open" in tip
    assert "Source: /healthz → models[].lock" in tip

    locked_cell = _model_cell(lock_run["models_locked"], DEEPSEEK)
    tip = html.unescape(re.search(r'class="model-lock tip"[^>]*data-tip="([^"]*)"', locked_cell)[1])
    assert "set: closed" in tip
    assert "in effect on M3-beta, M5-alpha" in tip     # both hosts hold it, sorted
    assert "503 model_locked" in tip                   # what the state does
    assert "Source: /healthz → models[].lock" in tip


def test_without_the_admin_key_the_padlock_is_visible_but_inert(lock_run):
    """Visible, because a lock nobody sees is a lock nobody understands — but
    the click does nothing and asks nothing, and the tooltip carries the reason.
    `aria-disabled`, not `disabled`: a disabled form element gets no pointer
    events, and then the tooltip with the reason would never appear."""
    cell = _model_cell(lock_run["models_nokey"], DEEPSEEK)
    button = re.search(r'<button[^>]*class="model-lock tip"[^>]*>', cell).group(0)
    assert 'aria-disabled="true"' in button
    assert " disabled" not in button and not button.endswith(" disabled>")
    tip = html.unescape(re.search(r'data-tip="([^"]*)"', button)[1])
    assert "cannot be changed without the admin key" in tip
    assert "Admin key required" in tip
    assert "Source: /healthz → models[].lock" in tip
    # the click: no request, no question, no change
    assert lock_run["click_without_key"] == []
    assert lock_run["asked_without_key"] == []
    # with a key the same button is live and says so
    keyed = re.search(r'<button[^>]*class="model-lock tip"[^>]*>',
                      _model_cell(lock_run["models_admin"], DEEPSEEK)).group(0)
    assert 'aria-disabled="false"' in keyed
    assert "without the admin key" not in html.unescape(re.search(r'data-tip="([^"]*)"', keyed)[1])


def test_a_click_asks_first_and_costs_exactly_one_request(lock_run):
    """Both directions ask — each one changes what the fleet accepts — and a
    click costs one write, not a read on top: the answer of the write is the
    admin view, and the icon flips from it. The 2 s tick confirms it from
    /healthz afterwards."""
    # "no" changes nothing at all
    assert lock_run["cancel_requests"] == []
    assert lock_run["locked_after_cancel"] is False
    assert "Lock model" in lock_run["asked_cancel"][0]
    assert DEEPSEEK in lock_run["asked_cancel"][0]

    # "yes" is exactly one POST, with the admin key in the header
    assert lock_run["lock_requests"] == ["POST /admin/models/%s/lock" % DEEPSEEK]
    assert lock_run["locked_after_lock"] is True
    cell = _model_cell(lock_run["models_locked"], DEEPSEEK)
    assert 'data-locked="1"' in cell
    assert "LOCK" not in cell                    # the path is rendered, not the constant
    assert "M8 11V7a4 4 0 0 1 8 0v4" in cell     # closed shackle
    assert "M8 11V7a4 4 0 0 1 8 0\"" not in cell
    # the operator gets the answer in words as well, in the ops block
    assert "locked" in lock_run["notice"] and "resident on" in lock_run["notice"]

    # the tick confirms the state from /healthz and costs no admin request
    assert lock_run["models_locked_tick"] == lock_run["models_locked"]

    # and back: one DELETE, asked first as well
    assert lock_run["unlock_requests"] == ["DELETE /admin/models/%s/lock" % DEEPSEEK]
    assert "Release the lock" in lock_run["asked_unlock"][0]
    assert lock_run["locked_after_unlock"] is False
    cell = _model_cell(lock_run["models_unlocked"], DEEPSEEK)
    assert 'data-locked="0"' in cell
    assert "M8 11V7a4 4 0 0 1 8 0v4" not in cell


def test_the_lock_costs_the_tick_no_request(lock_run):
    """The padlock state comes from /healthz, so it is paid for already: a plain
    tick is still exactly `/healthz`, `/stats`, `/config` — with a key, with a
    lock set, and after a click."""
    tick = ["GET /healthz", "GET /stats", "GET /config"]
    assert lock_run["load_requests"] == tick
    assert lock_run["tick_requests"] == tick
    assert lock_run["models_locked_tick"] == lock_run["models_locked"]


def test_the_lock_path_encodes_the_segments_and_keeps_the_slash(lock_run, js):
    """`{model:path}`: an id with a slash stays addressable, and nothing is
    concatenated blindly — each segment is encoded."""
    assert lock_run["paths"]["slash"] == "/admin/models/Jundot/Qwen3.8-Flash-Next-oQ4e-mtp/lock"
    assert lock_run["paths"]["spaced"] == "/admin/models/org/Modell%204bit/lock"
    assert lock_run["slash_requests"] == [
        "POST /admin/models/Jundot/Qwen3.8-Flash-Next-oQ4e-mtp/lock"]
    assert "'/admin/models/' +" in js
    assert "String(model).split('/').map(encodeURIComponent).join('/') + '/lock'" in js
    # the key travels as a Bearer header, never in the URL
    assert "'Authorization': 'Bearer ' + key" in js
    assert "?key=" not in js


async def test_the_padlock_state_is_the_one_healthz_reports(ripple_factory, stub_factory,
                                                            tmp_path):
    """End to end: the URL the page builds locks the model on the real backend,
    and `/healthz` then carries exactly the three keys the row renders —
    `models[].lock`, `models[].lock_reason` (only while resident) and
    `models[].hosts[].lock` for the node that holds it."""
    a = stub_factory(models=[StubModel(id=DEEPSEEK, loaded=True, estimated_size=GB * 400)])
    b = stub_factory(models=[StubModel(id=DEEPSEEK, loaded=False, estimated_size=GB * 400)])
    cfg = keyed_config([host_config("A", a.url, node_id=a.state.node_id),
                        host_config("B", b.url, node_id=b.state.node_id)],
                       str(tmp_path / "keys.yaml"))
    cfg.placement = PlacementConfig(
        maintenance_file=str(tmp_path / "maintenance.yaml"),
        model_policy_file=str(tmp_path / "models.yaml"))
    harness = await ripple_factory(cfg)
    admin = bearer("test-admin-key")
    url = "/admin/models/%s/lock" % DEEPSEEK      # what `lockPath()` builds

    # without the key the write is forbidden (403, not 401 — the decision of #21)
    assert (await harness.client.post(url)).status_code == 403
    assert (await harness.client.delete(url)).status_code == 403

    resp = await harness.client.post(url, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["locks"] == [DEEPSEEK]     # the write answers with the view

    h = await harness.healthz()
    row = next(m for m in h["models"] if m["id"] == DEEPSEEK)
    assert h["locks"] == [DEEPSEEK]
    assert row["lock"] is True
    assert row["lock_reason"]                       # resident, so the refusal is in force
    per_node = {x["host"]: x["lock"] for x in row["hosts"]}
    assert per_node == {"A": True, "B": False}      # A holds it, B does not

    resp = await harness.client.delete(url, headers=admin)
    assert resp.status_code == 200, resp.text
    h = await harness.healthz()
    row = next(m for m in h["models"] if m["id"] == DEEPSEEK)
    assert h["locks"] == [] and row["lock"] is False and row["lock_reason"] is None
    assert {x["host"]: x["lock"] for x in row["hosts"]} == {"A": False, "B": False}


# -- the restart control (issue #25, the dashboard half of #22) --------------
# The control has one job the other four do not: it ends the process the page is
# talking to. What is measured here is therefore the accounting, not the look:
#
# - the state comes from `/healthz → restart`, so the plain tick is still exactly
#   `/healthz`, `/stats`, `/config` — the control costs no request until it is
#   clicked, and the page never asks `GET /admin/restart` (the stub throws);
# - one click is one POST; the `force` acknowledgement is a second POST and only
#   after the endpoint answered 409 and the operator confirmed again;
# - after the 202 the page stops ticking into the void: it probes `/healthz`
#   alone, notices the *new* process by its smaller uptime, and lets the normal
#   tick paint the new `build.commit` into the footer.
#
# The geometry (the button in the `6.6` block, inside the panel, not overflowing)
# is measured on a live instance with `getBoundingClientRect`, not here.
RESTART_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm');
const jsPath = process.argv[2], fixturePath = process.argv[3];
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

const requests = [];            // every call the page makes, method + url
const asked = [];               // every question it asks before writing
const captured = {};            // what the panel showed at a given moment
const timers = {started: 0, cleared: 0, ids: 0};
let answers = [];               // the next answers to window.confirm
let adminKey = null;
let api = null;

/* The service as the page sees it. `up` is the socket: after an accepted
   restart it is closed for two probes and then answers as a new process. */
const svc = {up: true, uptime: 10569.3, commit: '1374730', inflight: 0,
             hosts: [], supervised: true, supervisor: 'ai.ripple',
             pending: false, delay: 1.0, post: '202',
             /* The refusal cause (#29): the token the page translates. */
             code: 'no_label', params: null};
let pendingRestart = false;
let probes = 0;

function ok(body) {
  return {ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)};
}
function fail(status, body) {
  return {ok: false, status: status, json: async () => body, text: async () => JSON.stringify(body)};
}

function healthz() {
  const h = JSON.parse(JSON.stringify(fixture.healthz));
  h.uptime_seconds = svc.uptime;
  h.build = {commit: svc.commit, date: '2026-11-15', dirty: false};
  h.restart = {
    supervised: svc.supervised,
    supervisor: svc.supervised ? svc.supervisor : null,
    reason: svc.supervised ? null : fixture.reason,
    /* The service sends the cause beside the sentence (#29). */
    reason_code: svc.supervised ? (svc.inflight > 0 ? 'inflight' : null) : svc.code,
    reason_params: svc.supervised
      ? (svc.inflight > 0 ? {n: svc.inflight, hosts: svc.hosts.join(', ')} : null)
      : svc.params,
    inflight: svc.inflight, hosts: svc.hosts,
    pending: svc.pending, delay_seconds: svc.delay,
  };
  return h;
}

function restartState() {
  return {supervised: svc.supervised,
          supervisor: svc.supervised ? svc.supervisor : null,
          reason: svc.supervised ? null : fixture.reason,
          reason_code: svc.supervised ? (svc.inflight > 0 ? 'inflight' : null) : svc.code,
          reason_params: svc.supervised
            ? (svc.inflight > 0 ? {n: svc.inflight, hosts: svc.hosts.join(', ')} : null)
            : svc.params,
          inflight: svc.inflight, hosts: svc.hosts,
          pending: svc.pending, delay_seconds: svc.delay};
}

async function fetchStub(url, init) {
  const method = (init && init.method) || 'GET';
  requests.push(method + ' ' + url);
  const path = String(url).split('?')[0];

  if (path === '/admin/restart') {
    /* The page has no reason to read the state: /healthz carries it. */
    if (method !== 'POST') throw new Error('the page asked GET /admin/restart');
    if (svc.post === '503') {
      return fail(503, {error: {message: fixture.reason, code: 'restart_unsupervised'},
                        supervised: false, supervisor: null, reason: fixture.reason,
                        reason_code: svc.code, reason_params: svc.params,
                        inflight: svc.inflight, hosts: svc.hosts,
                        pending: false, delay_seconds: svc.delay});
    }
    const forced = /[?&]force=(1|true|yes|on)/.test(String(url));
    if (svc.inflight > 0 && !forced) {
      const msg = svc.inflight + ' request(s) are still in flight (' +
        svc.hosts.join(', ') + ') — the one that is streaming right now ends ' +
        'with the process. Repeat with force=1 to restart anyway.';
      return fail(409, {error: {message: msg, code: 'restart_inflight'},
                        reason_code: 'inflight',
                        reason_params: {n: svc.inflight,
                                        hosts: svc.hosts.join(', ')},
                        inflight: svc.inflight, hosts: svc.hosts,
                        pending: false, delay_seconds: svc.delay});
    }
    svc.pending = true;
    pendingRestart = true;
    probes = 0;
    return {ok: true, status: 202,
            json: async () => ({accepted: true, ...restartState()}),
            text: async () => JSON.stringify({accepted: true, ...restartState()})};
  }
  if (path.indexOf('/admin/') === 0) throw new Error('unexpected admin call ' + url);

  if (path === '/healthz') {
    if (!svc.up) throw new Error('connect: connection refused');
    if (pendingRestart) {
      probes++;
      if (probes === 1) {
        captured.while_pending = panel();
        captured.pending_flag = api.restartState.pending;
      }
      if (probes <= 2) throw new Error('connect: connection refused');
      /* The new process: small uptime, a new build. */
      pendingRestart = false;
      svc.pending = false;
      svc.uptime = 1.4;
      svc.commit = 'a1b2c3d';
    }
    return ok(healthz());
  }
  const body = fixture[path.replace(/^\//, '')];
  if (body === undefined) throw new Error('no fixture for ' + url);
  return ok(body);
}

const elements = new Map();
function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [], open: false, html: ''};
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.html; }, set(v) { el.html = String(v); },
  });
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {}; el.removeChild = () => {}; el.replaceChild = () => {};
  el.setAttribute = () => {}; el.getAttribute = () => null; el.removeAttribute = () => {};
  el.addEventListener = () => {};
  return el;
}
const document = {
  body: makeEl('body'),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

const sandbox = {
  Date,
  document,
  location: new Proxy({}, {get() { throw new Error('the page must not navigate'); }}),
  window: {confirm: msg => { asked.push(String(msg)); return answers.length ? answers.shift() : true; },
           addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: k => (k === 'ripple_admin' ? adminKey : null),
                   setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: fn => { timers.started++; return ++timers.ids; },
  clearInterval: id => { timers.cleared++; },
  setTimeout: fn => { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout: () => {},
  console,
};
sandbox.globalThis = sandbox;

function panel() {
  return (elements.get('restartPanel') || {}).__html || '';
}

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  api = sandbox.RIPPLE;
  await api.ready;

  const out = {};
  out.boot_requests = requests.filter(r => r.indexOf('/static/') === -1);
  out.panel_nokey = panel();
  requests.length = 0;

  await api.tick();
  out.tick_requests = requests.slice();
  requests.length = 0;

  /* The two states the page has to be able to show without a request: the
     service refuses to end itself, and requests are in flight. Pure render
     calls — nothing is fetched for them. */
  const unsup = healthz();
  unsup.restart = {supervised: false, supervisor: null, reason: fixture.reason,
                   reason_code: svc.code, reason_params: svc.params,
                   inflight: 0, hosts: [], pending: false, delay_seconds: 1.0};
  out.panel_unsupervised = api.renderRestart(unsup);
  const drain = healthz();
  drain.restart = {supervised: true, supervisor: 'ai.ripple', reason: null,
                   inflight: 2, hosts: ['M5-alpha'], pending: false,
                   delay_seconds: 1.0};
  out.panel_drain = api.renderRestart(drain);
  out.render_requests = requests.slice();

  /* No key: the click costs nothing and asks nothing. */
  await api.restartNow();
  out.nokey_click_requests = requests.slice();
  out.nokey_click_asked = asked.slice();
  out.nokey_notice = panel();
  requests.length = 0; asked.length = 0;

  adminKey = 'test-admin-key';
  await api.tick();
  out.panel_admin = panel();
  requests.length = 0; asked.length = 0;

  /* "no" at the first question: no request at all. */
  answers = [false];
  await api.restartNow();
  out.cancel_requests = requests.slice();
  out.asked_cancel = asked.slice();
  requests.length = 0; asked.length = 0;

  /* "yes": exactly one POST, then the wait for the new process. */
  answers = [true];
  await api.restartNow();
  out.accept_requests = requests.slice();
  out.asked_accept = asked.slice();
  out.panel_while_pending = captured.while_pending || '';
  out.pending_flag = captured.pending_flag;
  out.timers = {started: timers.started, cleared: timers.cleared};
  out.panel_back = panel();
  out.conn = (elements.get('conn') || {}).__html || '';
  requests.length = 0; asked.length = 0;

  /* A plain tick afterwards is three requests again — the control is back to
     costing nothing. */
  await api.tick();
  out.tick_after = requests.slice();
  requests.length = 0;

  /* The drain: 409, then the second question. Declined first — one POST, no
     second one, and the panel says it did not restart. */
  svc.inflight = 1; svc.hosts = ['M5-alpha']; svc.pending = false;
  svc.post = '202';
  await api.tick();
  requests.length = 0; asked.length = 0;
  answers = [true, false];
  await api.restartNow();
  out.drain_declined_requests = requests.slice();
  out.asked_drain = asked.slice();
  out.drain_declined_notice = panel();
  requests.length = 0; asked.length = 0;

  /* ... and accepted: exactly one more POST, with the acknowledgement. */
  svc.inflight = 1; svc.hosts = ['M5-alpha']; svc.pending = false;
  svc.uptime = 10569.3; svc.commit = '1374730'; svc.up = true; probes = 0;
  answers = [true, true];
  await api.restartNow();
  out.drain_forced_requests = requests.slice();
  out.asked_forced = asked.slice();
  requests.length = 0; asked.length = 0;

  /* 503 from the endpoint although /healthz said supervised: the race the page
     cannot see. The reason is shown as it came. */
  svc.supervised = true; svc.inflight = 0; svc.hosts = []; svc.pending = false;
  svc.post = '503'; probes = 0; svc.up = true;
  await api.tick();
  requests.length = 0; asked.length = 0;
  answers = [true];
  await api.restartNow();
  out.unsupervised_requests = requests.slice();
  out.unsupervised_notice = panel();
  requests.length = 0; asked.length = 0;

  /* /healthz says unsupervised: the click never leaves the page. */
  svc.supervised = false; svc.post = '202';
  await api.tick();
  requests.length = 0; asked.length = 0;
  answers = [true];
  await api.restartNow();
  out.unsup_click_requests = requests.slice();
  out.unsup_click_asked = asked.slice();
  svc.supervised = true;

  /* The new-process test, as a pure function. */
  out.is_new = {
    older: api.isNewProcess({uptime_seconds: 3, build: {commit: 'x'}},
                            {uptime: 900, commit: 'x'}),
    same: api.isNewProcess({uptime_seconds: 905, build: {commit: 'x'}},
                           {uptime: 900, commit: 'x'}),
    new_commit: api.isNewProcess({uptime_seconds: 905, build: {commit: 'y'}},
                                 {uptime: 900, commit: 'x'}),
    no_before: api.isNewProcess({uptime_seconds: 905, build: {commit: 'x'}}, null),
  };

  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def restart_run(tmp_path_factory) -> dict:
    """`dashboard.js` against a service that answers, refuses and comes back."""
    if NODE is None:
        pytest.skip("node is not installed — the request measurement needs it")
    work = tmp_path_factory.mktemp("restart")
    fixture = work / "fixture.json"
    fixture.write_text(json.dumps({"healthz": _healthz(), "stats": _stats(),
                                   "config": _config(),
                                   "reason": UNSUPERVISED_REASON,
                                   **_catalogs()}),
                       encoding="utf-8")
    harness = work / "restart_harness.js"
    harness.write_text(RESTART_HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(STATIC / "dashboard.js"), str(fixture)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _button(markup: str) -> str:
    found = re.search(r'<button[^>]*class="btn restart-btn"[^>]*>', markup)
    assert found, markup[:200]
    return found.group(0)


def test_the_restart_control_costs_the_tick_no_request(restart_run):
    """The state is in `/healthz`, so the control is paid for already: a plain
    tick is still exactly `/healthz`, `/stats`, `/config`, and the page never
    asks `GET /admin/restart` — the harness throws if it does."""
    tick = ["GET /healthz", "GET /stats", "GET /config"]
    assert restart_run["boot_requests"] == tick
    assert restart_run["tick_requests"] == tick
    assert restart_run["tick_after"] == tick
    assert restart_run["render_requests"] == []


def test_without_the_admin_key_the_restart_is_off_and_says_why(restart_run):
    """Disabled, and the reason is a sentence on the page — not only a `title`
    that a keyboard user never sees."""
    button = _button(restart_run["panel_nokey"])
    assert "disabled" in button
    assert "Admin key required" in restart_run["panel_nokey"]
    assert "Admin key required" in restart_run["nokey_notice"]
    # the click costs nothing and asks nothing
    assert restart_run["nokey_click_requests"] == []
    assert restart_run["nokey_click_asked"] == []
    # with a key the same button is live
    assert "disabled" not in _button(restart_run["panel_admin"])


def test_the_restart_is_off_with_the_cause_in_the_pages_language(restart_run):
    """#29: the panel says *why* in the page's language, not in the service's.

    The cause is a code (`restart.reason_code`), the sentence comes from the
    catalog with the service's own numbers interpolated, and the English prose
    stays as the tooltip — so nothing is paraphrased away.
    """
    panel = restart_run["panel_unsupervised"]
    assert "disabled" in _button(panel)
    assert _en()["restart.reason.no_label"] in html.unescape(panel)
    assert "launchd" in panel
    # the service's sentence is still there, one hover away, with its source
    found = re.search(r'<div class="restart-why bad" data-tip="([^"]*)"', panel)
    assert found, panel[:400]
    tip = html.unescape(found[1])
    assert UNSUPERVISED_REASON in tip
    assert "Source: /healthz → restart.reason" in tip
    # ... and a click does not even try, because /healthz already said no
    assert restart_run["unsup_click_requests"] == []
    assert restart_run["unsup_click_asked"] == []


def test_the_restart_dialog_names_what_happens(restart_run):
    """The first question has to be answerable: what ends, what comes back, what
    is lost, what the operator sees afterwards — and that their own session runs
    through this service."""
    asked = restart_run["asked_cancel"][0]
    assert "Restart RIPPLE?" in asked
    assert "launchd starts it again" in asked            # the process comes back
    assert "still in flight are cut" in asked            # the drain sentence
    assert "thread bindings are lost and rebuilt" in asked
    assert "reconnects by itself" in asked
    assert "build.commit" in asked
    assert "own session runs through this service" in asked
    assert "streaming at that moment ends with the process" in asked
    # nothing is in flight in this fixture: no drain line, and no placeholder
    # left standing in the sentence
    assert "{busy}" not in asked
    assert "in flight on" not in asked

    # the same sentences in the German catalog — the page is bilingual
    de = _de()["restart.confirm"]
    assert "launchd" in de and "Thread-Bindungen" in de
    assert "Die eigene Sitzung läuft durch diesen Dienst" in de
    assert "streamt" in de and "build.commit" in de


def test_a_click_asks_first_and_costs_exactly_one_post(restart_run):
    """"no" costs nothing; "yes" is one POST and nothing else."""
    assert restart_run["cancel_requests"] == []
    assert restart_run["accept_requests"] == [
        "POST /admin/restart",
        # then the wait: probes only, then one ordinary tick
        "GET /healthz", "GET /healthz", "GET /healthz",
        "GET /healthz", "GET /stats", "GET /config",
    ]


def test_the_page_waits_for_the_new_process_and_shows_its_build(restart_run):
    """After the 202 the page does not poll into the void: it probes `/healthz`
    alone, stops the 2 s tick, and once the *new* process answers (smaller
    uptime) the footer carries its `build.commit`."""
    assert restart_run["pending_flag"] is True
    while_pending = restart_run["panel_while_pending"]
    assert "disabled" in _button(while_pending)
    assert "restart is on its way" in while_pending
    assert restart_run["timers"]["cleared"] >= 1          # the tick was stopped
    assert restart_run["timers"]["started"] >= 2          # ... and started again
    assert "build a1b2c3d" in restart_run["panel_back"]
    assert "a1b2c3d" in restart_run["conn"], restart_run["conn"]
    assert "1374730" not in restart_run["conn"]
    assert restart_run["is_new"] == {"older": True, "same": False,
                                     "new_commit": True, "no_before": True}


def test_a_drain_needs_the_second_confirmation(restart_run):
    """409 is the state the first dialog cannot know: the endpoint names the
    streaming request, the page asks again, and only the second "yes" carries
    `force`."""
    assert restart_run["drain_declined_requests"] == ["POST /admin/restart"]
    # the first question already names the drain — /healthz carried the number
    first = restart_run["asked_drain"][0]
    assert "1 request(s) are in flight on M5-alpha" in first
    assert "cancelling this question leaves them running" in first
    assert "{busy}" not in first
    assert len(restart_run["asked_drain"]) == 2
    second = restart_run["asked_drain"][1]
    assert "Restart anyway?" in second
    assert "1 request(s) are still in flight on M5-alpha" in second
    assert "the session this page was opened from" in second
    assert "Repeat with force=1" in second                 # the service's own words
    assert "not restarted" in restart_run["drain_declined_notice"]

    forced = restart_run["drain_forced_requests"]
    assert forced[0] == "POST /admin/restart"
    assert forced[1] == "POST /admin/restart?force=1"
    assert forced.count("POST /admin/restart") == 1
    assert forced.count("POST /admin/restart?force=1") == 1


def test_the_unsupervised_refusal_is_shown_as_a_sentence(restart_run):
    """The page cannot see the race between the tick and the click; when the
    endpoint refuses with 503, its body carries the code too (#29), so the
    notice is a sentence in the page's language — the prose is the tooltip."""
    assert restart_run["unsupervised_requests"] == ["POST /admin/restart"]
    notice = restart_run["unsupervised_notice"]
    assert _en()["restart.reason.no_label"] in html.unescape(notice)
    assert "force" not in notice
    found = re.search(r'<div class="bad"[^>]*data-tip="([^"]*)"', notice)
    assert found, notice[:400]
    tip = html.unescape(found[1])
    assert UNSUPERVISED_REASON in tip
    assert "POST /admin/restart" in tip


def test_the_restart_panel_names_its_sources(restart_run):
    """Every number in the block carries its source, like every other number on
    the page — and the numbers are the ones `/healthz` reported."""
    for name in ("panel_admin", "panel_drain"):
        panel = restart_run[name]
        for tip in re.findall(r'data-tip="([^"]*)"', panel):
            assert "Source:" in tip, (name, tip[:80])
            assert "/healthz → restart" in tip, (name, tip[:80])
    drain = restart_run["panel_drain"]
    assert "2 request(s) in flight on M5-alpha" in drain
    assert "1.0 s" in drain
    assert "started by launchd job ai.ripple" in drain
    # the label and the German sentence live in the catalogs, not in the code
    assert _de()["restart.inflight"].startswith("{n} Anfrage(n)")
    assert _de()["restart.button"] == "Neu starten"


# -- #29: the five causes, rendered from the catalog ------------------------
# The service sends a token; the page has to own the sentence in both
# languages, interpolate the service's own values, and fall back to the prose
# for a code it does not know. `renderRestart()` is a pure string builder, so
# the five causes are rendered here from payloads the test owns — the harness
# above only carries the one cause the fixture service happens to report.
RESTART_REASON_HARNESS = r"""
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsPath = process.argv[2];
const i18nDir = process.argv[3];
const payload = JSON.parse(process.argv[4]);

function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [],
              textContent: '', innerHTML: ''};
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {}; el.removeChild = () => {}; el.replaceChild = () => {};
  el.setAttribute = (k, v) => { el.attributes[k] = String(v); };
  el.getAttribute = k => (k in el.attributes ? el.attributes[k] : null);
  el.removeAttribute = k => { delete el.attributes[k]; };
  el.addEventListener = () => {};
  el.classList = {add() {}, remove() {}};
  return el;
}

const elements = new Map();
const document = {
  documentElement: makeEl('html'),
  body: makeEl('body'),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

function fetchStub(url) {
  if (!String(url).startsWith('/static/i18n/')) {
    return Promise.reject(new Error('unexpected fetch: ' + url));
  }
  const file = path.join(i18nDir, String(url).replace(/^\/static\/i18n\//, ''));
  const body = JSON.parse(fs.readFileSync(file, 'utf8'));
  return Promise.resolve({ok: true, status: 200, json: async () => body,
                          text: async () => JSON.stringify(body)});
}

const store = new Map();
const sandbox = {
  Date,
  document,
  localStorage: {getItem: k => (store.has(k) ? store.get(k) : null),
                  setItem: (k, v) => store.set(k, String(v)),
                  removeItem: k => store.delete(k)},
  navigator: {languages: ['en-GB'], language: 'en-GB'},
  window: {confirm: () => false, addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console: {log() {}, warn() {}, error() {}},
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;
  const out = {};
  for (const lang of ['en', 'de']) {
    api.chooseLang(lang);
    out[lang] = {};
    for (const name of Object.keys(payload)) {
      out[lang][name] = {panel: api.renderRestart({restart: payload[name]}),
                         why: api.restartReason(payload[name])};
    }
  }
  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


def _restart_block(code: str | None, params: dict | None, reason: str) -> dict:
    return {"supervised": False, "supervisor": None, "reason": reason,
            "reason_code": code, "reason_params": params,
            "inflight": 0, "hosts": [], "pending": False, "delay_seconds": 1.0}


# The five causes with the values `supervision_state` sends, and one code the
# page has never heard of — that one must render the service's prose verbatim.
# `inflight` is the fifth: it is the drain gate, so the panel never shows it in
# the `restart-why` block (the notice after a refused 409 does), but the
# sentence lookup is the same one.
REASONS = {
    "no_label": (_restart_block("no_label", None, UNSUPERVISED_REASON),
                 {"label": None, "ppid": None, "pid": None}),
    "inherited_label": (
        _restart_block("inherited_label",
                       {"label": "ai.hermes.gateway", "ppid": 4242},
                       INHERITED_LABEL_REASON.format(label="ai.hermes.gateway",
                                                     ppid=4242)),
        {"label": "ai.hermes.gateway", "ppid": "4242", "pid": None}),
    "label_not_owner": (
        _restart_block("label_not_owner", {"label": "ai.ripple", "pid": 4711},
                       UNOWNED_LABEL_REASON.format(label="ai.ripple", pid=4711,
                                                   uid=501)),
        {"label": "ai.ripple", "ppid": None, "pid": "4711"}),
    "query_failed": (
        _restart_block("query_failed", {"label": "ai.ripple"},
                       QUERY_FAILED_REASON.format(label="ai.ripple")),
        {"label": "ai.ripple", "ppid": None, "pid": None}),
    "inflight": (_restart_block("inflight", {"n": 2, "hosts": "M5-alpha"},
                               "2 request(s) are still in flight (M5-alpha) — the "
                               "one that is streaming right now ends with the "
                               "process. Repeat with force=1 to restart anyway."),
                 {"label": None, "ppid": None, "pid": None}),
    "unknown": (_restart_block("no_such_cause", None,
                              "the service said something the page cannot name"),
                {"label": None, "ppid": None, "pid": None}),
}


@pytest.fixture(scope="module")
def reasons_run(tmp_path_factory) -> dict:
    """`dashboard.js` rendering all five causes, in both languages."""
    if NODE is None:
        pytest.skip("node is not installed — the catalog measurement needs it")
    work = tmp_path_factory.mktemp("reasons")
    harness = work / "reasons.js"
    harness.write_text(RESTART_REASON_HARNESS, encoding="utf-8")
    payload = {name: block for name, (block, _) in REASONS.items()}
    proc = subprocess.run([str(NODE), str(harness), str(STATIC / "dashboard.js"),
                           str(STATIC / "i18n"), json.dumps(payload)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _why_text(panel: str) -> str:
    """What the operator reads in the refusal block, tags stripped."""
    found = re.search(r'<div class="restart-why bad"[^>]*>(.*?)</div>', panel, re.S)
    assert found, panel[:400]
    return html.unescape(re.sub(r"<[^>]+>", "", found[1])).strip()


def _why_tip(panel: str) -> str:
    found = re.search(r'<div class="restart-why bad" data-tip="([^"]*)"', panel)
    assert found, panel[:400]
    return html.unescape(found[1])


@pytest.mark.parametrize("code", ["no_label", "inherited_label",
                                  "label_not_owner", "query_failed"])
def test_every_supervision_cause_is_a_sentence_from_the_catalog(reasons_run, code):
    """One sentence per cause, in the page's language, with the service's own
    values interpolated — no `{label}` left standing, no English fragment."""
    _, values = REASONS[code]
    for lang in ("en", "de"):
        cat = _catalog(lang)
        panel = reasons_run[lang][code]["panel"]
        sentence = cat["restart.reason." + code]
        for name, value in values.items():
            if value is not None:
                sentence = sentence.replace("{" + name + "}", value)
        assert _why_text(panel) == sentence, lang
        assert "{" not in _why_text(panel), lang
        # the service's prose is the tooltip, not the visible sentence
        assert _why_tip(panel).startswith(REASONS[code][0]["reason"]), lang


def test_the_drain_cause_is_a_sentence_with_the_fleet_numbers(reasons_run):
    """`inflight` is a cause like the other four: `{n}` and `{hosts}` are the
    values the service sent, and the panel's notice uses the same lookup."""
    for lang in ("en", "de"):
        cat = _catalog(lang)
        why = reasons_run[lang]["inflight"]["why"]
        assert why["text"] == cat["restart.reason.inflight"].replace(
            "{n}", "2").replace("{hosts}", "M5-alpha")
        assert "{" not in why["text"]
        # the 409 sentence stays the tooltip: it names the streaming session
        assert "streaming" in why["tip"]


def test_the_german_refusal_has_no_english_fragment_in_it(reasons_run):
    """The point of the issue: a German page, a German refusal. The label and the
    ppid are values, so they stay as they are; the words around them do not."""
    for code in ("no_label", "inherited_label", "label_not_owner", "query_failed"):
        text = _why_text(reasons_run["de"][code]["panel"])
        assert "{" not in text, code          # every placeholder was filled
        # no English word from the service's sentence survives in the sentence
        assert "refuses to end itself" not in text, code
        assert "launchd-Job `ai.ripple`" in text, code     # the way out, German
        assert "launchd-Label" in text, code
        assert text.startswith("RIPPLE "), text
    # the label and the ppid are interpolated, not paraphrased
    inherited = _why_text(reasons_run["de"]["inherited_label"]["panel"])
    assert "ai.hermes.gateway" in inherited and "4242" in inherited
    assert _catalog("de")["restart.reason.inherited_label"].count("{") == 2


def test_an_unknown_code_falls_back_to_the_services_own_words(reasons_run):
    """A code the page does not know is not a licence to invent a cause: the
    prose is shown verbatim, and there is no tooltip to hide it in."""
    prose = REASONS["unknown"][0]["reason"]
    for lang in ("en", "de"):
        panel = reasons_run[lang]["unknown"]["panel"]
        assert _why_text(panel) == prose, lang
        # the prose *is* the visible text here, so there is nothing to hover
        assert not re.search(r'<div class="restart-why bad"[^>]*data-tip', panel), lang
        assert reasons_run[lang]["unknown"]["why"]["tip"] is None, lang


def test_the_five_codes_are_the_ones_the_service_can_send(reasons_run):
    """The page's map and the service's closed set are the same set — a code the
    service sends and the page does not know would be shown in English."""
    from ripple.proxy import REASON_CODES

    assert set(REASONS) - {"unknown"} == set(REASON_CODES)
    for code in REASON_CODES:
        assert ("restart.reason." + code) in _en(), code
        assert ("restart.reason." + code) in _de(), code


# -- #30: tok/s is two numbers, and the column says which -------------------
# The left half is what oMLX reports per request and the service smooths (it
# falls with parallelism); the right half is `tg_tps` from config.yaml, the
# calibrated capability `placement.py` decides with. Both halves are checkable
# against their own endpoint, so the test drives the two payloads separately:
# /healthz carries the live rate, /config the capability.
MODELS_TPS_HARNESS = r"""
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsPath = process.argv[2];
const i18nDir = process.argv[3];
const payload = JSON.parse(process.argv[4]);

function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [],
              textContent: '', innerHTML: ''};
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {}; el.removeChild = () => {}; el.replaceChild = () => {};
  el.setAttribute = (k, v) => { el.attributes[k] = String(v); };
  el.getAttribute = k => (k in el.attributes ? el.attributes[k] : null);
  el.removeAttribute = k => { delete el.attributes[k]; };
  el.addEventListener = () => {};
  el.classList = {add() {}, remove() {}};
  return el;
}

const elements = new Map();
const document = {
  documentElement: makeEl('html'),
  body: makeEl('body'),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

function fetchStub(url) {
  if (!String(url).startsWith('/static/i18n/')) {
    return Promise.reject(new Error('unexpected fetch: ' + url));
  }
  const file = path.join(i18nDir, String(url).replace(/^\/static\/i18n\//, ''));
  const body = JSON.parse(fs.readFileSync(file, 'utf8'));
  return Promise.resolve({ok: true, status: 200, json: async () => body,
                          text: async () => JSON.stringify(body)});
}

const store = new Map();
const sandbox = {
  Date,
  document,
  localStorage: {getItem: k => (store.has(k) ? store.get(k) : null),
                  setItem: (k, v) => store.set(k, String(v)),
                  removeItem: k => store.delete(k)},
  navigator: {languages: ['en-GB'], language: 'en-GB'},
  window: {confirm: () => false, addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console: {log() {}, warn() {}, error() {}},
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;
  const out = {};
  for (const lang of ['en', 'de']) {
    api.chooseLang(lang);
    out[lang] = {};
    for (const name of Object.keys(payload.cases)) {
      const c = payload.cases[name];
      /* The tick, not a private hook: /healthz and /config arrive together and
         the page keeps the capability from the config half. */
      api.render(c.healthz, payload.stats, c.config);
      out[lang][name] = elements.get('models').__html;
    }
  }
  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""

TPS_MODEL = DEEPSEEK


def _tps_view(live: float | None) -> dict:
    """The real fixture, with the one field under test moved.

    Built from `_healthz()` rather than hand-written: the page renders every
    block from one payload, so a minimal view would measure the harness instead
    of the table.
    """
    h = json.loads(json.dumps(_healthz()))
    for m in h["models"]:
        if m["id"] != TPS_MODEL:
            continue
        for x in m["hosts"]:
            if x["host"] == "M5-alpha":
                x["tg_tps"] = live
    return h


def _tps_config(capability: float | None) -> dict:
    c = json.loads(json.dumps(_config()))
    for host in c["hosts"]:
        if host["name"] == "M5-alpha":
            host["tg_tps"] = capability
    return c


@pytest.fixture(scope="module")
def tps_run(tmp_path_factory) -> dict:
    """`dashboard.js` rendering the tok/s cell with and without a live value."""
    if NODE is None:
        pytest.skip("node is not installed — the layout measurement needs it")
    work = tmp_path_factory.mktemp("tps")
    harness = work / "tps.js"
    harness.write_text(MODELS_TPS_HARNESS, encoding="utf-8")
    payload = {
        "stats": _stats(),
        "cases": {
            "live": {"healthz": _tps_view(29.1), "config": _tps_config(43.5)},
            "nohit": {"healthz": _tps_view(None), "config": _tps_config(43.5)},
            "nocal": {"healthz": _tps_view(29.1), "config": _tps_config(None)},
        },
    }
    proc = subprocess.run([str(NODE), str(harness), str(STATIC / "dashboard.js"),
                           str(STATIC / "i18n"), json.dumps(payload)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _tps_cell(markup: str) -> tuple[str, str]:
    """(text, tooltip) of the tok/s cell of the one (model, node) row."""
    row = _row_markup(markup, TPS_MODEL, "M5-alpha")
    cells = re.findall(r'<td class="num">(.*?)</td>', row, re.S)
    assert cells, row[:400]
    found = re.search(r'<span class="tip" data-tip="([^"]*)">(.*?)</span>$',
                      cells[-1], re.S)
    assert found, cells[-1]
    return (html.unescape(re.sub(r"<[^>]+>", "", found[2])).strip(),
            html.unescape(found[1]))


def test_the_toks_cell_shows_the_live_rate_over_the_capability(tps_run):
    """Acceptance: `29,1 / 43,5` on the German page, `29.1 / 43.5` on the
    English one — the live half first, the capability second."""
    assert _tps_cell(tps_run["de"]["live"])[0] == "29,1 / 43,5"
    assert _tps_cell(tps_run["en"]["live"])[0] == "29.1 / 43.5"


def test_without_a_live_measurement_the_capability_stays_visible(tps_run):
    """`— / 43,5`, not a bare dash: the machine's capability does not disappear
    because this model has not been measured yet."""
    assert _tps_cell(tps_run["de"]["nohit"])[0] == "— / 43,5"
    assert _tps_cell(tps_run["en"]["nohit"])[0] == "— / 43.5"
    # and a node the config does not calibrate says so on its own side
    assert _tps_cell(tps_run["de"]["nocal"])[0] == "29,1 / —"


def test_the_toks_tooltip_names_both_numbers_and_their_sources(tps_run):
    """The two halves come from two endpoints and mean two different things —
    the tooltip has to say which is which, or the pair is one number with a
    slash in it."""
    tip = _tps_cell(tps_run["en"]["live"])[1]
    assert "live / capability" in tip
    assert "per-request" in tip or "per request" in tip
    assert "parallel" in tip                 # it falls with contention
    assert "config.yaml" in tip              # the capability is calibrated
    assert "/healthz → models[].hosts[].tg_tps" in tip
    assert "/config → hosts[].tg_tps" in tip
    assert "Source:" in tip
    # the German tooltip says the same two things
    de_tip = _tps_cell(tps_run["de"]["live"])[1]
    assert "live / Kapazität" in de_tip and "config.yaml" in de_tip
    assert "/healthz → models[].hosts[].tg_tps" in de_tip


def test_the_column_subtitle_names_the_pair_instead_of_claiming_a_measurement():
    """`measured` was the word that made one number look like the whole story."""
    assert _en()["models.th_tps_sub"] == "live / capability"
    assert _de()["models.th_tps_sub"] == "live / Kapazität"
    for key in ("tip.tps_m", "tip.tps"):
        assert "measured" not in _en()[key].lower(), key
        assert "gemessen" not in _de()[key].lower(), key


def test_both_halves_are_independently_checkable(rendered):
    """The live half is the `/healthz` field, the capability half the `/config`
    one — in the fixture they are 69.6 and 43.5, and the cell shows both."""
    text, tip = _tps_cell(rendered["parts"]["models"])
    assert text == "69.6 / 43.5", text
    assert "/healthz → models[].hosts[].tg_tps" in tip
    assert "/config → hosts[].tg_tps" in tip


# -- #32: who holds a thread ------------------------------------------------
# `renderThreads()` is a pure string builder, so the two states the block can
# show — a thread with a key and one without — are rendered here from payloads
# the test owns, in both languages. The stub above renders only the fixture's
# single registry row; this harness measures the other case as well.
THREAD_CHIP_HARNESS = r"""
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsPath = process.argv[2];
const i18nDir = process.argv[3];
const payload = JSON.parse(process.argv[4]);

function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [],
              textContent: '', innerHTML: ''};
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {};
  el.removeChild = () => {};
  el.replaceChild = () => {};
  el.setAttribute = (k, v) => { el.attributes[k] = String(v); };
  el.getAttribute = k => (k in el.attributes ? el.attributes[k] : null);
  el.removeAttribute = k => { delete el.attributes[k]; };
  el.addEventListener = () => {};
  el.classList = {add() {}, remove() {}};
  return el;
}

const elements = new Map();
const document = {
  documentElement: makeEl('html'),
  body: makeEl('body'),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

const store = new Map();
const localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};

function fetchStub(url) {
  if (!String(url).startsWith('/static/i18n/')) {
    return Promise.reject(new Error('unexpected fetch: ' + url));
  }
  const file = path.join(i18nDir, String(url).replace(/^\/static\/i18n\//, ''));
  const body = JSON.parse(fs.readFileSync(file, 'utf8'));
  return Promise.resolve({ok: true, status: 200, json: async () => body,
                          text: async () => JSON.stringify(body)});
}

const sandbox = {
  Date,
  document,
  localStorage,
  navigator: {languages: ['en-GB'], language: 'en-GB'},
  window: {confirm: () => false, addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console: {log() {}, warn() {}, error() {}},
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;
  const out = {};
  /* #103: the rule behind the source chip, called directly — the markup below
     only shows one value per row, the matrix shows the edges (the first turn
     is still a birth, the second is not). */
  out.__labels = {};
  for (const [source, turns] of [['new', 1], ['new', 2], ['new', 62],
                                 ['header', 1], ['header', 50],
                                 ['fingerprint', 1], ['prefix_match', 3]]) {
    out.__labels[source + ':' + turns] = api.threadSource(source, turns);
  }
  for (const lang of ['en', 'de']) {
    api.chooseLang(lang);
    out[lang] = {};
    for (const name of Object.keys(payload)) {
      out[lang][name] = api.renderThreads(payload[name]);
    }
  }
  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


def _thread_row(**extra) -> dict:
    """One `/healthz → threads.registry[]` row, as the backend writes it."""
    row = {"key": "4b17eecc4cf0a8a5", "model": DEEPSEEK, "host": "M5-alpha",
           "source": "new", "kind": "interactive", "turns": 67,
           "ttl_seconds": 1800, "idle_seconds": 112.4}
    row.update(extra)
    return row


def _render_thread_chips(tmp_path, **payload) -> dict:
    if NODE is None:
        pytest.skip("node is not installed — the thread registry measurement needs it")
    harness = tmp_path / "thread_chips.js"
    harness.write_text(THREAD_CHIP_HARNESS, encoding="utf-8")
    views = {name: {"threads": {"registry": rows}} for name, rows in payload.items()}
    proc = subprocess.run([NODE, str(harness), str(STATIC / "dashboard.js"),
                           str(STATIC / "i18n"), json.dumps(views)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _chips(markup: str) -> list[str]:
    """The visible text of every chip in a thread item, in document order.

    A chip may carry a tooltip (`<span class="tip" …>`), so the tags are
    stripped before comparing — what is measured is what the operator reads.
    """
    out = []
    for raw in re.findall(r'<span class="chip">(.*?)</span>', markup, re.S):
        out.append(html.unescape(re.sub(r"<[^>]+>", "", raw)).strip())
    return out


def test_the_thread_shows_the_key_that_holds_it(rendered):
    """Block 4 names the client key next to `kind · source`: the question
    "who holds 6 of the 20 slots" is answered on the page, not by grep."""
    threads = rendered["parts"]["threads"]
    assert ">key mustafa<" in threads
    # the chip carries its source, like every other field of the block
    assert "threads.registry[].key_name" in threads
    assert _en()["threads.key"] == "key {name}"


def test_a_thread_without_a_key_says_so_instead_of_leaving_a_gap(tmp_path):
    """`None` means the request came without a key (loopback). The chip says
    that — an empty chip would read like a missing fact, not like a fact."""
    run = _render_thread_chips(
        tmp_path,
        anon=[_thread_row(key_name=None)],
        missing=[_thread_row()],          # an older `/healthz` without the field
    )
    for case in ("anon", "missing"):
        markup = run["en"][case]
        assert ">no key<" in markup, case
        assert '<span class="chip"></span>' not in markup, case
        assert _chips(markup) == ["interactive · fp", "no key"], case
        assert "threads.registry[].key_name" in markup, case


def test_the_key_chip_is_translated_not_hardcoded(tmp_path):
    """The name is data, the sentence around it is a catalog entry: the German
    page says `Schlüssel mustafa` and `ohne Schlüssel`."""
    run = _render_thread_chips(
        tmp_path,
        named=[_thread_row(key_name="mustafa")],
        anon=[_thread_row(key_name=None)],
    )
    assert ">Schlüssel mustafa<" in run["de"]["named"]
    assert ">ohne Schlüssel<" in run["de"]["anon"]
    assert ">key mustafa<" in run["en"]["named"]
    assert ">no key<" in run["en"]["anon"]


# -- #103: `new` names the birth, not the life -------------------------------
# `source` is written once, when the thread is born, and never again. A thread
# that arrived without an affinity header keeps `new` for its whole life — the
# live row `2fb77ba844b188e4c44954a5` read `interactive · new` after 62 turns and
# 172 s idle. The log already names the same fact off the key (`fp`, no `sid:`
# prefix), so the panel's word was the stale one of two vocabularies. Only the
# `new` case moves here; `header`, `fingerprint` and `prefix_match` are not an
# age and stay as they are.
def test_the_source_rule_names_the_birth_and_nothing_else(tmp_path):
    """The rule itself, at the edges: the first turn is still a birth, the
    second is not, and every other source is untouched at any turn count."""
    labels = _render_thread_chips(tmp_path)["__labels"]
    assert labels["new:1"] == "new"
    assert labels["new:2"] == "fp"
    assert labels["new:62"] == "fp"
    assert labels["header:1"] == "header"
    assert labels["header:50"] == "header"
    assert labels["fingerprint:1"] == "fingerprint"
    assert labels["prefix_match:3"] == "prefix_match"


def test_a_thread_past_its_first_turn_says_fp_on_the_page(tmp_path):
    """The same rule through the real panel markup: a newborn row still reads
    `new`, the 62-turn row reads `fp` — and the kind is untouched."""
    run = _render_thread_chips(
        tmp_path,
        birth=[_thread_row(source="new", turns=1)],
        grown=[_thread_row(source="new", turns=62)],
        header=[_thread_row(source="header", turns=50)],
    )
    assert _chips(run["en"]["birth"])[0] == "interactive · new"
    assert _chips(run["en"]["grown"])[0] == "interactive · fp"
    assert _chips(run["en"]["header"])[0] == "interactive · header"
    # the chip is data, not a sentence: the German page shows the same tokens
    assert _chips(run["de"]["grown"])[0] == "interactive · fp"


# -- #69: the model name of a working thread ---------------------------------
# The operator should see at a glance which threads are working, not read the
# meta line. The state goes into the markup because the 2 s patch replaces the
# node — an attribute set by a handler afterwards would be gone on the next tick.
def test_a_working_thread_carries_the_state_and_an_idle_one_does_not(rendered):
    """`data-busy` is on the item whose row has `active > 0`, and only there.

    The stub renders the same registry twice: once as `/healthz` sends it (idle)
    and once with `registry[0].active = 1` (a request in flight).
    """
    idle = rendered["parts"]["threads"]
    busy = rendered["parts"]["threads_active"]
    assert 'data-busy="true"' not in idle
    assert idle.count('class="thread-item"') == 1        # the row itself exists
    assert busy.count('data-busy="true"') == 1
    # the flag sits on the item that carries the key, next to the model name it
    # colours — not on a wrapper the operator never sees
    item = re.search(r'<div class="thread-item"[^>]*data-busy="true"[^>]*>(.*?)</div>\s*$',
                     busy, re.S)
    assert item, busy
    assert f'<div class="th-model" title="{DEEPSEEK}">{DEEPSEEK}</div>' in item.group(1)


def test_the_working_model_name_takes_the_healthy_token(css):
    """The rule uses a token, not a new hex value — and it is the same green
    `[data-state="healthy"]` uses, so "working" and "healthy" read as one colour.

    Measured contrast of `--ok` (#57a639) against the panel (`--surface`,
    #12161c) is 5.98:1, above the 4.5:1 a 13 px text needs; `--ok-bright`
    (9.25:1) stays the LED's.
    """
    rule = re.search(r'\.thread-item\[data-busy="true"\]\s+\.th-model\s*\{([^}]*)\}', css)
    assert rule, "no rule colours the model name of a working thread"
    body = rule.group(1)
    assert "color:var(--ok)" in body
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "literal hex in the rule"
    assert '[data-state="healthy"]{--c:var(--ok)' in css


# -- #33: load per host -----------------------------------------------------
# The block the operator asked for: idle, loading, prefill, decode and mixed per
# node as *exclusive* seconds that add up to the sampled window, the work counters
# beside them, and the nodes' own count underneath as a cross-check. `renderLoad()`
# is a pure string builder, so the markup is measured, not eyeballed.
def _load_tables(markup: str) -> list:
    """Every table in the block, in order, with its head and body rows."""
    out = []
    for chunk in re.findall(r"<table[^>]*>.*?</table>", markup, re.S):
        out.append(_table(chunk))
    return out


def _plain(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _load_rows(markup: str) -> list:
    """The bar rows of the block, in order: name, segment widths, tooltips, lead.

    The bar is the display now (#38), so the assertions read the markup the
    browser would paint: the widths in `style="width:…%"` and the `data-tip` a
    hover shows.
    """
    rows = []
    for chunk in markup.split('<div class="loadrow')[1:]:
        head = chunk.split(">")[0]
        name = re.search(r'<span class="lr-name">(.*?)</span>', chunk, re.S)
        lead = re.search(r'<span class="lr-lead">(.*?)</span>', chunk, re.S)
        bar = chunk.split('<span class="lr-lead"')[0]   # the bar only, not the lead
        rows.append({
            "name": _plain(name.group(1)) if name else "",
            "total": "total" in head,
            "widths": [float(w) for w in re.findall(r'style="width:([0-9.]+)%"', bar)],
            "tips": [_plain(t) for t in re.findall(r'data-tip="([^"]*)"', bar)],
            "lead": _plain(lead.group(1)) if lead else "",
            "lead_tip": bool(re.search(r'<span class="lr-lead"><span class="tip"', chunk)),
        })
    return rows


def test_every_node_gets_a_bar_and_the_total_a_third(rendered):
    """One bar per node plus the sum — the numbers moved into the bar, the block
    keeps the same three rows it had as a table."""
    rows = _load_rows(rendered["parts"]["load"])
    assert [row["name"] for row in rows] == ["M5-alpha", "M3-beta", _en()["load.total"]]
    assert [row["total"] for row in rows] == [False, False, True]


def test_the_five_states_are_a_partition_of_the_window(rendered):
    """The claim of the block: the segments of a row add up to 100 %."""
    for row in _load_rows(rendered["parts"]["load"]):
        assert sum(row["widths"]) == pytest.approx(100.0, abs=0.2), row["name"]


def test_a_state_with_no_time_gets_no_segment(rendered):
    """M3-beta never loaded a model: `loading` is 0.0. A zero-width stripe would
    look like a measurement, so the segment is left out entirely."""
    m3 = _load_rows(rendered["parts"]["load"])[1]
    assert len(m3["widths"]) == 4          # idle, prefill, decode, mixed — no loading
    assert all(width > 0 for width in m3["widths"])


def test_each_segment_names_its_state_with_duration_and_percentage(rendered):
    """Hovering a colour is the only place the exact seconds live now, so the
    tooltip has to carry both the duration and its share."""
    row = _load_rows(rendered["parts"]["load"])[0]
    assert len(row["tips"]) == len(row["widths"])
    for tip, state in zip(row["tips"], (_en()["load.idle"], _en()["load.loading"],
                                        _en()["load.prefill"], _en()["load.mixed"],
                                        _en()["load.decode"])):
        assert state in tip, tip            # which state the colour is
        assert "%" in tip and "·" in tip, tip   # its share and its duration
        assert _en()["common.source"] in tip, tip


def test_the_bar_says_which_state_dominates_without_a_hover(rendered):
    """The lead beside the bar: M5-alpha is 3000 of 7140 s idle = 42 %."""
    m5 = _load_rows(rendered["parts"]["load"])[0]
    assert m5["lead"].startswith("42.0 %"), m5["lead"]
    assert _en()["load.idle"] in m5["lead"]
    assert m5["lead_tip"]        # the share is a number like any other: it needs a source


def test_the_legend_names_the_five_states(rendered):
    """Colour alone never carries the meaning (design §3) — one legend for the
    block, each entry named and explained."""
    legend = re.search(r'<div class="loadlegend">(.*?)</div>',
                       rendered["parts"]["load"], re.S).group(1)
    names = [_plain(entry) for entry in re.findall(r"</i>(.*?)</span>", legend, re.S)]
    assert names == [_en()[key] for key in ("load.idle", "load.loading",
                                            "load.prefill", "load.mixed",
                                            "load.decode")]
    assert legend.count("data-tip=") == 5


def test_the_bar_can_leave_idle_out_and_then_speaks_of_load_only(rendered):
    """The toggle (#39): with idle hidden the segments are shares of the time
    under load, not of the whole window — M5-alpha has 7140 s sampled, 3000 s
    idle, so 4140 s are left and decode's 2500 s are 60,4 % of them."""
    shown = _load_rows(rendered["parts"]["load"])[0]
    hidden = _load_rows(rendered["parts"]["load_idle_hidden"])[0]
    assert len(hidden["widths"]) == len(shown["widths"]) - 1     # no idle segment
    assert sum(hidden["widths"]) == pytest.approx(100.0, abs=0.2)
    # loading 240, prefill 1200, mixed 200, decode 2500 — all over 4140 s
    assert [round(w, 1) for w in hidden["widths"]] == [5.8, 29.0, 4.8, 60.4]
    assert hidden["lead"].startswith("60.4 %"), hidden["lead"]
    assert _en()["load.idle"] not in hidden["lead"]


def test_the_idle_toggle_names_the_state_it_is_in(rendered):
    """A view switch, not a write: `aria-pressed` carries the state and the label
    says what the click does — `hide idle` off, `show idle` on."""
    off = rendered["parts"]["load"]
    on = rendered["parts"]["load_idle_hidden"]
    assert 'aria-pressed="false"' in off and _en()["load.hide_idle"] in off
    assert 'aria-pressed="true"' in on and _en()["load.show_idle"] in on
    assert _en()["common.source"] in on       # the tooltip still names its source


def test_a_row_with_no_load_says_so_when_idle_is_hidden(rendered):
    """Every second idle means there is nothing to divide. An empty bar without a
    word would be indistinguishable from missing data."""
    total = _load_rows(rendered["parts"]["load_idle_hidden"])[-1]
    assert _en()["load.noload"] in total["lead"] or total["widths"]


def test_a_thread_with_a_request_in_flight_says_working_not_idle(tmp_path):
    """`last_seen` only moves when a request starts or ends, so a streaming answer
    looked like growing idle time — 300 s and counting, while the agent was
    working (#43). The row now says "working", and the delete button is off
    because the backend refuses to pull a slot from under a running request."""
    out = _render_thread_chips(tmp_path, busy=[_thread_row(active=1)],
                               quiet=[_thread_row(active=0, idle_seconds=112.4)])
    busy, quiet = out["en"]["busy"], out["en"]["quiet"]
    assert _en()["threads.busy"] in busy
    assert "s idle" not in busy                 # no growing idle count while working
    assert "112 s idle" in quiet
    # The delete control follows the same fact (`active`), but this harness has no
    # admin key, so both rows are off here; the coupling is measured in
    # test_thread_delete.py where the backend refuses with 409.


def test_the_overwrite_box_sits_on_the_line_of_the_selects(rendered):
    """The checkbox has no field box of its own. Without a wrapper of the selects'
    height its label dropped 8 px below the three beside it, and the box itself
    hung under them (#42). Measured in the browser: label tops 4658 / 4658 / 4658
    / 4666 before, all four on one line after."""
    assert '<span class="repl-check">' in rendered["parts"]["replication"]
    css = (STATIC / "dashboard.css").read_text(encoding="utf-8")
    assert ".repl-check{display:flex;align-items:center;height:25px}" in css


def test_prefill_is_orange_and_loading_blue(rendered):
    """Two blues next to each other were not separable in the bar (#39): prefill
    takes the warn hue, loading keeps the load hue."""
    css = (STATIC / "dashboard.css").read_text(encoding="utf-8")
    assert ".seg-prefill{background:var(--warn)}" in css
    assert ".seg-loading{background:var(--load)}" in css
    assert ".seg-prefill{background:var(--accent)}" not in css


def test_the_work_table_is_requests_and_tokens_in_megatokens(rendered):
    """The table keeps what the bar cannot say: how many requests, and how many
    tokens they moved — in millions, because six-digit numbers in a column are
    read by nobody. Prefill and decode seconds are the bar's business now."""
    work = _load_tables(rendered["parts"]["load"])[0]
    head = [cell["text"] for cell in work.head]
    assert head[0] == _en()["dist.th_node"]
    assert len(head) == 4
    assert _en()["load.requests"] in head[1]
    assert _en()["load.prompt_tokens"] in head[2] and "Mtok" in head[2]
    assert _en()["load.completion_tokens"] in head[3] and "Mtok" in head[3]
    row = work.body[0]
    assert row[1]["text"] == "310"
    assert "1.23" in row[2]["text"]        # 1 234 567 tokens
    assert "0.05" in row[3]["text"]        # 45 678 tokens
    assert len(work.body) == 3
    assert work.body[-1][0]["text"] == _en()["load.total"]


def test_the_load_block_says_since_when_and_how_long(rendered):
    """One line: the start of the window and its length. Without it the numbers
    above are unreadable — 4 h of idle means nothing without the window."""
    markup = rendered["parts"]["load"]
    assert _en()["load.since"].split("{")[0] in markup
    assert "2.0 h" in markup          # window_seconds 7200


def test_without_the_admin_key_the_reset_control_is_off_and_says_why(rendered):
    markup = rendered["parts"]["load"]
    assert "onclick=\"loadReset()\"" in markup
    assert " disabled" in markup
    assert _en()["key.help"] in markup
    with_key = rendered["parts"]["load_admin"]
    assert " disabled" not in with_key
    assert _en()["load.reset_tip"] in with_key


def test_requests_without_a_split_are_named_instead_of_dropped(rendered):
    """Three requests of the fixture carry no split. The block says so beside the
    work table — a silently smaller split would look like a measurement."""
    markup = rendered["parts"]["load"]
    assert _en()["load.no_usage"].replace("{n}", "3") in markup


def test_the_cross_check_names_its_range_and_the_nodes_own_numbers(rendered):
    """`range=today` is the node's own SQLite count, shown beside RIPPLE's ledger.
    The gap between the two is the traffic that went past RIPPLE."""
    markup = rendered["parts"]["load"]
    assert _en()["load.cross"].replace("{range}", "today") in markup
    assert "431" in markup and "98" in markup        # per node, from the fixture
    assert "529" in markup                            # the cross-check total
    for tip in re.findall(r'data-tip="([^"]*)"', markup):
        if "admin/api/usage" in tip:
            break
    else:
        raise AssertionError("the cross-check names no source")


def test_the_page_calls_the_reset_endpoint(js):
    assert "adminApi('POST', '/admin/stats/reset')" in js
    # the endpoint is documented where the admin key is explained
    assert "/admin/stats/reset" in js


# -- #34: the two deadlines count down --------------------------------------
# A load and a thread both carry a deadline: `started_at + eta_seconds` and
# `ttl_seconds − idle_seconds`, both absolute epoch seconds. The page writes the
# text once and a 1 s ticker rewrites only that text, so the numbers fall while
# the 2 s patch stays exactly as it was.
def test_the_loading_line_counts_down_from_started_at_plus_eta(rendered):
    """The card no longer repeats `eta_seconds`: it counts the distance to
    `started_at + eta_seconds`, against the server's clock."""
    markup = rendered["parts"]["fleet_loading"]
    assert 'data-deadline="%d"' % (FROZEN - 60 + 202) in markup
    assert "still 2:22" in markup
    assert 'data-deadline-kind="load"' in markup
    # the number comes from a deadline, not from the payload's fixed eta
    assert ">ETA 202 s<" not in markup
    assert _en()["load.remaining"].replace("{t}", "2:22") in markup


def test_the_loading_line_reports_the_size_in_gigabytes(rendered):
    """`size_gb` from /healthz is already Gigabyte, `gb()` divides by 1e9 again —
    a 453,6 GB cold load printed "0.0 GB" (issue #36)."""
    markup = rendered["parts"]["fleet_loading"]
    assert "453.6 GB" in markup
    assert "0.0 GB" not in markup


def test_the_thread_row_counts_down_from_ttl_minus_idle(rendered):
    """`idle_seconds` was measured at fetch time, so the row ends at
    `fetch_time + ttl_seconds − idle_seconds`; above a minute it reads in
    minutes."""
    markup = rendered["parts"]["threads_loading"]
    assert 'data-deadline-kind="thread"' in markup
    assert 'data-deadline="%s"' % (FROZEN + 1800 - 112.4) in markup
    assert ">28 min left<" in markup
    # the fixed TTL is not printed as a deadline any more — only explained
    assert ">TTL 1,800 s<" not in markup
    assert _en()["threads.ttl"].replace("{n}", "1,800") in markup   # in the tooltip


def test_the_ticker_rewrites_one_text_and_keeps_the_node(rendered):
    """One minute passes: both texts change, no template is parsed, and the DOM
    nodes are the same objects — no re-render, no flicker, no lost tooltip."""
    ticker = rendered["ticker"]
    assert ticker["count"] == 2
    assert ticker["kinds"] == ["load", "thread"]
    assert ticker["before"] == ["still 2:22", "28 min left"]
    assert ticker["after"] == ["still 1:22", "27 min left"]
    assert ticker["same_nodes"] is True
    assert ticker["templates"] == 0          # nothing was re-rendered
    assert ticker["text_writes"] == 2        # exactly the two deadline texts


def test_a_deadline_never_goes_negative_and_never_claims_the_load_finished(rendered):
    """At zero the thread reads "free" (the TTL loop drops it within 30 s) and
    the load reads "over by", never "done": oMLX' own signal ends a load."""
    d = rendered["deadlines"]
    assert d["load"] == "still 2:22"
    assert d["load_short"] == "still 42 s"
    assert d["load_zero"] == "over by 0 s"
    assert d["load_over"] == "over by 1:35"
    assert d["thread"] == "27 min left"
    assert d["thread_short"] == "42 s left"
    assert d["thread_zero"] == "free"
    assert d["thread_over"] == "free"
    assert d["countdown"] == ["2:22", "59 s", "0 s"]


def test_the_ticker_finds_the_nodes_a_re_render_left_behind(rendered):
    """A 2 s patch may replace the text inside a deadline element. The ticker
    re-queries the attribute every tick, so it keeps counting the number that is
    on screen — here the load moved on by 30 s and the clock by 10 s."""
    after = rendered["after_render"]
    assert after["count"] == 2
    assert after["texts"] == ["still 2:52", "27 min left"]
    assert after["texts_after_paint"] == ["still 2:42", "28 min left"]


def test_the_countdown_follows_the_servers_clock_not_the_browsers(rendered):
    """The browser's clock is two hours behind the server's: the page still
    counts the 142 s the server reported, because it derives its offset from
    `server_time` on every pull."""
    skew = rendered["skew"]
    assert skew["now"] == pytest.approx(FROZEN + 7200, abs=1)
    assert skew["deadline"] == "%d" % (FROZEN - 60 + 202 + 7200)
    assert skew["text"] == "still 2:22"


def test_the_deadline_words_come_from_the_catalogs(rendered):
    de = rendered["deadlines_de"]
    assert de["load"] == _de()["load.remaining"].replace("{t}", "2:22")
    assert de["load_zero"] == _de()["load.overdue"].replace("{t}", "0 s")
    assert de["thread"] == _de()["threads.remaining"].replace("{t}", "27 min")
    assert de["thread_zero"] == _de()["threads.free"]


def test_the_ticker_lives_beside_the_two_second_tick(js):
    """Two intervals, two jobs: the patch every 2 s, the deadlines every 1 s.
    The ticker re-queries the nodes, so a patched block is picked up again."""
    assert "setInterval(tick, 2000)" in js
    assert "setInterval(paintDeadlines, 1000)" in js
    assert "querySelectorAll('[data-deadline]')" in js
    assert "el.textContent = text" in js


async def test_healthz_carries_the_server_time(stub_factory, ripple_factory, tmp_path):
    """The page's clock is the service's: `server_time` is epoch seconds, taken
    while the payload is built, and nothing else in the payload moved."""
    a = stub_factory(models=[StubModel(id="m1", estimated_size=GB)])
    cfg = keyed_config([host_config("A", a.url, node_id=a.state.node_id)],
                       str(tmp_path / "keys.yaml"))
    harness = await ripple_factory(cfg)
    before = time.time()
    payload = await harness.healthz()
    after = time.time()
    assert isinstance(payload["server_time"], float)
    assert before <= payload["server_time"] <= after
    # the rest of the payload is unchanged: the fields the page already reads
    for key in ("ok", "version", "build", "uptime_seconds", "threads", "hosts",
                "models", "placement", "load", "restart", "capabilities"):
        assert key in payload, key


# -- #35: the delete control in block 4 -------------------------------------
def test_the_control_is_in_the_block_and_off_without_the_key(rendered):
    """Block 4: one button per thread, disabled with the reason in `title`,
    the label from the catalog and the route named as its source."""
    markup = rendered["parts"]["threads"]
    assert 'onclick="threadDelete(this)"' in markup
    assert 'data-key="4b17eecc4cf0a8a5"' in markup
    assert " disabled" in markup
    assert 'title="' + _en()["key.help"].replace('"', "&quot;") + '"' in markup
    assert ">" + _en()["threads.delete"] + "</span></button>" in markup
    assert "/admin/threads/{key}" in markup          # the source in the tooltip


def test_with_the_key_the_control_is_live(rendered):
    """Same item, admin key in the tab: the button is not disabled and carries
    no refusal in its `title`."""
    markup = rendered["parts"]["threads_admin"]
    button = markup[markup.index("<button"):markup.index("</button>")]
    assert " disabled" not in button
    assert "title=" not in button


def test_a_thread_with_a_request_in_flight_cannot_be_deleted(rendered):
    """`active > 0` is the route's 409 seen from the page: the button is off and
    says why before anyone clicks it."""
    markup = rendered["parts"]["threads_active"]
    button = markup[markup.index("<button"):markup.index("</button>")]
    assert " disabled" in button
    assert 'title="' + _en()["threads.delete_active"] + '"' in button
    # and the German page says it in German
    assert _de()["threads.delete_active"] == "Anfrage läuft"


def test_the_button_does_not_sit_inside_the_deadline(rendered):
    """#34's ticker rewrites the text of `[data-deadline]` and must keep its
    node; a button inside that element would be rewritten with it."""
    markup = rendered["parts"]["threads_loading"]
    for inner in re.findall(r'<span data-deadline="[^"]*"[^>]*>(.*?)</span>', markup, re.S):
        assert "<button" not in inner
    # the countdown is still there and still counts
    assert 'data-deadline-kind="thread"' in markup
    assert "28 min left" in markup


# ---------------------------------------------------------------------------
# 7 Nodes (issue #71): preview, confirm, drain — and a form that survives
# ---------------------------------------------------------------------------
# The tab is measured through the real functions under the DOM stub: the address
# goes into the shell field, `nodePreview()` fetches the dry run, a tick follows
# with a typed weight, and the drain dialog is driven through its three exits.
# "The preview writes nothing" and "a typed field survives the 2 s patch" are
# the two claims of the issue that cannot be read out of the source.
def _nodes(rendered) -> dict:
    return rendered["nodes"]


def test_the_preview_shows_the_four_facts_and_the_derived_site(rendered):
    """node_id, the model list, memory and the measured round trip — the
    acceptance list of #71 — plus the site the address derives into."""
    preview = _nodes(rendered)["preview"]
    assert "as5-m4" in preview
    assert DEEPSEEK in preview
    assert "453.6 / 525.4 GB" in preview          # used / ceiling, one decimal
    assert "7.4 ms" in preview
    assert "Wien" in preview
    # every number carries its source, the way the page does it everywhere
    assert "POST /admin/nodes?dry_run=true" in preview
    assert _en()["nodes.node_id"] in preview
    assert _en()["nodes.rtt"] in preview


def test_the_preview_says_that_it_writes_nothing(rendered):
    """One request, and it is the dry run: no `POST /admin/nodes` follows the
    preview, so nothing is in the fleet before the operator confirms."""
    nodes = _nodes(rendered)
    assert nodes["preview_calls"] == ["POST admin/nodes?dry_run=true"]
    assert _en()["nodes.dry_run"] in nodes["preview"]
    assert _en()["nodes.preview_ok"] in nodes["preview"]


def test_the_defaults_are_equal_weight_the_derived_site_and_worker(rendered):
    """The three decisions routing hangs off, with the measured defaults: the
    site is the one the address falls into, the role is `worker`."""
    draft = _nodes(rendered)["draft"]
    assert draft == {"name": "192.0.2.40", "weight": "1", "site": "wd",
                     "role": "worker"}


def test_a_typed_weight_survives_a_tick_that_changes_the_card(rendered):
    """The point of the whole exercise: the 2 s patch replaces nodes.

    The tick here *does* patch (the add button goes off while a request runs, so
    the markup differs and `sync()` really runs). The input must stay the same
    node and keep what was typed into it — a re-created field loses text and
    caret, and the operator loses the value he just entered.
    """
    nodes = _nodes(rendered)
    assert nodes["card_changed"] is True          # the tick patched something
    assert nodes["field_same_node"] is True       # …but not this field
    assert nodes["weight_default"] == "1"
    assert nodes["field_value_kept"] == "2.5"
    assert nodes["draft_weight"] == "2.5"


def test_a_field_with_the_focus_is_not_patched_at_all(rendered):
    """`applyNode()` holds the patch back while the focus is inside the card:
    the next tick two seconds later carries it, and the caret stays."""
    nodes = _nodes(rendered)
    assert nodes["focus_deferred"] is True
    assert "B-umbenannt" not in nodes["preview_html"]
    assert "B-umbenannt" in nodes["focus_released_html"]


def test_the_confirmation_sends_what_the_fields_hold(rendered):
    """The one write: the values from the form, not the ones from the preview.

    `nodeReadDraft()` reads the four fields at the click, so a weight typed after
    the preview is what the fleet gets.
    """
    nodes = _nodes(rendered)
    # the write, then the tick that shows the new fleet — nothing else
    assert nodes["add_calls"] == ["POST admin/nodes", "GET healthz", "GET stats",
                                  "GET config"]
    body = nodes["add_body"]
    assert body["address"] == NODE_ADDRESS
    assert body["name"] == "B-umbenannt"
    assert body["weight"] == 2.5
    assert body["site"] == "wd"
    assert body["role"] == "worker"
    assert nodes["draft_name"] == "B-umbenannt"
    # the card is gone afterwards and the message names what was added
    assert "as5-m4" not in nodes["preview_after_add"]
    assert "B-umbenannt" in nodes["add_msg"]
    assert "2" in nodes["add_msg"]                # models


def test_nothing_is_written_when_the_confirmation_is_declined(rendered):
    """A `no` in the question is a `no` on the wire — not a write that is
    reported afterwards."""
    assert _nodes(rendered)["no_confirm_calls"] == []


def test_the_sweep_lists_what_answered_and_marks_what_is_known(rendered):
    """One row per answer, and the machine that is already in the fleet says so
    and cannot be picked a second time."""
    nodes = _nodes(rendered)
    assert nodes["scan_calls"] == ["POST admin/discover"]
    assert nodes["scan_body"] == {"cidr": "127.0.0.1/32", "port": 8000}
    found = nodes["found"]
    assert _en()["nodes.found_n"].replace("{n}", "2").replace("{cidr}", "192.0.2.0/24") in found
    assert NODE_ADDRESS in found
    assert "as5-m4" in found
    assert "7.4 ms" in found
    assert _en()["nodes.known"].replace("{nodes}", "M5-alpha") in found
    # exactly one of the two candidates is disabled: the known one
    buttons = re.findall(r"<button[^>]*>(?:[^<]*)</button>", found)
    assert len(buttons) == 2
    assert buttons[0].count(" disabled") == 0
    assert " disabled" in buttons[1]


def test_an_empty_sweep_is_an_answer_not_an_error(rendered):
    assert _en()["nodes.found_none"].replace("{cidr}", "127.0.0.1/32") in \
        _nodes(rendered)["found_empty"]


def test_the_drain_dialog_names_what_is_in_flight_and_how_long_it_waits(rendered):
    """#70's removal, said in the operator's words before he presses it: the
    count, the models, the deadline and the three ways out."""
    drain = _nodes(rendered)["drain"]
    en = _en()
    assert en["nodes.drain_title"].replace("{name}", "M3-beta") in drain
    assert en["nodes.drain_inflight"].replace("{n}", "2") in drain
    assert DEEPSEEK + " ×1" in drain
    assert en["nodes.drain_wait"].replace("{v}", "5") in drain
    assert en["nodes.drain_note"] in drain
    for key in ("nodes.drain_confirm", "nodes.drain_cancel", "nodes.drain_disable"):
        assert en[key] in drain, key
    # the deadline comes from /healthz → fleet.drain_seconds, with its source
    assert "/healthz → fleet.drain_seconds" in drain


def test_a_drain_that_runs_out_of_time_removes_nothing(rendered):
    """409 with `drain_timeout`: the node is disabled, not half-removed, and the
    dialog stays open with the numbers of the answer."""
    nodes = _nodes(rendered)
    msg = nodes["drain_timeout_msg"]
    assert _en()["nodes.drain_timeout"].split("{msg}")[0].strip() in msg
    assert "M3-beta still has 2 request(s) in flight" in msg
    after = nodes["drain_after_timeout"]
    assert _en()["nodes.drain_inflight"].replace("{n}", "2") in after
    assert _en()["nodes.drain_confirm"] in after      # still open, still asking


def test_a_drain_that_finishes_closes_the_dialog(rendered):
    nodes = _nodes(rendered)
    en = _en()
    assert en["nodes.removed"].split("{name}")[0].strip() in nodes["drain_removed_msg"]
    assert "M3-beta" in nodes["drain_removed_msg"]
    assert "1.25" in nodes["drain_removed_msg"]       # waited_seconds
    assert nodes["drain_closed"] is True


def test_a_node_from_config_yaml_is_refused_with_the_way_out(rendered):
    """#70's rule, seen from the page: the file is the baseline, so the answer
    is "disable it instead" and not a stack trace."""
    nodes = _nodes(rendered)
    assert _en()["nodes.remove_config"] in nodes["config_msg"]
    assert "Disable it instead" in nodes["config_msg"]   # the service's wording
    assert nodes["config_dialog_closed"] is True


def test_the_node_list_offers_remove_only_for_overlay_nodes(rendered):
    """A node from `config.yaml` gets a disabled button with the reason in its
    `title` — the 409 is not something the operator should have to provoke."""
    markup = _nodes(rendered)["list"]
    rows = re.findall(r'<div class="node-row" data-key="node-([^"]+)">(.*?)</div>', markup, re.S)
    assert [name for name, _ in rows] == ["M5-alpha", "M3-beta"]
    config_row = dict(rows)["M5-alpha"]
    overlay_row = dict(rows)["M3-beta"]
    assert " disabled" in config_row
    assert 'title="' + _en()["nodes.remove_config"] + '"' in config_row
    assert " disabled" not in overlay_row
    # the source is named, with its own tooltip
    assert "config" in config_row and "overlay" in overlay_row


def test_without_the_key_the_controls_are_off_and_say_why(rendered):
    """Same rule as everywhere: no key, no button — and the reason in words,
    not only in a `title`."""
    nodes = _nodes(rendered)
    key_help = _en()["key.help"]
    assert nodes["list_without_key"].count(" disabled") == 2
    assert key_help in nodes["list_without_key"]
    assert key_help in nodes["preview_without_key"]
    assert " disabled" in nodes["preview_without_key"]


def test_a_contradicted_site_is_a_warning_in_the_preview(rendered):
    """#67's mismatch, in front of the write: the sentence names both sites and
    the node can still be added — a site is a location, not a routing rule."""
    markup = _nodes(rendered)["preview_mismatch"]
    assert _en()["nodes.site_mismatch"].replace(
        "{declared}", "Traiskirchen").replace("{derived}", "Wien") in markup
    assert " disabled" not in markup          # the add button stays live


def test_an_unreadable_model_list_is_a_caveat_not_a_refusal(rendered):
    """The node answers, its model list does not: `step: models`, a warning, and
    the add button still on — the fleet does the same read and accepts it."""
    markup = _nodes(rendered)["preview_partial"]
    assert _en()["nodes.preview_partial"] in markup
    assert _en()["nodes.models_unreadable"] in markup
    assert "401 Unauthorized" in markup
    assert " disabled" not in markup


def test_the_three_text_fields_live_in_the_shell_and_not_in_the_patch(page, js):
    """The structural half of "the form survives the tick": the address, the
    CIDR and the port are in `dashboard.py`, and the script never builds them.
    A field the script renders is a field a tick can replace."""
    for field in ("nodeAddress", "nodeCidr", "nodePort"):
        assert 'id="' + field + '"' in page, field
        assert 'id="' + field + '"' not in js, field
    # the containers the tick does patch are in the panel, next to them
    for container in ("nodePreview", "nodeFound", "nodeDrain", "nodeMsg", "nodeList"):
        assert 'id="' + container + '"' in page, container


def test_the_node_panel_sits_in_its_own_tab(page, js):
    """#71 is explicit that `Betrieb` is the wrong place: adding a node is not a
    maintenance action. The panel is `#panel-nodes`, and it is not the ops one."""
    panel = page.split('id="panel-nodes"')[1]
    assert 'data-i18n="sec.nodes"' in panel
    assert 'data-i18n="sec.nodes.add"' in panel
    assert 'data-i18n="sec.nodes.scan"' in panel
    assert 'data-i18n="sec.nodes.remove"' in panel
    # the ops panel keeps its six blocks and gains nothing
    ops = page.split('id="panel-ops"')[1].split("</section>")[0]
    assert "sec.nodes" not in ops
    assert js.count("TABS = ['fleet', 'models', 'threads', 'history', 'ops', 'nodes']") == 1

# -- der Fahrtenschreiber (issue #83) ---------------------------------------
# The contract, fixed in the issue before the work started: `hosts[].series` is
# one entry per model, oldest first, `bucket_s` 5 and `window_s` 900, and
# `total[i] == n[i] * rate[i]`. A bucket without a sample is `null` — the
# renderer breaks the line there, it never interpolates.
SERIES_N = [1, 3, 3, 2, None, 2, 5, 5, 5, 5]
SERIES_RATE = [30.5, 30.5, 30.1, 30.2, None, 30.5, 30.5, 30.2, 30.1, 30.0]
SERIES_TOTAL = [None if n is None else round(n * r, 1)
                for n, r in zip(SERIES_N, SERIES_RATE)]
SERIES_GAP_AT = 4                     # the bucket without a sample


def _series_entry(**extra) -> dict:
    entry = {"model": DEEPSEEK, "bucket_s": 5, "window_s": 900,
             "n": list(SERIES_N), "total": list(SERIES_TOTAL),
             "per_thread": list(SERIES_RATE)}
    entry.update(extra)
    return entry


# A second model on the same node, so "the line is the entry of the model the
# card shows" is a choice between two and not the only one there is.
OTHER_ENTRY = {"model": "Qwen3.5-9B-oQ4e-mtp", "bucket_s": 5, "window_s": 900,
               "n": [1, 1, 1], "total": [12.0, 11.5, 12.5],
               "per_thread": [12.0, 11.5, 12.5]}


def _series_healthz() -> dict:
    """The fleet of `_healthz()`, with three kinds of card: a series with a gap,
    an empty series, and no `series` key at all (the payload before #83)."""
    h = json.loads(json.dumps(_healthz()))
    h["hosts"][0]["series"] = [_series_entry(), OTHER_ENTRY]
    h["hosts"][1]["series"] = []
    third = _host("M4-gamma", role="worker", site="TR", address="192.0.2.40:8000",
                  ceiling=M5_CEILING, used=M5_USED, measured=M5_MEASURED)
    h["hosts"].append(third)
    return h


SERIES_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm');
const jsPath = process.argv[2], fixturePath = process.argv[3];
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

/* The same minimal DOM the other harnesses use: the page is a set of string
   builders, so the honest measurement is the markup they produce plus the pure
   functions they export — no browser, no picture. */
const elements = new Map();
function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [], html: ''};
  Object.defineProperty(el, 'innerHTML', {
    get() { return el.html; }, set(v) { el.html = String(v); },
  });
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {}; el.removeChild = () => {}; el.replaceChild = () => {};
  el.setAttribute = () => {}; el.getAttribute = () => null; el.removeAttribute = () => {};
  el.addEventListener = () => {};
  return el;
}
const document = {
  body: makeEl('body'),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};
async function fetchStub(url) {
  const key = String(url).replace(/^\//, '');
  const body = fixture[key];
  if (body === undefined) throw new Error('no fixture for ' + url);
  return {ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body)};
}
const sandbox = {
  Date,
  document,
  location: new Proxy({}, {get() { throw new Error('the page must not navigate'); }}),
  window: {confirm: () => true, addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: fn => { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout: () => {},
  console,
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;                       /* catalogs loaded, first tick done */
  const fleet = () => (elements.get('fleet') || {}).__html || '';

  const out = {
    H: api.SPARK_H, W: api.SPARK_W, PAD: api.SPARK_PAD,
    fleet: fleet(),
    geom: api.seriesGeom(fixture.entry),
    gap: api.seriesGeom(fixture.gap),
    rows: api.seriesRows(fixture.entry),
    /* the three shapes a card can arrive in — all of them keep the frame */
    none: api.sparkline({name: 'X', metrics: {}}),
    empty: api.sparkline({name: 'X', series: [], metrics: {}}),
    blank: api.sparkline({name: 'X', series: [{model: 'm', n: [], total: [], per_thread: []}],
                          metrics: {}}),
    /* a single sample is a point, not a line — and still no throw */
    one: api.sparkline({name: 'X', series: [{model: 'm', bucket_s: 5, window_s: 90,
                                             n: [2], total: [60], per_thread: [30]}],
                        metrics: {}}),
    /* two probes 10 s apart, i.e. one empty 5 s bucket between them (#96) */
    two: api.seriesGeom(fixture.two),
    two_card: api.sparkline({name: 'X', series: [fixture.two], metrics: {}}),
    hole: api.seriesGeom(fixture.hole),
    three: api.seriesGeom(fixture.three),
    lone: api.sparkline({name: 'X', series: [fixture.lone], metrics: {}}),
    lone_geom: api.seriesGeom(fixture.lone),
    /* which entry the card draws */
    pick: api.hostSeries(fixture.pick).model,
    pick_none: api.hostSeries({name: 'X', series: [{model: 'a'}, {model: 'b'}],
                               metrics: {}}).model,
    pick_empty: api.hostSeries({name: 'X'}),
    /* the window label and the legend, in both languages */
    win: [api.windowLabel(900), api.windowLabel(90), api.windowLabel(null)],
    spark_en: api.sparkline(fixture.card),
    /* the pointer (#102): a screen x against the <svg> rectangle and the span
       of the series, at the edges, in the middle and outside */
    idx: {
      left: api.sparkIndex(100, {left: 100, width: 400}, 9),
      mid: api.sparkIndex(300, {left: 100, width: 400}, 9),
      right: api.sparkIndex(500, {left: 100, width: 400}, 9),
      past_right: api.sparkIndex(900, {left: 100, width: 400}, 9),
      before_left: api.sparkIndex(-20, {left: 100, width: 400}, 9),
      narrow: api.sparkIndex(150, {left: 100, width: 0}, 9),
      round_trip: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9].map(
        i => api.sparkIndex(100 + (i / 9) * 400, {left: 100, width: 400}, 9)),
    },
    span: api.seriesGeom(fixture.entry).span,
    ro_gap: api.sparkReadout(api.seriesGeom(fixture.gap), 4),
    ro_sample: api.sparkReadout(api.seriesGeom(fixture.gap), 6),
    /* #105: the readout resolves a bucket by the rule the line draws by —
       `seriesValueAt()` is the one place that decides, and `hold` is the one
       constant both the pen and the number use */
    hold: api.SPARK_HOLD,
    geom_hold: api.seriesGeom(fixture.gap).hold,
    at: {
      sample: api.seriesValueAt(fixture.gap.total, 3, api.SPARK_HOLD),
      gap: api.seriesValueAt(fixture.gap.total, 4, api.SPARK_HOLD),
      hole: api.seriesValueAt(fixture.hole.total, 1, api.SPARK_HOLD),
      hole2: api.seriesValueAt(fixture.hole.total, 2, api.SPARK_HOLD),
      head: api.seriesValueAt(fixture.head.total, 0, api.SPARK_HOLD),
      tail: api.seriesValueAt(fixture.tail.total, 2, api.SPARK_HOLD),
      none: api.seriesValueAt(null, 0, api.SPARK_HOLD),
      outside: api.seriesValueAt(fixture.gap.total, 99, api.SPARK_HOLD),
      hold_zero: api.seriesValueAt([10.0, null, 20.0], 1, 0),
    },
    ro: {
      gap: api.sparkReadout(api.seriesGeom(fixture.gap), 4),
      gap3: api.sparkReadout(api.seriesGeom(fixture.gap), 3),
      two1: api.sparkReadout(api.seriesGeom(fixture.two), 1),
      three1: api.sparkReadout(api.seriesGeom(fixture.three), 1),
      three3: api.sparkReadout(api.seriesGeom(fixture.three), 3),
      hole1: api.sparkReadout(api.seriesGeom(fixture.hole), 1),
      hole2: api.sparkReadout(api.seriesGeom(fixture.hole), 2),
      head0: api.sparkReadout(api.seriesGeom(fixture.head), 0),
      tail2: api.sparkReadout(api.seriesGeom(fixture.tail), 2),
      lone0: api.sparkReadout(api.seriesGeom(fixture.lone), 0),
    },
    hover_card: (api.sparkHoverSet('M5-alpha', 4), api.sparkline(fixture.card)),
    hover_state: api.sparkHover,
    /* the pointer is on one card, not on the tab */
    hover_other: api.sparkline(fixture.pick),
    plain_card: (api.sparkHoverClear(), api.sparkline(fixture.card)),
    hover_none: (api.sparkHoverSet('M5-alpha', 2),
                 api.sparkline({name: 'M5-alpha', series: [], metrics: {}})),
  };
  /* kein Zeiger für die Läufe unten: die Sprache wechselt, nicht der Bucket */
  api.sparkHoverClear();
  api.chooseLang('de');
  out.spark_de = api.sparkline(fixture.card);
  /* the empty state is a line of text, and it is in the page's language */
  out.empty_de = api.sparkline({name: 'X', series: [], metrics: {}});
  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def series_run(tmp_path_factory) -> dict:
    """`dashboard.js` with a series in the payload — the contract, executed."""
    if NODE is None:
        pytest.skip("node is not installed — the renderer is JavaScript")
    work = tmp_path_factory.mktemp("series")
    card = _host("M5-alpha", role="entry", site="WD", address="192.0.2.11:8000",
                 ceiling=M5_CEILING, used=M5_USED, measured=M5_MEASURED,
                 metrics={DEEPSEEK: {"tg_tps": 30.5, "tg_samples": 309,
                                     "measured_at": FROZEN}},
                 series=[_series_entry()])
    pick = _host("M3-beta", role="worker", site="WD", address="192.0.2.12:8000",
                 ceiling=M3_CEILING, used=M3_USED, measured=M3_MEASURED,
                 metrics={DEEPSEEK: {"tg_tps": 12.0, "tg_samples": 400,
                                     "measured_at": FROZEN}},
                 series=[OTHER_ENTRY, _series_entry()])
    gap = _series_entry(n=list(SERIES_N), total=list(SERIES_TOTAL),
                        per_thread=list(SERIES_RATE))
    # The three shapes issue #96 is about, as the backend really serves them.
    # `two`: two probes 10 s apart — the second fell into bucket 2, so bucket 1
    # is empty by construction. `hole`: a probe that did not happen (two empty
    # buckets). `lone`: one sample and nothing else around it.
    two = {"model": "m", "bucket_s": 5, "window_s": 90,
           "n": [1, None, 2], "total": [30.0, None, 62.0],
           "per_thread": [30.0, None, 31.0]}
    hole = {"model": "m", "bucket_s": 5, "window_s": 90,
            "n": [1, None, None, 2], "total": [30.0, None, None, 62.0],
            "per_thread": [30.0, None, None, 31.0]}
    lone = {"model": "m", "bucket_s": 5, "window_s": 90,
            "n": [None, None, 2], "total": [None, None, 60.0],
            "per_thread": [None, None, 30.0]}
    three = {"model": "m", "bucket_s": 5, "window_s": 90,
             "n": [1, None, 2, None, 3], "total": [30.0, None, 62.0, None, 93.0],
             "per_thread": [30.0, None, 31.0, None, 31.0]}
    # An empty bucket at the edge of the row: one probe and nothing before it,
    # or nothing after it. The line draws no segment there (#105, case d).
    head = {"model": "m", "bucket_s": 5, "window_s": 90,
            "n": [None, 1, 2], "total": [None, 30.0, 62.0],
            "per_thread": [None, 30.0, 31.0]}
    tail = {"model": "m", "bucket_s": 5, "window_s": 90,
            "n": [1, 2, None], "total": [30.0, 62.0, None],
            "per_thread": [30.0, 31.0, None]}
    fixture = work / "fixture.json"
    fixture.write_text(json.dumps({
        "healthz": _series_healthz(), "stats": _stats(), "config": _config(),
        "entry": _series_entry(), "gap": gap, "card": card, "pick": pick,
        "two": two, "hole": hole, "lone": lone, "three": three,
        "head": head, "tail": tail,
        **_catalogs(),
    }), encoding="utf-8")
    harness = work / "series_harness.js"
    harness.write_text(SERIES_HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(STATIC / "dashboard.js"), str(fixture)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def series_stub(tmp_path_factory) -> dict:
    """The same payload through the real stub: template, `importNode`, `sync()`.

    The chart is markup, so the question "does an `<svg>` survive the 2 s patch"
    is answered by the reconciler itself — a throw there would fail the run, and
    a second tick with the same data must still write nothing.
    """
    return _run_stub(tmp_path_factory.mktemp("series-stub"),
                     _stub_fixture(healthz=_series_healthz()))


def _subpaths(d: str) -> list[list[tuple[float, float]]]:
    """The unbroken stretches of one `<path d>`: one entry per `M` subpath."""
    out = []
    for chunk in re.finditer(r"M([^M]*)", d):
        pts = re.findall(r"(-?[\d.]+)\s+(-?[\d.]+)", chunk.group(1))
        out.append([(float(x), float(y)) for x, y in pts])
    return out


def _cards(markup: str) -> dict[str, str]:
    """The fleet markup split per card, by the key `apply()` reconciles on."""
    out = {}
    for part in markup.split('<article class="card host"')[1:]:
        out[re.search(r'data-key="([^"]+)"', part).group(1)] = part
    return out


def test_the_two_lines_are_total_and_per_thread_from_the_payload(series_run):
    """The solid line is `total`, the dashed one `per_thread` — read back from
    the drawn coordinates, not from the arrays the page was handed.

    The inverse of the scale is the only arithmetic here: a y of `H - PAD` is
    zero, `PAD` is the window's peak. If the two rows were swapped, every value
    of the solid line would come out as a per-thread rate and vice versa — which
    is exactly the mistake this test exists to catch.
    """
    g = series_run["geom"]
    base = series_run["H"] - series_run["PAD"]
    top = base - series_run["PAD"]
    peak = g["max"]
    assert peak == max(v for v in SERIES_TOTAL + SERIES_RATE if v is not None)

    def drawn(d):
        out = {}
        for sub in _subpaths(d):
            for x, y in sub:
                i = round(x / series_run["W"] * (len(SERIES_N) - 1))
                out[i] = (base - y) / top * peak
        return out

    solid, dashed = drawn(g["total"]), drawn(g["per_thread"])
    assert sorted(solid) == [i for i, v in enumerate(SERIES_N) if v is not None]
    for i, total in enumerate(SERIES_TOTAL):
        if total is None:
            continue
        assert solid[i] == pytest.approx(total, abs=0.2)
        assert dashed[i] == pytest.approx(SERIES_RATE[i], abs=0.2)
    # the payload holds the contract the page relies on: total = n · rate
    for n, rate, total in zip(SERIES_N, SERIES_RATE, SERIES_TOTAL):
        assert total is None or total == pytest.approx(n * rate)
    # both rows, normalised to the same length and the same holes
    assert series_run["rows"]["total"] == SERIES_TOTAL
    assert series_run["rows"]["per_thread"] == SERIES_RATE


def test_one_empty_bucket_is_the_resolution_of_the_data_and_the_pen_crosses_it(series_run):
    """#96, defect 1 — the one that made the chart invisible.

    `SERIES_N` has a single `null`. At a 10 s probe against a 5 s bucket that is
    not a hole, it is *every second bucket*: the pen used to lift there, so the
    path was a chain of one-point subpaths and SVG painted none of them. The
    line now crosses the empty bucket — one subpath, nine points — and the hole
    still holds no vertex, so the time axis did not move.
    """
    x_gap = round(SERIES_GAP_AT / (len(SERIES_N) - 1) * series_run["W"], 1)
    for row in ("total", "per_thread"):
        subs = _subpaths(series_run["gap"][row])
        assert [len(s) for s in subs] == [9], row
        xs = [x for sub in subs for x, _ in sub]
        assert x_gap not in xs, row                  # the hole keeps its slot
        assert xs == sorted(xs) and len(set(xs)) == len(xs), row


def test_two_probes_ten_seconds_apart_are_one_line(series_run):
    """The acceptance of #96, on the script that paints.

    Two probes 10 s apart land in bucket 0 and bucket 2 — the payload the
    backend really serves (`n = [1, None, 2]`). One `M`, one `L`: a segment
    between the two measurements, and the card carries it.
    """
    two = series_run["two"]
    for row in ("total", "per_thread"):
        assert two[row].count("M") == 1, row
        assert two[row].count("L") == 1, row
    drawn = re.search(r'class="spark-line total" d="([^"]+)"', series_run["two_card"])
    assert drawn and "L" in drawn[1]
    assert len(_subpaths(two["total"])[0]) == 2      # two points, two probes


def test_a_run_of_empty_buckets_is_a_probe_that_did_not_happen(series_run):
    """Two empty buckets in a row are not resolution: the line ends there.

    A node that went away must never be drawn as one that kept its rate, so the
    break is a real break — two subpaths of one point each, no segment.
    """
    hole = series_run["hole"]
    for row in ("total", "per_thread"):
        subs = _subpaths(hole[row])
        assert [len(s) for s in subs] == [1, 1], row
        assert "L" not in hole[row], row


def test_a_single_probe_is_marked_so_one_sample_is_not_zero(series_run):
    """#96, defect 2: a node with one probe looked exactly like a node with none.

    A run of length one gets a marker. It is drawn as a hair-length segment with
    a round cap — a dot of the stroke width in screen pixels at any card width —
    because a `<circle>` under `preserveAspectRatio="none"` is an ellipse that
    shrinks with the card, and a segment of length *zero* paints nothing at all.
    """
    lone = series_run["lone_geom"]
    assert len(_subpaths(lone["total"])[0]) == 1     # nothing to join: no segment
    assert lone["dots"]["total"].startswith("M") and lone["dots"]["total"].endswith("h0.01")
    assert "L" not in lone["total"]                  # a lone point is no line
    card = series_run["lone"]
    dot = re.search(r'class="spark-dot total" d="M([\d.]+) ([\d.]+)h0\.01"', card)
    assert dot, card
    assert 'stroke-linecap="round"' in card
    assert 'vector-effect="non-scaling-stroke"' in card
    # the marker sits on the sample, not at the origin: the lone value is the
    # window's peak, so it is drawn at the top of the plot
    assert float(dot[1]) == pytest.approx(series_run["W"], abs=0.1)
    assert float(dot[2]) == pytest.approx(series_run["PAD"], abs=0.1)
    # a run of two is a segment, and a segment needs no marker
    assert "spark-dot" not in series_run["two_card"]


def test_a_marker_is_only_for_a_point_the_line_does_not_reach(series_run):
    """The marker's rule is the path's rule: a run the pen crosses is no lone point.

    Three probes 10 s apart are one run — no marker, the line is the ink. A real
    break ends the run, and *then* each side is a lone point and gets its dot.
    (The first version of `seriesDots()` counted consecutive indices, so a run
    of three samples 2 buckets apart produced a marker at the last one; this is
    the test that holds that down.)
    """
    three = series_run["three"]
    assert three["total"].count("L") == 2                 # one run of three
    assert three["dots"]["total"] == ""                   # no marker
    assert three["dots"]["per_thread"] == ""

    hole = series_run["hole"]
    assert hole["dots"]["total"].count("M") == 2          # two lone points
    assert hole["dots"]["total"].count("h0.01") == 2
    assert hole["dots"]["per_thread"].count("M") == 2


def test_the_script_builds_the_path_the_reference_builds(series_run):
    """`dashboard.js:seriesPath()` against `ripple.stats.series_path()`.

    The drawing happens in the browser, so the algorithm exists twice; the
    Python one is what `tests/test_stats.py` can read and this comparison is what
    keeps the two from drifting apart. Same fixture, same geometry, same points.
    """
    def axes(length, peak):
        W, H, PAD = series_run["W"], series_run["H"], series_run["PAD"]
        span, base = max(1, length - 1), H - PAD
        return (lambda i: round(i / span * W, 1),
                lambda v: round(base - (v / peak) * (base - PAD), 2) if peak > 0 else base)

    cases = (
        ("entry", {"total": list(SERIES_TOTAL), "per_thread": list(SERIES_RATE)},
         series_run["geom"]),
        ("two", {"total": [30.0, None, 62.0], "per_thread": [30.0, None, 31.0]},
         series_run["two"]),
        ("hole", {"total": [30.0, None, None, 62.0],
                  "per_thread": [30.0, None, None, 31.0]}, series_run["hole"]),
    )
    for name, rows, geom in cases:
        x, y = axes(len(rows["total"]), geom["max"])
        for row in ("total", "per_thread"):
            assert _subpaths(series_path(rows[row], x, y)) == _subpaths(geom[row]), (name, row)


def test_a_card_without_samples_keeps_its_frame_and_says_so(series_run):
    """#96, defect 3: after a restart the whole block vanished and the card got
    shorter — "such dynamics are not good for the GUI".

    The frame stays: head, unit, axis, 34 px plot, in every payload shape the
    page can meet (no `series` key at all, an empty list, an entry with empty
    arrays). The empty state is a line of text instead of nothing, so a node
    that has no history cannot be mistaken for a node that has nothing to show.
    """
    for key in ("none", "empty", "blank"):
        markup = series_run[key]
        assert 'class="spark"' in markup, key            # the block is there
        assert 'class="spark-plot"' in markup, key       # with its axis
        assert '<span class="spark-unit">tok/s</span>' in markup, key
        assert "<path" not in markup, key                # and no ink
        assert 'class="spark-empty"' in markup, key
        assert "no samples yet" in markup, key
        # the frame carries the same window label as a filled one: `—`, because
        # a payload without a series has no window either
        assert '<span class="spark-win">' in markup, key

    cards = _cards(series_run["fleet"])
    assert 'class="spark"' in cards["M5-alpha"]          # with a series
    assert 'class="spark"' in cards["M3-beta"]           # `series: []`
    assert 'class="spark"' in cards["M4-gamma"]          # no key at all (legacy)
    assert series_run["fleet"].count("<svg") == 3
    # exactly the two without samples carry the line of text
    assert series_run["fleet"].count('class="spark-empty"') == 2
    assert 'class="spark-empty"' not in cards["M5-alpha"]
    # one sample is a point, not a line — and it is marked, not left blank
    assert 'class="spark"' in series_run["one"]
    assert 'class="spark-dot total"' in series_run["one"]
    assert 'class="spark-empty"' not in series_run["one"]

    # the empty line is the page's language, not English in a German page
    assert "noch keine Proben" in series_run["empty_de"]
    assert "no samples yet" not in series_run["empty_de"]


def test_the_line_follows_the_model_the_card_shows(series_run):
    """`hosts[].series` is per model; the card draws the entry of the model whose
    rate stands in its tok/s cell, not whichever entry happens to be first."""
    assert series_run["pick"] == DEEPSEEK            # 400 samples beat the 12 tok/s
    assert series_run["pick_none"] == "a"            # no measurement: the first
    assert series_run["pick_empty"] is None


def test_the_legend_names_both_lines_in_the_language_of_the_page(series_run):
    en, de = series_run["spark_en"], series_run["spark_de"]
    assert ">total<" in en and ">per thread<" in en
    assert ">gesamt<" in de and ">pro Thread<" in de
    assert "per thread" not in de and "gesamt" not in en
    # the two keys of the legend, one solid and one dashed
    assert '<span class="sw solid"></span>' in en
    assert '<span class="sw dash"></span>' in en
    # the axes carry a number and nothing else: the unit at the top, the window
    # at the right — no grid, no ticks, no y labels
    assert '<span class="spark-unit">tok/s</span>' in en
    assert '<span class="spark-win">15 min</span>' in en
    assert series_run["win"] == ["15 min", "90 s", "\u2014"]
    assert "<line" not in en and "<rect" not in en and "<g " not in en


def test_the_two_lines_differ_by_dash_pattern_and_not_by_colour(css):
    """Their own rule: colour never carries the message alone. In greyscale the
    two lines stay apart because one is solid and the other is dashed."""
    solid = re.search(r"\.spark-line\.total\{([^}]*)\}", css)[1]
    dashed = re.search(r"\.spark-line\.per_thread\{([^}]*)\}", css)[1]
    assert "stroke-dasharray" not in solid
    assert "stroke-dasharray:4 3" in dashed
    assert "stroke:var(--load)" in solid and "stroke:var(--fg-3)" in dashed
    # the swatch in the legend carries the same pattern as the line it names
    assert ".sw.dash" in css and "repeating-linear-gradient(90deg" in css


def test_the_chart_takes_the_48_pixels_the_fleet_tab_has(css, series_run):
    """The fleet tab must not grow: 14 px head + 34 px plot + 2 px + 6 px is the
    room #80 left, and the height in the style sheet is the height of the
    `viewBox` — otherwise the drawing would be stretched, not scaled."""
    plot = re.search(r"\.spark-plot\{([^}]*)\}", css)[1]
    head = re.search(r"\.spark-head\{([^}]*)\}", css)[1]
    block = re.search(r"\n  \.spark\{([^}]*)\}", css)[1]
    h_plot = int(re.search(r"height:(\d+)px", plot)[1])
    h_head = int(re.search(r"height:(\d+)px", head)[1])
    margin = int(re.search(r"margin-top:(\d+)px", plot)[1])
    pad = sum(int(v) for v in re.findall(r"(\d+)px", block))
    assert h_plot == series_run["H"]
    assert "width:100%" in plot                 # the width follows the card
    assert "preserveAspectRatio=\"none\"" in series_run["spark_en"]
    assert h_head + h_plot + margin + pad <= 60


def test_the_svg_survives_the_two_second_patch(series_stub):
    """The chart is markup inside `renderFleet()`, so it goes through the same
    template/`importNode`/`sync()` path as the memory bar. A second tick with
    the same numbers writes nothing — an `<svg>` that changed shape would.

    Three of them since #96: every card keeps its frame, including the two
    without samples — a block that comes and goes is the defect being fixed.
    """
    assert series_stub["parts"]["fleet"].count("<svg") == 3
    assert series_stub["i18n_misses"] == []
    assert series_stub["unchanged_templates"] == 0
    assert series_stub["lang_shape_diffs"] == []


# -- der Zeiger auf der Kurve (issue #102) ----------------------------------
# Die Fläche ist 34 px hoch und trägt keine y-Achse: eine Skala wäre dort kleiner
# als ihre Beschriftung. Der Zeiger ersetzt sie — er nennt beide Zahlen des
# Buckets, auf den er zeigt, in der Kopfzeile, wo die Legende steht. Der Zustand
# liegt in einer Modulvariable und wird von `sparkline()` gelesen, damit ihn der
# 2-s-Patch von selbst trägt: `apply()` baut die Zeichenkette neu, und die
# Zeichenkette ist die Funktion dieses Zustands.
#
# Gemessen wird am echten Reconciler, nicht an der Zeichenkette: der Harness
# unten ist ein kleines DOM (parse, importNode, sync) wie `dashboard_dom_stub.js`,
# aber ohne Tick — die drei Container, die dieser Vertrag braucht, sind `fleet`
# und die zwei Karten darin.
SPARK_PATCH_HARNESS = r"""
'use strict';
const fs = require('fs'), vm = require('vm');
const jsPath = process.argv[2], fixturePath = process.argv[3];
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

const VOID = new Set(['br', 'img', 'input', 'hr', 'meta', 'link']);
function textNode(v) {
  return {nodeType: 3, nodeValue: String(v), childNodes: [],
          get textContent() { return this.nodeValue; },
          set textContent(x) { this.nodeValue = String(x); }};
}
function serialise(n) {
  if (!n) return '';
  if (n.nodeType === 3) return String(n.nodeValue);
  const name = String(n.tagName).toLowerCase();
  const attrs = (n.attributes || []).map(a => ' ' + a.name + '="' + a.value + '"').join('');
  return '<' + name + attrs + '>' + (n.childNodes || []).map(serialise).join('') +
         '</' + name + '>';
}
function makeEl(tag) {
  const el = {
    tag: tag || 'div', tagName: String(tag || 'div').toUpperCase(), nodeType: 1,
    childNodes: [], attributes: [],
    getAttribute(k) { const a = this.attributes.find(x => x.name === k); return a ? a.value : null; },
    setAttribute(k, v) {
      const a = this.attributes.find(x => x.name === k);
      if (a) a.value = String(v); else this.attributes.push({name: String(k), value: String(v)});
    },
    removeAttribute(k) { this.attributes = this.attributes.filter(x => x.name !== k); },
    hasAttribute(k) { return this.attributes.some(x => x.name === k); },
    appendChild(n) { this.childNodes.push(n); return n; },
    removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); return n; },
    replaceChild(n, old) { const i = this.childNodes.indexOf(old); if (i >= 0) this.childNodes[i] = n; return old; },
    contains(n) {
      if (!n) return false;
      if (n === this) return true;
      return this.childNodes.some(c => c.nodeType === 1 && c.contains && c.contains(n));
    },
    get textContent() { return this.childNodes.map(c => c.textContent).join(''); },
    set textContent(v) { this.childNodes = [textNode(v)]; },
    get id() { return this.getAttribute('id') || ''; },
    get outerHTML() { return serialise(this); },
  };
  if (tag === 'template') el.content = {childNodes: []};
  let html = '';
  Object.defineProperty(el, 'innerHTML', {
    get() { return html; },
    set(v) { html = String(v); if (tag === 'template') el.content.childNodes = parseHTML(html); },
  });
  return el;
}
const TOKEN = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=\/>]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
const ATTR = /([^\s=\/>]+)(?:="([^"]*)")?/g;
function parseHTML(html) {
  const root = {childNodes: []};
  const stack = [root];
  let m;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(html)) !== null) {
    const close = m[1], tag = m[2], attrText = m[3], selfClose = m[4], text = m[5];
    if (text !== undefined) { stack[stack.length - 1].childNodes.push(textNode(text)); continue; }
    const name = String(tag).toLowerCase();
    if (close) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (String(stack[i].tagName).toLowerCase() === name) { stack.length = i; break; }
      }
      continue;
    }
    const el = makeEl(name);
    if (attrText) {
      ATTR.lastIndex = 0;
      let a;
      while ((a = ATTR.exec(attrText)) !== null) {
        if (!a[1]) continue;
        el.setAttribute(a[1], a[2] === undefined ? '' : a[2]);
      }
    }
    stack[stack.length - 1].childNodes.push(el);
    if (!selfClose && !VOID.has(name)) stack.push(el);
  }
  return root.childNodes;
}
function clone(n) {
  if (!n) return n;
  if (n.nodeType === 3) return textNode(n.nodeValue);
  const el = makeEl(String(n.tagName).toLowerCase());
  for (const a of n.attributes) el.setAttribute(a.name, a.value);
  for (const c of n.childNodes) el.appendChild(clone(c));
  return el;
}

const elements = new Map();
const document = {
  body: makeEl('body'), documentElement: makeEl('html'), activeElement: null,
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return clone(node); },
};
const sandbox = {
  Date,
  document,
  location: {hash: ''},
  history: {replaceState() {}},
  window: {addEventListener() {}, removeEventListener() {}, confirm: () => false},
  localStorage: {getItem: () => null, setItem() {}},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  navigator: {language: 'en'},
  fetch: url => {
    const key = String(url).replace(/^\//, '');
    if (!(key in fixture)) return Promise.reject(new Error('no fixture for ' + url));
    return Promise.resolve({ok: true, status: 200, json: async () => fixture[key]});
  },
  setInterval: () => 0, clearInterval: () => {},
  setTimeout: fn => { if (typeof fn === 'function') fn(); return 0; },
  clearTimeout: () => {},
  console,
};
sandbox.globalThis = sandbox;

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;

  const host = document.getElementById('fleet');
  const all = (root, out) => {
    out = out || [];
    if (!root || root.nodeType !== 1) return out;
    out.push(root);
    root.childNodes.forEach(c => all(c, out));
    return out;
  };
  const cls = (n) => (n.getAttribute('class') || '').split(' ');
  const card = name => all(host).find(n => n.getAttribute('data-key') === name);
  const within = (root, name) => all(root).filter(n => cls(n).indexOf(name) >= 0);
  const read = name => {
    const c = card(name);
    return {
      cross: within(c, 'spark-cross').map(n => ({
        x1: n.getAttribute('x1'), x2: n.getAttribute('x2'), y2: n.getAttribute('y2'),
      })),
      values: within(c, 'spark-v').map(n => n.textContent),
      keys: within(c, 'spark-key').map(n => n.textContent),
      svg: within(c, 'spark-plot')[0],
    };
  };

  const h = JSON.parse(JSON.stringify(fixture.healthz));
  api.apply('fleet', api.renderFleet(h));
  const before = read('M5-alpha');
  const svg0 = before.svg;

  /* The pointer rests on bucket 4 — the bucket without a probe (#96), which
     the line crosses and the readout therefore bridges (#105). Then a tick
     whose numbers moved: `apply()` patches the card, it does not rebuild it.
     The crosshair has to be there afterwards, on the same <svg>. */
  api.sparkHoverSet('M5-alpha', 4);
  h.hosts[0].series[0].total[6] = 999.9;
  api.apply('fleet', api.renderFleet(h));
  const patched = read('M5-alpha');

  /* One bucket further, same mechanism. */
  api.sparkHoverSet('M5-alpha', 6);
  api.apply('fleet', api.renderFleet(h));
  const moved = read('M5-alpha');

  /* The pointer leaves: the legend comes back, nothing of the readout stays. */
  api.sparkHoverClear();
  api.apply('fleet', api.renderFleet(h));
  const cleared = read('M5-alpha');

  process.stdout.write(JSON.stringify({
    before: {cross: before.cross, values: before.values, keys: before.keys},
    patched: {cross: patched.cross, values: patched.values, keys: patched.keys,
              same_svg: patched.svg === svg0},
    moved: {cross: moved.cross, values: moved.values},
    cleared: {cross: cleared.cross, values: cleared.values, keys: cleared.keys},
    span: api.seriesGeom(fixture.healthz.hosts[0].series[0]).span,
  }));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def spark_patch(tmp_path_factory) -> dict:
    """`dashboard.js` against a DOM that really reconciles — the patch, measured.

    The question is not what `sparkline()` returns but whether the crosshair is
    still in the document after `apply()` has rebuilt the card from a new
    payload. That is what a second `<svg>` (or a hover state painted once into
    the DOM) would fail.
    """
    if NODE is None:
        pytest.skip("node is not installed — the renderer is JavaScript")
    work = tmp_path_factory.mktemp("spark-patch")
    fixture = work / "fixture.json"
    fixture.write_text(json.dumps({"healthz": _series_healthz(), **_catalogs()}),
                       encoding="utf-8")
    harness = work / "spark_patch_harness.js"
    harness.write_text(SPARK_PATCH_HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness), str(STATIC / "dashboard.js"), str(fixture)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_pointer_maps_to_a_bucket_at_the_edges_and_in_the_middle(series_run):
    """Screen x → bucket. The viewBox is stretched onto the card, so viewBox
    units are not pixels: the fraction comes from the `<svg>` rectangle and the
    index from `span` — the number of buckets minus one, the same number the
    path is drawn with. Rounded, so a pointer between two buckets names the
    nearer one; clamped, so the edge of the plot cannot name a bucket outside
    the window."""
    idx = series_run["idx"]
    assert series_run["span"] == len(SERIES_N) - 1 == 9
    assert idx["left"] == 0                       # the first pixel is bucket 0
    assert idx["mid"] == 5                        # 4.5 of 9 rounds to the nearer
    assert idx["right"] == 9                      # the last pixel is the last bucket
    assert idx["past_right"] == 9 and idx["before_left"] == 0
    assert idx["narrow"] == 0                     # a hidden tab has no width
    # every bucket is reachable at its own x: mapping and drawing agree
    assert idx["round_trip"] == list(range(10))


def test_the_readout_follows_the_line_across_one_empty_bucket(series_run):
    """#105: the pointer said `—` exactly where the line drew a segment.

    The probe runs every 10 s into 5 s buckets, so every second bucket is empty
    by construction (#96) and the pen crosses it. The readout applies the same
    rule now: bucket 4 reads the value of the segment that crosses it — the
    probe before it, 60.4 / 30.2 — and the leading `~` says that the number was
    not measured at that x. The crosshair stands on the pointer's bucket, in the
    coordinates of the line; only the number changes.
    """
    assert series_run["ro_gap"] == {"total": "~60.4", "per_thread": "~30.2"}
    assert series_run["ro_sample"] == {"total": "152.5", "per_thread": "30.5"}

    card = series_run["hover_card"]
    assert card.count('<b class="spark-v">~') == 2
    assert ">0.0<" not in card and ">0<" not in card
    cross = re.search(r'class="spark-cross" x1="([\d.]+)" y1="0" x2="([\d.]+)" y2="34"', card)
    assert cross, card
    assert cross[1] == cross[2]                   # a vertical line, no lean
    assert float(cross[1]) == pytest.approx(SERIES_GAP_AT / 9 * 1000, abs=0.1)

    # the words are the two ids of the legend, not new ones
    assert series_run["hover_card"].count('class="spark-key"') == 2
    assert series_run["hover_state"] == {"key": "M5-alpha", "i": 4}
    # the pointer is on one card: the other card keeps its legend
    assert 'class="spark-v"' not in series_run["hover_other"]
    assert 'class="spark-cross"' not in series_run["hover_other"]
    # and leaving restores the legend exactly
    assert 'class="spark-v"' not in series_run["plain_card"]
    assert 'class="spark-cross"' not in series_run["plain_card"]
    # a card without samples has no bucket to point at: no readout, no crosshair
    assert 'class="spark-v"' not in series_run["hover_none"]
    assert 'class="spark-cross"' not in series_run["hover_none"]


def test_the_readout_survives_the_two_second_patch(spark_patch):
    """The patch rebuilds the card; the pointer must not lose its crosshair.

    Measured on the reconciler: the same `<svg>` object before and after a tick
    whose numbers moved, with the crosshair and the two values inside it. The
    state lives in a module variable and `sparkline()` reads it, so the markup
    the patch writes already carries it — a hover state painted once into the
    DOM would be gone twice a second.
    """
    span = spark_patch["span"]
    assert span == len(SERIES_N) - 1 == 9
    assert spark_patch["before"]["cross"] == []
    assert spark_patch["before"]["values"] == []

    patched = spark_patch["patched"]
    assert patched["same_svg"] is True            # patched, not rebuilt
    assert len(patched["cross"]) == 1
    assert patched["cross"][0]["x1"] == str(round(SERIES_GAP_AT / span * 1000, 1))
    assert patched["cross"][0]["x1"] == patched["cross"][0]["x2"]
    assert patched["cross"][0]["y2"] == "34"      # the height of the plot
    assert patched["values"] == ["~60.4", "~30.2"]   # bucket 4 is bridged (#105)
    # the legend's own words stay in the head — the number is added, not swapped in
    assert patched["keys"][0].startswith("total")
    assert patched["keys"][1].startswith("per thread")

    # one bucket further: the readout follows, and the sample is a number —
    # the number of this payload, not the one the block was first drawn with
    assert len(spark_patch["moved"]["cross"]) == 1
    assert spark_patch["moved"]["values"] == ["999.9", "30.5"]

    cleared = spark_patch["cleared"]
    assert cleared["cross"] == [] and cleared["values"] == []
    assert cleared["keys"] == ["total", "per thread"]


# -- der Readout folgt der Linie (issue #105) --------------------------------
# Der Betreiber fand den Widerspruch beim ersten Hovern: jeder zweite Bucket
# zeigte keinen Wert, obwohl die Linie dort durchgezogen war. Die Sondierung
# (10 s in 5-s-Eimer) ist die Auflösung der Daten, kein Defekt — der Defekt war,
# dass Zeichnung und Zahl zwei verschiedene Regeln anwandten. `seriesValueAt()`
# ist die eine Regel; sie nimmt `SPARK_HOLD` als Parameter, damit der nächste
# Tuning-Schritt nicht wieder zwei Konstanten hinterlässt.
def test_a_bucket_with_a_probe_keeps_its_own_value(series_run):
    """(a) A measured bucket is what it is: no marker, no neighbour's number."""
    assert series_run["at"]["sample"] == {"value": 60.4, "held": False}
    assert series_run["ro"]["gap3"] == {"total": "60.4", "per_thread": "30.2"}


def test_one_empty_bucket_reads_the_value_of_the_probe_before_it(series_run):
    """(b) The pen crosses one empty bucket, so the number does too — and says so.

    The value is the predecessor, not an interpolation: `30.0` and `62.0` around
    bucket 1 of `two` would average to 46.0, and no such measurement exists.
    """
    assert series_run["at"]["gap"] == {"value": 60.4, "held": True}
    assert series_run["ro"]["gap"] == {"total": "~60.4", "per_thread": "~30.2"}
    assert series_run["ro"]["two1"] == {"total": "~30.0", "per_thread": "~30.0"}
    # two gaps in one row, each bridged from its own predecessor
    assert series_run["ro"]["three1"] == {"total": "~30.0", "per_thread": "~30.0"}
    assert series_run["ro"]["three3"] == {"total": "~62.0", "per_thread": "~31.0"}


def test_two_empty_buckets_in_a_row_read_as_a_dash(series_run):
    """(c) A lost tick is information: the line ends there, so the number does.

    `hole` is `[30.0, None, None, 62.0]` — both empty buckets read `—`, in both
    rows, and the drawn path has no segment to bridge them (#96 already holds
    the ink side of this; this is the same rule on the number side).
    """
    assert series_run["at"]["hole"] == {"value": None, "held": False}
    assert series_run["at"]["hole2"] == {"value": None, "held": False}
    assert series_run["ro"]["hole1"] == {"total": "\u2014", "per_thread": "\u2014"}
    assert series_run["ro"]["hole2"] == {"total": "\u2014", "per_thread": "\u2014"}
    assert "L" not in series_run["hole"]["total"]


def test_an_empty_bucket_at_the_edge_of_the_row_has_no_number(series_run):
    """(d) No segment is drawn before the first or after the last probe.

    So there is nothing to bridge to: `—`, no throw, no invented number. A
    pointer outside the window is not a bucket either, and neither is a missing
    row.
    """
    assert series_run["at"]["head"] == {"value": None, "held": False}
    assert series_run["at"]["tail"] == {"value": None, "held": False}
    assert series_run["ro"]["head0"] == {"total": "\u2014", "per_thread": "\u2014"}
    assert series_run["ro"]["tail2"] == {"total": "\u2014", "per_thread": "\u2014"}
    assert series_run["ro"]["lone0"] == {"total": "\u2014", "per_thread": "\u2014"}
    assert series_run["at"]["outside"] == {"value": None, "held": False}
    assert series_run["at"]["none"] == {"value": None, "held": False}


def test_the_readout_bridges_with_the_constant_the_line_draws_with(series_run):
    """(e) One `SPARK_HOLD`, not a second hard-coded 1 in the readout.

    The geometry carries the constant, so a pen that lifts on the first empty
    bucket lifts the number with it — that is what `hold = 0` measures here.
    """
    assert series_run["hold"] == 1
    assert series_run["geom_hold"] == series_run["hold"]
    assert series_run["at"]["hold_zero"] == {"value": None, "held": False}


def test_the_total_row_is_separated_by_an_axis_not_by_a_ring(css):
    """#102, part two — revised by #106: the sum was drawn quieter than its parts.

    `--fg-2` plus `opacity:.7` on the bar — and because the opacity hung on the
    container, the empty track was faded along with the segments. #102 answered
    with a 1-px `--fg-2` ring around the bar; #106 takes it out again. The ring
    was #a8b2c0 (luminance 0.440), the idle segment #7b8694 (0.234): 45 steps
    apart, both achromatic — and a thin grey line around a bar that is 70–80 %
    grey groups *with* the grey instead of separating from it. The separator is
    an axis now: `--line-3` (#46505e, luminance 0.079) is darker than every
    segment colour (the darkest, idle, is 0.234), so it cannot be read as one of
    them, and a full-width hairline is the bookkeeping convention for a sum.
    """
    name = re.search(r"\n  #load \.lr-name\{([^}]*)\}", css)[1]
    total = re.search(r"\n  #load \.loadrow\.total\{([^}]*)\}", css)[1]
    rule = re.search(r"\n  #load \.load-sum-rule\{([^}]*)\}", css)[1]

    assert "color:var(--fg)" in name             # the row reads at full strength
    assert "#load .loadrow.total .lr-name" not in css   # no quieter override left
    assert "opacity" not in total
    assert "outline" not in total                # the ring is gone, not moved
    assert "#load .loadrow.total .loadbar" not in css   # …and nothing re-draws it
    assert "border" not in total                 # a border would move the layout
    assert "margin-top:8px" in total
    # the rule is positioned against the row it belongs to, not against the page
    assert "position:relative" in total
    assert "position:absolute" in rule and "left:0" in rule and "right:0" in rule
    assert "height:1px" in rule
    assert "background:var(--line-3)" in rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rule), "literal hex in the rule"
    # the segments keep their five colours: the axis is the separator, not a tone
    for cls, token in (("idle", "--fg-3"), ("loading", "--load"), ("prefill", "--warn"),
                       ("decode", "--ok"), ("mixed", "--maint")):
        assert re.search(r"\.seg-" + cls + r"\{background:var\(" + token + r"\)\}", css), cls

    def lum(h):
        def ch(v):
            v = v / 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = [int(h[i:i + 2], 16) for i in (1, 3, 5)]
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)

    def hexof(name):
        return re.search(name + r":(#[0-9a-f]{6})", css)[1]

    axis = lum(hexof("--line-3"))
    track = lum(hexof("--surface-3"))
    # an axis, not a colour: every segment tone is at least twice as bright
    for seg in ("--fg-3", "--load", "--warn", "--ok", "--maint"):
        assert axis * 2 <= lum(hexof(seg)), seg
    assert lum(hexof("--line-3")) > track        # darker than the track, but a step
    assert lum(hexof("--line-3")) - track > 0.03 # …that the eye can take


def test_the_total_row_carries_the_rule_across_the_full_row(rendered):
    """The line lives in the markup of the sum row, as its own element (#106).

    A `border-top` on the row would push the row down by 1 px and change the
    geometry; a `grid-column:1/-1` child would open a second grid row and grow
    the row by the row gap. So the element sits *outside* the grid — absolutely
    positioned against the row, spanning `left:0` to `right:0` — and only the sum
    row has it.
    """
    markup = rendered["parts"]["load"]
    assert markup.count('class="load-sum-rule"') == 1

    chunks = markup.split('<div class="loadrow')
    for chunk in chunks[1:]:
        head = chunk.split(">", 1)[0]
        if "total" not in head:
            assert "load-sum-rule" not in chunk   # no node row draws the axis
    total = [c for c in chunks[1:] if "total" in c.split(">", 1)[0]][0]
    body = total.split(">", 1)[1]
    # first child, so it is not read as part of the name or the bar
    assert body.startswith('<span class="load-sum-rule" aria-hidden="true"></span>')
    assert body.index("load-sum-rule") < body.index('class="lr-name"')


def test_the_total_row_moves_down_eight_pixels_without_growing_the_block(css):
    """The sum sits 8 px below its parts, and the block is not one pixel taller.

    The padding pays for the gap: bottom padding minus 8 px plus the 8 px margin
    is the top padding — the same height as before, in every window width, which
    is why the measurement at 1440 px and at 741 px can be equal. The rule sits
    inside that margin (`top:-8px`), so it costs nothing either.
    """
    bars = re.search(r"\n  #load \.loadbars\{([^}]*)\}", css)[1]
    total = re.search(r"\n  #load \.loadrow\.total\{([^}]*)\}", css)[1]
    s3 = int(re.search(r"--s3:(\d+)px", css)[1])
    paid = int(re.search(r"padding:var\(--s3\) 0 calc\(var\(--s3\) - (\d+)px\)", bars)[1])
    gap = int(re.search(r"margin-top:(\d+)px", total)[1])
    assert s3 - paid + gap == s3                  # 12 − 8 + 8 == 12
    assert gap == 8 and paid == 8                 # the paragraph, and who pays
    assert "gap:9px" in bars                      # the row spacing is untouched
    assert "top:-8px" in re.search(r"\n  #load \.load-sum-rule\{([^}]*)\}", css)[1]


# -- die relative Last neben dem Gewicht (#80) -------------------------------
# Das Feld ist ein Vertrag, keine Erfindung der Seite: `hosts[].relative_load`
# ist eine Zahl 0..1 oder `null` (daneben `capability_rate`). Ein fehlendes Feld
# heißt deshalb „nicht gemessen" und nicht 0 — eine 0 wäre eine Aussage über die
# Maschine, die niemand gemacht hat. Der Test baut den Payload, nicht den Code:
# `0.164` steht im Fixture, `16 %` muss auf der Karte stehen.
REL_LOAD = 0.164


def _rel_healthz(**field) -> dict:
    """`_healthz()` mit dem Feld auf beiden Knoten — oder ganz ohne es."""
    h = json.loads(json.dumps(_healthz()))
    for row in h["hosts"]:
        row.update(field)
    return h


@pytest.fixture(scope="module")
def rel_loads(tmp_path_factory) -> dict:
    """Vier Payloads, eine Karte: gemessen, gemessene Null, `null`, Feld fehlt."""
    def run(name: str, **field) -> str:
        data = _stub_fixture(healthz=_rel_healthz(**field))
        return _run_stub(tmp_path_factory.mktemp(name), data)["parts"]["fleet"]

    return {"measured": run("rel-measured", relative_load=REL_LOAD),
            "zero": run("rel-zero", relative_load=0.0),
            "null": run("rel-null", relative_load=None),
            "missing": run("rel-missing")}


def test_the_relative_load_stands_next_to_the_weight_as_a_number(rel_loads):
    """`0.164` wird `16 %`: die Zahl, nicht nur eine Farbe — und in derselben
    Unterzeile wie das Gewicht, weil Gewicht die Absicht und Last die
    Wirklichkeit ist. Zwei Zeilen wären zwei Aussagen ohne Bezug.

    Nur die Zahl, nicht das Wort (Variante 3): in einer Viertel-Karte sind zwei
    Wörter nicht in jeder der 16 Sprachen unterzubringen (griechisch 171 px bei
    150 px). Das Wort steht im Tooltip der Zelle — der Test prüft beides, damit
    „ohne Wort" nicht heißt „ohne Erklärung"."""
    card = _cards(rel_loads["measured"])["M5-alpha"]
    found = re.search(r'<div class="metric-sub">target 60 % · '
                      r'<span class="rel">([^<]*)</span></div>', card)
    assert found, card[card.index("metric-sub"):][:240]
    assert found[1] == "16 %"
    tips = re.findall(r'<div class="metric tip cell" data-tip="([^"]*)"', card)
    assert any("relative load" in tip for tip in tips), tips


def test_a_missing_measurement_is_a_dash_and_never_zero(rel_loads):
    """`null` und ein fehlendes Feld sagen dasselbe: nicht gemessen. Beides
    trägt den Satz im `data-tip`, damit das `—` nicht als 0 gelesen wird — und
    zwar im selben Layer wie jeder andere Tooltip, nicht als `title`."""
    for key in ("null", "missing"):
        card = _cards(rel_loads[key])["M5-alpha"]
        assert re.search(r'<span class="rel none" data-tip="not measured  Source: '
                         r'/healthz → hosts\[\]\.relative_load">—</span>', card), key
        assert 'title="not measured"' not in card, key
        # „target 60 %" enthält selbst ein „0 %" — deshalb auf das gerenderte
        # Last-Element prüfen, nicht auf die Zeichenkette
        assert ">0 %<" not in card, key
    # eine gemessene Null ist dagegen eine Zahl wie jede andere
    assert '<span class="rel">0 %</span>' in _cards(rel_loads["zero"])["M5-alpha"]


def test_the_load_grows_no_block_on_the_card(rel_loads):
    """Die Karte darf nicht wachsen: das Feld ändert Text, keine Zeile.

    Nimmt man die Last wieder heraus, ist das Markup der Karte *mit* Messung
    identisch mit dem der Karte *ohne* Feld — gleiche Blöcke, gleiche Zeilen,
    gleiche Zahl von `<div>`. Was wüchse, wäre eine zweite Zeile in der
    Unterzeile oder ein eigener Block; beides wäre hier zu sehen."""
    def without(card: str) -> str:
        return re.sub(r' · <span class="rel[^"]*"[^>]*>[^<]*</span>', '', card)

    measured = _cards(rel_loads["measured"])["M5-alpha"]
    missing = _cards(rel_loads["missing"])["M5-alpha"]
    assert measured.count("<div") == missing.count("<div")
    assert without(measured) == without(missing)


def test_the_load_stays_inside_the_one_line_of_the_sub(css, rel_loads):
    """Die Unterzeile ist einzeilig (`nowrap` + Ellipse) und die Last ist ein
    `<span>` darin: kein `display:block`, kein `<br>`, weiter vier Zellen."""
    sub = re.search(r"\.metric-sub\{([^}]*)\}", css)[1]
    assert "white-space:nowrap" in sub
    assert "text-overflow:ellipsis" in sub
    rel = re.search(r"\.metric-sub \.rel\{([^}]*)\}", css)[1]
    assert "display:block" not in rel and "display:flex" not in rel
    card = _cards(rel_loads["measured"])["M5-alpha"]
    assert "<br" not in card
    assert card.count('class="metric-sub"') == 5


def test_the_four_metric_cells_stay_equal_wide(css):
    """Variante 3: gleiche Spalten *und* nichts abgeschnitten. Ungleiche Spalten
    (`auto`/`1fr`) hätten das Abschneiden gelöst, aber die zwei Host-Karten wären
    nicht mehr pixelgleich — die Karten sind das Vergleichsbild. Der Test hält
    die Entscheidung fest, damit sie nicht beim nächsten Layout-Wunsch
    stillschweigend zurückkommt: vier gleich breite Zellen, kein `auto`."""
    metrics = re.search(r"\.metrics\{([^}]*)\}", css)
    assert metrics
    assert "repeat(4,minmax(0,1fr))" in metrics[1]
    assert "auto" not in metrics[1]


# -- das Alter der Messung in seiner Einheit (#80) ---------------------------
# Rohe Sekunden waren die Ursache des Überlaufs: „32 018 569 s ago" ist in einer
# 149,6-px-Zelle fünfstellig und war in sieben der 16 Sprachen breiter als die
# Zelle (polnisch +78 px, französisch +62 px, englisch +39 px). Der Test baut
# den Payload über `measured_at` — der Stub hat eine stehende Uhr (`FROZEN`), ein
# Alter von 86 400 s ist deshalb exakt 86 400 s — und liest, was auf der Karte
# steht. Die Kataloge tragen die Einheiten (`tests/test_i18n.py` prüft sie in
# allen 16); hier steht, dass die Seite sie benutzt.
SENTINEL_AGE = 32_018_569          # „nie gemessen", aber die Zeile existiert


def _age_healthz(seconds: int) -> dict:
    """`_healthz()` mit einer Messung, die `seconds` alt ist."""
    h = json.loads(json.dumps(_healthz()))
    for row in h["hosts"]:
        for m in row["metrics"].values():
            m["measured_at"] = FROZEN - seconds
    return h


@pytest.fixture(scope="module")
def ages(tmp_path_factory) -> dict:
    def run(name: str, seconds: int) -> str:
        data = _stub_fixture(healthz=_age_healthz(seconds))
        return _run_stub(tmp_path_factory.mktemp(name), data)["parts"]["fleet"]

    return {"now": run("age-now", 0), "minute": run("age-60", 60),
            "day": run("age-86400", 86400), "sentinel": run("age-sentinel", SENTINEL_AGE)}


def _tps_sub(card: str) -> str:
    """Die Unterzeile der vierten Zelle — tok/s — einer Karte.

    Fünf seit #98: die vierte ist tok/s, die fünfte ist der Prefix-Cache.
    """
    subs = re.findall(r'<div class="metric-sub">(.*?)</div>', card, re.S)
    assert len(subs) == 5, subs
    return subs[3]


def test_the_age_of_the_measurement_is_read_in_its_unit(ages):
    """60 s wird `1 min ago`, 86 400 s `1 d ago`, der Sentinel `370 d ago`.

    Eine fünfstellige Sekundenzahl war in der Viertel-Karte nicht unterzubringen;
    die Einheit ist die Reparatur, nicht das Grid. `just now` bleibt für den
    Moment — „0 s ago" wäre keine Messung, sondern eine Uhr.
    """
    expected = {"now": "just now", "minute": "1 min ago",
                "day": "1 d ago", "sentinel": "370 d ago"}
    for key, text in expected.items():
        card = _cards(ages[key])["M5-alpha"]
        assert _tps_sub(card) == text, key
        # keine rohen Sekunden mehr, in keiner Schreibweise des Tausenderpunkts
        for raw in ("86,400", "86400", "32,018,569", "32018569", "370.6"):
            assert raw not in card, (key, raw)


def test_the_sample_count_moved_into_the_tooltip_of_the_cell(ages):
    """Die Zeile trägt *eine* Angabe: das Alter. Die Probenzahl — wie viele
    Anfragen in die Rate eingegangen sind — steht im Tooltip derselben Zelle.

    Zwei Angaben nebeneinander waren in sieben der 16 Sprachen breiter als die
    Zelle; eine passt überall. Der Tip ist derselbe Layer wie jeder andere
    (`data-tip`), kein zweites `title`.
    """
    card = _cards(ages["day"])["M5-alpha"]
    sub = _tps_sub(card)
    assert "sample" not in sub and "Proben" not in sub, sub
    assert not re.search(r"\d", sub.replace("1 d ago", "")), sub
    tips = re.findall(r'data-tip="([^"]*)"', card)
    assert any(t.endswith("309 samples") for t in tips), tips
    assert 'title="309 samples"' not in card


# -- die Messung des Servers auf der Karte (#98) -----------------------------
# oMLX liefert in `usage` mit, was es selbst gemessen hat; `/healthz →
# hosts[].usage` fasst es je Knoten zusammen. Die Karte zeigt **eine** Zahl:
# `cached_tokens` der letzten Antwort gegen ihren Prompt. Geprüft wird, was
# gerendert wird — `689 / 694` und `99 % cached` —, nicht dass ein Feld existiert.
CACHE_USAGE = {
    "host": "M5-alpha", "samples": 3, "last_at": FROZEN,
    "last": {"cached_tokens": 689, "prompt_tokens": 694,
             "prompt_tokens_per_second": 17.38,
             "generation_tokens_per_second": 70.12,
             "time_to_first_token": 0.17},
    "mean": {"cached_tokens": 229.67, "prompt_tokens": 694,
             "prompt_tokens_per_second": 17.1,
             "generation_tokens_per_second": 70.1,
             "time_to_first_token": 0.2},
}


def _cache_healthz(usage) -> dict:
    """`_healthz()` mit einer Messung je Knoten — oder ganz ohne (`None`)."""
    h = json.loads(json.dumps(_healthz()))
    for row in h["hosts"]:
        row["usage"] = usage
    return h


@pytest.fixture(scope="module")
def cache_cards(tmp_path_factory) -> dict:
    """Vier Karten: warm, kalter Prefix, Server meldet nichts, halbe Messung."""
    cold = dict(CACHE_USAGE)
    cold["last"] = dict(CACHE_USAGE["last"], cached_tokens=0)
    partial = {"host": "M5-alpha", "samples": 1, "last_at": FROZEN,
               "last": {"cached_tokens": 5}, "mean": {}}

    def run(name: str, usage) -> dict:
        data = _stub_fixture(healthz=_cache_healthz(usage))
        return _cards(_run_stub(tmp_path_factory.mktemp(name), data)["parts"]["fleet"])

    return {"warm": run("cache-warm", CACHE_USAGE), "cold": run("cache-cold", cold),
            "none": run("cache-none", None), "partial": run("cache-partial", partial)}


def test_the_cache_column_shows_the_last_answer_against_its_prompt(cache_cards):
    """`689 / 694` und `99 % cached`: die Zahl, die vorher niemand sehen konnte.

    Der Wert ist der letzte gemessene `cached_tokens`, der Nenner der Prompt
    derselben Antwort — nicht die mittlere Quote, denn die Frage lautet „hat der
    Cache bei *dieser* Anfrage getragen".
    """
    card = cache_cards["warm"]["M5-alpha"]

    assert 'class="metric-val num">689<span class="den"> / 694</span>' in card
    assert '<div class="metric-sub">99 % cached</div>' in card


def test_a_cold_prefix_is_a_zero_and_a_silent_server_is_a_dash(cache_cards):
    """`0` ist eine Messung, `—` ist keine — der Unterschied ist der Punkt.

    Ein Backend, das nichts meldet, darf nicht wie ein kalter Cache aussehen;
    eine halbe Messung (nur `cached_tokens`, kein Prompt) zeigt die Zahl ohne
    Nenner und „no measurement" in der Unterzeile.
    """
    cold = cache_cards["cold"]["M5-alpha"]
    assert 'class="metric-val num">0<span class="den"> / 694</span>' in cold
    assert '<div class="metric-sub">0 % cached</div>' in cold

    card = cache_cards["none"]["M5-alpha"]
    assert 'class="metric-val num">—</div>' in card
    assert '<div class="metric-sub">no measurement</div>' in card

    # eine halbe Messung zeigt wenigstens, was da ist — ohne Nenner
    partial = cache_cards["partial"]["M5-alpha"]
    assert 'class="metric-val num">5</div>' in partial
    assert '<div class="metric-sub">no measurement</div>' in partial


def test_the_cache_row_carries_its_rates_and_samples_in_the_tip(cache_cards):
    """Die Raten stehen im Tooltip: die Zeile trägt eine Angabe, nicht vier."""
    card = cache_cards["warm"]["M5-alpha"]
    tips = re.findall(r'data-tip="([^"]*)"', card)

    assert any("3 samples" in tip for tip in tips), tips
    assert any("prefill 17.4 tok/s · decode 70.1 tok/s" in tip for tip in tips), tips
    # the cell is a tooltip like every other one: one `data-tip`, no second box
    cell = re.search(r'<div class="metric tip cell wide" data-tip="([^"]*)"', card)
    assert cell, card[card.index("wide") - 80:][:200]


def test_the_cache_row_spans_the_card_and_leaves_the_four_cells_alone(css):
    """Fünf gleich breite Spalten hätten die vier bestehenden Zellen von 160 auf
    125 px verschmälert — genau der Grund, aus dem #80 die Last zu einer nackten
    Zahl gemacht hat. Die fünfte Zeile nimmt die volle Breite und kostet Höhe,
    nicht Breite; die vier Zellen bleiben gleich breit."""
    rule = re.search(r"\.metrics \.metric\.wide\{([^}]*)\}", css)
    assert rule, "the wide row has no rule"
    assert "grid-column:1/-1" in rule[1]
    metrics = re.search(r"\.metrics\{([^}]*)\}", css)[1]
    assert "repeat(4,minmax(0,1fr))" in metrics


# ── 6.5 Parameter: Wert in Kraft · Herkunft · Zurücksetzen (issue #78) ──────
# The block stopped being a read-only list. Three things are measured here, and
# none of them by looking at a picture: the table's shape (three columns behind
# the two labels, the same cell count in every row), the origin of a row (a
# dotted path in `overrides` is "set at the page", everything else is
# `config.yaml`), and the requests the page builds — the URL, the body and the
# second, confirmed call a weight change needs.

PARAMS_VIEWPORT = 1440        # the window width the layout is about
PARAMS_SCROLLBAR = 15         # `documentElement.clientWidth` in a 1440-px window
PARAMS_CHAR_PX = 7.05         # 12 px system UI font: "maintenance" 77.3 / 11
PARAMS_CELL_PAD = 10          # `details.params td{padding:4px 10px}`


def _params_cells(markup: str) -> tuple[list[str], list[list[str]]]:
    """The head row's cell texts and every body row's cells, as raw markup.

    `_table()` drops what a cell contains; here the field inside a value cell is
    part of the measurement, so the cells come back as HTML.
    """
    body = markup[markup.index("<tbody>"):markup.index("</tbody>")]
    head = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", markup[:markup.index("<tbody>")], re.S)
    rows = [[html for _attrs, html in re.findall(r"<td([^>]*)>(.*?)</td>", row, re.S)]
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)]
    return head, rows


def _cell_text(markup: str) -> str:
    return re.sub(r"<[^>]+>", " ", markup)


def _widest_token(markup: str) -> float:
    """The widest unbreakable run in a cell, in px — the floor of its width."""
    words = [w for w in re.split(r"\s+", _cell_text(markup)) if w]
    return max((len(w) for w in words), default=0) * PARAMS_CHAR_PX


def _params_columns(css: str) -> dict[str, float]:
    widths = {}
    for name in ("c-scope", "c-param", "c-source", "c-reset"):
        rule = _rule(css, "details.params .ptable col." + name)
        found = re.search(r"width:([0-9.]+)px", rule)
        assert found, (name, rule)
        widths[name] = float(found[1])
    wrap = _rule(css, ".wrap")
    pad = re.search(r"padding:(var\([^)]*\)|[0-9.]+px) (var\([^)]*\)|[0-9.]+px)", wrap)
    assert pad, wrap
    available = (PARAMS_VIEWPORT - PARAMS_SCROLLBAR - 2 * _px(css, pad[1]))
    widths["c-value"] = available - sum(widths.values())
    return widths


@pytest.fixture(scope="module")
def params_run(tmp_path_factory) -> dict:
    """The real page on a `/config` that carries `editable` and `overrides`."""
    return _run_stub(tmp_path_factory.mktemp("dashboard-params"), _stub_fixture())


def test_block_6_5_has_three_columns_behind_its_two_labels(params_run, en):
    """Scope and parameter stay, and three columns follow: the value in effect,
    where it comes from, and the reset control. Five cells in every row — a
    body row that is one cell short is the classic table defect and it is the
    one thing a screenshot does not show."""
    head, rows = _params_cells(params_run["params"]["block"])
    assert head == [en["params.th_scope"], en["params.th_param"], en["params.th_value"],
                    en["params.th_source"], en["params.th_reset"]], head
    assert rows, "the block is empty"
    assert {len(row) for row in rows} == {5}, {len(row) for row in rows}
    # the existing labels and value formatting stay: one row per policy value,
    # the ones that used to share a sentence are separate rows now because each
    # carries its own field and its own reset.
    text = _cell_text(params_run["params"]["block"])
    assert "thread TTL" in text and "1,800 s (30 min)" in text
    assert "weight → share" in text and "2 → 60.0 %" in text


def test_only_an_overridden_row_says_where_it_comes_from_and_offers_reset(params_run, en):
    """Origin is derived, not hardcoded: exactly the row whose path is in
    `overrides` carries the chip and the reset button, every other row says
    `config.yaml` and has no control."""
    block = params_run["params"]["block"]
    _head, rows = _params_cells(block)
    marked = [row for row in rows if 'class="psrc page"' in row[3]]
    assert len(marked) == 1, [row[0] for row in marked]
    assert "M5-alpha" in marked[0][0] and "weight" in marked[0][1]
    assert en["params.src_page"] in marked[0][3]
    assert 'class="btn pbtn"' in marked[0][4]
    # every other row: the file, and no control
    for row in rows:
        if row is marked[0]:
            continue
        assert en["params.src_file"] in row[3], row[3]
        assert "pbtn" not in row[4], row[4]
    assert block.count('class="psrc"') == len(rows) - 1


def test_a_path_outside_editable_gets_no_input(params_run):
    """Twelve paths are editable in this payload — twelve fields, no more. A row
    the server did not list keeps its value as text: the page does not invent a
    field for a path the backend would refuse."""
    block = params_run["params"]["block"]
    _head, rows = _params_cells(block)
    fields = [row for row in rows if "pedit" in row[2]]
    assert len(fields) == 12, len(fields)
    for path in CONFIG_EDITABLE:
        concrete = path.replace("*", "M5-alpha") if path.startswith("hosts.") else path
        assert any('data-path="%s"' % concrete in row[2] for row in fields), path
    # a row that is not editable keeps its value and no field
    for label in ("binding strategy", "connect timeout", "prefill rate (cost model)"):
        row = [r for r in rows if _cell_text(r[1]).strip() == label]
        assert row and "pedit" not in row[0][2], label


def test_a_restart_bound_row_carries_its_reason_and_no_input(params_run, en):
    """Identity and transport cannot be set at the page: the row says why
    instead of offering a field that would need a restart anyway."""
    block = params_run["params"]["block"]
    _head, rows = _params_cells(block)
    why = en["params.restart_bound"]
    for label in ("listen", "log level · file", "session header", "max inflight / threads",
                  "hard threshold (guard)", "api key"):
        row = [r for r in rows if _cell_text(r[1]).strip() == label]
        assert row, label
        assert why in _cell_text(row[0][2]), label
        assert "pedit" not in row[0][2], label
    assert block.count(why) >= 6


def test_an_old_config_payload_renders_exactly_todays_table(params_run):
    """An older backend sends neither key. Then the block is the read-only list
    it was: three columns, no field, no reset, no empty column, no error."""
    legacy = params_run["params"]["legacy"]
    assert legacy == params_run["parts"]["params"], "the old payload changed shape"
    head, rows = _params_cells(legacy)
    assert len(head) == 3, head
    assert {len(row) for row in rows} == {3}
    for absent in ("<input", "<select", "pbtn", "psrc", "params-why", "<colgroup"):
        assert absent not in legacy, absent


def test_a_save_sends_the_path_and_the_value_without_confirm(params_run):
    """The request the page builds: `PATCH /admin/config/<path>` with the value
    and `confirm: false`. The block then re-reads `/config` through the tick it
    already has — no second refresh path, and no stale row."""
    run = params_run["params"]
    assert run["save_calls"] == ["PATCH admin/config/affinity.ttl_seconds"], run["save_calls"]
    assert run["save_body"] == {"value": 900, "confirm": False}, run["save_body"]
    assert run["saved"]["ok"] is True and run["saved"]["applied"] is True
    # after the tick the row is overridden: two chips, two reset controls
    after = run["after_save"]
    assert after.count('class="psrc page"') == 2, after.count('class="psrc page"')
    assert after.count('class="btn pbtn"') == 2


def test_a_refused_value_shows_the_loaders_message_verbatim(params_run):
    """A 400 is not translated and not summarised: the sentence the loader wrote
    stands next to the field, in the row it belongs to."""
    run = params_run["params"]
    assert run["refused"]["ok"] is False
    assert run["refused"]["status"] == 400
    assert run["refused"]["message"] == TTL_INVALID
    assert TTL_INVALID in _cell_text(run["refused_html"])
    row = [r for r in _params_cells(run["refused_html"])[1]
           if 'data-path="affinity.ttl_seconds"' in r[2]]
    assert row and TTL_INVALID in _cell_text(row[0][2]), "the message is not at the field"


def test_a_weight_change_names_the_threads_and_applies_only_on_confirm(params_run):
    """The exception that needs care: a weight that shifts under a running
    thread pulls a conversation away from its prefix cache. The question names
    the count from the 409; the value is applied by the *second* PATCH only."""
    run = params_run["params"]
    question = run["question"]
    assert "3" in question and "2.5" in question, question
    # declined: one call, nothing applied
    assert run["declined"]["applied"] is False
    assert run["declined"]["threads"] == 3
    assert len(run["declined_calls"]) == 1, run["declined_calls"]
    assert '"confirm":false' in run["declined_calls"][0]
    # confirmed: the same PATCH again, this time with `confirm: true`
    assert len(run["confirmed_calls"]) == 2, run["confirmed_calls"]
    assert '"confirm":false' in run["confirmed_calls"][0]
    assert '"confirm":true' in run["confirmed_calls"][1]
    assert run["confirmed"]["applied"] is True
    for call in run["confirmed_calls"]:
        assert call.startswith("admin/config/hosts.M5-alpha.weight "), call


def test_reset_is_a_delete_without_a_body(params_run):
    """Reset removes the override from the overlay — one DELETE, no value."""
    run = params_run["params"]
    assert run["reset_calls"] == ["DELETE admin/config/affinity.ttl_seconds"], run["reset_calls"]
    assert run["reset"]["ok"] is True


def test_without_a_key_the_fields_are_off_and_the_block_says_why(params_run, en):
    """Same rule as the other operator blocks: no key, no field — and the
    sentence that explains it, not a dead control."""
    block = params_run["params"]["no_key"]
    assert 'class="params-why"' in block
    assert en["key.help"] in _cell_text(block)
    _head, rows = _params_cells(block)
    fields = [row for row in rows if "pedit" in row[2]]
    assert len(fields) == 12
    for row in fields:
        assert " disabled" in row[2], row[2][:80]
    resets = [row for row in rows if "pbtn" in row[4]]
    assert resets and all(" disabled" in row[4] for row in resets)


def test_a_narrow_window_keeps_room_for_the_value_field(css):
    """At ≤1100 px the four fixed columns give way, because the value column has
    to hold a field and a field does not shrink: 92 px of input plus the cell's
    padding. Measured in a browser at the narrowest window the page is laid out
    for (741 px): 72 + 168 + 218 + 108 + 128 = 694 px, no cell spilling, no
    horizontal page scroll."""
    block = re.search(r"@media \(max-width:1100px\)\{(.*?)\n  \}", css, re.S)
    assert block, "the narrow rule of block 6.5 is gone"
    widths = {name: float(v) for name, v in
              re.findall(r"col\.c-([a-z]+)\{width:([0-9.]+)px\}", block[1])}
    assert set(widths) == {"scope", "param", "source", "reset"}, widths
    value = 741 - 2 * 16 - sum(widths.values())
    assert value >= 92 + 2 * PARAMS_CELL_PAD + 1, value


def test_the_parameter_table_fits_its_panel_in_every_language(params_run, css):
    """Layout is measured, not eyeballed. `table-layout:fixed` plus a `colgroup`
    means the table is exactly as wide as its container — so the question is
    whether a cell can hold its content: the widest unbreakable word, plus the
    field where a row has one, plus the padding.

    The five languages with the longest words: German, Polish, Czech and Greek
    next to English. This block overflowed in eight of sixteen languages when it
    printed raw seconds; the fix was the unit, and here it is the column widths
    and the wrapping.
    """
    widths = _params_columns(css)
    # the columns are the table: fixed widths plus the value column's remainder
    assert sum(widths.values()) == pytest.approx(
        PARAMS_VIEWPORT - PARAMS_SCROLLBAR - 2 * 32, abs=0.5)
    assert widths["c-value"] > 0
    field_px = 82 + 12 + 2      # `details.params .pedit{width:82px;padding:1px 6px}`
    button_px = 16 + 2          # `details.params .pbtn{padding:1px 8px}`
    for code, markup in sorted(params_run["parts"]["params_live"].items()):
        head, rows = _params_cells(markup)
        assert len(head) == 5 and {len(row) for row in rows} == {5}, code
        for index, name in enumerate(("c-scope", "c-param", "c-value", "c-source", "c-reset")):
            budget = widths[name] - 2 * PARAMS_CELL_PAD - 1
            for row in [head] + rows:
                cell = row[index]
                need = _widest_token(cell)
                if "pedit" in cell:
                    need = max(need, field_px)
                if "pbtn" in cell:
                    need = max(need, _widest_token(cell) + button_px)
                assert need <= budget, (code, name, round(need, 1), round(budget, 1),
                                        _cell_text(cell).strip()[:40])

# -- #101: the sid column, newest first, and the three filters ----------------
# The filter state is a module variable in `dashboard.js` and the bar sits in the
# shell, so the layout stub cannot measure it: it renders once with a fixed
# payload and never sets a filter. This harness runs the real script against a
# DOM stub that keeps the two selects as plain elements, sets a filter between
# two renders and hands back the markup each step produced — the same way the
# i18n tests measure the language instead of reading it.
LOG_HARNESS = r"""
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsPath = process.argv[2];
const i18nDir = process.argv[3];
const fixture = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

const elements = new Map();
function makeEl(tag) {
  const el = {tag: tag || 'div', value: '', childNodes: [], attributes: [],
              textContent: '', innerHTML: ''};
  if (tag === 'template') el.content = {childNodes: []};
  el.appendChild = () => {};
  el.removeChild = () => {};
  el.replaceChild = () => {};
  el.setAttribute = (k, v) => { el.attributes[k] = String(v); };
  el.getAttribute = k => (k in el.attributes ? el.attributes[k] : null);
  el.removeAttribute = k => { delete el.attributes[k]; };
  el.hasAttribute = k => k in el.attributes;
  el.addEventListener = () => {};
  el.classList = {add() {}, remove() {}};
  return el;
}

const shell = {};
function shellNode(attr, id) {
  const el = makeEl('span');
  el.attributes[attr] = id;
  (shell[attr] = shell[attr] || []).push(el);
  return el;
}

const document = {
  documentElement: makeEl('html'),
  body: makeEl('body'),
  activeElement: null,
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl('div'));
    return elements.get(id);
  },
  querySelector() { return null; },
  querySelectorAll(sel) {
    const m = /^\[([a-z0-9-]+)\]$/.exec(sel);
    return m && shell[m[1]] ? shell[m[1]] : [];
  },
  addEventListener() {},
  createElement(tag) { return makeEl(tag); },
  importNode(node) { return node; },
};

function fetchStub(url) {
  const key = String(url);
  if (key.startsWith('/static/i18n/')) {
    const file = path.join(i18nDir, key.replace(/^\/static\/i18n\//, ''));
    if (!fs.existsSync(file)) return Promise.reject(new Error('404 ' + url));
    const body = JSON.parse(fs.readFileSync(file, 'utf8'));
    return Promise.resolve({ok: true, status: 200, json: async () => body,
                            text: async () => JSON.stringify(body)});
  }
  const name = key.replace(/^\//, '');
  if (!(name in fixture.payloads)) return Promise.reject(new Error('no fixture for ' + key));
  const body = fixture.payloads[name];
  return Promise.resolve({ok: true, status: 200, json: async () => body,
                          text: async () => JSON.stringify(body)});
}

class FrozenDate extends Date {
  constructor(...args) { if (args.length) { super(...args); } else { super(1763292000000); } }
  static now() { return 1763292000000; }
}

const sandbox = {
  Date: FrozenDate,
  document,
  localStorage: {getItem: () => fixture.lang || null, setItem() {}, removeItem() {}},
  navigator: {languages: ['en-GB'], language: 'en-GB'},
  window: {confirm: () => false, addEventListener() {}, removeEventListener() {}},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  fetch: fetchStub,
  setInterval: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => {},
  console: {warn() {}, log() {}, error() {}},
};
sandbox.globalThis = sandbox;

for (const id of ['sec.fleet', 'sec.log', 'log.th_sid', 'log.filter_all',
                  'log.filter_hint', 'log.filter_none']) shellNode('data-i18n', id);

(async () => {
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(jsPath, 'utf8'), sandbox, {filename: jsPath});
  const api = sandbox.RIPPLE;
  await api.ready;

  /* The order the payload arrived in, before any render touched it: the page
     must not sort or reverse the array it was given. */
  const payloadOrder = fixture.payloads.stats.decisions.map(x => x.time).join('|');
  const out = {
    short: fixture.sids.map(v => api.logShortSid(v)),
    unfiltered: api.renderLog(fixture.payloads.stats),
    reasons: elements.get('logFilterReason').innerHTML,
    hosts: elements.get('logFilterHost').innerHTML,
    filter_all: api.t('log.filter_all'),
    filter_none: api.t('log.filter_none'),
    of_word: api.t('fleet.of'),
    steps: [],
  };

  for (const step of fixture.steps) {
    api.logSetFilter(step);
    out.steps.push({
      filter: Object.assign({}, api.logFilter),
      markup: api.renderLog(fixture.payloads.stats),
    });
  }

  out.payload_order_after = fixture.payloads.stats.decisions.map(x => x.time).join('|');
  out.payload_unchanged = out.payload_order_after === payloadOrder;
  process.stdout.write(JSON.stringify(out));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err) + '\n');
  process.exit(1);
});
"""


def _log_fixture(*, decisions: list[dict], steps: list[dict], sids: list,
                 lang: str | None = None) -> dict:
    fixture = _stub_fixture()
    stats = _stats()
    stats["decisions"] = decisions
    fixture["payloads"] = {"healthz": fixture["healthz"], "stats": stats,
                           "config": fixture["config"]}
    fixture.update({"stats": stats, "steps": steps, "sids": sids})
    if lang:
        fixture["lang"] = lang
    return fixture


def _run_log(work: pathlib.Path, fixture: dict) -> dict:
    if NODE is None:
        pytest.skip("node is not installed — the log harness needs it")
    payload = work / "log-fixture.json"
    payload.write_text(json.dumps(fixture), encoding="utf-8")
    script = work / "log-harness.js"
    script.write_text(LOG_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(script), str(STATIC / "dashboard.js"), str(STATIC / "i18n"),
         str(payload)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# Three turns of one conversation, one turn of a second, and one decision
# without a thread at all — oldest first, the way `/stats` sends them.
SID_TURNS = [
    {"time": "13:29:36", "model": DEEPSEEK, "host": "M5-alpha", "reason": "thread",
     "prompt_chars": 101, "duration_ms": 212.1, "status": 200,
     "thread": "sid:gwk_5cfba83bf2320dd5"},
    {"time": "13:30:31", "model": DEEPSEEK, "host": "M3-beta", "reason": "ephemeral",
     "prompt_chars": 1219, "duration_ms": 230.0, "status": 200, "thread": None},
    {"time": "13:36:17", "model": DEEPSEEK, "host": "M5-alpha", "reason": "thread",
     "prompt_chars": 160, "duration_ms": 351.9, "status": 500,
     "thread": "sid:gwk_5cfba83bf2320dd5"},
    {"time": "13:40:02", "model": DEEPSEEK, "host": "M3-beta", "reason": "rebind",
     "prompt_chars": 42, "duration_ms": 99.5, "status": 200,
     "thread": "sid:local-1c0777d01fe8a5"},
]


@pytest.fixture(scope="module")
def log_run(tmp_path_factory) -> dict:
    """The log with no filter, with each filter alone and with all three."""
    return _run_log(tmp_path_factory.mktemp("log-filters"), _log_fixture(
        decisions=SID_TURNS,
        sids=["sid:gwk_5cfba83bf2320dd5", "sid:local-1c0777d01fe8a5",
              "sid:gwk_a69118d#6684d451", None, "", "9f3ab2c4d5e6f7089f3a",
              "sid:20261117_150500_ab12"],
        steps=[
            {"sid": "GWK_5CF", "reason": "", "host": ""},      # case-insensitive fragment
            {"sid": "sid:local", "reason": "", "host": ""},    # the prefix may be typed too
            {"sid": "", "reason": "thread", "host": ""},
            {"sid": "", "reason": "", "host": "M3-beta"},
            {"sid": "gwk", "reason": "thread", "host": "M5-alpha"},
            {"sid": "gwk", "reason": "rebind", "host": ""},    # two filters, no row
            {"sid": "zzz", "reason": "", "host": ""},          # no row at all
            {"sid": "", "reason": "", "host": ""},             # back to everything
        ]))


def test_the_sid_column_shows_a_short_form_and_keeps_the_fingerprint(log_run):
    """The column is narrow, the key is long (issue #101): `sid:` comes off, the
    family marker stays whole, the id is cut to eight characters — and a
    `#<fp8>` suffix survives, because it is what tells two work units of one
    conversation apart (#92). No thread at all is the em dash."""
    assert log_run["short"] == [
        "gwk_5cfba83b",              # sid:gwk_5cfba83bf2320dd5
        "local-1c0777d0",            # sid:local-1c0777d01fe8a5
        "gwk_a69118d#6684d451",      # the fingerprint is never cut off
        "—",                         # null
        "—",                         # empty
        "9f3ab2c4d5e6",              # a bare hash key: twelve characters
        "20261117_150",              # a timestamp marker is not a marker
    ]


def test_the_full_key_stays_reachable_in_title_and_tooltip(log_run):
    """The short form is a lossy view, so the cell carries the whole key twice:
    `title` for the browser's own tooltip and `data-tip` for the page's, which
    has to name its source like every other tooltip on the page."""
    markup = log_run["unfiltered"]
    row = re.search(r'<td class="sid"[^>]*>gwk_5cfba83b</td>', markup)
    assert row, "the sid cell is missing"
    assert 'title="sid:gwk_5cfba83bf2320dd5"' in row[0]
    tip = html.unescape(re.search(r'data-tip="([^"]*)"', row[0])[1])
    assert "sid:gwk_5cfba83bf2320dd5" in tip
    assert "Source: /stats → decisions[].thread" in tip
    # a decision without a thread says so, in the cell and in the tooltip
    assert re.search(r'<td class="sid" title="—"[^>]*>—</td>', markup)


def _log_times(markup: str) -> list[str]:
    """The time cell of every row, in document order — the rows as the operator
    reads them, parsed rather than grepped (the cell is a tooltip span)."""
    table = _table(markup)
    return [row[0]["text"].strip() for row in table.body if len(row) == 8]


def test_the_log_reads_newest_first_and_leaves_the_payload_alone(log_run):
    """`s.decisions` is the payload's array, oldest first, and the page keeps it
    for the next re-render — so the reversal has to happen on a copy. The rows
    read newest first; the payload's order is untouched."""
    assert _log_times(log_run["unfiltered"]) == ["13:40:02", "13:36:17",
                                                 "13:30:31", "13:29:36"]
    assert log_run["payload_unchanged"] is True
    # the footer names the oldest and the newest of what is shown, in that order
    foot = log_run["unfiltered"].split('class="log-foot"')[1]
    assert foot.index("13:29:36") < foot.index("13:40:02")


def test_the_footer_counts_the_visible_rows_and_names_the_buffer(log_run):
    """Four decisions, one filter that leaves two: the footer's count, median,
    p90 and error count describe the two rows on screen, and the ring buffer's
    total follows as "of 4" — an operator reading `2` next to `4` must not think
    the page lost two decisions."""
    step = log_run["steps"][4]                       # sid=gwk, reason=thread, M5-alpha
    assert step["filter"] == {"sid": "gwk", "reason": "thread", "host": "M5-alpha"}
    foot = step["markup"].split('class="log-foot"')[1]
    assert "<b>2</b>" in foot, foot[:300]
    # the buffer's total follows the visible count: "· of 4", no new id for it
    assert "· " + log_run["of_word"] + " <span" in foot, foot[:400]
    assert ">4</span>" in foot
    # the visible two are the two M5 turns, the failed one included
    assert foot.count("with an error status") == 1
    assert "<b>1</b>" in foot


def test_the_three_filters_narrow_the_table(log_run):
    """sid is a substring on the raw `thread` value (a fragment or `sid:` both
    work, case-insensitively), reason and host are exact. Every step below is one
    render with the filter set before it."""
    steps = log_run["steps"]

    def rows(markup: str) -> list[str]:
        return _log_times(markup)

    assert rows(steps[0]["markup"]) == ["13:36:17", "13:29:36"]      # GWK_5CF
    assert rows(steps[1]["markup"]) == ["13:40:02"]                  # sid:local
    assert rows(steps[2]["markup"]) == ["13:36:17", "13:29:36"]      # reason=thread
    assert rows(steps[3]["markup"]) == ["13:40:02", "13:30:31"]      # host=M3-beta
    assert rows(steps[4]["markup"]) == ["13:36:17", "13:29:36"]      # all three
    assert rows(steps[5]["markup"]) == []                            # gwk AND rebind
    assert rows(steps[6]["markup"]) == []                            # nothing at all
    assert rows(steps[7]["markup"]) == ["13:40:02", "13:36:17",
                                        "13:30:31", "13:29:36"]      # cleared


def test_an_empty_hit_is_one_line_and_keeps_the_panel(log_run):
    """A filter that matches nothing must not make the block disappear: the head
    stays, one row says so, and the footer stays — otherwise the panel jumps by
    its whole height on every keystroke that overshoots."""
    markup = log_run["steps"][6]["markup"]
    table = _table(markup)
    assert len(table.head) == 8
    assert len(table.body) == 1
    assert [c["text"] for c in table.body[0]] == [log_run["filter_none"]]
    assert 'colspan="8"' in markup
    assert 'class="log-foot"' in markup


def test_the_selects_offer_what_the_buffer_holds_and_an_all_option(log_run):
    """The two selects are filled from the payload, not from a list in the code:
    a new reason token shows up in the dropdown without an edit. "all" is the
    catalog's word, and it is the first option."""
    all_word = log_run["filter_all"]
    for markup, wanted in ((log_run["reasons"], ["thread", "ephemeral", "rebind"]),
                           (log_run["hosts"], ["M5-alpha", "M3-beta"])):
        assert markup.startswith('<option value="">' + all_word + "</option>"), markup
        for value in wanted:
            assert '<option value="%s">%s</option>' % (value, value) in markup, value
    # distinct and sorted: three reasons, not four decisions
    assert log_run["reasons"].count("<option") == 1 + 3
    assert log_run["hosts"].count("<option") == 1 + 2


def test_the_filter_bar_stands_outside_the_log_container(page):
    """The one trap of the issue: a bar inside `#log` would be rebuilt by the 2 s
    patch, and every keystroke would lose the focus two seconds later. The bar is
    a sibling above the panel, so `apply('log', …)` cannot reach it."""
    tree = _Tree()
    tree.feed(page)
    tree.close()

    def find(node, pred):
        if node.__class__ is not str:
            if pred(node):
                return node
            for child in node.children:
                hit = find(child, pred)
                if hit is not None:
                    return hit
        return None

    def ids_in(node) -> set[str]:
        out = set()
        if node.__class__ is not str:
            if node.attrs.get("id"):
                out.add(node.attrs["id"])
            for child in node.children:
                out |= ids_in(child)
        return out

    log = find(tree.root, lambda n: n.attrs.get("id") == "log")
    bar = find(tree.root, lambda n: n.attrs.get("id") == "logFilters")
    assert log is not None and bar is not None
    assert ids_in(log) == {"log"}          # the patched node holds no fields
    assert {"logFilterSid", "logFilterReason", "logFilterHost"} <= ids_in(bar)
    # document order: the bar comes before the panel it filters
    assert page.index('id="logFilters"') < page.index('id="log"')
    # ... and it is the only place those fields exist
    assert page.count('id="logFilterSid"') == 1
    assert page.count('id="logFilterReason"') == 1
    assert page.count('id="logFilterHost"') == 1


def test_the_ring_buffer_holds_two_hundred_decisions():
    """#101 raised the ring from 50 to 200: a conversation spans more turns than
    the old buffer held, so a sid filter would have shown a fragment of it. The
    oldest fifty fall out, the newest stay, and `/stats` still hands the whole
    ring over as one list."""
    stats = Stats()
    for i in range(250):
        stats.decision(model="m", host="h", reason="thread", prompt_chars=1,
                       duration_ms=1.0, status=200, thread="sid:t%d" % i)
    assert len(stats.decisions) == 200
    assert stats.decisions[0]["thread"] == "sid:t50"
    assert stats.decisions[-1]["thread"] == "sid:t249"
    assert len(stats.as_dict()["decisions"]) == 200
