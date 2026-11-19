"""The tachograph's arithmetic (issue #83): one derivation, one shape.

`per_thread = rate` and `total = n · per_thread` live in `ripple/stats.py` and
nowhere else — the renderer, the optimizer (#80) and the external sampler all
read the numbers this module produces, so a second formula anywhere would be a
second answer. The tests here are the two claims that make the payload
trustworthy: the product really is the product (also at `n = 0`), and the three
arrays cannot drift apart in length.
"""

from __future__ import annotations

import re

import pytest

from ripple.stats import (BUCKET_SECONDS, SERIES_BUCKETS, SERIES_HOLD_BUCKETS,
                          SERIES_WINDOW_SECONDS, USAGE_FIELDS, USAGE_WINDOW, Bucket,
                          UsageLedger, bucket_index, derive_total, series_entry,
                          series_path, series_points, usage_observation)


# ---------------------------------------------------------------------------
# The derivation
# ---------------------------------------------------------------------------
def test_total_is_the_product_of_streams_and_per_thread_rate():
    """The two lines the issue describes: concurrency up, per-thread rate down."""
    n = [1, 2, 5, 8]
    per_thread = [40.0, 39.4, 30.0, 22.5]
    entry = series_entry("m", n, per_thread)

    assert entry["per_thread"] == per_thread
    assert entry["total"] == [round(n[i] * per_thread[i], 1) for i in range(4)]
    # 1×40, 2×39.4, 5×30, 8×22.5 — the total grows with `n` while the per-thread
    # rate falls. That widening gap *is* the picture the issue asks for.
    assert entry["total"] == [40.0, 78.8, 150.0, 180.0]


def test_zero_streams_give_a_zero_total_and_no_per_thread_rate():
    """`n = 0` is a legal point — but there is no rate *per stream* to report.

    The bucket says nothing ran, so `total` is 0.0 and `per_thread` is a gap.
    Handing a rate along here is the lie from issue #83: `tg_tps` outlives the
    request that measured it, so an idle tick would carry 55.5 tok/s beside
    `n = 0` and `rate_h(n)` (#80) would take that as a measured concurrency.
    """
    entry = series_entry("m", [0], [30.5])

    assert entry["n"] == [0]
    assert entry["total"] == [0.0]
    assert entry["per_thread"] == [None]
    assert entry["per_thread"][0] is None


def test_a_bucket_with_no_stream_never_carries_a_rate():
    """The invariant, over a whole payload: `n == 0` → `per_thread is None`.

    Mixed on purpose — a gap, an idle bucket, a single stream, five streams —
    so the rule is checked where it holds and where it must not fire. Where both
    values exist the identity still holds exactly (`total` is derived, not
    copied).
    """
    n = [None, 0, 1, 5, 0]
    per_thread = [None, 55.5, 40.0, 30.0, 22.0]
    entry = series_entry("m", n, per_thread)

    for i, count in enumerate(entry["n"]):
        if count is None:
            assert entry["per_thread"][i] is None
            assert entry["total"][i] is None
        elif count == 0:
            assert entry["per_thread"][i] is None, f"index {i}: n=0 with a rate"
            assert entry["total"][i] == 0.0
        else:
            assert entry["per_thread"][i] == per_thread[i]
            assert entry["total"][i] == round(count * entry["per_thread"][i], 1)

    assert entry["per_thread"] == [None, None, 40.0, 30.0, None]
    assert entry["total"] == [None, 0.0, 40.0, 150.0, 0.0]


def test_a_rate_without_a_count_is_not_served_as_a_point():
    """A rate that belongs to no bucket is dropped, not attributed to one."""
    entry = series_entry("m", [None, 2], [30.0, None])

    assert entry["per_thread"] == [None, None]
    assert entry["total"] == [None, None]


def test_derive_total_is_the_one_formula():
    assert derive_total(3, 30.5) == 91.5
    assert derive_total(0, 30.5) == 0.0
    # One decimal, like `tg_tps`: the identity holds to within that rounding.
    assert derive_total(3, 30.14) == 90.4


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------
def test_a_gap_is_null_in_all_three_arrays_never_a_zero():
    """A bucket without a sample is a hole, not a zero: the line breaks there."""
    entry = series_entry("m", [1, None, 2], [30.0, None, 31.0])

    assert entry["n"] == [1, None, 2]
    assert entry["per_thread"] == [30.0, None, 31.0]
    assert entry["total"] == [30.0, None, 62.0]
    assert entry["total"][1] is None


def test_the_three_arrays_always_have_the_same_length():
    entry = series_entry("m", [1, None, 2, None], [30.0, None, 31.0, None])

    assert (len(entry["n"]) == len(entry["total"])
            == len(entry["per_thread"]) == 4)


