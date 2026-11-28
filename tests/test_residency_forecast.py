"""The eviction forecast counts engines, not rows (GitHub #48).

oMLX lists every warm profile of an engine as its own loaded model carrying the
base engine's **full** estimated size (`X:agent`, `X:think-1m`, …) and names the
engine it belongs to in `source_model_id` — the field #195 added so the measured
sum could stop counting one engine five times. The forecast never got that
treatment, so on Scott Lowry's two-node fleet a pinned 113 GiB whale with four
warm profiles forecast 565 GiB against a 242 GiB ceiling while `/healthz`
honestly reported 81.8 GiB used. A 0.665 GiB embedding request then "would evict
the pinned engine", every host answered `conflict`, and nothing could cold-load
anywhere until the node was reloaded by hand:

```
503 ripple_error: no host can take 'Qwen3-Embedding-0.6B-oQ8e' without evicting
a running thread — m5u: conflict (would evict pinned Qwen3.8-Flash-Next-oQ4e-mtp)
```

These four tests pin the arithmetic: one engine counted once, a profile of a
warm engine free, a genuinely tight ceiling still a conflict, and an eviction
that frees a whole group at a time.
"""

from __future__ import annotations

from ripple.backends import ModelInfo
from ripple.config import AffinityConfig, HostConfig
from ripple.hosts import HostRuntime
from ripple.placement import (TIER_COLD_LOAD, TIER_CONFLICT, TIER_EVICT_IDLE,
                              residency_tier)
from ripple.registry import ThreadRegistry

GIB = 1024 ** 3
WHALE = "Qwen3.8-Flash-Next-oQ4e-mtp"
EMBEDDING = "Qwen3-Embedding-0.6B-oQ8e"
OTHER = "Qwen3.6-35B-A3B-oQ4e-mtp"

# The numbers from the report: 241.7 GiB pool, 81.8 GiB used, a 113.05 GiB whale
# with up to four warm profiles, and a 0.665 GiB embedding model.
CEILING = int(241.7 * GIB)
WHALE_SIZE = int(113.05 * GIB)


class _Capabilities:
    """Only the capability set matters here: the forecast asks the host whether
    it has residency signals, nothing else."""

    capabilities = frozenset({"residency", "models", "slots"})


def _host(*models: ModelInfo, hard_threshold: float = 0.95) -> HostRuntime:
    cfg = HostConfig(name="m5u", addresses=["http://m5u.test:8000"], weight=1.0)
    cfg.hard_threshold = hard_threshold
    h = HostRuntime(cfg=cfg, client=None)      # no HTTP on the decision path
    h.healthy = True
    h.status = "healthy"
    h.identity_ok = True
    h.ceiling_bytes = CEILING
    h.guard_enabled = True
    h.used_bytes = int(81.8 * GIB)
    h.backend = _Capabilities()  # type: ignore[assignment]
    for m in models:
        h.models[m.id] = m
    return h


def _engine(model_id: str, *, size: int = WHALE_SIZE, loaded: bool = True,
            pinned: bool = False, last_access: float = 1_000.0,
            source: str = "") -> ModelInfo:
    return ModelInfo(id=model_id, available=True, loaded=loaded,
                     estimated_size=size, actual_size=size, pinned=pinned,
                     last_access=last_access, source_model_id=source)


def _warm_whale(*, pinned: bool = True, profiles: int = 4,
                last_access: float = 1_000.0) -> list[ModelInfo]:
    """One engine plus its warm profile rows, each carrying the engine's size."""
    rows = [_engine(WHALE, pinned=pinned, last_access=last_access)]
    for i, profile in enumerate(("agent", "think-medium", "think-xhigh", "think-1m")[:profiles]):
        rows.append(_engine(f"{WHALE}:{profile}", pinned=pinned,
                            last_access=last_access + i, source=WHALE))
    return rows


def _tier(h: HostRuntime, model: str):
    return residency_tier(h, model, None, ThreadRegistry(AffinityConfig()))


def test_a_pinned_whale_with_warm_profiles_does_not_block_a_cold_load():
    """The reported case: five rows of one 113 GiB engine, ~160 GiB honestly free,
    and a 0.665 GiB request that must be a plain cold load."""
    h = _host(*_warm_whale(), _engine(EMBEDDING, size=int(0.665 * GIB), loaded=False))

    cand = _tier(h, EMBEDDING)

    assert cand.tier == TIER_COLD_LOAD, cand.note
    assert cand.evicts == []


def test_a_new_profile_of_a_warm_engine_adds_no_memory():
    """A fifth profile is the same resident engine wearing another hat: the bytes
    are already there, so it is neither a conflict nor an eviction."""
    h = _host(*_warm_whale(),
              _engine(f"{WHALE}:think-2m", loaded=False, source=WHALE))

    cand = _tier(h, f"{WHALE}:think-2m")

    assert cand.tier == TIER_COLD_LOAD, cand.note
    assert cand.evicts == []


def test_a_genuinely_tight_ceiling_still_names_the_pinned_engine():
    """Grouping must not turn the guard off: whale (113 GiB, pinned) plus a
    150 GiB request is 263 GiB against a 229.6 GiB target, so the only way in is
    evicting the pinned engine — and that is still a conflict."""
    h = _host(*_warm_whale(),
              _engine(OTHER, size=int(150 * GIB), loaded=False))

    cand = _tier(h, OTHER)

    assert cand.tier == TIER_CONFLICT
    assert WHALE in (cand.note or "") and "pinned" in (cand.note or "")
    # The whole group is named, not just the base row: an engine unloads with
    # every profile it serves.
    assert WHALE in cand.evicts
    assert f"{WHALE}:agent" in cand.evicts


def test_an_eviction_never_frees_half_an_engine():
    """A group unloads as a unit, so the forecast may not claim the bytes of
    "the base row plus one profile" — those are the same bytes. Row arithmetic
    freed another 113 GiB per dropped row, so it could stop in the middle of the
    whale (and take unrelated engines with it) while believing the ceiling was
    satisfied."""
    # Two other engines interleaved between the whale's rows by access time, and
    # the whale's newest profile at the far end: the LRU walk visits half of the
    # whale, then a stranger, then the other half.
    rows = [
        _engine(WHALE, pinned=False, last_access=500.0),
        _engine(OTHER, last_access=600.0),
        _engine(f"{WHALE}:agent", last_access=700.0, source=WHALE),
        _engine("another-engine", last_access=800.0),
        _engine(f"{WHALE}:think-medium", last_access=900.0, source=WHALE),
        _engine(f"{WHALE}:think-xhigh", last_access=1_000.0, source=WHALE),
    ]
    h = _host(*rows, _engine("third-model", size=int(50 * GIB), loaded=False))

    cand = _tier(h, "third-model")

    assert cand.tier == TIER_EVICT_IDLE, cand.note
    evicted_groups = {h.models[i].source_model_id or i for i in cand.evicts}
    for gid in evicted_groups:
        members = {m.id for m in h.models.values()
                   if (m.source_model_id or m.id) == gid and m.loaded}
        assert members <= set(cand.evicts), f"half of {gid} was left standing"
    # Every evicted id is a loaded row: nothing is named twice, nothing invented.
    assert len(cand.evicts) == len(set(cand.evicts))
    assert all(h.models[i].loaded for i in cand.evicts)
