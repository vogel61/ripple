#!/usr/bin/env python3
"""A reproducible trace for the arriving turn (#95): 20 turns, two nodes, one slowed.

`docs/requirements.md` §6.4 states its acceptance as a table — 0 moves on equal
curves, a lower wall clock with migration, more moves once the hysteresis is
removed — and says plainly that the table states an *intention* until a
reproducible trace exists. This module is that trace, and nothing else: it does
not implement migration, it prices one.

The model
---------
A node is a pair of rates, `pp_tps` (prefill) and `tg_tps` (decode). A turn is a
prompt and a completion. One turn on one node costs

    prefill = prompt_tokens / pp_tps   if the thread's prefix is NOT warm there
            = 0                        if it is
    decode  = completion_tokens / tg_tps

A conversation is warm on exactly one node: the one that served its last turn.
That single rule is the whole economics of the feature — staying is free after
the first turn, and a move costs a full prefill of the *current* prompt, however
long the context has grown. Every number in §6.4 follows from it.

Three traces
------------
`equal_curves`   two nodes, identical curves, one thread, 20 turns. `delta_total`
                 is zero; no decider has a reason to move.
`one_slow_node`  one node 30 % slower in decode, one long conversation: prompt
                 2000 tokens growing 400 per turn, completion 400. The only trace
                 that measures anything: the move pays one prefill and earns the
                 decode delta on every turn after it.
`noisy_fleet`    `one_slow_node` with a small per-turn jitter on the rates, so a
                 decision that depends on the curves is made through noise — the
                 trace the hysteresis band is supposed to survive. Deterministic
                 from a seed.

The decider protocol
--------------------
    def decider(turn_index: int, prompt_tokens: int, warm_host: str | None,
                nodes: dict[str, Node], now_s: float) -> tuple[str, str]:
        \"\"\"Return (host_name, reason).\"\"\"

`now_s` is *simulated* seconds since the start of the trace; the model never
reads a clock and never sleeps, so a 20-turn trace costs microseconds and the
same input always yields the same output.

Decisions the contract left open, and how they are settled here
--------------------------------------------------------------
* **One warm host.** A move does not leave the source node warm for the thread:
  the old copy is stale because the prompt has grown, and reusing it would be
  re-prefilling anyway. Modelling the source as still-warm would halve the cost
  of a flip-flop and flatter the feature.
* **Turn 0 pays prefill** whatever it does — the first turn is cold everywhere.
  It is not a move (`moves` counts host *changes* after the first turn), but it
  is a prefill, so a 20-turn flip-flop shows 19 moves and 20 prefills.
* **`greedy` is anchored.** On turn 0 there is no warm host, so a greedy decider
  still has to land somewhere: it starts on the trace's `start_host` and only
  thereafter takes the better curve. Ties go to the warm host (then to the name),
  which is why equal curves with `greedy` do not move.
* **Jitter is hashed, not drawn.** `noisy_fleet` derives each per-turn factor from
  blake2b(seed, node, kind, turn) instead of `random.Random`, so the trace is
  stable across Python versions, across draw order, and across processes — a
  number quoted in a review must reproduce on another machine.
* **The curves are round numbers, not measurements.** 800 pp_tps, 40 and 28
  tg_tps (28 = 30 % below 40). They are chosen so the hand-computed difference
  is checkable on paper; they are not a claim about any node in the fleet.

Usage:
    ./.venv/bin/python -m tools.trace_migrate --trace one_slow_node --decider greedy
    ./.venv/bin/python -m tools.trace_migrate --trace one_slow_node --decider stay --host m5-alpha
    ./.venv/bin/python -m tools.trace_migrate --trace noisy_fleet --decider greedy --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

#: The trace's curves. `TG_SLOW` is 30 % below `TG_FAST`, which is the one
#: artificial handicap §6.4 asks for. Both nodes prefill at the same rate: the
#: handicap is in decode, because that is what `delta_total` prices.
PP_TPS = 800.0
TG_FAST = 40.0
TG_SLOW = 28.0

#: The conversation of `one_slow_node` and `noisy_fleet`: 20 turns, a prompt that
#: grows the way a real thread grows, a completion of the assumed 400 tokens.
TURNS = 20
PROMPT_START = 2000
PROMPT_STEP = 400
COMPLETION = 400

#: Node names as the fleet names them: the entry node and the smaller one.
FAST = "m5-alpha"
SLOW = "m3-beta"


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


def _unit(seed: int, name: str, kind: str, turn_index: int) -> float:
    """A deterministic number in [0, 1) for (seed, node, kind, turn).

    Hashed rather than drawn so the sequence does not depend on how many numbers
    were drawn before it: adding a node or reordering the dict cannot shift the
    noise of another node, and a number quoted in a review reproduces on a
    different machine and a different Python.
    """
    material = f"{seed}|{name}|{kind}|{turn_index}".encode("utf-8")
    digest = hashlib.blake2b(material, digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


@dataclass(frozen=True)
class Node:
    """One inference node: how fast it prefills and how fast it decodes.

    `jitter` is a fraction (0.08 = ±8 %): the per-turn factor is drawn
    deterministically from `seed`, so the same node always produces the same
    series. `jitter=0.0` returns the configured rate untouched.
    """

    name: str
    pp_tps: float
    tg_tps: float
    jitter: float = 0.0
    seed: int = 0

    def _rate(self, base: float, kind: str, turn_index: int) -> float:
        if self.jitter == 0.0:
            return base
        factor = 1.0 + self.jitter * (2.0 * _unit(self.seed, self.name, kind, turn_index) - 1.0)
        return base * factor

    def pp_rate(self, turn_index: int) -> float:
        """Prefill tokens per second on `turn_index` (the turn's own noise)."""
        return self._rate(self.pp_tps, "pp", turn_index)

    def tg_rate(self, turn_index: int) -> float:
        """Decode tokens per second on `turn_index` (the turn's own noise)."""
        return self._rate(self.tg_tps, "tg", turn_index)


@dataclass(frozen=True)
class Turn:
    """One turn of one conversation: the prompt it arrives with, the answer."""

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class TurnRecord:
    """Where one turn's seconds went — so a reader sees *why*, not just a total."""

    index: int
    host: str
    reason: str
    prefill_s: float
    decode_s: float
    moved: bool

    @property
    def total_s(self) -> float:
        return self.prefill_s + self.decode_s

    @property
    def prefilled(self) -> bool:
        return self.prefill_s > 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "host": self.host,
            "reason": self.reason,
            "prefill_s": self.prefill_s,
            "decode_s": self.decode_s,
            "total_s": self.total_s,
            "moved": self.moved,
        }


