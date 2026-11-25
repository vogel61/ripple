"""A utility request holds no thread and no slot (issue #193, step 4).

An embeddings call is not a conversation. Before this step it went through
`_identify()`, opened a registry thread under the key `anon:x` and reserved one
of the node's `max_threads` slots for the whole thread TTL — for a prefix cache
nothing will ever ask for again. Twenty calls with a per-request `X-Session-Id`
took a twenty-slot account, and a call the node rejected (500) still left the
ghost row behind, because the thread was registered *before* the forward.

These six tests pin the contract the plan asks for: no identity, no registry
entry, `holds_slot=False` at both placement call sites, `reason=utility`,
`stats.requests_utility`, and no 60 s `note_ephemeral()` tail either — a
utility call counts as load only while it runs.
"""

from __future__ import annotations

from typing import Any

from tests.support import (Harness, chat_body, host_config, make_config, prompt_for,
                           stub_model_loaded)

EMB = "Qwen3-Embedding-4B-4bit-DWQ"


def _fleet(stub_factory: Any, *, slots: int = 20, **state_kwargs: Any):
    """One host carrying a chat model and an embedding model, `slots` threads."""
    stub = stub_factory(models=[stub_model_loaded("m1"),
                                stub_model_loaded(EMB, model_type="embedding")],
                        **state_kwargs)
    cfg = make_config([
        host_config("A", stub.url, node_id=stub.state.node_id, max_threads=slots),
    ])
    return stub, cfg


async def _open_threads(harness: Harness, count: int) -> None:
    """Bind `count` distinct conversations — each one holds a real slot.

    The reason on these is `model_pinned` (one host offers the model, FA-4/AS-8),
    not `new_thread`; the test cares about the slot account, not the label.
    """
    for i in range(count):
        resp = await harness.ok(chat_body("m1", prompt_for(i), turns=2),
                                headers={"X-Session-Id": f"chat-{i}"})
        assert "x-ripple-thread" in resp.headers, resp.text


async def _embed(harness: Harness, headers: dict[str, str] | None = None):
    return await harness.client.post("/v1/embeddings",
                                     json={"model": EMB, "input": "hello"},
                                     headers=headers or {})


async def test_an_embedding_request_leaves_no_thread(stub_factory, ripple_factory):
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)

    before = await harness.healthz()
    assert before["threads"]["registry"] == []
    assert before["hosts"][0]["thread_slots"] == 0

    resp = await _embed(harness)
    assert resp.status_code == 200, resp.text

    after = await harness.healthz()
    assert after["threads"]["registry"] == []
    assert after["hosts"][0]["thread_slots"] == 0
    assert (await harness.stats())["threads"] == []
    assert stub.state.count("embeddings") == 1


async def test_an_embedding_request_is_served_with_every_slot_taken(
        stub_factory, ripple_factory):
    """A full slot account neither refuses the call nor evicts a conversation."""
    stub, cfg = _fleet(stub_factory, slots=20)
    harness = await ripple_factory(cfg)
    await _open_threads(harness, 20)

    assert (await harness.healthz())["hosts"][0]["thread_slots"] == 20

    resp = await _embed(harness)
    assert resp.status_code == 200, resp.text
    # Nothing was evicted to make room, and nothing was opened for it.
    assert (await harness.healthz())["hosts"][0]["thread_slots"] == 20


async def test_the_response_says_why_it_is_utility(stub_factory, ripple_factory):
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)

    resp = await _embed(harness)
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-ripple-reason"] == "utility"
    assert "x-ripple-thread" not in resp.headers
    assert resp.headers["x-ripple-host"] == "A"


async def test_a_failed_embedding_request_leaves_no_thread(stub_factory, ripple_factory):
    """The node rejects the call; the registry must be exactly as empty as before.

    This is the ghost-thread defect: a registered-before-forward thread survived
    a 500, because only the dispatch failed and the registry had already taken
    the slot.
    """
    stub, cfg = _fleet(stub_factory, embeddings_status=500)
    harness = await ripple_factory(cfg)

    resp = await _embed(harness)
    assert resp.status_code >= 400, resp.text

    health = await harness.healthz()
    assert health["threads"]["registry"] == []
    assert health["hosts"][0]["thread_slots"] == 0
    assert (await harness.stats())["threads"] == []
    # The node was asked — the failure is the node's, not a routing refusal.
    assert stub.state.count("embeddings") == 1


async def test_twenty_embedding_calls_do_not_fill_the_slot_account(
        stub_factory, ripple_factory):
    """Distinct session headers must not turn embeddings into twenty threads."""
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)
    before = await harness.stats()

    for i in range(20):
        resp = await _embed(harness, {"X-Session-Id": f"embedding-{i}"})
        assert resp.status_code == 200, resp.text

    health = await harness.healthz()
    stats = await harness.stats()
    assert health["hosts"][0]["thread_slots"] == 0
    assert stats["threads_new"] == before["threads_new"] == 0
    assert stats["requests_utility"] == 20
    assert stats["threads"] == []


async def test_an_embedding_response_records_no_decode_rate(
        stub_factory, ripple_factory):
    """An embedding has no decode phase; nothing may land in `hosts[].metrics`."""
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)

    resp = await _embed(harness)
    assert resp.status_code == 200, resp.text

    health = await harness.healthz()
    assert EMB not in health["hosts"][0]["metrics"]
