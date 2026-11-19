"""Admin surface for per-node load calibration (docs/loading-state.md §6.2/§6.3).

Mounted from `proxy.py` in the foundation wave (W0) so that `proxy.py` never has
to change again: the router exists once and the dashboard can call it from the
day it is written. W3 fills in the bodies.

Two endpoints, both admin-key protected:

* ``POST /admin/nodes/{name}/calibrate[?force=1]`` — start the job, answer 202
  with the models it will load, the resident ones it has to unload first, and the
  models oMLX may evict. A 453 GB load takes minutes, so the job runs as its own
  task and the HTTP call never waits for it.
* ``GET /admin/nodes/{name}/calibration`` — the stored curve, the fit, the job
  state and a dry-run `plan`. The plan exists because the dashboard's
  confirmation dialog must name what will be loaded and what may be evicted
  *before* the operator confirms; it is computed by the same function as the POST,
  so the dialog cannot promise something the job would not do.

The calibration engine itself lives in `ripple/calibration.py` (pure logic and
storage, no FastAPI, no network); this module owns the conversation with the
node — which models, when a load counts as done, what has to be unloaded again.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import calibration as engine
from .backends import ModelInfo
from .hosts import epoch_seconds

log = logging.getLogger("ripple.calibration_api")

# oMLX's own resolution for the residency poll (§6.3.3). A load shorter than this
# is not worth a curve: startup and materialisation dominate it anyway.
POLL_SECONDS = 0.5
# The unload is a 3 s operation for 453 GB (§3), so this is generous; it is a
# bound, not an expectation.
UNLOAD_TIMEOUT_SECONDS = 15.0
# Largest and smallest must differ by this factor, or the two samples say nothing
# about the slope (§6.3.2).
MIN_SIZE_RATIO = 3.0
# The reload call blocks until the model is resident (oMLX), so this only has to
# catch a node that answered ok and then dropped the model again — it is a
# verification window, not a load ceiling.
RELOAD_VERIFY_SECONDS = 15.0

_TRUE = frozenset({"1", "true", "yes", "on"})


class _CalibrationFailed(Exception):
    """A job that cannot finish for a reason the operator can read."""


class _LoadRefused(_CalibrationFailed):
    """The node refuses to load this model on its own (oMLX answers 409).

    Skippable: a drafter or a broken model file in the allow-list must not take
    the whole calibration down. Verified live on 15.11.2026 with
    `Qwen3.8-Flash-Next-DFlash2` ("81 parameters not in model")."""


@dataclass
class Plan:
    """What a POST would do: what it unloads, what it loads, what may go.

    `models` are the loads to be measured, `unloads` the subset that is resident
    now and has to make room for its own measurement first, `evicts` what oMLX
    may drop on the way. `note` is for the log, not for the JSON: the wire shape
    is models/evicts/error plus `unloads` (added with issue #4; the three
    original keys keep their meaning, contract §7).
    """

    models: list[ModelInfo] = field(default_factory=list)
    evicts: list[str] = field(default_factory=list)
    unloads: list[str] = field(default_factory=list)
    error: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"models": [m.id for m in self.models],
                "evicts": list(self.evicts),
                "unloads": list(self.unloads),
                "error": self.error}


# ---------------------------------------------------------------------------
# Helpers: identity, sizes, time
# ---------------------------------------------------------------------------
def _node_id(host) -> str | None:
    """The FA-21 identity: configured first, then what the node reported."""
    return host.cfg.node_id or host.node_id_seen


def _size_gb(info: ModelInfo) -> float:
    """`estimated_size` in GB — the same number the dashboard shows."""
    return round(float(info.estimated_size or 0) / 1e9, 2)


def _now_iso() -> str:
    """ISO-8601 with the local offset, the shape the calibration file stores."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


async def _residency(host) -> dict[str, ModelInfo]:
    """The node's current view, fetched fresh where the backend allows it.

    `host.models` is only as fresh as the last refresh tick (10 s live, an hour
    in a test), and the model choice depends on what is loaded *now*.
    """
    if host.backend is None:
        return dict(host.models or {})
    try:
        residency = await host.backend.residency()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        log.warning("calibration_residency_failed node=%s error=%s", host.cfg.name, exc)
        residency = None
    if residency is None:
        return dict(host.models or {})
    host.models = residency
    return residency


