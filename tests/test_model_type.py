"""The node's `model_type` survives the probe, not just the model list (issue #193).

`/v1/models` names what each id is — `llm`, `embedding`, `vlm` — and that word is
what later steps route a utility model by. The field used to be read there only
to derive `vision` and then dropped, and even a parsed value would not have
survived: `_read_models()` replaces `host.models` with the `/v1/models/status`
answer, and the status endpoint does not carry `model_type`.

So the type lives in two places with a fixed precedence: the fresh `ModelInfo`
wins, and the map filled from the list the capability probe already reads is the
fallback. These four tests pin the parse, the survival, the honest empty value
and the cost — one list request per probe, not two.
"""

from __future__ import annotations

import httpx

from ripple.backends import ModelInfo, OmlxBackend

from tests.support import StubModel, host_config, make_config, stub_model_loaded

EMBEDDING = "Qwen3-Embedding-4B-4bit-DWQ"


def _one_host(stub) -> object:
    return make_config([host_config("A", stub.url, node_id=stub.state.node_id)])


async def test_the_model_list_carries_the_type(stub_factory):
    """`/v1/models` says `embedding`, so `ModelInfo` carries `embedding`."""
    stub = stub_factory(models=[StubModel(id=EMBEDDING, model_type="embedding")])
    async with httpx.AsyncClient() as client:
        listed = await OmlxBackend(stub.url, client=client).models()

    assert [m.id for m in listed] == [EMBEDDING]
    assert listed[0].model_type == "embedding"


async def test_the_type_survives_a_residency_read(stub_factory, ripple_factory):
    """The status answer replaced `host.models`, and the type is still there.

    The stub's status route deliberately omits `model_type`, which is the shape
    the live node has: after the probe, `host.models` holds a `ModelInfo` whose
    `model_type` is empty. The answer can only come from the list map — and the
    fresh `ModelInfo` must still win when it does say something, or a version
    that starts reporting the field would be ignored in favour of a stale map.
    """
    a = stub_factory(models=[stub_model_loaded(EMBEDDING, model_type="embedding")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")

    assert host.models[EMBEDDING].model_type == ""      # residency has no field
    assert host.model_type(EMBEDDING) == "embedding"    # the list map answers

    # Precedence, pinned: the same id, two different answers — the fresh
    # `ModelInfo` is the node's current word and must win over the map.
    host.models[EMBEDDING].model_type = "vlm"
    assert host.model_type(EMBEDDING) == "vlm"


async def test_an_absent_type_is_empty_and_not_llm(stub_factory, ripple_factory):
    """A node that does not say leaves `""` — never a guess at `llm`."""
    # The stub's routes always emit the key; an empty value is the shape of a
    # node that has nothing to say about the type. RIPPLE must keep that
    # emptiness: defaulting to `llm` would silently classify a utility model as
    # an ordinary one.
    a = stub_factory(models=[StubModel(id="plain", model_type="")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")

    assert host.model_type("plain") == ""
    assert host.model_type("never-listed") == ""
    assert ModelInfo(id="plain").model_type == ""


async def test_the_probe_does_not_ask_for_the_list_twice(stub_factory, ripple_factory):
    """One refresh reads `/v1/models` once — from the capability probe.

    The map is filled from the list `probe_capabilities()` already fetches, so
    the count must grow by exactly one per refresh. A second request would show
    up here as two, which is the cost this design exists to avoid.
    """
    a = stub_factory(models=[stub_model_loaded("m1")])
    harness = await ripple_factory(_one_host(a))
    host = harness.host("A")

    before = a.state.count("models")
    for _ in range(2):
        # The transition path is where the capability probe runs; a steady tick
        # does not read the list at all.
        host.relearn_due = True
        await host.refresh()

    assert a.state.count("models") - before == 2
