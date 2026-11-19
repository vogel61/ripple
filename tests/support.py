"""Test support for RIPPLE: stub oMLX upstreams, config builders, small helpers.

Every upstream used by the suite is a stub FastAPI app served by uvicorn on an
ephemeral port (127.0.0.1:0), so the proxy talks real HTTP and real SSE. No test
contacts a real oMLX instance, loads a model or needs network access.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import string
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ripple.config import (AffinityConfig, AuthConfig, Config, DiscoveryConfig,
                           FleetConfig, HostConfig, LoadingConfig, PlacementConfig,
                           ProxyConfig, StatsConfig)
from ripple.proxy import Ripple

# The M5 figures from the specification (FA-18), in bytes.
CEILING = 487_700_000_000
DEEPSEEK_SIZE = 422_500_000_000
QWEN_SIZE = 103_900_000_000

_LETTERS = string.ascii_lowercase

# The calibration store is machine state (the operator's measured curve). A test
# must never read ~/.ripple/calibration.yaml: the live curve would silently change
# the estimates a test asserts on. This path never exists.
TEST_CALIBRATION_FILE = str(Path(tempfile.gettempdir()) /
                            f"ripple-test-{os.getpid()}-calibration.yaml")
# The same for the operator's model policy (issue #11): a model switched off on
# the live machine must not make a test see a different fleet.
TEST_MODEL_POLICY_FILE = str(Path(tempfile.gettempdir()) /
                             f"ripple-test-{os.getpid()}-models.yaml")
# And for the maintenance windows (issue #15): this is the one piece of state that
# takes a *production* node out of the pool. A test that sets a window must not
# write ~/.ripple/maintenance.yaml — the next start would keep a real node out.
TEST_MAINTENANCE_FILE = str(Path(tempfile.gettempdir()) /
                            f"ripple-test-{os.getpid()}-maintenance.yaml")
# And for the per-host ledger (issue #33): the live file holds the operator's
# measured history, and a test that wrote it would either inherit a fleet that is
# not its own or wipe the numbers the dashboard is meant to show.
TEST_STATS_FILE = str(Path(tempfile.gettempdir()) /
                      f"ripple-test-{os.getpid()}-host_stats.json")
# And for the runtime overlay (issue #70, #78): a test that adds or disables a
# node, or sets a policy value, must not write `~/.ripple/runtime.yaml` — the
# next start of the live service would build a fleet that no `config.yaml`
# declares. `persist: false` on top, so two tests in one run cannot read each
# other's overlay through one path.
TEST_FLEET_FILE = str(Path(tempfile.gettempdir()) /
                      f"ripple-test-{os.getpid()}-runtime.yaml")


# ---------------------------------------------------------------------------
# Stub upstream
# ---------------------------------------------------------------------------
@dataclass
class StubModel:
    """One model as a stub oMLX instance reports it."""

    id: str
    loaded: bool = False
    pinned: bool = False
    estimated_size: int = 0
    actual_size: int | None = None
    context_len: int = 32768
    last_access: float = 0.0
    engine_type: str = "llm"
    model_type: str = "llm"
    # oMLX's loading signals. `is_loading` is None by default so a test has to
    # opt in — a stub that silently reports "not loading" would hide the state
    # machine behind a false negative.
    is_loading: bool | None = None
    loading_started_at: float | None = None
    # A model oMLX refuses to load on its own (a drafter, a broken file). The
    # calibration has to skip it and keep measuring the others.
    load_fails: bool = False

    def __post_init__(self) -> None:
        if self.actual_size is None:
            self.actual_size = self.estimated_size


@dataclass
class StubState:
    """Everything a test may want to control on one stub upstream."""

    node_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    models: list[StubModel] = field(default_factory=list)
    version: str = "0.8.0rc1"
    health_status: str = "healthy"
    final_ceiling: int = CEILING
    current_model_memory: int | None = None
    chat_status: int = 200
    # What a non-streaming completion answers. Defaults keep the historical
    # fixture; a test that needs an empty answer (a reasoning model that spent
    # its whole budget, issue #82) sets both of these.
    chat_content: str = "stub answer"
    chat_finish_reason: str = "stop"
    delay: float = 0.0
    stream_chunks: int = 3
    usage_chunks_served: int = 0
    stream_chunk_delay: float = 0.0
    # Opt-in load simulation. False keeps the historical behaviour exactly: a
    # `/v1/models/{id}/load` only records the call, because most tests care about
    # routing, not about residency changing under them.
    load_simulates: bool = False
    load_delay: float = 0.0
    # The timestamp the simulated load reports as `loading_started_at`. A test
    # that wants oMLX's clock to differ from the request clock sets this into the
    # past (the calibration must measure the former).
    load_started_at: float | None = None
    # A simulated load finishes in the background, like oMLX's queued load; the
    # reference keeps the task from being collected mid-flight.
    load_tasks: list[Any] = field(default_factory=list)
    # What `GET /admin/api/activity` reports (issue #33): the node's own view of
    # what it is doing right now. `activity_models` replaces the whole list for a
    # test that needs a shape the two counters cannot express.
    activity_prefilling: int = 0
    activity_generating: int = 0
    activity_models: list[dict[str, Any]] | None = None
    # The status `/admin/api/activity` answers. 401 is what a node without the
    # right admin key really says — the ledger must then leave the tick out
    # instead of calling the node idle.
    activity_status: int = 200
    # The status `/v1/models` and `/v1/models/status` answer (issue #53). 401 is
    # what a node without the oMLX admin key says: `/health` is open, the model
    # list is not — the state RIPPLE used to read as "this node carries nothing".
    models_status_code: int = 200
    # What `GET /admin/api/usage?range=today` reports: the node's own SQLite
    # count, which the dashboard shows beside RIPPLE's ledger.
    usage_requests: int = 0
    usage_prefill_seconds: float = 0.0
    usage_generation_seconds: float = 0.0
    usage_ranges: list[str] = field(default_factory=list)
    # The Authorization header of every admin call, so a test can prove RIPPLE
    # sent the oMLX admin key and not the client key it passes through.
    admin_auth: list[str] = field(default_factory=list)

    # observations
    calls: dict[str, int] = field(default_factory=dict)
    chat_bodies: list[dict[str, Any]] = field(default_factory=list)
    auth_headers: list[str] = field(default_factory=list)
    load_calls: list[str] = field(default_factory=list)
    unload_calls: list[str] = field(default_factory=list)

    def bump(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def count(self, name: str) -> int:
        return self.calls.get(name, 0)

    @property
    def chat_calls(self) -> int:
        return self.count("chat")

    def model(self, model_id: str) -> StubModel | None:
        for m in self.models:
            if m.id == model_id:
                return m
        return None

    def start_load(self, model_id: str, *, started_at: float | None = None) -> StubModel:
        """A load that is still running: `is_loading` true, `loaded` still false."""
        m = self.model(model_id)
        if m is None:
            raise KeyError(model_id)
        m.is_loading = True
        m.loaded = False
        m.loading_started_at = started_at if started_at is not None else time.time()
        return m

    def finish_load(self, model_id: str) -> StubModel:
        """The load completed: `loaded` true, `is_loading` false."""
        m = self.model(model_id)
        if m is None:
            raise KeyError(model_id)
        m.is_loading = False
        m.loaded = True
        m.loading_started_at = None
        return m

    @property
    def memory_in_use(self) -> int:
        """`current_model_memory`, defaulting to the sum of the loaded models."""
        if self.current_model_memory is not None:
            return self.current_model_memory
        return sum(m.estimated_size for m in self.models if m.loaded)


def stub_model_loaded(model_id: str, **overrides: Any) -> StubModel:
    """A model that is resident on the stub (tier `resident`)."""
    return StubModel(id=model_id, loaded=True, **overrides)


def _completion(state: StubState, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": state.chat_content},
            "finish_reason": state.chat_finish_reason,
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    }


async def _sse(state: StubState, body: dict[str, Any]):
    """Server-sent events, one chunk per yield, with a delay in between."""
    for i in range(max(1, state.stream_chunks)):
        if i:
            await asyncio.sleep(state.stream_chunk_delay)
        chunk = {
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "model": body.get("model"),
            "choices": [{"index": 0, "delta": {"content": f"tok{i} "},
                         "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
    # Faithful to oMLX: the finish chunk carries no usage. Usage arrives in its own
    # chunk with an empty `choices` list, and only when it was asked for.
    final = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "model": body.get("model"),
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    opts = body.get("stream_options") or {}
    if isinstance(opts, dict) and opts.get("include_usage"):
        # oMLX shape: a separate chunk with no choices at all, carrying its own
        # measurements. RIPPLE must never let this reach a client that did not ask.
        state.usage_chunks_served += 1
        usage_chunk = {
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "model": body.get("model"),
            "choices": [],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                      "generation_tokens_per_second": 51.5,
                      "generation_duration": 0.058,
                      "prompt_eval_duration": 0.02,
                      "time_to_first_token": 0.02},
        }
        yield f"data: {json.dumps(usage_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


async def _finish_load_later(state: StubState, model_id: str) -> None:
    """The simulated load completes after `load_delay`, in the background.

    oMLX's `/load` answers as soon as the load is queued; a stub that blocked
    until the model was resident would hide the `is_loading` phase that the
    calibration measures from.
    """
    if state.load_delay:
        await asyncio.sleep(state.load_delay)
    state.finish_load(model_id)


def build_stub_app(state: StubState) -> FastAPI:
    """The oMLX surface RIPPLE reads: health, models, residency, identity,
    lifecycle probe, chat (JSON and SSE)."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        state.bump("health")
        loaded = [m for m in state.models if m.loaded]
        return {
            "status": state.health_status,
            "engine_pool": {
                "final_ceiling": state.final_ceiling,
                "current_model_memory": state.memory_in_use,
                "model_count": len(state.models),
                "loaded_count": len(loaded),
            },
        }

    @app.get("/v1/models")
    async def models() -> Any:
        state.bump("models")
        if state.models_status_code >= 400:
            return JSONResponse({"detail": "admin key required"},
                                status_code=state.models_status_code)
        return {"object": "list", "data": [
            {"id": m.id, "object": "model", "max_model_len": m.context_len,
             "model_type": m.model_type}
            for m in state.models
        ]}

    @app.get("/v1/models/status")
    async def models_status() -> Any:
        state.bump("status")
        if state.models_status_code >= 400:
            return JSONResponse({"detail": "admin key required"},
                                status_code=state.models_status_code)
        return {"models": [
            {"id": m.id, "loaded": m.loaded, "pinned": m.pinned,
             "estimated_size": m.estimated_size, "actual_size": m.actual_size,
             "max_context_window": m.context_len, "engine_type": m.engine_type,
             "last_access": m.last_access,
             "is_loading": m.is_loading,
             "loading_started_at": m.loading_started_at}
            for m in state.models
        ]}

    @app.get("/api/cluster/node_id")
    async def node_id() -> dict[str, Any]:
        state.bump("identity")
        return {"node_id": state.node_id, "version": state.version}

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
            return StreamingResponse(_sse(state, body),
                                     media_type="text/event-stream")
        return _completion(state, body)

    @app.post("/v1/models/{model_id}/load")
    async def load(model_id: str):
        state.load_calls.append(model_id)
        m = state.model(model_id)
        if m is not None and m.load_fails:
            # A model that cannot be loaded alone — a drafter, a broken file. oMLX
            # answers 409 with a message; the calibration must skip it, not abort.
            return JSONResponse(
                {"error": {"message": f"Model '{model_id}' is unavailable after a "
                                      f"previous load failure", "type": "invalid_request_error"}},
                status_code=409)
        if state.load_simulates:
            # The model becomes resident the way oMLX does it: `is_loading` true
            # and `loaded` still false while it works, then both flip.
            state.start_load(model_id, started_at=state.load_started_at)
            state.load_tasks.append(
                asyncio.create_task(_finish_load_later(state, model_id)))
        return {"status": "ok"}

    @app.post("/v1/models/{model_id}/unload")
    async def unload(model_id: str) -> dict[str, str]:
        state.unload_calls.append(model_id)
        if state.load_simulates:
            m = state.model(model_id)
            if m is not None:
                m.loaded = False
                m.is_loading = False
                m.loading_started_at = None
        return {"status": "ok"}

    @app.options("/v1/models/{model_id}/load")
    async def load_options(model_id: str) -> JSONResponse:
        # A route that exists answers 405 to OPTIONS — that is how RIPPLE probes
        # the lifecycle capability without loading anything.
        return JSONResponse({"detail": "method not allowed"}, status_code=405)

    # -- oMLX admin API (issue #33) ----------------------------------------
    @app.get("/admin/api/activity")
    async def activity(request: Request) -> Any:
        """The node's own view of what it is doing — one tick of the ledger.

        Shape as measured on both nodes (16.11.2026): `active_models.models[]`
        carries `is_loading`, `active_requests`, `prefilling`, `generating`.
        """
        state.bump("activity")
        state.admin_auth.append(request.headers.get("authorization", ""))
        if state.activity_status >= 400:
            return JSONResponse({"detail": "admin key required"},
                                status_code=state.activity_status)
        if state.activity_models is not None:
            models = state.activity_models
        else:
            models = [{
                "id": m.id,
                "is_loading": bool(m.is_loading),
                "active_requests": state.activity_prefilling + state.activity_generating,
                "prefilling": [{"request": i} for i in range(state.activity_prefilling)],
                "generating": [{"request": i} for i in range(state.activity_generating)],
                "idle_seconds": 0.0,
            } for m in state.models]
        return {"active_models": {
            "models": models,
            "total_active_requests": state.activity_prefilling + state.activity_generating,
        }}

    @app.get("/admin/api/usage")
    async def usage(request: Request, range: str = "today") -> dict[str, Any]:
        """The node's own SQLite count — the cross-check under the ledger."""
        state.bump("usage")
        state.admin_auth.append(request.headers.get("authorization", ""))
        state.usage_ranges.append(range)
        return {"range": range, "totals": {
            "requests": state.usage_requests,
            "prefill_seconds": state.usage_prefill_seconds,
            "generation_seconds": state.usage_generation_seconds,
        }}

    return app


