"""§6.4's acceptance table, measured with the **real** `decide_migration`.

`tests/test_migrate_trace.py` proves the simulator measures a difference; it
uses `stay`, `greedy` and `flip_flop`, none of which is the feature. This file
plugs `placement.decide_migration` itself into the simulator's decider protocol,
so the three rows of §6.4 are a statement about the code that ships, not about
the arithmetic of a toy.

The adapter (`_TraceDecider`) is the whole seam: it maps a simulated turn
`(nodes, warm_host, now_s, prompt_tokens)` onto a `ThreadState` and a
`MigrateFleet`, calls the decision, and writes the verdict back onto its own
thread state — host, `moved_from`, `moved_at`, `move_count`, `pinned_until`.
Nothing in the simulator knows about migration; nothing in `placement.py` knows
about the simulator.

The measured numbers (`.venv/bin/python -m pytest -q tests/test_migrate_acceptance.py`)

| row | trace | moves | wall clock |
|---|---|---|---|
| 1 | `equal_curves`, migrate on | **0** | **202.500 s**, exactly the `stay` baseline |
| 2 | `one_slow_node`, migrate on | **1** | **209.786 s** against **288.214 s** with `enabled: false` (78.43 s saved) |
| 3a | `one_slow_node`, band 0 / return 1.0 | **1** | 209.786 s — *the same one move*: this trace is **too quiet** to prove row 3 |
| 3b | `flapping_fleet`, band 0 / return 1.0 | **10** | 199.2 s — against **4** moves / 196.0 s with the measured band and `return_factor: 2.0` |

Row 3a is the honest negative result and is asserted as such, loudly: after the
one move on `one_slow_node` the *only* other host is the slow one, so
`Δtotal ≤ 0` forever and no decider — however neutral its hysteresis — can find
a second move. A trace on which row 3 cannot be measured must not be allowed to
pass row 3 by accident, so it is a separate test with the reason in its name.
Row 3b supplies what row 3 needs: two nodes of equal speed whose per-turn rates
cross, so the unguarded calculus moves on ten of nineteen turns (the prefill
storm §6.4 warns about) and the band plus the return factor cut that to four.

Two numbers are held out of the comparison on purpose, and both are named in
the test that would otherwise be measuring them:

* `rebind.max_per_hour` — the fleet budget. The simulator has one thread, so
  passing it in would mean "this conversation's own moves", not the fleet's;
  it is left at 0 (no budget) so rows 2 and 3 measure the band, the return
  factor and the amortisation.
* `migrate.max_moves_per_thread` — the pin. At the shipped 2 it binds before
  the hysteresis does (two nodes allow exactly two moves), so rows 2 and 3 run
  with a high pin and the pin gets its own test at the shipped value.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from ripple.config import Config, MigrateConfig, PlacementConfig, RebindConfig
from ripple.placement import (MigrateFleet, MigrateHost, ThreadState,
                              decide_migration)
from tools.trace_migrate import (COMPLETION, FAST, PP_TPS, PROMPT_START,
                                 PROMPT_STEP, SLOW, TG_FAST, Node, Scenario,
                                 Turn, equal_curves, one_slow_node, run_trace,
                                 stay)

#: The default 400-token answer of §6.4, and the one every trace uses.
ASSUMED = COMPLETION

#: The simulator's two nodes are called this in every trace; a third would need
#: a name of its own, and none of the rows needs one.
TWO = (FAST, SLOW)


def _cfg(*, enabled: bool = True, band_samples: int = 5, return_factor: float = 2.0,
         max_moves_per_thread: int = 20, window: float = 3600.0,
         cooldown: float = 600.0) -> Config:
    """The configuration a row runs under — `placement.rebind` plus `migrate`.

    `rebind.enabled` is off: this file measures the arriving turn, and leaving
    the sweep on would let a second mechanism move the thread and make the
    numbers unattributable. The sweep is tested where it lives
    (`tests/test_placement.py`); `rebind`'s *parameters* are reused by the
    decision, which is the point of §6.4 — the same calculus, a new moment.

    `max_moves_per_thread` defaults to 20 here, not to the shipped 2: with two
    nodes the pin at 2 binds before the band does, and a row that asks whether
    the band suppressed moves would be answering "the pin did". The shipped
    value has its own test (`test_the_pin_stops_the_flapping_at_the_shipped_two`).
    """
    cfg = Config()
    cfg.placement = PlacementConfig(
        rebind=RebindConfig(enabled=False, per_thread_cooldown_seconds=cooldown,
                            safety_factor=1.5, min_gain_seconds=2.0,
                            default_pp_tps=0.0),
        migrate=MigrateConfig(enabled=enabled, band_samples=band_samples,
                              return_factor=return_factor,
                              max_moves_per_thread=max_moves_per_thread,
                              window_seconds=window),
    )
    return cfg


def _history(scenario: Scenario, samples: int = 5) -> tuple[float, ...]:
    """The fleet's recent `Δtotal` scans, taken from the trace's own curves.

    §6.4's band is the *observed* spread of `Δtotal`, so a trace must supply
    the observation instead of a constant: this is `rate_dst(1) − rate_src(1)`
    at the first `samples` turns — the fleet verdict a scan would have recorded
    before the conversation arrived. A clean trace yields five identical
    samples and therefore a band of 0.0 s, which is the honest answer for a
    fleet that is not noisy; the noisy trace yields a spread.
    """
    return tuple(round(scenario.nodes[FAST].tg_rate(i) - scenario.nodes[SLOW].tg_rate(i), 1)
                 for i in range(samples))


class _TraceDecider:
    """The simulator's decider protocol, answered by `decide_migration`.

    One object per run, because a decision about *this* turn needs the thread
    the previous turns produced: the host it is warm on, when it last moved and
    how often. The simulator supplies the clock (`now_s` = the sum of the turn
    durations it has already measured), so nothing here reads a clock either.

    `turns` is `turn_index + 1` — the count the registry holds at the arrival of
    that turn (it registers with 1 and `touch`es on every arrival), which is
    what `_turns_left` reads.
    """

    def __init__(self, cfg: Config, start_host: str, *,
                 history: tuple[float, ...] = (), ttl_s: float = 7200.0,
                 max_moves_per_hour: int = 0) -> None:
        self.cfg = cfg
        self.start_host = start_host
        self.history = tuple(history)
        self.ttl_s = ttl_s
        self.max_moves_per_hour = max_moves_per_hour
        self.host: str | None = None
        self.moved_from: str | None = None
        self.moved_at = 0.0
        self.move_count = 0
        self.pinned_until = 0.0
        self.last_turn_at = 0.0
        #: `(turn, reason, priced)` per decision, for a row that wants to show
        #: why — and for the assertions that read a refusal reason.
        self.decisions: list[tuple[int, str, dict]] = []

    def _fleet(self, nodes: dict[str, Node], turn_index: int) -> MigrateFleet:
        """The fleet as values, at this turn's measured rates.

        `curve` carries one point per level: `rate_h(1)` is the node's rate on
        this turn (the fitted curve a scan would report), `rate_h(2)` the same
        with a stream's worth of contention — a shape, not a measurement, and
        unused while one thread moves between two hosts (`n = 1`, `m = 0`, so
        only level 1 is read).

        `pp_tps` is the node's own prefill rate: §6.4's `cost = tokens / pp_tps`
        must be priced at the rate the node actually prefills at, and both
        trace nodes prefill at `PP_TPS`.
        """
        hosts: dict[str, MigrateHost] = {}
        for name, node in nodes.items():
            rate = node.tg_rate(turn_index)
            hosts[name] = MigrateHost(
                name=name,
                pp_tps=node.pp_rate(turn_index),
                tg_tps=rate,
                curve={1.0: rate, 2.0: rate * 0.97},
                threads=1 if name == self.host else 0,
                on_host=1 if name == self.host else 0,
                capacity=4,
            )
        return MigrateFleet(hosts=hosts, delta_history=self.history)

    def __call__(self, turn_index: int, prompt_tokens: int, warm_host: str | None,
                nodes: dict[str, Node], now_s: float) -> tuple[str, str]:
        if warm_host is None:
            # No thread exists before the first turn: nothing to price, and the
            # trace says where the conversation opens.
            self.host = self.start_host
            self.last_turn_at = now_s
            return self.start_host, "start"

        self.host = warm_host
        state = ThreadState(
            host=self.host,
            turns=turn_index + 1,
            created=0.0,
            last_turn_at=self.last_turn_at,
            ttl_remaining_s=self.ttl_s - now_s,
            moved_from=self.moved_from,
            moved_at=self.moved_at,
            pinned_until=self.pinned_until,
            move_count=self.move_count,
        )
        decision = decide_migration(
            cfg=self.cfg, thread_state=state, fleet=self._fleet(nodes, turn_index),
            prompt_tokens=prompt_tokens, now_s=now_s,
            assumed_completion_tokens=ASSUMED,
            moves_last_hour=self.move_count,
            max_moves_per_hour=self.max_moves_per_hour,
        )
        self.last_turn_at = now_s
        self.decisions.append((turn_index, decision.reason, dict(decision.priced)))

        if decision.moves:
            self.moved_from = self.host
            self.moved_at = now_s
            self.move_count += 1
            # Mechanism (c) is written back exactly as the decision priced it.
            self.pinned_until = float(decision.priced.get("pin_until") or 0.0)
        return decision.host, decision.reason


def _run(scenario: Scenario, cfg: Config, **kwargs):
    turns, nodes = scenario
    decider = _TraceDecider(cfg, scenario.start_host, **kwargs)
    return run_trace(turns, nodes, decider), decider


def _flapping_fleet(jitter: float = 0.25, turns: int = 20) -> Scenario:
    """Two equally fast nodes whose per-turn rates cross — row 3's trace.

    `equal_curves` is a tie and `one_slow_node`'s gap never closes, so neither
    can show what the hysteresis *suppresses*: both have nothing to suppress
    (0 moves, and one move that can never be undone). Here the nodes are the
    same speed on average and ±25 % apart on any given turn, so the leader
    flips: `Δtotal` changes sign between turns, and every turn on which the
    other node is momentarily ahead is an opportunity the band and the return
    factor have to refuse.

    Prompts are short (400 tokens, +50 a turn) on purpose: the price of a move
    has to stay small enough that the *unguarded* calculus says yes, otherwise
    the trace measures `no_repayment` — the amortisation — instead of the
    hysteresis. The same trace is used for row 2's hysteresis comparison and
    row 3, so the two differ only in `band_samples` and `return_factor`.
    """
    nodes = {
        FAST: Node(FAST, pp_tps=PP_TPS, tg_tps=TG_FAST, jitter=jitter, seed=95),
        SLOW: Node(SLOW, pp_tps=PP_TPS, tg_tps=TG_FAST, jitter=jitter, seed=7),
    }
    conversation = [Turn(prompt_tokens=400 + 50 * index, completion_tokens=COMPLETION)
                    for index in range(turns)]
    return Scenario("flapping_fleet", conversation, nodes, FAST)


# --------------------------------------------------------------------------- #
# Row 1 — equal curves: 0 moves, and the same wall clock to the microsecond
# --------------------------------------------------------------------------- #


def test_row1_equal_curves_never_move_and_the_wall_clock_is_the_stay_baseline():
    """§6.4 row 1: `delta_total` is zero, so the band is never beaten.

    Measured: **0 moves**, **202.500 s** — and the equality with the `stay`
    baseline is exact (`==`, not `approx`), because the decision changed
    nothing: the same turn ran on the same host with the same arithmetic. A
    tolerance here would hide the only interesting failure, a decider that
    moves a thread and lands back on the same node.
    """
    scenario = equal_curves()
    result, decider = _run(scenario, _cfg(), history=_history(scenario))
    baseline = run_trace(*scenario, stay(scenario.start_host))

    assert result.moves == 0
    assert result.hosts == [scenario.start_host] * len(scenario.turns)
    assert result.wall_clock_s == baseline.wall_clock_s
    assert result.wall_clock_s == pytest.approx(202.5, abs=1e-9)
    assert result.prefills == 1, "turn 0 is cold once; a move would have made it two"

    # Every turn asked the question and every turn got the same answer: the
    # fleet verdict is zero, not unknown.
    reasons = Counter(reason for _turn, reason, _priced in decider.decisions)
    assert reasons == {"delta_non_positive": len(scenario.turns) - 1}
    assert all(priced.get("delta_total", 0.0) <= 0.0
               for _turn, _reason, priced in decider.decisions)


# --------------------------------------------------------------------------- #
# Row 2 — one slow node: at most one move per cooldown, and a lower wall clock
# --------------------------------------------------------------------------- #


def test_row2_one_slow_node_moves_once_and_beats_the_disabled_wall_clock():
    """§6.4 row 2 — the only row that measures the feature.

    Measured: **1 move** on turn 2, **209.786 s** with migration against
    **288.214 s** with `placement.migrate.enabled: false` — **78.43 s** saved,
    which is the 19 remaining turns of decode delta (81.43 s) minus the 2400
    token prefill the move paid (3.00 s).

    "At most one move per cooldown window" is asserted twice: the count is 1,
    and the window the trace spans (209.8 s) is shorter than
    `rebind.per_thread_cooldown_seconds` (600 s), so a second move would have
    had to happen inside one window — it did not.

    The move lands on turn 2 and not turn 1 because a thread with no history
    cannot be priced: the first turn opens the conversation. At the arrival of
    the second turn `turns = 2`, so `turns_left = 2` (the arriving turn and the
    one after it), 2 × 4.29 s = 8.57 s of gain against 4.50 s of
    `cost × safety_factor` — the move repays itself with one turn to spare.
    """
    scenario = one_slow_node()
    history = _history(scenario)
    assert history == (12.0,) * 5, "the clean trace's Δtotal is 12 tok/s every scan"

    result, decider = _run(scenario, _cfg(), history=history)
    disabled, disabled_decider = _run(scenario, _cfg(enabled=False), history=history)
    baseline = run_trace(*scenario, stay(scenario.start_host))

    assert result.moves == 1
    moved = [record for record in result.per_turn if record.moved]
    assert [record.index for record in moved] == [1], "the second turn's arrival"
    assert moved[0].host == FAST
    assert result.wall_clock_s < disabled.wall_clock_s
    assert result.wall_clock_s == pytest.approx(209.785714285, abs=1e-6)
    assert disabled.wall_clock_s == pytest.approx(288.214285714, abs=1e-6)
    assert disabled.wall_clock_s == baseline.wall_clock_s

    # The trace is shorter than the cooldown, so one move is one move per window.
    assert result.wall_clock_s < 600.0

    # The move's own price: the whole prompt as it stands on turn 2, not turn 1.
    assert moved[0].prefill_s == pytest.approx((PROMPT_START + PROMPT_STEP) / PP_TPS, abs=1e-9)
    assert result.prefills == 2, "the cold opening and the move"

    # The reasons name the arithmetic, turn by turn.
    reasons = [reason for _turn, reason, _priced in decider.decisions]
    assert reasons[0] == "moved"
    assert set(reasons[1:]) == {"delta_non_positive"}, "the slow node is never a destination"
    priced = decider.decisions[0][2]
    assert priced["cost_s"] == pytest.approx(3.0, abs=0.01)
    assert priced["gain_per_turn_s"] == pytest.approx(4.286, abs=0.01)
    assert priced["turns_left"] == 2.0
    assert priced["band_s"] == 0.0, "a clean trace has no spread for the band to measure"

    # `enabled: false` is today's behaviour, not a different reason per turn.
    assert {reason for _turn, reason, _priced in disabled_decider.decisions} == {"disabled"}
    assert disabled.moves == 0
    assert disabled.hosts == [scenario.start_host] * len(scenario.turns)


# --------------------------------------------------------------------------- #
# Row 3 — the hysteresis suppressed moves, and this is where that is visible
# --------------------------------------------------------------------------- #


def test_row3_one_slow_node_is_too_quiet_to_prove_anything():
    """§6.4 row 3, attempted on row 2's own trace: **it cannot be measured here**.

    Measured: **1 move** with `band_samples: 5, return_factor: 2.0` and **1
    move** with band 0 and `return_factor: 1.0` — the same single move, the
    same 209.786 s. This is not the hysteresis working; it is the trace having
    no second opportunity. After the move the only other host is the slow one,
    so `Δtotal ≤ 0` on every later turn and *no* setting of the band or the
    return factor can produce a second move.

    The test asserts the equality on purpose. §6.4 says a row 3 that is not
    "more" means "the trace was too quiet to prove anything"; a suite that
    silently passed this row on this trace would be claiming a measurement it
    does not have. The row is measured on `flapping_fleet` below, and this test
    is what says why that trace had to be built.
    """
    scenario = one_slow_node()
    guarded, _ = _run(scenario, _cfg(return_factor=2.0), history=_history(scenario))
    neutral, neutral_decider = _run(scenario, _cfg(band_samples=5, return_factor=1.0),
                                    history=(0.0,) * 5)

    assert neutral_decider.decisions[0][2]["band_s"] == 0.0
    assert neutral.moves == guarded.moves == 1, (
        "one_slow_node cannot show row 3: after the move Δtotal is negative for "
        "good, so band 0 and return_factor 1.0 buy exactly the same single move. "
        "This trace is too quiet to prove anything — see "
        "test_row3_neutralised_hysteresis_moves_more_than_the_measured_band."
    )
    assert neutral.wall_clock_s == guarded.wall_clock_s


def test_row3_neutralised_hysteresis_moves_more_than_the_measured_band():
    """§6.4 row 3: band 0 and `return_factor: 1.0` move **more**, not fewer.

    Measured on `flapping_fleet` (two equal nodes, ±25 % per-turn crossing
    rates, 20 turns): **10 moves** and 199.2 s with the hysteresis neutralised
    against **4 moves** and 196.0 s with the band measured from the trace's own
    `Δtotal` scans and `return_factor: 2.0`. The `stay` baseline is 200.2 s.

    Ten moves of nineteen opportunities is the prefill storm §6.4 describes —
    a move on every turn on which the other node is momentarily ahead — and it
    is *worse* than the four the band and the return factor allow, even though
    each of those ten looked like a win when it was priced. That is the whole
    argument for the hysteresis, in one number.

    The reasons say which mechanism refused: `gain_below_band` (the benefit
    cleared `cost × safety_factor` but not the measured spread) and `cooldown`
    (a move back inside `per_thread_cooldown_seconds`, priced up by the return
    factor). Without them the same turns are `moved`.
    """
    scenario = _flapping_fleet()
    history = _history(scenario)
    assert len(set(history)) > 1, "row 3 needs a fleet whose Δtotal actually varies"

    guarded, guarded_decider = _run(scenario, _cfg(return_factor=2.0), history=history)
    neutral, neutral_decider = _run(scenario, _cfg(return_factor=1.0), history=(0.0,) * 5)
    baseline = run_trace(*scenario, stay(scenario.start_host))

    assert neutral.moves > guarded.moves, (
        f"band 0 / return_factor 1.0 moved {neutral.moves} times and the measured "
        f"band {guarded.moves} — the trace is too quiet to prove anything"
    )
    assert (neutral.moves, guarded.moves) == (10, 4)
    assert neutral.wall_clock_s == pytest.approx(199.2, abs=0.1)
    assert guarded.wall_clock_s == pytest.approx(196.0, abs=0.1)
    # The guarded run is not merely cheaper than the storm — it beats staying,
    # which is what makes the four moves a decision and not a coin flip.
    assert guarded.wall_clock_s < baseline.wall_clock_s

    refused = Counter(reason for _turn, reason, _priced in guarded_decider.decisions)
    assert refused["gain_below_band"] >= 1
    assert refused["cooldown"] >= 1
    assert refused["moved"] == 4
    # Neutralised, those same turns pass: every refusal above becomes a move.
    neutral_reasons = Counter(reason for _turn, reason, _priced in neutral_decider.decisions)
    assert neutral_reasons["gain_below_band"] == 0
    assert neutral_reasons["cooldown"] == 0
    assert neutral_reasons["moved"] == 10


def test_the_pin_stops_the_flapping_at_the_shipped_two():
    """Mechanism (c) at the value the shipped config uses: two moves, then pinned.

    `placement.migrate.max_moves_per_thread: 2` with the hysteresis neutralised
    turns the ten-move storm of row 3 into **two** moves (on turns 6 and 7):
    the third and every later turn is refused with `pinned`, and
    `priced["pin_until"]` is written back so the thread keeps its cache for the
    rest of the window. This is the last line of defence, not the first — the
    band did most of the work above — and it is the one that bounds a single
    conversation's share of the fleet's moves.
    """
    scenario = _flapping_fleet()
    result, decider = _run(scenario, _cfg(return_factor=1.0, max_moves_per_thread=2),
                           history=(0.0,) * 5)

    assert result.moves == 2
    reasons = [reason for _turn, reason, _priced in decider.decisions]
    second_move = max(index for index, reason in enumerate(reasons) if reason == "moved")
    assert set(reasons[second_move + 1:]) == {"pinned"}, (
        "after the second move nothing may move again inside the window")
    second = [priced for _turn, reason, priced in decider.decisions if reason == "moved"][-1]
    assert second["pin_until"] > 0.0
    pinned = [priced for _turn, reason, priced in decider.decisions if reason == "pinned"][0]
    assert pinned["pinned_until"] == second["pin_until"], (
        "the refusal quotes the pin the move wrote back, not a new one")


def test_the_adapter_speaks_the_placement_vocabulary_and_carries_the_price():
    """A row may only claim a reason the decision can produce, and it must price it.

    The simulator records the reason verbatim, so the vocabulary crossing the
    seam is `placement.py`'s own — not a string invented here. And every
    `moved` decision carries the numbers §6.4's log line is supposed to show:
    `cost_s`, `gain_per_turn_s`, `benefit_s`, `threshold_s`, `delta_total`.
    """
    scenario = one_slow_node()
    result, decider = _run(scenario, _cfg(), history=_history(scenario))

    allowed = {"moved", "no_repayment", "cost_exceeds_ttl", "gain_below_band",
               "no_curve", "delta_non_positive", "cooldown", "budget", "pinned",
               "disabled", "batch_no_rebind", "no_target", "start"}
    assert {reason for _turn, reason, _priced in decider.decisions} <= allowed

    for _turn, reason, priced in decider.decisions:
        if reason != "moved":
            continue
        for key in ("cost_s", "gain_per_turn_s", "benefit_s", "threshold_s",
                    "delta_total", "turns_left", "band_s"):
            assert key in priced, f"a moved decision without {key} cannot be logged"
        assert priced["benefit_s"] > priced["threshold_s"]
    assert result.moves == 1
