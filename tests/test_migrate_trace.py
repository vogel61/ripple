"""The trace of §6.4, checked against arithmetic done by hand.

Every number here is derived on paper first and only then compared with the
simulator: the point of the trace is that three other units will trust it, so a
test that asks the simulator what it thinks is worth nothing. Nothing sleeps,
nothing listens, nothing reads a clock — a 20-turn trace is pure arithmetic.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import trace_migrate  # noqa: E402
from tools.trace_migrate import (  # noqa: E402
    COMPLETION,
    FAST,
    PP_TPS,
    PROMPT_START,
    PROMPT_STEP,
    SLOW,
    TG_FAST,
    TG_SLOW,
    TURNS,
    Turn,
    equal_curves,
    greedy,
    noisy_fleet,
    one_slow_node,
    run_trace,
    stay,
)

#: How far the float sums may drift from the closed form. The closed forms below
#: are exact in real arithmetic; twenty float divisions are not, so a tolerance
#: is needed — 1e-6 s over a ~200 s trace is 5 parts per billion.
EPS = 1e-6


def hand_wall_clock(tg_tps: float, pp_tps: float = PP_TPS) -> float:
    """The wall clock of `TURNS` turns that never move, computed by hand.

    Only the first turn prefills (2000 tokens, the prompt it arrives with); every
    later turn is warm, so its prefill is zero and its prompt growth is free.
    Decode is 400 tokens at the node's rate, on every turn.
    """
    return PROMPT_START / pp_tps + TURNS * COMPLETION / tg_tps


def flip_flop(first: str, second: str):
    """Alternate between two hosts every turn — the prefill storm, deliberately."""

    def decide(turn_index, prompt_tokens, warm_host, nodes, now_s):
        return (first if turn_index % 2 == 0 else second), "flip_flop"

    return decide


def test_equal_curves_never_move_and_both_deciders_agree():
    """§6.4 row 1: delta_total is zero, so the band is never beaten."""
    scenario = equal_curves()
    turns, nodes = scenario
    assert nodes[FAST].tg_tps == nodes[SLOW].tg_tps == TG_FAST

    stayed = run_trace(turns, nodes, stay(FAST))
    greedy_run = run_trace(turns, nodes, greedy(FAST))

    assert stayed.moves == 0
    assert greedy_run.moves == 0, "a tie is not an improvement — greedy must not move"
    assert stayed.wall_clock_s == pytest.approx(greedy_run.wall_clock_s, abs=EPS)
    assert stayed.wall_clock_s == pytest.approx(hand_wall_clock(TG_FAST), abs=EPS)
    assert stayed.hosts == [FAST] * TURNS
    assert stayed.prefills == 1, "turn 0 is cold once; the rest are warm"


def test_one_slow_node_stay_difference_is_the_hand_computed_decode_delta():
    """§6.4 row 2, the arithmetic: 20 turns × 400 tokens × (1/28 − 1/40)."""
    scenario = one_slow_node()
    turns, nodes = scenario
    assert nodes[SLOW].tg_tps == pytest.approx(TG_FAST * 0.7), "30 % slower in decode"

    slow = run_trace(turns, nodes, stay(SLOW))
    fast = run_trace(turns, nodes, stay(FAST))

    expected_delta = TURNS * COMPLETION * (1.0 / TG_SLOW - 1.0 / TG_FAST)
    measured_delta = slow.wall_clock_s - fast.wall_clock_s

    assert measured_delta == pytest.approx(expected_delta, abs=EPS)
    assert slow.wall_clock_s == pytest.approx(hand_wall_clock(TG_SLOW), abs=EPS)
    assert fast.wall_clock_s == pytest.approx(hand_wall_clock(TG_FAST), abs=EPS)
    # 85.714 s: the whole reason the feature exists, and both runs paid exactly
    # one prefill, so the difference is decode and nothing else.
    assert expected_delta == pytest.approx(85.714285714, abs=1e-6)
    assert slow.prefills == fast.prefills == 1
    assert slow.moves == fast.moves == 0


def test_greedy_pays_one_prefill_and_lands_between_the_two_baselines():
    """§6.4 row 2: a move that happened is not a win — a lower total time is.

    Greedy from the slow node moves once, on turn 2, and pays the full 2400-token
    prompt for it; it earns 19 turns of the decode delta. So it must beat staying
    on the slow node and lose to staying on the fast one.
    """
    scenario = one_slow_node()
    turns, nodes = scenario

    greedy_run = run_trace(turns, nodes, greedy(SLOW))
    slow = run_trace(turns, nodes, stay(SLOW))
    fast = run_trace(turns, nodes, stay(FAST))

    assert greedy_run.moves == 1
    moved = [record for record in greedy_run.per_turn if record.moved]
    assert len(moved) == 1
    assert moved[0].index == 1, "the move happens on the second turn, at arrival"
    assert moved[0].host == FAST
    assert greedy_run.hosts == [SLOW] + [FAST] * (TURNS - 1)
    assert greedy_run.prefills == 2, "the cold first turn and the move"

    # The price of the move: the whole prompt as it stands on turn 2, not turn 1.
    assert moved[0].prefill_s == pytest.approx((PROMPT_START + PROMPT_STEP) / PP_TPS, abs=EPS)

    # The earnings: 19 turns at (1/28 − 1/40) s per 400-token answer.
    earned = (TURNS - 1) * COMPLETION * (1.0 / TG_SLOW - 1.0 / TG_FAST)
    assert earned == pytest.approx(81.428571428, abs=1e-6)
    assert earned > moved[0].prefill_s, "this conversation repays the prefill"

    hand_greedy = (
        PROMPT_START / PP_TPS + COMPLETION / TG_SLOW                    # turn 1, cold on the slow node
        + (PROMPT_START + PROMPT_STEP) / PP_TPS + COMPLETION / TG_FAST  # turn 2, the move
        + (TURNS - 2) * COMPLETION / TG_FAST                            # turns 3..20, warm
    )
    assert greedy_run.wall_clock_s == pytest.approx(hand_greedy, abs=EPS)
    assert greedy_run.wall_clock_s == pytest.approx(209.785714285, abs=1e-6)

    assert fast.wall_clock_s < greedy_run.wall_clock_s < slow.wall_clock_s
    assert slow.wall_clock_s - greedy_run.wall_clock_s == pytest.approx(earned - moved[0].prefill_s, abs=EPS)


def test_flip_flop_is_the_prefill_storm_and_the_worst_trace():
    """A move every turn: 20 prefills, and nothing is cheaper than it."""
    scenario = one_slow_node()
    turns, nodes = scenario

    storm = run_trace(turns, nodes, flip_flop(SLOW, FAST))
    greedy_run = run_trace(turns, nodes, greedy(SLOW))
    slow = run_trace(turns, nodes, stay(SLOW))
    fast = run_trace(turns, nodes, stay(FAST))

    assert storm.moves == TURNS - 1, "19 host changes after the first turn"
    assert storm.prefills == TURNS, "every single turn pays a full prefill"
    assert all(record.prefill_s > 0 for record in storm.per_turn)

    total_prompt = sum(PROMPT_START + PROMPT_STEP * index for index in range(TURNS))
    hand_storm = (
        total_prompt / PP_TPS
        + (TURNS // 2) * COMPLETION / TG_SLOW
        + (TURNS // 2) * COMPLETION / TG_FAST
    )
    assert storm.wall_clock_s == pytest.approx(hand_storm, abs=EPS)

    assert storm.wall_clock_s > slow.wall_clock_s
    assert storm.wall_clock_s > greedy_run.wall_clock_s
    assert storm.wall_clock_s > fast.wall_clock_s
    # The storm costs more than the machine it is trying to escape: 387.9 s
    # against 288.2 s for simply staying on the slow node.
    assert storm.wall_clock_s - slow.wall_clock_s == pytest.approx(99.642857142, abs=1e-6)


def test_the_bill_adds_up_to_the_total_and_the_reasons_survive():
    """A reader must be able to see where the time went, not just the total."""
    scenario = one_slow_node()
    turns, nodes = scenario
    result = run_trace(turns, nodes, greedy(SLOW))

    assert len(result.per_turn) == TURNS
    assert [record.index for record in result.per_turn] == list(range(TURNS))
    assert sum(record.total_s for record in result.per_turn) == pytest.approx(result.wall_clock_s, abs=EPS)
    assert {record.reason for record in result.per_turn} == {"start", "greedy_better_tg", "greedy_already_best"}
    assert result.as_dict()["per_turn"][1]["moved"] is True

    with pytest.raises(ValueError):
        run_trace(turns, nodes, stay("m9-nowhere"))


def test_noisy_fleet_is_reproducible_and_still_a_one_slow_node_trace():
    """The third trace: the same decision made through ±8 % noise, twice alike."""
    first = noisy_fleet()
    second = noisy_fleet()

    assert [turn.prompt_tokens for turn in first.turns] == [turn.prompt_tokens for turn in second.turns]
    for name in (FAST, SLOW):
        assert [first.nodes[name].tg_rate(index) for index in range(TURNS)] == [
            second.nodes[name].tg_rate(index) for index in range(TURNS)
        ]
    # The noise perturbs the gap, never its sign: a trace that flipped the
    # ordering would be measuring the noise.
    assert all(first.nodes[SLOW].tg_rate(i) < first.nodes[FAST].tg_rate(i) for i in range(TURNS))
    assert [n.tg_rate(3) for n in first.nodes.values()] != [n.tg_tps for n in first.nodes.values()]

    one = run_trace(first.turns, first.nodes, greedy(first.start_host))
    two = run_trace(second.turns, second.nodes, greedy(second.start_host))
    assert one.as_dict() == two.as_dict()
    assert one.moves == 1
    assert one.wall_clock_s == pytest.approx(
        sum(record.total_s for record in one.per_turn), abs=EPS
    )
    # Noise is a small perturbation, not a second scenario: within a few percent
    # of the clean trace, and still far better than staying on the slow node.
    clean = run_trace(one_slow_node().turns, one_slow_node().nodes, greedy(SLOW))
    assert one.wall_clock_s == pytest.approx(clean.wall_clock_s, rel=0.02)
    assert one.wall_clock_s < run_trace(one_slow_node().turns, one_slow_node().nodes, stay(SLOW)).wall_clock_s


def test_the_simulator_never_sleeps_or_reads_a_clock():
    """No wall clock, no sleeps: the whole trace is arithmetic and stays under 1 s."""
    source = inspect.getsource(trace_migrate)
    assert "import time" not in source
    assert "time.time" not in source
    assert "time.sleep" not in source
    assert "import random" not in source, "jitter is hashed, not drawn — a seed must reproduce everywhere"

    started = time.perf_counter()
    for _ in range(50):
        for factory in (equal_curves, one_slow_node, noisy_fleet):
            scenario = factory()
            turns, nodes = scenario
            run_trace(turns, nodes, stay(scenario.start_host))
            run_trace(turns, nodes, greedy(scenario.start_host))
        run_trace(*one_slow_node(), flip_flop(SLOW, FAST))
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"{elapsed:.3f} s for 350 traces — the model is not pure arithmetic"