class StubUpstream:
    """A stub oMLX instance in a background thread."""

    def __init__(self, state: StubState | None = None) -> None:
        self.state = state or StubState()
        self.app = build_stub_app(self.state)
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "StubUpstream":
        server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_config=None,
            access_log=False, lifespan="off", timeout_graceful_shutdown=1))
        thread = threading.Thread(target=server.run, name="stub-upstream",
                                  daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("stub upstream failed to start")
            time.sleep(0.005)
        self._server, self._thread = server, thread
        self.port = server.servers[0].sockets[0].getsockname()[1]
        return self

    def stop(self) -> None:
        """Stop serving; the port stays known so the config still points at it."""
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("stub upstream was never started")
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self._server is not None


def closed_port() -> int:
    """A TCP port on 127.0.0.1 that nothing listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Configuration builders
# ---------------------------------------------------------------------------
def host_config(name: str, addresses: str | list[str], *, node_id: str | None = None,
                weight: float = 1.0, **overrides: Any) -> HostConfig:
    if isinstance(addresses, str):
        addresses = [addresses]
    return HostConfig(name=name, addresses=list(addresses), node_id=node_id,
                      weight=weight, **overrides)


def make_config(hosts: list[HostConfig], *,
                affinity: AffinityConfig | None = None,
                placement: PlacementConfig | None = None,
                discovery: DiscoveryConfig | None = None,
                proxy: ProxyConfig | None = None,
                sites: list[Any] | None = None) -> Config:
    cfg = Config(hosts=hosts)
    if sites is not None:
        # Named locations (issue #67). Not a default: a test that wants a site
        # declares one, so no test inherits a location it did not write.
        cfg.sites = list(sites)
    if affinity is not None:
        cfg.affinity = affinity
    if placement is not None:
        cfg.placement = placement
    # Never read the operator's real curve (see TEST_CALIBRATION_FILE). A test that
    # wants its own store passes one explicitly, and keeps it.
    if cfg.placement.loading.calibration_file == LoadingConfig().calibration_file:
        cfg.placement.loading = replace(cfg.placement.loading,
                                        calibration_file=TEST_CALIBRATION_FILE)
    if cfg.placement.model_policy_file == PlacementConfig().model_policy_file:
        cfg.placement = replace(cfg.placement, model_policy_file=TEST_MODEL_POLICY_FILE)
    if cfg.placement.maintenance_file == PlacementConfig().maintenance_file:
        cfg.placement = replace(cfg.placement, maintenance_file=TEST_MAINTENANCE_FILE)
    # The per-host ledger (issue #33). Both halves matter: the path, so a test
    # never writes the operator's history, and `persist: false`, so two tests in
    # one run cannot read each other's counters through the same file. A test
    # that wants the file passes its own `stats` block with a tmp_path.
    if cfg.stats.file == StatsConfig().file:
        cfg.stats = replace(cfg.stats, file=TEST_STATS_FILE, persist=False)
    # The runtime overlay (issue #70, #78). The path so a test never writes the
    # operator's overlay, and `persist: false` so no two tests share one file — a
    # test that wants the file (a restart) passes its own `fleet` block.
    if cfg.fleet.file == FleetConfig().file:
        cfg.fleet = replace(cfg.fleet, file=TEST_FLEET_FILE, persist=False)
    # The background refresh must not change state in the middle of a test.
    cfg.discovery = discovery or DiscoveryConfig(interval_seconds=3600.0,
                                                 timeout_seconds=2.0)
    if proxy is not None:
        cfg.proxy = proxy
    return cfg


def keyed_config(hosts: list[HostConfig], keys_file: str, *,
                 affinity: AffinityConfig | None = None) -> Config:
    """A config with client auth on and its key store at `keys_file`.

    The store need not exist yet: `KeyStore` reads a missing file as an empty
    store, so a test creates its keys afterwards with
    `harness.ripple.keys.create(name, kind)` and gets the secret back.
    """
    cfg = make_config(hosts, affinity=affinity)
    cfg.auth = AuthConfig(enabled=True, keys_file=keys_file, admin_key="test-admin-key")
    return cfg


def bearer(token: str) -> dict[str, str]:
    """The header a client key travels in."""
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
def chat_body(model: str = "m1", prompt: str = "hello", *, turns: int = 2,
              stream: bool = False, **extra: Any) -> dict[str, Any]:
    """An OpenAI-shaped request body.

    `turns` is the number of user messages: 1 makes it a short single-message
    request (ephemeral, FA-7), 2 or more keeps a thread slot (FA-6/FA-19).
    """
    messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
    for i in range(1, max(1, turns)):
        messages.append({"role": "assistant", "content": f"answer {i}"})
        messages.append({"role": "user", "content": f"follow-up {i}"})
    body: dict[str, Any] = {"model": model, "messages": messages}
    if stream:
        body["stream"] = True
    body.update(extra)
    return body


def prompt_for(index: int) -> str:
    """A distinct opening prompt per thread. The first characters differ, so no
    two threads can be confused by the prefix similarity of FA-6."""
    return f"{_LETTERS[index % len(_LETTERS)]}{index:02d} conversation about topic {index}"


# ---------------------------------------------------------------------------
# /healthz readers
# ---------------------------------------------------------------------------
def host_row(health: dict[str, Any], name: str) -> dict[str, Any]:
    for row in health["hosts"]:
        if row["name"] == name:
            return row
    raise AssertionError(f"host {name!r} is missing from /healthz")


def slots(health: dict[str, Any]) -> dict[str, int]:
    return {row["name"]: row["thread_slots"] for row in health["hosts"]}


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0,
                     interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not reached within the timeout")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class Harness:
    """A running RIPPLE instance plus an in-process client for its ASGI app."""

    def __init__(self, ripple: Ripple, client: httpx.AsyncClient) -> None:
        self.ripple = ripple
        self.client = client

    async def chat(self, body: dict[str, Any], headers: dict[str, str] | None = None
                   ) -> httpx.Response:
        return await self.client.post("/v1/chat/completions", json=body,
                                      headers=headers or {})

    async def ok(self, body: dict[str, Any], headers: dict[str, str] | None = None
                 ) -> httpx.Response:
        resp = await self.chat(body, headers)
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        return resp

    async def healthz(self) -> dict[str, Any]:
        resp = await self.client.get("/healthz")
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def stats(self) -> dict[str, Any]:
        resp = await self.client.get("/stats")
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def slots(self) -> dict[str, int]:
        return slots(await self.healthz())

    def host(self, name: str):
        host = self.ripple.fleet.get(name)
        assert host is not None, f"unknown host {name!r}"
        return host
