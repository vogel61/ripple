"""The registry as the optimizer's source of candidates (issue #80).

The optimizer needs two facts from here and neither existed before:

- **the price of a move**, which is one prefill of the thread's next prompt —
  so a thread carries the size of its *last* request (`last_prompt_tokens`),
  overwritten and never summed: a 500-turn conversation after a compression is
  cheap, a single Hermes turn of 83 192 tokens is not;
- **which threads may move at all**, ranked cheapest first
  (`idle_threads`) — busy, batch, expired and just-moved threads are out.

A wrong order here is not cosmetic: the list is what the operator sees in block
4, and a list that reorders itself between two scans looks like the fleet moved
when nothing did.
"""

from __future__ import annotations

import time
from typing import Any

from ripple.config import AffinityConfig
from ripple.registry import (ThreadRegistry, estimate_tokens, prompt_size_text)

from tests.support import host_config, make_config, prompt_for, stub_model_loaded


def _registry(**aff: Any) -> ThreadRegistry:
    return ThreadRegistry(AffinityConfig(**aff))


def _thread(reg: ThreadRegistry, key: str, *, host: str = "alpha",
            model: str = "m1", turns: int = 1, prompt_tokens: int = 0,
            kind: str = "interactive", active: int = 0,
            last_rebind: float = 0.0, idle: float = 0.0,
            created: float | None = None, ttl_seconds: float | None = None):
    t = reg.register(key=key, model=model, host=host, text=f"text of {key}",
                     holds_slot=True, source="header", kind=kind,
                     ttl_seconds=ttl_seconds, prompt_tokens=prompt_tokens)
    t.turns = turns
    t.active = active
    t.last_rebind = last_rebind
    t.last_seen = time.time() - idle
    if created is not None:
        t.created = created
    return t


# ---------------------------------------------------------------------------
# 1. The price of a move: the size of the last request, not the turn count
# ---------------------------------------------------------------------------
def test_the_size_of_the_last_request_replaces_the_previous_one():
    """Two requests, the second smaller: the thread gets *cheaper*, not bigger.

    This is the whole point of the field. Summed (or averaged) the thread would
    look expensive after a compression and never be moved again, although its
    next prefill is short.
    """
    reg = _registry()
    t = _thread(reg, "sid:a", prompt_tokens=83_192)
    assert t.last_prompt_tokens == 83_192

    reg.touch(t, prompt_tokens=1_200)
    assert t.last_prompt_tokens == 1_200
    assert t.turns == 2          # the turn count still counts turns


def test_a_known_size_is_not_reset_when_the_caller_does_not_know_it():
    """`touch()` without a size leaves the stored price alone.

    "Unknown" must never overwrite "known": a caller that cannot measure the
    prompt would otherwise turn a priced thread into an unpriceable one.
    """
    reg = _registry()
    t = _thread(reg, "sid:a", prompt_tokens=4_000)
    reg.touch(t)
    assert t.last_prompt_tokens == 4_000


def test_an_unknown_size_stays_zero():
    """0 means unknown — not "cheap", not "small", not a guess."""
    reg = _registry()
    t = _thread(reg, "sid:a")
    assert t.last_prompt_tokens == 0
    reg.touch(t, prompt_tokens=0)
    assert t.last_prompt_tokens == 0


def test_registering_with_a_size_keeps_it():
    reg = _registry()
    t = _thread(reg, "sid:a", prompt_tokens=12_345)
    assert reg.threads["sid:a"].last_prompt_tokens == 12_345
    assert t is reg.threads["sid:a"]


# ---------------------------------------------------------------------------
# 2. Which threads may move
# ---------------------------------------------------------------------------
def test_a_thread_with_a_request_in_flight_is_not_a_candidate():
    """Moving it would throw the running generation away."""
    reg = _registry()
    busy = _thread(reg, "sid:busy", active=1, prompt_tokens=100)
    idle = _thread(reg, "sid:idle", prompt_tokens=200)
    assert reg.idle_threads("alpha") == [idle]
    assert busy not in reg.idle_threads("alpha")


def test_a_batch_thread_is_not_a_candidate():
    """Nobody waits for a batch prefix cache — `batch_no_rebind` everywhere."""
    reg = _registry()
    _thread(reg, "sid:batch", kind="batch", prompt_tokens=100)
    human = _thread(reg, "sid:human", prompt_tokens=200)
    assert reg.idle_threads("alpha") == [human]


def test_an_expired_thread_is_not_a_candidate():
    """Its slot is gone already; there is nothing left to move."""
    reg = _registry(ttl_seconds=60)
    _thread(reg, "sid:dead", prompt_tokens=100, idle=600)
    alive = _thread(reg, "sid:alive", prompt_tokens=200, idle=1)
    assert reg.idle_threads("alpha") == [alive]


def test_a_thread_inside_the_cooldown_is_not_a_candidate():
    """It just paid its prefill; moving it again is the flap the guard forbids."""
    reg = _registry()
    now = time.time()
    _thread(reg, "sid:fresh", prompt_tokens=100, last_rebind=now - 30)
    cool = _thread(reg, "sid:cool", prompt_tokens=200, last_rebind=now - 900)
    got = reg.idle_threads("alpha", now=now, cooldown_seconds=600)
    assert got == [cool]


