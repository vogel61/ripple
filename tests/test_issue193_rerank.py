"""`/v1/rerank` is proxied, and a model's type is published (issue #193, step 7).

oMLX serves a rerank endpoint from its own `RerankerEngine` with a Cohere/Jina
body (`query`, `documents`, `top_n`, `return_documents`). RIPPLE never routed it:
`v1/rerank` was missing from `PROXY_PATHS`, so the registration loop built no
route for it and the call answered 404 at RIPPLE — a utility model that could not
be used at all, however well the fleet knew it.

Two things are pinned here. First, the path is routed *and* classified: a rerank
request is utility because `is_utility_path()` says so, so it holds no thread and
no slot and carries `reason=utility` — the step 4 contract, reached by path this
time instead of by model type. Second, the model list says what a model *is*:
`type` on `/v1/models` rows, on `/healthz → models[]` and on every
`models[].hosts[]` row, straight from the node's own label.

The last test is the guard step 2 promised: `UTILITY_PATHS ⊆ PROXY_PATHS`. A path
that is classified but not routed is a request class that can never arrive, and
that is exactly the state this step found the rerank path in.
"""

from __future__ import annotations

from typing import Any

from ripple.proxy import PROXY_PATHS
from ripple.utility import UTILITY_PATHS
from tests.support import Harness, host_config, make_config, stub_model_loaded

EMB = "Qwen3-Embedding-4B-4bit-DWQ"
RER = "bge-reranker"
CHAT = "m1"

RERANK_BODY = {
    "model": RER,
    "query": "q",
    "documents": ["a", "b"],
    "top_n": 1,
}


def _fleet(stub_factory: Any, **state_kwargs: Any):
    """One host carrying a chat model, an embedding and a reranker."""
    stub = stub_factory(models=[
        stub_model_loaded(CHAT),
        stub_model_loaded(EMB, model_type="embedding"),
        stub_model_loaded(RER, model_type="reranker"),
    ], **state_kwargs)
    cfg = make_config([host_config("A", stub.url, node_id=stub.state.node_id)])
    return stub, cfg


async def _rerank(harness: Harness, body: dict[str, Any] | None = None):
    return await harness.client.post("/v1/rerank", json=body or RERANK_BODY)


async def _model_row(harness: Harness, model: str) -> dict[str, Any]:
    resp = await harness.client.get("/v1/models")
    assert resp.status_code == 200, resp.text
    return next(row for row in resp.json()["data"] if row["id"] == model)


async def test_rerank_is_proxied_and_named(stub_factory, ripple_factory):
    """The route exists, the node answered, and the reason says why."""
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)

    resp = await _rerank(harness)
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-ripple-host"] == "A"
    assert resp.headers["x-ripple-reason"] == "utility"
    # The node's own answer, passed through: RIPPLE does not reshape a rerank.
    assert resp.json()["results"] == [
        {"index": 0, "relevance_score": 0.9, "document": {"text": "a"}}]
    assert stub.state.count("rerank") == 1


async def test_rerank_leaves_no_thread(stub_factory, ripple_factory):
    """A rerank is utility by path: no registry entry, no slot, no 60 s tail."""
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)

    before = await harness.healthz()
    assert before["threads"]["registry"] == []
    assert before["hosts"][0]["thread_slots"] == 0

    resp = await _rerank(harness)
    assert resp.status_code == 200, resp.text

    after = await harness.healthz()
    assert after["threads"]["registry"] == []
    assert after["hosts"][0]["thread_slots"] == 0
    assert (await harness.stats())["threads"] == []


async def test_the_rerank_body_reaches_the_node_unchanged(stub_factory, ripple_factory):
    """`query`, `documents` and `top_n` are the client's — including key order."""
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)
    # `return_documents` and an unknown key ride along: RIPPLE forwards the body,
    # it does not filter it against a schema of its own.
    body = {**RERANK_BODY, "return_documents": True, "x_trace": "keep me"}

    resp = await _rerank(harness, body)
    assert resp.status_code == 200, resp.text

    seen = stub.state.rerank_bodies[0]
    assert seen == body
    assert seen["query"] == "q"
    assert seen["documents"] == ["a", "b"]
    assert seen["top_n"] == 1
    assert list(seen) == list(body)


async def test_the_model_list_names_the_type(stub_factory, ripple_factory):
    """`/v1/models` says what each row is, in the node's word."""
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)

    assert (await _model_row(harness, EMB))["type"] == "embedding"
    assert (await _model_row(harness, RER))["type"] == "reranker"
    # A chat model is labelled too — the key is on every row, not only on the
    # utility ones, so a client never has to infer it from its absence.
    assert (await _model_row(harness, CHAT))["type"] == "llm"


async def test_healthz_names_the_type_per_host(stub_factory, ripple_factory):
    """The page reads the same word, per host and for the model row."""
    stub, cfg = _fleet(stub_factory)
    harness = await ripple_factory(cfg)

    health = await harness.healthz()
    models = {row["id"]: row for row in health["models"]}
    assert models[EMB]["type"] == "embedding"
    assert models[RER]["type"] == "reranker"
    assert models[RER]["hosts"][0]["type"] == "reranker"
    assert models[EMB]["hosts"][0]["type"] == "embedding"
    # Same word on both payloads: one fact, two readers.
    assert models[RER]["type"] == (await _model_row(harness, RER))["type"]


def test_every_utility_path_is_a_proxy_path():
    """A utility path that is not routed is a class that can never arrive."""
    assert UTILITY_PATHS <= set(PROXY_PATHS)