@dataclass
class TraceResult:
    """The verdict of one run: the cost, the moves, and the turn-by-turn bill."""

    moves: int
    wall_clock_s: float
    per_turn: List[TurnRecord] = field(default_factory=list)

    @property
    def prefills(self) -> int:
        """Turns that paid a full prefill — the prefill storm, as a number."""
        return sum(1 for record in self.per_turn if record.prefilled)

    @property
    def hosts(self) -> List[str]:
        return [record.host for record in self.per_turn]

    def as_dict(self) -> Dict[str, object]:
        return {
            "moves": self.moves,
            "prefills": self.prefills,
            "wall_clock_s": self.wall_clock_s,
            "per_turn": [record.as_dict() for record in self.per_turn],
        }


#: `(host_name, reason)` for one turn. The interface the migration units plug into.
Decider = Callable[[int, int, Optional[str], Dict[str, Node], float], Tuple[str, str]]


def run_trace(turns: Sequence[Turn], nodes: Dict[str, Node], decider: Decider) -> TraceResult:
    """Run `turns` over `nodes`, asking `decider` where each turn should run.

    The decider is called with the turn's index, its prompt size, the node that
    holds the thread's prefix (`None` on the first turn), the fleet, and the
    simulated seconds elapsed so far. It returns `(host, reason)`; the reason is
    recorded verbatim, so the caller's vocabulary (`moved`, `no_repayment`,
    `pinned`, …) survives into the table.

    Raises `ValueError` if the decider names a host that is not in `nodes`: a
    silent fallback would make a broken decider look like a good one.
    """
    if not turns:
        return TraceResult(moves=0, wall_clock_s=0.0, per_turn=[])

    per_turn: List[TurnRecord] = []
    warm_host: Optional[str] = None
    previous_host: Optional[str] = None
    now_s = 0.0
    moves = 0

    for index, turn in enumerate(turns):
        host, reason = decider(index, turn.prompt_tokens, warm_host, nodes, now_s)
        if host not in nodes:
            raise ValueError(
                f"decider returned unknown host {host!r} on turn {index}; "
                f"nodes are {sorted(nodes)}"
            )
        node = nodes[host]

        # Warmth is the whole model: a node that holds this thread's prefix
        # prefills nothing, and only one node holds it.
        prefill_s = 0.0 if warm_host == host else turn.prompt_tokens / node.pp_rate(index)
        decode_s = turn.completion_tokens / node.tg_rate(index)
        moved = previous_host is not None and host != previous_host
        if moved:
            moves += 1

        duration = prefill_s + decode_s
        per_turn.append(
            TurnRecord(
                index=index,
                host=host,
                reason=reason,
                prefill_s=prefill_s,
                decode_s=decode_s,
                moved=moved,
            )
        )
        now_s += duration
        warm_host = host
        previous_host = host

    return TraceResult(moves=moves, wall_clock_s=now_s, per_turn=per_turn)


