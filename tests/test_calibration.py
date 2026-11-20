"""The calibration engine (wave W2a): storage, fit, estimate.

These tests are the specification of docs/loading-state.md §6.4/§6.5 — they run
without a host, a stub or a network, because the engine must be trustworthy on
its own before an admin endpoint calls it.
"""

from __future__ import annotations

import os
import stat

import pytest
import yaml

from ripple.calibration import (DEFAULT_SAMPLES, MIN_ESTIMATE_SECONDS, Calibration,
                               estimate, fit, load, record, samples_for, save,
                               source_for, worst_slope)


DEFAULTS = list(DEFAULT_SAMPLES)          # [(111.6, 24.7), (453.6, 131.0)]


def _node(cal: Calibration, node_id: str, samples, **kwargs) -> Calibration:
    record(cal, node_id, samples, by="test", when="2026-11-15T08:00:00+01:00",
           **kwargs)
    return cal


# -- estimate: interpolation, extrapolation, factor, clamp ------------------
def test_estimate_reproduces_a_measured_sample():
    cal = Calibration()
    # 111,6 GB measured 24,7 s; the safety factor is the only change.
    assert estimate(cal, "nid", 111.6, safety_factor=1.0) == pytest.approx(24.7)
    assert estimate(cal, "nid", 111.6, safety_factor=1.3) == pytest.approx(32.1)


def test_estimate_interpolates_between_two_samples():
    cal = Calibration()
    # halfway between 111,6 and 453,6 GB
    middle = (111.6 + 453.6) / 2
    expected = 24.7 + (131.0 - 24.7) / 2
    assert estimate(cal, "nid", middle, safety_factor=1.0) == pytest.approx(expected, abs=0.1)


def test_estimate_extrapolates_with_the_worst_slope():
    cal = Calibration()
    slope = worst_slope(DEFAULTS)                     # 0.3108 s/GB
    expected = 131.0 + (600.0 - 453.6) * slope
    assert estimate(cal, "nid", 600.0, safety_factor=1.0) == pytest.approx(expected, abs=0.1)
    # the worst slope is the steepest segment, never the linear-fit slope
    assert slope > fit(DEFAULTS)["slope"]


def test_estimate_applies_the_safety_factor():
    cal = Calibration()
    plain = estimate(cal, "nid", 453.6, safety_factor=1.0)
    safe = estimate(cal, "nid", 453.6, safety_factor=2.0)
    assert safe == pytest.approx(plain * 2.0, abs=0.2)


def test_estimate_is_clamped_to_the_ceiling_and_the_floor():
    cal = Calibration()
    assert estimate(cal, "nid", 5000.0, safety_factor=1.0, max_seconds=120.0) == 120.0
    # a tiny model must not produce "0 s" — below the floor a load is not a state
    assert estimate(cal, "nid", 1.0, safety_factor=1.0) == MIN_ESTIMATE_SECONDS


def test_unknown_size_uses_the_largest_measured_load():
    cal = Calibration()
    expected = max(seconds for _, seconds in DEFAULTS) * 1.3
    assert estimate(cal, "nid", None, safety_factor=1.3) == pytest.approx(expected, abs=0.1)


def test_single_sample_extrapolates_with_the_worst_known_slope():
    cal = _node(Calibration(), "nid", [(100.0, 20.0)])
    # A single sample has no segment, so its own average (0,2 s/GB) is all it
    # knows — and §6.5 says that is not enough: extrapolating with 0,2 would
    # predict 40 s for 200 GB. The default curve's steepest segment is the
    # honest bound, because a too-short estimate puts a probe inside the load.
    assert worst_slope([(100.0, 20.0)]) == pytest.approx(0.2)
    default_worst = worst_slope(list(DEFAULT_SAMPLES))
    expected = 20.0 + (200.0 - 100.0) * default_worst
    assert estimate(cal, "nid", 200.0, safety_factor=1.0) == pytest.approx(expected, abs=0.1)
    assert expected > 40.0


