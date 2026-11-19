"""Prefill bias in the deficit rule (FA-2, §0.2; docs/as6-measurement.md).

Decode is 1,3 : 1 between the two Macs and already in the 3 : 2 weights; cold
prefill is 3,6–4,0 : 1 and was invisible to the placement rule. A long context
placed on the weak node therefore cost minutes of prefill nobody had accounted
for. The bias charges it in slots:

    t_h    = prompt_tokens / pp_tps_h
    bias_h = −min(max_slots, max(0, t_h − t_fastest) / slot_seconds)

All rates below are the measured ones from `docs/as6-measurement.md`
(8k: M5 3 734 / M3 1 047 tok/s, 24k and above: 4 154 / 1 048), which is what the
documented numbers are computed from.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ripple.config import (ConfigError, HostConfig, PlacementConfig,
                           PrefillBiasConfig, load_config)
from ripple.placement import (TIER_RESIDENT, Candidate, _prefill_bias, choose)
from ripple.registry import estimate_tokens, prompt_size_text

from tests.support import (chat_body, host_config, make_config, prompt_for,
                           stub_model_loaded)

# (prompt tokens, M5 pp_tps, M3 pp_tps, bias for the M3 against the M5)
BIAS_TABLE = [
    (8_000, 3734.0, 1047.0, -0.09),
    (24_000, 4154.0, 1048.0, -0.29),
    (60_000, 4154.0, 1048.0, -0.71),
    (100_000, 4154.0, 1048.0, -1.19),
    (200_000, 4154.0, 1048.0, -2.38),
]


@dataclass
class _FakeHost:
    """`_prefill_bias` only reads the host config — no runtime needed."""

    cfg: HostConfig


def _cand(name: str, pp_tps: float | None, weight: float = 1.0) -> Candidate:
    return Candidate(host=_FakeHost(host_config(name, "http://x:1", weight=weight,
                                                pp_tps=pp_tps)),
                     tier=TIER_RESIDENT)


def _placement(*, enabled: bool = True, slot_seconds: float = 60.0,
               max_slots: float = 3.0, min_prompt_tokens: int = 4096
               ) -> PlacementConfig:
    return PlacementConfig(prefill_bias=PrefillBiasConfig(
        enabled=enabled, slot_seconds=slot_seconds, max_slots=max_slots,
        min_prompt_tokens=min_prompt_tokens))


def _two_hosts(stub_factory, *, pp_a: float | None = 4154.0,
               pp_b: float | None = 1048.0) -> list[HostConfig]:
    """M5-alpha : M3-beta = 3 : 2 — the live fleet."""
    a = stub_factory(models=[stub_model_loaded("m1")])
    b = stub_factory(models=[stub_model_loaded("m1")])
    return [
        host_config("M5-alpha", a.url, node_id=a.state.node_id, weight=3.0,
                    pp_tps=pp_a),
        host_config("M3-beta", b.url, node_id=b.state.node_id, weight=2.0,
                    pp_tps=pp_b),
    ]


def _seed(harness, host: str, count: int, model: str = "m1") -> None:
    """Occupied slots without a request: exactly the state a deficit reads."""
    for i in range(count):
        harness.ripple.registry.register(key=f"seed-{host}-{i}", model=model,
                                         host=host, text=f"seed {host} {i}",
                                         holds_slot=True, source="header")


@pytest.mark.parametrize("prompt_tokens, pp_fast, pp_slow, expected", BIAS_TABLE)
def test_bias_table_matches_the_measurement(prompt_tokens, pp_fast, pp_slow,
                                            expected) -> None:
    """The documented numbers, computed from the measured cold-prefill rates."""
    fast = _cand("M5-alpha", pp_fast, weight=3.0)
    slow = _cand("M3-beta", pp_slow, weight=2.0)
    cfg = make_config([])

    assert _prefill_bias(fast, [fast, slow], cfg, prompt_tokens) == 0.0
    assert round(_prefill_bias(slow, [fast, slow], cfg, prompt_tokens), 2) == expected


def test_1m_prompt_is_capped_at_max_slots() -> None:
    fast = _cand("M5-alpha", 4154.0)
    slow = _cand("M3-beta", 1048.0)
    cfg = make_config([])

    # 11,9 slots of extra prefill, capped at 3 — a huge context must not turn
    # the deficit into a single-host rule.
    assert _prefill_bias(slow, [fast, slow], cfg, 1_000_000) == -3.0


async def test_8k_on_a_balanced_fleet_changes_nothing(stub_factory,
                                                      ripple_factory):
    """(a) 8k is below the point where -0,09 slots can outvote a deficit step."""
    on = await ripple_factory(make_config(_two_hosts(stub_factory)))
    off = await ripple_factory(make_config(
        _two_hosts(stub_factory), placement=_placement(enabled=False)))

    # 32 000 characters ≈ 8 000 tokens: above min_prompt_tokens, bias live.
    def body(i: int) -> dict:
        return chat_body("m1", prompt_for(i) + " " + "w" * 32_000, turns=2)

    seq_on = [(await on.ok(body(i))).headers["x-ripple-host"] for i in range(5)]
    seq_off = [(await off.ok(body(i))).headers["x-ripple-host"] for i in range(5)]

    assert seq_on == seq_off == ["M5-alpha", "M3-beta", "M5-alpha", "M3-beta",
                                 "M5-alpha"]
    assert await on.slots() == {"M5-alpha": 3, "M3-beta": 2}


async def test_200k_prompt_moves_the_thread_to_the_fast_host(
        stub_factory, ripple_factory):
    """(b) The case the bias exists for: the M3 wins on slots, loses on prefill."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    _seed(harness, "M5-alpha", 3)       # 3 : 0 — the deficit now wants the M3

    # Old behaviour: the deficit alone sends the 200k context to the M3.
    off = choose(harness.ripple.fleet, harness.ripple.cfg,
                 harness.ripple.registry, "m1", prompt_tokens=200_000)
    disabled = make_config(harness.ripple.cfg.hosts,
                           placement=_placement(enabled=False))
    old = choose(harness.ripple.fleet, disabled, harness.ripple.registry, "m1",
                 prompt_tokens=200_000)

    assert off.host is not None and off.host.cfg.name == "M5-alpha"
    assert off.bias is not None and off.bias["M3-beta"] == -2.38
    assert "prefill bias M3-beta -2.38" in (off.note or "")

    # enabled: false — byte-identical to the rule before the bias existed.
    assert old.host is not None and old.host.cfg.name == "M3-beta"
    assert old.bias is None and old.note is None


