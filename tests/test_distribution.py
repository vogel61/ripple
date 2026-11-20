"""Thread distribution: AS-1, AS-2, AS-4, FA-2 (reservation + hysteresis), FA-3, FA-5.

The 3 : 2 split is a property of the deficit rule (FA-2): every *new* thread goes
to the host that is furthest below its target share.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from dataclasses import dataclass

import pytest

from ripple.config import AffinityConfig, HostConfig, PlacementConfig
from ripple.placement import TIER_RESIDENT, Candidate, _deficit, imbalance
from ripple.registry import Thread, ThreadRegistry

from tests.support import (chat_body, host_config, make_config, prompt_for,
                           stub_model_loaded, wait_until)


class _Loads:
    """Minimal registry stand-in for the hysteresis unit test."""

    def __init__(self, loads: dict[str, int]) -> None:
        self.loads = loads

    def slots(self, host: str, model: str) -> int:
        return self.loads.get(host, 0)

    def ephemeral_count(self, host: str, model: str) -> int:
        return 0


@dataclass
class _FakeHost:
    cfg: HostConfig


def _two_hosts(stub_factory, *, weight_a: float = 3.0, weight_b: float = 2.0,
               **host_overrides) -> list[HostConfig]:
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    return [
        host_config("A", a.url, node_id=a.state.node_id, weight=weight_a,
                    **host_overrides),
        host_config("B", b.url, node_id=b.state.node_id, weight=weight_b,
                    **host_overrides),
    ]


async def _open_threads(harness, count: int, *, model: str = "m1") -> list[str]:
    """Open `count` threads with two turns each and return the host per thread."""
    hosts: list[str] = []
    for i in range(count):
        first = await harness.ok(chat_body(model, prompt_for(i), turns=2))
        second = await harness.ok(chat_body(model, prompt_for(i), turns=3))
        assert second.headers["x-ripple-host"] == first.headers["x-ripple-host"]
        assert second.headers["x-ripple-reason"] == "thread"
        hosts.append(first.headers["x-ripple-host"])
    return hosts


async def test_as1_five_threads_split_three_to_two(stub_factory, ripple_factory):
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    hosts = await _open_threads(harness, 5)

    assert Counter(hosts) == {"A": 3, "B": 2}
    # FA-13: /healthz reports the occupied slots per host
    assert await harness.slots() == {"A": 3, "B": 2}
    health = await harness.healthz()
    assert health["threads"]["active"] == 5
    assert health["threads"]["target_share"] == {"A": 0.6, "B": 0.4}


async def test_as2_ten_threads_split_six_to_four(stub_factory, ripple_factory):
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    hosts = await _open_threads(harness, 10)

    assert Counter(hosts) == {"A": 6, "B": 4}
    assert await harness.slots() == {"A": 6, "B": 4}


async def test_as4_twenty_short_requests_split_twelve_to_eight(
        stub_factory, ripple_factory):
    """FA-7: short single-message requests hold no slot but still count for the
    split for `ephemeral_window_seconds`."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))

    hosts: list[str] = []
    for i in range(20):
        resp = await harness.ok(chat_body("m1", prompt_for(i), turns=1))
        assert resp.headers["x-ripple-reason"] == "ephemeral"
        hosts.append(resp.headers["x-ripple-host"])

    assert Counter(hosts) == {"A": 12, "B": 8}
    # no thread survived the request: nothing holds a slot
    assert await harness.slots() == {"A": 0, "B": 0}
    health = await harness.healthz()
    assert health["threads"]["active"] == 0
    assert (await harness.stats())["threads"] == []


def test_fa2_idle_threads_fade_quadratically() -> None:
    """The weight a thread contributes to the deficit: 1.0 while in flight, then
    `(1 - idle/decay)²`. Measured 14.11.2026 — two threads of the operator's own
    session, idle for 10 and 23 minutes, were the reason every new conversation
    went to the weaker node although nothing was running anywhere."""
    now = time.time()          # real clock: slots() counts against the TTL
    reg = ThreadRegistry(AffinityConfig())

    def add(key: str, idle: float, *, active: int = 0) -> None:
        reg.threads[key] = Thread(key=key, model="m1", host="A", prefix="p",
                                  source="header", created=now - 3600,
                                  last_seen=now - idle, active=active)

    add("hot", 0.0)
    assert reg.weighted_slots("A", "m1", 300, now) == 1.0

    add("tenth", 30.0)      # 0.9² = 0.81
    add("half", 150.0)      # 0.5² = 0.25
    assert round(reg.weighted_slots("A", "m1", 300, now), 6) == 2.06

    add("silent", 400.0)    # past the window: invisible for binding
    assert round(reg.weighted_slots("A", "m1", 300, now), 6) == 2.06

    # A request in flight on an old thread counts fully — real work is never
    # invisible, only the silent tail is.
    add("busy", 299.0, active=1)
    assert round(reg.weighted_slots("A", "m1", 300, now), 6) == 3.06

    # The hard slot limit stays raw — all five live threads hold a slot.
    assert reg.slots("A", "m1") == 5


