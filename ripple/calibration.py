"""Per-node load calibration: storage, fit and the load-time estimate (§6).

The load rate is a property of the node's storage path, not of RIPPLE. An M5
loading from an external SSD is roughly twice as slow as one loading from its
internal disk, so a single hard-coded rate would be wrong for every setup but
one. Each node therefore carries its own samples; an uncalibrated node falls
back to the two measurements from the AS-5 night run (`m3u-conservative`).

Pure logic and storage: no FastAPI, no httpx, no import of `ripple.hosts`. The
admin endpoints (wave W3) are the only caller that talks to a node.

Why not a linear fit: the two measured points (111,6 GB → 24,7 s and 453,6 GB →
131 s) give a *negative* intercept of −10,0 s, which is not physical. Allocation
and materialisation grow with the model, so the curve is convex over this range.
`fit()` therefore reports the regression for the operator but never uses it;
`estimate()` interpolates monotonically through the samples and extrapolates
with the worst observed slope. Too long is harmless — the probes stay off a
little longer; too short is not, because a probe would land inside the load.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

log = logging.getLogger("ripple.calibration")

# The AS-5 night run, 15.11.2026 01:00–01:03 on M3-beta (docs/loading-state.md §3).
DEFAULT_SAMPLES: list[tuple[float, float]] = [(111.6, 24.7), (453.6, 131.0)]
DEFAULT_SOURCE = "m3u-conservative"

# A load shorter than this is not worth a special state: startup, model
# materialisation and the probe interval itself dominate below ~10 s.
MIN_ESTIMATE_SECONDS = 10.0

VERSION = 1


@dataclass
class Calibration:
    """Samples per node, keyed by `node_id` (FA-21), never by host name.

    A node keeps its calibration when it is renamed or gets a new address; a host
    name is a label an operator may change at any time.
    """

    defaults: list[tuple[float, float]] = field(
        default_factory=lambda: list(DEFAULT_SAMPLES))
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    path: Path | None = None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def load(path: str | Path) -> Calibration:
    """Read the calibration file. A missing or broken file is the defaults.

    Broken is not fatal on purpose: a corrupt calibration must never keep RIPPLE
    from starting. It is logged, and the node simply stays on the conservative
    default curve.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return Calibration(path=p)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("calibration_unreadable path=%s error=%s — using defaults", p, exc)
        return Calibration(path=p)
    if not isinstance(raw, dict):
        log.warning("calibration_invalid path=%s — root is not a mapping", p)
        return Calibration(path=p)

    defaults = _samples(raw.get("defaults"))
    cal = Calibration(defaults=defaults or list(DEFAULT_SAMPLES), path=p)
    nodes = raw.get("nodes")
    if isinstance(nodes, dict):
        for node_id, entry in nodes.items():
            if not isinstance(entry, dict):
                continue
            samples = _samples(entry)
            if not samples:
                continue
            cal.nodes[str(node_id)] = {
                "calibrated_at": entry.get("calibrated_at"),
                "calibrated_by": entry.get("calibrated_by"),
                "source": str(entry.get("source") or "measured"),
                "samples": samples,
                "models": [str(m) for m in (entry.get("models") or [])],
            }
    return cal


