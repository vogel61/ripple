"""The PP/TG phase of issue #18: what a node is doing *right now*.

Both dots on the page used to be static — "loaded / not loaded" next to a model,
"healthy" next to a host name — so a node in a 26 s prefill looked exactly like
an idle one. RIPPLE knows the phase per request already (the first token ends
the prefill, the proxy measures it); it just never said so.

This file is the contract behind the blinking dots:

- `/healthz` carries `models[].hosts[].phase` (`pp` / `tg` / `None`) and
  `hosts[].phase` as the aggregate — prefill wins over decode, idle is `None`;
- the phase is derived from RIPPLE's **own** in-flight bookkeeping, so reading
  `/healthz` while requests run costs the upstream nothing. That is asserted by
  counting the stub's calls, not by reading the source: an implementation that
  "just probes the node" would pass every other test in this file;
- it moves `pp` → `tg` on the first token and back to `None` when the request
  ends, on the stream path (where the first token is visible) and on the plain
  path (where the whole request is prefill).

The CSS side — 1 Hz, the 500 ms brighter half, the reduced-motion replacement —
is in `tests/test_dashboard.py`, because it is a property of the style sheet and
not of the backend.

**Why a second stub factory.** The shared stub in `tests/support.py` serialises
its SSE chunks with `json.dumps`' default separators, i.e. `"content": "tok0 "`.
RIPPLE's first-token detector looks for `"content":"…"` — the compact form real
oMLX emits — so every streaming test in the suite stays in the prefill and the
transition is never exercised. This file needs the transition, so it replaces
exactly that one route with a compact-JSON twin and leaves the rest of the stub
(states, counters, residency) untouched.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ripple.hosts import PHASE_PP, PHASE_TG, HostRuntime

from tests.support import (StubModel, StubState, StubUpstream, chat_body, host_config,
                           host_row, make_config, wait_until)

GB = 1_000_000_000


# -- a stub whose deltas look like oMLX's -----------------------------------
async def _compact_sse(state: StubState, body: dict[str, Any]):
    """SSE chunks with no space after the colon — the shape the proxy parses."""
    for i in range(max(1, state.stream_chunks)):
        if i:
            await asyncio.sleep(state.stream_chunk_delay)
        chunk = {"id": "chatcmpl-stub", "object": "chat.completion.chunk",
                 "model": body.get("model"),
                 "choices": [{"index": 0, "delta": {"content": f"tok{i} "},
                              "finish_reason": None}]}
        yield b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"
    final = {"id": "chatcmpl-stub", "object": "chat.completion.chunk",
             "model": body.get("model"),
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    yield b"data: " + json.dumps(final, separators=(",", ":")).encode() + b"\n\n"
    opts = body.get("stream_options") or {}
    if isinstance(opts, dict) and opts.get("include_usage"):
        usage = {"id": "chatcmpl-stub", "object": "chat.completion.chunk",
                 "model": body.get("model"), "choices": [],
                 "usage": {"prompt_tokens": 12, "completion_tokens": 3,
                           "total_tokens": 15}}
        yield b"data: " + json.dumps(usage, separators=(",", ":")).encode() + b"\n\n"
    yield b"data: [DONE]\n\n"


def _use_compact_sse(stub: StubUpstream) -> None:
    """Replace the stub's chat route before uvicorn starts on it."""
    state = stub.state
    app = stub.app
    app.router.routes = [r for r in app.router.routes
                         if getattr(r, "path", None) != "/v1/chat/completions"]

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        state.bump("chat")
        body = await request.json()
        state.chat_bodies.append(body)
        state.auth_headers.append(request.headers.get("authorization", ""))
        if state.delay:
            await asyncio.sleep(state.delay)
        if state.chat_status >= 400:
            return JSONResponse(
                {"error": {"message": f"stub failure {state.chat_status}"}},
                status_code=state.chat_status)
        if body.get("stream"):
            return StreamingResponse(_compact_sse(state, body),
                                     media_type="text/event-stream")
        return {"id": "chatcmpl-stub", "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": "stub answer"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4,
                          "total_tokens": 16}}


@pytest.fixture
def phase_stub() -> Callable[..., StubUpstream]:
    """Stub upstreams that stream the way oMLX does (see the module docstring)."""
    created: list[StubUpstream] = []

    def make(**state_kwargs: Any) -> StubUpstream:
        stub = StubUpstream(StubState(**state_kwargs))
        _use_compact_sse(stub)
        stub.start()
        created.append(stub)
        return stub

    yield make
    for stub in created:
        stub.stop()