# ---------------------------------------------------------------------------
# Model choice (§6.3.2) and the eviction forecast (§6.3.5)
# ---------------------------------------------------------------------------
def _size_ratio(models: list[ModelInfo]) -> float:
    """Largest / smallest `estimated_size` in a candidate set (>= 1.0)."""
    sizes = [float(m.estimated_size or 0) for m in models]
    if len(sizes) < 2:
        return 0.0
    smallest, largest = min(sizes), max(sizes)
    return largest / smallest if smallest > 0 else 0.0


def _pick_models(host, residency: dict[str, ModelInfo], entry_loaded: set[str],
                 wanted: int) -> Plan:
    """Largest and smallest usable model on this node, plus one in between.

    Candidates are the node's allow-list (everything it reports when the list is
    empty) minus models with no size, because the curve is seconds per GB.

    A *resident* model is not a candidate while two models that are not loaded
    span the range: measuring it means unloading it first, and that takes the
    residency away from the node for the length of the job. When nothing else is
    left — the M5's allow-list holds exactly two models and both are normally
    resident, so the plan used to answer 409 and the operator had to unload by
    hand (`docs/loading-state-live-2026-11-15.md` §10) — such a model is taken
    anyway: the job unloads it, measures the load and puts it back (`_reload`).

    The entry host's models are dropped where that still leaves two: those carry
    the operator's session, and loading one on this node is the cheapest way to
    evict it. A preference, not a rule — a fleet with two nodes and one session
    model still needs a calibration.
    """
    allow = host.cfg.models
    usable = [m for m in residency.values()
              if (not allow or m.id in allow) and (m.estimated_size or 0) > 0]
    # Size first, so largest/smallest are the extremes; `loaded` only breaks a
    # tie in favour of the model the job does not have to unload.
    usable.sort(key=lambda m: (m.estimated_size or 0, m.loaded))
    fresh = [m for m in usable if not m.loaded]
    pool = fresh if len(fresh) >= 2 and _size_ratio(fresh) >= MIN_SIZE_RATIO else usable
    if len(pool) < 2:
        return Plan(error=(f"{host.cfg.name} offers {len(pool)} usable model(s); "
                           f"a calibration needs at least two"))
    if len(pool) >= 2 and entry_loaded:
        # The entry host's models carry the operator's session, so they are the
        # ones we would rather not load. But the preference must not destroy the
        # spread: on M3-beta it removed the only small model (6,5 GB, resident on
        # the M5) and left 111,6 vs 159,2 GB — a 1,4× range, which says nothing
        # about the slope. Drop them only while what remains still spans 3×.
        preferred = [m for m in pool if m.id not in entry_loaded]
        if len(preferred) >= 2 and _size_ratio(preferred) >= MIN_SIZE_RATIO:
            pool = preferred
    smallest, largest = pool[0], pool[-1]
    ratio = _size_ratio(pool)
    if ratio < MIN_SIZE_RATIO:
        return Plan(error=(f"the usable models on {host.cfg.name} differ by only "
                           f"{ratio:.1f}× in size; a calibration needs at least "
                           f"{MIN_SIZE_RATIO:.0f}×"))

    chosen = [largest, smallest]
    note = None
    if wanted >= 3 and len(pool) > 2:
        # The geometric mean is the size where a fixed cost would show up: it is
        # the middle of the curve, not the middle of the range.
        middle = math.sqrt(float(smallest.estimated_size or 0) *
                           float(largest.estimated_size or 0))
        rest = [m for m in pool if m.id not in (smallest.id, largest.id)]
        chosen.append(min(rest, key=lambda m: abs(float(m.estimated_size or 0) - middle)))
    elif wanted >= 3:
        note = (f"only two usable models on {host.cfg.name}: measuring two sizes "
                f"instead of {wanted}")
    unloads = [m.id for m in chosen if m.loaded]
    if unloads:
        note = ((note + "; ") if note else "") + \
            f"resident and unloaded for the measurement: {', '.join(unloads)}"
    return Plan(models=chosen, unloads=unloads,
                evicts=_forecast_evicts(host, residency, chosen, unloads),
                note=note)