# --------------------------------------------------------------------------- #
# Deciders
# --------------------------------------------------------------------------- #


def stay(host: str) -> Decider:
    """Never move: every turn runs on `host`. The `enabled: false` baseline."""

    def decide(
        turn_index: int,
        prompt_tokens: int,
        warm_host: Optional[str],
        nodes: Dict[str, Node],
        now_s: float,
    ) -> Tuple[str, str]:
        return host, "stay"

    return decide


def greedy(start_host: str) -> Decider:
    """Always take the better decode curve, ignoring what the move costs.

    It exists to prove the simulator *measures* the difference rather than
    assuming it: on `one_slow_node` it buys 19 turns of decode for one prefill,
    and on a noisy fleet it can be made to buy one prefill per turn. Turn 0 has
    no warm host to improve on, so it lands on `start_host`; ties go to the warm
    host, so identical curves never move.
    """

    def decide(
        turn_index: int,
        prompt_tokens: int,
        warm_host: Optional[str],
        nodes: Dict[str, Node],
        now_s: float,
    ) -> Tuple[str, str]:
        if warm_host is None:
            return start_host, "start"
        best_rate = max(node.tg_rate(turn_index) for node in nodes.values())
        if nodes[warm_host].tg_rate(turn_index) >= best_rate:
            return warm_host, "greedy_already_best"
        # max() over a dict is insertion-ordered; sort by name so the tie-break
        # is a property of the fleet, not of how it was written down.
        target = max(sorted(nodes.values(), key=lambda node: node.name),
                     key=lambda node: node.tg_rate(turn_index))
        return target.name, "greedy_better_tg"

    return decide


# --------------------------------------------------------------------------- #
# The three traces
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Scenario:
    """Turns, nodes, and where the conversation starts — one runnable trace.

    Unpacks as `turns, nodes = one_slow_node()` for a test, and carries the
    start host a CLI needs: a `stay` or `greedy` decider has to be told where
    turn 0 lands, and that choice is part of the trace, not of the decider.
    """

    name: str
    turns: List[Turn]
    nodes: Dict[str, Node]
    start_host: str

    def __iter__(self):
        yield self.turns
        yield self.nodes