async def test_fa2_idle_threads_do_not_attract_new_threads(stub_factory, ripple_factory):
    """Regression for the live case: 3:2 established, then everything goes idle.

    Before the decay the two threads on the entry host kept pulling every new
    thread onto the other node for the whole 30 min TTL — the stronger host
    stayed empty while both were idle. Now they fade out and the new thread
    lands where the weight says it belongs (A, 3 : 2)."""
    cfg = make_config(_two_hosts(stub_factory),
                      placement=PlacementConfig(thread_decay_seconds=2.0))
    harness = await ripple_factory(cfg)

    assert Counter(await _open_threads(harness, 5)) == {"A": 3, "B": 2}
    assert await harness.slots() == {"A": 3, "B": 2}

    await asyncio.sleep(2.3)     # every thread is now past its decay window

    resp = await harness.ok(chat_body("m1", prompt_for(5), turns=2))
    assert resp.headers["x-ripple-reason"] == "new_thread"
    assert resp.headers["x-ripple-host"] == "A"
    # the binding of the old threads is untouched, they only stopped counting
    assert await harness.slots() == {"A": 4, "B": 2}


async def test_fa2_atomic_reservation_never_overshoots_one_host(
        stub_factory, ripple_factory):
    """Four threads opened at the same time: 3/1 or 2/2, never 4/0.

    Placement and slot reservation happen under the admission lock, so the four
    concurrent requests must not all read the same state and pick host A.
    """
    a = stub_factory(models=[stub_model_loaded("m1")], delay=0.05)
    b = stub_factory(models=[stub_model_loaded("m1")], delay=0.05)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=3),
        host_config("B", b.url, node_id=b.state.node_id, weight=2),
    ]))

    bodies = [chat_body("m1", prompt_for(i), turns=2) for i in range(4)]
    responses = await asyncio.gather(*(harness.chat(body) for body in bodies))

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    counts = Counter(r.headers["x-ripple-host"] for r in responses)
    assert (counts["A"], counts["B"]) in {(3, 1), (2, 2)}
    assert counts["A"] != 4
    assert await harness.slots() == {"A": counts["A"], "B": counts["B"]}


def test_fa2_hysteresis_needs_both_thresholds():
    """`imbalance` is an AND of an absolute and a relative threshold — with an
    OR one of the two would never take effect."""
    loads = _Loads({"A": 5, "B": 1})
    cands = [Candidate(host=_FakeHost(host_config("A", "http://a:1", weight=1)),
                       tier=TIER_RESIDENT),
             Candidate(host=_FakeHost(host_config("B", "http://b:1", weight=1)),
                       tier=TIER_RESIDENT)]
    cfg = make_config([])
    assert imbalance(cands, loads, cfg, "m1")["imbalanced"] is True

    # absolute threshold exceeded (1.33 > 1) but relative not (1.44 <= 1.5)
    loads = _Loads({"A": 13, "B": 6})
    cands = [Candidate(host=_FakeHost(host_config("A", "http://a:1", weight=3)),
                       tier=TIER_RESIDENT),
             Candidate(host=_FakeHost(host_config("B", "http://b:1", weight=2)),
                       tier=TIER_RESIDENT)]
    report = imbalance(cands, loads, cfg, "m1")
    assert report["spread"] > 1.0
    assert report["ratio"] <= 1.5
    assert report["imbalanced"] is False

    # relative threshold exceeded (10 > 1.5) but absolute not (0.9 <= 1)
    loads = _Loads({"A": 1, "B": 1})
    cands = [Candidate(host=_FakeHost(host_config("A", "http://a:1", weight=10)),
                       tier=TIER_RESIDENT),
             Candidate(host=_FakeHost(host_config("B", "http://b:1", weight=1)),
                       tier=TIER_RESIDENT)]
    report = imbalance(cands, loads, cfg, "m1")
    assert report["ratio"] > 1.5
    assert report["spread"] <= 1.0
    assert report["imbalanced"] is False