def _forecast_evicts(host, residency: dict[str, ModelInfo], chosen: list[ModelInfo],
                     unloads: list[str] | None = None) -> list[str]:
    """What oMLX would drop to fit the calibration models — read-only.

    Same arithmetic as `placement.residency_tier` (FA-18): oMLX's own
    `estimated_size` of everything loaded plus the models we are about to load,
    against `hard_threshold × ceiling`, evicting in LRU order until it fits.
    Nothing is evicted by us; the job only *reports* what actually disappeared.

    Models in `unloads` leave residency before the first load, so they do not
    count against the ceiling twice — the forecast would otherwise predict an
    eviction that the job's own unload has already made unnecessary.
    """
    ceiling = host.ceiling_bytes
    if not ceiling:
        return []
    gone = set(unloads or ())
    loaded = [m for m in residency.values() if m.loaded and m.id not in gone]
    target = host.cfg.hard_threshold * ceiling
    remaining = sum(m.estimated_size or 0 for m in loaded)
    remaining += sum(m.estimated_size or 0 for m in chosen)
    if remaining <= target:
        return []
    evicts: list[str] = []
    for m in sorted(loaded, key=lambda m: m.last_access or 0.0):
        if remaining <= target:
            break
        evicts.append(m.id)
        remaining -= m.estimated_size or 0
    return evicts


# ---------------------------------------------------------------------------
# The job (§6.3)
# ---------------------------------------------------------------------------
async def _measure(host, model: str, max_seconds: float) -> float:
    """Seconds this node needed for one load.

    oMLX's `/v1/models/{id}/load` blocks until the model is resident, so the wall
    time of the call *is* the load time — and on the instances measured on
    15.11.2026 that is the only usable source: `loading_started_at` there carries
    a monotonic clock value (158021.3 s after boot on M3-beta), not an epoch, and
    it does not move while the load runs. Read as epoch it would store a sample of
    ~1.8 billion seconds. When the field *is* an epoch it is preferred, because it
    excludes our own request latency (`ripple.hosts.epoch_seconds` decides).
    """
    started = time.time()
    # The client timeout must not be shorter than the ceiling the job waits for,
    # or a slow load would be reported as a refusal while it is still running.
    ok = await host.backend.load(model, timeout=max(600.0, float(max_seconds)))
    if not ok:
        raise _LoadRefused(f"the node refused to load {model}")
    deadline = time.monotonic() + max(1.0, float(max_seconds))
    omlx_started: float | None = None
    while True:
        residency = await host.backend.residency()
        now = time.time()
        if residency is not None:
            info = residency.get(model)
            if info is None:
                raise _CalibrationFailed(f"{model} disappeared from the node while loading")
            if omlx_started is None:
                omlx_started = epoch_seconds(info.loading_started_at, now, max_seconds)
            if info.loaded:
                if omlx_started is not None:
                    return round(max(0.0, now - omlx_started), 1)
                log.info("calibration_timestamp_unusable node=%s model=%s — measuring "
                         "from the load call", host.cfg.name, model)
                # From issuing the call to the first sighting of `loaded`. This is
                # right whether the load call blocks (oMLX does) or returns as soon
                # as the load is queued — the wall time of the call alone would be
                # 0 in the second case.
                return round(max(0.0, now - started), 1)
        if time.monotonic() >= deadline:
            raise _CalibrationFailed(f"load of {model} did not finish within "
                                     f"{float(max_seconds):.0f} s")
        await asyncio.sleep(POLL_SECONDS)


async def _restore(host, models: list[str]) -> list[str]:
    """Unload every calibration model and verify. Returns what would not go.

    A model the job did not load is never touched: the previous loaded set is
    restored "as far as it can" (§9) — bringing back a model oMLX evicted would
    be a load RIPPLE causes for no request, which NZ-4 forbids.
    """
    if host.backend is None or not models:
        return list(models)
    for model in models:
        try:
            await host.backend.unload(model)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.warning("calibration_unload_failed node=%s model=%s error=%s",
                        host.cfg.name, model, exc)
    deadline = time.monotonic() + UNLOAD_TIMEOUT_SECONDS
    while True:
        residency = await host.backend.residency()
        if residency is not None:
            host.models = residency
            still = [m for m in models
                     if residency.get(m) is not None and residency[m].loaded]
            if not still:
                return []
        else:
            still = list(models)
        if time.monotonic() >= deadline:
            return still
        await asyncio.sleep(POLL_SECONDS)