def test_a_thread_never_moved_has_no_cooldown():
    """`last_rebind == 0` means "never moved", not "moved in 1970"."""
    reg = _registry()
    t = _thread(reg, "sid:new", prompt_tokens=100)
    assert reg.idle_threads("alpha", cooldown_seconds=600) == [t]


def test_only_threads_of_the_named_host():
    reg = _registry()
    _thread(reg, "sid:a", host="alpha", prompt_tokens=100)
    other = _thread(reg, "sid:b", host="beta", prompt_tokens=50)
    assert reg.idle_threads("beta") == [other]
    assert [t.key for t in reg.idle_threads("alpha")] == ["sid:a"]


def test_a_thread_without_a_slot_is_not_a_candidate():
    """An ephemeral thread holds nothing, so there is nothing to give up."""
    reg = _registry()
    reg.register(key="sid:e", model="m1", host="alpha", text="x",
                 holds_slot=False, source="header", prompt_tokens=100)
    assert reg.idle_threads("alpha") == []


# ---------------------------------------------------------------------------
# 3. The order: cheapest first, stable on a tie
# ---------------------------------------------------------------------------
def test_a_short_prompt_with_few_turns_comes_before_a_long_one():
    """The 3-turn thread moves, not the 500-turn one (acceptance of #80).

    Turns are the wrong price: the 500-turn thread may have been compressed and
    be cheaper, but here its prompt really is the bigger one.
    """
    reg = _registry()
    _thread(reg, "sid:long", turns=500, prompt_tokens=60_000)
    _thread(reg, "sid:short", turns=3, prompt_tokens=900)
    assert [t.key for t in reg.idle_threads("alpha")] == ["sid:short", "sid:long"]


def test_turns_do_not_decide_the_price():
    """Many turns, small prompt → still the cheaper of the two."""
    reg = _registry()
    _thread(reg, "sid:many-turns-small", turns=200, prompt_tokens=1_000)
    _thread(reg, "sid:few-turns-big", turns=2, prompt_tokens=40_000)
    assert [t.key for t in reg.idle_threads("alpha")] == [
        "sid:many-turns-small", "sid:few-turns-big"]


def test_equal_size_breaks_the_tie_on_turns_then_created():
    """Two threads at the same price: the list must not wobble."""
    reg = _registry()
    base = time.time() - 1_000
    _thread(reg, "sid:old-many", turns=9, prompt_tokens=5_000, created=base)
    _thread(reg, "sid:new-few", turns=2, prompt_tokens=5_000, created=base + 10)
    _thread(reg, "sid:new-many", turns=9, prompt_tokens=5_000, created=base + 20)
    got = [t.key for t in reg.idle_threads("alpha")]
    assert got == ["sid:new-few", "sid:old-many", "sid:new-many"]

    # A second scan of an unchanged fleet gives the same list — the point of
    # the tie-break (a list that reorders looks like a move).
    assert [t.key for t in reg.idle_threads("alpha")] == got


def test_an_unknown_size_sorts_last_not_first():
    """A price of 0 must not look free.

    The thread stays visible (so the decision layer can say why it did not
    move), but a priced candidate is always preferred over an unpriceable one.
    """
    reg = _registry()
    _thread(reg, "sid:unknown", prompt_tokens=0)
    _thread(reg, "sid:priced", prompt_tokens=50_000)
    assert [t.key for t in reg.idle_threads("alpha")] == ["sid:priced", "sid:unknown"]


# ---------------------------------------------------------------------------
# 4. Where the size comes from: the number the request log carries
# ---------------------------------------------------------------------------
def _two_hosts(stub_factory) -> list:
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    return [host_config("alpha", a.url, node_id=a.state.node_id, weight=3.0),
            host_config("beta", b.url, node_id=b.state.node_id, weight=2.0)]


def _turn(system: str | None) -> dict:
    messages: list[dict] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages += [{"role": "user", "content": prompt_for(7)},
                 {"role": "assistant", "content": "answer"},
                 {"role": "user", "content": "follow-up"}]
    return {"model": "m1", "messages": messages}


async def test_a_request_through_the_proxy_stores_the_size_the_log_carries(
        stub_factory, ripple_factory):
    """The proxy hands over exactly `estimate_tokens(prompt_size_text(body))`.

    Not a second estimate and not the user text: the size in the thread is the
    same number that goes into the request log, so the price of a move and the
    logged prompt size can never disagree. The second turn is the smaller one
    (the system prompt is gone), and the thread must get *cheaper*.
    """
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    headers = {"X-Session-Id": "agent-turn"}

    big = _turn("S" * 40_000)
    await harness.ok(big, headers=headers)
    thread = harness.ripple.registry.threads["sid:agent-turn"]
    assert thread.last_prompt_tokens == estimate_tokens(prompt_size_text(big))
    assert thread.last_prompt_tokens > 9_000

    small = _turn(None)
    await harness.ok(small, headers=headers)
    assert thread.last_prompt_tokens == estimate_tokens(prompt_size_text(small))
    assert thread.last_prompt_tokens < 100
    assert thread.turns == 2