def test_a_non_monotone_sample_set_never_predicts_less_for_a_bigger_model():
    # 200 GB "faster" than 100 GB is a measurement artefact (warm cache, a
    # concurrent load). Using it would predict a load that is too short.
    cal = _node(Calibration(), "nid", [(100.0, 30.0), (200.0, 25.0), (400.0, 60.0)])
    small = estimate(cal, "nid", 150.0, safety_factor=1.0)
    big = estimate(cal, "nid", 350.0, safety_factor=1.0)
    assert big > small
    # the 200 GB sample is raised to the 100 GB value, so 150 GB costs 30 s
    assert small == pytest.approx(30.0, abs=0.5)


# -- fit is a report, not the model -----------------------------------------
def test_fit_reports_the_negative_intercept_instead_of_hiding_it():
    result = fit(DEFAULTS)
    assert result["points"] == 2
    assert result["slope"] == pytest.approx(0.311, abs=0.002)
    assert result["intercept"] == pytest.approx(-10.0, abs=0.5)
    assert result["linearity"] is not None


def test_fit_of_a_single_sample_has_no_regression():
    result = fit([(100.0, 20.0)])
    assert result["slope"] is None and result["intercept"] is None
    assert result["worst_slope"] == pytest.approx(0.2)


# -- storage ----------------------------------------------------------------
def test_save_and_load_roundtrip_is_keyed_by_node_id(tmp_path):
    path = tmp_path / "sub" / "calibration.yaml"
    cal = Calibration()
    record(cal, "22222222-2222-4222-8222-222222222222",
           [(111.6, 24.7), (453.6, 131.0)], by="dashboard",
           when="2026-11-15T07:20:00+01:00", models=["big", "small"])
    save(path, cal)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert list(raw["nodes"]) == ["22222222-2222-4222-8222-222222222222"]
    assert raw["defaults"]["source"] == "m3u-conservative"
    assert raw["nodes"]["22222222-2222-4222-8222-222222222222"]["models"] == ["big", "small"]

    again = load(path)
    assert samples_for(again, "22222222-2222-4222-8222-222222222222") == \
        [(111.6, 24.7), (453.6, 131.0)]
    assert source_for(again, "22222222-2222-4222-8222-222222222222") == "measured"


def test_the_file_is_written_with_mode_0600(tmp_path):
    path = tmp_path / "calibration.yaml"
    save(path, Calibration())
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_calibration_survives_a_rename_of_the_host(tmp_path):
    # The key is the node_id (FA-21), so the samples follow the node, not the
    # label an operator may change at any time.
    path = tmp_path / "calibration.yaml"
    cal = _node(Calibration(), "nid-1", [(111.6, 30.0)])
    save(path, cal)

    again = load(path)
    assert samples_for(again, "nid-1") == [(111.6, 30.0)]
    assert source_for(again, "nid-1") == "measured"
    # an unknown node (a new one, or the same machine under a new id) is default
    assert samples_for(again, "nid-2") == DEFAULTS
    assert source_for(again, "nid-2") == "default"
    assert estimate(again, "nid-2", 111.6, safety_factor=1.0) == pytest.approx(24.7)


def test_a_missing_file_means_defaults(tmp_path):
    cal = load(tmp_path / "does-not-exist.yaml")
    assert cal.nodes == {}
    assert cal.defaults == DEFAULTS
    assert source_for(cal, "nid") == "default"


def test_a_corrupt_file_does_not_raise(tmp_path):
    path = tmp_path / "calibration.yaml"
    path.write_text("nodes: [this is not a mapping", encoding="utf-8")
    cal = load(path)
    assert cal.defaults == DEFAULTS
    assert source_for(cal, "nid") == "default"


def test_entries_without_usable_samples_are_ignored(tmp_path):
    path = tmp_path / "calibration.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "nodes": {
            "good": {"samples": [{"size_gb": 100.0, "seconds": 20.0}]},
            "empty": {"samples": []},
            "garbage": {"samples": [{"size_gb": "x", "seconds": None}]},
            "not-a-mapping": "nope",
        },
    }), encoding="utf-8")
    cal = load(path)
    assert list(cal.nodes) == ["good"]
    assert samples_for(cal, "good") == [(100.0, 20.0)]
    assert source_for(cal, "garbage") == "default"