async def _reload(host, models: list[str], max_seconds: float) -> list[str]:
    """Load back the models the job unloaded before measuring. Returns failures.

    The mirror image of `_restore`: a model that was resident when the job
    started has to be resident when it ends, or the calibration left the node
    worse than it found it. It is only ever called for models this job unloaded
    itself — `plan.unloads` — never for one oMLX evicted on its own.

    Each load is supervised like the measured ones (`begin_loading`): the node is
    loading a 453 GB model, and that is exactly the state the loading flag and
    the suspended probes exist for.
    """
    if host.backend is None or not models:
        return list(models)
    failed: list[str] = []
    for model in models:
        info = (host.models or {}).get(model)
        size_gb = _size_gb(info) if info is not None else None
        host.begin_loading(model, size_gb=size_gb,
                           eta_seconds=host.estimate_load_seconds(size_gb),
                           source="calibration_restore")
        try:
            ok = await host.backend.load(model, timeout=max(600.0, float(max_seconds)))
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.warning("calibration_reload_failed node=%s model=%s error=%s",
                        host.cfg.name, model, exc)
            ok = False
        finally:
            host.end_loading("calibration_restore_done")
        if not ok:
            failed.append(model)
            continue
        log.info("calibration_reload node=%s model=%s", host.cfg.name, model)

    # The load call blocks on oMLX, but a node that answered ok and then dropped
    # the model again must not be reported as restored: verify like `_restore`.
    deadline = time.monotonic() + max(1.0, min(float(max_seconds), RELOAD_VERIFY_SECONDS))
    while True:
        residency = await host.backend.residency()
        missing = [m for m in models if m not in failed]
        if residency is not None:
            host.models = residency
            missing = [m for m in missing
                       if residency.get(m) is None or not residency[m].loaded]
        if not missing:
            return failed
        if time.monotonic() >= deadline:
            log.warning("calibration_reload_incomplete node=%s models=%s",
                        host.cfg.name, ",".join(missing))
            return failed + missing
        await asyncio.sleep(POLL_SECONDS)


