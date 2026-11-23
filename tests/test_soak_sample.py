"""soak_sample.py: per-run deltas of the cumulative counters (#132).

`/stats` reports `counters`, `by_host` and `by_model` cumulatively for the running
process. The old report summed `by_host` over all 240 samples of 21.11.2026 and
called that a host share: 137,864 "requests" for a day with 4,438 — each request
counted once per sample that followed it, so the share followed the sample count
and the run length instead of the traffic.

The tests pin the three things that must hold: a delta is the difference to the
previous sample of the *same* run, a restart yields `null` instead of a negative
or doubled number, and a two-run fixture with known request counts comes out to
the request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import soak_sample as s


# --- delta_map / derive_delta ------------------------------------------------

def record(at: str, uptime: float | None, by_host: dict | None = None,
           by_model: dict | None = None, counters: dict | None = None,
           reachable: bool = True, ts: float = 0.0) -> dict:
    """A sample the way the sampler writes it, minus the delta."""
    row: dict = {"ts": ts, "at": at, "reachable": reachable}
    if reachable:
        row["uptime_seconds"] = uptime
        row["by_host"] = dict(by_host or {})
        row["by_model"] = dict(by_model or {})
        row["counters"] = dict(counters or {})
    return row


def test_delta_map_subtracts_per_key():
    assert s.delta_map({"a": 14, "b": 6}, {"a": 10, "b": 5}) == {"a": 4, "b": 1}


def test_delta_map_counts_a_new_host_whole():
    assert s.delta_map({"a": 3, "b": 7}, {"a": 3}) == {"a": 0, "b": 7}


def test_delta_map_without_a_predecessor_is_none():
    assert s.delta_map({"a": 3}, None) is None
    assert s.delta_map(None, {"a": 3}) is None


def test_delta_map_refuses_a_value_that_went_backwards():
    assert s.delta_map({"a": 3}, {"a": 10}) is None


def test_derive_delta_of_a_run():
    prev = record("00:05", 300, {"a": 10}, {"m": 10}, {"requests_total": 10})
    cur = record("00:10", 600, {"a": 14}, {"m": 14}, {"requests_total": 14})
    assert s.derive_delta(cur, prev) == {
        "by_host": {"a": 4},
        "by_model": {"m": 4},
        "counters": {"requests_total": 4},
    }


def test_derive_delta_of_the_first_sample_of_a_run_is_none():
    assert s.derive_delta(record("00:05", 300, {"a": 10}), None) is None


def test_derive_delta_after_a_restart_is_none_not_negative():
    """uptime went backwards: the counters belong to a new process."""
    prev = record("08:55", 5.75 * 3600, {"alpha": 415, "beta": 386})
    cur = record("09:01", 2.14 * 3600, {"alpha": 8, "beta": 1})
    assert s.derive_delta(cur, prev) is None


def test_derive_delta_with_uptime_standing_still_is_none():
    prev = record("00:05", 300, {"a": 10})
    assert s.derive_delta(record("00:10", 300, {"a": 12}), prev) is None


def test_derive_delta_behind_an_unreachable_sample_is_none():
    """The run may have restarted in the gap — no delta rather than a guess."""
    prev = record("00:05", 300, {"a": 10}, reachable=False)
    assert s.derive_delta(record("00:10", 600, {"a": 12}), prev) is None


def test_derive_delta_with_a_counter_going_backwards_is_none():
    """Uptime rose but a counter fell: the series is not comparable either way."""
    prev = record("00:05", 300, {"a": 10}, counters={"requests_total": 10})
    cur = record("00:10", 600, {"a": 12}, counters={"requests_total": 4})
    assert s.derive_delta(cur, prev) is None


def test_derive_delta_keeps_a_missing_map_null():
    prev = record("00:05", 300, {"a": 10})
    cur = record("00:10", 600, {"a": 12})
    cur["by_model"] = None
    assert s.derive_delta(cur, prev)["by_model"] is None


# --- sample(): what lands on disk -------------------------------------------

class FakeInstance:
    """Stands in for the two GETs the sampler makes."""

    def __init__(self) -> None:
        self.uptime = 0.0
        self.by_host: dict[str, int] = {}
        self.by_model: dict[str, int] = {}
        self.counters: dict[str, int] = {}

    def __call__(self, url: str) -> dict:
        if url.endswith("/healthz"):
            return {"hosts": [{"name": h, "status": "healthy"} for h in self.by_host],
                    "threads": {"active": 0},
                    "capabilities": {"eviction_guard": True},
                    "warnings": []}
        return {
            "uptime_seconds": self.uptime,
            "by_host": {k: {"requests": v} for k, v in self.by_host.items()},
            "by_model": {k: {"requests": v} for k, v in self.by_model.items()},
            **self.counters,
        }


@pytest.fixture
def instance(monkeypatch) -> FakeInstance:
    fake = FakeInstance()
    monkeypatch.setattr(s, "get", fake)
    return fake


def samples(out_dir: Path) -> list[dict]:
    return s.load(out_dir)


def test_sample_writes_the_delta_of_the_previous_sample(tmp_path, instance):
    instance.uptime, instance.by_host = 300, {"a": 10}
    instance.by_model, instance.counters = {"m": 10}, {"requests_total": 10}
    first = s.sample("http://stub", tmp_path)
    instance.uptime, instance.by_host = 600, {"a": 14, "b": 2}
    instance.by_model, instance.counters = {"m": 16}, {"requests_total": 16}
    second = s.sample("http://stub", tmp_path)

    assert first["delta"] is None
    assert second["delta"] == {
        "by_host": {"a": 4, "b": 2},
        "by_model": {"m": 6},
        "counters": {"requests_total": 6},
    }
    # The cumulative values stay in the sample — the delta is next to them.
    assert second["by_host"] == {"a": 14, "b": 2}


def test_sample_after_a_restart_carries_no_delta(tmp_path, instance):
    instance.uptime, instance.by_host = 20_000, {"a": 415, "b": 386}
    s.sample("http://stub", tmp_path)
    instance.uptime, instance.by_host = 120, {"a": 3, "b": 1}
    restarted = s.sample("http://stub", tmp_path)

    assert restarted["delta"] is None
    for row in samples(tmp_path):
        if row["delta"] is None:
            continue
        for part in row["delta"].values():
            assert all(v >= 0 for v in (part or {}).values())


def test_sample_records_an_unreachable_instance_without_delta(tmp_path, instance, monkeypatch):
    def boom(url: str) -> dict:
        raise OSError("connection refused")

    monkeypatch.setattr(s, "get", boom)
    row = s.sample("http://stub", tmp_path)
    assert row["reachable"] is False
    assert row["delta"] is None


# --- report(): the share is the sum of the deltas ---------------------------

def two_runs(tmp_path: Path, instance: FakeInstance) -> Path:
    """Two runs, 125 requests, known to the request.

    Run 1 starts empty, so its head costs nothing: 30+40 = 70 for alpha, 10+20 = 30
    for beta. Run 2 after the restart: 20 for alpha, 5 for beta. The old code
    summed the cumulative values instead and reported 45 alpha / 20 beta.
    """
    script = [
        (300, {"alpha": 0, "beta": 0}),            # run 1, head
        (600, {"alpha": 30, "beta": 10}),
        (900, {"alpha": 70, "beta": 30}),          # run 1 ends: alpha 70, beta 30
        (60, {"alpha": 0, "beta": 0}),             # restart, head
        (360, {"alpha": 20, "beta": 5}),
    ]
    for uptime, by_host in script:
        instance.uptime = float(uptime)
        instance.by_host = by_host
        instance.by_model = {"m": sum(by_host.values())}
        instance.counters = {"requests_total": sum(by_host.values())}
        s.sample("http://stub", tmp_path)
    return tmp_path


def test_report_sums_deltas_to_the_request(tmp_path, instance, capsys):
    two_runs(tmp_path, instance)
    assert s.report(tmp_path) == 0
    out = capsys.readouterr().out

    assert "alpha 72.0 %" in out
    assert "beta 28.0 %" in out
    assert "host requests: 125" in out
    assert "no delta" not in out  # both run heads carried zero requests


def test_report_shares_add_up_to_a_hundred(tmp_path, instance, capsys):
    two_runs(tmp_path, instance)
    s.report(tmp_path)
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("host share"))
    parts = [float(p.rsplit(" ", 2)[1]) for p in line.split(": ", 1)[1].split(", ")]
    assert sum(parts) == 100.0


def test_report_does_not_double_a_repeated_cumulative_value(tmp_path, instance, capsys):
    """The bug: one request seen by ten samples counted ten times."""
    for uptime, by_host in [(100, {"alpha": 0, "beta": 0}),
                            (200, {"alpha": 6, "beta": 2}),
                            (300, {"alpha": 6, "beta": 2}),
                            (400, {"alpha": 10, "beta": 5}),
                            (500, {"alpha": 10, "beta": 5})]:
        instance.uptime = float(uptime)
        instance.by_host = by_host
        s.sample("http://stub", tmp_path)
    s.report(tmp_path)
    out = capsys.readouterr().out
    # 6+0+4+0 = 10 for alpha, 2+0+3+0 = 5 for beta. Summing the cumulative values
    # instead would say 32 and 14.
    assert "host requests: 15" in out
    assert "alpha 66.7 %" in out and "beta 33.3 %" in out
    assert "sum of 4 deltas" in out


def test_report_reports_the_head_of_a_run_separately(tmp_path, instance, capsys):
    instance.uptime, instance.by_host = 5.75 * 3600, {"alpha": 415, "beta": 386}
    s.sample("http://stub", tmp_path)
    instance.uptime, instance.by_host = 2.14 * 3600, {"alpha": 8, "beta": 1}
    s.sample("http://stub", tmp_path)
    s.report(tmp_path)
    out = capsys.readouterr().out

    assert "no delta     : 810 requests" in out  # both run heads: 415+386, 8+1
    assert "host share" not in out  # a restart alone yields no share


def test_report_derives_deltas_for_files_written_before_the_fix(tmp_path, capsys):
    """Old samples carry no `delta` field; the series still knows the runs."""
    rows = [
        record("00:05", 300, {"a": 10, "b": 5}, {"m": 15}, {"requests_total": 15}, ts=0),
        record("00:10", 600, {"a": 40, "b": 15}, {"m": 55}, {"requests_total": 55}, ts=300),
        record("06:40", 60, {"a": 5, "b": 0}, {"m": 5}, {"requests_total": 5}, ts=600),
        record("06:45", 360, {"a": 25, "b": 5}, {"m": 30}, {"requests_total": 30}, ts=900),
    ]
    path = tmp_path / "samples-2026-11-21.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    assert s.report(tmp_path) == 0
    out = capsys.readouterr().out
    # Run 1: 40-10 = 30 and 15-5 = 10. Run 2 after the restart: 25-5 = 20, 5-0 = 5.
    assert "a 76.9 %" in out and "b 23.1 %" in out
    assert "host requests: 65" in out  # not 80 cumulative
    assert "no delta     : 20 requests" in out  # 15 in the first head, 5 in the second


def test_report_without_samples_says_so(tmp_path, capsys):
    assert s.report(tmp_path) == 1
    assert "no samples" in capsys.readouterr().out


# --- shares(): percentages that add up --------------------------------------

def test_shares_round_without_losing_a_point():
    counts = {"a": 1, "b": 1, "c": 1}
    assert [p for _, p in s.shares(counts)] == [33.4, 33.3, 33.3]
    assert sum(p for _, p in s.shares(counts)) == 100.0


def test_shares_of_nothing_is_empty():
    assert s.shares({}) == []
