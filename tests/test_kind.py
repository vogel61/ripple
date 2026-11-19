"""Traffic classes: promotion on adoption and the rebind veto (FA-18, FA-19).

Two refinements on top of the class handling from `7f98702`:

* **promotion** — a batch thread that an interactive request adopts by prefix
  match is raised to interactive. A person is waiting on that conversation now,
  and a 120 s batch TTL would pull the prefix cache out from under it. One-way:
  there is no demotion, and it happens exactly once per thread.
* **no rebind for batch threads** — inside a 120 s TTL a prefill move cannot
  earn itself back, and `placement.rebind.assumed_completion_tokens` can
  over-estimate a short batch answer into an apparent gain. `plan_rebind()`
  returns `batch_no_rebind` without computing a cost at all.

The rates in the rebind tests are the measured ones (AS-6): decode M5 60.2 /
M3 43.5 tok/s, cold prefill M5 3 680 / M3 970 tok/s.
"""

from __future__ import annotations

import time

from ripple.backends import ModelInfo
from ripple.config import (AffinityConfig, Config, HostConfig, PlacementConfig,
                          RebindConfig)
from ripple.hosts import HostRuntime
from ripple.placement import plan_rebind
from ripple.registry import Thread, ThreadRegistry

from tests.support import (bearer, chat_body, host_config, keyed_config,
                           stub_model_loaded)

M5_TG, M3_TG = 60.2, 43.5
M5_PP, M3_PP = 3680.0, 970.0

# Long enough to hold a slot (2 user messages) and long enough for the stored
# 4096-character prefix to be worth matching.
LONG_OPENING = ("Placement is decided per thread, not per request. " * 120)[:5000]


def _rewritten_tail() -> str:
    """The same opening with a rewritten tail — a compression, not a new topic.

    The first user message differs, so the fingerprint cannot hit; the opening
    still matches the stored prefix, so the thread is adopted by prefix match.
    """
    return LONG_OPENING[:4000] + " [compressed] the rest was summarised by the client"


# ---------------------------------------------------------------------------
# Promotion: an interactive request adopts a batch thread
# ---------------------------------------------------------------------------
async def test_interactive_adoption_promotes_a_batch_thread(
        stub_factory, ripple_factory, tmp_path) -> None:
    """The TTL switches from 120 s to 1800 s and the thread is counted once.

    Without the promotion the conversation would be purged 120 s after the
    person's first turn, taking the prefix cache with it.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml"),
        affinity=AffinityConfig(ttl_seconds=1800, batch_ttl_seconds=120)))
    job = harness.ripple.keys.create("nightly", "batch")
    human = harness.ripple.keys.create("hermes", "interactive")

    first = await harness.ok(chat_body("m1", LONG_OPENING, turns=2),
                             headers=bearer(job.key))
    label = first.headers["x-ripple-thread"]
    row = (await harness.stats())["threads"][0]
    assert row["kind"] == "batch"
    assert row["ttl_seconds"] == 120

    second = await harness.ok(chat_body("m1", _rewritten_tail(), turns=2),
                              headers=bearer(human.key))
    assert second.headers["x-ripple-reason"] == "thread"
    assert second.headers["x-ripple-thread"] == label

    stats = await harness.stats()
    assert stats["prefix_matches"] == 1
    assert stats["threads_promoted"] == 1
    row = stats["threads"][0]
    assert row["kind"] == "interactive"
    assert row["ttl_seconds"] == 1800

    registry = harness.ripple.registry
    assert len(registry.threads) == 1
    thread = next(iter(registry.threads.values()))
    assert registry.ttl_of(thread) == 1800
    # 121 s after the last turn: gone as a batch thread, still alive as this one
    assert not registry.expired(thread, now=time.time() + 121)

    # exactly once: a third turn of the same conversation does not count again
    third = await harness.ok(chat_body("m1", _rewritten_tail(), turns=3),
                             headers=bearer(human.key))
    assert third.headers["x-ripple-thread"] == label
    assert (await harness.stats())["threads_promoted"] == 1


async def test_batch_adoption_does_not_demote_an_interactive_thread(
        stub_factory, ripple_factory, tmp_path) -> None:
    """There is no way back: the class of a conversation only ever rises.

    A batch job that happens to reuse a conversation's prefix does not make that
    conversation less urgent — demoting it would hand a person's thread the
    120 s TTL.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml"),
        affinity=AffinityConfig(ttl_seconds=1800, batch_ttl_seconds=120)))
    human = harness.ripple.keys.create("hermes", "interactive")
    job = harness.ripple.keys.create("nightly", "batch")

    first = await harness.ok(chat_body("m1", LONG_OPENING, turns=2),
                             headers=bearer(human.key))
    second = await harness.ok(chat_body("m1", _rewritten_tail(), turns=2),
                              headers=bearer(job.key))
    assert second.headers["x-ripple-reason"] == "thread"
    assert second.headers["x-ripple-thread"] == first.headers["x-ripple-thread"]

    stats = await harness.stats()
    assert stats["prefix_matches"] == 1
    assert stats["threads_promoted"] == 0
    row = stats["threads"][0]
    assert row["kind"] == "interactive"
    assert row["ttl_seconds"] == 1800
    assert harness.ripple.registry.ttl_of(
        next(iter(harness.ripple.registry.threads.values()))) == 1800