def test_mismatched_input_is_refused_rather_than_served_ragged():
    with pytest.raises(ValueError):
        series_entry("m", [1, 2], [30.0])


def test_the_entry_carries_the_window_it_was_cut_to():
    entry = series_entry("m", [1], [30.0])

    assert entry["model"] == "m"
    assert entry["bucket_s"] == BUCKET_SECONDS == 5
    assert entry["window_s"] == SERIES_WINDOW_SECONDS == 900


# ---------------------------------------------------------------------------
# The bucket
# ---------------------------------------------------------------------------
def test_a_bucket_averages_its_samples_instead_of_keeping_the_last():
    bucket = Bucket(index=7).add(1, 30.0).add(3, 40.0)

    assert bucket.n == 2.0
    assert bucket.rate == 35.0
    assert bucket.samples == 2


def test_bucket_index_is_a_floor_of_the_wall_clock():
    assert bucket_index(0.0) == 0
    assert bucket_index(4.999) == 0
    assert bucket_index(5.0) == 1
    assert bucket_index(1002.0) == 200


def test_a_window_is_counted_in_whole_buckets():
    assert series_points(900) == 180
    assert series_points(SERIES_BUCKETS * BUCKET_SECONDS) == SERIES_BUCKETS
    # A window narrower than a bucket still asks for one point, not zero.
    assert series_points(2) == 1
    assert series_points(-5) == 1


# ---------------------------------------------------------------------------
# The drawn line (issue #96)
# ---------------------------------------------------------------------------
# The defect the operator saw: a chart that held data and drew nothing. The
# probe runs every 10 s, a bucket is 5 s wide, so a sample lands in bucket 0 and
# the next in bucket 2 — with the empty bucket 1 the pen was lifted at, and a
# subpath of one point paints nothing in SVG. Measured on the live page: six `M`
# and not a single `L`.
#
# The geometry below is `dashboard.js:seriesGeom()` in Python — same span, same
# padding, same rounding — so a path built here is the path the browser builds.
SPARK_W, SPARK_H, SPARK_PAD = 1000, 34, 3


def _axes(*rows):
    """`x(i)` and `y(v)` as the renderer computes them: one scale over both rows."""
    length = max(len(row) for row in rows)
    nums = [v for row in rows for v in row if isinstance(v, (int, float))]
    peak = max(nums) if nums else 0
    span = max(1, length - 1)
    base = SPARK_H - SPARK_PAD

    def y(value):
        return round(base - (value / peak) * (base - SPARK_PAD), 2) if peak > 0 else base

    return (lambda i: round(i / span * SPARK_W, 1)), y


def _vertices(d: str) -> list[tuple[float, float]]:
    """Every drawn point of a `d`, in order, whichever command put it there."""
    return [(float(a), float(b)) for a, b in
            re.findall(r"[ML](-?[\d.]+) (-?[\d.]+)", d)]


def test_two_adjacent_probes_are_one_segment_and_not_two_stray_marks():
    """The acceptance of #96: two probes 10 s apart draw a **line**.

    Built from the payload the backend really serves — `n = [1, None, 2]`,
    because the second probe fell into bucket 2 — and asserted on the path
    string, not on a picture. One `M`, one `L`: a segment between the two
    measurements. Before the fix this string was `M…M…`, six of them on the
    live page and not one `L`, which is why nothing was drawn.
    """
    entry = series_entry("m", [1, None, 2], [30.0, None, 31.0])
    x, y = _axes(entry["total"], entry["per_thread"])

    d = series_path(entry["total"], x, y)

    assert d.count("M") == 1
    assert d.count("L") == 1
    assert "L" in d
    # both measurements are drawn, at their own slot on the axis
    assert _vertices(d) == [(0.0, y(30.0)), (x(2), y(62.0))]
    # the empty bucket contributed no vertex: its x is not in the path
    assert x(1) not in [vx for vx, _ in _vertices(d)]


def test_a_probe_that_did_not_happen_still_breaks_the_line():
    """Two empty buckets in a row are not resolution, they are a lost probe.

    A node that went away must not be drawn as a node that kept its rate: the
    pen lifts, and the two stretches are two subpaths.
    """
    entry = series_entry("m", [1, None, None, 2], [30.0, None, None, 31.0])
    x, y = _axes(entry["total"], entry["per_thread"])

    d = series_path(entry["total"], x, y)

    assert d.count("M") == 2
    assert "L" not in d
    assert len(_vertices(d)) == 2