def _conversation(turns: int = TURNS) -> List[Turn]:
    return [
        Turn(prompt_tokens=PROMPT_START + PROMPT_STEP * index, completion_tokens=COMPLETION)
        for index in range(turns)
    ]


def equal_curves() -> Scenario:
    """Two nodes, identical curves, one thread, 20 turns: nothing to win."""
    nodes = {
        FAST: Node(FAST, pp_tps=PP_TPS, tg_tps=TG_FAST),
        SLOW: Node(SLOW, pp_tps=PP_TPS, tg_tps=TG_FAST),
    }
    return Scenario("equal_curves", _conversation(), nodes, FAST)


def one_slow_node() -> Scenario:
    """One node 30 % slower in decode, one long conversation: the trace that measures."""
    nodes = {
        FAST: Node(FAST, pp_tps=PP_TPS, tg_tps=TG_FAST),
        SLOW: Node(SLOW, pp_tps=PP_TPS, tg_tps=TG_SLOW),
    }
    return Scenario("one_slow_node", _conversation(), nodes, SLOW)


def noisy_fleet(seed: int = 95, jitter: float = 0.08) -> Scenario:
    """`one_slow_node` through ±8 % per-turn noise, deterministic from `seed`.

    The noise never inverts the curves (28 × 1.08 = 30.24 < 40 × 0.92 = 36.8):
    it perturbs the *magnitude* of the gap, which is what a fitted curve does,
    and leaves the decision's direction intact. A trace that flipped the sign
    would be measuring noise, not the feature.
    """
    nodes = {
        FAST: Node(FAST, pp_tps=PP_TPS, tg_tps=TG_FAST, jitter=jitter, seed=seed),
        SLOW: Node(SLOW, pp_tps=PP_TPS, tg_tps=TG_SLOW, jitter=jitter, seed=seed),
    }
    return Scenario("noisy_fleet", _conversation(), nodes, SLOW)


TRACES: Dict[str, Callable[..., Scenario]] = {
    "equal_curves": equal_curves,
    "one_slow_node": one_slow_node,
    "noisy_fleet": noisy_fleet,
}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _table(result: TraceResult, scenario: Scenario) -> str:
    curves = ", ".join(
        f"{name} (pp {node.pp_tps:g}, tg {node.tg_tps:g}"
        + (f" ±{node.jitter:.0%}" if node.jitter else "")
        + ")"
        for name, node in scenario.nodes.items()
    )
    lines = [f"trace {scenario.name}: {curves}", ""]
    lines.append("turn  host      prefill_s  decode_s  total_s   moved  reason")
    for record in result.per_turn:
        lines.append(
            f"{record.index + 1:>4}  {record.host:<8}  {record.prefill_s:>9.3f}  "
            f"{record.decode_s:>8.3f}  {record.total_s:>7.3f}  "
            f"{'yes' if record.moved else '-':>5}  {record.reason}"
        )
    lines.append("")
    lines.append(f"moves {result.moves} · prefills {result.prefills} of {len(result.per_turn)} turns")
    lines.append(f"wall clock {result.wall_clock_s:.3f} s")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.trace_migrate",
        description="Run one of §6.4's traces and print where the seconds went.",
    )
    parser.add_argument("--trace", choices=sorted(TRACES), default="one_slow_node")
    parser.add_argument("--decider", choices=("stay", "greedy"), default="stay")
    parser.add_argument(
        "--host",
        default=None,
        help="where turn 0 lands (default: the trace's own start host)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    scenario = TRACES[args.trace]()
    host = args.host or scenario.start_host
    if host not in scenario.nodes:
        parser.error(f"--host {host!r} is not in this trace: {sorted(scenario.nodes)}")

    decider = stay(host) if args.decider == "stay" else greedy(host)
    result = run_trace(scenario.turns, scenario.nodes, decider)

    if args.json:
        payload = {"trace": scenario.name, "decider": args.decider, "start_host": host}
        payload.update(result.as_dict())
        print(json.dumps(payload, indent=2))
    else:
        print(_table(result, scenario))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