async def test_one_shot_still_adopts_nothing(stub_factory, ripple_factory,
                                             tmp_path) -> None:
    """Regression: the one-shot contract is untouched by the promotion.

    It would match by prefix, but it neither takes the thread over nor changes
    its class — traffic that never comes back must not keep a cache warm.
    """
    stub = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(keyed_config(
        [host_config("A", stub.url, node_id=stub.state.node_id)],
        str(tmp_path / "keys.yaml")))
    human = harness.ripple.keys.create("hermes", "interactive")
    probe = harness.ripple.keys.create("probe", "one-shot")

    first = await harness.ok(chat_body("m1", LONG_OPENING, turns=2),
                             headers=bearer(human.key))
    one = await harness.ok(chat_body("m1", _rewritten_tail(), turns=2),
                           headers=bearer(probe.key))
    assert one.headers["x-ripple-reason"] == "one_shot"
    assert "x-ripple-thread" not in one.headers

    stats = await harness.stats()
    assert stats["requests_one_shot"] == 1
    assert stats["thread_hits"] == 0
    assert stats["prefix_matches"] == 0
    assert stats["threads_promoted"] == 0
    assert len(stats["threads"]) == 1
    assert stats["threads"][0]["kind"] == "interactive"
    assert stats["threads"][0]["key"] == first.headers["x-ripple-thread"]


# ---------------------------------------------------------------------------
# Rebind: a batch thread never moves
# ---------------------------------------------------------------------------
class _FakeFleet:
    def __init__(self, hosts: list[HostRuntime]) -> None:
        self.hosts = hosts

    def get(self, name: str) -> HostRuntime | None:
        for h in self.hosts:
            if h.cfg.name == name:
                return h
        return None


def _host(name: str, *, tg: float, pp: float) -> HostRuntime:
    cfg = HostConfig(name=name, addresses=[f"http://{name}.test:8000"], weight=1.0,
                     pp_tps=pp, tg_tps=tg, spill_threshold=4, max_threads=20)
    h = HostRuntime(cfg=cfg, client=None)   # no HTTP needed for the decision
    h.healthy = True
    h.status = "healthy"
    h.identity_ok = True
    h.models["m1"] = ModelInfo(id="m1", available=True, loaded=True)
    return h


def _thread(host: str, *, kind: str = "interactive",
            ttl_seconds: float | None = None, idle: float = 600.0) -> Thread:
    now = time.time()
    return Thread(key="k", model="m1", host=host, prefix="p", source="fingerprint",
                  created=now - 3600, last_seen=now - idle, kind=kind,
                  ttl_seconds=ttl_seconds)


def _cfg(**rebind) -> Config:
    return Config(hosts=[], placement=PlacementConfig(
        thread_decay_seconds=300.0, rebind=RebindConfig(**rebind)))


def _fleet() -> _FakeFleet:
    return _FakeFleet([_host("M5", tg=M5_TG, pp=M5_PP),
                       _host("M3", tg=M3_TG, pp=M3_PP)])


def test_a_batch_thread_is_never_moved_even_when_the_gain_looks_huge() -> None:
    """A batch thread is refused before any cost is computed.

    The 120 s TTL cannot amortise a prefill, and an assumed completion would only
    make the paper gain look better than the real one. Since issue #80 the rule
    lives in two places, and both are asserted here: `idle_threads` never offers a
    batch thread as a candidate, and a host whose only idle work is batch says so
    instead of a vague `no_target`.
    """
    fleet = _fleet()
    reg = ThreadRegistry(AffinityConfig())
    thread = _thread("M3", kind="batch", ttl_seconds=120.0, idle=60.0)
    reg.threads["k"] = thread

    assert reg.idle_threads("M3") == []

    plan = plan_rebind(fleet, _cfg(), reg)
    assert not plan.moves
    assert plan.reason == "batch_no_rebind"
    assert plan.dst is None
    assert (plan.gain_seconds, plan.cost_seconds) == (0.0, 0.0)


def test_a_promoted_thread_is_rebindable_again() -> None:
    """Promotion is the only thing that makes a batch-born thread movable.

    Judged normally means judged by the same rules as any other thread — and on a
    fleet whose nodes have no measured curve that answer is `no_curve`, not the
    batch refusal.
    """
    fleet = _fleet()
    reg = ThreadRegistry(AffinityConfig())
    thread = _thread("M3", kind="batch", ttl_seconds=120.0, idle=60.0)
    reg.threads["k"] = thread

    assert plan_rebind(fleet, _cfg(), reg).reason == "batch_no_rebind"

    assert reg.promote(thread) is True
    plan = plan_rebind(fleet, _cfg(), reg)
    assert plan.reason != "batch_no_rebind", "a promoted thread is judged normally"
    assert reg.promote(thread) is False       # exactly once per thread


def test_a_promoted_thread_is_no_longer_preferred_for_eviction() -> None:
    """The interactive-first order drops finished *batch* threads; a promoted one
    is an ordinary conversation again and only pure age can take it."""
    reg = ThreadRegistry(AffinityConfig())
    now = time.time()
    human = reg.register(key="human", model="m1", host="A", text="x",
                         holds_slot=True, source="header", kind="interactive")
    human.last_seen = now - 300                     # the older of the two
    job = reg.register(key="job", model="m1", host="A", text="y", holds_slot=True,
                       source="header", kind="batch", ttl_seconds=120)
    job.last_seen = now - 60                        # younger, but batch

    assert reg.evictable("A", "m1", prefer_kind="batch") is job
    assert reg.promote(job) is True
    assert reg.evictable("A", "m1", prefer_kind="batch") is human

    # …and it ages by the interactive TTL from that moment on
    assert job.kind == "interactive"
    assert reg.ttl_of(job) == 1800
    assert not reg.expired(job, now=time.time() + 121)
    assert reg.evictable("A", "m1", prefer_kind="batch").key == "human"