def save(path: str | Path, cal: Calibration) -> None:
    """Write atomically with mode 0600 — the file is not a secret, but it is
    operator data and must not be world-writable next to a running proxy."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": VERSION,
        "defaults": {
            "source": DEFAULT_SOURCE,
            "samples": [{"size_gb": size, "seconds": seconds}
                        for size, seconds in cal.defaults],
        },
        "nodes": {},
    }
    for node_id, entry in cal.nodes.items():
        payload["nodes"][node_id] = {
            "calibrated_at": entry.get("calibrated_at"),
            "calibrated_by": entry.get("calibrated_by"),
            "source": entry.get("source") or "measured",
            "models": list(entry.get("models") or []),
            "samples": [{"size_gb": size, "seconds": seconds}
                        for size, seconds in entry.get("samples") or []],
        }
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".calibration-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    cal.path = p


def record(cal: Calibration, node_id: str, samples: Iterable[tuple[float, float]],
           *, by: str, when: str, models: Iterable[str] = ()) -> None:
    """Store a completed calibration for one node (in memory; call save())."""
    usable = [(float(s), float(t)) for s, t in samples if float(s) > 0 and float(t) > 0]
    cal.nodes[str(node_id)] = {
        "calibrated_at": when,
        "calibrated_by": by,
        "source": "measured",
        "samples": sorted(usable),
        "models": [str(m) for m in models],
    }


def lock_refusal(node: str, locked: list[str]) -> str:
    """Why a calibration cannot run on `node` while `locked` is resident there.

    A calibration is the one job that loads models on purpose, and a 453 GB load
    makes oMLX evict what it has to — measured on the M5, where the operator's own
    session is exactly such a model. The lock exists for this case (issue #21), so
    the job refuses *before* it unloads anything and names the model, instead of
    starting a run whose first effect is the thing the lock was meant to prevent.

    `force=1` does not override it: `force` is about bound threads, i.e. about a
    prefix cache, and this is about a model's residency.
    """
    names = ", ".join(locked)
    plural = "models" if len(locked) > 1 else "model"
    undo = ", ".join(f"DELETE /admin/models/{m}/lock" for m in locked)
    return (f"{node} has the locked {plural} {names} loaded; a calibration loads "
            f"other models on {node} and oMLX would evict "
            f"{'them' if len(locked) > 1 else 'it'} to make room. Unlock with {undo}, "
            f"or calibrate another node")


def _samples(entry: Any) -> list[tuple[float, float]]:
    """Read `samples: [{size_gb, seconds}]` tolerantly, sorted by size."""
    if not isinstance(entry, dict):
        return []
    raw = entry.get("samples")
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        size_raw = item.get("size_gb")
        seconds_raw = item.get("seconds")
        if size_raw is None or seconds_raw is None:
            continue
        try:
            size = float(size_raw)
            seconds = float(seconds_raw)
        except (TypeError, ValueError):
            continue
        if size <= 0 or seconds <= 0:
            continue
        out.append((size, seconds))
    return _monotone(sorted(out))


def _monotone(samples: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Two samples of the same size keep the slower one; the curve never falls.

    A stored set that claims a bigger model loads faster is a measurement
    artefact (a warm page cache, a concurrent load), and using it would predict a
    load that is too short — the one error this module must not make.
    """
    out: list[tuple[float, float]] = []
    ceiling = 0.0
    for size, seconds in samples:
        seconds = max(seconds, ceiling)
        ceiling = seconds
        if out and out[-1][0] == size:
            out[-1] = (size, max(out[-1][1], seconds))
            continue
        out.append((size, seconds))
    return out


# ---------------------------------------------------------------------------
# Fit and estimate
# ---------------------------------------------------------------------------
def samples_for(cal: Calibration, node_id: str | None) -> list[tuple[float, float]]:
    """This node's samples, else the defaults. `source` follows from it."""
    if node_id:
        entry = cal.nodes.get(str(node_id))
        if entry:
            samples = _monotone(sorted(entry.get("samples") or []))
            if samples:
                return samples
    return _monotone(sorted(cal.defaults))


def source_for(cal: Calibration, node_id: str | None) -> str:
    """`measured` only when this node was calibrated, otherwise `default`."""
    if node_id:
        entry = cal.nodes.get(str(node_id))
        if entry and entry.get("samples"):
            return str(entry.get("source") or "measured")
    return "default"


def worst_slope(samples: list[tuple[float, float]]) -> float:
    """Seconds per GB of the steepest segment — the slope used to extrapolate.

    A single sample has no segment, so it is read as "everything scales like
    this one": seconds / size.
    """
    points = _monotone(sorted(samples))
    if not points:
        return 0.0
    if len(points) == 1:
        size, seconds = points[0]
        return seconds / size if size > 0 else 0.0
    slopes = [(b[1] - a[1]) / (b[0] - a[0])
              for a, b in zip(points, points[1:]) if b[0] > a[0]]
    if not slopes:
        size, seconds = points[-1]
        return seconds / size if size > 0 else 0.0
    return max(slopes)


def fit(samples: list[tuple[float, float]]) -> dict[str, Any]:
    """Least-squares report — for the operator, never for the estimate.

    `intercept` is allowed to come out negative: that is exactly the finding that
    a linear model does not describe a load (docs/loading-state.md §3), and
    hiding it would hide why the estimate interpolates instead.
    """
    points = _monotone(sorted(samples))
    if len(points) < 2:
        return {"points": len(points), "slope": None, "intercept": None,
                "worst_slope": round(worst_slope(points), 4), "linearity": None}
    n = float(len(points))
    sum_x = sum(size for size, _ in points)
    sum_y = sum(seconds for _, seconds in points)
    sum_xx = sum(size * size for size, _ in points)
    sum_xy = sum(size * seconds for size, seconds in points)
    denominator = n * sum_xx - sum_x * sum_x
    slope = (n * sum_xy - sum_x * sum_y) / denominator if denominator else None
    intercept = (sum_y - slope * sum_x) / n if slope is not None else None
    # linearity: worst relative deviation of a sample from the fitted line, so a
    # convex curve is visible as one number instead of a table.
    linearity = None
    if slope is not None and intercept is not None:
        # A zero sample would be a broken measurement, not a data point: it must
        # not take the whole report (and with it the GET) down.
        deviations = [abs(seconds - (intercept + slope * size)) / seconds
                      for size, seconds in points if seconds > 0]
        linearity = round(max(deviations), 4) if deviations else None
    return {
        "points": len(points),
        "slope": round(slope, 4) if slope is not None else None,
        "intercept": round(intercept, 2) if intercept is not None else None,
        "worst_slope": round(worst_slope(points), 4),
        "linearity": linearity,
    }


def estimate(cal: Calibration, node_id: str | None, size_gb: float | None, *,
             safety_factor: float = 1.3, max_seconds: float = 900.0) -> float:
    """Seconds this node needs to load `size_gb`.

    Piecewise-linear and monotone between the samples, extrapolated outside the
    measured range with the worst slope, then the safety factor, then the clamp.
    An unknown size is treated as "the biggest load we have seen here": the
    ceiling is a probe suspension, and guessing small is the only error that
    costs correctness.
    """
    points = samples_for(cal, node_id)
    ceiling = max(float(max_seconds), MIN_ESTIMATE_SECONDS)
    factor = max(float(safety_factor), 1.0)
    if not points:
        return ceiling
    if size_gb is None or size_gb <= 0:
        return _clamp(max(seconds for _, seconds in points) * factor, ceiling)
    slope = worst_slope(points)
    if len(points) == 1:
        # §6.5: one sample cannot show the slope, so extrapolate with the worst
        # of what is known — its own average and the default curve's steepest
        # segment. Its own average alone is the one error that predicts a load
        # that is too short.
        slope = max(slope, worst_slope(list(cal.defaults)))
    seconds = _interpolate(points, float(size_gb), slope)
    return _clamp(seconds * factor, ceiling)


def _interpolate(points: list[tuple[float, float]], size: float,
                 slope: float) -> float:
    if size <= points[0][0]:
        # Below the smallest sample there is no curve, only the worst slope.
        return max(0.0, points[0][1] - (points[0][0] - size) * slope)
    for (size_a, seconds_a), (size_b, seconds_b) in zip(points, points[1:]):
        if size <= size_b:
            if size_b <= size_a:
                return seconds_b
            ratio = (size - size_a) / (size_b - size_a)
            return seconds_a + ratio * (seconds_b - seconds_a)
    return points[-1][1] + (size - points[-1][0]) * slope


def _clamp(seconds: float, ceiling: float) -> float:
    return round(min(max(seconds, MIN_ESTIMATE_SECONDS), ceiling), 1)