# -- readers ----------------------------------------------------------------
def _cell(health: dict[str, Any], model: str, host: str) -> dict[str, Any]:
    """One (model, host) cell of `/healthz`."""
    return next(x for m in health["models"] if m["id"] == model
                for x in m["hosts"] if x["host"] == host)


async def _wait_phase(harness, model: str, host: str, phase: str | None, *,
                      timeout: float = 10.0) -> dict[str, Any]:
    """Poll `/healthz` until the (host, model) cell shows `phase`.

    Polling `/healthz` is the point: the phase has to be observable from the
    outside through the endpoint the page reads, not only in the object.
    """
    deadline = time.monotonic() + timeout
    seen: Any = "never read"
    while time.monotonic() < deadline:
        health = await harness.healthz()
        seen = _cell(health, model, host)["phase"]
        if seen == phase:
            return health
        await asyncio.sleep(0.01)
    raise AssertionError(f"phase of {model} on {host} stayed {seen!r}, wanted {phase!r}")


def _slow_stream(stub, *, prefill: float, chunk: float, chunks: int) -> None:
    """A stub that spends `prefill` s before the first token, then streams slowly.

    `delay` is awaited before the response headers, so it *is* the prefill as far
    as RIPPLE can see it: nothing has been sent until it is over. The chunk delay
    keeps the generation open long enough to observe it.
    """
    stub.state.delay = prefill
    stub.state.stream_chunk_delay = chunk
    stub.state.stream_chunks = chunks


# -- the transition itself --------------------------------------------------
async def test_the_phase_goes_prefill_generation_idle(phase_stub, ripple_factory):
    """Acceptance 1: orange while prefilling, green at the first token, steady
    again when the request is over — read from `/healthz`, the endpoint the
    dashboard patches from."""
    a = phase_stub(models=[StubModel(id="m1", loaded=True, estimated_size=GB)])
    _slow_stream(a, prefill=0.5, chunk=0.2, chunks=8)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    # Idle: both levels say nothing, so the dots stay the steady ones.
    health = await harness.healthz()
    assert _cell(health, "m1", "A")["phase"] is None
    assert host_row(health, "A")["phase"] is None
    assert host_row(health, "A")["phase_switches"] == 0

    request = asyncio.create_task(harness.chat(chat_body("m1", stream=True)))

    # Between dispatch and the first token the node is prefilling ...
    health = await _wait_phase(harness, "m1", "A", PHASE_PP)
    assert host_row(health, "A")["phase"] == PHASE_PP
    assert not request.done(), "the client must not have an answer yet"

    # ... and from the first token on it is generating.
    health = await _wait_phase(harness, "m1", "A", PHASE_TG)
    assert host_row(health, "A")["phase"] == PHASE_TG

    resp = await request
    assert resp.status_code == 200
    health = await harness.healthz()
    assert _cell(health, "m1", "A")["phase"] is None
    assert host_row(health, "A")["phase"] is None
    assert host.inflight == 0
    # idle→pp, pp→tg, tg→idle — one switch per real transition, no polling noise
    assert host_row(health, "A")["phase_switches"] == 3


async def test_a_tick_with_active_requests_adds_no_upstream_request(
        phase_stub, ripple_factory):
    """Acceptance 5: the phase comes from RIPPLE's own bookkeeping.

    Five `/healthz` reads while a generation runs must leave the node's call
    counters exactly where they were. A phase that were probed would show up
    here as `/health` or `/v1/models/status` traffic — and on a real fleet as a
    probe landing inside a 26 s prefill, which is the incident this state exists
    for.
    """
    a = phase_stub(models=[StubModel(id="m1", loaded=True, estimated_size=GB)])
    _slow_stream(a, prefill=0.2, chunk=0.3, chunks=20)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    request = asyncio.create_task(harness.chat(chat_body("m1", stream=True)))
    try:
        await _wait_phase(harness, "m1", "A", PHASE_TG)
        before = dict(a.state.calls)
        for _ in range(5):
            health = await harness.healthz()
            assert _cell(health, "m1", "A")["phase"] == PHASE_TG
        assert dict(a.state.calls) == before
    finally:
        await request


