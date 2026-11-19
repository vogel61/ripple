"""The periodic optimizer (issue #80): the decision runs on a timer, not on the
request path.

What these tests pin, and why each one matters:

* **the move happens** — one scan moves the idle thread the decider named, with
  the existing bookkeeping (`note_rebind`, `threads_rebound`, the log line);
* **it happens once** — the per-thread cooldown, not a second look, is what stops
  a thread from flapping between two hosts;
* **the budget is the optimizer's own** — `placement.optimizer.max_moves_per_hour`
  is a second number next to `placement.rebind.max_per_hour`, and a scan must not
  spend the other one's allowance;
* **`enabled: false` means no task at all**, not a task that decides to do
  nothing;
* **the request path decides nothing** — a request on a thread that would qualify
  for a move stays where it is and says `thread`, because a decision taken while
  the request is being admitted either throws its work away or blocks it.
"""

from __future__ import annotations

import logging

from ripple.config import OptimizerConfig, PlacementConfig, RebindConfig

from tests.support import chat_body, host_config, make_config, stub_model_loaded
from tests.test_placement import M3_CURVE, M5_CURVE, seed_curve

M5_PP = 3680.0
M3_PP = 970.0


def _two_hosts(a, b, **placement) -> object:
    hosts = [
        host_config("A", a.url, node_id=a.state.node_id, weight=3, pp_tps=M5_PP),
        host_config("B", b.url, node_id=b.state.node_id, weight=2, pp_tps=M3_PP),
    ]
    plc = PlacementConfig(rebind=RebindConfig(per_thread_cooldown_seconds=600.0,
                                              max_per_hour=10),
                          optimizer=placement.pop("optimizer", OptimizerConfig()))
    for key, value in placement.items():
        setattr(plc, key, value)
    return make_config(hosts, placement=plc)


def _seed(harness, *, loaded: str = "A") -> None:
    """Give both nodes the measured curves and the live rates of the issue."""
    seed_curve(harness.host("A"), M5_CURVE)
    seed_curve(harness.host("B"), M3_CURVE)
    harness.host("A").metrics["m1"] = {"tg_tps": 18.3, "tg_samples": 9}
    harness.host("B").metrics["m1"] = {"tg_tps": 43.5, "tg_samples": 9}


def _threads(harness, host: str, count: int, *, tokens: int = 4_000) -> list:
    out = []
    for i in range(count):
        out.append(harness.ripple.registry.register(
            key=f"sid:{host}{i}", model="m1", host=host, text=f"prompt {i}",
            holds_slot=True, source="fingerprint", prompt_tokens=tokens))
    return out


async def test_one_scan_moves_the_idle_thread_and_logs_the_numbers(
        stub_factory, ripple_factory, caplog) -> None:
    """Five threads on A (18.3 of 60.2 tok/s), B idle: one scan moves one thread.

    Δtotal = [1·43.5 + 4·22.0] − [5·18.3] = +40.0 tok/s, cost 4 000/970 = 4.1 s.
    The log line is the third view of the decision and must carry the numbers.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_two_hosts(a, b))
    _seed(harness)
    threads = _threads(harness, "A", 5)

    with caplog.at_level(logging.WARNING, logger="ripple.proxy"):
        plan = await harness.ripple.optimize_once()

    assert plan.moves, plan.reason
    assert plan.src == "A" and plan.dst == "B"
    assert plan.delta_total == 40.0
    moved = plan.thread
    assert moved is not None and moved.host == "B", "the thread is bound to B now"
    assert moved.last_rebind, "the cooldown clock started"
    assert (await harness.stats())["threads_rebound"] == 1

    line = next(r.getMessage() for r in caplog.records if "rebind" in r.getMessage())
    assert "src=A" in line and "dst=B" in line
    assert "delta_total=+40.0" in line
    assert "gain_seconds=" in line and "cost_seconds=" in line
    # Exactly one thread left; the other four stayed.
    assert sum(1 for t in threads if t.host == "A") == 4


async def test_the_same_thread_does_not_move_twice_inside_the_cooldown(
        stub_factory, ripple_factory) -> None:
    """The second scan sees the same fleet and must not move the thread again."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_two_hosts(a, b))
    _seed(harness)
    _threads(harness, "A", 5)

    first = await harness.ripple.optimize_once()
    assert first.moves, first.reason
    moved = first.thread
    assert moved is not None

    later = [await harness.ripple.optimize_once() for _ in range(3)]

    assert moved.host == "B", "the moved thread stays where it was moved to"
    assert all(p.thread_key != first.thread_key for p in later), \
        "no scan moves it back inside the cooldown"
    assert (await harness.stats())["threads_rebound"] == 1


async def test_the_optimizer_budget_is_its_own(stub_factory, ripple_factory) -> None:
    """`max_moves_per_hour: 1` — the second scan refuses, and says so."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_two_hosts(
        a, b, optimizer=OptimizerConfig(max_moves_per_hour=1)))
    _seed(harness)
    _threads(harness, "A", 5)

    assert (await harness.ripple.optimize_once()).moves
    assert harness.ripple.optimizer_moves_last_hour() == 1

    # A second, freshly created thread would otherwise be movable.
    harness.ripple.registry.register(key="sid:late", model="m1", host="A",
                                     text="late", holds_slot=True,
                                     source="fingerprint", prompt_tokens=1_000)
    blocked = await harness.ripple.optimize_once()
    assert not blocked.moves
    assert blocked.reason == "budget"


async def test_disabled_starts_no_task(stub_factory, ripple_factory) -> None:
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_two_hosts(
        a, b, optimizer=OptimizerConfig(enabled=False)))

    assert harness.ripple.start_optimizer() is None
    assert harness.ripple._optimizer_task is None
    assert (await harness.ripple.optimize_once()).reason == "disabled"


async def test_the_request_path_does_not_rebind(stub_factory, ripple_factory) -> None:
    """A request on a thread that a scan would move stays where it is.

    This is the removal that #80 asks for: the request path may refuse (503), may
    fall back when the host is gone (FA-11) — but it never moves a thread, because
    the thread it would move is the one whose request is being admitted.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_two_hosts(a, b))
    _seed(harness)
    _threads(harness, "A", 5)
    # The conversation the request will use lives on A, the loaded host.
    thread = harness.ripple.registry.register(
        key="sid:conv", model="m1", host="A", text="a conversation",
        holds_slot=True, source="fingerprint", prompt_tokens=4_000)

    answer = await harness.ok(chat_body("m1", "a conversation", turns=2),
                              headers={"X-Session-Id": "conv"})

    assert answer.headers["x-ripple-host"] == "A", "no move on the request path"
    assert answer.headers["x-ripple-reason"] == "thread"
    assert thread.host == "A"
    assert (await harness.stats())["threads_rebound"] == 0