async def test_host_without_pp_tps_is_neither_penalised_nor_favoured(
        stub_factory, ripple_factory):
    """(c) Unknown is not fast: one known rate means nothing to compare with."""
    harness = await ripple_factory(make_config(
        _two_hosts(stub_factory, pp_b=None)))
    _seed(harness, "M5-alpha", 3)

    decision = choose(harness.ripple.fleet, harness.ripple.cfg,
                      harness.ripple.registry, "m1", prompt_tokens=200_000)

    assert decision.host is not None and decision.host.cfg.name == "M3-beta"
    assert decision.bias is None
    assert decision.note is None


async def test_prompt_below_min_prompt_tokens_gets_no_bias(stub_factory,
                                                           ripple_factory):
    """(d) 4 000 tokens < 4 096: exactly today's behaviour, down to the note."""
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    _seed(harness, "M5-alpha", 3)

    decision = choose(harness.ripple.fleet, harness.ripple.cfg,
                      harness.ripple.registry, "m1", prompt_tokens=4_000)

    assert decision.host is not None and decision.host.cfg.name == "M3-beta"
    assert decision.bias is None and decision.note is None


async def test_bias_applies_to_the_deficit_strategy_only(stub_factory,
                                                         ripple_factory):
    """`least_threads` does not weigh the deficit, so a slot penalty is noise."""
    harness = await ripple_factory(make_config(
        _two_hosts(stub_factory),
        placement=PlacementConfig(binding_strategy="least_threads")))
    _seed(harness, "M5-alpha", 3)

    decision = choose(harness.ripple.fleet, harness.ripple.cfg,
                      harness.ripple.registry, "m1", prompt_tokens=200_000)

    assert decision.host is not None and decision.host.cfg.name == "M3-beta"
    assert decision.bias is None


async def test_disabled_is_exactly_the_rule_without_a_prompt_size(
        stub_factory, ripple_factory):
    """(f) `enabled: false` must be byte-identical to the rule before the bias.

    Same fleet state, same prompt: a decision with the bias switched off picks
    the host that `prompt_tokens=0` picks, at every size, and carries neither a
    bias nor a note.
    """
    harness = await ripple_factory(make_config(
        _two_hosts(stub_factory), placement=_placement(enabled=False)))
    _seed(harness, "M5-alpha", 3)

    unknown = choose(harness.ripple.fleet, harness.ripple.cfg,
                     harness.ripple.registry, "m1")
    assert unknown.host is not None

    for tokens in (8_000, 60_000, 200_000, 1_000_000):
        decision = choose(harness.ripple.fleet, harness.ripple.cfg,
                          harness.ripple.registry, "m1", prompt_tokens=tokens)
        assert decision.host is not None
        assert decision.host.cfg.name == unknown.host.cfg.name == "M3-beta"
        assert decision.reason == unknown.reason
        assert decision.bias is None and decision.note is None