async def test_two_models_on_one_host_keep_their_own_phase(phase_stub, ripple_factory):
    """Acceptance 2: one model generating while another prefills.

    Each cell shows its own phase, and the node's aggregate is prefill — the
    phase that produces no tokens at all, which is what an operator watching the
    host dot cares about.
    """
    a = phase_stub(models=[StubModel(id="m1", loaded=True, estimated_size=GB),
                           StubModel(id="m2", loaded=True, estimated_size=GB)])
    _slow_stream(a, prefill=1.0, chunk=0.3, chunks=12)
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    first = asyncio.create_task(harness.chat(chat_body("m1", stream=True)))
    await _wait_phase(harness, "m1", "A", PHASE_TG)

    second = asyncio.create_task(harness.chat(chat_body("m2", stream=True)))
    health = await _wait_phase(harness, "m2", "A", PHASE_PP)

    assert _cell(health, "m1", "A")["phase"] == PHASE_TG      # streaming
    assert _cell(health, "m2", "A")["phase"] == PHASE_PP      # waiting
    assert host_row(health, "A")["phase"] == PHASE_PP         # prefill wins

    assert (await first).status_code == 200
    assert (await second).status_code == 200
    health = await harness.healthz()
    assert host_row(health, "A")["phase"] is None
    assert [c["phase"] for m in health["models"] for c in m["hosts"]] == [None, None]


async def test_a_non_streamed_request_is_prefill_for_its_whole_life(
        phase_stub, ripple_factory):
    """A request that never streams has no first token to observe, so it stays
    `pp` until it returns — and then nothing is left over."""
    a = phase_stub(models=[StubModel(id="m1", loaded=True, estimated_size=GB)])
    a.state.delay = 0.4
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    request = asyncio.create_task(harness.chat(chat_body("m1")))
    health = await _wait_phase(harness, "m1", "A", PHASE_PP)
    assert host_row(health, "A")["phase"] == PHASE_PP

    resp = await request
    assert resp.status_code == 200
    health = await harness.healthz()
    assert _cell(health, "m1", "A")["phase"] is None


async def test_a_failed_dispatch_leaves_no_phase_behind(phase_stub, ripple_factory):
    """A 5xx from the node is a request that never reached a first token. The
    phase has to be given back on that path too, or the dot would blink for ever
    on a node that is doing nothing."""
    a = phase_stub(models=[StubModel(id="m1", loaded=True, estimated_size=GB)])
    a.state.chat_status = 500
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    resp = await harness.chat(chat_body("m1", stream=True))
    assert resp.status_code == 502
    health = await harness.healthz()
    assert _cell(health, "m1", "A")["phase"] is None
    assert host_row(health, "A")["phase"] is None
    assert harness.host("A").phases == {}


@pytest.mark.parametrize("stream", [True, False])
async def test_the_phase_never_outlives_the_slot(phase_stub, ripple_factory, stream):
    """Whatever the path, the phase and `inflight` end together — the invariant
    that keeps a dot from blinking on an idle node."""
    a = phase_stub(models=[StubModel(id="m1", loaded=True, estimated_size=GB)])
    a.state.stream_chunk_delay = 0.05
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.host("A")

    resp = await harness.chat(chat_body("m1", stream=stream))
    assert resp.status_code == 200
    assert host.inflight == 0
    assert host.phases == {}


async def test_a_stream_the_client_drops_gives_the_phase_back(phase_stub,
                                                              live_proxy_factory):
    """Closing the tab is a normal end, not a leak: `_stream`'s finally runs on
    the cancelled generator too, so the dot goes back to steady.

    A real socket is needed here — `httpx.ASGITransport` buffers the whole body
    before it hands back a response, so an early close cannot interrupt anything.
    """
    a = phase_stub(models=[StubModel(id="m1", loaded=True, estimated_size=GB)])
    _slow_stream(a, prefill=0.1, chunk=0.2, chunks=60)
    url = live_proxy_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    async with httpx.AsyncClient(base_url=url, timeout=30.0) as client:
        async with client.stream("POST", "/v1/chat/completions",
                                 json=chat_body("m1", stream=True)) as resp:
            assert resp.status_code == 200
            health = (await client.get("/healthz")).json()
            assert _cell(health, "m1", "A")["phase"] == PHASE_TG
            # leaving the block closes the connection mid-generation
        deadline = time.monotonic() + 10.0
        phase: Any = "never read"
        while time.monotonic() < deadline:
            phase = _cell((await client.get("/healthz")).json(), "m1", "A")["phase"]
            if phase is None:
                break
            await asyncio.sleep(0.05)
        assert phase is None, "the phase outlived the connection that carried it"


