"""The utility budget, end to end (issue #193, step 10).

The unit tests in `tests/test_placement.py` prove the counters and the two
admission paths. This one proves the seam they hang on: `handle()` decides the
request's class once, from the path or from the model type the node reports, and
that one flag reaches `choose()`, `admit()`, `acquire()` and `release()`. Without
it a budget exists that nothing ever spends.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tests.support import (Harness, chat_body, host_config, make_config, prompt_for,
                           stub_model_loaded)

EMB = "Qwen3-Embedding-4B-4bit-DWQ"


async def _host_until(harness: Harness, want: int) -> dict[str, Any]:
    """The host row, once its utility counter reads `want` — or when time is up."""
    row: dict[str, Any] = {}
    for _ in range(60):
        row = (await harness.healthz())["hosts"][0]
        if row["inflight_utility"] == want:
            return row
        await asyncio.sleep(0.02)
    return row


async def test_the_proxy_admits_an_embedding_model_against_the_utility_budget(
        stub_factory, ripple_factory):
    """A spent utility budget refuses the next embedding and no conversation.

    One host, a utility budget of one, a stub that takes 0.4 s: the second
    concurrent call to the embedding model is refused with the budget's name,
    while a chat request on the same host is admitted — its counter is empty.
    """
    stub = stub_factory(models=[stub_model_loaded(EMB, model_type="embedding"),
                                stub_model_loaded("m1")], delay=0.4)
    harness = await ripple_factory(make_config([
        host_config("A", stub.url, node_id=stub.state.node_id,
                    max_inflight_utility=1),
    ]))

    first = asyncio.create_task(harness.chat(chat_body(EMB, prompt_for(1))))
    # Let the first request reach the node and take its place in the budget.
    row = await _host_until(harness, 1)
    assert row["inflight_utility"] == 1, row

    second = await harness.chat(chat_body(EMB, prompt_for(2)))
    assert second.status_code == 503, second.text
    body = second.json()["error"]
    assert "max_inflight_utility (1/1)" in body["message"], body

    # The chat budget is untouched: the same host still takes a conversation.
    chat = await harness.chat(chat_body("m1", prompt_for(3)))
    assert chat.status_code == 200, chat.text

    assert (await first).status_code == 200
    # And the place is given back — a released budget must not stay spent.
    row = await _host_until(harness, 0)
    assert row["inflight_utility"] == 0, row
    assert row["inflight"] == 0, row