def test_the_hold_is_the_only_thing_that_bridges_a_hole():
    """`hold` is the whole rule, and 0 is the old behaviour — in one number."""
    entry = series_entry("m", [1, None, 2], [30.0, None, 31.0])
    x, y = _axes(entry["total"], entry["per_thread"])

    assert SERIES_HOLD_BUCKETS == 1
    assert series_path(entry["total"], x, y, hold=0).count("M") == 2
    assert series_path(entry["total"], x, y, hold=1).count("L") == 1
    # a longer hole needs a longer hold; the default never invents one
    long_hole = series_entry("m", [1, None, None, None, 2], [30.0, None, None, None, 31.0])
    lx, ly = _axes(long_hole["total"], long_hole["per_thread"])
    assert series_path(long_hole["total"], lx, ly).count("M") == 2
    assert series_path(long_hole["total"], lx, ly, hold=3).count("M") == 1


def test_a_lone_probe_is_one_point_and_no_line():
    """One sample is a point: the renderer marks it, this function does not lie.

    The `d` is a single `M` — no segment, because there is nothing to connect it
    to. `dashboard.js:seriesDots()` is what makes that visible; the path builder
    must not invent a second point to get a line out of it.
    """
    entry = series_entry("m", [None, 2, None], [None, 31.0, None])
    x, y = _axes(entry["total"], entry["per_thread"])

    d = series_path(entry["total"], x, y)

    assert d.count("M") == 1
    assert "L" not in d
    assert _vertices(d) == [(x(1), y(62.0))]


def test_an_idle_bucket_is_a_point_at_zero_and_not_a_hole():
    """`n = 0` is a measurement — the line goes down to the baseline there."""
    entry = series_entry("m", [1, 0, 1], [30.0, 30.0, 30.0])
    x, y = _axes(entry["total"], entry["per_thread"])

    assert entry["total"] == [30.0, 0.0, 30.0]
    d = series_path(entry["total"], x, y)
    assert d.count("M") == 1 and d.count("L") == 2
    assert (x(1), y(0.0)) in _vertices(d)


# ---------------------------------------------------------------------------
# What the server measured itself (issue #98)
# ---------------------------------------------------------------------------
# The numbers below were read off the live oMLX on 19.11. for one prefix twice:
# the first turn `cached_tokens 0`, the follow-up `689` of `694` prompt tokens.
WARM = {"prompt_tokens": 694, "cached_tokens": 689, "completion_tokens": 61,
        "prompt_tokens_per_second": 17.38, "generation_tokens_per_second": 70.12,
        "time_to_first_token": 0.17, "prompt_eval_duration": 0.17,
        "generation_duration": 0.06}
COLD = {"prompt_tokens": 691, "cached_tokens": 0, "completion_tokens": 40,
        "prompt_tokens_per_second": 16.9, "generation_tokens_per_second": 71.4,
        "time_to_first_token": 40.2}


def test_the_five_measured_fields_are_read_off_the_usage_block():
    """`cached_tokens` and the rates are the server's, not an estimate."""
    observation = usage_observation(WARM)

    assert set(observation) == set(USAGE_FIELDS)
    assert observation["cached_tokens"] == 689
    assert observation["prompt_tokens"] == 694
    assert observation["prompt_tokens_per_second"] == 17.38
    assert observation["generation_tokens_per_second"] == 70.12
    assert observation["time_to_first_token"] == 0.17


def test_cached_tokens_is_read_from_both_shapes_the_server_sends():
    """Measured live on 19.11.: the top-level field in one build, the OpenAI
    `prompt_tokens_details.cached_tokens` in the other. Both must land."""
    def cached(usage):
        observation = usage_observation(usage)
        assert observation is not None
        return observation["cached_tokens"]

    assert cached({"prompt_tokens": 690, "cached_tokens": None,
                   "prompt_tokens_details": {"cached_tokens": 688}}) == 688
    assert cached({"prompt_tokens": 690, "cached_tokens": 1,
                   "prompt_tokens_details": {"cached_tokens": 688}}) == 688
    # a details object without the field is not a measurement either
    assert cached({"prompt_tokens": 690,
                   "prompt_tokens_details": {"audio_tokens": 0}}) is None


def test_a_field_the_backend_did_not_send_is_none_and_never_zero():
    """A silent backend must not look like a cache miss.

    `cached_tokens: 0` is a measurement ("the prefix was cold"); a missing field
    is not. The difference is the whole reason this issue exists, so it is a
    test and not a comment.
    """
    observation = usage_observation({"prompt_tokens": 571, "completion_tokens": 8})

    assert observation["cached_tokens"] is None
    assert observation["prompt_tokens"] == 571
    assert observation["prompt_tokens_per_second"] is None
    # and a block with none of the five is no observation at all
    assert usage_observation({"completion_tokens": 8}) is None
    assert usage_observation({}) is None
    assert usage_observation(None) is None
    assert usage_observation("nope") is None