# -- the bookkeeping itself -------------------------------------------------
def test_the_phase_is_a_count_not_a_flag():
    """Two conversations on one model: the model keeps its phase until the last
    of them is over. A boolean would drop the dot to steady while a request is
    still streaming — the defect the old static dot had, in a new place."""
    host = HostRuntime(cfg=host_config("A", "http://127.0.0.1:1"),
                       client=httpx.AsyncClient())

    host.begin_phase("m1")
    assert host.model_phase("m1") == PHASE_PP
    host.begin_phase("m1")                    # a second conversation
    host.note_first_token("m1")               # ... the first one starts decoding
    assert host.model_phase("m1") == PHASE_PP  # the second one still prefills

    host.note_first_token("m1")
    assert host.model_phase("m1") == PHASE_TG
    host.end_phase("m1", PHASE_TG)
    assert host.model_phase("m1") == PHASE_TG  # one is still generating
    host.end_phase("m1", PHASE_TG)
    assert host.model_phase("m1") is None
    assert host.phases == {}                   # the entry does not linger


def test_the_host_aggregate_prefers_prefill():
    """`hosts[].phase`: prefill if any model prefills, else decode, else None."""
    host = HostRuntime(cfg=host_config("A", "http://127.0.0.1:1"),
                       client=httpx.AsyncClient())

    host.begin_phase("m1")
    host.note_first_token("m1")
    assert host.phase == PHASE_TG

    host.begin_phase("m2")
    assert host.phase == PHASE_PP              # prefill wins
    host.note_first_token("m2")
    assert host.phase == PHASE_TG              # both decoding

    host.end_phase("m1", PHASE_TG)
    assert host.phase == PHASE_TG
    host.end_phase("m2", PHASE_TG)
    assert host.phase is None


def test_ending_a_phase_twice_does_not_go_below_zero():
    """The three end signals of a stream can all fire; the counter must not
    underflow into a phase that never ends."""
    host = HostRuntime(cfg=host_config("A", "http://127.0.0.1:1"),
                       client=httpx.AsyncClient())
    host.begin_phase("m1")
    host.end_phase("m1", PHASE_PP)
    host.end_phase("m1", PHASE_PP)
    host.end_phase("m1", PHASE_TG)             # a phase it was never in
    assert host.model_phase("m1") is None
    assert host.phase is None


# -- the endpoint -----------------------------------------------------------
async def test_healthz_carries_the_phase_beside_the_lock_keys(phase_stub,
                                                              ripple_factory):
    """The #21 keys stay exactly where they were; the phase is added next to
    them, at both levels."""
    a = phase_stub(models=[StubModel(id="m1", loaded=True, estimated_size=GB)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))

    health = await harness.healthz()
    row = host_row(health, "A")
    assert row["phase"] is None and row["phase_switches"] == 0
    cell = _cell(health, "m1", "A")
    assert cell["phase"] is None
    # the lock contract of #21 is untouched by this
    assert row["inflight"] == 0
    assert "lock" in cell and "lock_reason" in health["models"][0]
    assert health["locks"] == []
    assert (await harness.stats())["requests_model_locked"] == 0


async def test_healthz_says_which_model_a_node_is_loading(phase_stub,
                                                          ripple_factory):
    """#149: `models[].hosts[].loading` names the (host, model) pair being loaded.

    A separate fact from `phase`, not a second name for it: the request that
    triggered the load does sit in `pp`, but the reason is the load and not a
    prefill of resident weights. The page blinks the dot blue from this flag and
    must not have to guess it out of `loaded` + `phase`.
    """
    a = phase_stub(models=[StubModel(id="m1", loaded=False, estimated_size=GB),
                           StubModel(id="m2", loaded=True, estimated_size=GB)])
    harness = await ripple_factory(make_config([
        host_config("A", a.url, node_id=a.state.node_id)]))
    host = harness.ripple.fleet.hosts[0]
    host.begin_loading("m1", size_gb=1.0, eta_seconds=60.0, source="dispatch")

    health = await harness.healthz()
    assert _cell(health, "m1", "A")["loading"] is True
    assert _cell(health, "m2", "A")["loading"] is False
    # the node-level dict stays the same fact it was (docs/loading-state.md)
    assert host_row(health, "A")["loading"]["model"] == "m1"