async def test_fa3_weight_change_shifts_the_distribution(stub_factory,
                                                         ripple_factory):
    """FA-3: a restart with different weights moves the split, no code change."""
    three_two = await ripple_factory(make_config(_two_hosts(stub_factory)))
    assert Counter(await _open_threads(three_two, 5)) == {"A": 3, "B": 2}

    two_three = await ripple_factory(make_config(
        _two_hosts(stub_factory, weight_a=2.0, weight_b=3.0)))
    assert Counter(await _open_threads(two_three, 5)) == {"A": 2, "B": 3}


async def test_fa5_spill_threshold_moves_new_thread_to_the_other_host(
        stub_factory, ripple_factory):
    """With `spill_threshold: 1` a host counts as full while one request runs.

    Weights 9 : 1 make the deficit prefer host A, so only the spill threshold can
    send the second thread to B.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=9,
                    spill_threshold=1),
        host_config("B", b.url, node_id=b.state.node_id, weight=1,
                    spill_threshold=1),
    ]))

    a.state.delay = 0.6
    slow = asyncio.create_task(
        harness.ok(chat_body("m1", prompt_for(0), turns=2)))
    await wait_until(lambda: a.state.chat_calls == 1)

    second = await harness.ok(chat_body("m1", prompt_for(1), turns=2))
    assert second.headers["x-ripple-host"] == "B"
    assert second.headers["x-ripple-reason"] == "new_thread"

    await slow
    a.state.delay = 0.0

    # control: with A idle again the deficit rule prefers A
    third = await harness.ok(chat_body("m1", prompt_for(2), turns=2))
    assert third.headers["x-ripple-host"] == "A"


# ---------------------------------------------------------------------------
# Issue #14: running work is load, and one conversation must not starve a host
# ---------------------------------------------------------------------------
@dataclass
class _Runtime:
    """What `_deficit` reads off a host: its config and its `inflight`."""

    cfg: HostConfig
    inflight: int = 0


@dataclass
class _Fleet:
    hosts: list


def _deficit_of(hosts: list[_Runtime], cfg, registry: ThreadRegistry,
                name: str) -> float:
    cand = Candidate(host=next(h for h in hosts if h.cfg.name == name),
                     tier=TIER_RESIDENT)
    return _deficit(cand, cfg, registry, "m1", _Fleet(hosts))


def _fleet_3_2() -> tuple[list[_Runtime], object]:
    """alpha : beta = 3 : 2, the live weights."""
    hosts = [_Runtime(host_config("alpha", "http://m5:1", weight=3.0)),
             _Runtime(host_config("beta", "http://m3:1", weight=2.0))]
    return hosts, make_config([h.cfg for h in hosts])


def test_fa2_a_running_generation_counts_as_load_after_its_window_expired() -> None:
    """Acceptance (issue #14): a 200 s generation is still load after 60 s.

    The ephemeral window is 60 s, so the entry of a generation that started
    200 s ago is pruned. `inflight` keeps it visible: the host scores exactly
    as if it held an occupied slot.
    """
    hosts, cfg = _fleet_3_2()
    registry = ThreadRegistry(AffinityConfig(ephemeral_window_seconds=60))
    # The entry a 200 s generation left behind: already outside the window.
    registry._ephemeral[("beta", "m1")] = deque([time.time() - 200])
    assert registry.ephemeral_count("beta", "m1") == 0

    idle = _deficit_of(hosts, cfg, registry, "beta")
    hosts[1].inflight = 1                     # the generation is still running
    running = _deficit_of(hosts, cfg, registry, "beta")

    # And exactly as a host with one occupied slot scores.
    busy = ThreadRegistry(AffinityConfig())
    busy.register(key="seed", model="m1", host="beta", text="x",
                  holds_slot=True, source="header")
    occupied = _deficit_of([hosts[0], _Runtime(hosts[1].cfg, 0)], cfg, busy, "beta")

    assert running == pytest.approx(occupied, abs=1e-6)
    assert running < idle - 0.3, "the running generation was invisible"
    # The other host sees it too: the load is in the fleet total.
    assert _deficit_of(hosts, cfg, registry, "alpha") > _deficit_of(
        [hosts[0], _Runtime(hosts[1].cfg, 0)], cfg, registry, "alpha")


def test_fa2_the_same_work_is_not_counted_twice() -> None:
    """A bound thread with a request in flight stays 1.0 — `max`, not `+`."""
    hosts, cfg = _fleet_3_2()
    registry = ThreadRegistry(AffinityConfig())
    registry.register(key="seed", model="m1", host="alpha", text="x",
                      holds_slot=True, source="header")
    thread = registry.threads["seed"]

    registry.enter(thread)                    # its request is running
    assert hosts[0].inflight == 0
    without = _deficit_of(hosts, cfg, registry, "alpha")
    hosts[0].inflight = 1
    with_inflight = _deficit_of(hosts, cfg, registry, "alpha")

    assert with_inflight == without


def test_fa2_a_host_never_claims_more_than_one_thread_of_deficit() -> None:
    """The share term is bounded by what one placement can close (issue #14).

    One decision places one thread, so a host that is many threads behind may
    claim at most one thread's worth: the share term must not grow without
    bound as threads pile up on the other node (three hosts, so the cap is
    reachable — with the two hosts of this fleet the other host's score is
    already negative whenever this one would exceed one slot).
    """
    hosts = [_Runtime(host_config("A", "http://a:1", weight=2.0)),
             _Runtime(host_config("B", "http://b:1", weight=1.0)),
             _Runtime(host_config("C", "http://c:1", weight=1.0))]
    cfg = make_config([h.cfg for h in hosts])
    registry = ThreadRegistry(AffinityConfig())
    for i in range(20):                       # everything piled up on A
        registry.register(key=f"a{i}", model="m1", host="A", text=f"a{i}",
                          holds_slot=True, source="header")

    assert _deficit_of(hosts, cfg, registry, "C") == 1.0


async def test_fa2_one_conversation_does_not_starve_the_entry_host(
        stub_factory, ripple_factory):
    """Acceptance (issue #14): 10 real agent turns with one thread on the M5.

    Reproduces the live case: the operator's session sits on the entry host, the
    subagent turns are long (each outlives the 60 s ephemeral window), and the
    M3 was idle. Sized from the user text those turns were "ephemeral": they
    held no slot, their entry expired while they generated, the M3 looked empty
    forever and took **all ten**. Sized from the whole prompt they are threads,
    the M3's finished work counts for its window, and the burst splits 3 : 2
    toward the faster host — the weight the fleet was configured with.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    a.state.delay = 0.5                       # longer than the window below
    b.state.delay = 0.5
    harness = await ripple_factory(make_config([
        host_config("alpha", a.url, node_id=a.state.node_id, weight=3.0),
        host_config("beta", b.url, node_id=b.state.node_id, weight=2.0),
    ], affinity=AffinityConfig(ephemeral_window_seconds=0.3)))

    # The operator's own conversation, already bound to the entry host.
    harness.ripple.registry.register(key="sid:operator", model="m1",
                                     host="alpha", text="the operator's session",
                                     holds_slot=True, source="header")

    hosts = []
    for i in range(10):
        body = {"model": "m1", "messages": [
            {"role": "system", "content": "S" * 300_000},
            {"role": "user", "content": prompt_for(i)},
        ]}
        resp = await harness.ok(body)
        assert resp.headers["x-ripple-reason"] == "new_thread"
        hosts.append(resp.headers["x-ripple-host"])

    assert Counter(hosts)["alpha"] >= 6, hosts        # at least 3 : 2
    assert Counter(hosts)["beta"] <= 4, hosts


async def test_fa2_a_finished_short_request_keeps_counting_for_its_window(
        stub_factory, ripple_factory):
    """The ephemeral window starts when the request is done, not when it starts.

    Otherwise a request that generates for longer than the window is invisible
    from the moment it ends — the host that just did the work looks idle and
    takes the next one (issue #14, cause 3).
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    a.state.delay = 0.12
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id, weight=1.0),
        host_config("B", b.url, node_id=b.state.node_id, weight=1.0),
    ], affinity=AffinityConfig(ephemeral_window_seconds=0.05)))

    resp = await harness.ok(chat_body("m1", prompt_for(0), turns=1))
    host = resp.headers["x-ripple-host"]

    assert resp.headers["x-ripple-reason"] == "ephemeral"
    assert harness.ripple.registry.ephemeral_count(host, "m1") == 1