def test_a_value_that_is_not_a_number_is_a_hole_not_a_zero():
    """`bool`, a string, `NaN` and a negative number are not measurements."""
    observation = usage_observation({"cached_tokens": True, "prompt_tokens": "694",
                                     "prompt_tokens_per_second": float("nan"),
                                     "generation_tokens_per_second": -3.0,
                                     "time_to_first_token": 0.0})

    assert observation["cached_tokens"] is None
    assert observation["prompt_tokens"] is None
    assert observation["prompt_tokens_per_second"] is None
    assert observation["generation_tokens_per_second"] is None
    # zero seconds to the first token is a fast node, not a missing value
    assert observation["time_to_first_token"] == 0.0


def test_a_row_keeps_the_last_observation_the_mean_and_the_count():
    """Three questions, three answers: now, recently, how often."""
    ledger = UsageLedger()
    ledger.record(host="M5", thread="t1", usage=COLD, now=100.0)
    ledger.record(host="M5", thread="t1", usage=WARM, now=110.0)

    row = ledger.thread_row("t1", "M5")
    assert row["samples"] == 2
    assert row["last"]["cached_tokens"] == 689          # the last one, not a mean
    assert row["mean"]["cached_tokens"] == 344.5        # (0 + 689) / 2
    assert row["mean"]["prompt_tokens_per_second"] == 17.14
    assert row["last_at"] == 110.0


def test_the_mean_covers_the_last_eight_turns_and_no_more():
    """A short window: a node that was warm ten turns ago is not warm now."""
    ledger = UsageLedger()
    for i in range(USAGE_WINDOW + 2):
        ledger.record(host="M5", thread="t1",
                      usage={"prompt_tokens": 10, "cached_tokens": i}, now=float(i))

    row = ledger.thread_row("t1", "M5")
    assert row["samples"] == USAGE_WINDOW + 2            # the count is the truth
    # the mean is over the last eight: 2..9, not 0..9
    assert row["mean"]["cached_tokens"] == 5.5


def test_a_missing_field_does_not_drag_the_mean_down():
    """Same rule as `Bucket.rate`: absent is skipped, not counted as zero."""
    ledger = UsageLedger()
    ledger.record(host="M5", thread="t1", usage={"prompt_tokens": 10, "cached_tokens": 0},
                  now=1.0)
    ledger.record(host="M5", thread="t1", usage={"prompt_tokens": 10}, now=2.0)

    row = ledger.thread_row("t1", "M5")
    assert row["mean"]["cached_tokens"] == 0.0
    assert row["last"]["cached_tokens"] is None
    assert row["mean"]["prompt_tokens"] == 10.0


def test_a_thread_that_moved_keeps_one_row_per_host():
    """The price of a move: ~0 on the new node beside 689 on the old one."""
    ledger = UsageLedger()
    ledger.record(host="M5", thread="t1", usage=WARM, now=100.0)
    ledger.record(host="M3", thread="t1", usage=COLD, now=200.0)

    assert ledger.thread_row("t1", "M5")["last"]["cached_tokens"] == 689
    assert ledger.thread_row("t1", "M3")["last"]["cached_tokens"] == 0
    history = ledger.thread_history("t1", "M3")
    assert [row["host"] for row in history] == ["M5"]
    assert history[0]["last"]["cached_tokens"] == 689
    # the thread sits on M3 now: nothing to report as history there
    assert ledger.thread_history("t1", "M5")[0]["host"] == "M3"


def test_a_request_without_a_thread_still_counts_for_its_host():
    """The ephemeral path holds no slot — its measurements must not vanish.

    A one-shot request below `affinity.min_prompt_tokens` has no thread row by
    construction. If the numbers only landed on thread rows, a node that serves
    nothing but short requests would look unmeasured, and the node column would
    read `—` on a node that is answering.
    """
    ledger = UsageLedger()
    ledger.record(host="M5", usage=COLD, thread=None, now=1.0)

    assert ledger.rows == {}                            # no thread row, by design
    host = ledger.host_row("M5")
    assert host["samples"] == 1
    assert host["last"]["cached_tokens"] == 0
    assert ledger.thread_row(None, "M5") is None


def test_a_silent_backend_writes_no_row_at_all():
    """A node that reports nothing is not a node that reported zero."""
    ledger = UsageLedger()
    assert ledger.record(host="M5", thread="t1", usage={"completion_tokens": 3}) is None
    assert ledger.thread_row("t1", "M5") is None
    assert ledger.host_row("M5") is None


def test_the_ledger_is_bounded_and_drops_the_oldest_row():
    """Threads come and go; the ledger must not grow with the uptime."""
    ledger = UsageLedger(max_rows=3)
    for i in range(5):
        ledger.record(host="M5", thread=f"t{i}", usage=WARM, now=float(i))

    assert len(ledger.rows) == 3
    assert sorted(key for key, _ in ledger.rows) == ["t2", "t3", "t4"]
    assert ledger.thread_row("t0", "M5") is None