async def _run_job(ripple, host, name: str, job: dict[str, Any],
                   plan: Plan) -> None:
    """Load each chosen model, measure it, unload, store. Never raises.

    The job owns the node for its duration: one at a time per node, and the
    host's probes are suspended for each load exactly like a cold load RIPPLE
    caused itself, because that is what it is.

    A model that is already resident cannot be measured — it would take zero
    seconds — so it is unloaded first and loaded back once the curve is written
    (`plan.unloads`, issue #4). That is the only case in which this job takes a
    model out of a node without a request having asked for it, which is why
    `restore_failed` exists: whatever does not come back is reported in the job
    and in the log, and the node's residency is part of the job's result.
    """
    ld = ripple.cfg.placement.loading
    chosen = [m.id for m in plan.models]
    # Only what this job actually asked the node to load is ever unloaded again.
    attempted: list[str] = []
    # Models that were resident when the job started and had to go for their own
    # measurement. They are loaded back in the `finally` block, after the curve
    # is stored and before the node leaves the maintenance window.
    unloaded_before: list[str] = []
    before: set[str] = set()
    # `ok` is the measurement, `error` the job's verdict: the job is only
    # settled after its cleanup, so both are read there, not in the handlers.
    ok = False
    error: str | None = None
    restore_error: str | None = None
    cancelled = False
    samples: list[tuple[float, float]] = []
    measured: list[str] = []          # the models behind `samples`, same order
    node_id = _node_id(host)
    # The job owns the node: while it runs, no new work may be routed here. That
    # is the operator's maintenance flag, set in memory only — a window the
    # operator declared stays untouched, and this one never reaches the file
    # (`save()` skips `by="calibration"`), so a crash cannot leave the node out.
    operator_window = host.maintenance
    if operator_window is None:
        ripple.maintenance.set(name, by="calibration", note=f"job {job.get('id', '')}")
        ripple._apply_windows()
        log.info("calibration_maintenance node=%s (out of service for the job)", name)
    try:
        if host.backend is None:
            raise _CalibrationFailed("the node has no backend (it is not resolved)")
        store = ripple.fleet.calibration
        if store is None:
            raise _CalibrationFailed("no calibration store is loaded")
        if plan.note:
            log.warning("calibration_models node=%s note=%s", name, plan.note)
        # The plan is a snapshot from the POST; the job asks the node once more,
        # because a model may have been loaded or evicted in between and
        # "resident right now" is what decides whether it has to be unloaded
        # before it can be measured.
        residency = await _residency(host)
        resident_now = {m.id for m in residency.values() if m.loaded}
        before = set(resident_now)
        for info in plan.models:
            size_gb = _size_gb(info)
            if info.id in resident_now:
                # A resident model measures zero seconds: it has to leave the
                # node before the load that is to be timed. `_reload` brings it
                # back, so the node ends up as resident as it was found.
                job["step"] = f"unload {info.id}"
                left = await _restore(host, [info.id])
                if left:
                    raise _CalibrationFailed(
                        f"could not unload {', '.join(left)} before measuring it")
                resident_now.discard(info.id)
                unloaded_before.append(info.id)
                log.info("calibration_unload_before node=%s model=%s", name, info.id)
            job["step"] = f"load {info.id}"
            attempted.append(info.id)
            host.begin_loading(info.id, size_gb=size_gb,
                               eta_seconds=host.estimate_load_seconds(size_gb),
                               source="calibration")
            try:
                seconds = await _measure(host, info.id, ld.max_seconds)
            except _LoadRefused as exc:
                # A model the node will not load on its own (a drafter, a broken
                # file) is skipped, not fatal: the sizes that do load still make a
                # curve, and aborting would waste the loads already paid for.
                host.end_loading("calibration_skipped")
                job["skipped"].append({"model": info.id, "reason": str(exc)})
                log.warning("calibration_model_skipped node=%s model=%s reason=%s",
                            name, info.id, exc)
                continue
            except BaseException:
                host.end_loading("calibration_failed")
                raise
            host.end_loading("calibration_done")
            samples.append((size_gb, seconds))
            measured.append(info.id)
            log.info("calibration_sample node=%s model=%s size_gb=%.2f seconds=%.1f",
                     name, info.id, size_gb, seconds)

        job["step"] = "unload"
        leftovers = await _restore(host, attempted)
        if leftovers:
            raise _CalibrationFailed("could not unload " + ", ".join(leftovers))
        if len(samples) < 2:
            detail = ("; skipped: " + ", ".join(s["model"] for s in job["skipped"])
                      if job["skipped"] else "")
            raise _CalibrationFailed(
                f"only {len(samples)} load(s) could be measured; a curve needs two{detail}")

        # Samples and their model names must stay aligned: the GET maps them by
        # index, and a skipped model must not shift the table.
        pairs = sorted(zip(samples, measured), key=lambda item: item[0][0])
        # Re-read the file before writing: another instance (a second RIPPLE on
        # the same machine, or a hand edit) may have calibrated a different node
        # since this process started, and saving our in-memory copy would drop it.
        path = ripple.cfg.placement.loading.calibration_file
        fresh = engine.load(path)
        if fresh is not None and fresh is not store:
            store.nodes.update(fresh.nodes)
        engine.record(store, str(node_id), [pair for pair, _ in pairs],
                      by="dashboard", when=_now_iso(),
                      models=[model for _, model in pairs])
        engine.save(path, store)
        ripple.stats.calibrations_ok += 1
        # `state` and `step` are set at the end of the `finally` block: the job
        # is not done while the models it unloaded are still coming back.
        ok = True
        log.info("calibration_done node=%s node_id=%s samples=%d models=%s",
                 name, node_id, len(samples), ",".join(chosen))
    except _CalibrationFailed as exc:
        error = str(exc)
        log.warning("calibration_failed node=%s error=%s", name, exc)
    except asyncio.CancelledError:
        cancelled = True
        error = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — a job must never take the loop down
        log.exception("calibration_crashed node=%s", name)
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            host.end_loading("calibration_done")   # idempotent
            if not ok and host.backend is not None:
                try:
                    leftovers = await _restore(host, attempted)
                except Exception:  # noqa: BLE001 — cleanup must not mask the reason
                    log.warning("calibration_restore_failed node=%s", name)
                    leftovers = list(attempted)
                if leftovers:
                    # A model that would not unload keeps occupying the node, and
                    # the operator has to know — the job's own error is already set.
                    job["restore_failed"] = leftovers
                    restore_error = f"could not unload: {', '.join(leftovers)}"
            # What the job took out of residency goes back in, before the node
            # leaves the maintenance window: no request may land while a 453 GB
            # model is still loading. A failure here does not undo the
            # measurement — the curve is already stored — but it is reported,
            # because the node is not what it was.
            if unloaded_before and host.backend is not None:
                if ok:
                    job["step"] = "restore"
                try:
                    reload_failed = await _reload(host, unloaded_before, ld.max_seconds)
                except Exception:  # noqa: BLE001 — cleanup must not mask the reason
                    log.warning("calibration_reload_crashed node=%s", name)
                    reload_failed = list(unloaded_before)
                if reload_failed:
                    job["restore_failed"] = list(dict.fromkeys(
                        [*(job.get("restore_failed") or []), *reload_failed]))
                    note = f"could not reload: {', '.join(reload_failed)}"
                    restore_error = f"{restore_error} ({note})" if restore_error else note
                    log.warning("calibration_restore_failed node=%s models=%s",
                                name, ",".join(reload_failed))
            # Back into service — unless the operator had already taken the node
            # out for a reason of their own, in which case that window stays.
            if operator_window is None and ripple.maintenance.clear(name):
                ripple._apply_windows()
                log.info("calibration_maintenance_clear node=%s", name)
            after = {m.id for m in (host.models or {}).values() if m.loaded}
            # What the forecast did not predict: loaded before the job, gone
            # after it, and not one of the models we unloaded ourselves.
            job["evicted_observed"] = sorted(before - after - set(chosen))
        finally:
            # The job is settled only when its cleanup is: a GET must not report
            # `done` while the node is still restoring its residency, and it must
            # not report `failed` while a model is still being loaded back.
            # A failed restore does not fail the job — the curve is measured and
            # stored — but it is reported in `restore_failed` and here.
            if error is None:
                job["state"] = "done"
                job["step"] = "done"
                job["error"] = restore_error
            else:
                job["state"] = "failed"
                job["step"] = "cancelled" if cancelled else "failed"
                job["error"] = (f"{error} ({restore_error})" if restore_error
                                else error)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def build_router(ripple) -> APIRouter:
    """`ripple` is the `Ripple` instance: it carries fleet, cfg, registry, stats."""
    router = APIRouter()
    # One job per node: `jobs` keeps the last job (the GET reports it after the
    # fact too), `tasks` only the running one.
    jobs: dict[str, dict[str, Any]] = {}
    tasks: dict[str, asyncio.Task] = {}

    def _conflict(message: str) -> JSONResponse:
        return JSONResponse({"error": {"message": message}}, status_code=409)

    def _unknown(name: str) -> JSONResponse:
        return JSONResponse({"error": {"message": f"unknown node {name!r}"}},
                            status_code=404)

    async def _plan_for(host) -> Plan:
        if host.backend is None:
            return Plan(error=f"{host.cfg.name} is not resolved; there is nothing "
                              f"to load on")
        residency = await _residency(host)
        entry = ripple.fleet.entry_host()
        entry_loaded = {m.id for m in (entry.models or {}).values() if m.loaded} \
            if entry is not None else set()
        return _pick_models(host, residency, entry_loaded,
                            ripple.cfg.placement.loading.samples)

    @router.post("/admin/nodes/{name}/calibrate")
    async def calibrate(name: str, request: Request) -> JSONResponse:
        denied = ripple._require_admin(request)
        if denied is not None:
            return denied
        host = ripple.fleet.get(name)
        if host is None:
            return _unknown(name)

        force = (request.query_params.get("force") or "").strip().lower() in _TRUE
        # `force` only ever overrides the thread check (§9). Saying so in the
        # refusal keeps the operator from trying it again for the wrong reason.
        not_overridable = (" (force=1 was given but does not override this)"
                           if force else "")
        running = tasks.get(name)
        if running is not None and not running.done():
            # Keyed on the task, not on the state string: the job sets
            # `state = "done"` before its cleanup runs, so a second POST could
            # otherwise start while the first one is still unloading models.
            return _conflict(f"a calibration is already running on {name}"
                             f"{not_overridable}")
        if not host.in_pool:
            return _conflict(f"{name} is not in the pool (state {host.status}); "
                             f"calibrating a node that carries no traffic measures "
                             f"a machine that may be gone{not_overridable}")
        if not host.healthy:
            # §6.3.1: an unreachable or unhealthy node measures nothing but its
            # own failure. `healthy` is the last probe verdict and stays true for
            # a host whose eviction guard is off.
            return _conflict(f"{name} is not healthy (last error: "
                             f"{host.last_error or 'unknown'}); a calibration on a "
                             f"node that cannot answer is not a measurement"
                             f"{not_overridable}")
        held = ripple.registry.slots_on_host(name)
        if held > 0 and not force:
            return _conflict(f"{name} holds {held} bound thread(s); pass force=1 to "
                             f"calibrate anyway (that session may lose its prefix "
                             f"cache)")
        # The model lock (issue #21): a calibration loads models on purpose, and
        # the load would take a locked model's memory away. Checked here, before
        # the plan touches the node and long before the job unloads anything, and
        # not overridable by `force` — that flag is about a prefix cache.
        locked = host.locked_loaded()
        if locked:
            return _conflict(engine.lock_refusal(name, locked) + not_overridable)

        plan = await _plan_for(host)
        if plan.error:
            return _conflict(plan.error)

        job: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "state": "running",
            "step": "starting",
            "started_at": _now_iso(),
            "error": None,
            "models": [m.id for m in plan.models],
            "evicts": list(plan.evicts),
            # Resident models the job takes out of the node to measure them; it
            # loads them back, and what will not come back lands in
            # `restore_failed` (issue #4).
            "unloads": list(plan.unloads),
            "restore_failed": [],
            "evicted_observed": [],
            # Models the node refused to load on its own; the job continues
            # without them and the operator sees why.
            "skipped": [],
        }
        jobs[name] = job
        ripple.stats.calibrations_started += 1
        if force and held > 0:
            ripple.stats.calibrations_forced += 1
            log.warning("calibration_forced node=%s threads=%d", name, held)
        task = asyncio.create_task(_run_job(ripple, host, name, job, plan),
                                   name=f"ripple-calibrate-{name}")
        tasks[name] = task
        task.add_done_callback(lambda _t, n=name: tasks.pop(n, None))
        log.info("calibration_started node=%s job=%s models=%s unloads=%s evicts=%s",
                 name, job["id"], ",".join(job["models"]), ",".join(job["unloads"]),
                 ",".join(job["evicts"]))
        return JSONResponse({"job": job["id"], "node": name,
                             "node_id": _node_id(host),
                             "models": list(job["models"]),
                             "unloads": list(job["unloads"]),
                             "evicts": list(job["evicts"])}, status_code=202)

    @router.get("/admin/nodes/{name}/calibration")
    async def calibration(name: str, request: Request) -> JSONResponse:
        denied = ripple._require_admin(request)
        if denied is not None:
            return denied
        host = ripple.fleet.get(name)
        if host is None:
            return _unknown(name)

        store = ripple.fleet.calibration
        node_id = _node_id(host)
        entry: dict[str, Any] = {}
        if store is not None and node_id:
            entry = store.nodes.get(str(node_id)) or {}
        source = engine.source_for(store, node_id) if store is not None else "default"

        samples: list[dict[str, Any]] = []
        stored = entry.get("samples") or []
        if stored:
            models = [str(m) for m in (entry.get("models") or [])]
            samples = [{"model": models[i] if i < len(models) else None,
                        "size_gb": round(float(size), 2),
                        "seconds": round(float(seconds), 1)}
                       for i, (size, seconds) in enumerate(stored)]
        elif store is not None:
            # Uncalibrated: show the curve that is actually in force, so the
            # operator sees what `source: default` means instead of an empty table.
            samples = [{"model": None, "size_gb": round(float(size), 2),
                        "seconds": round(float(seconds), 1)}
                       for size, seconds in engine.samples_for(store, None)]

        curve = engine.samples_for(store, node_id) if store is not None else []
        plan = await _plan_for(host)
        job = jobs.get(name)
        return JSONResponse({
            "node": name,
            "node_id": node_id,
            "source": source,
            "samples": samples,
            "fit": engine.fit(curve),
            "calibrated_at": entry.get("calibrated_at"),
            "calibrated_by": entry.get("calibrated_by"),
            "job": dict(job) if job is not None else None,
            "plan": plan.as_dict(),
        })

    return router