async def test_a_300kb_system_prompt_reaches_the_bias_of_the_slow_host(
        stub_factory, ripple_factory):
    """Acceptance (issue #14): the size of a real agent turn decides placement.

    A 300 000-character system prompt with a 100-character user message is
    ~75 000 tokens. Cold prefill at the measured rates (M5 4 154, M3 1 048
    tok/s) costs the M3 53,5 s more than the M5 — −0,89 slots at
    `slot_seconds: 60`, "about one slot". Sized from the user text alone the
    same request was 25 tokens: no bias at all, and the M3 took it.
    """
    harness = await ripple_factory(make_config(_two_hosts(stub_factory)))
    _seed(harness, "M5-alpha", 1)       # the deficit alone wants the M3
    body = {"model": "m1", "messages": [
        {"role": "system", "content": "S" * 300_000},
        {"role": "user", "content": "wo steht die anlage in traiskirchen?"},
    ]}

    tokens = estimate_tokens(prompt_size_text(body))
    expected = -min(3.0, (tokens / 1048.0 - tokens / 4154.0) / 60.0)
    assert -1.05 < expected < -0.75, expected     # "about −1 slot"

    decision = choose(harness.ripple.fleet, harness.ripple.cfg,
                      harness.ripple.registry, "m1", prompt_tokens=tokens)
    assert decision.host is not None and decision.host.cfg.name == "M5-alpha"
    assert decision.bias is not None
    assert round(decision.bias["M3-beta"], 2) == round(expected, 2)
    assert round(decision.bias["M3-beta"], 1) == -0.9

    # Without the bias the same state sends the same prompt to the M3.
    off = make_config(harness.ripple.cfg.hosts,
                      placement=_placement(enabled=False))
    plain = choose(harness.ripple.fleet, off, harness.ripple.registry, "m1",
                   prompt_tokens=tokens)
    assert plain.host is not None and plain.host.cfg.name == "M3-beta"

    # And through the request path: the sized prompt is what the bias sees.
    resp = await harness.ok(body)
    assert resp.headers["x-ripple-host"] == "M5-alpha"
    assert (await harness.stats())["prefill_bias"] == 1


def test_config_rejects_a_useless_exchange_rate(tmp_path) -> None:
    """`slot_seconds: 0` would make the cap decide every placement — refused."""
    path = tmp_path / "config.yaml"
    path.write_text("hosts:\n  - name: A\n    addresses: [http://127.0.0.1:1]\n"
                    "placement:\n  prefill_bias:\n    slot_seconds: 0\n",
                    encoding="utf-8")
    with pytest.raises(ConfigError, match="slot_seconds must be > 0"):
        load_config(path)

    # Off is fine: the value is then never read.
    path.write_text("hosts:\n  - name: A\n    addresses: [http://127.0.0.1:1]\n"
                    "placement:\n  prefill_bias:\n    enabled: false\n"
                    "    slot_seconds: 0\n", encoding="utf-8")
    assert load_config(path).placement.prefill_bias.enabled is False


async def test_stats_counts_the_decisions_the_bias_decided(
        stub_factory, ripple_factory):
    """The counter says whether the term does anything: /stats, one per decision.

    `slot_seconds: 6` keeps the request small — 20k tokens against the measured
    rates is 14,3 s of extra prefill, i.e. the same 2,38 slots as 200k at 60 s.
    """
    harness = await ripple_factory(make_config(
        _two_hosts(stub_factory),
        placement=_placement(slot_seconds=6.0)))
    _seed(harness, "M5-alpha", 3)

    assert (await harness.stats())["prefill_bias"] == 0

    resp = await harness.ok(chat_body("m1", prompt_for(9) + " " + "w" * 80_000,
                                      turns=2))

    assert resp.headers["x-ripple-host"] == "M5-alpha"
    assert resp.headers["x-ripple-reason"] == "new_thread"
    assert (await harness.stats())["prefill_bias"] == 1
